"""test_bi_web_queries 的 people 域测试；断言自原文件等价迁移。"""

from tests.common.bi_web_query_test_support import *


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
        # + 归位两表各一条（2026-10-19 归位覆盖并入，dim_robot_member
        # + dim_org_assignment_override 每轮采集各一查）
        self.assertEqual(4, len(connection.executed))

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
        # 两区各一轮采集（日历 + 事实 + 归位两表各两条），第二张卡走缓存
        self.assertEqual(8, len(connection.executed))

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
