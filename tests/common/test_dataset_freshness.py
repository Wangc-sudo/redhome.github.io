import os
from dataclasses import replace
from datetime import date, datetime, time
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from common.public_data.dataset_contract import (
    CONTRACT_BY_KEY,
    DatasetContract,
    list_contracts,
    service_recovery_allowed,
    validate_contracts,
)
from common.public_data.freshness_repository import (
    KnowledgeIndexFreshnessRepository,
)
from common.public_data.freshness import (
    STATUS_BLOCKED,
    STATUS_DEGRADED,
    STATUS_FRESH,
    STATUS_STALE,
    STATUS_UNKNOWN,
    FreshnessChecker,
)
from common.public_data.mart_extract_schema import EXTRACT_DATASETS


NOW = datetime(2026, 10, 29, 10, 0)
PUBLISHED = datetime(2026, 10, 28, 23, 30)
COLLECTED = datetime(2026, 10, 28, 23, 20)


def contract(**changes):
    values = dict(
        dataset_key="sample_dataset",
        owner="ops",
        source="dingtalk",
        schedule="0 4 * * *",
        expected_by=time(7),
        max_lag_minutes=120,
        target_table="fact_sample",
        raw_dataset="sample_dataset",
        business_date_column="business_date",
        business_key_columns=("business_date",),
        required_window="t1",
        reconcile_rules=("record_count",),
    )
    values.update(changes)
    return DatasetContract(**values)


class StubChecker(FreshnessChecker):
    def __init__(self, *, now=NOW):
        super().__init__(connection=None, now=lambda: now)
        self.summary = {
            "sync_run_id": "run-1",
            "completed_at": PUBLISHED,
            "run_status": "completed",
            "failure_code": None,
            "records_read": 10,
            "raw_records_written": 10,
            "skipped": False,
        }
        self.raw = {
            "sync_run_id": "raw-1",
            "completed_at": COLLECTED,
            "run_status": "completed",
            "failure_code": None,
            "records_read": 10,
            "raw_records_written": 10,
        }
        self.latest_business_date = date(2026, 10, 28)
        self.window = {"required": True, "complete": True}
        self.quality = None
        self.diff = {"null_business_keys": 0, "record_count_matches": True}

    def _latest_summary(self, dataset_key, source_name):
        return self.summary

    def _latest_raw_summary(self, contract):
        return self.raw

    def _latest_business_date(self, contract, now):
        return self.latest_business_date

    def _check_business_window(self, contract, now):
        return self.window

    def _latest_quality(self, contract):
        return self.quality

    def _source_target_diff(self, contract, mart, raw):
        return self.diff


class DatasetContractTests(TestCase):
    def test_covers_every_registered_extract_dataset(self):
        registered = {item.dataset for item in EXTRACT_DATASETS}
        contracts = set(CONTRACT_BY_KEY)
        self.assertEqual(set(), registered - contracts)

    def test_covers_extract_auxiliary_steps_and_manual_reports(self):
        expected = {
            "dim_calendar", "dim_robot_member", "dim_dept_leader", "ecom_people",
            "ecommerce_monthly", "restaurant_monthly", "platform_bill_monthly",
        }
        self.assertEqual(set(), expected - set(CONTRACT_BY_KEY))

    def test_registry_rejects_duplicate_keys(self):
        item = contract()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_contracts((item, item))

    def test_all_enabled_business_contracts_have_recovery_policy(self):
        for item in list_contracts(kind="business"):
            self.assertTrue(item.auto_retry, item.dataset_key)
            self.assertTrue(item.auto_catchup, item.dataset_key)
            self.assertTrue(item.auto_replay, item.dataset_key)

    def test_service_recovery_uses_conservative_dataset_policy(self):
        self.assertTrue(service_recovery_allowed("extract-channel", "auto_retry"))
        restricted = replace(
            CONTRACT_BY_KEY["ecom_people"], auto_replay=False, auto_catchup=False,
        )
        with patch.dict(CONTRACT_BY_KEY, {"ecom_people": restricted}):
            self.assertFalse(service_recovery_allowed("extract-channel", "auto_replay"))
            self.assertFalse(service_recovery_allowed("extract-channel", "auto_catchup"))
        self.assertTrue(service_recovery_allowed("sync-wdt", "auto_replay"))
        self.assertTrue(service_recovery_allowed("unregistered-service", "auto_replay"))


class KnowledgeFreshnessRepositoryTests(TestCase):
    def test_failure_preserves_last_good_and_increments_attempts(self):
        repository = KnowledgeIndexFreshnessRepository(Mock())
        previous_success = datetime(2026, 10, 28, 3, 17)
        repository.get = Mock(return_value={
            "index_key": "kb_doc_vector", "last_success_at": previous_success,
            "attempts": 2, "indexed_count": 8,
        })
        repository.upsert = Mock()
        item = repository.record({
            "index_key": "kb_doc_vector", "source_type": "wiki",
            "status": "degraded", "last_error": "EmbedError",
        }, checked_at=NOW)
        self.assertEqual(item["last_success_at"], previous_success)
        self.assertEqual(item["attempts"], 3)
        self.assertEqual(item["indexed_count"], 8)
        self.assertEqual(item["last_error"], "EmbedError")

    def test_success_sets_success_time_and_clears_attempts(self):
        repository = KnowledgeIndexFreshnessRepository(Mock())
        repository.get = Mock(return_value={
            "index_key": "kb_doc_vector", "attempts": 3,
            "last_success_at": PUBLISHED,
        })
        repository.upsert = Mock()
        item = repository.record({
            "index_key": "kb_doc_vector", "source_type": "wiki",
            "status": "fresh", "last_error": "ignored",
        }, checked_at=NOW)
        self.assertEqual(item["last_success_at"], NOW)
        self.assertEqual(item["attempts"], 0)
        self.assertIsNone(item["last_error"])

    def test_current_fresh_status_does_not_turn_stale_inside_grace(self):
        repository = KnowledgeIndexFreshnessRepository(Mock())
        repository.get = Mock(return_value={
            "index_key": "kb_doc_vector", "status": "fresh", "checked_at": NOW,
            "last_success_at": NOW, "attempts": 0,
        })
        repository.upsert = Mock()
        item = repository.check_sla(CONTRACT_BY_KEY["kb_doc_vector"], checked_at=NOW)
        self.assertEqual("fresh", item["status"])
        self.assertEqual(NOW, item["last_success_at"])

    def test_old_fresh_status_becomes_stale_when_task_stops(self):
        repository = KnowledgeIndexFreshnessRepository(Mock())
        repository.get = Mock(return_value={
            "index_key": "kb_doc_vector", "status": "fresh",
            "checked_at": datetime(2026, 10, 27, 3, 17),
            "last_success_at": datetime(2026, 10, 27, 3, 17),
        })
        repository.upsert = Mock()
        item = repository.check_sla(CONTRACT_BY_KEY["kb_doc_vector"], checked_at=NOW)
        self.assertEqual("stale", item["status"])
        self.assertEqual("exceeded_max_lag", item["last_error"])
        self.assertEqual(datetime(2026, 10, 27, 3, 17), item["last_success_at"])

    def test_hourly_product_structured_uses_lag_minutes(self):
        repository = KnowledgeIndexFreshnessRepository(Mock())
        repository.get = Mock(return_value={
            "index_key": "kb_product_structured", "status": "fresh",
            "checked_at": NOW.replace(hour=7, minute=59),
        })
        repository.upsert = Mock()
        item = repository.check_sla(
            CONTRACT_BY_KEY["kb_product_structured"], checked_at=NOW,
        )
        self.assertEqual("stale", item["status"])


class FreshnessStateTests(TestCase):
    def test_complete_raw_mart_window_and_reconcile_are_fresh(self):
        result = StubChecker().check(contract())
        self.assertEqual(STATUS_FRESH, result.status)
        self.assertEqual("run-1", result.last_run_id)
        self.assertEqual(COLLECTED, result.last_collected_at)

    def test_missing_required_business_window_is_stale_after_grace(self):
        checker = StubChecker()
        checker.window = {"required": True, "complete": False, "reason": "missing_t1_business_date"}
        checker.latest_business_date = None
        result = checker.check(contract())
        self.assertEqual(STATUS_STALE, result.status)
        self.assertEqual("missing_t1_business_date", result.error)

    def test_future_placeholder_rows_do_not_make_dataset_fresh(self):
        checker = StubChecker()
        checker.latest_business_date = None
        checker.window = {
            "required": True, "complete": False,
            "reason": "missing_t1_business_date",
        }
        result = checker.check(contract())
        self.assertEqual(STATUS_STALE, result.status)

    def test_missing_new_window_inside_grace_is_not_stale(self):
        checker = StubChecker(now=datetime(2026, 10, 29, 0, 30))
        checker.window = {"required": True, "complete": False}
        result = checker.check(contract())
        self.assertEqual(STATUS_FRESH, result.status)

    def test_failed_extract_is_blocked(self):
        checker = StubChecker()
        checker.summary.update(run_status="failed", failure_code="projection_failed")
        result = checker.check(contract())
        self.assertEqual(STATUS_BLOCKED, result.status)
        self.assertEqual("projection_failed", result.error)

    def test_failed_quality_gate_is_blocked(self):
        item = contract(dq_rules=("latest_quality_gate",))
        checker = StubChecker()
        checker.quality = {"status": "finished", "alert_status": "blocked"}
        result = checker.check(item)
        self.assertEqual(STATUS_BLOCKED, result.status)

    def test_missing_required_quality_run_is_unknown(self):
        item = contract(dq_rules=("latest_quality_gate",))
        result = StubChecker().check(item)
        self.assertEqual(STATUS_UNKNOWN, result.status)
        self.assertEqual("no_quality_run", result.error)

    def test_unchanged_skipped_projection_can_still_be_fresh(self):
        checker = StubChecker()
        checker.summary.update(skipped=True, raw_records_written=0)
        result = checker.check(contract())
        self.assertEqual(STATUS_FRESH, result.status)
        self.assertTrue(result.check_details["mart"]["skipped"])

    def test_source_target_count_mismatch_is_degraded(self):
        checker = StubChecker()
        checker.diff["record_count_matches"] = False
        result = checker.check(contract())
        self.assertEqual(STATUS_DEGRADED, result.status)

    def test_null_business_key_is_blocked(self):
        checker = StubChecker()
        checker.diff["null_business_keys"] = 1
        result = checker.check(contract())
        self.assertEqual(STATUS_BLOCKED, result.status)

    def test_missing_summary_is_unknown_not_fresh(self):
        checker = StubChecker()
        checker.summary = None
        result = checker.check(contract())
        self.assertEqual(STATUS_UNKNOWN, result.status)
        self.assertEqual("no_extract_summary", result.error)


class _RecordingOutbox:
    def __init__(self):
        self.calls = []

    def enqueue(self, **kwargs):
        self.calls.append(kwargs)
        return True


class FreshnessAlertTests(TestCase):
    def test_alerts_are_disabled_by_default(self):
        from common.public_data.cli import (
            _freshness_alerts_enabled,
            _freshness_check_enabled,
        )

        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(_freshness_check_enabled())
            self.assertFalse(_freshness_alerts_enabled())
        with patch.dict(os.environ, {
            "DATA_FRESHNESS_CHECK_ENABLE": "0",
            "DATA_FRESHNESS_ALERT_ENABLE": "1",
        }):
            self.assertFalse(_freshness_check_enabled())
            self.assertTrue(_freshness_alerts_enabled())

    def test_fresh_result_does_not_enqueue_alert(self):
        from common.public_data.cli import _enqueue_freshness_alert

        outbox = _RecordingOutbox()
        result = SimpleNamespace(status="fresh")
        self.assertFalse(_enqueue_freshness_alert(
            outbox, contract(), result, created_at=NOW,
        ))
        self.assertEqual([], outbox.calls)

    def test_abnormal_result_enqueues_safe_daily_deduped_alert(self):
        from common.public_data.cli import _enqueue_freshness_alert

        outbox = _RecordingOutbox()
        result = SimpleNamespace(
            status="stale", latest_business_date=date(2026, 10, 27),
            error="exceeded_max_lag",
        )
        self.assertTrue(_enqueue_freshness_alert(
            outbox, contract(), result, created_at=NOW,
        ))
        call = outbox.calls[0]
        self.assertEqual("ops", call["region"])
        self.assertEqual("alert", call["kind"])
        self.assertEqual(NOW.date(), call["business_date"])
        self.assertEqual(
            "freshness:sample_dataset:stale", call["dedupe_suffix"],
        )
        self.assertIn("exceeded_max_lag", call["body_md"])
        self.assertNotIn("run-1", call["body_md"])


class BusinessFreshnessApiTests(TestCase):
    def _client(self, authorize, audit_writer=None):
        from contextlib import contextmanager

        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient

        from common.ops_web.freshness import register_freshness_routes

        app = FastAPI()

        def allow(request: Request):
            return None

        @contextmanager
        def connector():
            yield object()

        register_freshness_routes(
            app, connector=connector, require_capability=allow,
            business_require_capability=authorize, audit_writer=audit_writer,
        )
        return TestClient(app)

    def test_business_view_is_scoped_and_hides_technical_fields(self):
        from common.public_data.freshness_repository import FreshnessRepository

        calls = []
        client = self._client(lambda request, region: calls.append(region))
        with patch.object(FreshnessRepository, "list_all", return_value=[]):
            response = client.get("/api/business/hangzhou/data-freshness")
        self.assertEqual(200, response.status_code)
        self.assertEqual(["hangzhou"], calls)
        items = response.json()["items"]
        self.assertIn("daily_report_offline", {item["dataset_key"] for item in items})
        self.assertNotIn("last_run_id", items[0])
        self.assertNotIn("check_details", items[0])

    def test_business_view_forwards_permission_denial(self):
        from fastapi import HTTPException

        def deny(request, region):
            raise HTTPException(status_code=403, detail="forbidden")

        response = self._client(deny).get("/api/business/shaoxing/data-freshness")
        self.assertEqual(403, response.status_code)

    def test_manual_check_writes_audit(self):
        audit = Mock()
        result = SimpleNamespace(
            dataset_key="daily_report_offline", status="fresh", lag_minutes=1, error=None,
        )
        with patch("common.ops_web.freshness.FreshnessChecker") as checker, \
                patch("common.ops_web.freshness.FreshnessRepository") as repository:
            checker.return_value.check.return_value = result
            response = self._client(None, audit).post(
                "/api/data-freshness/daily_report_offline/check",
            )
        self.assertEqual(200, response.status_code, response.text)
        repository.return_value.upsert.assert_called_once_with(result)
        self.assertEqual("daily_report_offline", audit.call_args.args[2])
        self.assertEqual("fresh", audit.call_args.args[3])
