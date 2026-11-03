import hashlib
import json
import unittest
from datetime import datetime

from common.price_agent.config import (
    PriceAgentConfigError,
    config_hash,
    normalize_config,
    parse_config,
)
from common.price_agent.service import (
    PriceAgentProtocolError,
    _result_message,
    claim_job,
    complete_job,
    dispatch_job,
    heartbeat_job,
    normalize_snapshot_row,
    report_auth_expired,
    result_fingerprint,
    validate_completion,
)


NOW = datetime(2026, 11, 3, 2, 0, 0)
JOB_ID = "11111111-1111-1111-1111-111111111111"
LEASE = "lease-secret"


def _config():
    return normalize_config({
        "subjects": ["110183794", "110342174"],
        "restore_subject": "110342174",
        "artifacts": {"bucket": "bucket-68cc", "prefix": "price-competition"},
        "notify": {"enabled": True, "region": "ops", "on_failure": True},
    })


def _row(**changes):
    value = {
        "business_date": "2026-11-03",
        "subject_key": "110183794",
        "dept_level2": "代销",
        "item_id": "",
        "sku_id": "",
        "product": "习酒窖藏1988",
        "product_spec": "53°500ml*6",
        "market_source": "京东",
        "market_product": "同款商品",
        "market_spec": "500ml",
        "market_price": "100.00",
        "current_price": "101.00",
        "suggested_price": "99.00",
        "price_gap": "1.00",
        "page_status": "跟价成功",
        "page_number": 1,
        "row_index": 1,
        "captured_at": "2026-11-03T10:00:00+08:00",
    }
    value.update(changes)
    return value


def _artifact():
    return {
        "subject_key": "",
        "artifact_type": "json",
        "bucket": "bucket-68cc",
        "object_key": f"price-competition/2026/11/03/{JOB_ID}/result.json",
        "sha256": "a" * 64,
        "size_bytes": 100,
        "upload_status": "uploaded",
    }


def _payload():
    return {
        "complete": True,
        "agent_id": "windows-1",
        "lease_token": LEASE,
        "restored_subject": "110342174",
        "subjects": [
            {"subject_key": "110183794", "status": "completed", "row_count": 1,
             "pages_captured": 1, "page_size_100": True},
            {"subject_key": "110342174", "status": "completed", "row_count": 0,
             "pages_captured": 1, "page_size_100": False, "empty": True},
        ],
        "rows": [_row()],
        "artifacts": [_artifact()],
    }


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = 1
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        pass

    def execute(self, sql, params=None):
        self._last = sql
        self.connection.executed.append((sql, params))
        self.rowcount = 1
        if sql.startswith("UPDATE `pd_price_agent_job` SET `status` = %s, `lease_expires_at`"):
            self.connection.job.update({
                "status": params[0], "lease_expires_at": params[1],
                "heartbeat_at": params[2], "current_subject": params[3],
                "current_page": params[4], "auth_state": params[5],
                "login_required_since": params[7], "session_version": params[8],
            })
        elif sql.startswith("UPDATE `pd_price_agent_job` SET `status` = 'completed'"):
            self.connection.job.update({
                "status": "completed", "row_count": params[0],
                "result_fingerprint": params[2], "completion_hash": params[3],
            })
        elif sql.startswith("INSERT INTO `fact_maochao_price_snapshot`"):
            self.connection.facts.append(params)
        elif sql.startswith("INSERT INTO `pd_price_agent_artifact`"):
            self.connection.artifacts.append(params)
        elif sql.startswith("INSERT IGNORE INTO `robot_outbox`"):
            self.connection.outbox.append(params)

    def fetchone(self):
        if self._last.startswith("SELECT `job_id` FROM `pd_price_agent_job`"):
            return {"job_id": self.connection.job["job_id"]}
        if self._last.startswith("SELECT * FROM `pd_price_agent_job`"):
            return self.connection.job
        if "SELECT `result_fingerprint`" in self._last:
            return self.connection.previous
        if "WHERE `active_pipeline`" in self._last:
            return self.connection.active
        return None

    def fetchall(self):
        return []


class _Connection:
    def __init__(self, job):
        self.job = job
        self.previous = None
        self.active = None
        self.facts = []
        self.artifacts = []
        self.outbox = []
        self.executed = []
        self.commits = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _running_job():
    return {
        "job_id": JOB_ID,
        "pipeline": "maochao",
        "status": "running",
        "agent_id": "windows-1",
        "lease_token_hash": hashlib.sha256(LEASE.encode()).hexdigest(),
        "config_snapshot": json.dumps(_config(), ensure_ascii=False),
        "config_hash": config_hash(_config()),
        "session_version": 0,
        "login_required_since": None,
        "row_count": 0,
        "completion_hash": None,
    }


class ConfigTests(unittest.TestCase):
    def test_normalize_and_hash_are_deterministic(self):
        config = _config()
        self.assertEqual(100, config["page_size"])
        self.assertEqual(config_hash(config), config_hash(dict(config)))
        parsed, digest = parse_config(
            "subjects: ['1']\nrestore_subject: '1'\n"
            "artifacts: {bucket: bucket-68cc, prefix: price-competition}\n"
        )
        self.assertEqual(["1"], parsed["subjects"])
        self.assertEqual(64, len(digest))

    def test_restore_must_be_in_subjects(self):
        with self.assertRaises(PriceAgentConfigError):
            normalize_config({"subjects": ["1"], "restore_subject": "2"})

    def test_disabled_dispatch_does_not_touch_db(self):
        config = _config()
        config["enabled"] = False
        result = dispatch_job(None, config, config_hash(config), source="cron")
        self.assertEqual("disabled", result["status"])


class PayloadTests(unittest.TestCase):
    def test_empty_item_and_sku_still_have_stable_non_null_row_uid(self):
        first = normalize_snapshot_row(_row(), _config())
        second = normalize_snapshot_row(_row(), _config())
        self.assertEqual(64, len(first["row_uid"]))
        self.assertEqual(first["row_uid"], second["row_uid"])
        self.assertEqual("", first["item_id"])

    def test_completion_accepts_empty_subject_without_page_selector(self):
        rows, artifacts, summaries = validate_completion(_payload(), _config(), JOB_ID)
        self.assertEqual(1, len(rows))
        self.assertEqual(1, len(artifacts))
        self.assertTrue(summaries[1]["empty"])

    def test_completion_rejects_missing_subject(self):
        payload = _payload()
        payload["subjects"] = payload["subjects"][:1]
        with self.assertRaisesRegex(PriceAgentProtocolError, "subject_set_mismatch"):
            validate_completion(payload, _config(), JOB_ID)

    def test_artifact_must_be_job_scoped(self):
        payload = _payload()
        payload["artifacts"][0]["object_key"] = "price-competition/2026/11/03/other/result.json"
        with self.assertRaisesRegex(PriceAgentProtocolError, "artifact_job_scope_invalid"):
            validate_completion(payload, _config(), JOB_ID)

    def test_result_fingerprint_ignores_capture_time_and_page(self):
        first = normalize_snapshot_row(_row(), _config())
        second = normalize_snapshot_row(_row(
            captured_at="2026-11-04T11:00:00+08:00", business_date="2026-11-04",
            page_number=2, row_index=8,
        ), _config())
        self.assertEqual(result_fingerprint([first]), result_fingerprint([second]))
        second["market_price"] = "99.00"
        self.assertNotEqual(result_fingerprint([first]), result_fingerprint([second]))


class StateMachineTests(unittest.TestCase):
    def test_claim_uses_job_snapshot_not_later_nacos_config(self):
        job = _running_job()
        job["status"] = "queued"
        connection = _Connection(job)
        current = _config()
        current["subjects"] = ["110342174"]
        current["job_lease_seconds"] = 1200
        claimed = claim_job(
            connection, "windows-1", current, "new-config-hash", now=NOW,
        )
        self.assertEqual(_config()["subjects"], claimed["config"]["subjects"])
        self.assertEqual(config_hash(_config()), claimed["config_hash"])
        self.assertEqual(900, claimed["config"]["job_lease_seconds"])

    def test_result_table_contains_department_status_and_price_gap(self):
        rows, _, _ = validate_completion(_payload(), _config(), JOB_ID)
        row = rows[0]
        self.assertEqual("代销", row["dept_level2"])
        message = _result_message(JOB_ID, [row], 10)
        self.assertIn("| 商品 | 当前价(元) | 建议价(元) | 差额(元) | 状态 | 二级部门 |", message)
        self.assertIn("| 101.00 | 99.00 | 1.00 | 跟价成功 | 代销 |", message)
        self.assertNotIn("101.0000", message)

    def test_complete_is_atomic_and_repeat_returns_original(self):
        connection = _Connection(_running_job())
        payload = _payload()
        first = complete_job(connection, JOB_ID, payload, now=NOW)
        self.assertEqual("completed", first["status"])
        self.assertEqual(1, len(connection.facts))
        self.assertEqual("代销", connection.facts[0][4])
        self.assertEqual(1, len(connection.artifacts))
        second = complete_job(connection, JOB_ID, payload, now=NOW)
        self.assertTrue(second["idempotent"])
        self.assertEqual(1, len(connection.facts))

    def test_standalone_auth_expiry_enqueues_safe_group_alert(self):
        connection = _Connection(_running_job())
        result = report_auth_expired(connection, _config(), {
            "agent_id": "windows-1",
            "session_version": 3,
            "checked_at": "2026-11-03T06:00:00+08:00",
        }, now=NOW)
        self.assertEqual("login_required", result["status"])
        self.assertTrue(result["notified"])
        self.assertEqual(1, len(connection.outbox))
        outbox = connection.outbox[0]
        self.assertEqual("ops", outbox[1])
        self.assertEqual("price_alert", outbox[2])
        self.assertIn("2026-11-03 06:00", outbox[5])
        self.assertIn("Windows 可见 Chrome", outbox[5])
        self.assertTrue(outbox[0].endswith("auth-check:windows-1:3:202611030600"))

    def test_login_required_alerts_once_and_recovery_requests_restart(self):
        connection = _Connection(_running_job())
        payload = {
            "agent_id": "windows-1", "lease_token": LEASE,
            "auth_state": "login_required", "session_version": 0,
        }
        first = heartbeat_job(connection, JOB_ID, payload, now=NOW)
        self.assertEqual("wait_login", first["action"])
        self.assertEqual(1, len(connection.outbox))
        second = heartbeat_job(connection, JOB_ID, payload, now=NOW)
        self.assertEqual("waiting_login", second["status"])
        self.assertEqual(1, len(connection.outbox))
        recovered = heartbeat_job(connection, JOB_ID, {
            **payload, "auth_state": "valid", "session_version": 1,
        }, now=NOW)
        self.assertEqual("restart_job", recovered["action"])

    def test_stale_lease_is_rejected(self):
        connection = _Connection(_running_job())
        with self.assertRaisesRegex(PriceAgentProtocolError, "lease_invalid"):
            heartbeat_job(connection, JOB_ID, {
                "agent_id": "windows-1", "lease_token": "stale",
                "auth_state": "valid", "session_version": 0,
            }, now=NOW)


if __name__ == "__main__":
    unittest.main()
