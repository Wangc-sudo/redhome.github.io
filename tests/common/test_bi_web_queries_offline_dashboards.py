"""test_bi_web_queries 的 offline_dashboards 域测试；断言自原文件等价迁移。"""

from tests.common.bi_web_query_test_support import *


class HzDashboardTests(unittest.TestCase):
    """杭州线下销售BI看板（hz-offline-sales，2026-10-09 裁决）：

    区域钉死 hangzhou（fact 表 region 列真值为英文 key，2026-10-09 云上
    DISTINCT 实测）；全卡 date 锚定；达成率真源 = mart_collect。
    """

    def _calendar_rows(self):
        return [
            {"business_date": date(2026, 8, 3)},
            {"business_date": date(2026, 8, 4)},
            {"business_date": date(2026, 8, 5)},
        ]

    def _people_connection(self):
        facts = [
            {
                "region": "hangzhou",
                "responsible_person": "张三",
                "department": "零售一组",
                "business_date": date(2026, 8, 3),
                "sales_amount": Decimal("30"),
                "monthly_target": Decimal("100"),
            },
            {
                "region": "hangzhou",
                "responsible_person": "张三",
                "department": "零售一组",
                "business_date": date(2026, 8, 4),
                "sales_amount": Decimal("40"),
                "monthly_target": Decimal("100"),
            },
            {
                "region": "hangzhou",
                "responsible_person": "李四",
                "department": None,
                "business_date": date(2026, 8, 3),
                "sales_amount": Decimal("10"),
                "monthly_target": None,
            },
        ]
        return _PeopleFakeConnection(
            rowsets={
                "dim_calendar": self._calendar_rows(),
                "fact_daily_report_offline": facts,
            }
        )

    def test_workday_options_maps_dates_to_iso_strings(self):
        connection = FakeConnection(
            rowsets={
                "dim_calendar": [
                    {"business_date": date(2026, 8, 3)},
                    {"business_date": None},
                    {"business_date": date(2026, 8, 4)},
                ]
            }
        )
        self.assertEqual(
            ["2026-08-03", "2026-08-04"],
            bi_web_queries.workday_options(connection),
        )
        sql = connection.executed[-1][0]
        self.assertIn("is_workday", sql)
        self.assertIn("CURDATE()", sql)  # 未来日截断

    def test_kpi_hz_today_pins_region_and_excludes_summary(self):
        connection = FakeConnection(
            rowsets={"fact_daily_report_offline": [{"s": Decimal("123")}]}
        )
        payload = bi_web_queries.run_kpi_hz_today(
            connection, {"date": "2026-08-04"})
        self.assertEqual(123.0, payload["value"])
        self.assertEqual("2026-08-04", payload["date"])
        sql, params = connection.executed[-1]
        self.assertIn("'hangzhou'", sql)          # 区域钉死进 SQL（无注入面）
        self.assertIn("%%合计%%", sql)
        self.assertEqual((date(2026, 8, 4),), params)

    def test_kpi_hz_progress_counts_elapsed_with_anchor_day(self):
        connection = FakeConnection(
            rowsets={"dim_calendar": self._calendar_rows()}
        )
        payload = bi_web_queries.run_kpi_hz_progress(
            connection, {"date": "2026-08-04"})
        # 含锚点当天（BI 实时口径）：elapsed={3,4}，total=3
        self.assertEqual(2.0, payload["value"])
        self.assertEqual(3.0, payload["target"])
        self.assertAlmostEqual(2 / 3, payload["rate"])
        self.assertEqual("个工作日", payload["unit"])

    def test_kpi_hz_mtd_derives_month_from_date_anchor(self):
        connection = _RegionFakeConnection(
            scalars={
                "fact_daily_report_offline": Decimal("80"),
                "region_target": Decimal("100"),
            }
        )
        payload = bi_web_queries.run_kpi_hz_mtd(
            connection, {"date": "2026-08-04"})
        self.assertEqual(80.0, payload["value"])
        self.assertEqual(100.0, payload["target"])
        self.assertEqual(0.8, payload["rate"])
        # 区域钉 hangzhou、月窗口来自锚点日（8-01 起）
        bound = [p for _, params in connection.executed for p in (params or ())]
        self.assertIn("hangzhou", bound)
        self.assertIn(date(2026, 8, 1), bound)

    def test_table_hz_people_progress_full_columns(self):
        payload = bi_web_queries.run_table_hz_people_progress(
            self._people_connection(), {"date": "2026-08-04"})
        self.assertEqual("2026-08-04", payload["date"])
        keys = [column["key"] for column in payload["columns"]]
        self.assertEqual(
            ["rank", "name", "dept", "completed", "target", "rate",
             "diff", "daily", "proj", "unfilled"],
            keys,
        )
        first, second = payload["rows"]
        # 张三：completed=70、rate=0.7、progress=2/3 → diff≈0.033、
        # daily=35、proj=1.05、unfilled=0
        self.assertEqual("张三", first["name"])
        self.assertEqual(70.0, first["completed"])
        self.assertEqual(0.7, first["rate"])
        self.assertAlmostEqual(0.7 - 2 / 3, first["diff"])
        self.assertEqual(35.0, first["daily"])
        self.assertAlmostEqual(1.05, first["proj"])
        self.assertEqual(0, first["unfilled"])
        # 李四：无目标 → rate/diff/proj None，未填 1 天（8-04 缺行）
        self.assertEqual("李四", second["name"])
        self.assertIsNone(second["rate"])
        self.assertIsNone(second["diff"])
        self.assertIsNone(second["proj"])
        self.assertEqual(1, second["unfilled"])

    def test_table_hz_people_progress_degrades_without_calendar(self):
        connection = _PeopleFakeConnection(
            rowsets={"dim_calendar": [], "fact_daily_report_offline": []}
        )
        payload = bi_web_queries.run_table_hz_people_progress(
            connection, {"date": "2026-08-04"})
        self.assertEqual([], payload["rows"])

    def test_table_hz_dept_progress_groups_by_dept(self):
        """部门进度表（2026-10-17 部门主视角）：按部门聚合，无目标沉底。"""
        payload = bi_web_queries.run_table_hz_dept_progress(
            self._people_connection(), {"date": "2026-08-04"})
        keys = [column["key"] for column in payload["columns"]]
        self.assertEqual(
            ["rank", "dept", "count", "completed", "target", "rate", "diff"],
            keys)
        rows = payload["rows"]
        self.assertEqual(2, len(rows))
        # 零售一组：张三 30+40=70 / 目标 100 → rate 0.7，进度差 0.7-2/3
        first = rows[0]
        self.assertEqual("零售一组", first["dept"])
        self.assertEqual(1, first["count"])
        self.assertEqual(70.0, first["completed"])
        self.assertEqual(100.0, first["target"])
        self.assertAlmostEqual(0.7, first["rate"])
        self.assertAlmostEqual(0.7 - 2 / 3, first["diff"])
        # 李四无部门/无目标 → 未分组沉底、rate None
        second = rows[1]
        self.assertEqual("未分组", second["dept"])
        self.assertIsNone(second["rate"])
        self.assertIsNone(second["diff"])

    def test_table_sx_dept_progress_pins_shaoxing(self):
        """绍兴部门进度表：区域钉 shaoxing。"""
        facts = [
            {"region": "shaoxing", "responsible_person": "王五",
             "department": "绍兴一组", "business_date": date(2026, 8, 3),
             "sales_amount": Decimal("90"), "monthly_target": Decimal("100")},
        ]
        connection = _PeopleFakeConnection(
            rowsets={
                "dim_calendar": self._calendar_rows(),
                "fact_daily_report_offline": facts,
            }
        )
        payload = bi_web_queries.run_table_sx_dept_progress(
            connection, {"date": "2026-08-04"})
        self.assertEqual(1, len(payload["rows"]))
        self.assertEqual("绍兴一组", payload["rows"][0]["dept"])
        self.assertAlmostEqual(0.9, payload["rows"][0]["rate"])
        # SQL 钉 shaoxing
        bound = [p for _, params in connection.executed for p in (params or ())]
        self.assertIn("shaoxing", bound)

    def test_anchor_falls_back_to_today_on_bad_date(self):
        anchor = bi_web_queries._hz_anchor({"date": "not-a-date"})
        self.assertEqual(datetime.now().date(), anchor)
        anchor = bi_web_queries._hz_anchor({})
        self.assertEqual(datetime.now().date(), anchor)


class HzUnfilledTodayTests(unittest.TestCase):
    """当日未填报人卡（2026-10-10 运维反馈）：与催办同真源同语义。"""

    def _connection(self, roster_rows):
        return _UnfilledFakeConnection(
            rowsets={
                "dim_robot_member": [
                    {"user_id": "u1", "name": "张三", "region": "hangzhou",
                     "dept_id": "d1", "dept_name": "零售一组",
                     "is_active": 1},
                    {"user_id": "u2", "name": "李四", "region": "hangzhou",
                     "dept_id": "d2", "dept_name": None,
                     "is_active": 1},
                    {"user_id": "u3", "name": "王五", "region": "hangzhou",
                     "dept_id": "d3", "dept_name": "零售二组",
                     "is_active": 0},  # 离职不计
                ],
                "dim_report_roster": roster_rows,
                "fact_daily_report_offline": [
                    {"region": "hangzhou", "responsible_person": "张三",
                     "business_date": date(2026, 10, 9)},
                ],
            }
        )

    def test_roster_bound_unfilled(self):
        connection = self._connection(
            roster_rows=[
                {"scope": "hangzhou", "entity_type": "person",
                 "person_name": "张三", "enabled": 1},
                {"scope": "hangzhou", "entity_type": "person",
                 "person_name": "李四", "enabled": 1},
            ]
        )
        payload = bi_web_queries.run_table_hz_unfilled_today(
            connection, {"date": "2026-10-09"})

        self.assertEqual("2026-10-09", payload["date"])
        # 张三已填、王五离职 → 仅李四未填（名册绑定口径）
        self.assertEqual(
            [{"rank": 1, "name": "李四", "dept": "未分组"}],
            payload["rows"],
        )

    def test_fail_open_when_roster_empty(self):
        payload = bi_web_queries.run_table_hz_unfilled_today(
            self._connection(roster_rows=[]), {"date": "2026-10-09"})

        # 名册无记录 fail-open = 全体在册成员口径（同催办）
        self.assertEqual(["李四"], [row["name"] for row in payload["rows"]])


class OfflineAllDashboardTests(unittest.TestCase):
    """线下整体销售BI看板（offline-all-sales，2026-10-10 裁决）：

    汇总卡 = 线下线全域（_OFFLINE_LINE_SQL）；人员/部门/板块卡真源 =
    collect_offline_all_people / AGG_SCOPES（与播报/静态页同函数）；
    全卡 date 锚定、BI 实时口径含当天。
    """

    def _calendar_rows(self):
        return [
            {"business_date": date(2026, 8, 3)},
            {"business_date": date(2026, 8, 4)},
            {"business_date": date(2026, 8, 5)},
        ]

    def _three_region_rows(self):
        return [
            # 杭州 张三（8-03=30、8-04=40，目标 100 → rate 0.7）
            {"region": "hangzhou", "responsible_person": "张三",
             "department": "杭中", "business_date": date(2026, 8, 3),
             "sales_amount": Decimal("30"), "monthly_target": Decimal("100")},
            {"region": "hangzhou", "responsible_person": "张三",
             "department": "杭中", "business_date": date(2026, 8, 4),
             "sales_amount": Decimal("40"), "monthly_target": Decimal("100")},
            # 绍兴 王五（8-03=90，目标 100 → rate 0.9）
            {"region": "shaoxing", "responsible_person": "王五",
             "department": "绍兴一组", "business_date": date(2026, 8, 3),
             "sales_amount": Decimal("90"), "monthly_target": Decimal("100")},
            # 省外板块（offline_extra：person=板块、department=责任人）
            {"region": "offline_extra", "responsible_person": "省外",
             "department": "余云涛", "business_date": date(2026, 8, 3),
             "sales_amount": Decimal("150"), "monthly_target": Decimal("100")},
            # 合计行（人员卡须排除）
            {"region": "hangzhou", "responsible_person": "杭州合计",
             "department": None, "business_date": date(2026, 8, 3),
             "sales_amount": Decimal("999"), "monthly_target": None},
        ]

    def _people_connection(self):
        return _OaFakeConnection(
            rowsets={
                "dim_calendar": self._calendar_rows(),
                "fact_daily_report_offline": self._three_region_rows(),
            }
        )

    def test_kpi_oa_mtd_offline_line_wide_without_region_param(self):
        connection = _RegionFakeConnection(
            scalars={
                "fact_daily_report_offline": Decimal("80"),
                "region_target": Decimal("100"),
            }
        )
        payload = bi_web_queries.run_kpi_oa_mtd(
            connection, {"date": "2026-08-04"})
        self.assertEqual(80.0, payload["value"])
        self.assertEqual(100.0, payload["target"])
        self.assertEqual(0.8, payload["rate"])
        # 线下线全域：无 region 绑定参数，月窗口来自锚点日
        sql, params = connection.executed[0]
        self.assertNotIn("region = %s", sql)
        self.assertEqual((date(2026, 8, 1), date(2026, 8, 31)), params)

    def test_kpi_oa_today_excludes_online_and_summary(self):
        connection = FakeConnection(
            rowsets={"fact_daily_report_offline": [{"s": Decimal("123")}]}
        )
        payload = bi_web_queries.run_kpi_oa_today(
            connection, {"date": "2026-08-04"})
        self.assertEqual(123.0, payload["value"])
        self.assertEqual("2026-08-04", payload["date"])
        sql, params = connection.executed[-1]
        self.assertIn("%%合计%%", sql)
        self.assertIn("qudao", sql)  # 线上排除名单（_OFFLINE_LINE_SQL）
        self.assertIn("vanke", sql)  # vanke 按 2026-10-10 裁决不计入
        self.assertEqual((date(2026, 8, 4),), params)

    def test_kpi_oa_progress_company_calendar(self):
        connection = FakeConnection(
            rowsets={"dim_calendar": self._calendar_rows()}
        )
        payload = bi_web_queries.run_kpi_oa_progress(
            connection, {"date": "2026-08-04"})
        self.assertEqual(2.0, payload["value"])
        self.assertEqual(3.0, payload["target"])
        self.assertAlmostEqual(2 / 3, payload["rate"])

    def test_trend_oa_daily_delegates_offline_line_wide(self):
        from unittest import mock

        connection = FakeConnection()
        with mock.patch.object(
            bi_web_offline_dashboards, "run_trend_region_daily",
            return_value={"chart": "line"},
        ) as trend:
            payload = bi_web_queries.run_trend_oa_daily(
                connection, {"date": "2026-08-04"})
        self.assertEqual({"chart": "line"}, payload)
        # region=None（线下线全域分区域多系列）、月=锚点月、gran 缺省日
        trend.assert_called_once_with(
            connection, {"month": "2026-08", "gran": "day"}
        )

    def test_table_oa_dept_progress_excludes_qudao(self):
        """回归（2026-10-17）：渠道日报行（region=qudao，10 月 2194 万
        实锤）不得漏进「其他」桶/部门进度表。"""
        rows = self._three_region_rows() + [
            {"region": "qudao", "responsible_person": "窦超",
             "department": "拼多多", "business_date": date(2026, 8, 3),
             "sales_amount": Decimal("31393"), "monthly_target": Decimal("50000")},
        ]
        connection = _OaFakeConnection(
            rowsets={
                "dim_calendar": self._calendar_rows(),
                "fact_daily_report_offline": rows,
            }
        )
        payload = bi_web_queries.run_table_oa_dept_progress(
            connection, {"date": "2026-08-04"})
        depts = [row["dept"] for row in payload["rows"]]
        self.assertEqual(["省外", "绍兴一组", "杭中"], depts)
        self.assertNotIn("拼多多", depts)

    def test_table_oa_dept_progress_aggregates_by_realigned_dept(self):
        """线下整体部门进度表（2026-10-17 部门主视角）：归位后部门聚合。"""
        payload = bi_web_queries.run_table_oa_dept_progress(
            self._people_connection(), {"date": "2026-08-04"})
        keys = [column["key"] for column in payload["columns"]]
        self.assertEqual(
            ["rank", "dept", "count", "completed", "target", "rate", "diff"],
            keys)
        rows = payload["rows"]
        # 省外 150/100=1.5 > 绍兴一组 0.9 > 杭中 0.7（合计行不进榜）
        self.assertEqual(["省外", "绍兴一组", "杭中"],
                         [row["dept"] for row in rows])
        self.assertEqual([1, 1, 1], [row["count"] for row in rows])
        self.assertEqual(150.0, rows[0]["completed"])
        self.assertEqual(100.0, rows[0]["target"])
        self.assertAlmostEqual(1.5, rows[0]["rate"])
        self.assertEqual("2026-08-04", payload["date"])

    def test_bar_oa_dept_groups_by_realigned_dept(self):
        payload = bi_web_queries.run_bar_oa_dept(
            self._people_connection(), {"date": "2026-08-04"})
        # 部门已归位（省外=板块归位、杭/绍=表内值兜底），Σ完成额 降序；
        # 合计行不进榜
        self.assertEqual(["省外", "绍兴一组", "杭中"], payload["categories"])
        self.assertEqual([150.0, 90.0, 70.0], payload["values"])
        self.assertEqual("2026-08-04", payload["date"])

    def test_table_oa_people_progress_merges_three_regions(self):
        payload = bi_web_queries.run_table_oa_people_progress(
            self._people_connection(), {"date": "2026-08-04"})
        self.assertEqual("2026-08-04", payload["date"])
        keys = [column["key"] for column in payload["columns"]]
        self.assertEqual(
            ["rank", "name", "dept", "completed", "target", "rate",
             "diff", "daily", "proj", "unfilled"],
            keys,
        )
        # 三区合并重排（rate 降序）：余云涛 1.5 > 王五 0.9 > 张三 0.7
        self.assertEqual(
            ["余云涛", "王五", "张三"],
            [row["name"] for row in payload["rows"]],
        )
        first = payload["rows"][0]
        # offline_extra 姓名↔部门互换：责任人余云涛为名、板块省外为部门
        self.assertEqual("省外", first["dept"])
        self.assertEqual(150.0, first["completed"])
        self.assertEqual(1.5, first["rate"])
        third = payload["rows"][2]
        # 张三：progress=2/3 → diff≈0.033、daily=35、proj=1.05、未填 0
        self.assertAlmostEqual(0.7 - 2 / 3, third["diff"])
        self.assertEqual(35.0, third["daily"])
        self.assertAlmostEqual(1.05, third["proj"])
        self.assertEqual(0, third["unfilled"])

    def test_table_oa_people_progress_degrades_without_calendar(self):
        connection = _OaFakeConnection(
            rowsets={"dim_calendar": [], "fact_daily_report_offline": []}
        )
        payload = bi_web_queries.run_table_oa_people_progress(
            connection, {"date": "2026-08-04"})
        self.assertEqual([], payload["rows"])

    def test_table_oa_scopes_overview_rows(self):
        connection = _OaFakeConnection(
            rowsets={
                "fact_daily_report_offline": [
                    {"region": "hangzhou", "responsible_person": "张三",
                     "department": "杭中", "business_date": date(2026, 8, 3),
                     "sales_amount": Decimal("30"),
                     "monthly_target": Decimal("100")},
                    {"region": "hangzhou", "responsible_person": "张三",
                     "department": "杭中", "business_date": date(2026, 8, 4),
                     "sales_amount": Decimal("40"),
                     "monthly_target": Decimal("100")},
                ],
            }
        )
        payload = bi_web_queries.run_table_oa_scopes(
            connection, {"date": "2026-08-04"})
        self.assertEqual("2026-08-04", payload["date"])
        self.assertEqual("2026-08-04", payload["report_day"])
        keys = [column["key"] for column in payload["columns"]]
        # 2026-10-10 排版裁决：期组内「累计 → 基期 → 环比」，组间分隔线
        self.assertEqual(
            ["scope", "day", "day_base", "dod",
             "week", "week_base", "wow",
             "completed", "month_base", "mom",
             "target", "rate", "prev_target", "prev_rate"],
            keys,
        )
        by_key = {column["key"]: column for column in payload["columns"]}
        # 分隔线落在每组首列（本周累计/月累计/月目标）
        self.assertEqual(
            ["week", "completed", "target"],
            [key for key in keys if by_key[key].get("cls") == "col-sep"],
        )
        # 3 桶 + 线下整体（2026-10-16 三桶口径：其他=省外/总经办补集）
        self.assertEqual(
            ["杭州", "绍兴", "其他", "线下整体"],
            [row["scope"] for row in payload["rows"]],
        )
        hangzhou = payload["rows"][0]
        self.assertEqual(40.0, hangzhou["day"])           # 报告日 8-04
        self.assertEqual(30.0, hangzhou["day_base"])      # 昨日基期可见
        self.assertAlmostEqual(40 / 30 - 1, hangzhou["dod"])
        self.assertEqual(70.0, hangzhou["week"])          # 本周一 8-03 起
        self.assertEqual(0.0, hangzhou["week_base"])      # 基期仍可见
        self.assertIsNone(hangzhou["wow"])                # 上周基期 0 → 比率 --
        self.assertEqual(70.0, hangzhou["completed"])
        self.assertEqual(0.0, hangzhou["month_base"])
        self.assertIsNone(hangzhou["mom"])                # 上月基期 0
        self.assertEqual(100.0, hangzhou["target"])
        self.assertEqual(0.7, hangzhou["rate"])
        # 上月目标/上月达成率 = 上月同期累计 ÷ 上月目标（同构口径）
        self.assertEqual(100.0, hangzhou["prev_target"])
        self.assertEqual(0.0, hangzhou["prev_rate"])
        shaoxing = payload["rows"][1]
        self.assertEqual(0.0, shaoxing["completed"])
        self.assertIsNone(shaoxing["target"])
        self.assertIsNone(shaoxing["rate"])
        self.assertIsNone(shaoxing["prev_target"])
        self.assertIsNone(shaoxing["prev_rate"])
        total = payload["rows"][-1]
        # 其余板块全零 → 线下整体 = 杭州
        self.assertEqual(70.0, total["completed"])
        self.assertEqual(100.0, total["target"])
        self.assertEqual(0.7, total["rate"])
        self.assertEqual(100.0, total["prev_target"])
        self.assertEqual(0.0, total["prev_rate"])

    def test_table_oa_scopes_empty_month_degrades(self):
        connection = _OaFakeConnection(
            rowsets={"fact_daily_report_offline": []}
        )
        payload = bi_web_queries.run_table_oa_scopes(
            connection, {"date": "2026-08-04"})
        self.assertEqual([], payload["rows"])


class OaAggregateScopeKeyTests(unittest.TestCase):
    """B5（2026-10-15 用户裁决）：``offline_all`` = oa 聚合授权键。

    oa 六卡消费 scope 时，``offline_all`` 展开为
    ``_OA_MERGED_REGIONS``（hangzhou/shaoxing/offline_extra）并集；
    其余区域授权语义不变；scope=None 路径逐字节不变。
    """

    def test_effective_scope_expands_offline_all(self):
        self.assertEqual(
            frozenset({"hangzhou", "shaoxing", "offline_extra"}),
            bi_web_queries._oa_effective_scope(frozenset({"offline_all"})),
        )

    def test_effective_scope_keeps_other_keys(self):
        self.assertEqual(
            frozenset({"hangzhou", "shaoxing", "offline_extra", "qudao"}),
            bi_web_queries._oa_effective_scope(
                frozenset({"offline_all", "qudao"})
            ),
        )

    def test_effective_scope_passes_through_without_offline_all(self):
        self.assertIsNone(bi_web_queries._oa_effective_scope(None))
        scope = frozenset({"hangzhou"})
        self.assertEqual(scope, bi_web_queries._oa_effective_scope(scope))

    def test_kpi_oa_today_offline_all_expands_to_three_regions(self):
        connection = FakeConnection(
            rowsets={"fact_daily_report_offline": [{"s": Decimal("123")}]}
        )
        payload = bi_web_queries.run_kpi_oa_today(
            connection, {"date": "2026-08-04", "_scope": "offline_all"})
        self.assertEqual(123.0, payload["value"])
        sql, params = connection.executed[-1]
        self.assertIn("region IN (%s, %s, %s)", sql)
        self.assertNotIn("offline_all", params)
        self.assertEqual(
            (date(2026, 8, 4), "hangzhou", "offline_extra", "shaoxing"),
            params,
        )

    def test_kpi_oa_mtd_offline_all_expands_to_three_regions(self):
        connection = _RegionFakeConnection(
            scalars={
                "fact_daily_report_offline": Decimal("80"),
                "region_target": Decimal("100"),
            }
        )
        bi_web_queries.run_kpi_oa_mtd(
            connection, {"date": "2026-08-04", "_scope": "offline_all"})
        sql, params = connection.executed[0]
        self.assertIn("region IN (%s, %s, %s)", sql)
        self.assertEqual(
            (date(2026, 8, 1), date(2026, 8, 31),
             "hangzhou", "offline_extra", "shaoxing"),
            params,
        )

    def test_trend_oa_daily_offline_all_expands_the_fingerprint(self):
        from unittest import mock

        connection = FakeConnection()
        with mock.patch.object(
            bi_web_offline_dashboards, "run_trend_region_daily",
            return_value={"chart": "line"},
        ) as trend:
            bi_web_queries.run_trend_oa_daily(
                connection, {"date": "2026-08-04", "_scope": "offline_all"})
        trend.assert_called_once_with(
            connection,
            {"month": "2026-08", "gran": "day",
             "_scope": "hangzhou,offline_extra,shaoxing"},
        )

    def test_oa_people_offline_all_passes_the_merged_gate(self):
        connection = _OaFakeConnection(
            rowsets={
                "dim_calendar": [
                    {"business_date": date(2026, 8, 3)},
                    {"business_date": date(2026, 8, 4)},
                ],
                "fact_daily_report_offline": [
                    {"region": "hangzhou", "responsible_person": "张三",
                     "department": "杭中", "business_date": date(2026, 8, 3),
                     "sales_amount": Decimal("30"),
                     "monthly_target": Decimal("100")},
                ],
            }
        )
        payload = bi_web_queries.run_table_oa_people_progress(
            connection, {"date": "2026-08-04", "_scope": "offline_all"})
        # 聚合授权键视同三区全授：合并榜非空（不再是整表空）。
        self.assertEqual(["张三"], [row["name"] for row in payload["rows"]])

    def test_oa_people_partial_scope_still_closes_the_merged_gate(self):
        # 回归：仅 hangzhou（三区不全）仍整表空——聚合键不影响原门径。
        connection = _OaFakeConnection(
            rowsets={
                "dim_calendar": [
                    {"business_date": date(2026, 8, 3)},
                    {"business_date": date(2026, 8, 4)},
                ],
                "fact_daily_report_offline": [
                    {"region": "hangzhou", "responsible_person": "张三",
                     "department": "杭中", "business_date": date(2026, 8, 3),
                     "sales_amount": Decimal("30"),
                     "monthly_target": Decimal("100")},
                ],
            }
        )
        payload = bi_web_queries.run_table_oa_people_progress(
            connection, {"date": "2026-08-04", "_scope": "hangzhou"})
        self.assertEqual([], payload["rows"])

    def test_table_oa_scopes_offline_all_passes_the_gate(self):
        connection = _OaFakeConnection(
            rowsets={
                "fact_daily_report_offline": [
                    {"region": "hangzhou", "responsible_person": "张三",
                     "department": "杭中", "business_date": date(2026, 8, 3),
                     "sales_amount": Decimal("30"),
                     "monthly_target": Decimal("100")},
                ],
            }
        )
        payload = bi_web_queries.run_table_oa_scopes(
            connection, {"date": "2026-08-04", "_scope": "offline_all"})
        self.assertEqual(
            ["杭州", "绍兴", "其他", "线下整体"],
            [row["scope"] for row in payload["rows"]],
        )


class SxDashboardTests(unittest.TestCase):
    """绍兴线下销售BI看板（sx-offline-sales，2026-10-10 裁决）：

    与杭州看板同构（区域钉死 shaoxing）；全卡 date 锚定；表格载荷与
    杭州共用 ``_people_progress_payload`` / ``_unfilled_payload``。
    """

    def _calendar_rows(self):
        return [
            {"business_date": date(2026, 8, 3)},
            {"business_date": date(2026, 8, 4)},
            {"business_date": date(2026, 8, 5)},
        ]

    def _people_connection(self):
        facts = [
            {"region": "shaoxing", "responsible_person": "王五",
             "department": "绍兴一组", "business_date": date(2026, 8, 3),
             "sales_amount": Decimal("90"), "monthly_target": Decimal("100")},
            {"region": "shaoxing", "responsible_person": "王五",
             "department": "绍兴一组", "business_date": date(2026, 8, 4),
             "sales_amount": Decimal("10"), "monthly_target": Decimal("100")},
            {"region": "shaoxing", "responsible_person": "合计",
             "department": None, "business_date": date(2026, 8, 3),
             "sales_amount": Decimal("999"), "monthly_target": None},
        ]
        return _PeopleFakeConnection(
            rowsets={
                "dim_calendar": self._calendar_rows(),
                "fact_daily_report_offline": facts,
            }
        )

    def test_kpi_sx_mtd_pins_shaoxing(self):
        connection = _RegionFakeConnection(
            scalars={
                "fact_daily_report_offline": Decimal("80"),
                "region_target": Decimal("100"),
            }
        )
        payload = bi_web_queries.run_kpi_sx_mtd(
            connection, {"date": "2026-08-04"})
        self.assertEqual(80.0, payload["value"])
        self.assertEqual(100.0, payload["target"])
        self.assertEqual(0.8, payload["rate"])
        bound = [p for _, params in connection.executed for p in (params or ())]
        self.assertIn("shaoxing", bound)
        self.assertIn(date(2026, 8, 1), bound)

    def test_kpi_sx_today_pins_region_and_excludes_summary(self):
        connection = FakeConnection(
            rowsets={"fact_daily_report_offline": [{"s": Decimal("123")}]}
        )
        payload = bi_web_queries.run_kpi_sx_today(
            connection, {"date": "2026-08-04"})
        self.assertEqual(123.0, payload["value"])
        self.assertEqual("2026-08-04", payload["date"])
        sql, params = connection.executed[-1]
        self.assertIn("'shaoxing'", sql)
        self.assertIn("%%合计%%", sql)
        self.assertEqual((date(2026, 8, 4),), params)

    def test_kpi_sx_progress_company_calendar(self):
        connection = FakeConnection(
            rowsets={"dim_calendar": self._calendar_rows()}
        )
        payload = bi_web_queries.run_kpi_sx_progress(
            connection, {"date": "2026-08-04"})
        self.assertEqual(2.0, payload["value"])
        self.assertEqual(3.0, payload["target"])
        self.assertAlmostEqual(2 / 3, payload["rate"])

    def test_trend_sx_daily_pins_shaoxing(self):
        from unittest import mock

        connection = FakeConnection()
        with mock.patch.object(
            bi_web_offline_dashboards, "run_trend_region_daily",
            return_value={"chart": "line"},
        ) as trend:
            payload = bi_web_queries.run_trend_sx_daily(
                connection, {"date": "2026-08-04"})
        self.assertEqual({"chart": "line"}, payload)
        trend.assert_called_once_with(
            connection,
            {"region": "shaoxing", "month": "2026-08", "gran": "day"},
        )

    def test_table_sx_people_progress_full_columns(self):
        payload = bi_web_queries.run_table_sx_people_progress(
            self._people_connection(), {"date": "2026-08-04"})
        self.assertEqual("2026-08-04", payload["date"])
        keys = [column["key"] for column in payload["columns"]]
        self.assertEqual(
            ["rank", "name", "dept", "completed", "target", "rate",
             "diff", "daily", "proj", "unfilled"],
            keys,
        )
        # 合计行排除，仅王五：completed=100、rate=1.0、progress=2/3
        self.assertEqual(1, len(payload["rows"]))
        row = payload["rows"][0]
        self.assertEqual("王五", row["name"])
        self.assertEqual("绍兴一组", row["dept"])
        self.assertEqual(100.0, row["completed"])
        self.assertEqual(1.0, row["rate"])
        self.assertAlmostEqual(1.0 - 2 / 3, row["diff"])
        self.assertEqual(50.0, row["daily"])
        self.assertAlmostEqual(1.5, row["proj"])
        self.assertEqual(0, row["unfilled"])

    def test_table_sx_people_progress_degrades_without_calendar(self):
        connection = _PeopleFakeConnection(
            rowsets={"dim_calendar": [], "fact_daily_report_offline": []}
        )
        payload = bi_web_queries.run_table_sx_people_progress(
            connection, {"date": "2026-08-04"})
        self.assertEqual([], payload["rows"])

    def test_table_sx_unfilled_today_roster_bound(self):
        connection = _UnfilledFakeConnection(
            rowsets={
                "dim_robot_member": [
                    {"user_id": "u1", "name": "王五", "region": "shaoxing",
                     "dept_id": "d1", "dept_name": "绍兴一组",
                     "is_active": 1},
                    {"user_id": "u2", "name": "赵六", "region": "shaoxing",
                     "dept_id": "d2", "dept_name": None,
                     "is_active": 1},
                ],
                "dim_report_roster": [
                    {"scope": "shaoxing", "entity_type": "person",
                     "person_name": "王五", "enabled": 1},
                    {"scope": "shaoxing", "entity_type": "person",
                     "person_name": "赵六", "enabled": 1},
                ],
                "fact_daily_report_offline": [
                    {"region": "shaoxing", "responsible_person": "王五",
                     "business_date": date(2026, 10, 9)},
                ],
            }
        )
        payload = bi_web_queries.run_table_sx_unfilled_today(
            connection, {"date": "2026-10-09"})
        self.assertEqual("2026-10-09", payload["date"])
        # 王五已填 → 仅赵六未填（名册绑定口径，与催办逐字一致）
        self.assertEqual(
            [{"rank": 1, "name": "赵六", "dept": "未分组"}],
            payload["rows"],
        )
