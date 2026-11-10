"""test_bi_web_queries 的 offline 域测试；断言自原文件等价迁移。"""

from tests.common.bi_web_query_test_support import *


class OfflineSqlShapeTests(SqlShapeTestCase):
    """The three offline-side queries: 合计 excluded, future rows truncated."""

    def test_offline_mtd_total_sql_shape(self):
        connection = _DodFakeConnection(
            scalars={"fact_daily_report_offline": Decimal("1")},
            latest={"fact_daily_report_offline": date(2026, 9, 16)},
        )

        total = offline_mtd_total(connection)

        # B2（水位批次）两条语句：静态 MAX 水位定锚 + 参数化窗口求和
        # （上界=真实水位；月首锚点仍 MySQL 时钟，时钟统一是 B6 的活）。
        self.assertEqual(Decimal("1"), total)
        self.assertEqual(2, len(connection.executed))
        latest_sql, latest_params = connection.executed[0]
        sum_sql, sum_params = connection.executed[1]
        self.assertIsNone(latest_params)
        self.assertIn("MAX(business_date)", latest_sql)
        self.assertIn("FROM fact_daily_report_offline", latest_sql)
        self.assertIn(_TRUNCATION, latest_sql)
        self.assertIn(_SUMMARY_EXCLUSION, latest_sql)
        self.assertIn(_LINE_EXCLUSION, latest_sql)
        self.assertIn("FROM fact_daily_report_offline", sum_sql)
        # 参数化语句的字面 % 必须双写（pymysql sql % params），
        # 残留单 % 审计与 sole_parameterized_sql 同款。
        self.assertIn(
            "business_date >= DATE_FORMAT(CURDATE(), '%%Y-%%m-01')", sum_sql
        )
        self.assertIn("business_date <= %s", sum_sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, sum_sql)
        self.assertIn(_LINE_EXCLUSION, sum_sql)
        self.assertNotIn(_TRUNCATION, sum_sql)
        self.assertEqual(0, sum_sql.replace("%%", "").replace("%s", "").count("%"))
        self.assertEqual((date(2026, 9, 16),), sum_params)

    def test_offline_mtd_total_empty_water_level_short_circuits(self):
        connection = _DodFakeConnection(
            latest={"fact_daily_report_offline": None}
        )

        self.assertEqual(Decimal(0), offline_mtd_total(connection))

        # 空表（水位 None）只发定锚一条，窗口求和短路、不发起。
        self.assertEqual(1, len(connection.executed))
        self.assertIn("MAX(business_date)", connection.executed[0][0])

    def test_offline_annual_total_sql_shape(self):
        connection = FakeConnection(
            scalars={"fact_daily_report_offline": Decimal("1")}
        )

        offline_annual_total(connection)

        sql = self.sole_static_sql(connection)
        self.assertIn("FROM fact_daily_report_offline", sql)
        self.assertIn(_SUMMARY_EXCLUSION, sql)
        self.assertIn(_LINE_EXCLUSION, sql)
        self.assertIn(_YEAR_START, sql)
        self.assertIn(_TRUNCATION, sql)

    def test_region_daily_series_sql_shape(self):
        connection = _DodFakeConnection(
            rowsets={"fact_daily_report_offline": []},
            latest={"fact_daily_report_offline": date(2026, 9, 16)},
        )

        region_daily_series(connection)

        # B2（水位批次）：当前月锚点=真实水位，事实表语句两条——静态
        # MAX 定锚 + 静态窗口序列；另加 dim_calendar 营业日查询
        # （month_workdays 复用）。两条事实语句均全静态。
        fact_statements = [
            (sql, parameters)
            for sql, parameters in connection.executed
            if "fact_daily_report_offline" in sql
        ]
        self.assertEqual(2, len(fact_statements))
        latest_sql, latest_params = fact_statements[0]
        self.assertIsNone(latest_params)
        self.assertIn("MAX(business_date)", latest_sql)
        self.assertIn(_LINE_EXCLUSION, latest_sql)
        sql, parameters = fact_statements[1]
        self.assertIsNone(parameters)
        self.assertIn("FROM fact_daily_report_offline", sql)
        self.assertIn(_SUMMARY_EXCLUSION, sql)
        self.assertIn(_LINE_EXCLUSION, sql)
        self.assertIn(_MONTH_START, sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertIn("GROUP BY business_date, region", sql)


class RegionDailySeriesTests(unittest.TestCase):
    """三态轴 + 水位锚点（§4.2 / B2）：轴=月首→min(月末, 水位) 完整窗口。

    B2 起当前月锚点 = 事实表 MAX(business_date)（最新入仓日）：T+1
    未入仓的日子不进轴、as_of 即水位。水位钉当天时轴与旧口径
    （锚点=今天）逐点一致；夹具时钟相对，轴端点随水位走。
    """

    def test_complete_axis_and_missing_points_are_none_not_zero_filled(self):
        today = date.today()
        month_first = today.replace(day=1)
        rows = [
            {"business_date": month_first, "region": "杭州", "total": Decimal("10")},
        ]
        connection = _DodFakeConnection(
            rowsets={"fact_daily_report_offline": rows},
            latest={"fact_daily_report_offline": today},
        )

        result = region_daily_series(connection)

        # 完整轴：月首 → 水位（钉今天），无数据日也在轴上（修复前根本不进轴）。
        self.assertEqual(today.day, len(result["dates"]))
        self.assertEqual(month_first.strftime("%m-%d"), result["dates"][0])
        self.assertEqual(today.strftime("%m-%d"), result["dates"][-1])
        self.assertEqual(today.isoformat(), result["as_of"])
        self.assertEqual(["杭州"], [entry["name"] for entry in result["series"]])
        data = result["series"][0]["data"]
        self.assertEqual(10.0, data[0])
        # 缺失日 None（断线），绝不用 0 表示「没数据」。
        self.assertEqual([None] * (today.day - 1), data[1:])
        status = result["status"][0]
        self.assertEqual("ok", status[0])
        self.assertEqual(["missing"] * (today.day - 1), status[1:])

    def test_axis_ends_at_water_level_not_today(self):
        # B2 核心：水位滞后 4 天时轴到水位为止——缺的天不再进轴标
        # missing（旧口径把它们画进轴、页脚却报「截至今天」）。
        today = date.today()
        if today.day < 5:
            self.skipTest("月初样本不足：水位需滞后 4 天且仍在月内")
        latest = today - timedelta(days=4)
        rows = [
            {"business_date": latest, "region": "杭州", "total": Decimal("10")},
        ]
        connection = _DodFakeConnection(
            rowsets={"fact_daily_report_offline": rows},
            latest={"fact_daily_report_offline": latest},
        )

        result = region_daily_series(connection)

        self.assertEqual(latest.day, len(result["dates"]))
        self.assertEqual(latest.strftime("%m-%d"), result["dates"][-1])
        self.assertEqual(latest.isoformat(), result["as_of"])
        # 水位日有行 → ok；轴内缺失日仍 None+missing。
        self.assertEqual(10.0, result["series"][0]["data"][-1])
        self.assertEqual("ok", result["status"][0][-1])
        self.assertIsNone(result["series"][0]["data"][-2])
        self.assertEqual("missing", result["status"][0][-2])

    def test_empty_water_level_yields_empty_axis(self):
        # 空表（水位 None）：轴为空、as_of 不下发（前端无角标），
        # 不再虚构「截至今天」的完整轴。
        connection = _DodFakeConnection(
            rowsets={"fact_daily_report_offline": []},
            latest={"fact_daily_report_offline": None},
        )

        result = region_daily_series(connection)

        self.assertEqual([], result["dates"])
        self.assertEqual([], result["series"])
        self.assertEqual([], result["status"])
        self.assertIsNone(result["as_of"])


class BucketAggregationTests(unittest.TestCase):
    """B1（P0 桶聚合）：周/月档必须是区间累计，不是「桶内最后一天」。

    两道防线分别锁定：SQL 侧分桶表达式别名 ``bucket_start`` 不再与
    真实列同名（MySQL GROUP BY 不再解析回 business_date 导致桶内
    按天散行）；Python 侧同 (series, bucket) 多行 Decimal 累加
    （旧的覆盖赋值会让「桶内最后一天胜出」）。
    """

    def _connection(self, *, daily_rows=(), week_rows=(), month_rows=(),
                    latest=None):
        return _TrendFakeConnection(
            rowsets={
                "fact_daily_report_offline": list(daily_rows),
                "dim_calendar": [],
            },
            week_rows=week_rows,
            month_rows=month_rows,
            latest={"fact_daily_report_offline": latest or date.today()},
        )

    def test_weekly_sql_groups_by_bucket_alias_not_real_column(self):
        connection = self._connection()

        region_weekly_series(connection, month=date.today().strftime("%Y-%m"))

        fact_sql = next(sql for sql, _ in connection.executed if "WEEKDAY" in sql)
        self.assertIn("AS bucket_start", fact_sql)
        self.assertIn("GROUP BY bucket_start, region", fact_sql)
        self.assertIn(_LINE_EXCLUSION, fact_sql)
        self.assertNotIn("GROUP BY business_date, region", fact_sql)

    def test_monthly_sql_groups_by_bucket_alias_not_real_column(self):
        connection = self._connection()

        region_monthly_series(connection, month=date.today().strftime("%Y-%m"))

        fact_sql = next(sql for sql, _ in connection.executed if "DAYOFMONTH" in sql)
        self.assertIn("AS bucket_start", fact_sql)
        self.assertIn("GROUP BY bucket_start, region", fact_sql)
        self.assertIn(_LINE_EXCLUSION, fact_sql)
        self.assertNotIn("GROUP BY business_date, region", fact_sql)

    def test_duplicate_bucket_rows_accumulate_instead_of_overwrite(self):
        # 第二道防线：即使上游分桶失效（同桶散行，修复前 SQL 的产出
        # 形态），同 (region, bucket_start) 多行也必须求和——修复前
        # 是「桶内最后一天胜出」。
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        connection = self._connection(
            week_rows=[
                {"bucket_start": monday, "region": "杭州", "total": Decimal("10")},
                {"bucket_start": monday, "region": "杭州", "total": Decimal("5")},
            ],
            latest=today,
        )

        payload = run_trend_region_daily(connection, {"gran": "week"})

        index = payload["dates"].index(monday.strftime("%m-%d"))
        entry = payload["series"][0]
        self.assertEqual("杭州", entry["name"])
        self.assertEqual(15.0, entry["data"][index])
        self.assertEqual("ok", payload["status"][0][index])

    def test_day_week_month_grans_reconcile_on_the_same_window(self):
        # 最强回归：同一份日行，三档合计必须相等（修复前实测 9 月：
        # 日档 35,467,393.88 vs 月档落点 1,643,879.37 = 9-16 单日值）。
        today = date.today()
        month_first = today.replace(day=1)
        days = [
            month_first + timedelta(days=offset)
            for offset in range(min(3, today.day))
        ]
        values = [Decimal("10"), Decimal("20"), Decimal("5")][: len(days)]
        total = sum(values)
        expected = float(total)
        monday = today - timedelta(days=today.weekday())
        connection = self._connection(
            daily_rows=[
                {"business_date": day, "region": "杭州", "total": value}
                for day, value in zip(days, values)
            ],
            week_rows=[
                {"bucket_start": monday, "region": "杭州", "total": total}
            ],
            month_rows=[
                {"bucket_start": month_first, "region": "杭州", "total": total}
            ],
            latest=today,
        )

        day_payload = run_trend_region_daily(connection, {})
        week_payload = run_trend_region_daily(connection, {"gran": "week"})
        month_payload = run_trend_region_daily(connection, {"gran": "month"})

        day_total = sum(
            value for value in day_payload["series"][0]["data"]
            if value is not None
        )
        week_index = week_payload["dates"].index(monday.strftime("%m-%d"))
        month_index = month_payload["dates"].index(month_first.strftime("%Y-%m"))
        week_total = week_payload["series"][0]["data"][week_index]
        month_total = month_payload["series"][0]["data"][month_index]
        self.assertEqual(expected, day_total)
        self.assertEqual(day_total, week_total)
        self.assertEqual(day_total, month_total)
        # 三档 as_of 同为水位日（B2）。
        for payload in (day_payload, week_payload, month_payload):
            self.assertEqual(today.isoformat(), payload["as_of"])


class OfflineLineExclusionTests(unittest.TestCase):
    """B3（2026-09-20 业务裁定）：region=电商 归电商线，线下取数族一律
    排除——漏任何一条都是「同一笔电商销售两条线各计一次」。全族扫描
    守门：新增线下查询忘了带排除时本测试即红。"""

    def test_every_offline_line_query_excludes_ecom_region(self):
        today = date.today()
        month = today.strftime("%Y-%m")
        first_day, last_day = month_bounds(month)

        def dod_fake():
            return _DodFakeConnection(
                scalars={"fact_daily_report_offline": Decimal("0")},
                rowsets={"fact_daily_report_offline": [], "dim_calendar": []},
                latest={"fact_daily_report_offline": today},
            )

        def scalar_fake():
            return FakeConnection(
                scalars={"fact_daily_report_offline": Decimal("0")}
            )

        cases = {
            "offline_mtd_total": (dod_fake, offline_mtd_total),
            "offline_annual_total": (scalar_fake, offline_annual_total),
            "offline_dod": (dod_fake, offline_dod),
            "region_daily_series": (dod_fake, region_daily_series),
            "region_month_daily_series": (
                dod_fake,
                lambda c: region_month_daily_series(
                    c, first_day=first_day, last_day=last_day
                ),
            ),
            "region_weekly_series": (
                dod_fake, lambda c: region_weekly_series(c, month=month)
            ),
            "region_monthly_series": (
                dod_fake, lambda c: region_monthly_series(c, month=month)
            ),
            "region_mtd_total": (
                scalar_fake,
                lambda c: region_mtd_total(
                    c, first_day=first_day, last_day=last_day
                ),
            ),
            "region_month_target": (
                scalar_fake,
                lambda c: region_month_target(
                    c, first_day=first_day, last_day=last_day
                ),
            ),
            "department_mtd_ranking": (
                lambda: FakeConnection(
                    rowsets={"fact_daily_report_offline": []}
                ),
                lambda c: department_mtd_ranking(
                    c, first_day=first_day, last_day=last_day
                ),
            ),
            "run_kpi_offline_mtd": (dod_fake, lambda c: run_kpi_offline_mtd(c, {})),
        }
        for name, (make_connection, run) in cases.items():
            with self.subTest(query=name):
                connection = make_connection()
                run(connection)
                fact_sqls = [
                    sql for sql, _ in connection.executed
                    if "fact_daily_report_offline" in sql
                ]
                self.assertTrue(fact_sqls)
                for sql in fact_sqls:
                    self.assertIn(_LINE_EXCLUSION, sql)


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
        self.assertIn(_LINE_EXCLUSION, sql)

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
        self.assertIn(_LINE_EXCLUSION, sql)

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
        self.assertIn(_LINE_EXCLUSION, sql)
        # 月目标是月级属性：与 summarize_people 同口径取全月行 MAX，
        # 刻意不做 CURDATE 截断（预填未来行不影响 MAX）。
        self.assertNotIn(_TRUNCATION, sql)

    def test_department_mtd_ranking_sql_shape_and_null_department(self):
        connection = FakeConnection(
            rowsets={"fact_daily_report_offline": [
                {"responsible_person": "甲", "region": "杭州",
                 "department": "零售一组", "total": Decimal("30")},
                {"responsible_person": "乙", "region": "杭州",
                 "department": None, "total": Decimal("10")},
            ]}
        )

        ranking = department_mtd_ranking(
            connection,
            region="杭州",
            first_day=date(2026, 9, 1),
            last_day=date(2026, 9, 30),
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30), "杭州"),
            table="fact_daily_report_offline",  # 归位升级后另有 dim 查询
        )
        # 2026-10-19 归位升级：人级聚合（归位映射在 Python 侧再聚合）
        self.assertIn(
            "GROUP BY responsible_person, region, department", sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, sql)
        self.assertIn(_LINE_EXCLUSION, sql)
        # 归位映射无命中（fake 无通讯录行）→ 回退表内 department 值
        self.assertEqual(["零售一组", "未分组"], ranking["categories"])
        self.assertEqual([Decimal("30"), Decimal("10")], ranking["values"])

    def test_department_mtd_ranking_applies_assignment_override(self):
        """归位覆盖并入（2026-10-19 裁决，杭州销售部案例）：覆盖层命中时
        部门按归位口径聚合（同人跨行合并到覆盖部门）。"""
        connection = FakeConnection(
            rowsets={
                "fact_daily_report_offline": [
                    {"responsible_person": "余发兴", "region": "hangzhou",
                     "department": "杭中项目部", "total": Decimal("100")},
                    {"responsible_person": "黄晓怡", "region": "hangzhou",
                     "department": "杭中项目部", "total": Decimal("50")},
                ],
                "dim_robot_member": [
                    {"user_id": "u1", "region": "hangzhou", "name": "余发兴",
                     "dept_id": "d1", "dept_name": "杭中项目部"},
                    {"user_id": "u2", "region": "hangzhou", "name": "黄晓怡",
                     "dept_id": "d1", "dept_name": "杭中项目部"},
                ],
                "dim_org_assignment_override": [
                    {"id": 1, "user_id": "u1", "name": "余发兴",
                     "region": "hangzhou", "dept_id": None,
                     "dept_name": "杭州销售部", "enabled": 1, "note": None,
                     "updated_by": "ops", "updated_at": None},
                ],
            }
        )

        ranking = department_mtd_ranking(
            connection,
            region="hangzhou",
            first_day=date(2026, 10, 1),
            last_day=date(2026, 10, 31),
        )

        self.assertEqual(["杭州销售部", "杭中项目部"], ranking["categories"])
        self.assertEqual([Decimal("100"), Decimal("50")], ranking["values"])

    def test_region_month_daily_series_sql_shape(self):
        connection = _DodFakeConnection(
            rowsets={"fact_daily_report_offline": [
                {"business_date": date(2026, 9, 2), "region": "杭州", "total": Decimal("5")},
            ]},
            latest={"fact_daily_report_offline": date(2026, 9, 16)},
        )

        region_month_daily_series(
            connection,
            region="杭州",
            first_day=date(2026, 9, 1),
            last_day=date(2026, 9, 30),
        )

        # B2（水位批次）：窗口落在当前月时多一条静态 MAX 水位定锚
        # （运行日越过 2026-09 则不发起）——滤掉定锚语句后，参数化
        # 窗口语句仍唯一，口径断言不变。
        window_statements = [
            (sql, parameters)
            for sql, parameters in connection.executed
            if "fact_daily_report_offline" in sql
            and "MAX(business_date)" not in sql
        ]
        self.assertEqual(1, len(window_statements))
        sql, parameters = window_statements[0]
        self.assertEqual(
            (date(2026, 9, 1), date(2026, 9, 30), "杭州"), parameters
        )
        self.assertIn("FROM fact_daily_report_offline", sql)
        self.assertIn("business_date BETWEEN %s AND %s", sql)
        self.assertIn("region = %s", sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, sql)
        self.assertIn(_LINE_EXCLUSION, sql)
        self.assertIn("GROUP BY business_date, region", sql)


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
        # 三态完整轴（执行提示词 §4.2）：历史月 2026-08 钉死 31 天窗口。
        rows = [
            {"business_date": date(2026, 8, 2), "region": "杭州", "total": Decimal("5")},
            {"business_date": date(2026, 8, 3), "region": "杭州", "total": Decimal("6")},
        ]
        connection = FakeConnection(rowsets={"fact_daily_report_offline": rows})

        payload = run_trend_region_daily(
            connection, {"region": "杭州", "month": "2026-08"}
        )

        self.assertEqual("line", payload["chart"])
        self.assertEqual(31, len(payload["dates"]))
        self.assertEqual("08-01", payload["dates"][0])
        self.assertEqual("08-31", payload["dates"][-1])
        self.assertEqual(
            [{"name": "杭州", "data": [None, 5.0, 6.0] + [None] * 28}],
            payload["series"],
        )
        self.assertEqual(
            [["missing", "ok", "ok"] + ["missing"] * 28], payload["status"]
        )
        sql, parameters = connection.executed[0]
        self.assertIn("region = %s", sql)
        self.assertIn("GROUP BY business_date, region", sql)
        self.assertEqual((date(2026, 8, 1), date(2026, 8, 31), "杭州"), parameters)

    def test_run_trend_region_daily_month_without_region_two_series(self):
        rows = [
            {"business_date": date(2026, 8, 2), "region": "杭州", "total": Decimal("5")},
            {"business_date": date(2026, 8, 2), "region": "绍兴", "total": Decimal("3")},
        ]
        connection = FakeConnection(rowsets={"fact_daily_report_offline": rows})

        payload = run_trend_region_daily(connection, {"month": "2026-08"})

        self.assertEqual(31, len(payload["dates"]))
        self.assertEqual("08-01", payload["dates"][0])
        self.assertEqual("08-31", payload["dates"][-1])
        self.assertEqual(
            [
                {"name": "杭州", "data": [None, 5.0] + [None] * 29},
                {"name": "绍兴", "data": [None, 3.0] + [None] * 29},
            ],
            payload["series"],
        )
        self.assertEqual(
            [["missing", "ok"] + ["missing"] * 29] * 2, payload["status"]
        )
        sql, parameters = connection.executed[0]
        self.assertNotIn("region = %s", sql)
        self.assertEqual((date(2026, 8, 1), date(2026, 8, 31)), parameters)

    def test_run_trend_region_daily_maps_region_key_to_chinese_label(self):
        """region 键 → dim_org_region.label 中文名（2026-10-19 英译中裁决）。

        生产事实表 region 是英文键（hangzhou/shaoxing），趋势图系列名须
        翻成中文；夹具无目录时回退原键（fail-open）。
        """
        rows = [
            {"business_date": date(2026, 8, 2), "region": "hangzhou",
             "total": Decimal("5")},
        ]
        connection = FakeConnection(rowsets={
            "fact_daily_report_offline": rows,
            "dim_org_region": [{"k": "hangzhou", "v": "杭州"}],
        })
        # 清模块级映射缓存，避免其它用例的目录串扰
        bi_web_queries._REGION_LABEL_CACHE["at"] = 0.0
        bi_web_queries._REGION_LABEL_CACHE["map"] = {}

        payload = run_trend_region_daily(connection, {"month": "2026-08"})

        names = [entry["name"] for entry in payload["series"]]
        self.assertIn("杭州", names)
        self.assertNotIn("hangzhou", names)

    def test_run_trend_region_daily_without_params_keeps_static_l1_path(self):
        connection = _DodFakeConnection(
            rowsets={"fact_daily_report_offline": []},
            latest={"fact_daily_report_offline": date.today()},
        )

        run_trend_region_daily(connection, {})

        # B2（水位批次）：当前月锚点=真实水位，事实表语句两条（静态
        # MAX 定锚 + 静态窗口序列），外加 dim_calendar 营业日查询；
        # 参数化语句一条都没有（StaticSqlTests 同款口径）。
        fact_statements = [
            (sql, parameters)
            for sql, parameters in connection.executed
            if "fact_daily_report_offline" in sql
        ]
        self.assertEqual(2, len(fact_statements))
        latest_sql, latest_params = fact_statements[0]
        self.assertIsNone(latest_params)
        self.assertIn("MAX(business_date)", latest_sql)
        sql, parameters = fact_statements[1]
        self.assertIsNone(parameters)
        self.assertIn(_MONTH_START, sql)

    def test_run_bar_department_mtd_payload(self):
        rows = [{"responsible_person": "甲", "region": "杭州",
                 "department": "零售一组", "total": Decimal("30")}]
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
        # 2026-10-19 归位升级：人级聚合（归位映射 Python 侧再聚合）
        self.assertIn(
            "GROUP BY responsible_person, region, department", sql)
        self.assertEqual((date(2026, 8, 1), date(2026, 8, 31), "杭州"), parameters)


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
