"""test_bi_web_queries 的 integration 域测试；断言自原文件等价迁移。"""

from tests.common.bi_web_query_test_support import *


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
                cursor.execute(_ORDER_LINE_CLEANUP_SQL)
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

    def _insert_order_line_rows(self, rows):
        """rows: (suffix, spec_no, goods_name, trade_time, paid_amount)。"""
        with transaction(self.mart_connection):
            with self.mart_connection.cursor() as cursor:
                cursor.executemany(
                    _ORDER_LINE_INSERT_SQL,
                    [
                        (
                            _FIXTURE_PREFIX + suffix,
                            trade_time,
                            spec_no,
                            goods_name,
                            paid_amount,
                            _SYNC_RUN_ID,
                        )
                        for (
                            suffix,
                            spec_no,
                            goods_name,
                            trade_time,
                            paid_amount,
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

    def test_annual_target_total_matches_repo_seed_all_line_sum(self):
        # 2026-09-21 起分母 = 全业务线：210,410,000 + 549,800,000
        # + 9,300,000 = 769,510,000（餐饮线目标计入，分子侧 0 标红）。
        replace_dim_target(
            self.mart_connection, load_target_seed(REPO_SEED_PATH)
        )

        self.assertEqual(
            Decimal("769510000"), annual_target_total(self.mart_connection)
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

    def test_sku_mtd_distribution_and_pie_payload(self):
        """DATETIME 截断口径：今日计入、明日与上月排除（差值法）。"""
        today = self._server_today()
        tomorrow = today + timedelta(days=1)
        last_month = self._server_month_start() - timedelta(days=1)
        noon = datetime.combine(today, datetime.min.time()).replace(hour=12)
        before = {
            item["name"]: item["value"]
            for item in sku_mtd_distribution(self.mart_connection)
        }
        prior = before.get("biweb测试商品", Decimal("0"))

        self._insert_order_line_rows(
            [
                ("sku-a", "biweb-spec-a", "biweb测试商品", noon, Decimal("100")),
                ("sku-b", "biweb-spec-a", "biweb测试商品", noon, Decimal("23")),
                (
                    "sku-future",
                    "biweb-spec-a",
                    "biweb测试商品",
                    datetime.combine(tomorrow, datetime.min.time()),
                    Decimal("888"),
                ),
                (
                    "sku-last-month",
                    "biweb-spec-a",
                    "biweb测试商品",
                    datetime.combine(last_month, datetime.min.time()),
                    Decimal("777"),
                ),
            ]
        )

        distribution = sku_mtd_distribution(self.mart_connection)
        names = [item["name"] for item in distribution]
        self.assertIn("biweb测试商品", names)
        values = [item["value"] for item in distribution]
        self.assertEqual(values, sorted(values, reverse=True))
        after = dict(zip(names, values))
        # 同 spec 两行聚合为一片；明日/上月行不得改变差值。
        self.assertEqual(Decimal("123"), after["biweb测试商品"] - prior)

        payload = run_pie_sku_mtd(self.mart_connection, {})
        self.assertEqual("pie", payload["chart"])
        self.assertEqual("元", payload["unit"])
        self.assertLessEqual(len(payload["items"]), 9)
        self.assertTrue(
            all(
                isinstance(item["name"], str)
                and isinstance(item["value"], float)
                for item in payload["items"]
            )
        )
        payload_values = [item["value"] for item in payload["items"][:-1]]
        self.assertEqual(payload_values, sorted(payload_values, reverse=True))
        if len(payload["items"]) == 9:
            self.assertEqual("其他", payload["items"][-1]["name"])

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
        # 2026-09-21 起分子 = 线下 + 电商渠道 + 餐饮（人工月报，无数据
        # 即 0），分母 = 全业务线目标 769,510,000。
        restaurant_value, _has_data = restaurant_annual_revenue(
            self.mart_connection
        )
        self.assertEqual(
            float(numerator_after + restaurant_value), payload["value"]
        )
        self.assertEqual(769510000.0, payload["target"])
        self.assertEqual(payload["value"] / payload["target"], payload["rate"])
        self.assertEqual(
            {"线下", "电商渠道", "餐饮"},
            {line["name"] for line in payload["lines"]},
        )

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

    def test_week_and_month_gran_sum_to_the_day_total(self):
        # B1 真库回归（订正方案 §5 验收第一条）：同一区域同一窗口，
        # 周/月档合计必须等于日档合计（修复前月档落点 = 桶内最后一天
        # 的单日值）。独立区域隔离既有数据，无需差值法。
        today = self._server_today()
        if today.day < 3:
            self.skipTest("月初样本不足：需要月内至少 3 个已过去的日子")
        self._insert_offline_rows(
            [
                ("bucket-1", "biweb桶", "biweb桶人员",
                 today - timedelta(days=2), Decimal("10")),
                ("bucket-2", "biweb桶", "biweb桶人员",
                 today - timedelta(days=1), Decimal("20")),
                ("bucket-3", "biweb桶", "biweb桶人员", today, Decimal("5")),
            ]
        )
        month = today.strftime("%Y-%m")

        def region_total(payload):
            entry = next(
                entry for entry in payload["series"] if entry["name"] == "biweb桶"
            )
            return sum(value for value in entry["data"] if value is not None)

        day_total = region_total(run_trend_region_daily(self.mart_connection, {}))
        week_total = region_total(
            run_trend_region_daily(
                self.mart_connection, {"gran": "week", "month": month}
            )
        )
        month_total = region_total(
            run_trend_region_daily(
                self.mart_connection, {"gran": "month", "month": month}
            )
        )
        self.assertEqual(35.0, day_total)
        self.assertEqual(day_total, week_total)
        self.assertEqual(day_total, month_total)

    def test_kpi_offline_mtd_grans_reconcile_with_the_day_series(self):
        # 粒度批次 A 真库回归：KPI 三档与独立路径（区域日序列）逐点对
        # 拍——日档 = 日序列末日；周档 = 锚点周一→锚点的日序列子段；
        # 月档 = 整段日序列。插入今日 fixture 使水位锚定今天后再对拍
        # （差值法在此不适用：水位随动时窗口会移动）。
        today = self._server_today()
        self._insert_offline_rows(
            [
                ("gran-a", "biweb粒", "biweb粒人员", today, Decimal("100")),
                ("gran-b", "biweb粒", "biweb粒人员", today, Decimal("200")),
            ]
        )

        trend = run_trend_region_daily(
            self.mart_connection, {"month": today.strftime("%Y-%m")}
        )
        daily = [
            sum(value for value in values if value is not None)
            for values in zip(*[series["data"] for series in trend["series"]])
        ]
        kpi = {
            gran: run_kpi_offline_mtd(self.mart_connection, {"gran": gran})
            for gran in ("day", "week", "month")
        }
        if not daily or kpi["month"]["as_of"] is None:
            self.skipTest("水位不在当前月，日序列轴为空")

        # 三档锚点一致，且与日序列末日同（同为真实水位）。
        self.assertEqual(trend["as_of"], kpi["day"]["as_of"])
        self.assertEqual(kpi["day"]["as_of"], kpi["week"]["as_of"])
        self.assertEqual(kpi["week"]["as_of"], kpi["month"]["as_of"])
        anchor = date.fromisoformat(kpi["day"]["as_of"])

        self.assertAlmostEqual(daily[-1], kpi["day"]["value"], places=2)
        self.assertAlmostEqual(
            sum(daily[-(anchor.weekday() + 1):]),
            kpi["week"]["value"],
            places=2,
        )
        self.assertAlmostEqual(sum(daily), kpi["month"]["value"], places=2)

        # A5 年档：水位 = 今天（刚插入今日行）时，YTD 窗口与
        # offline_annual_total（CURDATE 上界）同窗口，值必须相等；
        # 且年累计 ≥ 月累计（窗口包含）。
        year = run_kpi_offline_mtd(self.mart_connection, {"gran": "year"})
        self.assertEqual(kpi["month"]["as_of"], year["as_of"])
        self.assertAlmostEqual(
            float(offline_annual_total(self.mart_connection)),
            year["value"],
            places=2,
        )
        self.assertLessEqual(kpi["month"]["value"], year["value"])

    def test_ecom_region_rows_never_enter_the_offline_line(self):
        # B3 真库守门（差值法）：电商区域行对线下族全部不可见——MTD、
        # 年累计、日环比在插入电商行前后逐分不差。
        today = self._server_today()
        mtd_before = offline_mtd_total(self.mart_connection)
        annual_before = offline_annual_total(self.mart_connection)
        dod_before = offline_dod(self.mart_connection)

        self._insert_offline_rows(
            [("line-ecom", "电商", "biweb电商人员", today, Decimal("777777"))]
        )

        self.assertEqual(mtd_before, offline_mtd_total(self.mart_connection))
        self.assertEqual(annual_before, offline_annual_total(self.mart_connection))
        dod_after = offline_dod(self.mart_connection)
        self.assertEqual(
            None if dod_before is None else dod_before["date"],
            None if dod_after is None else dod_after["date"],
        )
        self.assertEqual(
            None if dod_before is None else dod_before["value"],
            None if dod_after is None else dod_after["value"],
        )

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
        # 三态完整轴（执行提示词 §4.2）：月首 → min(月末, 锚点日)，无数据
        # 日 None+missing，不再只取有数据的日。
        expected_dates = [
            (first_day + timedelta(days=offset)).strftime("%m-%d")
            for offset in range(today.day)
        ]
        self.assertEqual(expected_dates, series["dates"])
        self.assertEqual(len(series["dates"]), len(series["series"][0]["data"]))
        # 合计行被 `%%合计%%` 排除（这条 SQL 是双写证明点之一）：fixture
        # 当日 100，此前日 None。
        self.assertEqual(
            [None] * (today.day - 1) + [100.0], series["series"][0]["data"]
        )
        self.assertEqual(
            ["missing"] * (today.day - 1) + ["ok"], series["status"][0]
        )

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
