"""Pipeline scheduler -- fires registered pipelines on their cron schedules.

A single long-running container replaces per-service manual ``docker compose
run`` invocations (and host-level Windows/cron entries).  It reads the same
pipeline registry every service already consults -- Nacos first, the
version-controlled seed file as fallback (spec section 3, see
``pipeline_config.py``) -- so enabling/disabling a line or retuning its cron
in Nacos takes effect without restarting anything (configs are re-resolved
every tick).

Design notes:

* Commands mirror the one-shot services in ``docker-compose.integration.yml``
  verbatim; the scheduler container carries the union of their mounts and
  passes its environment through to each child, adding only the line's own
  ``PUBLIC_DATA_SERVICE_ID`` (and ``ROBOT_REGION`` for business lines) so the
  per-service registry enable gate keeps working unchanged.
* Each fire holds a MySQL named lock (``named_lock`` in ``db.py``) for the
  whole run.  The lock is non-blocking: if a previous run (or another
  scheduler replica) still holds it, the tick is skipped -- fail closed.
* ``project-mart`` stays unschedulable: ``rebuild-projection`` requires an
  explicit ``SYNC_RUN_ID`` and remains a manual/repair tool.  If someone
  puts a cron on it in the registry the scheduler logs a WARNING and skips.
* By default there is no catch-up beyond the current minute: the next fire
  is computed from ``now - 60s``, so a scheduler that comes up at 02:00:05
  still honours the 02:00 window, but one that was down since 01:00 does not
  retroactively fire hours-old misses at 09:00.  ``AUTO_CATCHUP_ENABLE=1``
  explicitly opts into at most one recent missed window per service, bounded
  by the last heartbeat and ``AUTO_CATCHUP_MAX_AGE_SECONDS``.
* Automatic recovery is off by default.  ``AUTO_RETRY_ENABLE=1`` retries
  retryable non-zero exits with bounded exponential backoff;
  ``AUTO_REPLAY_ENABLE=1`` replays only runs confirmed not to have started
  (lock acquisition failed).  Runner exceptions have an unknown outcome and
  are never replayed.  Every logical run keeps a deterministic run id for
  in-process dedup, and recovery reuses the normal command/dependency/lock
  gates without adding ``--force``.
* Command templates are generic for the ``robot-<region>`` /
  ``pages-<region>`` / ``leaderboard-<region>`` families
  (:func:`command_spec`): a new region can be
  registered in Nacos (e.g. via ops-web) and scheduled without a code
  change -- the region suffix is validated and passed to ``mart_cli``,
  which owns region legitimacy at runtime.
* Fleet enumeration re-resolves every tick from *service_ids* when it is a
  callable (:func:`build_fleet_source`): seed keys union the Nacos
  ``PIPELINES`` group listing, so ops-web additions take effect without a
  scheduler restart (Nacos listing failures degrade to seed keys).
* 「立即运行一次」channel: when *run_requests* is injected, each tick also
  drains ``pd_ops_run_request`` pending rows (ops-web writes them), firing
  them through the same lock/executor path as cron fires.
* 执行流水 channel: when *run_history* is injected, every actual fire
  (cron and run-once alike, same ``_run`` path) writes a running row to
  ``pd_ops_run_history`` before lock acquisition and finishes it with the
  exit code afterwards -- lock-unavailable / crash (no exit code) lands as
  failed.  History is a side-channel: a DB failure there only logs a
  WARNING and never blocks the pipeline itself.

All collaborators (config source, runner, lock factory, executor, clock,
sleeper) are injected, so unit tests need no Nacos, MySQL or subprocess.
"""

import contextlib
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common.public_data.pipeline_config import (
    DEFAULT_GROUP,
    PipelineConfig,
    build_config_source,
    load_seed,
)

logger = logging.getLogger(__name__)

#: Services whose registry ``schedule`` is honoured, mapped to the argv that
#: reproduces their compose one-shot command (``sys.executable -m`` prefix is
#: added by the runner).  ``extra_env`` is merged into the child environment.
#: ``{credentials}`` / ``{output_dir}`` are filled from scheduler settings.
def _sync_entry(*extra):
    """apps 线同步命令：live-sync 公共骨架 + 源差异参数。"""
    return {
        "argv": [
            "common.public_data.cli", "live-sync", "--live-read",
            "--confirm-local-test-write", *extra,
            "--source-credentials", "{credentials}",
        ],
    }


def _mart_cli_entry(subcommand, *extra, region=None):
    """业务线命令：mart_cli 子命令 + 可选 ROBOT_REGION 注入。"""
    entry = {
        "argv": [
            "common.daily_robot.mart_cli", subcommand,
            "--confirm-local-test-write", *extra,
        ],
    }
    if region is not None:
        entry["extra_env"] = {"ROBOT_REGION": region}
    return entry


def _extract_entry(*extra):
    """apps 线提取命令：extract-mart 公共骨架 + 数据集过滤参数。"""
    return {
        "argv": [
            "common.public_data.cli", "extract-mart",
            "--confirm-local-test-write", *extra,
        ],
    }


def _data_quality_entry(dataset):
    return {
        "argv": [
            "common.public_data.cli", "data-quality-check",
            "--confirm-local-test-write", "--dataset", dataset,
        ],
    }


def _pages_entry(region, *extra):
    return _mart_cli_entry(
        "leaderboard-html",
        "--output", f"{{output_dir}}/{region}.html",
        *extra,
        region=region,
    )


#: 日报机器人区域（robot-<region> → mart_cli remind 填报提醒；
#: robot-check-<region> → mart_cli check 催办未填人+DING，
#: 2026-10-07 裁决：check 从 once 小时分流拆出，独立注册/调度/可手动运行）
_ROBOT_REGIONS = ("hangzhou", "vanke", "shaoxing", "junpin", "qudao", "offline_all")

#: 榜单页区域（pages-<region> → mart_cli leaderboard-html）
_PAGES_REGIONS = (
    "hangzhou", "vanke", "shaoxing", "junpin", "qudao", "offline_all",
)

#: 榜单群播报区域（leaderboard-<region> → mart_cli leaderboard）。
#: 上云切换漏配（2026-10-07 查实）：mart_cli leaderboard 子命令一直存在，
#: 但旧 Windows cron「8:30 榜单」停后无任何注册线触发，09-23 起五区域
#: 榜单群播报全停；此处补齐，播报时间由 ops-web 注册表单线调整。
_LEADERBOARD_REGIONS = ("hangzhou", "shaoxing", "vanke", "qudao", "offline_all")

_COMMAND_TABLE = {
    "sync-dingtalk": _sync_entry("--source", "dingtalk"),
    "sync-wdt": _sync_entry("--source", "wdt"),
    # 知识库商品同步（2026-10-10 客服问答机器人 P1）：AI 表「产品资料」→
    # mart_ops.kb_products；数据层幂等（recordId upsert），手动触发无需 --force。
    "kb-sync-products": {
        "argv": [
            "common.kb_qa.sync_products",
            "--live-read",
            "--confirm-local-test-write",
        ],
    },
    # 知识库文档同步（2026-10-21，wiki OpenAPI 已授权）：遍历知识库 →
    # blocks→markdown → 双写清单 JSON（rag-index-docs 灌向量用）与
    # kb_doc_chunks（BM25 通路，按 doc_id 先删后灌）。幂等，手动触发
    # 无需 --force。
    "kb-sync-wiki": {
        "argv": [
            "common.kb_qa.sync_wiki",
            "--live-read",
            "--confirm-local-test-write",
        ],
    },
    # RAG 向量索引（2026-10-12 第一期，商品+图片跨模态）：AI 表「产品资料」→
    # jina-clip-v2 embed → Milvus Lite；数据层幂等（id 先 delete 后 insert）。
    "rag-index-products": {
        "argv": [
            "common.rag.index_products",
            "--live-read",
            "--confirm-local-test-write",
        ],
    },
    # RAG 第二期（2026-10-19）：知识库文档切片 embedding → 同一个 collection。
    # 输入是本地清单 JSON（默认 /opt/dops/config-local/kb_docs.json，可用
    # env RAG_DOCS_INPUT 覆盖），无外部调用故走 business 门禁；缺 key/缺文件
    # 静默 skipped。幂等（按 doc:{doc_id}: 前缀先清），手动触发无需 --force。
    "rag-index-docs": {
        "argv": [
            "common.rag.index_docs",
            "--confirm-local-test-write",
        ],
    },
    "roll-manifest": {
        "argv": [
            "common.public_data.cli", "roll-manifest",
            "--confirm-local-test-write",
        ],
    },
    "extract-mart": _extract_entry(),
    "dq-rerun-worker": {
        "argv": [
            "common.public_data.cli", "data-quality-rerun",
            "--confirm-local-test-write",
        ],
    },
    "dq-check-channel": _data_quality_entry("channel_daily_sales"),
    "dq-check-wdt": _data_quality_entry("wdt_order_line_fact"),
    # 渠道日销 T+1 采集窗口（10:31 补采 → 10:35 提取 → 10:40 到齐校验 →
    # 10:45 页面重算；方案 docs/渠道日销T+1采集与门店到齐催办方案-2026-09-28.md）。
    "sync-channel-sales": _sync_entry(
        "--source", "dingtalk",
        "--dataset", "channel_daily_sales*",
        "--dataset", "channel_monthly_target",
    ),
    "extract-channel": _extract_entry(
        "--dataset", "channel_daily_sales",
        "--dataset", "channel_monthly_target",
    ),
    # 渠道日销·订单口径（2026-10-23 第 3 步）：fact_order_line →
    # fact_channel_order_daily。数据层按 (business_date, channel_key) 幂等
    # upsert，手动触发无需 --force。放在 extract-mart 之后（17:00）跑。
    "channel-order-daily": {
        "argv": [
            "common.public_data.channel_order_daily",
            "--live-read",
            "--confirm-local-test-write",
        ],
    },
    "channel-missing-check": _mart_cli_entry("channel-missing", region="qudao"),
    # 10:45 T+1 重算用默认日期（今天）：渠道板块锚定「business_date 之前
    # 最近有效日」= T-1，带 --date yesterday 反而退到 T-2（2026-09-28 裁决，
    # 偏离执行提示词 §2.4——其 yesterday 前提与 resolve_channel_business_date
    # 的既有锚定语义冲突）。
    "pages-qudao-t1": _pages_entry("qudao"),
    **{f"robot-{r}": _mart_cli_entry("remind", region=r) for r in _ROBOT_REGIONS},
    **{
        f"robot-check-{r}": _mart_cli_entry("check", region=r)
        for r in _ROBOT_REGIONS
    },
    "channel-daily-qudao": _mart_cli_entry("channel-daily", region="qudao"),
    **{f"pages-{r}": _pages_entry(r) for r in _PAGES_REGIONS},
    **{
        f"leaderboard-{r}": _mart_cli_entry("leaderboard", region=r)
        for r in _LEADERBOARD_REGIONS
    },
    "offline-daily-summary": _mart_cli_entry("offline-daily", region="offline_all"),
    # 月目标双管道（2026-10-09 裁决「结转+月度提醒」）：跨区域任务，
    # 不注入 ROBOT_REGION；remind 的投递 region 由各 scope 自身决定。
    "target-rollover": _mart_cli_entry("target-rollover"),
    "target-remind": _mart_cli_entry("target-remind"),
    # 工作日历覆盖看门（2026-10-10 三层防线 B 层）：跨区域告警，
    # 不注入 ROBOT_REGION（投递 region 固定 offline_all）。
    "calendar-watch": _mart_cli_entry("calendar-watch"),
    # L3 数据新鲜度看门狗（A3）：跨区域，投递固定 region=ops；
    # 依赖 extract-channel 完成后触发，避免早于数据落地误报。
    "freshness-check": _mart_cli_entry("freshness-check"),
    # 统一数据集新鲜度检查（2026-10-29 自动化闭环 P0）：按数据集契约检查
    # raw 到达、mart 发布、业务窗口完整性与源目标对账，结果落
    # pd_dataset_freshness。与既有 L3 看门狗（freshness-check）并行运行。
    "dataset-freshness-check": {
        "argv": [
            "common.public_data.cli", "freshness-check",
            "--confirm-local-test-write",
        ],
    },
    "kb-export-cleanup": {
        "argv": [
            "common.public_data.cli", "kb-export-cleanup",
            "--confirm-local-test-write",
        ],
    },
    # 舰队月检（2026-10-17 治理 P2）：四节体检快照落 pd_ops_fleet_audit，
    # 产出只进 ops-web 治理抽屉（用户裁决：治理产出不发群播报）。
    "fleet-audit": {
        "argv": [
            "common.public_data.fleet_audit",
            "--confirm-local-test-write",
        ],
    },
}

#: Registered in the seed with a cron but intentionally not schedulable
#: (parameterized repair tool, see module docstring).
_UNSCHEDULABLE = ("project-mart",)

#: robot-/pages- 家族泛化解析的 region 后缀形态（小写字母/数字/下划线，
#: 字母或数字开头——拒绝中划线，避免 pages-qudao-t1 这类显式条目被
#: 家族规则误吞；显式表永远优先）。
_REGION_SUFFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


def command_spec(service_id):
    """调度命令模板：显式命令表优先，robot-/pages- 家族按后缀泛化解析。

    泛化（2026-09-30，ops-web「可添加」A 方案）：``robot-<region>`` →
    ``mart_cli remind``、``robot-check-<region>`` → ``mart_cli check``
    （2026-10-07 拆分）、``pages-<region>`` → ``leaderboard-html``，新区域
    注册即可调度、无需改代码；region 合法性由 mart_cli 运行时校验
    （未知 region 子进程非零退出，日志可观测）。无模板返回 None。
    """
    spec = _COMMAND_TABLE.get(service_id)
    if spec is not None:
        return spec
    for prefix, builder in (
        # robot-check- 必须先于 robot-（同前缀，长者优先）。
        ("robot-check-", lambda region: _mart_cli_entry("check", region=region)),
        ("robot-", lambda region: _mart_cli_entry("remind", region=region)),
        ("pages-", _pages_entry),
        ("leaderboard-", lambda region: _mart_cli_entry("leaderboard", region=region)),
    ):
        if service_id.startswith(prefix):
            region = service_id[len(prefix):]
            if _REGION_SUFFIX_RE.match(region):
                return builder(region)
            return None
    return None


def has_command(service_id):
    """service_id 是否有可调度的命令模板（ops-web 写面校验用）。"""
    return command_spec(service_id) is not None

DEFAULT_POLL_SECONDS = 30.0
_LOCK_PREFIX = "public-data-scheduler"

#: Grace window for honouring the current cron minute (see module docstring).
_FIRE_GRACE_SECONDS = 60

#: Recovery defaults are deliberately conservative.  The three feature flags
#: require an explicit truthy value; numeric knobs only bound an opted-in run.
DEFAULT_AUTO_RETRY_MAX_ATTEMPTS = 3
DEFAULT_AUTO_RETRY_BACKOFF_SECONDS = 60.0
DEFAULT_AUTO_CATCHUP_MAX_AGE_SECONDS = 3600.0
_MAX_AUTO_RETRY_ATTEMPTS = 10
_PERMANENT_EXIT_CODES = frozenset({2, 64, 78, 126, 127})


def _env_flag(env, name):
    return (env.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_number(env, name, default, cast, *, minimum, maximum):
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        logger.warning("%s 配置非法，使用安全默认值", name)
        return default
    return min(maximum, max(minimum, value))


@dataclass(frozen=True)
class SchedulerSettings:
    """Runtime settings resolved from the environment."""

    credentials_path: str = "/run/live-input/source-credentials.json"
    output_dir: str = "/output"
    poll_seconds: float = DEFAULT_POLL_SECONDS
    #: roll-manifest 依赖探针盯的 live manifest 路径（见 _roll_manifest_probe）。
    manifest_path: str = ""
    heartbeat_path: str = ""
    auto_retry_enable: bool = False
    auto_catchup_enable: bool = False
    auto_replay_enable: bool = False
    #: 总尝试次数（含首次），不是首次之外的额外重试次数。
    auto_retry_max_attempts: int = DEFAULT_AUTO_RETRY_MAX_ATTEMPTS
    auto_retry_backoff_seconds: float = DEFAULT_AUTO_RETRY_BACKOFF_SECONDS
    auto_catchup_max_age_seconds: float = DEFAULT_AUTO_CATCHUP_MAX_AGE_SECONDS

    @staticmethod
    def from_env(environ=None):
        env = os.environ if environ is None else environ
        poll = (env.get("PUBLIC_DATA_SCHEDULER_POLL_SECONDS") or "").strip()
        return SchedulerSettings(
            credentials_path=(
                env.get("PUBLIC_DATA_LIVE_CREDENTIALS_PATH")
                or SchedulerSettings.credentials_path
            ),
            output_dir=(
                env.get("PUBLIC_DATA_PAGES_OUTPUT_DIR")
                or SchedulerSettings.output_dir
            ),
            poll_seconds=float(poll) if poll else DEFAULT_POLL_SECONDS,
            manifest_path=(
                (env.get("PUBLIC_DATA_LIVE_MANIFEST_PATH") or "").strip()
                or (env.get("PUBLIC_DATA_CONFIG") or "").strip()
            ),
            heartbeat_path=(env.get("SCHEDULER_HEARTBEAT_PATH") or "").strip(),
            auto_retry_enable=_env_flag(env, "AUTO_RETRY_ENABLE"),
            auto_catchup_enable=_env_flag(env, "AUTO_CATCHUP_ENABLE"),
            auto_replay_enable=_env_flag(env, "AUTO_REPLAY_ENABLE"),
            auto_retry_max_attempts=_env_number(
                env, "AUTO_RETRY_MAX_ATTEMPTS",
                DEFAULT_AUTO_RETRY_MAX_ATTEMPTS, int,
                minimum=1, maximum=_MAX_AUTO_RETRY_ATTEMPTS,
            ),
            auto_retry_backoff_seconds=_env_number(
                env, "AUTO_RETRY_BACKOFF_SECONDS",
                DEFAULT_AUTO_RETRY_BACKOFF_SECONDS, float,
                minimum=0.0, maximum=86400.0,
            ),
            auto_catchup_max_age_seconds=_env_number(
                env, "AUTO_CATCHUP_MAX_AGE_SECONDS",
                DEFAULT_AUTO_CATCHUP_MAX_AGE_SECONDS, float,
                minimum=0.0, maximum=86400.0,
            ),
        )


def write_heartbeat(path):
    if not path:
        return
    target = Path(path)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(str(time.time()), encoding="ascii")
    os.replace(temporary, target)


def build_argv(service_id, settings):
    """Full argv (including ``python -m``) for *service_id*, or None."""
    spec = command_spec(service_id)
    if spec is None:
        return None
    argv = [sys.executable, "-m"]
    argv.extend(
        arg.format(
            credentials=settings.credentials_path,
            output_dir=settings.output_dir,
        )
        for arg in spec["argv"]
    )
    return argv


#: run-once 手动触发可附加 ``--force`` 的 mart_cli 子命令（2026-10-08 裁决
#: 「手动触发不应该被幂等」）：这些是 outbox 按 ``region:kind:date`` 去重
#: 的线——cron 窗口已发时手动触发会被 already_sent 静默吞掉；--force 以
#: ``manual-<时间戳>`` 后缀另起去重键强制重发（历史行保留作审计）。
#: leaderboard / channel-daily 自带 HHMM 后缀天然可重跑，leaderboard-html
#: 写页面文件无去重语义，同步/加工线幂等由数据层承载，均不在列。
_FORCEABLE_MART_SUBCOMMANDS = frozenset({
    "remind", "check", "once",
    "offline-daily",
    "channel-missing", "target-remind", "calendar-watch", "freshness-check",
})


def with_run_force(argv):
    """run-once 手动触发：outbox 去重线附加 ``--force``（明确意图重发）。"""
    if (
        argv is not None
        and len(argv) >= 4
        and argv[2] == "common.daily_robot.mart_cli"
        and argv[3] in _FORCEABLE_MART_SUBCOMMANDS
    ):
        return [*argv, "--force"]
    return argv


def build_child_env(service_id, environ=None):
    """Child environment: pass-through + the line's own registry identity."""
    env = dict(os.environ if environ is None else environ)
    env["PUBLIC_DATA_SERVICE_ID"] = service_id
    spec = command_spec(service_id) or {}
    env.update(spec.get("extra_env") or {})
    return env


class SubprocessRunner:
    """Runs one pipeline to completion as a child process (blocking)."""

    def __init__(self, environ=None):
        self._environ = environ

    def __call__(self, service_id, argv):
        completed = subprocess.run(
            argv, env=build_child_env(service_id, self._environ), check=False
        )
        return completed.returncode


class ThreadExecutor:
    """Production executor: each fire on its own daemon thread."""

    def launch(self, fn):
        thread = threading.Thread(target=fn, daemon=True)
        thread.start()


class SyncExecutor:
    """Test/debug executor: runs *fn* inline (deterministic, no threads)."""

    def launch(self, fn):
        fn()


class NullLock:
    """No-op lock (tests; production uses the MySQL named lock factory)."""
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def build_mysql_lock_factory(connect, database_settings):
    """Lock factory backed by a MySQL named lock on the mart database.

    Each acquisition opens a fresh connection and holds it for the whole
    run (the lock dies with the connection, so a crashed run never leaves a
    stale lock).  Non-blocking: timeout 0 -- the caller skips the tick when
    the lock is held elsewhere.
    """

    def factory(service_id):
        connection = connect(database_settings)
        lock_scope = (
            "kb-rag-milvus"
            if service_id in {"rag-index-docs", "rag-index-products"}
            else service_id
        )
        lock_name = f"{_LOCK_PREFIX}:{lock_scope}"
        return _MysqlLock(connection, lock_name)

    return factory


class _MysqlLock:
    def __init__(self, connection, lock_name):
        self._connection = connection
        self._lock_name = lock_name
        self._guard = None

    def __enter__(self):
        from common.public_data.db import named_lock

        self._guard = named_lock(self._connection, self._lock_name, timeout_seconds=0)
        self._guard.__enter__()
        return self

    def __exit__(self, *exc):
        try:
            return self._guard.__exit__(*exc)
        finally:
            self._connection.close()


def _cron_segments(schedule):
    """``;`` 分隔的多段 cron → 去空白/去空的段元组；非字符串 → 空元组。

    播报时间可配多个时点（2026-10-13 裁决 ≤3），底层 ``schedule`` 以 ``;``
    连接各时段的 5 段 cron；单段是退化形态（长度 1）。
    """
    if not isinstance(schedule, str):
        return ()
    return tuple(seg.strip() for seg in schedule.split(";") if seg.strip())


def _next_fire(schedule, now):
    """Next fire time strictly after *now*; None when any cron segment is invalid.

    多段（``;`` 分隔）取各段**最早**的下一次点火；任一段非法则整条按非法
    处理（None），调度器据此跳过该线——与单段的既有关闭语义一致。
    """
    try:
        from croniter import croniter
    except ImportError:  # pragma: no cover - dependency is pinned
        raise RuntimeError("croniter is required for the scheduler")
    segments = _cron_segments(schedule)
    if not segments:
        return None
    try:
        return min(
            croniter(segment, now).get_next(datetime) for segment in segments
        )
    except (ValueError, KeyError):
        return None


def _previous_fire(schedule, now):
    """Latest cron window strictly before *now*; invalid schedules return None."""
    try:
        from croniter import croniter
    except ImportError:  # pragma: no cover - dependency is pinned
        raise RuntimeError("croniter is required for the scheduler")
    segments = _cron_segments(schedule)
    if not segments:
        return None
    try:
        return max(
            croniter(segment, now).get_prev(datetime) for segment in segments
        )
    except (ValueError, KeyError):
        return None


def _read_heartbeat(path, now):
    """Read a heartbeat as *now*'s datetime shape; bad input fails closed."""
    if not path:
        return None
    try:
        timestamp = float(Path(path).read_text(encoding="ascii").strip())
        if now.tzinfo is None:
            return datetime.fromtimestamp(timestamp)
        return datetime.fromtimestamp(timestamp, tz=now.tzinfo)
    except (OSError, ValueError, OverflowError):
        return None


#: 摘要型依赖探针登记：service_id -> (source_name, dataset_name LIKE 模式,
#: 北京窗口时刻)。今日（北京）窗口时刻之后存在 completed run 的匹配摘要
#: 即视为上游已就绪；时间列存 UTC，阈值按 UTC+8 折算。
_DEP_SUMMARY_PROBES = {
    "sync-channel-sales": ("dingtalk", "channel_daily_sales%", (10, 30)),
    "extract-channel": ("extract", "ecom_people", (10, 34)),
    # pages-*（08:30 榜单页）依赖门禁的上游（2026-10-16 画布拉线首用）：
    # 07:00 批次的 extract 完成才生成页面——失败时暂缓+补发，治停机日
    # 页面静默陈旧。daily_report_offline 是每次 extract 的核心数据集。
    "extract-mart": ("extract", "daily_report_offline", (7, 0)),
}

#: 无 sync 摘要的知识任务以运行流水判断依赖；失败/降级非零退出时不会放行。
_DEP_HISTORY_PROBES = {
    "kb-sync-products": (3, 0),
    "kb-sync-wiki": (3, 5),
}


def build_summary_dep_probe(
    connect, database_settings, *, source_name, dataset_like, beijing_hm,
    now=None,
):
    """依赖探针工厂：上游线今日（北京）窗口时刻后已有 completed 摘要。

    任何 DB 异常一律 fail-closed 返回 False（宁可暂缓触发，不错过序——
    下一 tick 会重查）。*now* 可注入便于测试（默认 UTC 实时钟）。
    """
    clock = now or (lambda: datetime.now(timezone.utc))

    def probe():
        try:
            beijing_now = clock() + timedelta(hours=8)
            hour, minute = beijing_hm
            threshold_utc = beijing_now.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            ) - timedelta(hours=8)
            connection = connect(database_settings)
            try:
                with contextlib.closing(connection.cursor()) as cursor:
                    cursor.execute(
                        "SELECT 1 AS hit FROM `sync_dataset_summary` s "
                        "JOIN `sync_runs` r "
                        "ON r.`sync_run_id` = s.`sync_run_id` "
                        "WHERE r.`status` = 'completed' "
                        "AND s.`source_name` = %s "
                        "AND s.`dataset_name` LIKE %s "
                        "AND s.`completed_at` >= %s LIMIT 1",
                        (source_name, dataset_like, threshold_utc),
                    )
                    return cursor.fetchone() is not None
            finally:
                connection.close()
        except Exception:
            logger.warning("依赖探针查询异常，按未满足处理", exc_info=True)
            return False

    return probe


def build_history_dep_probe(
    connect, database_settings, *, service_id, beijing_hm, now=None,
):
    """要求上游在当日指定时刻后有成功运行流水，异常一律 fail-closed。"""
    clock = now or (lambda: datetime.now(timezone.utc))

    def probe():
        try:
            beijing_now = clock() + timedelta(hours=8)
            hour, minute = beijing_hm
            threshold_utc = beijing_now.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            ) - timedelta(hours=8)
            connection = connect(database_settings)
            try:
                with contextlib.closing(connection.cursor()) as cursor:
                    cursor.execute(
                        "SELECT 1 AS hit FROM `pd_ops_run_history` "
                        "WHERE `service_id` = %s AND `status` = 'finished' "
                        "AND `exit_code` = 0 AND `finished_at` >= %s LIMIT 1",
                        (service_id, threshold_utc),
                    )
                    return cursor.fetchone() is not None
            finally:
                connection.close()
        except Exception:
            logger.warning("运行流水依赖探针异常，按未满足处理", exc_info=True)
            return False

    return probe


def _roll_manifest_probe(settings, *, now):
    """roll-manifest 今日已成功：live manifest 今日被原子重写。

    roll-manifest 不碰 DB，其唯一可信成功信号是 manifest 文件本身——
    原子写（临时文件 + os.replace）只在成功时落盘，失败不会改 mtime。
    读不到文件/未配置路径一律按未满足（fail-closed：窗口不滚动时
    sync-wdt 会幂等重拉同一天，2026-09-23 生产实锤，宁可等不可错拉）。
    """
    path = (settings.manifest_path or "").strip()
    if not path:
        return False
    try:
        mtime = datetime.fromtimestamp(Path(path).stat().st_mtime)
    except OSError:
        return False
    return mtime.date() == now.date()


class Scheduler:
    """Polls the registry and fires pipelines whose cron is due.

    *config_source* resolves each service's :class:`PipelineConfig` (Nacos
    with seed fallback); *service_ids* enumerates the fleet -- either a
    fixed tuple (tests) or a callable re-invoked every tick (production:
    :func:`build_fleet_source`, so Nacos-only additions are picked up
    without a restart).  *run_requests* (optional) is the 「立即运行一次」
    store drained every tick.  *runner* / *lock_factory* / *executor* /
    *clock* / *sleeper* are all injectable -- see module docstring for the
    no-catch-up policy.
    """

    def __init__(self, *, config_source, service_ids, runner, lock_factory,
                 settings=None, executor=None, clock=None, sleeper=None,
                 poll_seconds=DEFAULT_POLL_SECONDS, dep_probe=None,
                 dep_probes=None, run_requests=None, run_history=None,
                 dead_letter_store=None):
        self._config_source = config_source
        if callable(service_ids):
            self._fleet_source = service_ids
            self._service_ids = tuple(service_ids())
        else:
            self._fleet_source = None
            self._service_ids = tuple(service_ids)
        self._runner = runner
        self._run_requests = run_requests
        self._run_history = run_history
        self._dead_letter_store = dead_letter_store
        self._lock_factory = lock_factory
        self._settings = settings or SchedulerSettings.from_env()
        self._executor = executor or ThreadExecutor()
        self._clock = clock or datetime.now
        self._sleeper = sleeper
        self._poll_seconds = poll_seconds
        self._dep_probe = dep_probe or self._default_dep_probe
        #: 按 service_id 的专用依赖探针（摘要型，main() 按
        #: _DEP_SUMMARY_PROBES 接线）；未登记的依赖走 _default_dep_probe。
        self._dep_probes = dict(dep_probes or {})
        self._configs = {}     # service_id -> PipelineConfig (last known good)
        self._next_fire = {}   # service_id -> datetime
        self._running = set()  # service_ids with a run in flight
        self._warned = set()   # one-shot WARNING dedup
        #: 自动恢复均为进程内状态；run_id 防止同一逻辑窗口被重复点燃。
        self._run_states = {}  # run_id -> running / finished / failed
        self._pending_recovery = {}  # run_id -> recovery attempt metadata
        self._dead_letters = {}  # service_id -> safe, bounded failure metadata
        if self._dead_letter_store is not None:
            try:
                for item in self._dead_letter_store.list_active():
                    self._dead_letters[item["service_id"]] = dict(item)
            except Exception:
                logger.warning("dead-letter 状态恢复失败，启动后按进程内状态运行")
        self._recovery_lock = threading.RLock()
        self._run_id_counter = 0
        self._catchup_pending = self._settings.auto_catchup_enable
        self._catchup_reference = (
            _read_heartbeat(self._settings.heartbeat_path, self._clock())
            if self._catchup_pending else None
        )

    def _default_dep_probe(self, service_id):
        """默认依赖探针：未知依赖与一切探针异常均 fail-closed。"""
        probe = self._dep_probes.get(service_id)
        if probe is not None:
            try:
                return bool(probe())
            except Exception:
                logger.warning(
                    "service=%s 依赖探针异常，按未满足处理", service_id,
                )
                return False
        if service_id == "roll-manifest":
            try:
                return _roll_manifest_probe(self._settings, now=self._clock())
            except Exception:
                logger.warning("roll-manifest 依赖探针异常，按未满足处理")
                return False
        self._warn_once(
            service_id, "dep-unknown",
            "service=%s 作为依赖被引用但无探针实现，按未满足处理（请检查 depends_on 拼写）",
            service_id,
        )
        return False

    def _unsatisfied_deps(self, service_id, config):
        """返回未满足的依赖列表（空 = 可触发）。"""
        pending = []
        for dependency in config.depends_on:
            try:
                satisfied = self._dep_probe(dependency)
            except Exception:
                logger.warning(
                    "service=%s 的依赖 %s 探针异常，按未满足处理",
                    service_id, dependency,
                )
                satisfied = False
            if not satisfied:
                pending.append(dependency)
        if pending:
            self._warn_once(
                service_id, "dep-wait",
                "service=%s 依赖 %s 未满足，暂缓触发（每 tick 重查，满足即补发）",
                service_id, pending,
            )
        return pending

    # -- configuration (re-resolved every tick: Nacos hot reload) -----------

    def _reload(self, now):
        catchup_reference = None
        if self._catchup_pending:
            # 只在首轮 reload 允许补跑；之后新增/改线的配置绝不追溯历史。
            self._catchup_pending = False
            catchup_reference = self._catchup_reference
            self._catchup_reference = None
            if catchup_reference is None:
                self._warn_once(
                    "*", "catchup-no-heartbeat",
                    "自动补跑已开启但上次心跳不可用，仅保留当前分钟宽限",
                )
        if self._fleet_source is not None:
            try:
                refreshed = tuple(self._fleet_source())
            except Exception:
                self._warn_once(
                    "*", "fleet-refresh",
                    "舰队清单刷新失败，沿用上次清单（下 tick 重试）",
                )
            else:
                if refreshed:
                    # 清单收缩（注册表删线）：撤掉其点火时间，不再触发。
                    for stale in set(self._next_fire) - set(refreshed):
                        self._next_fire.pop(stale, None)
                    self._service_ids = refreshed
        for service_id in self._service_ids:
            try:
                config = self._config_source.get_pipeline(service_id)
            except Exception:
                logger.warning(
                    "service=%s 注册表解析失败，沿用上次配置", service_id
                )
                continue
            previous = self._configs.get(service_id)
            self._configs[service_id] = config
            schedule = config.schedule if config.enabled else None
            if schedule is None:
                self._next_fire.pop(service_id, None)
            elif previous is None or previous.schedule != schedule or service_id not in self._next_fire:
                fire_at = _next_fire(
                    schedule, now - timedelta(seconds=_FIRE_GRACE_SECONDS)
                )
                if catchup_reference is not None and fire_at is not None:
                    candidate = _previous_fire(schedule, now)
                    cutoff = now - timedelta(
                        seconds=self._settings.auto_catchup_max_age_seconds
                    )
                    # 每线最多补最新一个窗口；必须晚于上次心跳且在年龄上限内。
                    if (
                        candidate is not None
                        and catchup_reference < candidate <= now
                        and cutoff <= candidate
                        and self._recovery_allowed(service_id, "auto_catchup")
                    ):
                        fire_at = candidate
                if fire_at is None:
                    self._next_fire.pop(service_id, None)
                    self._warn_once(
                        service_id, "invalid-cron",
                        "service=%s cron 非法（%r），已跳过该线", service_id, schedule,
                    )
                else:
                    self._next_fire[service_id] = fire_at
            if schedule is not None and service_id in _UNSCHEDULABLE:
                self._warn_once(
                    service_id, "unschedulable",
                    "service=%s 为参数化修复工具，不参与定时调度，忽略其 schedule",
                    service_id,
                )

    def _warn_once(self, service_id, tag, message, *args):
        key = (service_id, tag)
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(message, *args)

    # -- firing ---------------------------------------------------------------

    def _launch_run(self, service_id, argv, *, run_id, trigger_type, attempt=1,
                    run_once_request_id=None):
        """Dedup and launch one logical attempt through the existing run path."""
        with self._recovery_lock:
            state = self._run_states.get(run_id)
            if state is not None and state != "pending":
                return False
            if service_id in self._running:
                return False
            if trigger_type == "cron" and service_id in self._dead_letters:
                return False
            self._run_states[run_id] = "running"
            self._pending_recovery.pop(run_id, None)
            self._running.add(service_id)
        if run_once_request_id is None:
            fn = lambda sid=service_id, cmd=argv, rid=run_id, n=attempt: self._run(
                sid, cmd, trigger_type=trigger_type, run_id=rid, attempt=n,
            )
        else:
            fn = lambda sid=service_id, cmd=argv, rid=run_id, qid=run_once_request_id: (
                self._run_once(sid, cmd, qid, run_id=rid)
            )
        try:
            self._executor.launch(fn)
        except Exception:
            with self._recovery_lock:
                self._running.discard(service_id)
                self._run_states[run_id] = "failed"
            raise
        return True

    def tick(self):
        """One poll iteration: reload configs, fire whatever is due."""
        now = self._clock()
        self._reload(now)
        for service_id, fire_at in list(self._next_fire.items()):
            if now < fire_at:
                continue
            config = self._configs.get(service_id) or PipelineConfig(service_id)
            if service_id in self._dead_letters:
                self._next_fire[service_id] = _next_fire(config.schedule, now)
                self._warn_once(
                    service_id, "dead-letter",
                    "service=%s 已进入 dead-letter，定时/自动恢复暂停；须人工成功运行后解除",
                    service_id,
                )
                continue
            # 依赖门禁（DAG）：depends_on 未满足时暂缓——**不推进**点火时间，
            # 下一 tick 重查，满足即补发（与其他跳过语义的「等下一 cron 槽」
            # 不同：依赖是分钟级等待，不该错过整个周期）。
            if self._unsatisfied_deps(service_id, config):
                continue
            self._next_fire[service_id] = _next_fire(config.schedule, now)
            if service_id in _UNSCHEDULABLE:
                continue
            if service_id in self._running:
                logger.info("service=%s 上次运行未结束，本次跳过", service_id)
                continue
            if any(
                item["service_id"] == service_id
                for item in self._pending_recovery.values()
            ):
                logger.info("service=%s 已有待恢复运行，本次 cron 窗口跳过", service_id)
                continue
            argv = build_argv(service_id, self._settings)
            if argv is None:
                self._warn_once(
                    service_id, "no-command",
                    "service=%s 无调度命令映射，忽略其 schedule", service_id,
                )
                continue
            run_id = f"cron:{service_id}:{fire_at.isoformat()}"
            if self._launch_run(
                service_id, argv, run_id=run_id, trigger_type="cron",
            ):
                logger.info(
                    "service=%s 触发：%s run_id=%s",
                    service_id, " ".join(argv[2:4]), run_id,
                )
        self._drain_recovery(now)
        self._drain_run_requests()

    @staticmethod
    def _recovery_allowed(service_id, action):
        from common.public_data.dataset_contract import service_recovery_allowed

        return service_recovery_allowed(service_id, action)

    def _retry_limit(self):
        return min(
            _MAX_AUTO_RETRY_ATTEMPTS,
            max(1, int(self._settings.auto_retry_max_attempts)),
        )

    def _schedule_recovery(self, service_id, run_id, attempt, returncode, reason):
        delay = max(0.0, self._settings.auto_retry_backoff_seconds)
        delay *= 2 ** max(0, attempt - 1)
        due = self._clock() + timedelta(seconds=delay)
        self._run_states[run_id] = "pending"
        self._pending_recovery[run_id] = {
            "service_id": service_id,
            "attempt": attempt + 1,
            "due": due,
        }
        logger.warning(
            "service=%s run_id=%s 第 %d 次失败（退出码=%s），%.1fs 后执行第 %d 次尝试；reason=%s",
            service_id, run_id, attempt, returncode, delay, attempt + 1, reason,
        )

    def _dead_letter(self, service_id, run_id, attempt, returncode, reason):
        self._run_states[run_id] = "dead_letter"
        self._pending_recovery.pop(run_id, None)
        self._dead_letters[service_id] = {
            "run_id": run_id,
            "attempts": attempt,
            "returncode": returncode,
            "reason": reason,
            "at": self._clock(),
        }
        if self._dead_letter_store is not None:
            try:
                self._dead_letter_store.record(
                    service_id, run_id, attempt, returncode, reason,
                )
            except Exception:
                logger.error("service=%s dead-letter 持久化失败", service_id)
            try:
                self._dead_letter_store.notify(
                    service_id, run_id, reason, attempt,
                )
            except Exception:
                logger.warning("service=%s dead-letter 告警入队失败", service_id)
        # 安全日志：只含线名、内部 run_id、次数和退出码；不打印异常正文/命令/凭据。
        logger.error(
            "service=%s run_id=%s 连续失败达到上限或遇永久错误，进入 dead-letter；attempts=%d exit_code=%s reason=%s",
            service_id, run_id, attempt, returncode, reason,
        )

    def _after_run(self, service_id, argv, trigger_type, run_id, attempt,
                   returncode, failure_kind):
        """Update bounded recovery state after history has been finalized."""
        del argv  # 恢复时重建命令，确保沿用最新门禁与配置，不保存敏感参数。
        with self._recovery_lock:
            if returncode == 0:
                self._run_states[run_id] = "finished"
                self._pending_recovery.pop(run_id, None)
                for pending_id, item in list(self._pending_recovery.items()):
                    if item["service_id"] == service_id:
                        self._pending_recovery.pop(pending_id, None)
                        self._run_states[pending_id] = "cancelled"
                if service_id in self._dead_letters:
                    del self._dead_letters[service_id]
                    logger.info("service=%s 人工/后续运行成功，解除 dead-letter", service_id)
                if self._dead_letter_store is not None:
                    try:
                        self._dead_letter_store.resolve(service_id)
                    except Exception:
                        logger.warning("service=%s dead-letter 解除回写失败", service_id)
                return
            self._run_states[run_id] = "failed"
            if trigger_type != "cron":
                return  # 手动 --force 运行绝不自动重试，避免重复外部投递。
            auto_retry = (
                self._settings.auto_retry_enable
                and self._recovery_allowed(service_id, "auto_retry")
            )
            auto_replay = (
                self._settings.auto_replay_enable
                and self._recovery_allowed(service_id, "auto_replay")
            )
            if returncode is not None and auto_retry:
                if returncode in _PERMANENT_EXIT_CODES:
                    self._dead_letter(
                        service_id, run_id, attempt, returncode, "permanent_exit",
                    )
                elif attempt >= self._retry_limit():
                    self._dead_letter(
                        service_id, run_id, attempt, returncode, "retry_limit",
                    )
                else:
                    self._schedule_recovery(
                        service_id, run_id, attempt, returncode, "nonzero_exit",
                    )
                return
            if failure_kind == "unknown_outcome":
                # 已启动但异常：结果未知，自动重放可能造成重复写/重复投递。
                if auto_retry or auto_replay:
                    self._dead_letter(
                        service_id, run_id, attempt, returncode, "unknown_outcome",
                    )
                return
            if failure_kind == "not_started" and auto_replay:
                if attempt >= self._retry_limit():
                    self._dead_letter(
                        service_id, run_id, attempt, returncode, "replay_limit",
                    )
                else:
                    self._schedule_recovery(
                        service_id, run_id, attempt, returncode, "not_started",
                    )

    def _drain_recovery(self, now):
        """Fire due retries, re-checking enable/command/dependency gates."""
        for run_id, item in list(self._pending_recovery.items()):
            service_id = item["service_id"]
            if now < item["due"]:
                continue
            if service_id in self._dead_letters:
                self._pending_recovery.pop(run_id, None)
                continue
            if service_id in self._running:
                continue
            config = self._configs.get(service_id)
            if (
                config is None or not config.enabled or not config.schedule
                or service_id in _UNSCHEDULABLE
            ):
                self._pending_recovery.pop(run_id, None)
                self._run_states[run_id] = "cancelled"
                logger.warning(
                    "service=%s run_id=%s 配置已禁用或不可调度，取消自动恢复",
                    service_id, run_id,
                )
                continue
            if self._unsatisfied_deps(service_id, config):
                continue
            argv = build_argv(service_id, self._settings)
            if argv is None:
                self._pending_recovery.pop(run_id, None)
                self._run_states[run_id] = "cancelled"
                continue
            self._launch_run(
                service_id, argv, run_id=run_id, trigger_type="cron",
                attempt=item["attempt"],
            )

    # -- 「立即运行一次」通道（pd_ops_run_request，ops-web 写入）----------------

    def _drain_run_requests(self):
        """认领并触发 pending 的运行请求；无注入或 DB 故障时静默降级。"""
        store = self._run_requests
        if store is None:
            return
        try:
            pending = store.fetch_pending()
        except Exception:
            self._warn_once(
                "*", "runreq-read",
                "运行一次请求读取失败（DB 不可达？），本 tick 跳过轮询",
            )
            return
        for request in pending:
            service_id = request.service_id
            if service_id in self._running:
                continue  # 在途（含刚被 cron 点燃）：下 tick 再认领
            argv = with_run_force(build_argv(service_id, self._settings))
            if service_id in _UNSCHEDULABLE or argv is None:
                try:
                    store.mark_rejected(request.request_id, "无调度命令映射")
                except Exception:
                    logger.warning(
                        "run-once #%s 拒收回写失败", request.request_id,
                        exc_info=True,
                    )
                continue
            config = self._configs.get(service_id)
            if config is None:
                try:
                    config = self._config_source.get_pipeline(service_id)
                except Exception:
                    config = PipelineConfig(service_id)
            # 与 cron 同一套依赖门禁：未满足留 pending，下 tick 重查。
            if self._unsatisfied_deps(service_id, config):
                continue
            try:
                claimed = store.claim(request.request_id)
            except Exception:
                logger.warning(
                    "run-once #%s 认领失败，下 tick 重试",
                    request.request_id, exc_info=True,
                )
                continue
            if not claimed:
                continue  # 已被其他副本认领
            run_id = f"run_once:{service_id}:{request.request_id}"
            if self._launch_run(
                service_id, argv, run_id=run_id, trigger_type="run_once",
                run_once_request_id=request.request_id,
            ):
                logger.info(
                    "service=%s 手动触发（run-once #%s，发起人 %s）",
                    service_id, request.request_id, request.requested_by,
                )

    # -- 执行流水（pd_ops_run_history，旁路审计：故障只告警、绝不阻断运行）----

    def _record_start(self, service_id, trigger_type):
        """点火前落 running 行；未注入或 DB 故障返回 None（不影响运行）。"""
        store = self._run_history
        if store is None:
            return None
        try:
            return store.record_start(service_id, trigger_type)
        except Exception:
            logger.warning(
                "service=%s 运行流水登记失败（旁路故障，不影响本次运行）",
                service_id, exc_info=True,
            )
            return None

    def _record_finish(self, history_id, returncode):
        """结束回写；失败只告警（流水滞留 running，由巡检 stale 检测兜出）。"""
        if history_id is None:
            return
        try:
            self._run_history.record_finish(history_id, returncode)
        except Exception:
            logger.warning(
                "运行流水 #%s 结果回写失败（该行将滞留 running）",
                history_id, exc_info=True,
            )

    def _reconcile_history(self):
        """启动自愈：收口上一实例遗留的 running 流水行（旁路，只告警）。

        本进程启动时残留的 running 行必然属于已退出实例（管道跑在守护
        线程，主进程被重启时子进程随之被杀、回写来不及执行）。未注入
        store → no-op；故障 → 仅告警，绝不阻断调度。返回收口行数。
        """
        store = self._run_history
        if store is None:
            return 0
        try:
            return store.reconcile_orphans()
        except Exception:
            logger.warning(
                "运行流水启动自愈失败（旁路故障，不影响调度）", exc_info=True,
            )
            return 0

    def _run_once(self, service_id, argv, request_id, *, run_id=None):
        """run-once 包装：复用 _run 的锁/运行路径，结束后回写结果。"""
        returncode = self._run(
            service_id, argv, trigger_type="run_once", run_id=run_id,
        )
        try:
            self._run_requests.mark_finished(request_id, returncode)
        except Exception:
            logger.warning(
                "run-once #%s 结果回写失败（请求状态将滞留 launched）",
                request_id, exc_info=True,
            )

    def _run(self, service_id, argv, trigger_type="cron", *, run_id=None, attempt=1):
        """持锁运行一条管道；返回子进程退出码（锁失败/异常返回 None）。

        *trigger_type* 仅用于执行流水（cron / run_once）；自动恢复仍记 cron，
        以兼容既有流水枚举。流水行在取锁之前落库，锁被占用/运行异常
        （无退出码）也会留下 failed 记录。
        """
        if run_id is None:
            with self._recovery_lock:
                self._run_id_counter += 1
                run_id = f"direct:{service_id}:{self._run_id_counter}"
                self._run_states[run_id] = "running"
        history_id = self._record_start(service_id, trigger_type)
        returncode = None
        failure_kind = None
        started = False
        try:
            try:
                lock = self._lock_factory(service_id)
            except Exception:
                failure_kind = "not_started"
                logger.error(
                    "service=%s 获取调度锁失败（DB 不可达？），本次跳过",
                    service_id, exc_info=True,
                )
                return None
            try:
                with lock:
                    started = True
                    returncode = self._runner(service_id, argv)
            except Exception:
                failure_kind = "unknown_outcome" if started else "not_started"
                logger.error(
                    "service=%s 运行异常", service_id, exc_info=True
                )
                return None
            if returncode != 0:
                failure_kind = "exit"
                logger.error(
                    "service=%s 退出码 %s（自动恢复按显式开关与上限处理）",
                    service_id, returncode,
                )
            else:
                logger.info("service=%s 运行完成", service_id)
            return returncode
        finally:
            self._running.discard(service_id)
            self._record_finish(history_id, returncode)
            self._after_run(
                service_id, argv, trigger_type, run_id, attempt,
                returncode, failure_kind,
            )

    # -- main loop ------------------------------------------------------------

    def run_forever(self):
        reconciled = self._reconcile_history()
        logger.info(
            "scheduler 启动：%d 条注册管道，每 %.0fs 轮询",
            len(self._service_ids), self._poll_seconds,
        )
        if reconciled:
            logger.info("启动自愈：收口 %d 条遗留 running 流水行", reconciled)
        while True:
            try:
                write_heartbeat(self._settings.heartbeat_path)
            except OSError:
                logger.warning("scheduler 心跳写入失败", exc_info=True)
            self.tick()
            self._sleeper(self._poll_seconds)


def load_service_ids(environ=None):
    """Fleet enumeration: the seed keys (seed is always mounted)."""
    env = os.environ if environ is None else environ
    seed_path = (env.get("PUBLIC_DATA_PIPELINE_SEED") or "").strip()
    if not seed_path:
        raise RuntimeError(
            "PUBLIC_DATA_PIPELINE_SEED 未配置，无法枚举管道清单"
        )
    ids = tuple(load_seed(seed_path).keys())
    if not ids:
        raise RuntimeError(f"管道 seed 为空：{seed_path}")
    return ids


def _nacos_service_ids(env):
    """Nacos PIPELINES 组的 service_id 列表（data-id 去 .yaml 后缀）。

    未配置 Nacos / 客户端不支持列举（SDK 客户端无 list API）时返回
    空元组；列举异常抛给调用方降级。
    """
    server = (env.get("PUBLIC_DATA_NACOS_SERVER") or "").strip()
    if not server:
        return ()
    from common.public_data.nacos_client import build_nacos_client
    client = build_nacos_client(
        server,
        namespace=(env.get("PUBLIC_DATA_NACOS_NAMESPACE") or "").strip(),
        username=(env.get("PUBLIC_DATA_NACOS_USERNAME") or "").strip() or None,
        password=(env.get("PUBLIC_DATA_NACOS_PASSWORD") or "").strip() or None,
    )
    list_data_ids = getattr(client, "list_data_ids", None)
    if list_data_ids is None:
        return ()
    group = (
        (env.get("PUBLIC_DATA_NACOS_GROUP") or "").strip() or DEFAULT_GROUP
    )
    ids = []
    for data_id in list_data_ids(group):
        if data_id.endswith(".yaml"):
            data_id = data_id[:-len(".yaml")]
        if data_id:
            ids.append(data_id)
    return tuple(ids)


def build_fleet_source(environ=None):
    """舰队清单源（生产）：seed keys ∪ Nacos PIPELINES 组列举。

    返回一个零参 callable，scheduler 每 tick 重调——ops-web 新增的
    Nacos-only 管道无需重启调度器即可入列。Nacos 列举失败降级为
    seed keys（fail-safe：宁少不多，且告警去重、恢复后可再告）。
    seed 读不到/为空照常抛错（舰队不能没有地基）。
    """
    env = os.environ if environ is None else environ
    seed_path = (env.get("PUBLIC_DATA_PIPELINE_SEED") or "").strip()
    if not seed_path:
        raise RuntimeError(
            "PUBLIC_DATA_PIPELINE_SEED 未配置，无法枚举管道清单"
        )
    state = {"warned": False}

    def enumerate_fleet():
        ids = list(load_seed(seed_path).keys())
        if not ids:
            raise RuntimeError(f"管道 seed 为空：{seed_path}")
        try:
            extras = _nacos_service_ids(env)
        except Exception:
            extras = ()
            if not state["warned"]:
                state["warned"] = True
                logger.warning(
                    "nacos 管道清单列举失败，降级为 seed keys（恢复前每 tick 如此）",
                    exc_info=True,
                )
        else:
            state["warned"] = False
        seen = set(ids)
        for extra_id in extras:
            if extra_id not in seen:
                seen.add(extra_id)
                ids.append(extra_id)
        return tuple(ids)

    return enumerate_fleet


def main():  # pragma: no cover - thin wiring, exercised in integration env
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    import time

    from common.public_data.db import connect
    from common.public_data.ops_control import (
        DbDeadLetterGateway,
        DbRunHistoryGateway,
        DbRunRequestGateway,
    )
    from common.public_data.settings import Settings

    settings = SchedulerSettings.from_env()
    mart_database = Settings.from_environment().mart_database
    dep_probes = {
        service_id: build_summary_dep_probe(
            connect, mart_database,
            source_name=source_name, dataset_like=dataset_like,
            beijing_hm=beijing_hm,
        )
        for service_id, (source_name, dataset_like, beijing_hm)
        in _DEP_SUMMARY_PROBES.items()
    }
    dep_probes.update({
        service_id: build_history_dep_probe(
            connect, mart_database, service_id=service_id, beijing_hm=beijing_hm,
        )
        for service_id, beijing_hm in _DEP_HISTORY_PROBES.items()
    })
    scheduler = Scheduler(
        config_source=build_config_source(),
        service_ids=build_fleet_source(),
        runner=SubprocessRunner(),
        lock_factory=build_mysql_lock_factory(connect, mart_database),
        settings=settings,
        poll_seconds=settings.poll_seconds,
        sleeper=time.sleep,
        dep_probes=dep_probes,
        run_requests=DbRunRequestGateway(connect, mart_database),
        run_history=DbRunHistoryGateway(connect, mart_database),
        dead_letter_store=DbDeadLetterGateway(connect, mart_database),
    )
    scheduler.run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
