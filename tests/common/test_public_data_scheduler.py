"""Scheduler unit tests: no Nacos, MySQL or subprocesses -- all fakes."""

import logging
from datetime import datetime, timedelta

import pytest

from common.public_data.pipeline_config import StaticConfigSource
from common.public_data.scheduler import (
    NullLock,
    Scheduler,
    SchedulerSettings,
    SyncExecutor,
    build_argv,
    build_child_env,
)

T0 = datetime(2026, 9, 21, 1, 59, 30)  # 周一 01:59:30，离 02:00 窗口 30 秒

SETTINGS = SchedulerSettings(
    credentials_path="/creds/source-credentials.json", output_dir="/out"
)


class FakeClock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class RecordingRunner:
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, service_id, argv):
        self.calls.append((service_id, argv))
        return self.returncode


class LockedOutLock:
    """Simulates a MySQL named lock held by someone else."""

    def __enter__(self):
        from common.public_data.db import LockUnavailable

        raise LockUnavailable("held")

    def __exit__(self, *exc):
        return False


def make_scheduler(configs, clock=None, runner=None, lock_factory=None):
    return Scheduler(
        config_source=StaticConfigSource(configs),
        service_ids=tuple(configs.keys()),
        runner=runner or RecordingRunner(),
        lock_factory=lock_factory or (lambda service_id: NullLock()),
        settings=SETTINGS,
        executor=SyncExecutor(),
        clock=clock or FakeClock(),
    )


def due_scheduler(configs, clock=None, **kwargs):
    """Scheduler whose clock already sits inside the 02:00 fire window."""
    clock = clock or FakeClock(datetime(2026, 9, 21, 2, 0, 5))
    scheduler = make_scheduler(configs, clock=clock, **kwargs)
    return scheduler, clock


# -- command table ------------------------------------------------------------

def test_build_argv_mirrors_compose_sync_wdt():
    argv = build_argv("sync-wdt", SETTINGS)
    assert argv[1:3] == ["-m", "common.public_data.cli"]
    assert argv[3:] == [
        "live-sync", "--live-read", "--confirm-local-test-write",
        "--source", "wdt",
        "--source-credentials", "/creds/source-credentials.json",
    ]


def test_build_argv_pages_output_dir():
    argv = build_argv("pages-vanke", SETTINGS)
    assert argv[-1] == "/out/vanke.html"


def test_build_argv_unknown_service_returns_none():
    assert build_argv("bi-web", SETTINGS) is None
    assert build_argv("project-mart", SETTINGS) is None


def test_child_env_carries_line_identity_and_region():
    env = build_child_env("robot-hangzhou", {"FOO": "1"})
    assert env["FOO"] == "1"
    assert env["PUBLIC_DATA_SERVICE_ID"] == "robot-hangzhou"
    assert env["ROBOT_REGION"] == "hangzhou"


# -- firing --------------------------------------------------------------------

def test_fires_when_cron_due():
    scheduler, _ = due_scheduler({"sync-wdt": {"schedule": "0 2 * * *"}})
    scheduler.tick()
    assert [sid for sid, _ in scheduler._runner.calls] == ["sync-wdt"]


def test_not_fired_before_window():
    scheduler = make_scheduler({"sync-wdt": {"schedule": "0 2 * * *"}},
                               clock=FakeClock(T0))
    scheduler.tick()
    assert scheduler._runner.calls == []


def test_fires_once_per_window_and_reschedules():
    scheduler, clock = due_scheduler({"sync-wdt": {"schedule": "0 2 * * *"}})
    scheduler.tick()
    scheduler.tick()  # 同一分钟内重复轮询不重复触发
    clock.advance(hours=1)
    scheduler.tick()
    assert len(scheduler._runner.calls) == 1
    clock.advance(days=1)  # 次日 02:00 窗口再次触发
    scheduler.tick()
    assert len(scheduler._runner.calls) == 2


def test_disabled_line_never_fires():
    scheduler, _ = due_scheduler(
        {"sync-wdt": {"schedule": "0 2 * * *", "enabled": False}}
    )
    scheduler.tick()
    assert scheduler._runner.calls == []


def test_line_without_schedule_ignored():
    scheduler, _ = due_scheduler({"bi-web": {}})
    scheduler.tick()
    assert scheduler._runner.calls == []


def test_unschedulable_project_mart_skipped(caplog):
    scheduler, _ = due_scheduler({"project-mart": {"schedule": "0 3 * * *"}})
    with caplog.at_level(logging.WARNING):
        scheduler.tick()
    assert scheduler._runner.calls == []
    assert "不参与定时调度" in caplog.text


def test_service_without_command_mapping_skipped(caplog):
    scheduler, _ = due_scheduler({"mystery": {"schedule": "0 2 * * *"}})
    with caplog.at_level(logging.WARNING):
        scheduler.tick()
    assert scheduler._runner.calls == []
    assert "无调度命令映射" in caplog.text


def test_invalid_cron_warns_once_and_skips(caplog):
    scheduler, clock = due_scheduler({"sync-wdt": {"schedule": "not a cron"}})
    with caplog.at_level(logging.WARNING):
        scheduler.tick()
        clock.advance(minutes=1)
        scheduler.tick()
    assert scheduler._runner.calls == []
    assert caplog.text.count("cron 非法") == 1


def test_lock_unavailable_skips_run():
    scheduler, _ = due_scheduler(
        {"sync-wdt": {"schedule": "0 2 * * *"}},
        lock_factory=lambda service_id: LockedOutLock(),
    )
    scheduler.tick()
    assert scheduler._runner.calls == []
    assert "sync-wdt" not in scheduler._running  # 锁失败不卡死后续窗口


def test_lock_factory_failure_skips_run():
    def broken_factory(service_id):
        raise ConnectionError("db down")

    scheduler, _ = due_scheduler(
        {"sync-wdt": {"schedule": "0 2 * * *"}}, lock_factory=broken_factory
    )
    scheduler.tick()
    assert scheduler._runner.calls == []
    assert "sync-wdt" not in scheduler._running


def test_nonzero_exit_does_not_block_future_windows():
    runner = RecordingRunner(returncode=1)
    scheduler, clock = due_scheduler(
        {"sync-wdt": {"schedule": "0 2 * * *"}}, runner=runner
    )
    scheduler.tick()
    clock.advance(days=1)
    scheduler.tick()
    assert len(runner.calls) == 2  # 失败不禁跑，下个窗口照常重试


def test_overlap_skipped():
    scheduler, clock = due_scheduler({"sync-wdt": {"schedule": "* * * * *"}})
    scheduler._running.add("sync-wdt")  # 上次运行仍在途
    scheduler.tick()
    assert scheduler._runner.calls == []


# -- hot reload（注册表语义） ----------------------------------------------------

def test_hot_reload_picks_up_schedule_change():
    source = StaticConfigSource({"sync-wdt": {"schedule": "0 2 * * *"}})
    clock = FakeClock(datetime(2026, 9, 21, 2, 30, 0))
    scheduler = make_scheduler({}, clock=clock)
    scheduler._config_source = source
    scheduler._service_ids = ("sync-wdt",)
    scheduler.tick()
    assert scheduler._runner.calls == []  # 02:00 窗口已过，无补跑
    clock.advance(hours=23, minutes=30)  # 次日 02:00
    scheduler.tick()
    assert len(scheduler._runner.calls) == 1

    # Nacos 改 cron → 下 tick 生效（03:00 档），不重启调度器
    source._by_id["sync-wdt"] = StaticConfigSource(
        {"sync-wdt": {"schedule": "0 3 * * *"}}
    ).get_pipeline("sync-wdt")
    clock.advance(hours=1)  # 03:00
    scheduler.tick()
    assert len(scheduler._runner.calls) == 2


def test_hot_reload_disable_stops_firing():
    source = StaticConfigSource({"sync-wdt": {"schedule": "0 2 * * *"}})
    clock = FakeClock(datetime(2026, 9, 21, 1, 0, 0))
    scheduler = make_scheduler({}, clock=clock)
    scheduler._config_source = source
    scheduler._service_ids = ("sync-wdt",)
    scheduler.tick()
    source._by_id["sync-wdt"] = StaticConfigSource(
        {"sync-wdt": {"schedule": "0 2 * * *", "enabled": False}}
    ).get_pipeline("sync-wdt")
    clock.advance(hours=1, seconds=5)  # 02:00:05，已禁用
    scheduler.tick()
    assert scheduler._runner.calls == []


def test_config_error_keeps_last_known_good(caplog):
    class FlakySource:
        def __init__(self):
            self.fail = False

        def get_pipeline(self, service_id):
            if self.fail:
                raise RuntimeError("nacos unreachable")
            return StaticConfigSource(
                {"sync-wdt": {"schedule": "0 2 * * *"}}
            ).get_pipeline(service_id)

    source = FlakySource()
    clock = FakeClock(datetime(2026, 9, 21, 1, 0, 0))
    scheduler = make_scheduler({}, clock=clock)
    scheduler._config_source = source
    scheduler._service_ids = ("sync-wdt",)
    scheduler.tick()
    source.fail = True
    clock.advance(hours=1, seconds=5)  # 02:00:05，注册表暂时不可达
    with caplog.at_level(logging.WARNING):
        scheduler.tick()
    assert "沿用上次配置" in caplog.text
    assert len(scheduler._runner.calls) == 1  # 仍按缓存配置触发
