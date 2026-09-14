"""Tests for the bi-web cockpit queries (plan Task 3).

Two layers:

* Unit tests (this file, always run): a fake DictCursor-shaped connection
  captures the executed SQL text.  Every query must be *fully static* --
  合计 rows excluded on the offline side, pre-filled future rows truncated
  everywhere, the annual-target scope filter, and never any ``%``
  formatting that splices a user value into the SQL.
* Integration tests (same file, ``INTEGRATION_TEST_RUNNER=1`` only): the
  real Docker Compose MySQL via the difference method, so assertions
  never couple to pre-existing data.
"""

import os
import threading
import time
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from common.bi_web import queries as bi_web_queries
from common.bi_web.queries import (
    annual_target_total,
    channel_annual_total,
    channel_dod,
    channel_month_daily_series,
    channel_month_ranking,
    channel_mtd_comparison,
    channel_mtd_ranking,
    channel_mtd_total,
    channel_options,
    department_mtd_ranking,
    month_bounds,
    month_options,
    offline_annual_total,
    offline_dod,
    offline_mtd_total,
    region_daily_series,
    region_month_daily_series,
    region_month_target,
    region_mtd_total,
    region_options,
    run_bar_channel_mtd,
    run_bar_department_mtd,
    run_kpi_annual_progress,
    run_kpi_channel_dod,
    run_kpi_channel_mtd,
    run_kpi_offline_dod,
    run_kpi_offline_mtd,
    run_kpi_people_completed,
    run_kpi_people_count,
    run_kpi_people_rate,
    run_kpi_region_mtd,
    run_table_channel_mtd,
    run_table_people_leaderboard,
    run_table_store_mtd,
    run_trend_channel_daily,
    run_trend_region_daily,
    store_mtd_ranking,
)
from common.daily_robot.mart_leaderboard import mart_collect
from common.public_data.db import connect, transaction
from common.public_data.live_migrations import apply_live_migrations
from common.public_data.settings import Settings
from common.public_data.target_seed import load_target_seed, replace_dim_target

#: Month-start anchor, exactly as it must appear in the SQL text.
_MONTH_START = "business_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01')"

#: Year-start anchor, exactly as it must appear in the SQL text.
_YEAR_START = "business_date >= MAKEDATE(YEAR(CURDATE()), 1)"

#: Future-row truncation, exactly as it must appear in the SQL text.
_TRUNCATION = "business_date <= CURDATE()"

#: The offline 合计-row exclusion, exactly as it must appear in the SQL text.
_SUMMARY_EXCLUSION = "responsible_person NOT LIKE '%合计%'"

#: The parametrized-query form -- pymysql formats ``sql % params`` when
#: parameters are passed, so every literal ``%`` must be doubled there.
_PARAM_SUMMARY_EXCLUSION = "responsible_person NOT LIKE '%%合计%%'"


class FakeCursor:
    """Records executed SQL; serves scripted rows from its connection."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, query, parameters=None):
        self._connection.executed.append((query, parameters))

    def fetchone(self):
        return self._connection.scripted_fetchone(self._connection.executed[-1][0])

    def fetchall(self):
        return self._connection.scripted_fetchall(self._connection.executed[-1][0])

    def close(self):
        pass


class FakeConnection:
    """DictCursor-shaped fake whose rows are scripted per queried table.

    ``scalars`` maps table name -> the single summed value a total query
    should return; ``rowsets`` maps table name -> the list of rows a
    GROUP BY query should return.  Queries are recognised by the table
    they read, so scripting is independent of call order.
    """

    _TABLES = (
        "fact_daily_report_offline",
        "fact_channel_daily_sales",
        "dim_target",
        "months",  # 伪表键：month_options 的 UNION 查询横跨两张事实表
        "dim_calendar",  # l2-people: fetch_workdays 的日历查询
    )

    def __init__(self, scalars=None, rowsets=None):
        self.executed = []
        self._scalars = dict(scalars or {})
        self._rowsets = dict(rowsets or {})

    def cursor(self):
        return FakeCursor(self)

    def _table_of(self, sql):
        if "UNION" in sql:
            return "months"
        for table in self._TABLES:
            if table in sql:
                return table
        return None

    def scripted_fetchone(self, sql):
        value = self._scalars.get(self._table_of(sql))
        return {"total": Decimal("0") if value is None else value}

    def scripted_fetchall(self, sql):
        return [dict(row) for row in self._rowsets.get(self._table_of(sql), [])]


class SqlShapeTestCase(unittest.TestCase):
    """Shared helper: every query runs exactly one fully static statement."""

    def sole_static_sql(self, connection):
        self.assertEqual(1, len(connection.executed))
        sql, parameters = connection.executed[0]
        self.assertIsNone(parameters)
        return sql


class OfflineSqlShapeTests(SqlShapeTestCase):
    """The three offline-side queries: 合计 excluded, future rows truncated."""

    def test_offline_mtd_total_sql_shape(self):
        connection = FakeConnection(
            scalars={"fact_daily_report_offline": Decimal("1")}
        )

        offline_mtd_total(connection)

        sql = self.sole_static_sql(connection)
        self.assertIn("FROM fact_daily_report_offline", sql)
        self.assertIn(_SUMMARY_EXCLUSION, sql)
        self.assertIn(_MONTH_START, sql)
        self.assertIn(_TRUNCATION, sql)

    def test_offline_annual_total_sql_shape(self):
        connection = FakeConnection(
            scalars={"fact_daily_report_offline": Decimal("1")}
        )

        offline_annual_total(connection)

        sql = self.sole_static_sql(connection)
        self.assertIn("FROM fact_daily_report_offline", sql)
        self.assertIn(_SUMMARY_EXCLUSION, sql)
        self.assertIn(_YEAR_START, sql)
        self.assertIn(_TRUNCATION, sql)

    def test_region_daily_series_sql_shape(self):
        connection = FakeConnection(
            rowsets={"fact_daily_report_offline": []}
        )

        region_daily_series(connection)

        sql = self.sole_static_sql(connection)
        self.assertIn("FROM fact_daily_report_offline", sql)
        self.assertIn(_SUMMARY_EXCLUSION, sql)
        self.assertIn(_MONTH_START, sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertIn("GROUP BY business_date, region", sql)


class ChannelSqlShapeTests(SqlShapeTestCase):
    """The channel-side queries: truncation only, never a 合计 filter."""

    def test_channel_mtd_total_sql_shape(self):
        connection = FakeConnection(
            scalars={"fact_channel_daily_sales": Decimal("1")}
        )

        channel_mtd_total(connection)

        sql = self.sole_static_sql(connection)
        self.assertIn("FROM fact_channel_daily_sales", sql)
        self.assertIn(_MONTH_START, sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertNotIn("合计", sql)

    def test_channel_annual_total_sql_shape(self):
        connection = FakeConnection(
            scalars={"fact_channel_daily_sales": Decimal("1")}
        )

        channel_annual_total(connection)

        sql = self.sole_static_sql(connection)
        self.assertIn("FROM fact_channel_daily_sales", sql)
        self.assertIn(_YEAR_START, sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertNotIn("合计", sql)

    def test_channel_mtd_ranking_sql_shape(self):
        connection = FakeConnection(
            rowsets={"fact_channel_daily_sales": []}
        )

        channel_mtd_ranking(connection)

        sql = self.sole_static_sql(connection)
        self.assertIn("FROM fact_channel_daily_sales", sql)
        self.assertIn(_MONTH_START, sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertIn("GROUP BY channel", sql)
        self.assertIn("ORDER BY total DESC", sql)
        self.assertNotIn("合计", sql)


class AnnualTargetSqlShapeTests(SqlShapeTestCase):
    """The target denominator is the two-line scope, current year only."""

    def test_annual_target_total_sql_shape(self):
        connection = FakeConnection(scalars={"dim_target": Decimal("1")})

        annual_target_total(connection)

        sql = self.sole_static_sql(connection)
        self.assertIn("FROM dim_target", sql)
        self.assertIn("scope = 'line'", sql)
        self.assertIn("scope_key IN ('offline', 'channel')", sql)
        self.assertIn("year = YEAR(CURDATE())", sql)


class StaticSqlTests(unittest.TestCase):
    """SQL 全静态: no query ever binds or splices a value."""

    _QUERY_FUNCTIONS = (
        offline_mtd_total,
        offline_annual_total,
        channel_mtd_total,
        channel_annual_total,
        annual_target_total,
        region_daily_series,
        channel_mtd_ranking,
    )

    def test_every_query_executes_static_sql_without_parameters(self):
        for function in self._QUERY_FUNCTIONS:
            with self.subTest(query=function.__name__):
                connection = FakeConnection(
                    scalars={
                        "fact_daily_report_offline": Decimal("0"),
                        "fact_channel_daily_sales": Decimal("0"),
                        "dim_target": Decimal("0"),
                    },
                    rowsets={
                        "fact_daily_report_offline": [],
                        "fact_channel_daily_sales": [],
                    },
                )

                function(connection)

                self.assertTrue(connection.executed)
                for sql, parameters in connection.executed:
                    self.assertIsInstance(sql, str)
                    # No bound parameters and no %s placeholder: nothing
                    # user-controlled can ever enter the SQL text.
                    self.assertIsNone(parameters)
                    self.assertNotIn("%s", sql)


class ScalarTotalTests(unittest.TestCase):
    """The total queries return the scripted Decimal."""

    def test_scalar_totals_return_decimal(self):
        cases = (
            ("fact_daily_report_offline", (offline_mtd_total, offline_annual_total)),
            ("fact_channel_daily_sales", (channel_mtd_total, channel_annual_total)),
            ("dim_target", (annual_target_total,)),
        )
        for table, functions in cases:
            for function in functions:
                with self.subTest(query=function.__name__):
                    connection = FakeConnection(scalars={table: Decimal("42.5")})

                    self.assertEqual(Decimal("42.5"), function(connection))


class RegionDailySeriesTests(unittest.TestCase):
    """Date alignment, ascending order, MM-DD labels, zero-fill."""

    def test_aligns_dates_ascending_and_zero_fills_missing_points(self):
        rows = [
            {"business_date": date(2026, 9, 2), "region": "杭州", "total": Decimal("20")},
            {"business_date": date(2026, 9, 1), "region": "杭州", "total": Decimal("10")},
            {"business_date": date(2026, 9, 2), "region": "绍兴", "total": Decimal("5")},
        ]
        connection = FakeConnection(rowsets={"fact_daily_report_offline": rows})

        result = region_daily_series(connection)

        self.assertEqual(
            {
                "dates": ["09-01", "09-02"],
                "series": [
                    {"name": "杭州", "data": [Decimal("10"), Decimal("20")]},
                    {"name": "绍兴", "data": [Decimal("0"), Decimal("5")]},
                ],
            },
            result,
        )

    def test_empty_fact_rows_yield_empty_axis_and_series(self):
        connection = FakeConnection(rowsets={"fact_daily_report_offline": []})

        self.assertEqual({"dates": [], "series": []}, region_daily_series(connection))


class ChannelMtdRankingTests(unittest.TestCase):
    """The ranking is chart-ready: categories and descending values."""

    def test_ranking_returns_categories_and_totals(self):
        rows = [
            {"channel": "天猫", "total": Decimal("300")},
            {"channel": "京东", "total": Decimal("100")},
        ]
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": rows})

        result = channel_mtd_ranking(connection)

        self.assertEqual(
            {"categories": ["天猫", "京东"], "values": [Decimal("300"), Decimal("100")]},
            result,
        )


class DimensionOptionsTests(unittest.TestCase):
    """筛选器维表查询（spec §4）：regions / channels / months。"""

    def test_region_options_sql_shape_and_values(self):
        connection = FakeConnection(
            rowsets={"fact_daily_report_offline": [
                {"region": "杭州"}, {"region": "绍兴"},
            ]}
        )

        options = region_options(connection)

        self.assertEqual(["杭州", "绍兴"], options)
        sql, parameters = connection.executed[0]
        self.assertIn("SELECT DISTINCT region FROM fact_daily_report_offline", sql)
        self.assertIn("ORDER BY region", sql)
        self.assertIsNone(parameters)

    def test_channel_options_sql_shape_and_values(self):
        connection = FakeConnection(
            rowsets={"fact_channel_daily_sales": [{"channel": "天猫"}]}
        )

        options = channel_options(connection)

        self.assertEqual(["天猫"], options)
        sql, parameters = connection.executed[0]
        self.assertIn("SELECT DISTINCT channel FROM fact_channel_daily_sales", sql)
        self.assertIn("ORDER BY channel", sql)
        self.assertIsNone(parameters)

    def test_month_options_union_both_fact_tables_plus_current_month_desc(self):
        connection = FakeConnection(
            rowsets={"months": [{"month": "2026-09"}, {"month": "2026-08"}]}
        )

        options = month_options(connection)

        self.assertEqual(["2026-09", "2026-08"], options)
        sql, parameters = connection.executed[0]
        self.assertIn("FROM fact_daily_report_offline", sql)
        self.assertIn("FROM fact_channel_daily_sales", sql)
        self.assertIn("CURDATE()", sql)
        self.assertIn("ORDER BY 1 DESC", sql)
        self.assertIsNone(parameters)

    def test_month_options_drop_null_business_date_months(self):
        connection = FakeConnection(
            rowsets={
                "months": [
                    {"month": "2026-09"},
                    {"month": None},
                    {"month": "2026-08"},
                ]
            }
        )

        options = month_options(connection)

        self.assertEqual(["2026-09", "2026-08"], options)


class RunPayloadTests(unittest.TestCase):
    """The run_* wrappers produce the plan's chart-ready payload shapes."""

    def test_run_kpi_offline_mtd_payload(self):
        connection = FakeConnection(
            scalars={"fact_daily_report_offline": Decimal("10.5")}
        )

        payload = run_kpi_offline_mtd(connection, {})

        self.assertEqual({"chart": "scalar", "value": 10.5, "unit": "元"}, payload)
        self.assertIsInstance(payload["value"], float)

    def test_run_kpi_channel_mtd_payload(self):
        connection = FakeConnection(
            scalars={"fact_channel_daily_sales": Decimal("7")}
        )

        payload = run_kpi_channel_mtd(connection, {})

        self.assertEqual({"chart": "scalar", "value": 7.0, "unit": "元"}, payload)
        self.assertIsInstance(payload["value"], float)

    def test_run_kpi_annual_progress_combines_both_lines(self):
        connection = FakeConnection(
            scalars={
                "fact_daily_report_offline": Decimal("1000"),
                "fact_channel_daily_sales": Decimal("234"),
                "dim_target": Decimal("760210000"),
            }
        )

        payload = run_kpi_annual_progress(connection, {})

        self.assertEqual(
            {
                "chart": "scalar",
                "value": 1234.0,
                "target": 760210000.0,
                "rate": 1234.0 / 760210000.0,
                "unit": "元",
            },
            payload,
        )
        self.assertIsInstance(payload["value"], float)
        self.assertIsInstance(payload["target"], float)
        self.assertIsInstance(payload["rate"], float)

    def test_run_kpi_annual_progress_rate_is_none_when_target_is_zero(self):
        connection = FakeConnection(
            scalars={
                "fact_daily_report_offline": Decimal("1000"),
                "fact_channel_daily_sales": Decimal("0"),
                "dim_target": Decimal("0"),
            }
        )

        payload = run_kpi_annual_progress(connection, {})

        self.assertIsNone(payload["rate"])
        self.assertEqual(0.0, payload["target"])
        self.assertEqual(1000.0, payload["value"])

    def test_run_trend_region_daily_payload(self):
        rows = [
            {"business_date": date(2026, 9, 1), "region": "杭州", "total": Decimal("10")},
            {"business_date": date(2026, 9, 2), "region": "杭州", "total": Decimal("20")},
            {"business_date": date(2026, 9, 2), "region": "绍兴", "total": Decimal("5")},
        ]
        connection = FakeConnection(rowsets={"fact_daily_report_offline": rows})

        payload = run_trend_region_daily(connection, {})

        self.assertEqual(
            {
                "chart": "line",
                "dates": ["09-01", "09-02"],
                "series": [
                    {"name": "杭州", "data": [10.0, 20.0]},
                    {"name": "绍兴", "data": [0.0, 5.0]},
                ],
            },
            payload,
        )

    def test_run_bar_channel_mtd_payload(self):
        rows = [
            {"channel": "天猫", "total": Decimal("300")},
            {"channel": "京东", "total": Decimal("100")},
        ]
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": rows})

        payload = run_bar_channel_mtd(connection, {})

        self.assertEqual(
            {
                "chart": "bar",
                "categories": ["天猫", "京东"],
                "values": [300.0, 100.0],
                "unit": "元",
            },
            payload,
        )


class ParameterizedSqlShapeTests(unittest.TestCase):
    """阶段 B 参数化查询共用断言：恰好一条语句 + 参数元组逐位相等。

    另做两条通用 ``%`` 审计（FakeCursor 不做 pymysql 的 ``sql % params``
    格式化，漏写 ``%%`` 单测本不报错）：残留单 ``%`` 必须为 0；
    ``%s`` 占位符数量必须与参数元组长度一致。
    """

    def sole_parameterized_sql(self, connection, expected_params):
        self.assertEqual(1, len(connection.executed))
        sql, parameters = connection.executed[0]
        self.assertEqual(tuple(expected_params), parameters)
        self.assertEqual(0, sql.replace("%%", "").replace("%s", "").count("%"))
        self.assertEqual(sql.count("%s"), len(parameters))
        return sql


class RegionParamTests(ParameterizedSqlShapeTests):
    """l2-region 取数：region 过滤 / 月区间 / 合计排除（%% 双写）。"""

    def test_region_mtd_total_with_region(self):
        connection = FakeConnection(scalars={"fact_daily_report_offline": Decimal("1")})

        region_mtd_total(
            connection,
            region="杭州",
            first_day=date(2026, 9, 1),
            last_day=date(2026, 9, 30),
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30), "杭州")
        )
        self.assertIn("FROM fact_daily_report_offline", sql)
        self.assertIn("business_date BETWEEN %s AND %s", sql)
        self.assertIn("region = %s", sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, sql)

    def test_region_mtd_total_without_region_omits_region_filter(self):
        connection = FakeConnection(scalars={"fact_daily_report_offline": Decimal("1")})

        region_mtd_total(
            connection, first_day=date(2026, 9, 1), last_day=date(2026, 9, 30)
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30))
        )
        self.assertNotIn("region = %s", sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, sql)

    def test_region_mtd_total_returns_zero_when_no_rows(self):
        connection = FakeConnection()

        value = region_mtd_total(
            connection, first_day=date(2026, 9, 1), last_day=date(2026, 9, 30)
        )

        self.assertEqual(Decimal(0), value)

    def test_region_month_target_sums_per_person_max(self):
        connection = FakeConnection(scalars={"fact_daily_report_offline": Decimal("1")})

        region_month_target(
            connection,
            region="杭州",
            first_day=date(2026, 9, 1),
            last_day=date(2026, 9, 30),
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30), "杭州")
        )
        self.assertIn("SELECT COALESCE(SUM(mx), 0)", sql)
        self.assertIn("MAX(monthly_target)", sql)
        self.assertIn("GROUP BY responsible_person", sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, sql)
        # 月目标是月级属性：与 summarize_people 同口径取全月行 MAX，
        # 刻意不做 CURDATE 截断（预填未来行不影响 MAX）。
        self.assertNotIn(_TRUNCATION, sql)

    def test_department_mtd_ranking_sql_shape_and_null_department(self):
        connection = FakeConnection(
            rowsets={"fact_daily_report_offline": [
                {"department": "零售一组", "total": Decimal("30")},
                {"department": None, "total": Decimal("10")},
            ]}
        )

        ranking = department_mtd_ranking(
            connection,
            region="杭州",
            first_day=date(2026, 9, 1),
            last_day=date(2026, 9, 30),
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30), "杭州")
        )
        self.assertIn("GROUP BY department", sql)
        self.assertIn("ORDER BY total DESC", sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, sql)
        self.assertEqual(["零售一组", "未分组"], ranking["categories"])
        self.assertEqual([Decimal("30"), Decimal("10")], ranking["values"])

    def test_region_month_daily_series_sql_shape(self):
        connection = FakeConnection(
            rowsets={"fact_daily_report_offline": [
                {"business_date": date(2026, 9, 2), "region": "杭州", "total": Decimal("5")},
            ]}
        )

        region_month_daily_series(
            connection,
            region="杭州",
            first_day=date(2026, 9, 1),
            last_day=date(2026, 9, 30),
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30), "杭州")
        )
        self.assertIn("FROM fact_daily_report_offline", sql)
        self.assertIn("business_date BETWEEN %s AND %s", sql)
        self.assertIn("region = %s", sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, sql)
        self.assertIn("GROUP BY business_date, region", sql)


class MonthBoundsTests(unittest.TestCase):
    def test_month_bounds_expands_to_month_ends(self):
        self.assertEqual(
            (date(2026, 9, 1), date(2026, 9, 30)), month_bounds("2026-09")
        )

    def test_month_bounds_handles_february_leap_year(self):
        self.assertEqual(
            (date(2024, 2, 1), date(2024, 2, 29)), month_bounds("2024-02")
        )


class _RegionFakeConnection(FakeConnection):
    """kpi_region_mtd 两条查询同表不同聚合：按 SQL 内容区分 value/target。"""

    def scripted_fetchone(self, sql):
        if "MAX(monthly_target)" in sql:
            value = self._scalars.get("region_target")
            return {"total": Decimal("0") if value is None else value}
        return super().scripted_fetchone(sql)


class RegionRunPayloadTests(unittest.TestCase):
    def test_run_kpi_region_mtd_payload(self):
        connection = _RegionFakeConnection(
            scalars={
                "fact_daily_report_offline": Decimal("12"),
                "region_target": Decimal("40"),
            }
        )

        payload = run_kpi_region_mtd(connection, {"region": "杭州", "month": "2026-09"})

        self.assertEqual(
            {
                "chart": "scalar",
                "value": 12.0,
                "target": 40.0,
                "rate": 12.0 / 40.0,
                "unit": "元",
            },
            payload,
        )
        sql, parameters = connection.executed[0]
        self.assertIn("SUM(sales_amount)", sql)
        self.assertIn("region = %s", sql)
        self.assertEqual((date(2026, 9, 1), date(2026, 9, 30), "杭州"), parameters)

    def test_run_kpi_region_mtd_defaults_month_to_current(self):
        connection = _RegionFakeConnection(
            scalars={
                "fact_daily_report_offline": Decimal("12"),
                "region_target": Decimal("0"),
            }
        )

        payload = run_kpi_region_mtd(connection, {"region": "杭州"})

        self.assertIsNone(payload["rate"])
        sql, parameters = connection.executed[0]
        first_day, last_day, region = parameters
        self.assertEqual("杭州", region)
        self.assertEqual(date.today().replace(day=1), first_day)
        self.assertEqual(first_day, last_day.replace(day=1))

    def test_run_trend_region_daily_with_region_single_series(self):
        rows = [
            {"business_date": date(2026, 9, 2), "region": "杭州", "total": Decimal("5")},
            {"business_date": date(2026, 9, 3), "region": "杭州", "total": Decimal("6")},
        ]
        connection = FakeConnection(rowsets={"fact_daily_report_offline": rows})

        payload = run_trend_region_daily(
            connection, {"region": "杭州", "month": "2026-09"}
        )

        self.assertEqual(
            {
                "chart": "line",
                "dates": ["09-02", "09-03"],
                "series": [{"name": "杭州", "data": [5.0, 6.0]}],
            },
            payload,
        )
        sql, parameters = connection.executed[0]
        self.assertIn("region = %s", sql)
        self.assertIn("GROUP BY business_date, region", sql)
        self.assertEqual((date(2026, 9, 1), date(2026, 9, 30), "杭州"), parameters)

    def test_run_trend_region_daily_month_without_region_two_series(self):
        rows = [
            {"business_date": date(2026, 8, 2), "region": "杭州", "total": Decimal("5")},
            {"business_date": date(2026, 8, 2), "region": "绍兴", "total": Decimal("3")},
        ]
        connection = FakeConnection(rowsets={"fact_daily_report_offline": rows})

        payload = run_trend_region_daily(connection, {"month": "2026-08"})

        self.assertEqual(["08-02"], payload["dates"])
        self.assertEqual(
            [
                {"name": "杭州", "data": [5.0]},
                {"name": "绍兴", "data": [3.0]},
            ],
            payload["series"],
        )
        sql, parameters = connection.executed[0]
        self.assertNotIn("region = %s", sql)
        self.assertEqual((date(2026, 8, 1), date(2026, 8, 31)), parameters)

    def test_run_trend_region_daily_without_params_keeps_static_l1_path(self):
        connection = FakeConnection(rowsets={"fact_daily_report_offline": []})

        run_trend_region_daily(connection, {})

        self.assertEqual(1, len(connection.executed))
        sql, parameters = connection.executed[0]
        self.assertIsNone(parameters)
        self.assertIn(_MONTH_START, sql)

    def test_run_bar_department_mtd_payload(self):
        rows = [{"department": "零售一组", "total": Decimal("30")}]
        connection = FakeConnection(rowsets={"fact_daily_report_offline": rows})

        payload = run_bar_department_mtd(
            connection, {"region": "杭州", "month": "2026-08"}
        )

        self.assertEqual(
            {
                "chart": "bar",
                "categories": ["零售一组"],
                "values": [30.0],
                "unit": "元",
            },
            payload,
        )
        sql, parameters = connection.executed[0]
        self.assertIn("GROUP BY department", sql)
        self.assertEqual((date(2026, 8, 1), date(2026, 8, 31), "杭州"), parameters)


class _DodFakeConnection(FakeConnection):
    """DoD 卡两条 SQL 读同一张表：按 SQL 内容分流。

    MAX(business_date) 定锚查询走 scalars（None=空表，返回 None 行），
    窗口 GROUP BY 查询照常走 rowsets。
    """

    def scripted_fetchone(self, sql):
        if "MAX(business_date)" in sql:
            value = self._scalars.get(self._table_of(sql))
            if value is None:
                return None
            return {"d": value}
        return super().scripted_fetchone(sql)


class DodShapeTests(unittest.TestCase):
    """定锚 SQL 全静态（单 %），窗口 SQL 参数化（双 %）。"""

    def dod_statements(self, connection, first_day, latest):
        self.assertEqual(2, len(connection.executed))
        latest_sql, latest_params = connection.executed[0]
        window_sql, window_params = connection.executed[1]
        self.assertIsNone(latest_params)
        self.assertEqual((first_day, latest), window_params)
        return latest_sql, window_sql

    def test_offline_dod_sql_shape(self):
        connection = _DodFakeConnection(
            scalars={"fact_daily_report_offline": date(2026, 9, 10)}
        )

        offline_dod(connection)

        latest_sql, window_sql = self.dod_statements(
            connection, date(2026, 9, 4), date(2026, 9, 10)
        )
        self.assertIn("MAX(business_date)", latest_sql)
        self.assertIn("FROM fact_daily_report_offline", latest_sql)
        self.assertIn(_TRUNCATION, latest_sql)
        self.assertIn(_SUMMARY_EXCLUSION, latest_sql)
        self.assertIn("FROM fact_daily_report_offline", window_sql)
        self.assertIn("BETWEEN %s AND %s", window_sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, window_sql)
        self.assertIn("GROUP BY business_date", window_sql)

    def test_channel_dod_sql_shape(self):
        connection = _DodFakeConnection(
            scalars={"fact_channel_daily_sales": date(2026, 9, 10)}
        )

        channel_dod(connection)

        latest_sql, window_sql = self.dod_statements(
            connection, date(2026, 9, 4), date(2026, 9, 10)
        )
        self.assertIn("MAX(business_date)", latest_sql)
        self.assertIn("FROM fact_channel_daily_sales", latest_sql)
        self.assertIn(_TRUNCATION, latest_sql)
        self.assertNotIn("合计", latest_sql)
        self.assertIn("FROM fact_channel_daily_sales", window_sql)
        self.assertIn("BETWEEN %s AND %s", window_sql)
        self.assertNotIn("合计", window_sql)
        self.assertIn("GROUP BY business_date", window_sql)


class OfflineDodTests(unittest.TestCase):
    """⑭ 线下：全载荷、prev 缺失、prev 为 0、空表。"""

    def connection_with(self, rows, latest=date(2026, 9, 10)):
        return _DodFakeConnection(
            scalars={"fact_daily_report_offline": latest},
            rowsets={"fact_daily_report_offline": rows},
        )

    def test_full_payload_with_delta_and_trend7(self):
        rows = [
            {"business_date": date(2026, 9, 8), "total": Decimal("80")},
            {"business_date": date(2026, 9, 9), "total": Decimal("100")},
            {"business_date": date(2026, 9, 10), "total": Decimal("120")},
        ]

        payload = run_kpi_offline_dod(self.connection_with(rows), {})

        self.assertEqual(
            {
                "chart": "scalar",
                "value": 120.0,
                "date": "2026-09-10",
                "prev": 100.0,
                "delta_pct": 0.2,
                "trend7": [
                    {"date": "2026-09-04", "value": None},
                    {"date": "2026-09-05", "value": None},
                    {"date": "2026-09-06", "value": None},
                    {"date": "2026-09-07", "value": None},
                    {"date": "2026-09-08", "value": 80.0},
                    {"date": "2026-09-09", "value": 100.0},
                    {"date": "2026-09-10", "value": 120.0},
                ],
                "unit": "元",
            },
            payload,
        )

    def test_prev_missing_yields_delta_none(self):
        rows = [{"business_date": date(2026, 9, 10), "total": Decimal("120")}]

        payload = run_kpi_offline_dod(self.connection_with(rows), {})

        self.assertEqual(120.0, payload["value"])
        self.assertIsNone(payload["prev"])
        self.assertIsNone(payload["delta_pct"])

    def test_prev_zero_yields_delta_none(self):
        rows = [
            {"business_date": date(2026, 9, 9), "total": Decimal("0")},
            {"business_date": date(2026, 9, 10), "total": Decimal("120")},
        ]

        payload = run_kpi_offline_dod(self.connection_with(rows), {})

        self.assertEqual(0.0, payload["prev"])
        self.assertIsNone(payload["delta_pct"])

    def test_empty_table_yields_empty_payload(self):
        connection = _DodFakeConnection(
            scalars={"fact_daily_report_offline": None}
        )

        payload = run_kpi_offline_dod(connection, {})

        self.assertEqual(
            {
                "chart": "scalar",
                "value": 0.0,
                "date": None,
                "prev": None,
                "delta_pct": None,
                "trend7": [],
                "unit": "元",
            },
            payload,
        )


class ChannelDodTests(unittest.TestCase):
    """⑭ 电商：同口径载荷 + 空表（渠道表无合计行）。"""

    def test_full_payload(self):
        connection = _DodFakeConnection(
            scalars={"fact_channel_daily_sales": date(2026, 9, 10)},
            rowsets={
                "fact_channel_daily_sales": [
                    {"business_date": date(2026, 9, 9), "total": Decimal("40")},
                    {"business_date": date(2026, 9, 10), "total": Decimal("60")},
                ]
            },
        )

        payload = run_kpi_channel_dod(connection, {})

        self.assertEqual(60.0, payload["value"])
        self.assertEqual("2026-09-10", payload["date"])
        self.assertEqual(40.0, payload["prev"])
        self.assertEqual(0.5, payload["delta_pct"])
        self.assertEqual(7, len(payload["trend7"]))
        self.assertEqual({"date": "2026-09-10", "value": 60.0}, payload["trend7"][-1])

    def test_empty_table_yields_empty_payload(self):
        connection = _DodFakeConnection(
            scalars={"fact_channel_daily_sales": None}
        )

        payload = run_kpi_channel_dod(connection, {})

        self.assertEqual(0.0, payload["value"])
        self.assertIsNone(payload["date"])
        self.assertEqual([], payload["trend7"])


class ChannelMonthParamTests(ParameterizedSqlShapeTests):
    """l2-channel 取数：月区间 / channel 过滤（渠道表无合计行）。"""

    def test_channel_month_ranking_sql_shape(self):
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": []})

        channel_month_ranking(
            connection, first_day=date(2026, 9, 1), last_day=date(2026, 9, 30)
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30))
        )
        self.assertIn("FROM fact_channel_daily_sales", sql)
        self.assertIn("business_date BETWEEN %s AND %s", sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertIn("GROUP BY channel", sql)
        self.assertIn("ORDER BY total DESC", sql)
        self.assertNotIn("合计", sql)

    def test_channel_month_daily_series_sql_shape(self):
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": []})

        channel_month_daily_series(
            connection, first_day=date(2026, 9, 1), last_day=date(2026, 9, 30)
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30))
        )
        self.assertIn("GROUP BY business_date, channel", sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertNotIn("合计", sql)

    def test_channel_mtd_comparison_sql_shape(self):
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": []})

        channel_mtd_comparison(
            connection, first_day=date(2026, 9, 1), last_day=date(2026, 9, 30)
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30))
        )
        self.assertIn("SUM(sales_amount)", sql)
        self.assertIn("SUM(promotion_cost)", sql)
        self.assertIn("COUNT(DISTINCT store_name)", sql)
        self.assertIn("GROUP BY channel", sql)
        self.assertIn("ORDER BY sales DESC", sql)
        self.assertIn(_TRUNCATION, sql)

    def test_store_mtd_ranking_without_channel_sql_shape(self):
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": []})

        store_mtd_ranking(
            connection, first_day=date(2026, 9, 1), last_day=date(2026, 9, 30)
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30))
        )
        self.assertIn(_TRUNCATION, sql)
        self.assertIn("GROUP BY store_name, channel", sql)
        self.assertIn("ORDER BY total DESC", sql)
        self.assertNotIn("channel = %s", sql)

    def test_store_mtd_ranking_with_channel_sql_shape(self):
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": []})

        store_mtd_ranking(
            connection,
            channel="天猫",
            first_day=date(2026, 9, 1),
            last_day=date(2026, 9, 30),
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30), "天猫")
        )
        self.assertIn(_TRUNCATION, sql)
        self.assertIn("channel = %s", sql)
        self.assertIn("GROUP BY store_name, channel", sql)


class ChannelRunPayloadTests(unittest.TestCase):
    """l2-channel 四卡载荷：trend / bar(month) / 渠道对比表 / 店铺排行表。"""

    def test_run_trend_channel_daily_payload(self):
        rows = [
            {"business_date": date(2026, 9, 2), "channel": "天猫", "total": Decimal("5")},
            {"business_date": date(2026, 9, 2), "channel": "京东", "total": Decimal("3")},
            {"business_date": date(2026, 9, 3), "channel": "天猫", "total": Decimal("7")},
        ]
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": rows})

        payload = run_trend_channel_daily(connection, {"month": "2026-09"})

        self.assertEqual(
            {
                "chart": "line",
                "dates": ["09-02", "09-03"],
                "series": [
                    {"name": "京东", "data": [3.0, 0.0]},
                    {"name": "天猫", "data": [5.0, 7.0]},
                ],
            },
            payload,
        )
        sql, parameters = connection.executed[0]
        self.assertEqual((date(2026, 9, 1), date(2026, 9, 30)), parameters)

    def test_run_bar_channel_mtd_with_month_uses_parameterized_path(self):
        rows = [{"channel": "天猫", "total": Decimal("300")}]
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": rows})

        payload = run_bar_channel_mtd(connection, {"month": "2026-08"})

        self.assertEqual(
            {"chart": "bar", "categories": ["天猫"], "values": [300.0], "unit": "元"},
            payload,
        )
        sql, parameters = connection.executed[0]
        self.assertEqual((date(2026, 8, 1), date(2026, 8, 31)), parameters)

    def test_run_bar_channel_mtd_without_month_keeps_static_path(self):
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": []})

        run_bar_channel_mtd(connection, {})

        sql, parameters = connection.executed[0]
        self.assertIsNone(parameters)
        self.assertIn(_MONTH_START, sql)

    def test_run_table_channel_mtd_payload_with_roi(self):
        rows = [
            {"channel": "天猫", "sales": Decimal("300"), "promo": Decimal("60"), "stores": 3},
            {"channel": "直播", "sales": Decimal("48"), "promo": None, "stores": 1},
            {"channel": "拼多多", "sales": Decimal("90"), "promo": Decimal("0"), "stores": 2},
        ]
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": rows})

        payload = run_table_channel_mtd(connection, {"month": "2026-09"})

        self.assertEqual(
            {
                "chart": "table",
                "columns": [
                    {"key": "channel", "title": "渠道"},
                    {"key": "sales", "title": "本月销售额", "format": "wan"},
                    {"key": "promo", "title": "推广费", "format": "wan"},
                    {"key": "roi", "title": "ROI", "format": "ratio"},
                    {"key": "stores", "title": "店铺数"},
                ],
                "rows": [
                    {"channel": "天猫", "sales": 300.0, "promo": 60.0, "roi": 5.0, "stores": 3},
                    {"channel": "直播", "sales": 48.0, "promo": None, "roi": None, "stores": 1},
                    {"channel": "拼多多", "sales": 90.0, "promo": 0.0, "roi": None, "stores": 2},
                ],
            },
            payload,
        )

    def test_run_table_store_mtd_all_stores_includes_channel_column(self):
        rows = [
            {"store_name": "天猫官方旗舰店", "channel": "天猫", "total": Decimal("300")},
            {"store_name": "京东自营", "channel": "京东", "total": Decimal("100")},
        ]
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": rows})

        payload = run_table_store_mtd(connection, {"month": "2026-09"})

        self.assertEqual(
            {
                "chart": "table",
                "columns": [
                    {"key": "rank", "title": "排名"},
                    {"key": "store", "title": "店铺"},
                    {"key": "channel", "title": "渠道"},
                    {"key": "sales", "title": "本月销售额", "format": "wan"},
                ],
                "rows": [
                    {"rank": 1, "store": "天猫官方旗舰店", "channel": "天猫", "sales": 300.0},
                    {"rank": 2, "store": "京东自营", "channel": "京东", "sales": 100.0},
                ],
            },
            payload,
        )
        sql, parameters = connection.executed[0]
        self.assertEqual((date(2026, 9, 1), date(2026, 9, 30)), parameters)

    def test_run_table_store_mtd_single_channel_omits_channel_column(self):
        rows = [{"store_name": "天猫官方旗舰店", "channel": "天猫", "total": Decimal("300")}]
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": rows})

        payload = run_table_store_mtd(connection, {"channel": "天猫", "month": "2026-09"})

        self.assertEqual(
            {
                "chart": "table",
                "columns": [
                    {"key": "rank", "title": "排名"},
                    {"key": "store", "title": "店铺"},
                    {"key": "sales", "title": "本月销售额", "format": "wan"},
                ],
                "rows": [{"rank": 1, "store": "天猫官方旗舰店", "sales": 300.0}],
            },
            payload,
        )
        sql, parameters = connection.executed[0]
        self.assertIn("channel = %s", sql)
        self.assertEqual((date(2026, 9, 1), date(2026, 9, 30), "天猫"), parameters)


def _people_calendar_rows():
    """2026-08 工作日三日（供 mart 路径的 dim_calendar 查询）。"""
    return [
        {"business_date": date(2026, 8, 3)},
        {"business_date": date(2026, 8, 4)},
        {"business_date": date(2026, 8, 5)},
    ]


def _hangzhou_people_facts():
    """杭州三人形：张三（部分填写）、李四（无部门无目标）、合计行（须被排除）。"""
    return [
        {
            "region": "杭州",
            "responsible_person": "张三",
            "department": "零售一组",
            "business_date": date(2026, 8, 3),
            "sales_amount": Decimal("30"),
            "monthly_target": Decimal("100"),
        },
        {
            "region": "杭州",
            "responsible_person": "张三",
            "department": "零售一组",
            "business_date": date(2026, 8, 4),
            "sales_amount": Decimal("40"),
            "monthly_target": Decimal("100"),
        },
        {
            "region": "杭州",
            "responsible_person": "李四",
            "department": None,
            "business_date": date(2026, 8, 3),
            "sales_amount": Decimal("10"),
            "monthly_target": None,
        },
        {
            "region": "杭州",
            "responsible_person": "合计",
            "department": None,
            "business_date": date(2026, 8, 3),
            "sales_amount": Decimal("999"),
            "monthly_target": None,
        },
    ]


def _shaoxing_people_facts():
    """绍兴一人形：王五（rate 0.9，用于合并重排断言）。"""
    return [
        {
            "region": "绍兴",
            "responsible_person": "王五",
            "department": "绍兴一组",
            "business_date": date(2026, 8, 3),
            "sales_amount": Decimal("90"),
            "monthly_target": Decimal("100"),
        },
    ]


class _PeopleFakeConnection(FakeConnection):
    """mart 路径 fake：事实行按最近一条语句的 region 参数过滤
    （模拟 fetch_month_facts 的 ``WHERE region = %s``）；日历行走 rowsets。"""

    def scripted_fetchall(self, sql):
        if "fact_daily_report_offline" in sql:
            region = self.executed[-1][1][0]
            rows = [
                row
                for row in self._rowsets.get("fact_daily_report_offline", [])
                if row.get("region") == region
            ]
            return [dict(row) for row in rows]
        return super().scripted_fetchall(sql)


class _GatedPeopleConnection(_PeopleFakeConnection):
    """采集 fetch 阻塞在门闩上：制造「采集进行中」的确定性窗口。

    压测 _people_snapshot 并发路径用——主线程等到 fetch_started 后再
    发同键请求，可精确复现两请求同时 miss 缓存的时序。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fetch_started = threading.Event()
        self._fetch_gate = threading.Event()

    def scripted_fetchall(self, sql):
        self.fetch_started.set()
        self._fetch_gate.wait(timeout=10)
        return super().scripted_fetchall(sql)

    def release_fetch(self):
        self._fetch_gate.set()


class PeopleRunTests(unittest.TestCase):
    """l2-people 四卡：mart_collect 复用、快照缓存、日历缺行降级、两区合并。"""

    def setUp(self):
        bi_web_queries._PEOPLE_CACHE.clear()

    def hangzhou_connection(self):
        return _PeopleFakeConnection(
            rowsets={
                "dim_calendar": _people_calendar_rows(),
                "fact_daily_report_offline": _hangzhou_people_facts(),
            }
        )

    def test_kpi_people_count_excludes_summary_rows(self):
        payload = run_kpi_people_count(
            self.hangzhou_connection(), {"region": "杭州", "month": "2026-08"}
        )

        self.assertEqual({"chart": "scalar", "value": 2.0, "unit": "人"}, payload)

    def test_kpi_people_completed_sums_elapsed_workdays(self):
        payload = run_kpi_people_completed(
            self.hangzhou_connection(), {"region": "杭州", "month": "2026-08"}
        )

        self.assertEqual({"chart": "scalar", "value": 80.0, "unit": "元"}, payload)

    def test_kpi_people_rate_is_sum_ratio_not_personal_average(self):
        payload = run_kpi_people_rate(
            self.hangzhou_connection(), {"region": "杭州", "month": "2026-08"}
        )

        # Σcompleted÷Σtarget=80/100=0.8（个人率平均会得到 0.35）
        self.assertEqual(
            {
                "chart": "scalar",
                "value": 80.0,
                "target": 100.0,
                "rate": 0.8,
                "unit": "元",
            },
            payload,
        )

    def test_run_table_people_leaderboard_payload(self):
        payload = run_table_people_leaderboard(
            self.hangzhou_connection(), {"region": "杭州", "month": "2026-08"}
        )

        self.assertEqual(
            {
                "chart": "table",
                "columns": [
                    {"key": "rank", "title": "排名"},
                    {"key": "name", "title": "姓名"},
                    {"key": "dept", "title": "部门"},
                    {"key": "completed", "title": "完成额", "format": "wan"},
                    {"key": "target", "title": "月目标", "format": "wan"},
                    {"key": "rate", "title": "达成率", "format": "percent"},
                    {"key": "unfilled", "title": "未完成缺口"},
                ],
                "rows": [
                    {
                        "rank": 1,
                        "name": "张三",
                        "dept": "零售一组",
                        "completed": 70.0,
                        "target": 100.0,
                        "rate": 0.7,
                        "unfilled": 1,
                    },
                    {
                        "rank": 2,
                        "name": "李四",
                        "dept": "未分组",
                        "completed": 10.0,
                        "target": 0.0,
                        "rate": None,
                        "unfilled": 2,
                    },
                ],
            },
            payload,
        )

    def test_people_cards_share_one_snapshot(self):
        connection = self.hangzhou_connection()
        params = {"region": "杭州", "month": "2026-08"}

        run_kpi_people_count(connection, params)
        run_kpi_people_completed(connection, params)
        run_kpi_people_rate(connection, params)
        run_table_people_leaderboard(connection, params)

        # 四张卡共用一轮采集：dim_calendar + 事实表各一条
        self.assertEqual(2, len(connection.executed))

    def test_missing_calendar_degrades_to_empty_result(self):
        connection = FakeConnection(rowsets={"fact_daily_report_offline": []})
        params = {"region": "杭州", "month": "2026-08"}

        count_payload = run_kpi_people_count(connection, params)
        table_payload = run_table_people_leaderboard(connection, params)

        self.assertEqual(0.0, count_payload["value"])
        self.assertEqual([], table_payload["rows"])
        # 日历缺行即 MartTaskError：事实查询不再发起（空结果而非报错，spec §8）
        self.assertEqual(1, len(connection.executed))

    def test_no_region_merges_both_regions_and_resorts(self):
        connection = _PeopleFakeConnection(
            rowsets={
                "dim_calendar": _people_calendar_rows(),
                "fact_daily_report_offline": (
                    _hangzhou_people_facts() + _shaoxing_people_facts()
                ),
            }
        )

        count_payload = run_kpi_people_count(connection, {"month": "2026-08"})
        table_payload = run_table_people_leaderboard(connection, {"month": "2026-08"})

        self.assertEqual(3.0, count_payload["value"])
        # 合并后重排：王五 0.9 压过张三 0.7，李四（rate None）垫底
        self.assertEqual("王五", table_payload["rows"][0]["name"])
        self.assertEqual("张三", table_payload["rows"][1]["name"])
        self.assertEqual("李四", table_payload["rows"][2]["name"])
        # 两区各一轮采集（日历 + 事实各两条），第二张卡走缓存
        self.assertEqual(4, len(connection.executed))

    def test_run_kpi_people_count_defaults_month_to_current(self):
        connection = _PeopleFakeConnection(
            rowsets={
                "dim_calendar": [{"business_date": date.today().replace(day=1)}],
                "fact_daily_report_offline": [],
            }
        )

        payload = run_kpi_people_count(connection, {"region": "杭州"})

        self.assertEqual(0.0, payload["value"])
        sql, parameters = connection.executed[0]
        self.assertIn("dim_calendar", sql)
        self.assertEqual(
            month_bounds(date.today().strftime("%Y-%m")), parameters
        )

    def test_people_snapshot_locks_concurrent_same_key_misses(self):
        """同键并发首查只允许一轮采集：后到者等锁命中缓存。

        线程 A 的连接在采集 fetch 上阻塞；线程 B 此时请求同一
        (region, month)。无锁实现 B 会自行再采集（conn_b.executed
        非空）；带锁实现 B 等 A 写入缓存后直接命中。
        """
        blocking = _GatedPeopleConnection(
            rowsets={
                "dim_calendar": _people_calendar_rows(),
                "fact_daily_report_offline": _hangzhou_people_facts(),
            }
        )
        conn_b = self.hangzhou_connection()
        results = {}

        def blocking_call():
            results["blocking"] = bi_web_queries._people_snapshot(
                blocking, "杭州", "2026-08"
            )

        def second_call():
            results["second"] = bi_web_queries._people_snapshot(
                conn_b, "杭州", "2026-08"
            )

        thread_a = threading.Thread(target=blocking_call)
        thread_a.start()
        self.assertTrue(blocking.fetch_started.wait(timeout=5))

        thread_b = threading.Thread(target=second_call)
        thread_b.start()
        time.sleep(0.25)
        # A 仍在采集时 B 到达：必须等锁命中缓存，而非自己再采集一轮
        self.assertEqual([], conn_b.executed)

        blocking.release_fetch()
        thread_a.join(timeout=5)
        thread_b.join(timeout=5)
        self.assertEqual(2, len(results["blocking"]))
        self.assertEqual(results["blocking"], results["second"])
        self.assertEqual([], conn_b.executed)


# ---------------------------------------------------------------------------
# Integration layer: the real Docker Compose MySQL, via the difference method
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_SEED_PATH = REPO_ROOT / "docker" / "integration" / "target.seed.json"

#: Fixture rows always carry this prefix so teardown can find exactly them.
_FIXTURE_PREFIX = "biweb-test:"

#: 仅作植入行溯源标记；清理按 source_record_id 前缀，不依赖此值。
_SYNC_RUN_ID = "00000000-0000-0000-0000-000000000001"

_OFFLINE_INSERT_SQL = (
    "INSERT INTO `fact_daily_report_offline` "
    "(`source_record_id`, `region`, `responsible_person`, `business_date`, "
    "`sales_amount`, `department`, `monthly_target`, `synced_at`, `sync_run_id`) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(6), %s)"
)

_CHANNEL_INSERT_SQL = (
    "INSERT INTO `fact_channel_daily_sales` "
    "(`source_record_id`, `channel`, `business_date`, `sales_amount`, "
    "`store_name`, `promotion_cost`, `synced_at`, `sync_run_id`) "
    "VALUES (%s, %s, %s, %s, %s, %s, NOW(6), %s)"
)

_OFFLINE_CLEANUP_SQL = (
    "DELETE FROM `fact_daily_report_offline` "
    "WHERE `source_record_id` LIKE 'biweb-test:%' OR `region` LIKE 'biweb%'"
)

_CHANNEL_CLEANUP_SQL = (
    "DELETE FROM `fact_channel_daily_sales` "
    "WHERE `source_record_id` LIKE 'biweb-test:%' "
    "OR `channel` = 'biweb测试渠道'"
)


@unittest.skipUnless(
    os.environ.get("INTEGRATION_TEST_RUNNER") == "1",
    "Requires the Docker Compose MySQL integration environment.",
)
class BiWebQueriesIntegrationTests(unittest.TestCase):
    """Real-MySQL 口径 checks via the difference method (差值法).

    Every assertion compares a query result before and after inserting
    fixtures (prefix ``biweb-test:``), so nothing couples to whatever data
    the shared mart already holds.  合计 rows and pre-filled future rows are
    planted on purpose: they must change nothing.
    """

    @classmethod
    def setUpClass(cls):
        cls.settings = Settings.from_environment()
        cls.dingtalk_connection = connect(cls.settings.dingtalk_database)
        cls.wdt_connection = connect(cls.settings.wdt_database)
        cls.mart_connection = connect(cls.settings.mart_database)
        apply_live_migrations(
            cls.dingtalk_connection,
            cls.wdt_connection,
            cls.mart_connection,
        )

    def setUp(self):
        bi_web_queries._PEOPLE_CACHE.clear()

    @classmethod
    def tearDownClass(cls):
        cls.mart_connection.close()
        cls.wdt_connection.close()
        cls.dingtalk_connection.close()

    def tearDown(self):
        with transaction(self.mart_connection):
            with self.mart_connection.cursor() as cursor:
                cursor.execute(_OFFLINE_CLEANUP_SQL)
                cursor.execute(_CHANNEL_CLEANUP_SQL)
        # dim_target always ends up mirroring the version-controlled seed.
        replace_dim_target(
            self.mart_connection, load_target_seed(REPO_SEED_PATH)
        )

    # -- helpers -------------------------------------------------------------

    def _server_today(self):
        """CURDATE() as the server sees it (never trust the host clock/TZ)."""
        with self.mart_connection.cursor() as cursor:
            cursor.execute("SELECT CURDATE() AS today")
            return cursor.fetchone()["today"]

    def _server_month_start(self):
        with self.mart_connection.cursor() as cursor:
            cursor.execute(
                "SELECT DATE_FORMAT(CURDATE(), '%Y-%m-01') AS month_start"
            )
            return date.fromisoformat(cursor.fetchone()["month_start"])

    def _insert_offline_rows(self, rows):
        """rows: (suffix, region, responsible_person, business_date, amount)."""
        self._insert_offline_rows_full(
            [
                (suffix, region, person, business_date, amount, None, None)
                for suffix, region, person, business_date, amount in rows
            ]
        )

    def _insert_offline_rows_full(self, rows):
        """rows: (suffix, region, responsible_person, business_date, amount,
        department, monthly_target)。"""
        with transaction(self.mart_connection):
            with self.mart_connection.cursor() as cursor:
                cursor.executemany(
                    _OFFLINE_INSERT_SQL,
                    [
                        (
                            _FIXTURE_PREFIX + suffix,
                            region,
                            responsible_person,
                            business_date,
                            sales_amount,
                            department,
                            monthly_target,
                            _SYNC_RUN_ID,
                        )
                        for (
                            suffix,
                            region,
                            responsible_person,
                            business_date,
                            sales_amount,
                            department,
                            monthly_target,
                        ) in rows
                    ],
                )

    def _insert_channel_rows(self, rows):
        """rows: (suffix, channel, business_date, amount)."""
        self._insert_channel_rows_full(
            [
                (suffix, channel, business_date, amount, None, None)
                for suffix, channel, business_date, amount in rows
            ]
        )

    def _insert_channel_rows_full(self, rows):
        """rows: (suffix, channel, business_date, amount, store_name,
        promotion_cost)。"""
        with transaction(self.mart_connection):
            with self.mart_connection.cursor() as cursor:
                cursor.executemany(
                    _CHANNEL_INSERT_SQL,
                    [
                        (
                            _FIXTURE_PREFIX + suffix,
                            channel,
                            business_date,
                            sales_amount,
                            store_name,
                            promotion_cost,
                            _SYNC_RUN_ID,
                        )
                        for (
                            suffix,
                            channel,
                            business_date,
                            sales_amount,
                            store_name,
                            promotion_cost,
                        ) in rows
                    ],
                )

    def _current_month_bounds(self):
        """服务器时钟的当前月 → (month 串, (月首, 月末))。"""
        month = self._server_month_start().strftime("%Y-%m")
        return month, month_bounds(month)

    def _first_workday_of(self, first_day, last_day):
        """真实 dim_calendar 的首个工作日；None 即该月无日历行。"""
        with self.mart_connection.cursor() as cursor:
            cursor.execute(
                "SELECT business_date FROM dim_calendar "
                "WHERE is_workday = 1 AND business_date BETWEEN %s AND %s "
                "ORDER BY business_date LIMIT 1",
                (first_day, last_day),
            )
            row = cursor.fetchone()
        return None if row is None else row["business_date"]

    def _workday_count(self, first_day, last_day):
        """真实 dim_calendar 的当月工作日数（unfilled 绝对断言的基数）。"""
        with self.mart_connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM dim_calendar "
                "WHERE is_workday = 1 AND business_date BETWEEN %s AND %s",
                (first_day, last_day),
            )
            return int(cursor.fetchone()["n"])

    # -- tests ----------------------------------------------------------------

    def test_annual_target_total_matches_repo_seed_two_line_sum(self):
        replace_dim_target(
            self.mart_connection, load_target_seed(REPO_SEED_PATH)
        )

        self.assertEqual(
            Decimal("760210000"), annual_target_total(self.mart_connection)
        )

    def test_channel_mtd_ranking_and_bar_payload(self):
        today = self._server_today()
        before = channel_mtd_ranking(self.mart_connection)
        prior = dict(zip(before["categories"], before["values"])).get(
            "biweb测试渠道", Decimal("0")
        )

        self._insert_channel_rows(
            [("bar-mtd", "biweb测试渠道", today, Decimal("123"))]
        )

        ranking = channel_mtd_ranking(self.mart_connection)
        self.assertIn("biweb测试渠道", ranking["categories"])
        self.assertEqual(
            len(ranking["categories"]), len(ranking["values"])
        )
        self.assertEqual(
            ranking["values"], sorted(ranking["values"], reverse=True)
        )
        after = dict(zip(ranking["categories"], ranking["values"]))
        self.assertEqual(
            Decimal("123"), after["biweb测试渠道"] - prior
        )

        payload = run_bar_channel_mtd(self.mart_connection, {})
        self.assertEqual("bar", payload["chart"])
        self.assertEqual("元", payload["unit"])
        self.assertIn("biweb测试渠道", payload["categories"])
        self.assertEqual(
            len(payload["categories"]), len(payload["values"])
        )
        self.assertTrue(
            all(isinstance(value, float) for value in payload["values"])
        )
        self.assertEqual(
            payload["values"], sorted(payload["values"], reverse=True)
        )

    def test_run_kpi_annual_progress_two_line_numerator_and_rate(self):
        replace_dim_target(
            self.mart_connection, load_target_seed(REPO_SEED_PATH)
        )
        today = self._server_today()
        tomorrow = today + timedelta(days=1)
        numerator_before = (
            offline_annual_total(self.mart_connection)
            + channel_annual_total(self.mart_connection)
        )

        self._insert_offline_rows(
            [
                (
                    "progress-offline-normal",
                    "biweb甲",
                    "biweb甲人员",
                    today,
                    Decimal("100"),
                ),
                (
                    "progress-offline-summary",
                    "biweb甲",
                    "biweb甲合计",
                    today,
                    Decimal("999999"),
                ),
                (
                    "progress-offline-future",
                    "biweb甲",
                    "biweb甲人员",
                    tomorrow,
                    Decimal("888888"),
                ),
            ]
        )
        self._insert_channel_rows(
            [
                ("progress-channel-normal", "biweb测试渠道", today, Decimal("50")),
                ("progress-channel-future", "biweb测试渠道", tomorrow, Decimal("777")),
            ]
        )

        numerator_after = (
            offline_annual_total(self.mart_connection)
            + channel_annual_total(self.mart_connection)
        )
        self.assertEqual(Decimal("150"), numerator_after - numerator_before)

        payload = run_kpi_annual_progress(self.mart_connection, {})

        self.assertEqual("scalar", payload["chart"])
        self.assertEqual("元", payload["unit"])
        self.assertIsInstance(payload["value"], float)
        self.assertIsInstance(payload["target"], float)
        self.assertIsInstance(payload["rate"], float)
        self.assertEqual(float(numerator_after), payload["value"])
        self.assertEqual(760210000.0, payload["target"])
        self.assertEqual(payload["value"] / payload["target"], payload["rate"])

    def test_offline_mtd_total_excludes_summary_and_future_rows(self):
        today = self._server_today()
        tomorrow = today + timedelta(days=1)
        q0 = offline_mtd_total(self.mart_connection)

        self._insert_offline_rows(
            [
                ("mtd-normal-1", "biweb甲", "biweb甲人员", today, Decimal("100")),
                ("mtd-normal-2", "biweb甲", "biweb甲人员", today, Decimal("200")),
                ("mtd-summary", "biweb甲", "biweb甲合计", today, Decimal("999999")),
                ("mtd-future", "biweb甲", "biweb甲人员", tomorrow, Decimal("888888")),
            ]
        )

        q1 = offline_mtd_total(self.mart_connection)
        self.assertEqual(Decimal("300"), q1 - q0)

    def test_region_daily_series_alignment_and_zero_fill(self):
        today = self._server_today()
        yesterday = today - timedelta(days=1)
        if yesterday < self._server_month_start():
            self.skipTest(
                "run on a month-start day: the MTD window holds one date only"
            )

        self._insert_offline_rows(
            [
                ("trend-a-d1", "biweb甲", "biweb甲人员", yesterday, Decimal("100")),
                ("trend-a-d2", "biweb甲", "biweb甲人员", today, Decimal("200")),
                ("trend-b-d2", "biweb乙", "biweb乙人员", today, Decimal("50")),
            ]
        )

        result = region_daily_series(self.mart_connection)

        dates = result["dates"]
        self.assertEqual(sorted(set(dates)), dates)
        self.assertIn(yesterday.strftime("%m-%d"), dates)
        self.assertIn(today.strftime("%m-%d"), dates)
        series_by_name = {entry["name"]: entry["data"] for entry in result["series"]}
        self.assertIn("biweb甲", series_by_name)
        self.assertIn("biweb乙", series_by_name)
        for name in ("biweb甲", "biweb乙"):
            self.assertEqual(len(dates), len(series_by_name[name]))

        first = dates.index(yesterday.strftime("%m-%d"))
        second = dates.index(today.strftime("%m-%d"))
        self.assertEqual(Decimal("100"), series_by_name["biweb甲"][first])
        self.assertEqual(Decimal("200"), series_by_name["biweb甲"][second])
        self.assertEqual(Decimal("0"), series_by_name["biweb乙"][first])
        self.assertEqual(Decimal("50"), series_by_name["biweb乙"][second])

        payload = run_trend_region_daily(self.mart_connection, {})
        self.assertEqual("line", payload["chart"])
        self.assertEqual(dates, payload["dates"])
        payload_series = {
            entry["name"]: entry["data"] for entry in payload["series"]
        }
        self.assertEqual(
            [100.0, 200.0],
            [payload_series["biweb甲"][first], payload_series["biweb甲"][second]],
        )
        self.assertEqual(
            [0.0, 50.0],
            [payload_series["biweb乙"][first], payload_series["biweb乙"][second]],
        )

    def test_region_month_target_max_per_person_and_mtd_truncation(self):
        today = self._server_today()
        tomorrow = today + timedelta(days=1)
        month, (first_day, last_day) = self._current_month_bounds()

        self._insert_offline_rows_full(
            [
                ("melt-past-1", "biweb甲", "biweb甲人员", today, Decimal("100"), "biweb一部", Decimal("1000")),
                ("melt-past-2", "biweb甲", "biweb甲人员", today, Decimal("50"), "biweb一部", Decimal("3000")),
                ("melt-future", "biweb甲", "biweb甲人员", tomorrow, Decimal("888888"), "biweb一部", Decimal("5000")),
                ("melt-summary", "biweb甲", "biweb甲合计", today, Decimal("999999"), None, Decimal("999999")),
            ]
        )

        # melt 陷阱：Σ各人员 MAX(monthly_target)，绝不跨行 SUM（SUM=9000）。
        # 月末边界：tomorrow 落次月时被 BETWEEN 排除，期望退为 3000
        # （melt 陷阱断言 3000 vs 4000 依旧成立，CURDATE 截断证明当日不足）。
        expected_target = Decimal("5000") if tomorrow <= last_day else Decimal("3000")
        self.assertEqual(
            expected_target,
            region_month_target(
                self.mart_connection, region="biweb甲", first_day=first_day, last_day=last_day
            ),
        )
        # 取数对称面：Σsales 截断未来 + 排除合计（无排除会是 1000149）。
        self.assertEqual(
            Decimal("150"),
            region_mtd_total(
                self.mart_connection, region="biweb甲", first_day=first_day, last_day=last_day
            ),
        )

        payload = run_kpi_region_mtd(
            self.mart_connection, {"region": "biweb甲", "month": month}
        )
        self.assertEqual("scalar", payload["chart"])
        self.assertEqual(150.0, payload["value"])
        self.assertEqual(float(expected_target), payload["target"])
        self.assertEqual(150.0 / float(expected_target), payload["rate"])
        self.assertEqual("元", payload["unit"])

    def test_department_mtd_ranking_orders_desc_and_labels_null_dept(self):
        today = self._server_today()
        month, (first_day, last_day) = self._current_month_bounds()

        self._insert_offline_rows_full(
            [
                ("dept-a", "biweb乙", "biweb乙甲", today, Decimal("300"), "biweb乙一部", None),
                ("dept-b", "biweb乙", "biweb乙乙", today, Decimal("100"), "biweb乙二部", None),
                ("dept-c", "biweb乙", "biweb乙丙", today, Decimal("50"), None, None),
                ("dept-sum", "biweb乙", "biweb乙合计", today, Decimal("999999"), "biweb乙一部", None),
            ]
        )

        ranking = department_mtd_ranking(
            self.mart_connection, region="biweb乙", first_day=first_day, last_day=last_day
        )
        self.assertEqual(["biweb乙一部", "biweb乙二部", "未分组"], ranking["categories"])
        self.assertEqual([Decimal("300"), Decimal("100"), Decimal("50")], ranking["values"])

        payload = run_bar_department_mtd(
            self.mart_connection, {"region": "biweb乙", "month": month}
        )
        self.assertEqual("bar", payload["chart"])
        self.assertEqual(["biweb乙一部", "biweb乙二部", "未分组"], payload["categories"])
        self.assertEqual([300.0, 100.0, 50.0], payload["values"])
        self.assertEqual("元", payload["unit"])

    def test_offline_dod_latest_becomes_today_with_consistent_delta(self):
        today = self._server_today()
        before = offline_dod(self.mart_connection)
        # 真实 mart 存在当日预填行且 sales_amount 全 NULL（SUM=NULL → value
        # None）：NULL 对合计零贡献，base 取 0，与「今日无真实行」同分支。
        base = (
            before["value"]
            if before
            and before["date"] == today.isoformat()
            and before["value"] is not None
            else 0.0
        )

        self._insert_offline_rows_full(
            [
                ("dod-normal", "biweb丙", "biweb丙人员", today, Decimal("100"), None, None),
                ("dod-summary", "biweb丙", "biweb丙合计", today, Decimal("999999"), None, None),
            ]
        )

        after = offline_dod(self.mart_connection)
        # 今日插行 → 全局 latest 必为 today；value = base + 100（合计行被排除）。
        self.assertEqual(today.isoformat(), after["date"])
        self.assertAlmostEqual(base + 100.0, after["value"], places=2)
        # trend7：7 个条目、ISO 日期自 6 天前递增到今天。
        self.assertEqual(7, len(after["trend7"]))
        self.assertEqual(
            [(today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)],
            [entry["date"] for entry in after["trend7"]],
        )
        # delta_pct 内部一致性（prev 取决于真实数据，条件断言）。
        if after["prev"] not in (None, 0.0):
            self.assertAlmostEqual(
                (after["value"] - after["prev"]) / after["prev"],
                after["delta_pct"],
                places=9,
            )

        payload = run_kpi_offline_dod(self.mart_connection, {})
        self.assertEqual("scalar", payload["chart"])
        self.assertEqual(after["value"], payload["value"])
        self.assertEqual(after["date"], payload["date"])
        self.assertEqual(after["trend7"], payload["trend7"])
        self.assertEqual("元", payload["unit"])

    def test_channel_dod_and_run_payload(self):
        today = self._server_today()
        before = channel_dod(self.mart_connection)
        # 真实 mart 存在当日预填行且 sales_amount 全 NULL（SUM=NULL → value
        # None）：NULL 对合计零贡献，base 取 0，与「今日无真实行」同分支。
        base = (
            before["value"]
            if before
            and before["date"] == today.isoformat()
            and before["value"] is not None
            else 0.0
        )

        self._insert_channel_rows_full(
            [("dod-channel", "biweb测试渠道", today, Decimal("100"), None, None)]
        )

        after = channel_dod(self.mart_connection)
        self.assertEqual(today.isoformat(), after["date"])
        self.assertAlmostEqual(base + 100.0, after["value"], places=2)

        payload = run_kpi_channel_dod(self.mart_connection, {})
        self.assertEqual("scalar", payload["chart"])
        self.assertEqual(after["value"], payload["value"])
        self.assertEqual("元", payload["unit"])

    def test_run_table_channel_mtd_aggregates_promo_and_roi(self):
        today = self._server_today()
        tomorrow = today + timedelta(days=1)
        month, _ = self._current_month_bounds()

        self._insert_channel_rows_full(
            [
                ("cmp-a", "biweb测试渠道", today, Decimal("200"), "biweb店铺A", Decimal("100")),
                ("cmp-b", "biweb测试渠道", today, Decimal("100"), "biweb店铺B", None),
                ("cmp-zero", "biweb测试渠道零", today, Decimal("100"), "biweb店铺C", Decimal("0")),
                ("cmp-null", "biweb测试渠道空", today, Decimal("100"), "biweb店铺D", None),
                ("cmp-future", "biweb测试渠道", tomorrow, Decimal("777"), "biweb店铺A", Decimal("1")),
            ]
        )

        payload = run_table_channel_mtd(self.mart_connection, {"month": month})
        self.assertEqual("table", payload["chart"])
        self.assertEqual(
            ["channel", "sales", "promo", "roi", "stores"],
            [column["key"] for column in payload["columns"]],
        )
        rows = {row["channel"]: row for row in payload["rows"]}
        # 组内聚合：Σsales=300（未来行被排除，否则 1077）、Σpromo=100（NULL 不计）；
        # ROI 在 run 层算（Σsales÷Σpromo），绝不取行级 roi 源列。
        self.assertEqual(300.0, rows["biweb测试渠道"]["sales"])
        self.assertEqual(100.0, rows["biweb测试渠道"]["promo"])
        self.assertEqual(3.0, rows["biweb测试渠道"]["roi"])
        self.assertEqual(2, rows["biweb测试渠道"]["stores"])
        # promo=0 → 除零护栏 → roi None；聚合 NULL → promo None → roi None（两条 None 路径）。
        self.assertEqual(100.0, rows["biweb测试渠道零"]["sales"])
        self.assertEqual(0.0, rows["biweb测试渠道零"]["promo"])
        self.assertIsNone(rows["biweb测试渠道零"]["roi"])
        self.assertIsNone(rows["biweb测试渠道空"]["promo"])
        self.assertIsNone(rows["biweb测试渠道空"]["roi"])

    def test_run_table_store_mtd_drills_by_channel_and_switches_columns(self):
        today = self._server_today()
        tomorrow = today + timedelta(days=1)
        month, _ = self._current_month_bounds()

        self._insert_channel_rows_full(
            [
                ("store-a", "biweb测试渠道", today, Decimal("300"), "biweb店铺A", None),
                ("store-b", "biweb测试渠道", today, Decimal("100"), "biweb店铺B", None),
                ("store-c", "biweb测试渠道二", today, Decimal("999"), "biweb店铺C", None),
                ("store-future", "biweb测试渠道", tomorrow, Decimal("888888"), "biweb店铺B", None),
            ]
        )

        drill = run_table_store_mtd(
            self.mart_connection, {"channel": "biweb测试渠道", "month": month}
        )
        self.assertEqual("table", drill["chart"])
        # 指定 channel：fixture 渠道无真实店铺 → 行集完全确定，且无 channel 列。
        self.assertEqual(
            ["rank", "store", "sales"], [column["key"] for column in drill["columns"]]
        )
        self.assertEqual(
            [("biweb店铺A", 1, 300.0), ("biweb店铺B", 2, 100.0)],
            [(row["store"], row["rank"], row["sales"]) for row in drill["rows"]],
        )

        full = run_table_store_mtd(self.mart_connection, {"month": month})
        self.assertIn("channel", [column["key"] for column in full["columns"]])
        rows = {row["store"]: row for row in full["rows"]}
        # 未来行排除：店铺B 停在 100（否则 888988）。
        self.assertEqual(300.0, rows["biweb店铺A"]["sales"])
        self.assertEqual(100.0, rows["biweb店铺B"]["sales"])
        self.assertEqual("biweb测试渠道二", rows["biweb店铺C"]["channel"])
        # 全表行序按 Σsales 降序：真实店铺穿插其间，fixture 三家相对次序确定。
        names = [row["store"] for row in full["rows"]]
        self.assertLess(names.index("biweb店铺C"), names.index("biweb店铺A"))
        self.assertLess(names.index("biweb店铺A"), names.index("biweb店铺B"))

    def test_region_month_daily_series_parameterized_region_filter(self):
        today = self._server_today()
        month, (first_day, last_day) = self._current_month_bounds()

        self._insert_offline_rows_full(
            [
                ("series-r", "biweb甲", "biweb甲人员", today, Decimal("100"), None, None),
                ("series-r-sum", "biweb甲", "biweb甲合计", today, Decimal("999999"), None, None),
            ]
        )

        series = region_month_daily_series(
            self.mart_connection, region="biweb甲", first_day=first_day, last_day=last_day
        )
        self.assertEqual(["biweb甲"], [entry["name"] for entry in series["series"]])
        self.assertEqual([today.strftime("%m-%d")], series["dates"])
        self.assertEqual(len(series["dates"]), len(series["series"][0]["data"]))
        # 合计行被 `%%合计%%` 排除（这条 SQL 是双写证明点之一）。
        self.assertEqual([Decimal("100")], series["series"][0]["data"])

    def test_people_cards_reconcile_with_mart_collect_on_real_calendar(self):
        month_end = self._server_month_start() - timedelta(days=1)
        month = month_end.strftime("%Y-%m")
        first_day = month_end.replace(day=1)
        workday = self._first_workday_of(first_day, month_end)
        if workday is None:
            self.skipTest(f"dim_calendar 无 {month} 日历行（先跑 extract-mart）")
        workdays = self._workday_count(first_day, month_end)

        self._insert_offline_rows_full(
            [
                ("ppl-hz", "杭州", "biweb人员甲", workday, Decimal("100"), "biweb部门", Decimal("1000")),
                ("ppl-sx", "绍兴", "biweb人员乙", workday, Decimal("50"), None, Decimal("100")),
                ("ppl-sum", "杭州", "biweb部门合计", workday, Decimal("9999"), "biweb部门", None),
            ]
        )

        # oracle：直接调 mart_collect（历史月锚点=月末，与 _people_as_of 一致）。
        direct_hz = mart_collect(
            self.mart_connection, region="杭州", business_date=month_end, include_today=True
        ).people
        direct_sx = mart_collect(
            self.mart_connection, region="绍兴", business_date=month_end, include_today=True
        ).people
        by_name = {person["name"]: person for person in direct_hz}
        self.assertIn("biweb人员甲", by_name)
        self.assertNotIn("biweb部门合计", by_name)
        # 绝对口径：已知工作日上一行 → completed 确定值、unfilled = 全月工作日 − 1。
        self.assertEqual(100.0, by_name["biweb人员甲"]["completed"])
        self.assertEqual(workdays - 1, by_name["biweb人员甲"]["unfilled"])
        self.assertAlmostEqual(0.1, by_name["biweb人员甲"]["rate"])

        # run 层对拍：载荷与 mart_collect 逐字段一致（含缓存路径）。
        table = run_table_people_leaderboard(
            self.mart_connection, {"region": "杭州", "month": month}
        )
        rows = {row["name"]: row for row in table["rows"]}
        self.assertIn("biweb人员甲", rows)
        self.assertEqual(by_name["biweb人员甲"]["completed"], rows["biweb人员甲"]["completed"])
        self.assertEqual(by_name["biweb人员甲"]["rate"], rows["biweb人员甲"]["rate"])
        self.assertEqual(by_name["biweb人员甲"]["unfilled"], rows["biweb人员甲"]["unfilled"])
        self.assertEqual("biweb部门", rows["biweb人员甲"]["dept"])

        count = run_kpi_people_count(
            self.mart_connection, {"region": "杭州", "month": month}
        )
        self.assertEqual(float(len(direct_hz)), count["value"])
        self.assertEqual("人", count["unit"])

        completed = run_kpi_people_completed(
            self.mart_connection, {"region": "杭州", "month": month}
        )
        self.assertEqual(
            float(sum(person["completed"] for person in direct_hz)),
            completed["value"],
        )

        rate = run_kpi_people_rate(
            self.mart_connection, {"region": "杭州", "month": month}
        )
        total_completed = sum(person["completed"] for person in direct_hz)
        total_target = sum(person["target"] for person in direct_hz)
        self.assertEqual(float(total_completed), rate["value"])
        self.assertEqual(float(total_target), rate["target"])
        self.assertEqual(total_completed / total_target, rate["rate"])

        # region 缺省：两区合并 + 全局重排（fixture 两人相对次序由 rate 决定）。
        merged = run_table_people_leaderboard(self.mart_connection, {"month": month})
        names = [row["name"] for row in merged["rows"]]
        self.assertIn("biweb人员甲", names)
        self.assertIn("biweb人员乙", names)
        self.assertLess(names.index("biweb人员乙"), names.index("biweb人员甲"))


if __name__ == "__main__":
    unittest.main()
