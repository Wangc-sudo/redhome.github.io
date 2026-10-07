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
* No catch-up beyond the current minute: the next fire is computed from
  ``now - 60s``, so a scheduler that comes up at 02:00:05 still honours the
  02:00 window, but one that was down since 01:00 does not retroactively
  fire hours-old misses at 09:00 -- those surface via the data-watermark
  checks instead.
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


def _pages_entry(region, *extra):
    return _mart_cli_entry(
        "leaderboard-html",
        "--output", f"{{output_dir}}/{region}.html",
        *extra,
        region=region,
    )


#: 日报机器人区域（robot-<region> → mart_cli once）
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
    "sync-runner": _sync_entry(),
    "sync-dingtalk": _sync_entry("--source", "dingtalk"),
    "sync-wdt": _sync_entry("--source", "wdt"),
    "roll-manifest": {
        "argv": [
            "common.public_data.cli", "roll-manifest",
            "--confirm-local-test-write",
        ],
    },
    "extract-mart": _extract_entry(),
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
    "channel-missing-check": _mart_cli_entry("channel-missing", region="qudao"),
    # 10:45 T+1 重算用默认日期（今天）：渠道板块锚定「business_date 之前
    # 最近有效日」= T-1，带 --date yesterday 反而退到 T-2（2026-09-28 裁决，
    # 偏离执行提示词 §2.4——其 yesterday 前提与 resolve_channel_business_date
    # 的既有锚定语义冲突）。
    "pages-qudao-t1": _pages_entry("qudao"),
    **{f"robot-{r}": _mart_cli_entry("once", region=r) for r in _ROBOT_REGIONS},
    "channel-daily-qudao": _mart_cli_entry("channel-daily", region="qudao"),
    **{f"pages-{r}": _pages_entry(r) for r in _PAGES_REGIONS},
    **{
        f"leaderboard-{r}": _mart_cli_entry("leaderboard", region=r)
        for r in _LEADERBOARD_REGIONS
    },
    "offline-daily-summary": _mart_cli_entry("offline-daily", region="offline_all"),
    "offline-weekly-summary": _mart_cli_entry("offline-weekly", region="offline_all"),
    "offline-monthly-summary": _mart_cli_entry("offline-monthly", region="offline_all"),
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
    ``mart_cli once``、``pages-<region>`` → ``leaderboard-html``，新区域
    注册即可调度、无需改代码；region 合法性由 mart_cli 运行时校验
    （未知 region 子进程非零退出，日志可观测）。无模板返回 None。
    """
    spec = _COMMAND_TABLE.get(service_id)
    if spec is not None:
        return spec
    for prefix, builder in (
        ("robot-", lambda region: _mart_cli_entry("once", region=region)),
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


@dataclass(frozen=True)
class SchedulerSettings:
    """Runtime settings resolved from the environment."""

    credentials_path: str = "/run/live-input/source-credentials.json"
    output_dir: str = "/output"
    poll_seconds: float = DEFAULT_POLL_SECONDS
    #: roll-manifest 依赖探针盯的 live manifest 路径（见 _roll_manifest_probe）。
    manifest_path: str = ""

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
        )


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
        lock_name = f"{_LOCK_PREFIX}:{service_id}"
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


def _next_fire(schedule, now):
    """Next fire time strictly after *now*; None when the cron is invalid."""
    try:
        from croniter import croniter
    except ImportError:  # pragma: no cover - dependency is pinned
        raise RuntimeError("croniter is required for the scheduler")
    try:
        return croniter(schedule, now).get_next(datetime)
    except (ValueError, KeyError):
        return None


#: 摘要型依赖探针登记：service_id -> (source_name, dataset_name LIKE 模式,
#: 北京窗口时刻)。今日（北京）窗口时刻之后存在 completed run 的匹配摘要
#: 即视为上游已就绪；时间列存 UTC，阈值按 UTC+8 折算。
_DEP_SUMMARY_PROBES = {
    "sync-channel-sales": ("dingtalk", "channel_daily_sales%", (10, 30)),
    "extract-channel": ("extract", "ecom_people", (10, 34)),
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
                 dep_probes=None, run_requests=None):
        self._config_source = config_source
        if callable(service_ids):
            self._fleet_source = service_ids
            self._service_ids = tuple(service_ids())
        else:
            self._fleet_source = None
            self._service_ids = tuple(service_ids)
        self._runner = runner
        self._run_requests = run_requests
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

    def _default_dep_probe(self, service_id):
        """默认依赖探针：已知依赖逐项判定；未知依赖告警后按满足处理。"""
        probe = self._dep_probes.get(service_id)
        if probe is not None:
            return probe()
        if service_id == "roll-manifest":
            try:
                return _roll_manifest_probe(self._settings, now=self._clock())
            except Exception:
                logger.warning("roll-manifest 依赖探针异常，按未满足处理", exc_info=True)
                return False
        self._warn_once(
            service_id, "dep-unknown",
            "service=%s 作为依赖被引用但无探针实现，按已满足处理（请检查 depends_on 拼写）",
            service_id,
        )
        return True

    def _unsatisfied_deps(self, service_id, config):
        """返回未满足的依赖列表（空 = 可触发）。"""
        pending = [d for d in config.depends_on if not self._dep_probe(d)]
        if pending:
            self._warn_once(
                service_id, "dep-wait",
                "service=%s 依赖 %s 未满足，暂缓触发（每 tick 重查，满足即补发）",
                service_id, pending,
            )
        return pending

    # -- configuration (re-resolved every tick: Nacos hot reload) -----------

    def _reload(self, now):
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

    def tick(self):
        """One poll iteration: reload configs, fire whatever is due."""
        now = self._clock()
        self._reload(now)
        for service_id, fire_at in list(self._next_fire.items()):
            if now < fire_at:
                continue
            config = self._configs.get(service_id) or PipelineConfig(service_id)
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
            argv = build_argv(service_id, self._settings)
            if argv is None:
                self._warn_once(
                    service_id, "no-command",
                    "service=%s 无调度命令映射，忽略其 schedule", service_id,
                )
                continue
            self._running.add(service_id)
            logger.info("service=%s 触发：%s", service_id, " ".join(argv[2:4]))
            self._executor.launch(
                lambda sid=service_id, cmd=argv: self._run(sid, cmd)
            )
        self._drain_run_requests()

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
            argv = build_argv(service_id, self._settings)
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
            self._running.add(service_id)
            logger.info(
                "service=%s 手动触发（run-once #%s，发起人 %s）",
                service_id, request.request_id, request.requested_by,
            )
            self._executor.launch(
                lambda sid=service_id, cmd=argv, rid=request.request_id:
                    self._run_once(sid, cmd, rid)
            )

    def _run_once(self, service_id, argv, request_id):
        """run-once 包装：复用 _run 的锁/运行路径，结束后回写结果。"""
        returncode = self._run(service_id, argv)
        try:
            self._run_requests.mark_finished(request_id, returncode)
        except Exception:
            logger.warning(
                "run-once #%s 结果回写失败（请求状态将滞留 launched）",
                request_id, exc_info=True,
            )

    def _run(self, service_id, argv):
        """持锁运行一条管道；返回子进程退出码（锁失败/异常返回 None）。"""
        try:
            try:
                lock = self._lock_factory(service_id)
            except Exception:
                logger.error(
                    "service=%s 获取调度锁失败（DB 不可达？），本次跳过",
                    service_id, exc_info=True,
                )
                return None
            try:
                with lock:
                    returncode = self._runner(service_id, argv)
            except Exception:
                logger.error(
                    "service=%s 运行异常", service_id, exc_info=True
                )
                return None
            if returncode != 0:
                logger.error(
                    "service=%s 退出码 %s（失败不计入禁跑，下个 cron 窗口照常重试）",
                    service_id, returncode,
                )
            else:
                logger.info("service=%s 运行完成", service_id)
            return returncode
        finally:
            self._running.discard(service_id)

    # -- main loop ------------------------------------------------------------

    def run_forever(self):
        logger.info(
            "scheduler 启动：%d 条注册管道，每 %.0fs 轮询",
            len(self._service_ids), self._poll_seconds,
        )
        while True:
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
    from common.public_data.ops_control import DbRunRequestGateway
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
    )
    scheduler.run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
