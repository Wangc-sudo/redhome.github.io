"""test_bi_web_queries 的 channel 域测试；断言自原文件等价迁移。"""

from tests.common.bi_web_query_test_support import *


class ChannelSqlShapeTests(SqlShapeTestCase):
    """The channel-side queries: truncation only, never a 合计 filter."""

    def test_channel_mtd_total_sql_shape(self):
        connection = _DodFakeConnection(
            scalars={"fact_channel_daily_sales": Decimal("1")},
            latest={"fact_channel_daily_sales": date(2026, 9, 15)},
        )

        total = channel_mtd_total(connection)

        # 2026-09-21（渠道水位批次）两条语句：静态 MAX 真实水位定锚
        # （过滤预填 NULL 行）+ 参数化窗口求和；渠道侧无合计行过滤。
        self.assertEqual(Decimal("1"), total)
        self.assertEqual(2, len(connection.executed))
        latest_sql, latest_params = connection.executed[0]
        sum_sql, sum_params = connection.executed[1]
        self.assertIsNone(latest_params)
        self.assertIn("MAX(business_date)", latest_sql)
        self.assertIn("FROM fact_channel_daily_sales", latest_sql)
        self.assertIn(_TRUNCATION, latest_sql)
        self.assertIn("sales_amount IS NOT NULL", latest_sql)
        self.assertNotIn("合计", latest_sql)
        self.assertIn("FROM fact_channel_daily_sales", sum_sql)
        self.assertIn("business_date >= DATE_FORMAT(CURDATE(), '%%Y-%%m-01')", sum_sql)
        self.assertIn("business_date <= %s", sum_sql)
        self.assertNotIn(_TRUNCATION, sum_sql)
        self.assertNotIn("合计", sum_sql)
        self.assertEqual(0, sum_sql.replace("%%", "").replace("%s", "").count("%"))
        self.assertEqual((date(2026, 9, 15),), sum_params)

    def test_channel_mtd_total_empty_water_level_short_circuits(self):
        connection = _DodFakeConnection(
            latest={"fact_channel_daily_sales": None}
        )

        self.assertEqual(Decimal(0), channel_mtd_total(connection))

        self.assertEqual(1, len(connection.executed))

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
            connection, (date(2026, 9, 1), date(2026, 9, 30)),
            table="fact_channel_daily_sales",
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
        # 三态完整轴（_align_series_rows 三处共用，执行提示词 §4.2②）：
        # 历史月 2026-08 钉死 31 天窗口，缺失日 None+missing。
        rows = [
            {"business_date": date(2026, 8, 2), "channel": "天猫", "total": Decimal("5")},
            {"business_date": date(2026, 8, 2), "channel": "京东", "total": Decimal("3")},
            {"business_date": date(2026, 8, 3), "channel": "天猫", "total": Decimal("7")},
        ]
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": rows})

        payload = run_trend_channel_daily(connection, {"month": "2026-08"})

        self.assertEqual("line", payload["chart"])
        self.assertEqual(31, len(payload["dates"]))
        self.assertEqual("08-01", payload["dates"][0])
        self.assertEqual("08-31", payload["dates"][-1])
        self.assertEqual(
            [
                # 京东 08-03：本系列无行但当日天猫有行 → 0.0+missing。
                {"name": "京东", "data": [None, 3.0, 0.0] + [None] * 28},
                {"name": "天猫", "data": [None, 5.0, 7.0] + [None] * 28},
            ],
            payload["series"],
        )
        self.assertEqual(
            [["missing", "ok", "missing"] + ["missing"] * 28,
             ["missing", "ok", "ok"] + ["missing"] * 28],
            payload["status"],
        )
        sql, parameters = connection.executed[0]
        self.assertEqual((date(2026, 8, 1), date(2026, 8, 31)), parameters)

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
