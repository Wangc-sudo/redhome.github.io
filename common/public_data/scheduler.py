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

All collaborators (config source, runner, lock factory, executor, clock,
sleeper) are injected, so unit tests need no Nacos, MySQL or subprocess.
"""

import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from common.public_data.pipeline_config import (
    PipelineConfig,
    build_config_source,
    load_seed,
)

logger = logging.getLogger(__name__)

#: Services whose registry ``schedule`` is honoured, mapped to the argv that
#: reproduces their compose one-shot command (``sys.executable -m`` prefix is
#: added by the runner).  ``extra_env`` is merged into the child environment.
#: ``{credentials}`` / ``{output_dir}`` are filled from scheduler settings.
_COMMAND_TABLE = {
    "sync-runner": {
        "argv": [
            "common.public_data.cli", "live-sync", "--live-read",
            "--confirm-local-test-write",
            "--source-credentials", "{credentials}",
        ],
    },
    "sync-dingtalk": {
        "argv": [
            "common.public_data.cli", "live-sync", "--live-read",
            "--confirm-local-test-write", "--source", "dingtalk",
            "--source-credentials", "{credentials}",
        ],
    },
    "sync-wdt": {
        "argv": [
            "common.public_data.cli", "live-sync", "--live-read",
            "--confirm-local-test-write", "--source", "wdt",
            "--source-credentials", "{credentials}",
        ],
    },
    "extract-mart": {
        "argv": [
            "common.public_data.cli", "extract-mart",
            "--confirm-local-test-write",
        ],
    },
    "robot-hangzhou": {
        "argv": [
            "common.daily_robot.mart_cli", "once",
            "--confirm-local-test-write",
        ],
        "extra_env": {"ROBOT_REGION": "hangzhou"},
    },
    "robot-vanke": {
        "argv": [
            "common.daily_robot.mart_cli", "once",
            "--confirm-local-test-write",
        ],
        "extra_env": {"ROBOT_REGION": "vanke"},
    },
    "pages-hangzhou": {
        "argv": [
            "common.daily_robot.mart_cli", "leaderboard-html",
            "--confirm-local-test-write",
            "--output", "{output_dir}/hangzhou.html",
        ],
        "extra_env": {"ROBOT_REGION": "hangzhou"},
    },
    "pages-vanke": {
        "argv": [
            "common.daily_robot.mart_cli", "leaderboard-html",
            "--confirm-local-test-write",
            "--output", "{output_dir}/vanke.html",
        ],
        "extra_env": {"ROBOT_REGION": "vanke"},
    },
}

#: Registered in the seed with a cron but intentionally not schedulable
#: (parameterized repair tool, see module docstring).
_UNSCHEDULABLE = ("project-mart",)

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
        )


def build_argv(service_id, settings):
    """Full argv (including ``python -m``) for *service_id*, or None."""
    spec = _COMMAND_TABLE.get(service_id)
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
    spec = _COMMAND_TABLE.get(service_id) or {}
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


class Scheduler:
    """Polls the registry and fires pipelines whose cron is due.

    *config_source* resolves each service's :class:`PipelineConfig` (Nacos
    with seed fallback); *service_ids* enumerates the fleet (the seed keys).
    *runner* / *lock_factory* / *executor* / *clock* / *sleeper* are all
    injectable -- see module docstring for the no-catch-up policy.
    """

    def __init__(self, *, config_source, service_ids, runner, lock_factory,
                 settings=None, executor=None, clock=None, sleeper=None,
                 poll_seconds=DEFAULT_POLL_SECONDS):
        self._config_source = config_source
        self._service_ids = tuple(service_ids)
        self._runner = runner
        self._lock_factory = lock_factory
        self._settings = settings or SchedulerSettings.from_env()
        self._executor = executor or ThreadExecutor()
        self._clock = clock or datetime.now
        self._sleeper = sleeper
        self._poll_seconds = poll_seconds
        self._configs = {}     # service_id -> PipelineConfig (last known good)
        self._next_fire = {}   # service_id -> datetime
        self._running = set()  # service_ids with a run in flight
        self._warned = set()   # one-shot WARNING dedup

    # -- configuration (re-resolved every tick: Nacos hot reload) -----------

    def _reload(self, now):
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

    def _run(self, service_id, argv):
        try:
            try:
                lock = self._lock_factory(service_id)
            except Exception:
                logger.error(
                    "service=%s 获取调度锁失败（DB 不可达？），本次跳过",
                    service_id, exc_info=True,
                )
                return
            try:
                with lock:
                    returncode = self._runner(service_id, argv)
            except Exception:
                logger.error(
                    "service=%s 运行异常", service_id, exc_info=True
                )
                return
            if returncode != 0:
                logger.error(
                    "service=%s 退出码 %s（失败不计入禁跑，下个 cron 窗口照常重试）",
                    service_id, returncode,
                )
            else:
                logger.info("service=%s 运行完成", service_id)
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
    """Fleet enumeration: the seed keys (seed is always mounted, Nacos is not listable)."""
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


def main():  # pragma: no cover - thin wiring, exercised in integration env
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    import time

    from common.public_data.db import connect
    from common.public_data.settings import load_settings

    settings = SchedulerSettings.from_env()
    scheduler = Scheduler(
        config_source=build_config_source(),
        service_ids=load_service_ids(),
        runner=SubprocessRunner(),
        lock_factory=build_mysql_lock_factory(
            connect, load_settings().mart_database
        ),
        settings=settings,
        poll_seconds=settings.poll_seconds,
        sleeper=time.sleep,
    )
    scheduler.run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
