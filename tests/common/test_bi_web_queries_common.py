"""test_bi_web_queries 的 common 域测试；断言自原文件等价迁移。"""

from tests.common.bi_web_query_test_support import *


class AnnualTargetSqlShapeTests(SqlShapeTestCase):
    """The target denominator is the two-line scope, current year only."""

    def test_annual_target_total_sql_shape(self):
        connection = FakeConnection(rowsets={"dim_target": []})

        annual_target_total(connection)

        sql = self.sole_static_sql(connection)
        self.assertIn("FROM dim_target", sql)
        self.assertIn("scope = 'line'", sql)
        self.assertIn("year = YEAR(CURDATE())", sql)
        self.assertIn("GROUP BY scope_key", sql)
        # 2026-09-21 起分母 = 全业务线（含餐饮），不再只取两线。
        self.assertNotIn("scope_key IN", sql)


class StaticSqlTests(unittest.TestCase):
    """SQL 全静态: no query ever binds or splices a value.

    B2（水位批次）起 ``offline_mtd_total`` 移出本表：窗口上界改绑定
    真实水位（参数化），其形状由 OfflineSqlShapeTests 锁定；水位
    定锚（MAX）与窗口序列本身仍是静态语句，留在表内一并校验。
    """

    _QUERY_FUNCTIONS = (
        offline_annual_total,
        channel_annual_total,
        annual_target_total,
        region_daily_series,
        channel_mtd_ranking,
        sku_mtd_distribution,
    )

    def test_every_query_executes_static_sql_without_parameters(self):
        for function in self._QUERY_FUNCTIONS:
            with self.subTest(query=function.__name__):
                connection = _DodFakeConnection(
                    scalars={
                        "fact_daily_report_offline": Decimal("0"),
                        "fact_channel_daily_sales": Decimal("0"),
                        "dim_target": Decimal("0"),
                    },
                    rowsets={
                        "fact_daily_report_offline": [],
                        "fact_channel_daily_sales": [],
                    },
                    # region_daily_series 的水位定锚需要一个真日期；
                    # 钉当天 ⇒ 轴端点与旧口径（锚点=今天）一致。
                    latest={"fact_daily_report_offline": date.today()},
                )

                function(connection)

                self.assertTrue(connection.executed)
                for sql, parameters in connection.executed:
                    self.assertIsInstance(sql, str)
                    if "dim_calendar" in sql:
                        # 三态改造（执行提示词 §4.2⑥）：营业日判定复用
                        # month_workdays，只允许绑定计算所得的日期窗口，
                        # 绝无用户输入——与「全静态」同一保护级别。
                        self.assertIsNotNone(parameters)
                        self.assertTrue(
                            all(isinstance(value, date) for value in parameters)
                        )
                        continue
                    # No bound parameters and no %s placeholder: nothing
                    # user-controlled can ever enter the SQL text.
                    self.assertIsNone(parameters)
                    self.assertNotIn("%s", sql)


class ScalarTotalTests(unittest.TestCase):
    """The total queries return the scripted Decimal."""

    def test_scalar_totals_return_decimal(self):
        # B2：offline_mtd_total 改水位窗口（定锚 date + 求和 Decimal
        # 同表不同型），Decimal 透传断言移至 OfflineSqlShapeTests。
        # 2026-09-21：annual_target_total 改行查询（按线展开）、
        # channel_mtd_total 改水位窗口（同 offline B2），透传断言分别
        # 移至 RunPayloadTests 与 ChannelSqlShapeTests。
        cases = (
            ("fact_daily_report_offline", (offline_annual_total,)),
            ("fact_channel_daily_sales", (channel_annual_total,)),
        )
        for table, functions in cases:
            for function in functions:
                with self.subTest(query=function.__name__):
                    connection = FakeConnection(scalars={table: Decimal("42.5")})

                    self.assertEqual(Decimal("42.5"), function(connection))


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


class GranularityOptionsTests(unittest.TestCase):
    """granularity 静态值域（执行提示词 §4.2①/§6-5；2026-09-21 批次 A5
    增 year）：与查询型 source 同签名但不查库，供 option_sets.contains()
    值域闸校验。"""

    def test_returns_the_gran_domain(self):
        connection = FakeConnection()

        self.assertEqual(
            ["day", "week", "month", "year"], granularity_options(connection)
        )

    def test_static_domain_never_touches_the_database(self):
        connection = FakeConnection()

        granularity_options(connection)

        self.assertEqual([], connection.executed)


class ThreeStateTrendTests(unittest.TestCase):
    """折线三态夹具（执行提示词 §3/§4.2②/§6-5）：ok / zero / missing。

    全部钉历史月 2026-08（31 天完整轴，与运行日无关）；dim_calendar
    行集即营业日集合（month_workdays 复用，不另写 SQL）。
    """

    def _payload(self, fact_rows, workdays=()):
        connection = FakeConnection(
            rowsets={
                "fact_daily_report_offline": fact_rows,
                "dim_calendar": [{"business_date": day} for day in workdays],
            }
        )
        payload = run_trend_region_daily(connection, {"month": "2026-08"})
        self.assertEqual(31, len(payload["dates"]))  # 完整月轴
        return payload

    def test_all_missing_day_breaks_the_line(self):
        # 全 missing 日：全日任何系列都无行 → data=None（断线位置）、
        # status=missing，绝不用 0 表示「没数据」。
        payload = self._payload(
            [{"business_date": date(2026, 8, 3), "region": "杭州",
              "total": Decimal("10")}]
        )

        index = payload["dates"].index("08-04")
        self.assertIsNone(payload["series"][0]["data"][index])
        self.assertEqual("missing", payload["status"][0][index])
        ok_index = payload["dates"].index("08-03")
        self.assertEqual(10.0, payload["series"][0]["data"][ok_index])
        self.assertEqual("ok", payload["status"][0][ok_index])

    def test_real_zero_on_a_workday_is_zero_status_not_missing(self):
        # 真实 0 日：营业日 + 有行 + total=0 → data=0.0（红点位置）、
        # status=zero；非营业日（日历未覆盖）的 0 → 0.0+ok 不标红。
        workday = date(2026, 8, 3)  # 周一
        payload = self._payload(
            [
                {"business_date": workday, "region": "杭州", "total": Decimal("0")},
                {"business_date": date(2026, 8, 1), "region": "杭州",
                 "total": Decimal("0")},  # 周六，不在 dim_calendar 行集内
            ],
            workdays=(workday,),
        )

        zero_index = payload["dates"].index("08-03")
        self.assertEqual(0.0, payload["series"][0]["data"][zero_index])
        self.assertEqual("zero", payload["status"][0][zero_index])
        weekend_index = payload["dates"].index("08-01")
        self.assertEqual(0.0, payload["series"][0]["data"][weekend_index])
        self.assertEqual("ok", payload["status"][0][weekend_index])

    def test_normal_day_is_ok_and_series_gap_is_not_a_zero(self):
        # 正常日：有行 total>0 → ok。本系列无行但当日其他系列有行 →
        # 0.0+missing（该区域未上报，既不是营业 0 也不是断线 None）。
        payload = self._payload(
            [
                {"business_date": date(2026, 8, 3), "region": "杭州",
                 "total": Decimal("10")},
                {"business_date": date(2026, 8, 3), "region": "绍兴",
                 "total": Decimal("5")},
                {"business_date": date(2026, 8, 4), "region": "杭州",
                 "total": Decimal("6")},
            ]
        )

        index = payload["dates"].index("08-04")
        hangzhou, shaoxing = payload["series"]
        self.assertEqual(6.0, hangzhou["data"][index])
        self.assertEqual("ok", payload["status"][0][index])
        self.assertEqual(0.0, shaoxing["data"][index])
        self.assertEqual("missing", payload["status"][1][index])
        # 轴完整：两系列与 dates 等长，as_of 一并下发。
        self.assertIn("as_of", payload)
        for entry in payload["series"]:
            self.assertEqual(31, len(entry["data"]))
        self.assertEqual([31, 31], [len(row) for row in payload["status"]])


class RunPayloadTests(unittest.TestCase):
    """The run_* wrappers produce the plan's chart-ready payload shapes."""

    def test_run_kpi_offline_mtd_payload(self):
        connection = _DodFakeConnection(
            scalars={"fact_daily_report_offline": Decimal("10.5")},
            latest={"fact_daily_report_offline": date(2026, 9, 16)},
        )

        payload = run_kpi_offline_mtd(connection, {})

        # B2：窗口截至真实水位，as_of 随载荷下发（前端标量卡角标）。
        self.assertEqual(
            {
                "chart": "scalar",
                "value": 10.5,
                "as_of": "2026-09-16",
                "unit": "元",
            },
            payload,
        )
        self.assertIsInstance(payload["value"], float)

    def test_run_kpi_offline_mtd_empty_table_payload(self):
        connection = _DodFakeConnection(
            latest={"fact_daily_report_offline": None}
        )

        payload = run_kpi_offline_mtd(connection, {})

        self.assertEqual(0.0, payload["value"])
        self.assertIsNone(payload["as_of"])

    # --- 粒度批次 A（2026-09-21 订正）：kpi_offline_mtd 日/周/月三档 --------
    # 定锚水位 2026-09-16（周三）；断言窗口边界与 as_of，而不是数值真假
    # （数值由 scalars 脚本喂，窗口对不对看 SQL 参数）。

    def _offline_gran_connection(self):
        return _DodFakeConnection(
            scalars={"fact_daily_report_offline": Decimal("3")},
            latest={"fact_daily_report_offline": date(2026, 9, 16)},
        )

    def test_run_kpi_offline_mtd_day_gran_is_the_anchor_day_value(self):
        connection = self._offline_gran_connection()

        payload = run_kpi_offline_mtd(connection, {"gran": "day"})

        self.assertEqual(3.0, payload["value"])
        self.assertEqual("2026-09-16", payload["as_of"])
        self.assertEqual(2, len(connection.executed))
        sum_sql, sum_params = connection.executed[1]
        self.assertIn("business_date BETWEEN %s AND %s", sum_sql)
        self.assertIn(_LINE_EXCLUSION, sum_sql)
        self.assertEqual(0, sum_sql.replace("%%", "").replace("%s", "").count("%"))
        self.assertEqual((date(2026, 9, 16), date(2026, 9, 16)), sum_params)

    def test_run_kpi_offline_mtd_week_gran_starts_on_monday(self):
        connection = self._offline_gran_connection()

        payload = run_kpi_offline_mtd(connection, {"gran": "week"})

        self.assertEqual(3.0, payload["value"])
        self.assertEqual("2026-09-16", payload["as_of"])
        # 2026-09-16 是周三 → 周一 2026-09-14。
        self.assertEqual(
            (date(2026, 9, 14), date(2026, 9, 16)),
            connection.executed[1][1],
        )

    def test_run_kpi_offline_mtd_month_gran_with_month_param(self):
        connection = self._offline_gran_connection()

        payload = run_kpi_offline_mtd(
            connection, {"gran": "month", "month": "2026-08"}
        )

        self.assertEqual(3.0, payload["value"])
        # 历史月锚点 = 月末（min(月末, 水位)）。
        self.assertEqual("2026-08-31", payload["as_of"])
        self.assertEqual(
            (date(2026, 8, 1), date(2026, 8, 31)),
            connection.executed[1][1],
        )

    def test_run_kpi_offline_mtd_month_beyond_watermark_is_zero(self):
        connection = self._offline_gran_connection()

        payload = run_kpi_offline_mtd(
            connection, {"gran": "month", "month": "2026-10"}
        )

        # 窗口月整体在水位之后：该月无入仓，0 且不假装有截止日。
        self.assertEqual(0.0, payload["value"])
        self.assertIsNone(payload["as_of"])
        # 只发定锚一条，窗口求和短路。
        self.assertEqual(1, len(connection.executed))
        self.assertIn("MAX(business_date)", connection.executed[0][0])

    def test_run_kpi_offline_mtd_year_gran_is_ytd_to_the_watermark(self):
        # A5 年档：年首锚点吃 MySQL 时钟（MAKEDATE），上界 = 绑定的
        # 真实水位——与「年度目标达成」的 CURDATE 上界刻意分开。
        connection = self._offline_gran_connection()

        payload = run_kpi_offline_mtd(connection, {"gran": "year"})

        self.assertEqual(3.0, payload["value"])
        self.assertEqual("2026-09-16", payload["as_of"])
        self.assertEqual(2, len(connection.executed))
        sum_sql, sum_params = connection.executed[1]
        self.assertIn("MAKEDATE(YEAR(CURDATE()), 1)", sum_sql)
        self.assertIn("business_date <= %s", sum_sql)
        self.assertIn(_LINE_EXCLUSION, sum_sql)
        self.assertEqual(0, sum_sql.replace("%%", "").replace("%s", "").count("%"))
        self.assertEqual((date(2026, 9, 16),), sum_params)

    def test_run_kpi_offline_mtd_year_gran_with_month_param_anchor(self):
        # year + month：锚点 = min(窗口月末, 水位)——年累计截到该月末。
        connection = self._offline_gran_connection()

        payload = run_kpi_offline_mtd(
            connection, {"gran": "year", "month": "2026-08"}
        )

        self.assertEqual(3.0, payload["value"])
        self.assertEqual("2026-08-31", payload["as_of"])
        self.assertEqual((date(2026, 8, 31),), connection.executed[1][1])

    def test_run_kpi_channel_mtd_payload(self):
        connection = _DodFakeConnection(
            scalars={"fact_channel_daily_sales": Decimal("7")},
            latest={"fact_channel_daily_sales": date(2026, 9, 15)},
        )

        payload = run_kpi_channel_mtd(connection, {})

        # 2026-09-21：窗口截至真实水位（最新有数日），as_of 随载荷下发。
        self.assertEqual(
            {
                "chart": "scalar",
                "value": 7.0,
                "as_of": "2026-09-15",
                "unit": "元",
            },
            payload,
        )
        self.assertIsInstance(payload["value"], float)

    def test_run_kpi_channel_mtd_empty_table_payload(self):
        connection = _DodFakeConnection(
            latest={"fact_channel_daily_sales": None}
        )

        payload = run_kpi_channel_mtd(connection, {})

        self.assertEqual(0.0, payload["value"])
        self.assertIsNone(payload["as_of"])

    def test_run_kpi_annual_progress_combines_all_lines(self):
        # 2026-09-21 口径：全业务线年累计 ÷ 全业务线目标，lines 逐线
        # 列出；餐饮线取人工月报（有数 → has_data=True）。
        connection = FakeConnection(
            scalars={
                "fact_daily_report_offline": Decimal("1000"),
                "fact_channel_daily_sales": Decimal("234"),
            },
            rowsets={
                "dim_target": [
                    {"scope_key": "channel", "target": Decimal("500")},
                    {"scope_key": "offline", "target": Decimal("300")},
                    {"scope_key": "restaurant", "target": Decimal("200")},
                ],
                "fact_manual_report": [
                    {"total": Decimal("66"), "rows_found": 3}
                ],
            },
        )

        payload = run_kpi_annual_progress(connection, {})

        self.assertEqual(
            {
                "chart": "scalar",
                "value": 1300.0,
                "target": 1000.0,
                "rate": 1.3,
                "lines": [
                    {"name": "线下", "value": 1000.0, "target": 300.0,
                     "has_data": True},
                    {"name": "电商渠道", "value": 234.0, "target": 500.0,
                     "has_data": True},
                    {"name": "餐饮", "value": 66.0, "target": 200.0,
                     "has_data": True},
                ],
                "unit": "元",
            },
            payload,
        )
        self.assertIsInstance(payload["value"], float)
        self.assertIsInstance(payload["target"], float)
        self.assertIsInstance(payload["rate"], float)

    def test_run_kpi_annual_progress_missing_line_is_zero_and_flagged(self):
        # 「找不到数据标红为 0」：餐饮月报空表 → 值 0、has_data=False；
        # 目标里出现注册表没有的线（showroom）同样 0 标红，绝不只在
        # 分母里加目标。
        connection = FakeConnection(
            scalars={
                "fact_daily_report_offline": Decimal("1000"),
                "fact_channel_daily_sales": Decimal("234"),
            },
            rowsets={
                "dim_target": [
                    {"scope_key": "channel", "target": Decimal("500")},
                    {"scope_key": "offline", "target": Decimal("300")},
                    {"scope_key": "restaurant", "target": Decimal("200")},
                    {"scope_key": "showroom", "target": Decimal("100")},
                ],
                "fact_manual_report": [],
            },
        )

        payload = run_kpi_annual_progress(connection, {})

        self.assertEqual(1234.0, payload["value"])
        self.assertEqual(1100.0, payload["target"])
        self.assertEqual(
            [
                {"name": "线下", "value": 1000.0, "target": 300.0,
                 "has_data": True},
                {"name": "电商渠道", "value": 234.0, "target": 500.0,
                 "has_data": True},
                {"name": "餐饮", "value": 0.0, "target": 200.0,
                 "has_data": False},
                {"name": "showroom", "value": 0.0, "target": 100.0,
                 "has_data": False},
            ],
            payload["lines"],
        )

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
        # 夹具钉历史月（2026-08）：轴=完整 31 天，与运行日无关（三态改造
        # 后轴不再由数据行反推，执行提示词 §4.2②③⑤）。
        rows = [
            {"business_date": date(2026, 8, 1), "region": "杭州", "total": Decimal("10")},
            {"business_date": date(2026, 8, 2), "region": "杭州", "total": Decimal("20")},
            {"business_date": date(2026, 8, 2), "region": "绍兴", "total": Decimal("5")},
        ]
        connection = FakeConnection(rowsets={"fact_daily_report_offline": rows})

        payload = run_trend_region_daily(connection, {"month": "2026-08"})

        self.assertEqual("line", payload["chart"])
        self.assertIn("as_of", payload)
        self.assertEqual(31, len(payload["dates"]))
        self.assertEqual("08-01", payload["dates"][0])
        self.assertEqual("08-31", payload["dates"][-1])
        hangzhou, shaoxing = payload["series"]
        self.assertEqual("杭州", hangzhou["name"])
        self.assertEqual("绍兴", shaoxing["name"])
        self.assertEqual([10.0, 20.0] + [None] * 29, hangzhou["data"])
        # 绍兴 08-01：本系列无行但当日杭州有行 → 0.0+missing（该区域未
        # 上报，不是零也不是断线）；08-03 起全日无任何行 → None+missing。
        self.assertEqual([0.0, 5.0] + [None] * 29, shaoxing["data"])
        self.assertEqual(
            [["ok", "ok"] + ["missing"] * 29,
             ["missing", "ok"] + ["missing"] * 29],
            payload["status"],
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

    def test_run_pie_sku_mtd_payload(self):
        rows = [
            {"spec_no": "SKU-1", "goods_name": "冻干草莓", "total": Decimal("300")},
            {"spec_no": "SKU-2", "goods_name": "黄桃罐头", "total": Decimal("100")},
        ]
        connection = FakeConnection(rowsets={"fact_order_line": rows})

        payload = run_pie_sku_mtd(connection, {})

        self.assertEqual(
            {
                "chart": "pie",
                "items": [
                    {"name": "冻干草莓", "value": 300.0},
                    {"name": "黄桃罐头", "value": 100.0},
                ],
                "unit": "元",
            },
            payload,
        )

    def test_run_pie_sku_mtd_merges_tail_into_others(self):
        rows = [
            {"spec_no": f"SKU-{index}", "goods_name": f"商品{index}",
             "total": Decimal(1000 - index)}
            for index in range(10)
        ]
        connection = FakeConnection(rowsets={"fact_order_line": rows})

        payload = run_pie_sku_mtd(connection, {})

        # 头部 8 片单列，第 9、10 名合并「其他」且排在最后。
        self.assertEqual(9, len(payload["items"]))
        self.assertEqual(
            [f"商品{index}" for index in range(8)] + ["其他"],
            [item["name"] for item in payload["items"]],
        )
        self.assertEqual(
            float(Decimal(1000 - 8) + Decimal(1000 - 9)),
            payload["items"][-1]["value"],
        )
        self.assertTrue(
            all(isinstance(item["value"], float) for item in payload["items"])
        )

    def test_run_pie_sku_mtd_without_tail_has_no_others_slice(self):
        rows = [
            {"spec_no": "SKU-1", "goods_name": "冻干草莓", "total": Decimal("300")},
        ]
        connection = FakeConnection(rowsets={"fact_order_line": rows})

        payload = run_pie_sku_mtd(connection, {})

        self.assertEqual(1, len(payload["items"]))
        self.assertNotIn("其他", [item["name"] for item in payload["items"]])


class MonthBoundsTests(unittest.TestCase):
    def test_month_bounds_expands_to_month_ends(self):
        self.assertEqual(
            (date(2026, 9, 1), date(2026, 9, 30)), month_bounds("2026-09")
        )

    def test_month_bounds_handles_february_leap_year(self):
        self.assertEqual(
            (date(2024, 2, 1), date(2024, 2, 29)), month_bounds("2024-02")
        )


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
        self.assertIn(_LINE_EXCLUSION, latest_sql)
        self.assertIn("FROM fact_daily_report_offline", window_sql)
        self.assertIn("BETWEEN %s AND %s", window_sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, window_sql)
        self.assertIn(_LINE_EXCLUSION, window_sql)
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
        # P0-3（2026-09-28）：DoD 水位必须过滤预填 NULL 行，与
        # _CHANNEL_LATEST_SQL 同口径——否则水位虚报到预填末日、整卡全空。
        self.assertIn("sales_amount IS NOT NULL", latest_sql)
        self.assertNotIn("合计", latest_sql)
        self.assertIn("FROM fact_channel_daily_sales", window_sql)
        self.assertIn("BETWEEN %s AND %s", window_sql)
        self.assertNotIn("合计", window_sql)
        self.assertIn("GROUP BY business_date", window_sql)


class ScopeHelperTests(unittest.TestCase):
    """保留参数与过滤片段的纯函数（无 IO）。"""

    def test_absent_key_means_row_level_filtering_is_off(self):
        self.assertIsNone(bi_web_queries.scope_from_params({}))

    def test_empty_fingerprint_is_the_empty_set_not_disabled(self):
        # 关键区分："" 必须读成 frozenset()（fail-closed），不能读成 None。
        self.assertEqual(
            frozenset(), bi_web_queries.scope_from_params({"_scope": ""})
        )

    def test_fingerprint_splits_keys_and_drops_blanks(self):
        self.assertEqual(
            frozenset({"hangzhou", "hq"}),
            bi_web_queries.scope_from_params({"_scope": "hangzhou,hq,"}),
        )

    def test_corrupt_shape_is_fail_closed(self):
        self.assertEqual(
            frozenset(), bi_web_queries.scope_from_params({"_scope": 123})
        )

    def test_scope_fingerprint_is_sorted_stable_and_none_passthrough(self):
        self.assertEqual(
            "hangzhou,shaoxing",
            bi_web_queries.scope_fingerprint(frozenset({"shaoxing", "hangzhou"})),
        )
        self.assertEqual(
            bi_web_queries.scope_fingerprint(frozenset({"hq", "hangzhou"})),
            bi_web_queries.scope_fingerprint(frozenset({"hangzhou", "hq"})),
        )
        self.assertEqual("", bi_web_queries.scope_fingerprint(frozenset()))
        self.assertIsNone(bi_web_queries.scope_fingerprint(None))

    def test_region_filter_legacy_shape_is_untouched(self):
        self.assertEqual(("", ()), bi_web_queries._scope_region_filter(None))

    def test_region_filter_empty_scope_is_a_tautological_false(self):
        self.assertEqual(
            ("AND 1 = 0 ", ()), bi_web_queries._scope_region_filter(frozenset())
        )

    def test_region_filter_parameterises_sorted_keys(self):
        fragment, params = bi_web_queries._scope_region_filter(
            frozenset({"shaoxing", "hangzhou"})
        )
        self.assertEqual("AND region IN (%s, %s) ", fragment)
        self.assertEqual(("hangzhou", "shaoxing"), params)

    def test_region_filter_combines_pinned_region_and_scope(self):
        fragment, params = bi_web_queries._scope_region_filter(
            frozenset({"hangzhou"}), "hangzhou"
        )
        self.assertEqual("AND region = %s AND region IN (%s) ", fragment)
        self.assertEqual(("hangzhou", "hangzhou"), params)

    def test_append_region_sql_is_identity_on_the_legacy_path(self):
        self.assertEqual(
            "SELECT 1", bi_web_queries._append_region_sql("SELECT 1", "")
        )
        self.assertIn(
            "AND region IN (%s)",
            bi_web_queries._append_region_sql("SELECT 1", "AND region IN (%s) "),
        )

    def test_scope_allows_regions_requires_every_region(self):
        self.assertTrue(
            bi_web_queries._scope_allows_regions(None, "hangzhou", "hq")
        )
        self.assertTrue(
            bi_web_queries._scope_allows_regions(
                frozenset({"hangzhou", "hq"}), "hangzhou", "hq"
            )
        )
        self.assertFalse(
            bi_web_queries._scope_allows_regions(
                frozenset({"hangzhou"}), "hangzhou", "hq"
            )
        )
        self.assertFalse(
            bi_web_queries._scope_allows_regions(frozenset(), "hangzhou")
        )

    def test_carry_scope_forwards_only_when_present(self):
        self.assertEqual(
            {"region": "hangzhou", "_scope": "hangzhou"},
            bi_web_queries._carry_scope({"_scope": "hangzhou"}, {"region": "hangzhou"}),
        )
        self.assertEqual(
            {"region": "hangzhou"},
            bi_web_queries._carry_scope({}, {"region": "hangzhou"}),
        )


class RegionScopeQueryTests(unittest.TestCase):
    """咽喉点：显式 region 与行级 scope 同时生效，旧路径逐字节不变。"""

    def _connection(self):
        return _RegionFakeConnection(
            scalars={
                "fact_daily_report_offline": Decimal("12"),
                "region_target": Decimal("40"),
            }
        )

    def test_no_scope_keeps_the_previous_statement_and_params(self):
        connection = self._connection()
        bi_web_queries.region_mtd_total(
            connection, region="杭州",
            first_day=date(2026, 9, 1), last_day=date(2026, 9, 30),
        )
        sql, params = connection.executed[0]
        self.assertIn("region = %s", sql)
        self.assertNotIn("region IN", sql)
        self.assertEqual((date(2026, 9, 1), date(2026, 9, 30), "杭州"), params)

    def test_scope_pushes_a_parameterised_in_list(self):
        connection = self._connection()
        bi_web_queries.region_mtd_total(
            connection, first_day=date(2026, 9, 1), last_day=date(2026, 9, 30),
            scope=frozenset({"shaoxing", "hangzhou"}),
        )
        sql, params = connection.executed[0]
        self.assertIn("region IN (%s, %s)", sql)
        self.assertEqual(
            (date(2026, 9, 1), date(2026, 9, 30), "hangzhou", "shaoxing"), params
        )

    def test_empty_scope_is_tautologically_false(self):
        connection = self._connection()
        bi_web_queries.region_mtd_total(
            connection, first_day=date(2026, 9, 1), last_day=date(2026, 9, 30),
            scope=frozenset(),
        )
        sql, params = connection.executed[0]
        self.assertIn("AND 1 = 0", sql)
        self.assertNotIn("region IN", sql)
        self.assertEqual((date(2026, 9, 1), date(2026, 9, 30)), params)

    def test_run_layer_reads_the_reserved_param(self):
        connection = self._connection()
        run_kpi_region_mtd(
            connection, {"month": "2026-09", "_scope": "shaoxing"}
        )
        sql, params = connection.executed[0]
        self.assertIn("region IN (%s)", sql)
        self.assertEqual(
            (date(2026, 9, 1), date(2026, 9, 30), "shaoxing"), params
        )

    def test_pinned_region_outside_the_scope_carries_both_predicates(self):
        # 钉死区域 + 不含它的 scope：两条谓词同时拼上（交集空，行数为 0）。
        connection = self._connection()
        run_kpi_region_mtd(
            connection,
            {"region": "hangzhou", "month": "2026-09", "_scope": "shaoxing"},
        )
        sql, params = connection.executed[0]
        self.assertIn("region = %s", sql)
        self.assertIn("region IN (%s)", sql)
        self.assertEqual(
            (date(2026, 9, 1), date(2026, 9, 30), "hangzhou", "shaoxing"), params
        )

    def test_offline_mtd_sum_static_path_is_untouched(self):
        connection = _DodFakeConnection(
            scalars={"fact_daily_report_offline": Decimal("7")},
            latest={"fact_daily_report_offline": date(2026, 9, 30)},
        )
        bi_web_queries._offline_mtd_sum(connection, date(2026, 9, 30))
        sql, params = connection.executed[0]
        self.assertEqual(bi_web_queries._OFFLINE_MTD_SQL, sql)
        self.assertEqual((date(2026, 9, 30),), params)

    def test_offline_mtd_sum_scopes_the_water_mark_and_the_sum(self):
        connection = _DodFakeConnection(
            scalars={"fact_daily_report_offline": Decimal("7")},
            latest={"fact_daily_report_offline": date(2026, 9, 30)},
        )
        run_kpi_offline_mtd(connection, {"_scope": "hangzhou"})
        scoped = [
            (sql, params) for sql, params in connection.executed
            if "region IN (%s)" in sql
        ]
        # 水位定锚与求和都收窄到 scope 内：两处都不得再读全域。
        self.assertEqual(2, len(scoped), connection.executed)
        for sql, params in scoped:
            self.assertEqual("hangzhou", params[-1])

    def test_region_daily_series_static_path_has_no_bound_parameters(self):
        connection = _DodFakeConnection(
            rowsets={"fact_daily_report_offline": []},
            latest={"fact_daily_report_offline": date(2026, 9, 30)},
        )
        bi_web_queries.region_daily_series(connection)
        for sql, params in connection.executed:
            if "GROUP BY business_date, region" in sql:
                self.assertIsNone(params)

    def test_region_daily_series_drops_out_of_scope_rows(self):
        # 轴锚在「当前月首 → 当前水位」，故夹具必须落在本月。
        today = date.today()
        first_day = today.replace(day=1)
        connection = _DodFakeConnection(
            rowsets={
                "fact_daily_report_offline": [
                    {"business_date": first_day, "region": "hangzhou",
                     "total": Decimal("10")},
                    {"business_date": first_day, "region": "shaoxing",
                     "total": Decimal("20")},
                ]
            },
            latest={"fact_daily_report_offline": today},
        )
        series = bi_web_queries.region_daily_series(
            connection, frozenset({"hangzhou"})
        )
        self.assertEqual(["hangzhou"], [one["name"] for one in series["series"]])
        self.assertEqual(10.0, series["series"][0]["data"][0])

    def test_exempt_card_sql_never_carries_a_region_predicate(self):
        # 渠道日环比卡的间接调用会经过 `_dod_totals`（已 scope 化，但缺省
        # 参数为 None）——运行时必须是"一条 region 白名单谓词也没有"。
        connection = _DodFakeConnection(
            rowsets={"fact_channel_daily_sales": []},
            latest={"fact_channel_daily_sales": date(2026, 9, 30)},
        )
        bi_web_queries.run_kpi_channel_dod(connection, {"_scope": "hangzhou"})
        self.assertTrue(connection.executed)
        for sql, _params in connection.executed:
            self.assertNotIn("region IN", sql)
            self.assertNotIn("AND 1 = 0", sql)

    def test_people_snapshot_gates_the_pinned_region(self):
        # mart_collect 在共享采集层，无法下推——不在 scope 内即空榜，
        # 且**一次查询都不发起**（这里顺便守住「不该取数就别取」）。
        connection = FakeConnection()
        people = bi_web_queries._people_snapshot(
            connection, "杭州", "2026-09", frozenset({"shaoxing"})
        )
        self.assertEqual([], people)
        self.assertEqual([], connection.executed)

    def test_people_snapshot_merged_view_intersects_the_scope(self):
        connection = FakeConnection()
        people = bi_web_queries._people_snapshot(
            connection, None, "2026-09", frozenset({"shaoxing"})
        )
        # 两榜位（中文「杭州」「绍兴」）与区域键词表的交集为空：空榜而非全域榜。
        self.assertEqual([], people)


class CardRowScopeContractTests(unittest.TestCase):
    """逐卡合约：声明了 ``row_scope="region"`` 的卡必须消费 scope。

    判定走 queries 调用图（函数源码里出现任一 scope 记号，或它转调的函数
    里有），零 IO——这样「新加一张区域卡却忘了接线」会在 CI 直接红，而不是
    等到某次越权才发现。
    """

    _MARKERS = (
        "scope_from_params",
        "_scope_region_filter",
        "_scope_allows_regions",
        "_carry_scope",
        "ROW_SCOPE_PARAM",
    )

    def _callees(self, func):
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        names = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "bi_web_queries"
            ):
                names.append(target.attr)
            elif isinstance(target, ast.Name):
                names.append(target.id)
        return names

    def _consumes(self, func, seen=None):
        seen = set() if seen is None else seen
        if func in seen:
            return False
        seen.add(func)
        try:
            source = inspect.getsource(func)
        except (OSError, TypeError):
            return False
        if any(marker in source for marker in self._MARKERS):
            return True
        for name in self._callees(func):
            nested = getattr(bi_web_queries, name, None)
            if inspect.isfunction(nested) and self._consumes(nested, seen):
                return True
        return False

    def test_declared_region_cards_consume_the_scope(self):
        missing = [
            card_id for card_id, card in bi_web_cards.REGISTRY.items()
            if card.row_scope == "region" and not self._consumes(card.run)
        ]
        self.assertEqual([], missing, f"row_scope=region 但未消费 scope: {missing}")

    def test_exempt_cards_never_wire_the_scope_directly(self):
        # 反向锁只看本卡自身源码（不递归）：豁免卡一旦被"顺手"接上 scope，
        # 说明豁免理由已过期——要么补声明、要么撤接线，不允许两不占。
        # 不递归是因为若干**共享**核（如 ``_dod_totals``）已被 scope 化，
        # 豁免卡以缺省 scope=None 调它并不等于接线（行为另由
        # ``test_exempt_card_sql_never_carries_a_region_predicate`` 守住）。
        leaked = [
            card_id for card_id, card in bi_web_cards.REGISTRY.items()
            if card.row_scope != "region"
            and any(
                marker in textwrap.dedent(inspect.getsource(card.run))
                for marker in self._MARKERS
            )
        ]
        self.assertEqual([], leaked, f"豁免卡却接了 scope: {leaked}")
