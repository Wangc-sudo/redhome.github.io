"""Scheduler unit tests: no Nacos, MySQL or subprocesses -- all fakes."""

import logging
from datetime import datetime, timedelta, timezone

from dataclasses import dataclass

from common.public_data.pipeline_config import StaticConfigSource
from common.public_data.scheduler import (
    NullLock,
    Scheduler,
    SchedulerSettings,
    SyncExecutor,
    build_argv,
    build_child_env,
    command_spec,
    has_command,
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


def make_dag_scheduler(dep_cfg, *, probe, clock=None):
    """带依赖探针的 scheduler：roll-manifest + sync-wdt 两线。"""
    configs = {
        "roll-manifest": {"schedule": "55 1 * * *"},
        "sync-wdt": dep_cfg,
    }
    return Scheduler(
        config_source=StaticConfigSource(configs),
        service_ids=tuple(configs.keys()),
        runner=RecordingRunner(),
        lock_factory=lambda service_id: NullLock(),
        settings=SETTINGS,
        executor=SyncExecutor(),
        clock=clock or FakeClock(datetime(2026, 9, 21, 2, 0, 5)),
        dep_probe=probe,
    )


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


# -- dependencies (DAG) --------------------------------------------------------

_DEP_CFG = {"schedule": "0 2 * * *", "depends_on": ["roll-manifest"]}


def test_dependency_unsatisfied_defers_fire_and_retries_next_tick():
    probes = iter([False, True])  # 第一 tick 未满足，第二 tick 满足
    scheduler = make_dag_scheduler(_DEP_CFG, probe=lambda service_id: next(probes))
    scheduler.tick()
    assert scheduler._runner.calls == []        # 未满足：暂缓
    assert "sync-wdt" in scheduler._next_fire   # 点火时间保留（不推进）
    scheduler.tick()                            # 下一 tick 重查，满足即补发
    assert [sid for sid, _ in scheduler._runner.calls] == ["sync-wdt"]


def test_dependency_fire_at_not_advanced_while_waiting():
    clock = FakeClock(datetime(2026, 9, 21, 2, 0, 5))
    scheduler = make_dag_scheduler(
        _DEP_CFG, probe=lambda service_id: False, clock=clock
    )
    scheduler.tick()
    first = scheduler._next_fire["sync-wdt"]
    clock.advance(minutes=5)
    scheduler.tick()
    assert scheduler._next_fire["sync-wdt"] == first  # 等待期间不推进
    assert scheduler._runner.calls == []


def test_unknown_dependency_warns_once_and_fires(caplog):
    # probe=None → 走 Scheduler 默认探针（未知依赖告警一次后按满足处理）
    clock = FakeClock(datetime(2026, 9, 21, 2, 0, 5))
    scheduler = make_dag_scheduler(
        {"schedule": "0 2 * * *", "depends_on": ["typo-service"]},
        probe=None, clock=clock,
    )
    with caplog.at_level(logging.WARNING):
        scheduler.tick()
        clock.advance(days=1)  # 次日同一窗口（no-catch-up：同分钟不重燃）
        scheduler.tick()  # 第二次：告警去重
    fired = [sid for sid, _ in scheduler._runner.calls if sid == "sync-wdt"]
    assert fired == ["sync-wdt", "sync-wdt"]
    assert caplog.text.count("无探针实现") == 1


def test_roll_manifest_probe_reads_manifest_mtime(tmp_path):
    from common.public_data.scheduler import _roll_manifest_probe

    manifest = tmp_path / "source-manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    settings = SchedulerSettings(manifest_path=str(manifest))
    assert _roll_manifest_probe(settings, now=datetime.now()) is True

    # 昨天的时间戳 → 未滚动（fail-closed）
    old = datetime.now().timestamp() - 86400 * 2
    import os as _os
    _os.utime(manifest, (old, old))
    assert _roll_manifest_probe(settings, now=datetime.now()) is False

    # 未配置路径 / 文件不存在 → 未满足
    assert _roll_manifest_probe(SchedulerSettings(), now=datetime.now()) is False
    assert _roll_manifest_probe(
        SchedulerSettings(manifest_path=str(tmp_path / "nope.json")),
        now=datetime.now(),
    ) is False


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

# -- 渠道日销 T+1 窗口命令表（2026-09-28） -------------------------------------

def test_build_argv_sync_channel_sales():
    argv = build_argv("sync-channel-sales", SETTINGS)
    assert argv[1:3] == ["-m", "common.public_data.cli"]
    assert argv[3:] == [
        "live-sync", "--live-read", "--confirm-local-test-write",
        "--source", "dingtalk",
        "--dataset", "channel_daily_sales*",
        "--dataset", "channel_monthly_target",
        "--source-credentials", "/creds/source-credentials.json",
    ]


def test_build_argv_extract_channel():
    argv = build_argv("extract-channel", SETTINGS)
    assert argv[1:3] == ["-m", "common.public_data.cli"]
    assert argv[3:] == [
        "extract-mart", "--confirm-local-test-write",
        "--dataset", "channel_daily_sales",
        "--dataset", "channel_monthly_target",
    ]


def test_build_argv_channel_missing_check():
    argv = build_argv("channel-missing-check", SETTINGS)
    assert argv[1:4] == ["-m", "common.daily_robot.mart_cli", "channel-missing"]
    env = build_child_env("channel-missing-check", {})
    assert env["ROBOT_REGION"] == "qudao"
    assert env["PUBLIC_DATA_SERVICE_ID"] == "channel-missing-check"


def test_build_argv_pages_qudao_t1_uses_default_date():
    # 默认日期（今天）即锚定 T-1；--date yesterday 会退到 T-2（见 scheduler 注释）
    argv = build_argv("pages-qudao-t1", SETTINGS)
    assert argv[1:4] == ["-m", "common.daily_robot.mart_cli", "leaderboard-html"]
    assert "/out/qudao.html" in argv
    assert "--date" not in argv


# -- 摘要型依赖探针 ------------------------------------------------------------

class _ProbeCursor:
    def __init__(self, hit):
        self._hit = hit
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchone(self):
        return {"hit": 1} if self._hit else None

    def close(self):
        pass


class _ProbeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def _probe_connect(cursor):
    return lambda _settings: _ProbeConnection(cursor)


def test_summary_dep_probe_true_when_completed_summary_in_window():
    from common.public_data.scheduler import build_summary_dep_probe

    cursor = _ProbeCursor(hit=True)
    probe = build_summary_dep_probe(
        _probe_connect(cursor), object(),
        source_name="dingtalk", dataset_like="channel_daily_sales%",
        beijing_hm=(10, 30),
        now=lambda: datetime(2026, 9, 28, 2, 40, tzinfo=timezone.utc),
    )
    assert probe() is True
    _, params = cursor.queries[0]
    # 北京 10:30 阈值 = UTC 02:30；时间列存 UTC
    assert params == (
        "dingtalk", "channel_daily_sales%",
        datetime(2026, 9, 28, 2, 30, tzinfo=timezone.utc),
    )


def test_summary_dep_probe_false_without_matching_summary():
    from common.public_data.scheduler import build_summary_dep_probe

    probe = build_summary_dep_probe(
        _probe_connect(_ProbeCursor(hit=False)), object(),
        source_name="extract", dataset_like="ecom_people",
        beijing_hm=(10, 34),
        now=lambda: datetime(2026, 9, 28, 2, 40, tzinfo=timezone.utc),
    )
    assert probe() is False


def test_summary_dep_probe_fail_closed_on_db_error():
    from common.public_data.scheduler import build_summary_dep_probe

    def broken_connect(_settings):
        raise RuntimeError("db down")

    probe = build_summary_dep_probe(
        broken_connect, object(),
        source_name="dingtalk", dataset_like="channel_daily_sales%",
        beijing_hm=(10, 30),
        now=lambda: datetime(2026, 9, 28, 2, 40, tzinfo=timezone.utc),
    )
    assert probe() is False


def test_scheduler_consults_registered_dep_probes_first():
    calls = []

    def probe():
        calls.append(1)
        return False

    scheduler = make_scheduler({})
    scheduler._dep_probes = {"sync-channel-sales": probe}
    assert scheduler._default_dep_probe("sync-channel-sales") is False
    assert calls == [1]
    # 未登记的依赖维持原有语义（roll-manifest 之外告警后按满足处理）
    assert scheduler._default_dep_probe("some-unknown-line") is True


# -- 命令模板泛化（ops-web「可添加」A 方案，2026-09-30） --------------------

def test_command_spec_robot_family_generalizes_region():
    spec = command_spec("robot-hangzhou")
    assert spec["argv"][0:2] == ["common.daily_robot.mart_cli", "once"]
    assert spec["extra_env"] == {"ROBOT_REGION": "hangzhou"}


def test_command_spec_pages_family_generalizes_region():
    spec = command_spec("pages-qudao")
    assert spec["argv"][0:2] == ["common.daily_robot.mart_cli", "leaderboard-html"]
    assert spec["extra_env"] == {"ROBOT_REGION": "qudao"}


def test_command_spec_leaderboard_family_generalizes_region():
    # 榜单群播报（上云漏配补登，2026-10-07）：leaderboard-<region> → mart_cli leaderboard
    spec = command_spec("leaderboard-hangzhou")
    assert spec["argv"][0:2] == ["common.daily_robot.mart_cli", "leaderboard"]
    assert spec["extra_env"] == {"ROBOT_REGION": "hangzhou"}


def test_command_spec_explicit_wins_over_family():
    # pages-qudao-t1 在显式表，不应被 pages- 家族吞掉
    spec = command_spec("pages-qudao-t1")
    assert "--output" in spec["argv"]
    assert spec["extra_env"] == {"ROBOT_REGION": "qudao"}


def test_command_spec_rejects_dash_in_family_region():
    # 家族 region 不允许中划线（显式表已优先）
    assert command_spec("robot-qudao-t1") is None
    assert command_spec("pages-foo-bar") is None
    assert command_spec("leaderboard-qudao-t1") is None


def test_command_spec_empty_region_is_none():
    assert command_spec("robot-") is None
    assert command_spec("pages-") is None
    assert command_spec("leaderboard-") is None


def test_command_spec_unknown_is_none():
    assert command_spec("bi-web") is None
    assert command_spec("mystery") is None


def test_has_command_mirrors_command_spec():
    assert has_command("robot-hangzhou") is True
    assert has_command("pages-qudao") is True
    assert has_command("leaderboard-hangzhou") is True
    assert has_command("sync-wdt") is True
    assert has_command("bi-web") is False
    assert has_command("mystery") is False


def test_nacos_service_ids_without_server_returns_empty():
    # 未配 Nacos：列举降级为空元组（scheduler 仅按 seed keys）。
    from common.public_data.scheduler import _nacos_service_ids

    assert _nacos_service_ids({"PUBLIC_DATA_NACOS_SERVER": ""}) == ()


# -- 「立即运行一次」通道（pd_ops_run_request，ops-web 写入） ------------------

@dataclass
class _RunReq:
    request_id: int
    service_id: str
    requested_by: str


class FakeRunRequestStore:
    """scheduler._drain_run_requests 依赖的最小 store 接口（离线替身）。"""

    def __init__(self):
        self.pending = []
        self.claims = []
        self.finished = []
        self.rejected = []

    def fetch_pending(self):
        return list(self.pending)

    def claim(self, request_id):
        self.claims.append(request_id)
        return True

    def mark_finished(self, request_id, returncode):
        self.finished.append((request_id, returncode))

    def mark_rejected(self, request_id, note):
        self.rejected.append((request_id, note))


def _run_once_scheduler(configs, store, clock=None):
    return Scheduler(
        config_source=StaticConfigSource(configs),
        service_ids=tuple(configs.keys()),
        runner=RecordingRunner(),
        lock_factory=lambda service_id: NullLock(),
        settings=SETTINGS,
        executor=SyncExecutor(),
        clock=clock or FakeClock(datetime(2026, 9, 21, 9, 0, 0)),
        run_requests=store,
    )


def test_run_once_drains_pending_and_fires():
    store = FakeRunRequestStore()
    store.pending = [_RunReq(1, "sync-wdt", "alice")]
    scheduler = _run_once_scheduler(
        {"sync-wdt": {"schedule": "0 2 * * *"}}, store,
    )
    scheduler.tick()
    # 仅 run-once 触发（时钟不在 cron 窗口，无补跑）
    assert [sid for sid, _ in scheduler._runner.calls] == ["sync-wdt"]
    assert store.claims == [1]
    assert store.finished == [(1, 0)]


def test_run_once_rejects_service_without_command():
    store = FakeRunRequestStore()
    store.pending = [_RunReq(2, "bi-web", "bob")]
    scheduler = _run_once_scheduler({}, store)
    scheduler.tick()
    assert scheduler._runner.calls == []
    assert store.rejected == [(2, "无调度命令映射")]


def test_run_once_skips_when_already_running():
    store = FakeRunRequestStore()
    store.pending = [_RunReq(3, "sync-wdt", "carol")]
    scheduler = _run_once_scheduler(
        {"sync-wdt": {"schedule": "0 2 * * *"}}, store,
    )
    scheduler._running.add("sync-wdt")
    scheduler.tick()
    assert scheduler._runner.calls == []
    assert store.claims == []  # 在途：下 tick 再认领


def test_run_once_leaves_pending_on_unsatisfied_deps():
    # 依赖探针不满足 → 留 pending（不认领、不拒绝），下 tick 重查。
    calls = []

    def probe():
        calls.append(1)
        return False

    store = FakeRunRequestStore()
    store.pending = [_RunReq(4, "sync-wdt", "dave")]
    scheduler = _run_once_scheduler(
        {"sync-wdt": {"schedule": "0 2 * * *", "depends_on": ["roll-manifest"]}},
        store,
        clock=FakeClock(datetime(2026, 9, 21, 9, 0, 0)),
    )
    scheduler._dep_probes = {"sync-wdt": probe}
    scheduler.tick()
    assert scheduler._runner.calls == []
    assert store.claims == []
    assert store.rejected == []


# -- 执行流水（pd_ops_run_history，旁路审计） ---------------------------------

class FakeRunHistoryStore:
    """scheduler._record_start/_record_finish 依赖的最小 store 接口。"""

    def __init__(self):
        self.started = []    # (service_id, trigger_type)
        self.finished = []   # (history_id, returncode)
        self._next_id = 0

    def record_start(self, service_id, trigger_type):
        self._next_id += 1
        self.started.append((service_id, trigger_type))
        return self._next_id

    def record_finish(self, history_id, returncode):
        self.finished.append((history_id, returncode))


def _history_scheduler(configs, history, **kwargs):
    kwargs.setdefault("clock", FakeClock(datetime(2026, 9, 21, 2, 0, 5)))
    scheduler = make_scheduler(configs, **kwargs)
    scheduler._run_history = history
    return scheduler


def test_cron_fire_records_history_start_and_finish():
    history = FakeRunHistoryStore()
    scheduler = _history_scheduler(
        {"sync-wdt": {"schedule": "0 2 * * *"}}, history
    )
    scheduler.tick()
    assert history.started == [("sync-wdt", "cron")]
    assert history.finished == [(1, 0)]


def test_nonzero_exit_recorded_as_failed_history():
    history = FakeRunHistoryStore()
    scheduler = _history_scheduler(
        {"sync-wdt": {"schedule": "0 2 * * *"}}, history,
        runner=RecordingRunner(returncode=3),
    )
    scheduler.tick()
    assert history.finished == [(1, 3)]


def test_lock_unavailable_recorded_as_failed_history():
    # 锁被占用：无退出码（None）→ 流水落 failed，仍能看出"点过火"。
    history = FakeRunHistoryStore()
    scheduler = _history_scheduler(
        {"sync-wdt": {"schedule": "0 2 * * *"}}, history,
        lock_factory=lambda service_id: LockedOutLock(),
    )
    scheduler.tick()
    assert history.started == [("sync-wdt", "cron")]
    assert history.finished == [(1, None)]


def test_history_store_failure_never_blocks_run():
    class BrokenHistory:
        def record_start(self, service_id, trigger_type):
            raise ConnectionError("db down")

        def record_finish(self, history_id, returncode):  # pragma: no cover
            raise AssertionError("不应被调用（start 已失败 → id=None）")

    scheduler = _history_scheduler(
        {"sync-wdt": {"schedule": "0 2 * * *"}}, BrokenHistory()
    )
    scheduler.tick()
    assert [sid for sid, _ in scheduler._runner.calls] == ["sync-wdt"]


def test_run_once_records_run_once_trigger_type():
    history = FakeRunHistoryStore()
    store = FakeRunRequestStore()
    store.pending = [_RunReq(9, "sync-wdt", "alice")]
    scheduler = _run_once_scheduler(
        {"sync-wdt": {"schedule": "0 2 * * *"}}, store,
    )
    scheduler._run_history = history
    scheduler.tick()
    assert history.started == [("sync-wdt", "run_once")]
    assert history.finished == [(1, 0)]


def test_no_history_injection_means_no_recording():
    # 默认不注入（测试/遗留装配）：行为与旧版一致，不炸。
    scheduler, _ = due_scheduler({"sync-wdt": {"schedule": "0 2 * * *"}})
    scheduler.tick()
    assert [sid for sid, _ in scheduler._runner.calls] == ["sync-wdt"]
