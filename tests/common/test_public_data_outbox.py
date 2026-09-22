"""Tests for the robot_outbox repository (delivery contract, spec section 9)."""

import json
import unittest
from datetime import date, datetime, timezone

from common.public_data.outbox_repository import OutboxError, OutboxRepository


_NOW = datetime(2026, 9, 11, 10, tzinfo=timezone.utc)
_DAY = date(2026, 9, 11)


class _Cursor:
    def __init__(self, rows=(), rowcount=1, fetchone_row=None):
        self.rows = list(rows)
        self.rowcount = rowcount
        self.fetchone_row = fetchone_row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.fetchone_row

    def close(self):
        pass


class _Conn:
    def __init__(self, cursor):
        self.cursor_instance = cursor

    def cursor(self):
        return self.cursor_instance


def _repo(cursor):
    return OutboxRepository(_Conn(cursor)), cursor


class EnqueueTests(unittest.TestCase):

    def test_enqueue_inserts_pending_row_with_dedupe_key(self):
        repo, cursor = _repo(_Cursor(rowcount=1))
        created = repo.enqueue(
            region="hangzhou",
            kind="remind",
            business_date=_DAY,
            title="销售日报填写提醒",
            body_md="body",
            at_user_ids=["u1", "u2"],
            created_at=_NOW,
        )

        self.assertTrue(created)
        sql, params = cursor.executed[0]
        self.assertIn("INSERT IGNORE INTO `robot_outbox`", sql)
        self.assertEqual(params[0], "hangzhou:remind:2026-09-11")
        self.assertEqual(params[1:4], ("hangzhou", "remind", _DAY))
        self.assertEqual(json.loads(params[6]), ["u1", "u2"])
        self.assertEqual(params[7], _NOW)
        self.assertIn("'pending'", sql)

    def test_enqueue_duplicate_returns_false_without_overwriting(self):
        repo, _ = _repo(_Cursor(rowcount=0))
        created = repo.enqueue(
            region="hangzhou", kind="remind", business_date=_DAY,
            title="t", body_md="b", created_at=_NOW,
        )
        self.assertFalse(created)

    def test_dedupe_suffix_extends_the_key(self):
        repo, cursor = _repo(_Cursor(rowcount=1))
        repo.enqueue(
            region="hangzhou", kind="leaderboard", business_date=_DAY,
            title="t", body_md="b", created_at=_NOW, dedupe_suffix="0830",
        )
        self.assertEqual(
            cursor.executed[0][1][0], "hangzhou:leaderboard:2026-09-11:0830"
        )

    def test_rejects_unknown_kind(self):
        repo, _ = _repo(_Cursor())
        with self.assertRaises(OutboxError):
            repo.enqueue(
                region="hangzhou", kind="sms", business_date=_DAY,
                title="t", body_md="b", created_at=_NOW,
            )

    def test_rejects_bad_fields(self):
        repo, _ = _repo(_Cursor())
        base = dict(
            region="hangzhou", kind="remind", business_date=_DAY,
            title="t", body_md="b", created_at=_NOW,
        )
        for override in (
            {"region": ""}, {"title": ""}, {"body_md": ""},
            {"business_date": "2026-09-11"},
        ):
            with self.assertRaises(OutboxError):
                repo.enqueue(**{**base, **override})


class FetchPendingTests(unittest.TestCase):

    def test_fetch_pending_filters_orders_and_parses_at_ids(self):
        rows = [{
            "dedupe_key": "hangzhou:remind:2026-09-11",
            "region": "hangzhou",
            "kind": "remind",
            "business_date": _DAY,
            "title": "t",
            "body_md": "b",
            "at_user_ids": '["u1"]',
            "attempts": 0,
        }]
        repo, cursor = _repo(_Cursor(rows=rows))
        result = repo.fetch_pending()

        sql, params = cursor.executed[0]
        self.assertIn("`status` = 'pending'", sql)
        self.assertIn("`attempts` < %s", sql)
        self.assertIn("ORDER BY `created_at`", sql)
        self.assertEqual(params, (5, 100))
        self.assertEqual(result[0]["at_user_ids"], ["u1"])

    def test_fetch_pending_null_at_ids_becomes_empty_list(self):
        repo, _ = _repo(_Cursor(rows=[{
            "dedupe_key": "k", "region": "r", "kind": "ding",
            "business_date": _DAY, "title": "t", "body_md": "b",
            "at_user_ids": None, "attempts": 0,
        }]))
        self.assertEqual(repo.fetch_pending()[0]["at_user_ids"], [])

    def test_fetch_pending_validates_limit(self):
        repo, _ = _repo(_Cursor())
        for bad in (0, 1001, "100"):
            with self.assertRaises(OutboxError):
                repo.fetch_pending(limit=bad)

    def test_fetch_pending_claims_rows_with_skip_locked_by_default(self):
        repo, cursor = _repo(_Cursor())
        repo.fetch_pending()
        self.assertIn("FOR UPDATE SKIP LOCKED", cursor.executed[0][0])

    def test_fetch_pending_skip_locked_can_be_disabled(self):
        repo, cursor = _repo(_Cursor())
        repo.fetch_pending(skip_locked=False)
        self.assertNotIn("FOR UPDATE SKIP LOCKED", cursor.executed[0][0])

    def test_skip_locked_leaves_the_single_consumer_query_identical(self):
        """锁子句是纯后缀：WHERE/ORDER/LIMIT/参数与开关前逐字节一致。"""
        repo, cursor = _repo(_Cursor())
        repo.fetch_pending(skip_locked=False)
        base_sql, base_params = cursor.executed[0]

        repo2, cursor2 = _repo(_Cursor())
        repo2.fetch_pending()
        locked_sql, locked_params = cursor2.executed[0]

        self.assertEqual(locked_sql, base_sql + " FOR UPDATE SKIP LOCKED")
        self.assertEqual(locked_params, base_params)


class ListFailedTests(unittest.TestCase):

    def test_list_failed_queries_observability_fields_only(self):
        rows = [{
            "dedupe_key": "hangzhou:remind:2026-09-11",
            "region": "hangzhou",
            "kind": "remind",
            "business_date": _DAY,
            "attempts": 5,
            "last_error": "group_send_failed",
            "created_at": _NOW,
        }]
        repo, cursor = _repo(_Cursor(rows=rows))
        result = repo.list_failed()

        sql, params = cursor.executed[0]
        self.assertIn("`status` = 'failed'", sql)
        self.assertIn("`attempts`", sql)
        self.assertIn("`last_error`", sql)
        self.assertNotIn("`body_md`", sql)
        self.assertEqual(params, (100,))
        self.assertEqual(result[0]["attempts"], 5)
        self.assertEqual(result[0]["last_error"], "group_send_failed")

    def test_list_failed_validates_limit(self):
        repo, _ = _repo(_Cursor())
        for bad in (0, 1001, "100"):
            with self.assertRaises(OutboxError):
                repo.list_failed(limit=bad)


class RequeueTests(unittest.TestCase):

    def test_requeue_resets_only_failed_rows(self):
        repo, cursor = _repo(_Cursor(rowcount=1))
        self.assertTrue(repo.requeue("k"))

        sql, params = cursor.executed[0]
        self.assertIn("`status` = 'pending'", sql)
        self.assertIn("`attempts` = 0", sql)
        self.assertIn("`last_error` = NULL", sql)
        self.assertIn("WHERE `dedupe_key` = %s AND `status` = 'failed'", sql)
        self.assertEqual(params, ("k",))

    def test_requeue_is_idempotent_for_non_failed_rows(self):
        repo, _ = _repo(_Cursor(rowcount=0))
        self.assertFalse(repo.requeue("k"))


class DeliveryStateTests(unittest.TestCase):

    def test_mark_delivered_is_status_guarded(self):
        repo, cursor = _repo(_Cursor(rowcount=1))
        self.assertTrue(repo.mark_delivered("k", delivered_at=_NOW))

        sql, params = cursor.executed[0]
        self.assertIn("`status` = 'delivered'", sql)
        self.assertIn("WHERE `dedupe_key` = %s AND `status` = 'pending'", sql)
        self.assertEqual(params, (_NOW, "k"))

    def test_register_failure_escalates_to_failed_at_the_cap(self):
        repo, cursor = _repo(_Cursor(
            rowcount=1, fetchone_row={"status": "failed"},
        ))
        result = repo.register_failure("k", error_code="dingtalk_send_failed")

        sql, params = cursor.executed[0]
        self.assertIn("`attempts` = `attempts` + 1", sql)
        self.assertIn("IF(`attempts` + 1 >= %s, 'failed', 'pending')", sql)
        self.assertEqual(params, ("dingtalk_send_failed", 5, "k"))
        self.assertTrue(result)

    def test_register_failure_reports_pending_below_the_cap(self):
        repo, _ = _repo(_Cursor(rowcount=1, fetchone_row={"status": "pending"}))
        self.assertFalse(repo.register_failure("k", error_code="x"))

    def test_register_failure_returns_none_when_row_already_terminal(self):
        repo, cursor = _repo(_Cursor(rowcount=0))
        self.assertIsNone(repo.register_failure("k", error_code="x"))
        self.assertEqual(len(cursor.executed), 1)

    def test_error_code_is_truncated_to_the_column_width(self):
        repo, cursor = _repo(_Cursor(rowcount=1, fetchone_row={"status": "pending"}))
        repo.register_failure("k", error_code="x" * 500)
        self.assertEqual(len(cursor.executed[0][1][0]), 255)


if __name__ == "__main__":
    unittest.main()
