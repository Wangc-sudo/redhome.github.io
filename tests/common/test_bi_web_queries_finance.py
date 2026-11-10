"""test_bi_web_queries 的 finance 域测试；断言自原文件等价迁移。"""

from tests.common.bi_web_query_test_support import *


class FinSqlShapeTests(unittest.TestCase):
    """资金安全五卡 SQL 形态钉死（fund-safety-draft §3.1–3.5）。

    全部只读 mart ``fact_fin_*``、全静态（本批无 URL 参数）；快照表整表
    替换、无未来预填行，故**不做 CURDATE 截断**；>60 天桶/挂账天数是
    后端差额推导，SQL 里不得出现 CASE/日期差派生。
    """

    def _sole_static_sql(self, connection, run):
        run(connection, {})
        self.assertEqual(1, len(connection.executed))
        sql, parameters = connection.executed[0]
        self.assertIsNone(parameters)
        self.assertNotIn("CURDATE()", sql)
        return sql

    def test_kpi_overdue_sql_shape(self):
        connection = FakeConnection(
            rowsets={"fact_fin_receivables_aging": [
                {"overdue": Decimal("1"), "balance": Decimal("2")}
            ]}
        )

        sql = self._sole_static_sql(connection, run_kpi_fin_receivables_overdue)

        self.assertIn("FROM fact_fin_receivables_aging", sql)
        self.assertIn("SUM(overdue_amount)", sql)
        self.assertIn("SUM(ending_balance)", sql)

    def test_aging_sql_shape(self):
        connection = FakeConnection(rowsets={"fact_fin_receivables_aging": []})

        sql = self._sole_static_sql(connection, run_table_fin_receivables_aging)

        self.assertIn("FROM fact_fin_receivables_aging", sql)
        self.assertIn("aging_0_30", sql)
        self.assertIn("aging_31_60", sql)
        self.assertIn("updated_date", sql)  # 编制日期原样透传
        self.assertIn("ORDER BY overdue_amount DESC", sql)
        # >60 天桶是后端差额推导（draft §2.3 缺口），SQL 不得派生。
        self.assertNotIn("CASE", sql.upper())

    def test_prepayment_sql_shape(self):
        connection = FakeConnection(
            rowsets={"fact_fin_prepayment_invoice": []}
        )

        sql = self._sole_static_sql(
            connection, run_table_fin_prepayment_uninvoiced
        )

        self.assertIn("FROM fact_fin_prepayment_invoice", sql)
        self.assertIn("statement_date", sql)
        self.assertIn("ORDER BY uninvoiced_amount DESC", sql)

    def test_deposit_sql_shape_unions_offline_and_platform(self):
        connection = FakeConnection(rowsets={"fact_fin_offline_deposit": []})

        sql = self._sole_static_sql(connection, run_table_fin_deposit_status)

        self.assertIn("FROM fact_fin_offline_deposit", sql)
        self.assertIn("UNION ALL", sql)
        self.assertIn("FROM fact_fin_platform_deposit", sql)
        self.assertIn("'线下'", sql)
        self.assertIn("'平台'", sql)
        self.assertIn("ORDER BY deposit_balance DESC", sql)

    def test_store_funds_sql_shape(self):
        connection = FakeConnection(rowsets={"fact_fin_store_funds": []})

        sql = self._sole_static_sql(connection, run_trend_fin_store_funds)

        self.assertIn("FROM fact_fin_store_funds", sql)
        self.assertIn("ORDER BY month, store_name", sql)


class FinEntityTrendTests(unittest.TestCase):
    """⑩ 店铺资金余额趋势的主体参数化卡（2026-09-17 P1 下推 + P2 参数化）。

    形态钉死：entity 非空 → WHERE company_entity = %s 绑定（值绝不拼进
    SQL）；entity 空 → 不带 WHERE 的全主体聚合（绝不传恒真通配值）。
    载荷钉死：渠道名清洗（钉钉 dict 字面量）+ 缺月零填充。
    """

    def test_entity_path_binds_company_entity(self):
        connection = FakeConnection(rowsets={"fact_fin_store_funds": []})

        run_trend_fin_store_funds_entity(connection, {"entity": "biweb主体A"})

        self.assertEqual(1, len(connection.executed))
        sql, parameters = connection.executed[0]
        self.assertIn("FROM fact_fin_store_funds", sql)
        self.assertIn("WHERE company_entity = %s", sql)
        self.assertIn("GROUP BY month, channel", sql)
        self.assertIn("SUM(balance)", sql)
        self.assertEqual(("biweb主体A",), parameters)
        self.assertNotIn("biweb主体A", sql)  # 值绝不拼进 SQL 文本

    def test_blank_entity_uses_the_where_free_aggregate(self):
        for params in ({}, {"entity": ""}, {"entity": "   "}):
            with self.subTest(params=params):
                connection = FakeConnection(
                    rowsets={"fact_fin_store_funds": []}
                )

                run_trend_fin_store_funds_entity(connection, params)

                self.assertEqual(1, len(connection.executed))
                sql, parameters = connection.executed[0]
                self.assertIn("FROM fact_fin_store_funds", sql)
                self.assertNotIn("WHERE", sql)  # 空语义走另一条 SQL
                self.assertIsNone(parameters)  # 绝不传 ("%",) 之类

    def test_payload_cleans_channel_labels_and_zero_fills(self):
        rows = [
            {"month": "2026-01",
             "channel": "{'name': '拼多多', 'id': 'x'}",
             "balance": Decimal("10")},
            {"month": "2026-02",
             "channel": "{'name': '拼多多', 'id': 'x'}",
             "balance": Decimal("30")},
            {"month": "2026-02", "channel": "京东", "balance": Decimal("20")},
        ]
        connection = FakeConnection(rowsets={"fact_fin_store_funds": rows})

        payload = run_trend_fin_store_funds_entity(
            connection, {"entity": "biweb主体A"}
        )

        self.assertEqual("line", payload["chart"])
        self.assertEqual("元", payload["unit"])
        self.assertEqual(["2026-01", "2026-02"], payload["dates"])
        series = {entry["name"]: entry["data"] for entry in payload["series"]}
        self.assertEqual({"拼多多": [10.0, 30.0], "京东": [0.0, 20.0]}, series)

    def test_entity_options_sql_shape_and_values(self):
        connection = FakeConnection(
            rowsets={"fact_fin_store_funds": [
                {"company_entity": "biweb主体A"}, {"company_entity": None},
            ]}
        )

        options = entity_options(connection)

        self.assertEqual(["biweb主体A"], options)
        sql, parameters = connection.executed[0]
        self.assertIn("SELECT DISTINCT company_entity", sql)
        self.assertIn("FROM fact_fin_store_funds", sql)
        self.assertIn("ORDER BY company_entity", sql)
        self.assertIsNone(parameters)


class FinRunPayloadTests(unittest.TestCase):
    """资金安全五卡载荷：派生列（>60 桶/挂账天数/severity）与空值护栏。"""

    def test_kpi_overdue_payload_and_divide_by_zero_guard(self):
        connection = FakeConnection(
            rowsets={"fact_fin_receivables_aging": [
                {"overdue": Decimal("300"), "balance": Decimal("1000")}
            ]}
        )

        payload = run_kpi_fin_receivables_overdue(connection, {})

        self.assertEqual("scalar", payload["chart"])
        self.assertEqual("元", payload["unit"])
        self.assertEqual(300.0, payload["value"])
        self.assertEqual(1000.0, payload["target"])
        self.assertAlmostEqual(0.3, payload["rate"], delta=1e-9)

        zero = FakeConnection(
            rowsets={"fact_fin_receivables_aging": [
                {"overdue": Decimal("0"), "balance": Decimal("0")}
            ]}
        )
        # 除零护栏：balance 为 0 → rate None（前端「—」），绝不 ±∞。
        self.assertIsNone(run_kpi_fin_receivables_overdue(zero, {})["rate"])

    def test_aging_payload_derives_over_60_and_severity(self):
        rows = [
            {"counterparty_name": "biweb甲公司", "receivable_category": "货款",
             "company_entity": "biweb主体A", "ending_balance": Decimal("1000"),
             "overdue_amount": Decimal("0"), "aging_0_30": Decimal("300"),
             "aging_31_60": Decimal("200"), "updated_date": date(2026, 9, 15)},
            {"counterparty_name": "biweb乙公司", "receivable_category": "货款",
             "company_entity": "biweb主体A", "ending_balance": Decimal("800"),
             "overdue_amount": Decimal("10"), "aging_0_30": None,
             "aging_31_60": Decimal("100"), "updated_date": None},
        ]
        connection = FakeConnection(
            rowsets={"fact_fin_receivables_aging": rows}
        )

        payload = run_table_fin_receivables_aging(connection, {})

        self.assertEqual("table", payload["chart"])
        self.assertEqual("元", payload["unit"])
        self.assertEqual(("p0", "p1", "p2", "ok"),
                         tuple(payload["severity_domain"]))
        keys = [column["key"] for column in payload["columns"]]
        self.assertIn("aging_over_60", keys)
        self.assertEqual(
            "severity",
            [c for c in payload["columns"] if c["key"] == "severity"][0]["format"],
        )
        first, second = payload["rows"]
        self.assertEqual(500.0, first["aging_over_60"])  # 1000−300−200 差额推导
        self.assertIsInstance(first["ending_balance"], float)  # run_* 边界转 float
        self.assertEqual("p1", first["severity"])
        self.assertEqual("2026-09-15", first["updated_date"])  # 透传为 ISO 字符串
        self.assertIsNone(second["aging_over_60"])  # 分量缺失 → None，不补 0
        self.assertEqual("p2", second["severity"])  # 不可算 → p2
        self.assertIsNone(second["updated_date"])

    def test_prepayment_payload_derives_days_and_severity(self):
        today = datetime.now().date()
        rows = [
            {"supplier_name": "biweb供应商甲", "company_entity": "biweb主体A",
             "prepayment_ledger_amount": Decimal("500"),
             "ap_estimated_amount": Decimal("400"),
             "ledger_reconciliation_status": "相符",
             "uninvoiced_amount": Decimal("100"),
             "statement_date": today - timedelta(days=61)},
            {"supplier_name": "biweb供应商乙", "company_entity": "biweb主体A",
             "prepayment_ledger_amount": Decimal("300"),
             "ap_estimated_amount": None,
             "ledger_reconciliation_status": "待核",
             "uninvoiced_amount": Decimal("50"),
             "statement_date": None},
        ]
        connection = FakeConnection(
            rowsets={"fact_fin_prepayment_invoice": rows}
        )

        payload = run_table_fin_prepayment_uninvoiced(connection, {})

        first, second = payload["rows"]
        self.assertEqual(61, first["days_outstanding"])  # 挂账 61 天 > 60
        self.assertEqual("p1", first["severity"])
        self.assertEqual(
            (today - timedelta(days=61)).isoformat(), first["statement_date"]
        )
        self.assertIsNone(second["days_outstanding"])  # 对账日不可解析，不猜日期
        self.assertIsNone(second["statement_date"])
        self.assertEqual("p2", second["severity"])  # 不可算 → p2
        self.assertEqual("待核", second["reconciliation"])  # 账账核对原文透传

    def test_deposit_payload_passthrough_status_and_severity(self):
        rows = [
            {"company_entity": "biweb主体A", "counterparty": "biweb供应商甲",
             "project_name": "biweb项目X", "status": "正常合作",
             "deposit_balance": Decimal("200"),
             "updated_at": datetime(2026, 9, 1, 12, 0), "source": "线下"},
            {"company_entity": "biweb主体B", "counterparty": "biweb店铺乙",
             "project_name": "biweb项目Y", "status": "已终止",
             "deposit_balance": Decimal("300"),
             "updated_at": None, "source": "平台"},
        ]
        connection = FakeConnection(
            rowsets={"fact_fin_offline_deposit": rows}
        )

        payload = run_table_fin_deposit_status(connection, {})

        first, second = payload["rows"]
        self.assertEqual("正常合作", first["status"])  # 状态枚举不固化，原文透传
        self.assertEqual("ok", first["severity"])
        self.assertEqual("线下", first["source"])
        self.assertEqual("2026-09-01T12:00:00", first["updated_at"])
        self.assertEqual("已终止", second["status"])
        self.assertEqual("p2", second["severity"])  # 非正常状态 → p2 关注
        self.assertEqual("平台", second["source"])
        self.assertIsNone(second["updated_at"])  # 平台侧无更新时间列 → None

    def test_store_funds_trend_zero_fills_missing_months(self):
        rows = [
            {"month": "2026-02", "store_name": "biweb店A", "channel": "天猫",
             "balance": Decimal("10")},
            {"month": "2026-01", "store_name": "biweb店B", "channel": "京东",
             "balance": Decimal("20")},
            {"month": "2026-02", "store_name": "biweb店B", "channel": "京东",
             "balance": Decimal("30")},
        ]
        connection = FakeConnection(rowsets={"fact_fin_store_funds": rows})

        payload = run_trend_fin_store_funds(connection, {})

        self.assertEqual("line", payload["chart"])
        self.assertEqual("元", payload["unit"])
        self.assertEqual(["2026-01", "2026-02"], payload["dates"])  # 月升序轴
        series = {entry["name"]: entry["data"] for entry in payload["series"]}
        self.assertEqual([0.0, 10.0], series["biweb店A"])  # 缺月零填充
        self.assertEqual([20.0, 30.0], series["biweb店B"])

    def test_empty_tables_yield_empty_payloads(self):
        connection = FakeConnection()

        aging = run_table_fin_receivables_aging(connection, {})
        trend = run_trend_fin_store_funds(connection, {})

        self.assertEqual([], aging["rows"])
        self.assertEqual([], trend["dates"])
        self.assertEqual([], trend["series"])
