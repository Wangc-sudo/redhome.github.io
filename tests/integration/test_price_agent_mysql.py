"""真实 MySQL：活跃任务唯一、租约、登录恢复、完成重放与超时防旧写。"""
import json
import os
import unittest
from datetime import datetime, timedelta

from common.price_agent.config import config_hash, normalize_config
from common.price_agent.service import (
    PriceAgentProtocolError,
    claim_job,
    complete_job,
    dispatch_job,
    expire_jobs,
    heartbeat_job,
)
from common.public_data.db import connect
from common.public_data.live_migrations import apply_live_migrations
from common.public_data.settings import Settings

NOW = datetime(2026, 11, 3, 2, 0, 0)


def _config():
    return normalize_config({
        "subjects": ["110183794", "110342174"],
        "restore_subject": "110342174",
        "artifacts": {"bucket": "bucket-68cc", "prefix": "price-competition-integration"},
        "notify": {"enabled": True, "region": "ops", "on_failure": True},
    })


@unittest.skipUnless(
    os.environ.get("INTEGRATION_TEST_RUNNER") == "1",
    "Requires the Docker Compose MySQL integration environment.",
)
class PriceAgentMySQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = Settings.from_environment()
        cls.dingtalk = connect(cls.settings.dingtalk_database)
        cls.wdt = connect(cls.settings.wdt_database)
        cls.connection = connect(cls.settings.mart_database)
        apply_live_migrations(cls.dingtalk, cls.wdt, cls.connection)

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()
        cls.dingtalk.close()
        cls.wdt.close()

    def setUp(self):
        self.job_ids = []

    def tearDown(self):
        with self.connection.cursor() as cursor:
            for job_id in self.job_ids:
                for table in (
                    "fact_maochao_price_snapshot", "pd_price_agent_artifact",
                    "pd_price_agent_job",
                ):
                    cursor.execute(f"DELETE FROM `{table}` WHERE `job_id` = %s", (job_id,))
                cursor.execute(
                    "DELETE FROM `robot_outbox` WHERE `dedupe_key` LIKE %s",
                    (f"%{job_id}%",),
                )
        self.connection.commit()

    def _dispatch_and_claim(self):
        config = _config()
        result = dispatch_job(
            self.connection, config, config_hash(config), source="run_once", now=NOW,
        )
        self.assertEqual("queued", result["status"])
        self.job_ids.append(result["job_id"])
        claimed = claim_job(self.connection, "integration-agent", config, config_hash(config), now=NOW)
        self.assertEqual("claimed", claimed["status"])
        return config, result["job_id"], claimed

    def _payload(self, job_id, claimed):
        return {
            "complete": True,
            "agent_id": "integration-agent",
            "lease_token": claimed["lease_token"],
            "restored_subject": "110342174",
            "subjects": [
                {"subject_key": "110183794", "status": "completed", "row_count": 1,
                 "pages_captured": 1, "page_size_100": True},
                {"subject_key": "110342174", "status": "completed", "row_count": 0,
                 "pages_captured": 1, "page_size_100": False, "empty": True},
            ],
            "rows": [{
                "business_date": "2026-11-03", "subject_key": "110183794",
                "dept_level2": "代销", "item_id": "", "sku_id": "", "product": "集成测试商品",
                "product_spec": "500ml", "market_source": "京东",
                "market_product": "同款", "market_spec": "500ml",
                "market_price": "100", "current_price": "101", "suggested_price": "99",
                "price_gap": "1", "page_status": "跟价成功", "page_number": 1,
                "row_index": 1, "captured_at": "2026-11-03T10:00:00+08:00",
            }],
            "artifacts": [{
                "subject_key": "", "artifact_type": "json", "bucket": "bucket-68cc",
                "object_key": f"price-competition-integration/2026/11/03/{job_id}/result.json",
                "sha256": "a" * 64, "size_bytes": 100, "upload_status": "uploaded",
            }],
        }

    def test_full_job_and_complete_replay(self):
        config, job_id, claimed = self._dispatch_and_claim()
        second = dispatch_job(self.connection, config, config_hash(config), source="cron", now=NOW)
        self.assertEqual("active", second["status"])
        self.assertEqual(job_id, second["job"]["job_id"])
        heartbeat_job(self.connection, job_id, {
            "agent_id": "integration-agent", "lease_token": claimed["lease_token"],
            "auth_state": "login_required", "session_version": 0,
        }, now=NOW)
        heartbeat_job(self.connection, job_id, {
            "agent_id": "integration-agent", "lease_token": claimed["lease_token"],
            "auth_state": "valid", "session_version": 1,
        }, now=NOW + timedelta(seconds=15))
        payload = self._payload(job_id, claimed)
        first = complete_job(self.connection, job_id, payload, now=NOW + timedelta(seconds=30))
        second = complete_job(self.connection, job_id, payload, now=NOW + timedelta(seconds=31))
        self.assertEqual(1, first["row_count"])
        self.assertTrue(second["idempotent"])
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS amount FROM `fact_maochao_price_snapshot` WHERE `job_id` = %s",
                (job_id,),
            )
            self.assertEqual(1, cursor.fetchone()["amount"])
            cursor.execute(
                "SELECT COUNT(*) AS amount FROM `robot_outbox` WHERE `dedupe_key` LIKE %s",
                (f"%{job_id}%",),
            )
            # 首次结果只建基线；仅登录失效告警一条。
            self.assertEqual(1, cursor.fetchone()["amount"])

    def test_unclaimed_job_expires_instead_of_blocking_forever(self):
        config = _config()
        result = dispatch_job(
            self.connection, config, config_hash(config), source="cron", now=NOW,
        )
        self.job_ids.append(result["job_id"])
        expired = expire_jobs(self.connection, now=NOW + timedelta(seconds=901))
        self.assertEqual(1, expired["timed_out"])
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT `status` FROM `pd_price_agent_job` WHERE `job_id` = %s",
                (result["job_id"],),
            )
            self.assertEqual("timed_out", cursor.fetchone()["status"])

    def test_expired_lease_cannot_write_late(self):
        _, job_id, claimed = self._dispatch_and_claim()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE `pd_price_agent_job` SET `lease_expires_at` = %s WHERE `job_id` = %s",
                (NOW - timedelta(seconds=1), job_id),
            )
        self.connection.commit()
        expired = expire_jobs(self.connection, now=NOW)
        self.assertEqual(1, expired["timed_out"])
        with self.assertRaisesRegex(PriceAgentProtocolError, "job_terminal"):
            heartbeat_job(self.connection, job_id, {
                "agent_id": "integration-agent", "lease_token": claimed["lease_token"],
                "auth_state": "valid", "session_version": 0,
            }, now=NOW + timedelta(seconds=1))


if __name__ == "__main__":
    unittest.main()
