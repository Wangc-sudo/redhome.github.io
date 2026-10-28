import unittest
from datetime import date, datetime

from common.public_data.cleaning import rule_version
from common.public_data.data_quality import (
    DataQualityError,
    DataQualityNotFound,
    DataQualityRepository,
    DataQualityRerunWorker,
    DataQualityRunner,
    RERUN_WORKER_LOCK_NAME,
    SourceBatch,
    _wdt_quality_rows,
    data_quality_ddl_statements,
    data_quality_snapshot_ddl_statements,
)
from common.public_data.db import LockUnavailable
from common.public_data.extract_mart import MartExtractError


def _rule(dataset, code, rule_type, field_name=None, params=None, severity="error"):
    return {"dataset": dataset, "rule_code": code, "rule_type": rule_type,
            "field_name": field_name, "params": params or {}, "severity": severity,
            "enabled": 1, "version": 1}


class _Repository:
    def __init__(self, rules=(), overrides=None):
        self.rules = list(rules)
        self.overrides = overrides or {}
        self.runs = []
        self.issues = []
        self.finished = None
        self.sql_count = 2
        self.db_ms = 3
        self.override_reads = 0

    def new_id(self):
        return "00000000-0000-0000-0000-000000000001"

    def list_rules(self, dataset, enabled_only=False):
        return [r for r in self.rules if r["dataset"] == dataset]

    def list_overrides(self, dataset):
        self.override_reads += 1
        return self.overrides

    def create_run(self, **kwargs):
        self.runs.append(kwargs)
        return self.new_id()

    def insert_issues(self, run_id, issues):
        self.issues.extend(issues)

    def finish_run(self, run_id, **kwargs):
        self.finished = kwargs


class _Source:
    def __init__(self, batches=(), business_dates=()):
        self.batches = list(batches)
        self.sql_count = 1
        self.db_ms = 5
        self.channel_business_dates = set(business_dates)

    def iter_batches(self, dataset, **kwargs):
        yield from self.batches


class _SnapshotCursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.connection.statements.append((sql, params))
        if "INSERT INTO `dq_run_snapshot`" in sql:
            self.connection.snapshot = {
                "run_id": params[0], "dataset": params[1],
                "rule_version": params[2], "rules_json": params[3],
                "overrides_json": params[4],
            }
        elif "SELECT * FROM `dq_run_snapshot`" in sql:
            self.row = self.connection.snapshot

    def fetchone(self):
        return self.row

    def close(self):
        pass


class _SnapshotConnection:
    def __init__(self):
        self.statements = []
        self.snapshot = None

    def cursor(self):
        return _SnapshotCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


class DataQualityRepositorySnapshotTests(unittest.TestCase):
    def test_rerun_freezes_enabled_rules_and_active_overrides(self):
        connection = _SnapshotConnection()
        run_id = "00000000-0000-0000-0000-000000000001"
        repo = DataQualityRepository(
            connection, now=lambda: datetime(2026, 10, 27, 1, 2, 3),
            new_id=lambda: run_id,
        )
        rules = [_rule("channel_daily_sales", "CHANNEL_REQUIRED", "required", "channel")]
        overrides = {("raw-1", "channel"): "抖音"}
        repo.list_rules = lambda *_args, **_kwargs: rules
        repo.list_overrides = lambda _dataset: overrides

        expected_version = rule_version(rules)
        created = repo.create_run(
            dataset="channel_daily_sales", mode="rerun", requested_by="owner",
            window_start=date(2026, 10, 26), window_end=date(2026, 10, 26),
            dry_run=False, status="pending",
        )
        rules[0]["severity"] = "warning"
        overrides[("raw-1", "channel")] = "拼多多"

        snapshot = repo.get_run_snapshot(created)
        self.assertEqual("error", snapshot["rules"][0]["severity"])
        self.assertEqual("抖音", snapshot["overrides"][("raw-1", "channel")])
        self.assertEqual(expected_version, snapshot["rule_version"])
        run_insert = next(params for sql, params in connection.statements
                          if "INSERT INTO `dq_run`" in sql)
        self.assertEqual("pending", run_insert[5])

    def test_scoped_close_uses_null_safe_window_and_earlier_same_dataset_runs(self):
        connection = _SnapshotConnection()
        repo = DataQualityRepository(connection)
        repo.get_run = lambda _run_id: {"dataset": "channel_daily_sales"}

        closed = repo.close_resolved_issues("run-current", actor="scheduler")

        self.assertEqual(1, closed)
        update_sql = next(sql for sql, _ in connection.statements
                          if "UPDATE `dq_issue` i" in sql)
        self.assertIn("previous.`id` < current.`id`", update_sql)
        self.assertIn("previous.`dataset` = current.`dataset`", update_sql)
        self.assertIn("previous.`window_start` <=> current.`window_start`", update_sql)
        self.assertIn("previous.`window_end` <=> current.`window_end`", update_sql)
        self.assertIn("current_issue.`id` IS NULL", update_sql)


class DataQualityRunnerTests(unittest.TestCase):
    def test_check_persists_cross_batch_conflicts_without_per_row_sql(self):
        rules = [
            _rule("channel_daily_sales", "CHANNEL_REQUIRED", "required", "channel"),
            _rule("channel_daily_sales", "CHANNEL_UNIQUE", "unique",
                  params={"fields": ["channel", "store_name", "business_date"]}),
        ]
        first = {"source_record_id": "a", "channel": "抖音", "store_name": "甲",
                 "business_date": "2026-10-26"}
        second = dict(first, source_record_id="b")
        repo = _Repository(rules)
        source = _Source([
            SourceBatch([first], ["a"], 100),
            SourceBatch([second], ["b"], 100),
        ])
        result = DataQualityRunner(repo, source).run("channel_daily_sales")
        self.assertEqual(2, result["records_read"])
        self.assertEqual(1, result["records_passed"])
        self.assertEqual(1, result["records_quarantined"])
        self.assertEqual(1, result["duplicates"])
        self.assertEqual(["duplicate_conflict"], [i.reason for i in repo.issues])
        self.assertEqual("finished", repo.finished["status"])
        self.assertEqual(3, result["sql_count"])
        self.assertEqual(1, repo.override_reads)

    def test_preview_uses_proposed_rules_and_has_no_control_writes(self):
        proposed = [_rule("channel_daily_sales", "CHANNEL_REQUIRED", "required", "channel")]
        repo = _Repository(rules=[])
        source = _Source([SourceBatch([{"source_record_id": "a"}], ["a"], 10)])
        result = DataQualityRunner(repo, source).run(
            "channel_daily_sales", mode="preview", rules=proposed, persist=False,
        )
        self.assertEqual(1, result["issue_count"])
        self.assertEqual("required", result["samples"][0]["reason"])
        self.assertEqual([], repo.runs)
        self.assertEqual([], repo.issues)
        self.assertIsNone(repo.finished)

    def test_t1_rule_uses_actual_date_existence_not_max_date(self):
        target = date(2026, 10, 26)
        future = date(2026, 11, 1)
        freshness = _rule("channel_daily_sales", "CHANNEL_T1", "freshness",
                          "business_date", {"policy": "t1_exists"}, "critical")
        repo = _Repository([freshness])
        row = {"source_record_id": "future", "channel": "抖音", "business_date": future}
        source = _Source([SourceBatch([row], ["future"], 10)], [future])
        result = DataQualityRunner(repo, source).run(
            "channel_daily_sales", window_start=target, window_end=target,
        )
        self.assertEqual("missing_business_date", result["samples"][0]["reason"])
        self.assertEqual("blocked", result["alert_status"])

        repo = _Repository([freshness])
        source = _Source([SourceBatch([row], ["future"], 10)], [future, target])
        result = DataQualityRunner(repo, source).run(
            "channel_daily_sales", window_start=target, window_end=target,
        )
        self.assertEqual(0, result["issue_count"])

    def test_wdt_header_and_detail_rules_do_not_cross_contaminate(self):
        rules = [
            _rule("wdt_order_line_fact", "WDT_PAYLOAD", "schema", "payload_json",
                  {"format": "json_object"}),
            _rule("wdt_order_line_fact", "WDT_TRADE", "required", "trade_no"),
            _rule("wdt_order_line_fact", "WDT_SPEC", "required", "spec_no"),
        ]
        invalid = _wdt_quality_rows("raw-1", "not-json", "not-json")
        valid_payload = {"trade_no": "T1", "trade_status": "1",
                         "detail_list": [{"goods_name": "无规格"}]}
        valid = _wdt_quality_rows("raw-2", valid_payload, valid_payload)
        repo = _Repository(rules)
        source = _Source([SourceBatch(invalid + valid, ["raw-1", "raw-2"], 100)])
        result = DataQualityRunner(repo, source).run("wdt_order_line_fact")
        reasons = [(i.source_record_id, i.rule_code, i.reason) for i in repo.issues]
        self.assertIn(("raw-1", "WDT_PAYLOAD", "invalid_json_object"), reasons)
        self.assertIn(("raw-1", "WDT_TRADE", "required"), reasons)
        self.assertIn(("raw-2#1", "WDT_SPEC", "required"), reasons)
        self.assertFalse(any(rule == "WDT_SPEC" and source == "raw-1"
                             for source, rule, _ in reasons))
        self.assertEqual(2, result["records_read"])
        self.assertEqual(2, result["records_quarantined"])
        self.assertEqual(0, result["records_passed"])

    def test_wdt_empty_source_is_a_dataset_alert(self):
        freshness = _rule("wdt_order_line_fact", "WDT_NOT_EMPTY", "freshness",
                          params={"policy": "not_empty", "min_records": 1}, severity="critical")
        repo = _Repository([freshness])
        result = DataQualityRunner(repo, _Source()).run("wdt_order_line_fact")
        self.assertEqual("empty_source", result["samples"][0]["reason"])
        self.assertEqual("blocked", result["alert_status"])
        self.assertEqual(0, result["records_read"])

    def test_reconcile_without_registered_policy_fails_closed(self):
        reconcile = _rule("channel_daily_sales", "CHANNEL_RECONCILE", "reconcile")
        repo = _Repository([reconcile])
        with self.assertRaises(DataQualityError):
            DataQualityRunner(repo, _Source()).run("channel_daily_sales")
        self.assertEqual("failed", repo.finished["status"])

    def test_ddl_contains_control_tables_and_access_indexes(self):
        ddl = "\n".join(data_quality_ddl_statements())
        for table in ("dq_rule", "dq_run", "dq_issue", "dq_value_override", "dq_audit"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS `{table}`", ddl)
        self.assertIn("(`dataset`, `enabled`)", ddl)
        self.assertIn("(`dataset`, `status`, `id`)", ddl)
        self.assertIn("(`dataset`, `source_record_id`, `field_name`)", ddl)

    def test_snapshot_ddl_is_append_only_and_separate_from_v1(self):
        v1 = "\n".join(data_quality_ddl_statements())
        snapshot = "\n".join(data_quality_snapshot_ddl_statements())
        self.assertNotIn("dq_run_snapshot", v1)
        self.assertIn("CREATE TABLE IF NOT EXISTS `dq_run_snapshot`", snapshot)
        self.assertIn("`rules_json` JSON NOT NULL", snapshot)
        self.assertIn("`overrides_json` JSON NOT NULL", snapshot)


class _RerunConnection:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _RerunRepository:
    def __init__(self, run, snapshot, *, baseline=None):
        self.run = run
        self.snapshot = snapshot
        self.baseline = baseline
        self.connection = _RerunConnection()
        self.claim_calls = 0
        self.current_issues = {}
        self.deleted_issue_runs = []
        self.finish_calls = []
        self.close_calls = []
        self.baseline_reads = []
        self.legacy_issues = []
        self.sql_count = 0
        self.db_ms = 0

    def claim_next_rerun(self):
        self.claim_calls += 1
        claimed = dict(self.run)
        if self.run["mode"] == "rerun" and self.run["status"] == "pending":
            self.run["status"] = "running"
        return claimed

    def get_run_snapshot(self, run_id):
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        if self.snapshot is None:
            raise DataQualityNotFound("snapshot missing")
        return self.snapshot

    def delete_run_issues(self, run_id):
        self.deleted_issue_runs.append(run_id)
        previous = self.current_issues.get(run_id, set())
        self.current_issues[run_id] = set()
        return len(previous)

    def close_resolved_issues(self, run_id, *, actor):
        self.close_calls.append((run_id, actor))
        current_fingerprints = self.current_issues.get(run_id, set())
        closed = 0
        for issue in self.legacy_issues:
            same_scope = (
                issue["dataset"] == self.run["dataset"]
                and issue["window_start"] == self.run["window_start"]
                and issue["window_end"] == self.run["window_end"]
                and issue["run_order"] < self.run["id"]
            )
            if (same_scope and issue["status"] == "open"
                    and issue["fingerprint"] not in current_fingerprints):
                issue["status"] = "resolved"
                closed += 1
        return closed

    def previous_issue_baseline(self, run_id):
        self.baseline_reads.append(run_id)
        return self.baseline

    def finish_run(self, run_id, **kwargs):
        self.assert_running(run_id)
        self.finish_calls.append((run_id, kwargs))
        self.run.update(kwargs["counts"])
        self.run.update(
            status=kwargs["status"], alert_status=kwargs["alert_status"],
            failure_code=kwargs["failure_code"], diff=kwargs["diff"],
            sync_run_id=kwargs["sync_run_id"], finished_at=datetime(2026, 10, 27, 4, 5, 6),
        )

    def assert_running(self, run_id):
        if run_id != self.run["run_id"] or self.run["status"] != "running":
            raise AssertionError("terminal update requires the claimed running run")

    def list_rules(self, *_args, **_kwargs):
        raise AssertionError("worker must not read current rules")

    def list_overrides(self, *_args, **_kwargs):
        raise AssertionError("worker must not read current overrides")


class _RerunRunner:
    def __init__(self, repository, outcomes):
        self.repository = repository
        self.outcomes = list(outcomes)
        self.calls = []
        self.source = None

    def run(self, dataset, **kwargs):
        self.calls.append((dataset, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        result = dict(outcome)
        fingerprints = result.get("_issue_fingerprints")
        if fingerprints is not None:
            self.repository.current_issues[kwargs["existing_run_id"]] = set(fingerprints)
        return result


class _ReplayService:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.sync_runs_created = 0
        self.business_publishes = []

    def replay_dataset(self, dataset, **kwargs):
        call = (dataset, kwargs)
        self.calls.append(call)
        if not kwargs["dry_run"]:
            self.sync_runs_created += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if not kwargs["dry_run"]:
            self.business_publishes.append(call)
        return dict(outcome)


class _Outbox:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def enqueue(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return True


class _WorkerLock:
    def __init__(self, connection, name, timeout_seconds, unavailable):
        self.connection = connection
        self.name = name
        self.timeout_seconds = timeout_seconds
        self.unavailable = unavailable
        self.entered = False
        self.exited = False

    def __enter__(self):
        if self.unavailable:
            raise LockUnavailable("held by another worker")
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True
        return False


def _rerun(*, dataset="channel_daily_sales", dry_run=False, mode="rerun",
           status="pending", window_start=date(2026, 10, 26),
           window_end=date(2026, 10, 26)):
    return {
        "id": 100, "run_id": "dq-rerun-1", "dataset": dataset,
        "mode": mode, "status": status, "dry_run": dry_run,
        "window_start": window_start, "window_end": window_end,
        "requested_by": "owner", "reason": "audited correction",
        "rule_version": "frozen-v1",
    }


def _quality_result(*, alert_status="normal", issue_count=0, fingerprints=(),
                    reasons=(), rule_types=(), records_read=4, records_written=0):
    quarantined = records_read if alert_status in {"error", "blocked"} else 0
    return {
        "status": "finished", "alert_status": alert_status,
        "records_read": records_read,
        "records_passed": records_read - quarantined,
        "records_quarantined": quarantined, "records_written": records_written,
        "duplicates": 0, "unmapped": 0, "issue_count": issue_count,
        "sql_count": 7, "db_ms": 11, "batch_count": 1,
        "max_batch_rows": records_read, "max_batch_bytes": 128,
        "samples": [], "_issue_fingerprints": set(fingerprints),
        "_issue_reasons": set(reasons), "_issue_rule_types": set(rule_types),
    }


def _replay_result(*, dry_run=False, records_read=4, records_written=3):
    return {
        "dataset": "channel_daily_sales",
        "sync_run_id": None if dry_run else "sync-rerun-1",
        "records_read": records_read,
        "records_written": 0 if dry_run else records_written,
        "diff": {"before_count": 5, "window_before": 2,
                 "window_after": records_read, "after_count": 3 + records_read,
                 "records_written": 0 if dry_run else records_written},
    }


_DEFAULT_SNAPSHOT = object()


def _rerun_rig(run, quality_outcomes, *, replay_outcomes=(), snapshot=_DEFAULT_SNAPSHOT,
               baseline=None, outbox_error=None, lock_unavailable=False):
    if snapshot is _DEFAULT_SNAPSHOT:
        snapshot = {
            "run_id": run["run_id"], "dataset": run["dataset"],
            "rule_version": "frozen-v1",
            "rules": [_rule(run["dataset"], "FROZEN_REQUIRED", "required",
                            "source_record_id")],
            "overrides": {("raw-1", "source_record_id"): "frozen-source"},
        }
    repository = _RerunRepository(run, snapshot, baseline=baseline)
    runner = _RerunRunner(repository, quality_outcomes)
    replay = _ReplayService(replay_outcomes)
    outbox = _Outbox(outbox_error)
    locks = []

    def lock_factory(connection, name, timeout_seconds=0):
        lock = _WorkerLock(connection, name, timeout_seconds, lock_unavailable)
        locks.append(lock)
        return lock

    worker = DataQualityRerunWorker(
        repository=repository, runner=runner, replay_service=replay, outbox=outbox,
        lock_connection=object(), lock_factory=lock_factory,
        now=lambda: datetime(2026, 10, 27, 3, 4, 5),
    )
    return worker, repository, runner, replay, outbox, locks, snapshot


class DataQualityRerunWorkerTests(unittest.TestCase):
    def test_uses_frozen_snapshot_for_precheck_publish_and_postcheck(self):
        run = _rerun()
        worker, repo, runner, replay, _, locks, snapshot = _rerun_rig(
            run, [_quality_result(), _quality_result()],
            replay_outcomes=[_replay_result()],
        )

        result = worker.run_once()

        self.assertEqual("finished", result["status"])
        self.assertEqual(2, len(runner.calls))
        for dataset, kwargs in runner.calls:
            self.assertEqual(run["dataset"], dataset)
            self.assertIs(snapshot["rules"], kwargs["rules"])
            self.assertIs(snapshot["overrides"], kwargs["overrides"])
            self.assertEqual(run["run_id"], kwargs["existing_run_id"])
            self.assertFalse(kwargs["finish"])
            self.assertTrue(kwargs["collect_fingerprints"])
        replay_kwargs = replay.calls[0][1]
        self.assertIs(snapshot["rules"], replay_kwargs["rules"])
        self.assertIs(snapshot["overrides"], replay_kwargs["overrides"])
        self.assertEqual(RERUN_WORKER_LOCK_NAME, locks[0].name)
        self.assertNotEqual("public-data:extract:channel_daily_sales", locks[0].name)
        self.assertEqual(0, locks[0].timeout_seconds)
        self.assertTrue(locks[0].entered)
        self.assertTrue(locks[0].exited)

    def test_dry_run_gets_diff_without_business_sync_or_publish(self):
        worker, repo, runner, replay, outbox, _, _ = _rerun_rig(
            _rerun(dry_run=True), [_quality_result(records_read=3)],
            replay_outcomes=[_replay_result(dry_run=True, records_read=3)],
        )

        result = worker.run_once()

        self.assertEqual("finished", result["status"])
        self.assertEqual(1, len(runner.calls))
        self.assertEqual(1, len(replay.calls))
        self.assertTrue(replay.calls[0][1]["dry_run"])
        self.assertEqual(0, replay.sync_runs_created)
        self.assertEqual([], replay.business_publishes)
        self.assertEqual([], repo.close_calls)
        finish = repo.finish_calls[0][1]
        self.assertEqual(3, finish["counts"]["records_read"])
        self.assertEqual(0, finish["counts"]["records_written"])
        self.assertIsNone(finish["sync_run_id"])
        self.assertEqual(_replay_result(dry_run=True, records_read=3)["diff"],
                         finish["diff"])
        self.assertEqual([], outbox.calls)

    def test_critical_and_error_prechecks_block_formal_publish(self):
        for alert_status in ("blocked", "error"):
            with self.subTest(alert_status=alert_status):
                worker, repo, runner, replay, outbox, _, _ = _rerun_rig(
                    _rerun(), [_quality_result(
                        alert_status=alert_status, issue_count=1,
                        fingerprints=("bad",), reasons=("required",),
                        rule_types=("required",),
                    )],
                )

                result = worker.run_once()

                self.assertEqual("failed", result["status"])
                self.assertEqual("quality_gate_blocked", result["failure_code"])
                self.assertEqual(alert_status, result["alert_status"])
                self.assertEqual([], replay.calls)
                self.assertEqual(1, len(runner.calls))
                self.assertEqual({"bad"}, repo.current_issues["dq-rerun-1"])
                finish = repo.finish_calls[0][1]
                self.assertEqual("failed", finish["status"])
                self.assertEqual(0, finish["counts"]["records_written"])
                self.assertIsNotNone(repo.run["finished_at"])
                self.assertIn("quality_gate_blocked", outbox.calls[0]["body_md"])

    def test_warning_precheck_allows_formal_publish(self):
        worker, repo, _, replay, _, _, _ = _rerun_rig(
            _rerun(), [
                _quality_result(alert_status="warning", issue_count=1,
                                fingerprints=("warning",)),
                _quality_result(alert_status="warning", issue_count=1,
                                fingerprints=("warning",)),
            ], replay_outcomes=[_replay_result()],
        )

        result = worker.run_once()

        self.assertEqual("finished", result["status"])
        self.assertEqual("warning", result["alert_status"])
        self.assertEqual(1, len(replay.business_publishes))
        self.assertEqual("sync-rerun-1", result["sync_run_id"])
        self.assertEqual(3, result["records_written"])
        self.assertIsNone(repo.run["failure_code"])

    def test_matching_postcheck_fingerprints_close_only_scoped_absent_issues(self):
        worker, repo, _, _, _, _, _ = _rerun_rig(
            _rerun(), [
                _quality_result(alert_status="warning", issue_count=1,
                                fingerprints=("kept",)),
                _quality_result(alert_status="warning", issue_count=1,
                                fingerprints=("kept",)),
            ], replay_outcomes=[_replay_result()],
        )
        day = date(2026, 10, 26)
        repo.legacy_issues = [
            {"dataset": "channel_daily_sales", "window_start": day, "window_end": day,
             "run_order": 90, "fingerprint": "gone", "status": "open"},
            {"dataset": "channel_daily_sales", "window_start": day, "window_end": day,
             "run_order": 90, "fingerprint": "kept", "status": "open"},
            {"dataset": "wdt_order_line_fact", "window_start": day, "window_end": day,
             "run_order": 90, "fingerprint": "gone", "status": "open"},
            {"dataset": "channel_daily_sales", "window_start": date(2026, 10, 25),
             "window_end": date(2026, 10, 25), "run_order": 90,
             "fingerprint": "gone", "status": "open"},
            {"dataset": "channel_daily_sales", "window_start": day, "window_end": day,
             "run_order": 110, "fingerprint": "gone", "status": "open"},
        ]

        result = worker.run_once()

        self.assertEqual(1, result["issues_closed"])
        self.assertTrue(result["postcheck_fingerprint_match"])
        self.assertEqual(["resolved", "open", "open", "open", "open"],
                         [issue["status"] for issue in repo.legacy_issues])
        self.assertEqual({"kept"}, repo.current_issues["dq-rerun-1"])
        self.assertEqual([("dq-rerun-1", "dq-rerun-worker")], repo.close_calls)

    def test_mismatched_postcheck_fingerprints_do_not_close_old_issues(self):
        worker, repo, _, _, _, _, _ = _rerun_rig(
            _rerun(), [
                _quality_result(fingerprints=("before",)),
                _quality_result(fingerprints=("after",)),
            ], replay_outcomes=[_replay_result()],
        )
        day = date(2026, 10, 26)
        repo.legacy_issues = [
            {"dataset": "channel_daily_sales", "window_start": day, "window_end": day,
             "run_order": 90, "fingerprint": "gone", "status": "open"},
        ]

        result = worker.run_once()

        self.assertEqual("finished", result["status"])
        self.assertFalse(result["postcheck_fingerprint_match"])
        self.assertEqual([], repo.close_calls)
        self.assertEqual("open", repo.legacy_issues[0]["status"])
        self.assertEqual({"after"}, repo.current_issues["dq-rerun-1"])

    def test_publish_exception_finishes_failed_with_safe_summary(self):
        worker, repo, runner, _, _, _, _ = _rerun_rig(
            _rerun(), [_quality_result()],
            replay_outcomes=[RuntimeError("SELECT payload; password=secret")],
        )

        result = worker.run_once()

        self.assertEqual("failed", result["status"])
        self.assertEqual("publish_failed", result["failure_code"])
        self.assertEqual(1, len(runner.calls))
        self.assertEqual([], repo.deleted_issue_runs)
        self.assertIsNone(result["sync_run_id"])
        self.assertIsNone(result["diff"])
        self.assertNotIn("secret", str(repo.finish_calls))
        self.assertNotIn("SELECT", str(repo.finish_calls))
        self.assertIsNotNone(repo.run["finished_at"])

    def test_publish_quality_gate_is_blocked_without_success_facts(self):
        for precheck_alert_status in ("normal", "warning"):
            with self.subTest(precheck_alert_status=precheck_alert_status):
                worker, repo, runner, replay, outbox, _, _ = _rerun_rig(
                    _rerun(), [_quality_result(alert_status=precheck_alert_status)],
                    replay_outcomes=[MartExtractError("quality_gate_blocked")],
                )

                result = worker.run_once()

                self.assertEqual("failed", result["status"])
                self.assertEqual("quality_gate_blocked", result["failure_code"])
                self.assertEqual("blocked", result["alert_status"])
                self.assertEqual("blocked", repo.run["alert_status"])
                self.assertIsNone(result["sync_run_id"])
                self.assertIsNone(repo.run["sync_run_id"])
                self.assertIsNone(result["diff"])
                self.assertIsNone(repo.run["diff"])
                self.assertEqual(0, result["records_written"])
                self.assertEqual(0, repo.run["records_written"])
                self.assertEqual([], replay.business_publishes)
                self.assertEqual(1, len(replay.calls))
                self.assertFalse(replay.calls[0][1]["dry_run"])
                self.assertEqual(1, len(runner.calls))
                self.assertEqual([], repo.close_calls)
                self.assertEqual(1, len(outbox.calls))
                self.assertIn("quality_gate_blocked", outbox.calls[0]["body_md"])

    def test_postcheck_exception_preserves_committed_publish_facts(self):
        worker, repo, runner, replay, _, _, _ = _rerun_rig(
            _rerun(), [_quality_result(), RuntimeError("postcheck SQL credential")],
            replay_outcomes=[_replay_result()],
        )

        result = worker.run_once()

        self.assertEqual("failed", result["status"])
        self.assertEqual("postcheck_failed", result["failure_code"])
        self.assertEqual(2, len(runner.calls))
        self.assertEqual(["dq-rerun-1"], repo.deleted_issue_runs)
        self.assertEqual(1, len(replay.business_publishes))
        self.assertEqual("sync-rerun-1", result["sync_run_id"])
        self.assertEqual(3, result["records_written"])
        self.assertEqual(_replay_result()["diff"], result["diff"])
        self.assertEqual([], repo.close_calls)
        self.assertNotIn("credential", str(repo.finish_calls))

    def test_postcheck_quality_failure_is_terminal_and_does_not_close(self):
        worker, repo, _, replay, outbox, _, _ = _rerun_rig(
            _rerun(), [
                _quality_result(),
                _quality_result(alert_status="blocked", issue_count=1,
                                fingerprints=("new",)),
            ], replay_outcomes=[_replay_result()],
        )

        result = worker.run_once()

        self.assertEqual("failed", result["status"])
        self.assertEqual("postcheck_quality_gate_blocked", result["failure_code"])
        self.assertEqual("sync-rerun-1", result["sync_run_id"])
        self.assertEqual(1, len(replay.business_publishes))
        self.assertEqual([], repo.close_calls)
        self.assertIn("quality_gate_blocked", outbox.calls[0]["body_md"])

    def test_alert_failure_does_not_change_committed_business_result(self):
        worker, repo, _, _, outbox, _, _ = _rerun_rig(
            _rerun(), [
                _quality_result(alert_status="warning", issue_count=12,
                                fingerprints=("warning",)),
                _quality_result(alert_status="warning", issue_count=12,
                                fingerprints=("warning",)),
            ], replay_outcomes=[_replay_result()], baseline={"issue_count": 5},
            outbox_error=RuntimeError("outbox credential must not escape"),
        )

        result = worker.run_once()

        self.assertEqual("finished", result["status"])
        self.assertIsNone(result["failure_code"])
        self.assertEqual("sync-rerun-1", result["sync_run_id"])
        self.assertEqual(3, result["records_written"])
        self.assertTrue(result["alert_enqueue_failed"])
        self.assertEqual("finished", repo.run["status"])
        self.assertEqual(0, repo.connection.commits)
        self.assertEqual(1, repo.connection.rollbacks)
        self.assertIn("issue_surge", outbox.calls[0]["body_md"])

    def test_named_lock_unavailable_skips_before_claim(self):
        worker, repo, runner, replay, _, locks, _ = _rerun_rig(
            _rerun(), [], lock_unavailable=True,
        )

        result = worker.run_once()

        self.assertEqual("skipped", result["status"])
        self.assertEqual("lock_unavailable", result["reason"])
        self.assertEqual(0, repo.claim_calls)
        self.assertEqual([], runner.calls)
        self.assertEqual([], replay.calls)
        self.assertEqual("pending", repo.run["status"])
        self.assertEqual(1, len(locks))

    def test_wdt_empty_window_and_policy_failure_enqueue_ops_alert(self):
        run = _rerun(dataset="wdt_order_line_fact")
        worker, _, _, replay, outbox, _, _ = _rerun_rig(
            run, [_quality_result(
                alert_status="blocked", issue_count=1, fingerprints=("empty",),
                reasons=("empty_source",), rule_types=("freshness",), records_read=0,
            )],
        )

        result = worker.run_once()

        self.assertEqual("failed", result["status"])
        self.assertEqual([], replay.calls)
        alert = outbox.calls[0]
        self.assertEqual("ops", alert["region"])
        self.assertEqual("alert", alert["kind"])
        self.assertEqual(run["window_end"], alert["business_date"])
        self.assertEqual("dq-rerun:dq-rerun-1", alert["dedupe_suffix"])
        self.assertIn("wdt_empty_window", alert["body_md"])
        self.assertIn("wdt_quality_policy_failed", alert["body_md"])
        self.assertIn("quality_gate_blocked", alert["body_md"])

    def test_issue_surge_uses_baseline_and_conservative_threshold(self):
        worker, repo, _, _, outbox, _, _ = _rerun_rig(
            _rerun(), [
                _quality_result(alert_status="warning", issue_count=12,
                                fingerprints=("warning",)),
                _quality_result(alert_status="warning", issue_count=12,
                                fingerprints=("warning",)),
            ], replay_outcomes=[_replay_result()], baseline={"issue_count": 5},
        )

        result = worker.run_once()

        self.assertEqual("finished", result["status"])
        self.assertEqual(["dq-rerun-1"], repo.baseline_reads)
        self.assertEqual(1, repo.connection.commits)
        self.assertEqual(0, repo.connection.rollbacks)
        self.assertIn("issue_surge", outbox.calls[0]["body_md"])
        self.assertIn("上一基线 5", outbox.calls[0]["body_md"])
        self.assertFalse(worker._issue_surge(9, 0))
        self.assertFalse(worker._issue_surge(10, 5))
        self.assertFalse(worker._issue_surge(20, 10))
        self.assertTrue(worker._issue_surge(21, 10))

    def test_dry_run_gate_does_not_emit_formal_blocking_alert(self):
        worker, _, _, replay, outbox, _, _ = _rerun_rig(
            _rerun(dry_run=True), [_quality_result(
                alert_status="blocked", issue_count=1, fingerprints=("bad",),
            )],
        )

        result = worker.run_once()

        self.assertEqual("failed", result["status"])
        self.assertEqual("quality_gate_blocked", result["failure_code"])
        self.assertEqual([], replay.calls)
        self.assertEqual([], outbox.calls)

    def test_invalid_claims_fail_closed_without_leaking_exception_details(self):
        reversed_window = _rerun(window_start=date(2026, 10, 27),
                                 window_end=date(2026, 10, 26))
        cases = (
            (_rerun(), DataQualityNotFound("snapshot payload secret"),
             "rerun_snapshot_missing"),
            (reversed_window, _DEFAULT_SNAPSHOT, "invalid_business_window"),
            (_rerun(dataset="unsupported"), _DEFAULT_SNAPSHOT, "unsupported_dataset"),
        )
        for run, snapshot, failure_code in cases:
            with self.subTest(failure_code=failure_code):
                worker, repo, runner, replay, outbox, _, _ = _rerun_rig(
                    run, [], snapshot=snapshot,
                )

                result = worker.run_once()

                self.assertEqual("failed", result["status"])
                self.assertEqual(failure_code, result["failure_code"])
                self.assertEqual("failed", repo.run["status"])
                self.assertIsNotNone(repo.run["finished_at"])
                self.assertEqual([], runner.calls)
                self.assertEqual([], replay.calls)
                self.assertEqual([], outbox.calls)
                self.assertNotIn("secret", str(repo.finish_calls))
                self.assertNotIn("payload", str(repo.finish_calls))

    def test_non_rerun_claim_is_never_processed(self):
        run = _rerun(mode="check")
        worker, repo, runner, replay, _, _, _ = _rerun_rig(run, [])

        result = worker.run_once()

        self.assertEqual("skipped", result["status"])
        self.assertEqual("not_pending_rerun", result["reason"])
        self.assertEqual([], runner.calls)
        self.assertEqual([], replay.calls)
        self.assertEqual([], repo.finish_calls)
        self.assertEqual("pending", repo.run["status"])


if __name__ == "__main__":
    unittest.main()
