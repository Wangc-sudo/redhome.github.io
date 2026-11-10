"""test_bi_web_queries 的 sku 域测试；断言自原文件等价迁移。"""

from tests.common.bi_web_query_test_support import *


class SkuMtdDistributionTests(SqlShapeTestCase):
    """SKU 当月分布：trade_time 月内截断 + spec 聚合 + 名称回退。"""

    def test_sku_mtd_distribution_sql_shape(self):
        connection = FakeConnection(rowsets={"fact_order_line": []})

        sku_mtd_distribution(connection)

        sql = self.sole_static_sql(connection)
        self.assertIn("FROM fact_order_line", sql)
        # DATETIME 口径：月首起、「< 明天」截断（「<= 今天」会漏当日行）。
        self.assertIn(
            "trade_time >= DATE_FORMAT(CURDATE(), '%Y-%m-01')", sql
        )
        self.assertIn("trade_time < DATE_ADD(CURDATE(), INTERVAL 1 DAY)", sql)
        self.assertIn("GROUP BY spec_no, goods_name", sql)
        self.assertIn("ORDER BY total DESC", sql)

    def test_distribution_shapes_names_and_decimal_values(self):
        rows = [
            {
                "spec_no": "SKU-1",
                "goods_name": "冻干草莓",
                "total": Decimal("300"),
            },
            {"spec_no": "SKU-2", "goods_name": "黄桃罐头", "total": Decimal("100")},
        ]
        connection = FakeConnection(rowsets={"fact_order_line": rows})

        self.assertEqual(
            [
                {"name": "冻干草莓", "value": Decimal("300")},
                {"name": "黄桃罐头", "value": Decimal("100")},
            ],
            sku_mtd_distribution(connection),
        )

    def test_name_falls_back_to_spec_no_when_goods_name_missing(self):
        rows = [
            {"spec_no": "SKU-9", "goods_name": None, "total": Decimal("7")},
            {"spec_no": "SKU-8", "goods_name": "  ", "total": Decimal("5")},
        ]
        connection = FakeConnection(rowsets={"fact_order_line": rows})

        self.assertEqual(
            [
                {"name": "SKU-9", "value": Decimal("7")},
                {"name": "SKU-8", "value": Decimal("5")},
            ],
            sku_mtd_distribution(connection),
        )


class SkuHotRankingTests(unittest.TestCase):
    """商品动销（l2-product）：总榜 / 分品牌 / 分渠道榜单 + KPI。"""

    _AS_OF = date(2026, 9, 14)

    @staticmethod
    def _row(spec_no, goods, brand, mtd, qty, latest, prev):
        return {
            "spec_no": spec_no,
            "goods_name": goods,
            "brand_name": brand,
            "channel_name": "抖音",
            "mtd_amount": Decimal(str(mtd)),
            "mtd_qty": Decimal(str(qty)),
            "latest_amount": Decimal(str(latest)),
            "prev_amount": Decimal(str(prev)),
        }

    def test_total_ranking_shapes_rows_with_share_qty_and_delta(self):
        rows = [
            self._row("S1", "冻干草莓", "良品铺子", 300, 30, 120, 100),
            self._row("S2", "黄桃罐头", "良品铺子", 100, 10, 50, 0),
        ]
        connection = FakeSkuConnection(as_of=self._AS_OF, rollup_rows=rows)

        payload = run_table_sku_hot_total(connection, {"month": "2026-09"})

        self.assertEqual("table", payload["chart"])
        self.assertEqual("2026-09-14", payload["as_of"])
        self.assertEqual(
            ["排名", "商品", "品牌", "累计销售额", "销售占比", "累计销量",
             "昨日销售额", "环比"],
            [column["title"] for column in payload["columns"]],
        )
        first, second = payload["rows"]
        self.assertEqual(
            (1, "冻干草莓", "良品铺子"),
            (first["rank"], first["goods"], first["brand"]),
        )
        self.assertEqual(300.0, first["sales"])
        self.assertEqual(0.75, first["share"])
        self.assertEqual(30.0, first["qty"])
        self.assertEqual(120.0, first["latest"])
        self.assertAlmostEqual(0.2, first["delta_pct"])
        # 前一日为 0 → 环比不可算，给 null 而不是 0%（前端显示「—」）。
        self.assertIsNone(second["delta_pct"])
        self.assertEqual(0.25, second["share"])

    def test_rollup_binds_month_and_day_windows_as_parameters(self):
        connection = FakeSkuConnection(as_of=self._AS_OF)

        run_table_sku_hot_total(connection, {"month": "2026-09"})

        latest_sql, latest_params = connection.executed[0]
        self.assertIn("MAX(DATE(trade_time))", latest_sql)
        self.assertEqual((date(2026, 9, 1), date(2026, 10, 1)), latest_params)
        rollup_sql, rollup_params = connection.executed[1]
        self.assertIn("FROM fact_order_line", rollup_sql)
        self.assertIn("GROUP BY brand_name, spec_no, goods_name", rollup_sql)
        # 月累计 / 月销量 / 数据日 / 前一日 / 扫描窗口，十个绑定值。
        self.assertEqual(
            (date(2026, 9, 1), date(2026, 10, 1),
             date(2026, 9, 1), date(2026, 10, 1),
             date(2026, 9, 14), date(2026, 9, 15),
             date(2026, 9, 13), date(2026, 9, 14),
             date(2026, 9, 1), date(2026, 10, 1)),
            rollup_params,
        )

    def test_prev_day_before_month_start_widens_the_scan_window(self):
        # 数据截至 9/1 → 前一日是 8/31，扫描窗口必须跨到上月才取得到。
        connection = FakeSkuConnection(as_of=date(2026, 9, 1))

        run_table_sku_hot_total(connection, {"month": "2026-09"})

        _, rollup_params = connection.executed[1]
        self.assertEqual(date(2026, 8, 31), rollup_params[-2])
        self.assertEqual(date(2026, 10, 1), rollup_params[-1])

    def test_empty_month_yields_empty_rows_and_no_as_of(self):
        connection = FakeSkuConnection(as_of=None)

        payload = run_table_sku_hot_total(connection, {"month": "2026-09"})

        self.assertEqual([], payload["rows"])
        self.assertIsNone(payload["as_of"])

    def test_brand_ranking_keeps_top_five_per_brand(self):
        rows = [
            self._row(f"S{index}", f"商品{index}", "A", 100 - index, 1, 10, 5)
            for index in range(7)
        ]
        rows.append(self._row("T1", "黄桃罐头", "B", 50, 1, 5, 4))
        connection = FakeSkuConnection(as_of=self._AS_OF, rollup_rows=rows)

        payload = run_table_sku_hot_brand(connection, {"month": "2026-09"})

        self.assertEqual(
            ["品牌", "组内排名", "商品", "累计销售额", "昨日销售额", "环比"],
            [column["title"] for column in payload["columns"]],
        )
        # A 组 7 个截断到 5，B 组 1 个全留。
        self.assertEqual(6, len(payload["rows"]))
        self.assertEqual(
            [1, 2, 3, 4, 5],
            [row["rank"] for row in payload["rows"] if row["brand_name"] == "A"],
        )
        self.assertEqual(
            [1], [row["rank"] for row in payload["rows"] if row["brand_name"] == "B"]
        )
        self.assertEqual("B", payload["rows"][-1]["brand_name"])

    def test_brand_ranking_binds_the_selected_brand(self):
        connection = FakeSkuConnection(as_of=self._AS_OF)

        run_table_sku_hot_brand(
            connection, {"month": "2026-09", "brand": "良品铺子"}
        )

        rollup_sql, rollup_params = connection.executed[1]
        self.assertIn("AND brand_name = %s", rollup_sql)
        self.assertIn("GROUP BY brand_name, spec_no, goods_name", rollup_sql)
        self.assertEqual("良品铺子", rollup_params[-1])

    def test_total_share_uses_the_whole_month_as_denominator(self):
        # 榜只展示前 N 名，但占比分母是全部商品，否则「占比」合计会超过 100%。
        rows = [
            self._row(f"S{index}", f"商品{index}", "A", 100 - index, 1, 10, 5)
            for index in range(60)
        ]
        connection = FakeSkuConnection(as_of=self._AS_OF, rollup_rows=rows)

        payload = run_table_sku_hot_total(connection, {"month": "2026-09"})

        # 60 个商品销售额 100..41，合计 4230；榜上前 50 的占比仍以此为分母。
        self.assertEqual(50, len(payload["rows"]))
        self.assertAlmostEqual(100 / 4230, payload["rows"][0]["share"], places=6)

    def test_channel_ranking_binds_the_selected_channel(self):
        connection = FakeSkuConnection(as_of=self._AS_OF)

        run_table_sku_hot_channel(connection, {"month": "2026-09", "channel": "抖音"})

        rollup_sql, rollup_params = connection.executed[1]
        self.assertIn("AND channel_name = %s", rollup_sql)
        self.assertIn("GROUP BY channel_name, spec_no, goods_name", rollup_sql)
        self.assertEqual("抖音", rollup_params[-1])

    def test_kpi_sku_mtd_sums_month_and_day_windows(self):
        rows = [
            self._row("S1", "冻干草莓", "A", 300, 30, 120, 100),
            self._row("S2", "黄桃罐头", "A", 100, 10, 30, 50),
        ]
        connection = FakeSkuConnection(
            as_of=self._AS_OF,
            rollup_rows=rows,
            trend_rows=[
                {"d": date(2026, 9, 13), "total": Decimal("100")},
                {"d": date(2026, 9, 14), "total": Decimal("150")},
            ],
        )

        payload = run_kpi_sku_mtd(connection, {"month": "2026-09"})

        self.assertEqual("scalar", payload["chart"])
        self.assertEqual(400.0, payload["value"])
        self.assertEqual("2026-09-14", payload["date"])
        self.assertEqual(150.0, payload["prev"])
        self.assertAlmostEqual(0.0, payload["delta_pct"])
        self.assertEqual(2, payload["sku_count"])
        self.assertEqual(2, payload["active_sku_count"])
        # 趋势固定 7 天，缺数日为 null（前端断线而不是连到 0）。
        self.assertEqual(7, len(payload["trend7"]))
        self.assertEqual("2026-09-08", payload["trend7"][0]["date"])
        self.assertIsNone(payload["trend7"][0]["value"])
        self.assertEqual(150.0, payload["trend7"][-1]["value"])

    def test_kpi_sku_mtd_without_data_stays_zero(self):
        connection = FakeSkuConnection(as_of=None)

        payload = run_kpi_sku_mtd(connection, {"month": "2026-09"})

        self.assertEqual(0.0, payload["value"])
        self.assertIsNone(payload["date"])
        self.assertIsNone(payload["prev"])
        self.assertIsNone(payload["delta_pct"])
        self.assertEqual([], payload["trend7"])
        self.assertEqual(0, payload["sku_count"])

    def test_brand_options_drop_nulls(self):
        connection = FakeConnection(
            rowsets={"fact_order_line": [
                {"brand_name": "良品铺子"}, {"brand_name": None},
            ]}
        )

        self.assertEqual(["良品铺子"], brand_options(connection))
        sql, parameters = connection.executed[0]
        self.assertIn("SELECT DISTINCT brand_name FROM fact_order_line", sql)
        self.assertIn("ORDER BY brand_name", sql)
        self.assertIsNone(parameters)

    def test_sku_channel_options_drop_nulls(self):
        connection = FakeConnection(
            rowsets={"fact_order_line": [
                {"channel_name": "抖音"}, {"channel_name": ""},
            ]}
        )

        self.assertEqual(["抖音"], sku_channel_options(connection))
        sql, parameters = connection.executed[0]
        self.assertIn("SELECT DISTINCT channel_name FROM fact_order_line", sql)
        self.assertIn("ORDER BY channel_name", sql)
        self.assertIsNone(parameters)
