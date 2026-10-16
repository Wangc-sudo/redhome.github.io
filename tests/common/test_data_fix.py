# -*- coding: utf-8 -*-
"""数据修正通道（common.public_data.data_fix）测试。"""

import unittest
from datetime import date

from common.public_data.data_fix import (
    data_fix_ddl_statements,
    fetch_recent_audits,
    preview_person_region_realign,
    realign_offline_person_region,
)


class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        return None

    def execute(self, sql, params=None):
        if sql.startswith("SELECT COUNT(*)"):
            self._rows = [{
                "n": self._conn.hit_count,
                "amt": 159230.0,
                "d0": date(2026, 9, 1),
                "d1": date(2026, 9, 23),
            }]
        elif sql.startswith("UPDATE `fact_daily_report_offline`"):
            self.rowcount = self._conn.hit_count
            self._conn.updated = params
        elif sql.startswith("INSERT INTO `dim_data_fix_audit`"):
            self._conn.audits.append(params)
        elif sql.startswith("SELECT `actor`"):
            self._rows = [
                {"actor": a[1], "action": a[2], "fix_type": a[3],
                 "target": a[4], "detail": a[5], "row_count": a[6],
                 "created_at": a[7]}
                for a in self._conn.audits
            ][::-1]
        else:
            self._rows = []

    @property
    def rowcount(self):
        return getattr(self, "_rowcount", 0)

    @rowcount.setter
    def rowcount(self, value):
        self._rowcount = value

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, hit_count=0):
        self.hit_count = hit_count
        self.audits = []
        self.updated = None
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


class DataFixTests(unittest.TestCase):

    def test_ddl_registered_shape(self):
        (ddl,) = data_fix_ddl_statements()
        self.assertIn("dim_data_fix_audit", ddl)
        self.assertIn("CREATE TABLE IF NOT EXISTS", ddl)

    def test_preview_is_read_only(self):
        conn = _Conn(hit_count=20)
        stats = preview_person_region_realign(
            conn, person_name="李树军", from_region="shaoxing",
            to_region="other")
        self.assertEqual(20, stats["row_count"])
        self.assertEqual(159230.0, stats["sales_amount"])
        self.assertEqual("2026-09-01", stats["date_from"])
        self.assertIsNone(conn.updated)   # 预览零写入
        self.assertEqual(0, conn.committed)

    def test_apply_updates_and_audits_in_one_transaction(self):
        conn = _Conn(hit_count=20)
        stats = realign_offline_person_region(
            conn, person_name="李树军", from_region="shaoxing",
            to_region="other", actor="admin")
        self.assertEqual(("other", "李树军", "shaoxing"), conn.updated)
        self.assertEqual(1, conn.committed)
        self.assertEqual(0, conn.rolled_back)
        self.assertEqual(1, len(conn.audits))
        audit = conn.audits[0]
        self.assertEqual("admin", audit[1])
        self.assertEqual("realign", audit[2])
        self.assertEqual("offline_person_region_realign", audit[3])
        self.assertEqual("李树军", audit[4])
        self.assertIn("shaoxing→other", audit[5])
        self.assertEqual(20, audit[6])
        self.assertEqual(20, stats["row_count"])

    def test_zero_hit_writes_nothing_and_no_audit(self):
        conn = _Conn(hit_count=0)
        stats = realign_offline_person_region(
            conn, person_name="不存在", from_region="shaoxing",
            to_region="other", actor="admin")
        self.assertEqual(0, stats["row_count"])
        self.assertIsNone(conn.updated)
        self.assertEqual(0, conn.committed)
        self.assertEqual([], conn.audits)

    def test_fetch_recent_audits_desc(self):
        conn = _Conn(hit_count=1)
        realign_offline_person_region(
            conn, person_name="甲", from_region="a", to_region="b",
            actor="admin")
        rows = fetch_recent_audits(conn, limit=10)
        self.assertEqual(1, len(rows))
        self.assertEqual("甲", rows[0]["target"])


if __name__ == "__main__":
    unittest.main()
