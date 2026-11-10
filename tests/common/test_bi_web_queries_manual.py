"""test_bi_web_queries 的 manual 域测试；断言自原文件等价迁移。"""

from tests.common.bi_web_query_test_support import *


class ManualReportSqlShapeTests(unittest.TestCase):
    """人工报表窄行查询的形态钉死（消费契约 §2.2 的三处关键差异）。

    该查询绑定 dataset/period_start 两个参数（%s 占位、值绝不拼进 SQL），
    形态不符「全静态」，故不进 ``StaticSqlTests._QUERY_FUNCTIONS`` —— 这里
    以同等强度钉它的带参形态。
    """

    def test_manual_report_rows_binds_dataset_and_period_start(self):
        connection = FakeConnection(rowsets={"fact_manual_report": []})

        manual_report_rows(
            connection, dataset="ecommerce_monthly",
            period_start=date(2026, 8, 1),
        )

        self.assertEqual(1, len(connection.executed))
        sql, parameters = connection.executed[0]
        self.assertIn("FROM fact_manual_report", sql)
        self.assertIn("dataset = %s", sql)
        self.assertIn("period_start = %s", sql)
        self.assertEqual(("ecommerce_monthly", date(2026, 8, 1)), parameters)

    def test_manual_report_sql_has_no_truncation_summary_or_case_when(self):
        connection = FakeConnection(rowsets={"fact_manual_report": []})

        manual_report_rows(
            connection, dataset="restaurant_monthly",
            period_start=date(2026, 8, 1),
        )

        sql, _ = connection.executed[0]
        # 无未来预填行 → 不做 CURDATE 截断（历史月要看全月数）。
        self.assertNotIn("CURDATE()", sql)
        # 总计行不入库 → 不做「合计」过滤（与线下日报表的关键差异）。
        self.assertNotIn("合计", sql)
        # metric 不写死 → 无 CASE WHEN 列展开（模板加列不改 SQL）。
        self.assertNotIn("CASE", sql.upper())


class ManualReportRowsTests(unittest.TestCase):
    """只读事实层：窄行原样取回，Decimal 不透改、NULL 不补 0。"""

    def test_rows_are_returned_as_is(self):
        rows = [
            {"dim_scope": "channel", "dimension_value": "天猫",
             "metric": "revenue", "value": Decimal("100.5"), "unit": "元"},
            {"dim_scope": "channel", "dimension_value": "天猫",
             "metric": "expense", "value": None, "unit": "元"},
        ]
        connection = FakeConnection(rowsets={"fact_manual_report": rows})

        result = manual_report_rows(
            connection, dataset="ecommerce_monthly",
            period_start=date(2026, 8, 1),
        )

        self.assertEqual(rows, result)
        self.assertIsInstance(result[0]["value"], Decimal)  # 低层保持 Decimal
        self.assertIsNone(result[1]["value"])  # 选填未填 = None，绝不补 0


class PivotManualRowsTests(unittest.TestCase):
    """窄行→宽行：dim_scope 过滤、缺失指标=键不存在、直接取值不 SUM。"""

    def test_pivot_groups_metrics_by_dimension_value(self):
        rows = [
            {"dim_scope": "channel", "dimension_value": "天猫",
             "metric": "revenue", "value": Decimal("100"), "unit": "元"},
            {"dim_scope": "channel", "dimension_value": "天猫",
             "metric": "gross_profit", "value": Decimal("30"), "unit": "元"},
            {"dim_scope": "channel", "dimension_value": "京东",
             "metric": "revenue", "value": Decimal("50"), "unit": "元"},
        ]

        wide = pivot_manual_rows(rows, dim_scope="channel")

        self.assertEqual(
            [
                {"name": "天猫", "revenue": Decimal("100"),
                 "gross_profit": Decimal("30")},
                {"name": "京东", "revenue": Decimal("50")},
            ],
            wide,
        )
        # 缺失指标 = 键不存在（消费方按 None 处理），不是 0。
        self.assertNotIn("gross_profit", wide[1])

    def test_pivot_drops_other_dim_scopes_and_keeps_null_values(self):
        rows = [
            {"dim_scope": "store", "dimension_value": "天猫",
             "metric": "revenue", "value": Decimal("999"), "unit": "元"},
            {"dim_scope": "channel", "dimension_value": "天猫",
             "metric": "expense", "value": None, "unit": "元"},
        ]

        wide = pivot_manual_rows(rows, dim_scope="channel")

        self.assertEqual([{"name": "天猫", "expense": None}], wide)

    def test_pivot_skips_rows_without_name_or_metric(self):
        rows = [
            {"dim_scope": "channel", "dimension_value": None,
             "metric": "revenue", "value": Decimal("1"), "unit": "元"},
            {"dim_scope": "channel", "dimension_value": "天猫",
             "metric": None, "value": Decimal("1"), "unit": "元"},
        ]

        self.assertEqual([], pivot_manual_rows(rows, dim_scope="channel"))


class RunTableManualMonthlyTests(unittest.TestCase):
    """④⑤ 两张人工月报表卡：载荷形状、float 边界、派生列与空值护栏。"""

    _ROWS = (
        {"dim_scope": "channel", "dimension_value": "天猫",
         "metric": "revenue", "value": Decimal("200"), "unit": "元"},
        {"dim_scope": "channel", "dimension_value": "天猫",
         "metric": "gross_profit", "value": Decimal("60"), "unit": "元"},
        {"dim_scope": "channel", "dimension_value": "天猫",
         "metric": "expense", "value": None, "unit": "元"},
        {"dim_scope": "channel", "dimension_value": "京东",
         "metric": "revenue", "value": Decimal("100"), "unit": "元"},
        {"dim_scope": "channel", "dimension_value": "京东",
         "metric": "gross_profit", "value": Decimal("-25"), "unit": "元"},
    )

    def test_ecommerce_payload_shape_and_margin(self):
        connection = FakeConnection(
            rowsets={"fact_manual_report": [dict(row) for row in self._ROWS]}
        )

        payload = run_table_manual_ecommerce_monthly(
            connection, {"month": "2026-08"}
        )

        self.assertEqual("table", payload["chart"])
        self.assertEqual("元", payload["unit"])
        self.assertEqual("2026-08", payload["month"])
        self.assertEqual("ecommerce_monthly", payload["dataset"])
        self.assertEqual(
            ["name", "revenue", "gross_profit", "expense", "margin"],
            [column["key"] for column in payload["columns"]],
        )
        self.assertEqual("渠道", payload["columns"][0]["title"])
        self.assertEqual(
            "percent",
            [c for c in payload["columns"] if c["key"] == "margin"][0]["format"],
        )
        rows = {row["name"]: row for row in payload["rows"]}
        tmall = rows["天猫"]
        self.assertEqual(200.0, tmall["revenue"])
        self.assertIsInstance(tmall["revenue"], float)  # run_* 边界转 float
        self.assertIsNone(tmall["expense"])  # NULL → None（前端「—」），不补 0
        self.assertAlmostEqual(0.3, tmall["margin"], delta=1e-9)
        jingdong = rows["京东"]
        self.assertIsNone(jingdong["expense"])  # 缺失指标按 None 处理
        self.assertAlmostEqual(-0.25, jingdong["margin"], delta=1e-9)  # 负值照实

    def test_period_start_is_the_month_first_day(self):
        connection = FakeConnection(rowsets={"fact_manual_report": []})

        run_table_manual_restaurant_monthly(connection, {"month": "2026-08"})

        _sql, parameters = connection.executed[0]
        self.assertEqual(("restaurant_monthly", date(2026, 8, 1)), parameters)

    def test_margin_column_absent_without_revenue_and_gross_profit(self):
        rows = [
            {"dim_scope": "store", "dimension_value": "biweb门店甲",
             "metric": "expense", "value": Decimal("10"), "unit": "元"},
        ]
        connection = FakeConnection(rowsets={"fact_manual_report": rows})

        payload = run_table_manual_restaurant_monthly(
            connection, {"month": "2026-08"}
        )

        self.assertEqual("restaurant_monthly", payload["dataset"])
        self.assertEqual(
            ["name", "expense"],
            [column["key"] for column in payload["columns"]],
        )
        self.assertEqual("门店", payload["columns"][0]["title"])
        self.assertEqual(
            [{"name": "biweb门店甲", "expense": 10.0}], payload["rows"]
        )

    def test_unknown_metric_falls_back_to_key_as_title(self):
        rows = [
            {"dim_scope": "store", "dimension_value": "biweb门店甲",
             "metric": "tax", "value": Decimal("3"), "unit": "元"},
        ]
        connection = FakeConnection(rowsets={"fact_manual_report": rows})

        payload = run_table_manual_restaurant_monthly(
            connection, {"month": "2026-08"}
        )

        # 模板新增指标：列自动出现、键名兜底作标题，代码零改动。
        self.assertEqual(
            {"key": "tax", "title": "tax", "format": "wan"},
            payload["columns"][1],
        )

    def test_empty_month_yields_only_the_dimension_column(self):
        connection = FakeConnection(rowsets={"fact_manual_report": []})

        payload = run_table_manual_ecommerce_monthly(
            connection, {"month": "2026-08"}
        )

        self.assertEqual([{"key": "name", "title": "渠道"}], payload["columns"])
        self.assertEqual([], payload["rows"])


class RunTableManualShowroomMonthlyTests(unittest.TestCase):
    """⑪ 体验馆月报卡：克隆 ④⑤ 模式，dataset 固化 + 维度标题「体验馆」。"""

    def test_binds_showroom_dataset_and_month_first_day(self):
        connection = FakeConnection(rowsets={"fact_manual_report": []})

        payload = run_table_manual_showroom_monthly(connection, {"month": "2026-08"})

        _sql, parameters = connection.executed[0]
        self.assertEqual(("showroom_monthly", date(2026, 8, 1)), parameters)
        self.assertEqual("showroom_monthly", payload["dataset"])
        self.assertEqual("2026-08", payload["month"])
        self.assertEqual([{"key": "name", "title": "体验馆"}], payload["columns"])
        self.assertEqual([], payload["rows"])  # 未首填自然挂零

    def test_payload_uses_store_scope_with_hall_label(self):
        rows = [
            {"dim_scope": "store", "dimension_value": "biweb万科馆",
             "metric": "revenue", "value": Decimal("80"), "unit": "元"},
            {"dim_scope": "store", "dimension_value": "biweb万科馆",
             "metric": "gross_profit", "value": Decimal("20"), "unit": "元"},
        ]
        connection = FakeConnection(rowsets={"fact_manual_report": rows})

        payload = run_table_manual_showroom_monthly(connection, {"month": "2026-08"})

        self.assertEqual("体验馆", payload["columns"][0]["title"])
        self.assertEqual("元", payload["unit"])
        self.assertEqual(
            [{"name": "biweb万科馆", "revenue": 80.0, "gross_profit": 20.0,
              "margin": 0.25}],
            payload["rows"],
        )
