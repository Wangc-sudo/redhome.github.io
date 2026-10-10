# -*- coding: utf-8 -*-
"""calendar_store（dim_calendar_override DB 裁决层）离线单测。"""

import json
import tempfile
import unittest
from datetime import date

from common.public_data.calendar_store import (
    apply_overrides,
    delete_override,
    fetch_month_days,
    fetch_overrides,
    fetch_recent_audits,
    import_year_overrides,
    reset_month_overrides,
    rewrite_month,
    upsert_override,
)


class _FakeCursor:
    """按 SQL 内容路由到内存表：overrides / dim / audits。"""

    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def execute(self, sql, params=None):
        conn = self._conn
        conn.executed.append((sql, params))
        self._rows = []
        self.rowcount = 0
        if "dim_calendar_override_audit" in sql and "INSERT" in sql:
            conn.audits.append(params)
        elif sql.startswith("SELECT `business_date`, `is_workday`, `source`, `note` "
                            "FROM `dim_calendar_override`"):
            rows = sorted(conn.overrides.items())
            if params:
                rows = [(d, v) for d, v in rows if params[0] <= d <= params[1]]
            self._rows = [
                {"business_date": d, "is_workday": v[0], "source": v[1],
                 "note": v[2]}
                for d, v in rows
            ]
        elif "DELETE FROM `dim_calendar_override` WHERE `source` = 'manual'" in sql:
            year, month = params
            before = len(conn.overrides)
            conn.overrides = {
                d: v for d, v in conn.overrides.items()
                if not (v[1] == "manual" and d.year == year and d.month == month)
            }
            self.rowcount = before - len(conn.overrides)
        elif "DELETE FROM `dim_calendar_override` WHERE `source` = 'holiday_cn'" in sql:
            (year,) = params
            before = len(conn.overrides)
            conn.overrides = {
                d: v for d, v in conn.overrides.items()
                if not (v[1] == "holiday_cn" and d.year == year)
            }
            self.rowcount = before - len(conn.overrides)
        elif "DELETE FROM `dim_calendar_override` WHERE `business_date`" in sql:
            (day,) = params
            self.rowcount = 1 if conn.overrides.pop(day, None) is not None else 0
        elif "INSERT INTO `dim_calendar_override`" in sql and "VALUES" in sql:
            day, is_workday, source, note, _by, _at = params
            conn.overrides[day] = (is_workday, source, note)
        elif "DELETE FROM `dim_calendar`" in sql:
            year, month = params
            before = len(conn.dim)
            conn.dim = {
                d: v for d, v in conn.dim.items()
                if not (d.year == year and d.month == month)
            }
            self.rowcount = before - len(conn.dim)
        elif "FROM `dim_calendar`" in sql and "ORDER BY `business_date`" in sql:
            year, month = params
            self._rows = [
                {"business_date": d, "is_workday": v[0], "source": v[1],
                 "note": v[2]}
                for d, v in sorted(conn.dim.items())
                if d.year == year and d.month == month
            ]
        elif "FROM `dim_calendar_override_audit`" in sql:
            self._rows = [
                {"actor": a[0], "action": a[1], "business_date": a[2],
                 "year": a[3], "month": a[4], "detail": a[5],
                 "created_at": a[6]}
                for a in reversed(conn.audits)
            ][: params[0] if params else 20]

    def executemany(self, sql, seq):
        self._conn.executed.append((sql, list(seq)))
        if "INSERT INTO `dim_calendar`" in sql:
            for day, is_workday, source, note, _at, _run in self._conn.executed[-1][1]:
                self._conn.dim[day] = (is_workday, source, note)
        elif "INSERT INTO `dim_calendar_override`" in sql:
            for day, is_workday, note, _by, _at in self._conn.executed[-1][1]:
                self._conn.overrides[day] = (is_workday, "holiday_cn", note)
        self.rowcount = len(self._conn.executed[-1][1])

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.overrides = {}
        self.dim = {}
        self.audits = []
        self.executed = []

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _seed_file(document):
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    with handle:
        handle.write(json.dumps(document, ensure_ascii=False))
    return handle.name


class OverrideCrudTests(unittest.TestCase):
    def test_upsert_inserts_then_updates_with_audit(self):
        conn = _FakeConn()
        upsert_override(conn, date(2026, 10, 7), 1, actor="admin", note="改上班")
        self.assertEqual(
            {date(2026, 10, 7): (1, "manual", "改上班")}, conn.overrides)
        upsert_override(conn, date(2026, 10, 7), 0, actor="admin", note="改休息")
        self.assertEqual(
            {date(2026, 10, 7): (0, "manual", "改休息")}, conn.overrides)
        self.assertEqual(["upsert", "upsert"], [a[1] for a in conn.audits])
        self.assertEqual("admin", conn.audits[0][0])

    def test_delete_override_hit_and_miss(self):
        conn = _FakeConn()
        upsert_override(conn, date(2026, 10, 31), 0, actor="admin")
        self.assertTrue(delete_override(conn, date(2026, 10, 31), actor="admin"))
        self.assertFalse(delete_override(conn, date(2026, 10, 31), actor="admin"))
        self.assertEqual(["upsert", "reset"], [a[1] for a in conn.audits])

    def test_reset_month_keeps_holiday_cn_and_other_months(self):
        conn = _FakeConn()
        upsert_override(conn, date(2026, 10, 7), 1, actor="admin")
        upsert_override(conn, date(2026, 10, 31), 0, actor="admin")
        upsert_override(
            conn, date(2026, 11, 2), 0, actor="admin", source="manual")
        conn.overrides[date(2026, 1, 1)] = (0, "holiday_cn", "元旦")
        deleted = reset_month_overrides(conn, 2026, 10, actor="admin")
        self.assertEqual(2, deleted)
        self.assertEqual(
            {date(2026, 11, 2): (0, "manual", None),
             date(2026, 1, 1): (0, "holiday_cn", "元旦")},
            conn.overrides,
        )
        self.assertEqual("reset_month", conn.audits[-1][1])


class ImportYearOverridesTests(unittest.TestCase):
    _FIXTURE = [
        {"date": "2027-01-01", "name": "元旦", "isOffDay": True},
        {"date": "2027-01-04", "name": "元旦调休", "isOffDay": False},
        {"date": "2027-02-16", "name": "春节", "isOffDay": True},
    ]

    def test_replaces_holiday_cn_year_keeps_manual(self):
        conn = _FakeConn()
        conn.overrides[date(2027, 1, 1)] = (0, "holiday_cn", "旧元旦")
        conn.overrides[date(2027, 1, 5)] = (1, "manual", "人工裁决")
        conn.overrides[date(2026, 1, 1)] = (0, "holiday_cn", "元旦")
        stats = import_year_overrides(
            conn, 2027, actor="admin", fetcher=lambda year: self._FIXTURE)
        self.assertEqual(
            {"deleted": 1, "inserted": 3, "holidays": 2, "makeup": 1}, stats)
        self.assertNotIn(date(2027, 1, 5) , ())
        self.assertEqual(
            (1, "manual", "人工裁决"), conn.overrides[date(2027, 1, 5)])
        self.assertEqual(
            (0, "holiday_cn", "元旦"), conn.overrides[date(2026, 1, 1)])
        self.assertEqual(
            (0, "holiday_cn", "元旦"), conn.overrides[date(2027, 1, 1)])
        self.assertEqual(
            (1, "holiday_cn", "元旦调休（调休上班）"),
            conn.overrides[date(2027, 1, 4)],
        )
        self.assertEqual("import", conn.audits[-1][1])


class ApplyOverridesTests(unittest.TestCase):
    def test_override_wins_per_date(self):
        rows = [
            (date(2026, 10, 6), 1, "local", None),
            (date(2026, 10, 7), 0, "local", None),
        ]
        merged = apply_overrides(
            rows, {date(2026, 10, 7): (1, "manual", "改上班")})
        self.assertEqual((date(2026, 10, 6), 1, "local", None), merged[0])
        self.assertEqual((date(2026, 10, 7), 1, "manual", "改上班"), merged[1])


class RewriteMonthTests(unittest.TestCase):
    def _seed(self):
        return _seed_file({
            "version": 1,
            "months": [{
                "year": 2026, "month": 10,
                "bigRestSaturdays": [17],
                "holidays": [1, 2, 3, 4, 5, 6, 7],
                "makeupWorkdays": [10],
            }],
        })

    def test_rewrite_merges_baseline_and_overrides(self):
        conn = _FakeConn()
        conn.overrides[date(2026, 10, 7)] = (1, "manual", "改上班")
        written = rewrite_month(
            conn, self._seed(), 2026, 10, today=date(2026, 10, 15))
        self.assertEqual(31, written)
        # 10-07 法定休被裁决改上班（source/note 随 override）
        self.assertEqual((1, "manual", "改上班"), conn.dim[date(2026, 10, 7)])
        # 10-10 调休上班（基线）
        self.assertEqual((1, "local", None), conn.dim[date(2026, 10, 10)])
        # 10-06 法定休（基线）
        self.assertEqual((0, "local", None), conn.dim[date(2026, 10, 6)])
        # run id 前缀区分 ops 写与 extract 写（CHAR(36) 约束：恰好 36 位）
        sql, seq = conn.executed[-1]
        self.assertTrue(all(str(run).startswith("ops-") and len(str(run)) == 36
                            for *_, run in seq))

    def test_strict_rejects_out_of_baseline_lenient_skips(self):
        conn = _FakeConn()
        seed = self._seed()
        with self.assertRaises(ValueError):
            rewrite_month(conn, seed, 2027, 6, today=date(2026, 10, 15))
        self.assertEqual(
            0, rewrite_month(conn, seed, 2027, 6,
                             today=date(2026, 10, 15), strict=False))


class ReadSideTests(unittest.TestCase):
    def test_fetch_overrides_window_and_month_days_and_audits(self):
        conn = _FakeConn()
        upsert_override(conn, date(2026, 10, 7), 1, actor="a", note="n")
        upsert_override(conn, date(2026, 11, 2), 0, actor="a")
        overrides = fetch_overrides(
            conn, date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(
            {date(2026, 10, 7): (1, "manual", "n")}, overrides)
        conn.dim[date(2026, 10, 7)] = (1, "manual", "n")
        days = fetch_month_days(conn, 2026, 10)
        self.assertEqual([(date(2026, 10, 7), 1, "manual", "n")], days)
        audits = fetch_recent_audits(conn, limit=10)
        self.assertEqual(2, len(audits))
        self.assertEqual("upsert", audits[0]["action"])


if __name__ == "__main__":
    unittest.main()
