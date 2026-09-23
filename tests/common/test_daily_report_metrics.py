"""Tests for the shared daily-report metrics (spec section 9/10)."""

import unittest
from datetime import date
from decimal import Decimal

from common.metrics.daily_report import (
    achievement_rate,
    elapsed_workdays,
    fetch_channel_dept_rollup,
    fetch_channel_monthly_targets,
    fetch_filled_names,
    fetch_month_facts,
    fetch_region_members,
    fetch_unfilled_members,
    fetch_workdays,
    summarize_people,
    unfilled_members,
)


class _Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self.rows)

    def close(self):
        pass


class _Conn:
    def __init__(self, rows=()):
        self.cursor_instance = _Cursor(rows)

    def cursor(self):
        return self.cursor_instance


# ---------------------------------------------------------------------------
# achievement_rate
# ---------------------------------------------------------------------------

class AchievementRateTests(unittest.TestCase):

    def test_plain_ratio_without_unit_conversion(self):
        self.assertAlmostEqual(achievement_rate(300.0, 3000.0), 0.1)

    def test_missing_or_nonpositive_target_is_none(self):
        self.assertIsNone(achievement_rate(300.0, None))
        self.assertIsNone(achievement_rate(300.0, 0))
        self.assertIsNone(achievement_rate(300.0, -5))


# ---------------------------------------------------------------------------
# elapsed_workdays
# ---------------------------------------------------------------------------

class ElapsedWorkdaysTests(unittest.TestCase):
    _WORKDAYS = {date(2026, 9, d) for d in (9, 10, 11, 12)}

    def test_today_is_excluded_by_default(self):
        self.assertEqual(
            elapsed_workdays(self._WORKDAYS, today=date(2026, 9, 11)),
            {date(2026, 9, 9), date(2026, 9, 10)},
        )

    def test_include_today(self):
        self.assertEqual(
            elapsed_workdays(
                self._WORKDAYS, today=date(2026, 9, 11), include_today=True
            ),
            {date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11)},
        )

    def test_rejects_non_date_today(self):
        with self.assertRaises(TypeError):
            elapsed_workdays(self._WORKDAYS, today="2026-09-11")


# ---------------------------------------------------------------------------
# summarize_people
# ---------------------------------------------------------------------------

def _row(name, day, sales, target):
    return {
        "responsible_person": name,
        "business_date": date(2026, 9, day),
        "sales_amount": sales,
        "monthly_target": target,
    }


class SummarizePeopleTests(unittest.TestCase):
    # 2026-09：休息日 [6,13,19,25,26,27]，today=9/11 → 已过工作日
    # {1,2,3,4,5,7,8,9,10} 共 9 天（与 spec §10 口径自证同一场景）。
    _ELAPSED = {date(2026, 9, d) for d in (1, 2, 3, 4, 5, 7, 8, 9, 10)}

    def test_canonical_september_scenario(self):
        rows = [
            _row("张三", 1, Decimal("100"), Decimal("3000")),
            _row("张三", 2, Decimal("100"), Decimal("3000")),
            _row("张三", 3, Decimal("100"), Decimal("3000")),
            _row("张三", 4, None, Decimal("3000")),          # 空值 = 未填
            # 5、7、8、9、10 无事实行
        ]
        summary = summarize_people(rows, elapsed_days=self._ELAPSED)["张三"]
        self.assertEqual(summary.completed, 300.0)
        self.assertEqual(summary.unfilled, 6)
        self.assertEqual(summary.target, 3000.0)
        self.assertAlmostEqual(summary.rate, 0.1)

    def test_only_elapsed_workdays_count_toward_completed(self):
        rows = [
            _row("张三", 1, 100, 3000),
            _row("张三", 12, 999, 3000),   # 未来工作日：不计入也不算未填
            _row("张三", 6, 888, 3000),    # 休息日：不计入
        ]
        summary = summarize_people(rows, elapsed_days=self._ELAPSED)["张三"]
        self.assertEqual(summary.completed, 100.0)
        self.assertEqual(summary.unfilled, 8)

    def test_explicit_zero_is_filled_not_unfilled(self):
        rows = [_row("张三", 1, 0, 3000)]
        summary = summarize_people(rows, elapsed_days={date(2026, 9, 1)})["张三"]
        self.assertEqual(summary.completed, 0.0)
        self.assertEqual(summary.unfilled, 0)
        self.assertEqual(summary.rate, 0.0)

    def test_monthly_target_is_max_never_sum(self):
        """melt 陷阱：每行重复携带月目标，分母取 MAX，SUM 会放大 N 倍。"""
        rows = [
            _row("张三", 1, 100, 3000),
            _row("张三", 2, 100, 3000),
            _row("张三", 3, 100, 3000),
        ]
        summary = summarize_people(rows, elapsed_days=self._ELAPSED)["张三"]
        self.assertEqual(summary.target, 3000.0)
        self.assertAlmostEqual(summary.rate, 300.0 / 3000.0)

    def test_missing_target_yields_none_rate(self):
        rows = [_row("张三", 1, 100, None)]
        summary = summarize_people(rows, elapsed_days=self._ELAPSED)["张三"]
        self.assertIsNone(summary.target)
        self.assertIsNone(summary.rate)

    def test_rows_without_a_person_are_skipped(self):
        rows = [_row("", 1, 100, 3000), _row(None, 2, 100, 3000)]
        self.assertEqual(summarize_people(rows, elapsed_days=self._ELAPSED), {})

    def test_same_day_last_row_wins(self):
        rows = [_row("张三", 1, 100, 3000), _row("张三", 1, 200, 3000)]
        summary = summarize_people(rows, elapsed_days={date(2026, 9, 1)})["张三"]
        self.assertEqual(summary.completed, 200.0)

    def test_people_are_summarised_independently(self):
        rows = [
            _row("张三", 1, 100, 3000),
            _row("李四", 1, 50, 1000),
        ]
        summaries = summarize_people(rows, elapsed_days={date(2026, 9, 1)})
        self.assertAlmostEqual(summaries["张三"].rate, 100 / 3000)
        self.assertAlmostEqual(summaries["李四"].rate, 50 / 1000)


# ---------------------------------------------------------------------------
# unfilled_members
# ---------------------------------------------------------------------------

class UnfilledMembersTests(unittest.TestCase):

    def test_order_is_preserved(self):
        members = [{"name": "张三"}, {"name": "李四"}, {"name": "王五"}]
        self.assertEqual(
            [m["name"] for m in unfilled_members(members, {"李四"})],
            ["张三", "王五"],
        )

    def test_aliases_map_directory_names_to_table_names(self):
        """dim 存通讯录实名、事实表存表内用名：不经映射会永远判未填。"""
        members = [{"name": "张三丰"}]
        self.assertEqual(unfilled_members(members, {"张三丰"}), [])
        self.assertEqual(len(unfilled_members(members, {"老张"})), 1)
        self.assertEqual(
            unfilled_members(members, {"老张"}, aliases={"张三丰": "老张"}),
            [],
        )


# ---------------------------------------------------------------------------
# 结构性补全查询
# ---------------------------------------------------------------------------

class FetchTests(unittest.TestCase):

    def test_fetch_workdays_filters_and_ranges(self):
        conn = _Conn([
            {"business_date": date(2026, 9, 1)},
            {"business_date": date(2026, 9, 2)},
        ])
        result = fetch_workdays(conn, year=2026, month=9)

        sql, params = conn.cursor_instance.executed[0]
        self.assertIn("FROM `dim_calendar`", sql)
        self.assertIn("`is_workday` = 1", sql)
        self.assertEqual(params, (date(2026, 9, 1), date(2026, 9, 30)))
        self.assertEqual(result, {date(2026, 9, 1), date(2026, 9, 2)})

    def test_fetch_month_facts_scopes_region_and_month(self):
        conn = _Conn()
        fetch_month_facts(conn, region="hangzhou", year=2026, month=9)

        sql, params = conn.cursor_instance.executed[0]
        self.assertIn("FROM `fact_daily_report_offline`", sql)
        self.assertIn("`monthly_target`", sql)
        self.assertIn("`department`", sql)
        self.assertEqual(params, ("hangzhou", date(2026, 9, 1), date(2026, 9, 30)))

    def test_fetch_month_facts_february_uses_real_month_length(self):
        conn = _Conn()
        fetch_month_facts(conn, region="hangzhou", year=2026, month=2)
        _, params = conn.cursor_instance.executed[0]
        self.assertEqual(params[2], date(2026, 2, 28))

    def test_fetch_region_members_only_active(self):
        conn = _Conn([{"user_id": "u1", "name": "张三"}])
        rows = fetch_region_members(conn, region="hangzhou")

        sql, params = conn.cursor_instance.executed[0]
        self.assertIn("FROM `dim_robot_member`", sql)
        self.assertIn("`is_active` = 1", sql)
        self.assertEqual(params, ("hangzhou",))
        self.assertEqual(rows, [{"user_id": "u1", "name": "张三"}])

    def test_fetch_filled_names_returns_a_set(self):
        conn = _Conn([
            {"responsible_person": "张三"},
            {"responsible_person": "李四"},
        ])
        result = fetch_filled_names(
            conn, region="hangzhou", business_date=date(2026, 9, 10)
        )

        sql, params = conn.cursor_instance.executed[0]
        self.assertIn("DISTINCT `responsible_person`", sql)
        self.assertEqual(params, ("hangzhou", date(2026, 9, 10)))
        self.assertEqual(result, {"张三", "李四"})

    def test_fetch_unfilled_members_is_an_anti_join(self):
        conn = _Conn([{"user_id": "u2", "name": "李四"}])
        rows = fetch_unfilled_members(
            conn, region="hangzhou", business_date=date(2026, 9, 10)
        )

        sql, params = conn.cursor_instance.executed[0]
        self.assertIn("LEFT JOIN `fact_daily_report_offline`", sql)
        self.assertIn("ON f.`responsible_person` = m.`name`", sql)
        self.assertIn("f.`source_record_id` IS NULL", sql)
        self.assertEqual(params, ("hangzhou", date(2026, 9, 10), "hangzhou"))
        self.assertEqual(rows, [{"user_id": "u2", "name": "李四"}])


# ---------------------------------------------------------------------------
# qudao 店铺粒度部门真值（2026-09-23 共管店重复计数修复）
# ---------------------------------------------------------------------------

class _RoutingCursor:
    """按 SQL 里的表名路由到不同行集。"""

    def __init__(self, sales_rows=(), target_rows=(), fail=False):
        self._sales = list(sales_rows)
        self._targets = list(target_rows)
        self._fail = fail
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._fail:
            raise RuntimeError("table does not exist")
        self._rows = (
            self._sales if "fact_channel_daily_sales" in sql else self._targets
        )

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _RoutingConn:
    def __init__(self, sales_rows=(), target_rows=(), fail=False):
        self.cursor_instance = _RoutingCursor(sales_rows, target_rows, fail)

    def cursor(self):
        return self.cursor_instance


class ChannelDeptRollupTests(unittest.TestCase):

    def test_completed_uses_elapsed_days_and_target_sums_stores(self):
        conn = _RoutingConn(
            sales_rows=[
                {"channel": "直播", "s": Decimal("11744156.00")},
                {"channel": "天猫", "s": Decimal("2696396.00")},
            ],
            target_rows=[
                {"channel": "直播", "t": Decimal("25311000.00")},
                {"channel": "猫超", "t": Decimal("15000000.00")},
            ],
        )
        rollup = fetch_channel_dept_rollup(
            conn, year=2026, month=9,
            elapsed_days={date(2026, 9, 21), date(2026, 9, 22)},
        )
        self.assertEqual(rollup["直播"]["completed"], 11744156.0)
        self.assertEqual(rollup["直播"]["target"], 25311000.0)
        self.assertIsNone(rollup["天猫"]["target"])     # 无目标行 → None
        self.assertEqual(rollup["猫超"]["completed"], 0.0)  # 无销售行 → 0
        # 销售 SQL 用 IN 过滤已过工作日
        sql, params = conn.cursor_instance.executed[0]
        self.assertIn("IN (%s, %s)", sql)
        self.assertEqual(
            set(params), {date(2026, 9, 21), date(2026, 9, 22)}
        )

    def test_table_missing_fails_open(self):
        conn = _RoutingConn(fail=True)
        self.assertEqual(
            fetch_channel_dept_rollup(
                conn, year=2026, month=9, elapsed_days={date(2026, 9, 22)}
            ),
            {},
        )
        self.assertEqual(fetch_channel_monthly_targets(conn), {})

    def test_monthly_targets_keyed_by_channel(self):
        conn = _RoutingConn(target_rows=[
            {"channel": "拼多多", "t": Decimal("3620000.00")},
        ])
        self.assertEqual(
            fetch_channel_monthly_targets(conn), {"拼多多": 3620000.0}
        )


if __name__ == "__main__":
    unittest.main()
