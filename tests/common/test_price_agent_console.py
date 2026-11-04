import json
import unittest
from datetime import datetime
from unittest import mock

from fastapi import HTTPException

from common.ops_web.price_agent import list_agents, recollect_job, retry_job


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, sql, params=None):
        self.connection.sql.append((sql, params))
        if "FROM `pd_price_agent_job`" in sql:
            self._result = self.connection.job
        elif "FROM `fact_maochao_price_snapshot`" in sql:
            self._result = self.connection.rows
        elif "FROM `pd_price_agent_status`" in sql:
            self._result = self.connection.agents
        else:
            self._result = None

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result or []


class _Connection:
    def __init__(self, *, job=None, rows=(), agents=()):
        self.job = job
        self.rows = list(rows)
        self.agents = list(agents)
        self.sql = []

    def cursor(self):
        return _Cursor(self)


def _job(status="failed", config=None):
    return {
        "job_id": "job-old",
        "status": status,
        "config_hash": "old-hash",
        "config_snapshot": json.dumps(config or {
            "enabled": True,
            "subjects": ["3306086002", "3305659041"],
            "restore_subject": "3306086002",
            "job_lease_seconds": 1800,
        }),
        "lease_token_hash": "must-not-leak",
        "completion_hash": "must-not-leak",
    }


class PriceAgentConsoleTests(unittest.TestCase):
    def test_retry_requires_terminal_failure(self):
        with self.assertRaises(HTTPException) as caught:
            retry_job(_Connection(job=_job("running")), "job-old")
        self.assertEqual(400, caught.exception.status_code)
        self.assertEqual("job_not_retryable", caught.exception.detail)

    def test_retry_reuses_snapshot_and_records_parent(self):
        with mock.patch("common.ops_web.price_agent.dispatch_job", return_value={"status": "queued"}) as dispatch:
            result = retry_job(_Connection(job=_job()), "job-old")
        self.assertEqual("queued", result["status"])
        self.assertEqual("retry", dispatch.call_args.kwargs["source"])
        self.assertEqual("job-old", dispatch.call_args.kwargs["parent_job_id"])

    def test_recollect_limits_subjects_and_selected_skus(self):
        rows = [{"subject_key": "3306086002", "item_id": "12345", "sku_id": "SKU-1"}]
        with mock.patch("common.ops_web.price_agent.dispatch_job", return_value={"status": "queued"}) as dispatch:
            result = recollect_job(_Connection(job=_job("completed"), rows=rows), "job-old", rows)
        self.assertEqual("queued", result["status"])
        config = dispatch.call_args.args[1]
        self.assertEqual(["3306086002"], config["subjects"])
        self.assertEqual(rows, config["selected_skus"])
        self.assertEqual("recollect", dispatch.call_args.kwargs["source"])

    def test_recollect_rejects_sku_not_in_original_job(self):
        selected = [{"subject_key": "3306086002", "item_id": "99999", "sku_id": "SKU-X"}]
        with self.assertRaises(HTTPException) as caught:
            recollect_job(_Connection(job=_job("completed"), rows=[]), "job-old", selected)
        self.assertEqual("selected_skus_not_in_job", caught.exception.detail)

    def test_agent_status_covers_idle_and_offline(self):
        now = datetime(2026, 11, 4, 12, 0, 0)
        agents = [
            {"agent_id": "online", "last_seen_at": datetime(2026, 11, 4, 11, 59, 40), "last_claim_at": None, "last_job_id": None},
            {"agent_id": "idle", "last_seen_at": datetime(2026, 11, 4, 11, 58, 0), "last_claim_at": None, "last_job_id": None},
            {"agent_id": "offline", "last_seen_at": datetime(2026, 11, 4, 11, 50, 0), "last_claim_at": None, "last_job_id": None},
        ]
        result = list_agents(_Connection(agents=agents), now=now)
        self.assertEqual(["online", "idle", "offline"], [row["status"] for row in result])


if __name__ == "__main__":
    unittest.main()
