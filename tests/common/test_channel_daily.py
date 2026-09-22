"""电商渠道日销快报（channel_daily）测试。"""

import unittest
from datetime import date

from common.daily_robot.channel_daily import (
    CHANNEL_ORDER,
    build_channel_section,
    fetch_channel_daily,
    fetch_channel_month_facts,
)


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _Conn:
    def __init__(self, rows):
        self.cursor_obj = _Cursor(rows)

    def cursor(self):
        return self.cursor_obj


_WORKDAYS = {date(2026, 9, d) for d in (21, 22, 23)}


class FetchTests(unittest.TestCase):

    def test_fetch_channel_daily_aggregates_and_strips(self):
        conn = _Conn([
            {"channel": "天猫 ", "s": 100.0},
            {"channel": "京东", "s": 50.5},
            {"channel": None, "s": 999.0},
        ])
        result = fetch_channel_daily(conn, business_date=date(2026, 9, 22))
        self.assertEqual(result, {"天猫": 100.0, "京东": 50.5})
        sql, params = conn.cursor_obj.executed[0]
        self.assertIn("sales_amount` IS NOT NULL", sql)
        self.assertEqual(params, (date(2026, 9, 22),))

    def test_fetch_month_facts_groups_by_channel_and_day(self):
        conn = _Conn([
            {"channel": "天猫", "business_date": date(2026, 9, 21), "s": 10.0},
            {"channel": "天猫", "business_date": date(2026, 9, 22), "s": 20.0},
            {"channel": "京东", "business_date": date(2026, 9, 22), "s": 5.0},
        ])
        facts = fetch_channel_month_facts(conn, year=2026, month=9)
        self.assertEqual(facts["天猫"][date(2026, 9, 22)], 20.0)
        self.assertEqual(facts["京东"], {date(2026, 9, 22): 5.0})


class BuildSectionTests(unittest.TestCase):

    def _facts(self):
        return {
            "京东": {date(2026, 9, 21): 10000.0, date(2026, 9, 22): 12000.0},
            "天猫": {date(2026, 9, 21): 20000.0, date(2026, 9, 22): 10000.0},
            "拼多多": {date(2026, 9, 22): 500.0},
        }

    def test_order_follows_channel_order(self):
        body = build_channel_section(
            month_facts=self._facts(),
            business_date=date(2026, 9, 22),
            workdays=_WORKDAYS,
            monthly_targets={},
        )
        rows = [l for l in body.splitlines() if l.startswith("| ") and "渠道" not in l]
        self.assertEqual(
            [r.split("|")[1].strip() for r in rows],
            ["天猫", "京东", "拼多多"],
        )

    def test_mom_and_amounts(self):
        body = build_channel_section(
            month_facts=self._facts(),
            business_date=date(2026, 9, 22),
            workdays=_WORKDAYS,
            monthly_targets={},
        )
        self.assertIn("全渠道9月22日销售额：**2.2万 元**", body)
        self.assertIn("| 京东 | 1.2万 | +20.0% | -- | -- |", body)
        self.assertIn("| 天猫 | 1.0万 | -50.0% | -- | -- |", body)
        # 拼多多无前日 → 环比 --
        self.assertIn("| 拼多多 | 500 | -- | -- | -- |", body)

    def test_targets_show_month_target_and_mtd_rate(self):
        body = build_channel_section(
            month_facts=self._facts(),
            business_date=date(2026, 9, 22),
            workdays=_WORKDAYS,
            monthly_targets={"天猫": 60000},
        )
        # 天猫月目标 6 万，月累计（21+22 工作日）3 万 → 50.0%
        self.assertIn("| 天猫 | 1.0万 | -50.0% | 6.0万 | 50.0% |", body)
        # 未配目标的渠道仍为 --
        self.assertIn("| 京东 | 1.2万 | +20.0% | -- | -- |", body)

    def test_empty_day_still_has_header(self):
        body = build_channel_section(
            month_facts={},
            business_date=date(2026, 9, 22),
            workdays=_WORKDAYS,
            monthly_targets={},
        )
        self.assertIn("全渠道9月22日销售额：**0 元**", body)


if __name__ == "__main__":
    unittest.main()
