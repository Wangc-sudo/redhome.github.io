# -*- coding: utf-8 -*-
"""offline_summary（线下整体日/周/月报）单测：fake conn + 纯函数断言。"""

import unittest
from datetime import date, datetime
from unittest import mock

from common.daily_robot.offline_summary import (
    AGG_SCOPES,
    DAILY_KIND,
    MONTHLY_KIND,
    TOTAL_SCOPE_KEY,
    WEEKLY_KIND,
    build_daily_markdown,
    build_monthly_markdown,
    build_offline_all_html,
    build_weekly_markdown,
    collect_offline_all_people,
    compute_daily_metrics,
    compute_monthly_metrics,
    compute_weekly_metrics,
    fetch_dept_daily_facts,
    fetch_dept_month_targets,
    UNMAPPED_DEPT,
    merge_facts,
    merge_targets,
    previous_month,
    previous_week,
    run_daily_summary,
    run_monthly_summary,
    run_weekly_summary,
)


# ---------------------------------------------------------------------------
# 纯口径
# ---------------------------------------------------------------------------

class ComputeDailyTest(unittest.TestCase):
    def test_full_metrics(self):
        facts = {
            date(2026, 9, 22): 100.0,
            date(2026, 9, 23): 250.0,
            date(2026, 9, 16): 50.0,
            date(2026, 8, 20): 80.0,
            date(2026, 8, 23): 40.0,
        }
        m = compute_daily_metrics(
            "hangzhou", "杭州",
            facts=facts, business_date=date(2026, 9, 23), month_target=1000.0,
        )
        self.assertEqual(m.sales, 250.0)
        self.assertEqual(m.yesterday_sales, 100.0)  # 昨日（9-22）
        # 日环比：对前一自然日（9-22）
        self.assertEqual(m.dod_amount, 150.0)
        self.assertAlmostEqual(m.dod_rate, 1.5)
        # 本周（周一 9-21 至今）= 9-22 + 9-23 = 350
        # 上周同期（上周一 9-14 ~ 上周同星期几 9-16）= 9-16 = 50
        self.assertEqual(m.week_completed, 350.0)
        self.assertEqual(m.prev_week_completed, 50.0)
        # 周环比：本周 350 对上周同期 50（周口径）
        self.assertEqual(m.wow_amount, 300.0)
        self.assertAlmostEqual(m.wow_rate, 6.0)
        # 月累计 = 9-16 + 9-22 + 9-23
        self.assertEqual(m.month_completed, 400.0)
        self.assertEqual(m.month_target, 1000.0)
        self.assertAlmostEqual(m.month_rate, 0.4)
        # 上月累计（8-1~8-23）= 80 + 40 = 120；无上月目标 → 上月达成率 None
        self.assertEqual(m.prev_month_completed, 120.0)
        self.assertIsNone(m.prev_month_target)
        self.assertIsNone(m.prev_month_rate)
        # 月环比：本月 1~23 累计 对 上月 1~23 累计（80+40=120）
        self.assertEqual(m.mom_amount, 280.0)
        self.assertAlmostEqual(m.mom_rate, 280.0 / 120.0)

    def test_missing_days_are_zero_and_rate_guards(self):
        m = compute_daily_metrics(
            "shaoxing", "绍兴",
            facts={}, business_date=date(2026, 9, 23), month_target=None,
        )
        self.assertEqual(m.sales, 0.0)
        self.assertIsNone(m.dod_rate)
        self.assertIsNone(m.wow_rate)
        self.assertIsNone(m.mom_rate)
        self.assertIsNone(m.month_rate)

    def test_mom_prev_month_day_clamped(self):
        # 3-31 的上月同日日 = 2-28（2026 年 2 月 28 天）
        facts = {date(2026, 2, 28): 70.0, date(2026, 3, 31): 10.0}
        m = compute_daily_metrics(
            "shengwai", "省外",
            facts=facts, business_date=date(2026, 3, 31), month_target=None,
        )
        self.assertEqual(m.month_completed, 10.0)
        self.assertEqual(m.mom_amount, 10.0 - 70.0)


class PeriodBoundaryTest(unittest.TestCase):
    def test_previous_week(self):
        self.assertEqual(
            previous_week(date(2026, 9, 28)),
            (date(2026, 9, 21), date(2026, 9, 27)),
        )
        # 周日引用仍取上一整周
        self.assertEqual(
            previous_week(date(2026, 9, 27)),
            (date(2026, 9, 14), date(2026, 9, 20)),
        )

    def test_previous_month(self):
        self.assertEqual(
            previous_month(date(2026, 10, 1)),
            (date(2026, 9, 1), date(2026, 9, 30)),
        )
        self.assertEqual(
            previous_month(date(2026, 3, 15)),
            (date(2026, 2, 1), date(2026, 2, 28)),
        )

    def test_weekly_metrics(self):
        facts = {
            date(2026, 9, 22): 100.0,
            date(2026, 9, 25): 300.0,
            date(2026, 9, 16): 50.0,
        }
        m = compute_weekly_metrics(
            "hangzhou", "杭州", facts=facts,
            week_start=date(2026, 9, 21), week_end=date(2026, 9, 27),
        )
        self.assertEqual(m.sales, 400.0)
        self.assertEqual(m.wow_amount, 350.0)
        self.assertAlmostEqual(m.wow_rate, 7.0)

    def test_monthly_metrics(self):
        facts = {
            date(2026, 9, 3): 100.0,
            date(2026, 9, 20): 300.0,
            date(2026, 8, 10): 200.0,
        }
        m = compute_monthly_metrics(
            "hangzhou", "杭州", facts=facts,
            month_first=date(2026, 9, 1), month_last=date(2026, 9, 30),
            month_target=1000.0,
        )
        self.assertEqual(m.sales, 400.0)
        self.assertEqual(m.mom_amount, 200.0)
        self.assertAlmostEqual(m.mom_rate, 1.0)
        self.assertAlmostEqual(m.month_rate, 0.4)

    def test_merge(self):
        merged = merge_facts([
            {date(2026, 9, 1): 1.0, date(2026, 9, 2): 2.0},
            {date(2026, 9, 1): 3.0},
        ])
        self.assertEqual(merged[date(2026, 9, 1)], 4.0)
        self.assertEqual(merge_targets([None, 100.0, 200.0]), 300.0)
        self.assertIsNone(merge_targets([None, None]))


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _daily_rows():
    rows = [
        compute_daily_metrics(
            "hangzhou", "杭州",
            facts={date(2026, 9, 23): 20000.0, date(2026, 9, 22): 10000.0},
            business_date=date(2026, 9, 23), month_target=100000.0,
        ),
        compute_daily_metrics(
            "shaoxing", "绍兴",
            facts={}, business_date=date(2026, 9, 23), month_target=None,
        ),
    ]
    total = compute_daily_metrics(
        TOTAL_SCOPE_KEY, "线下整体",
        facts=merge_facts([
            {date(2026, 9, 23): 20000.0, date(2026, 9, 22): 10000.0},
            {},
        ]),
        business_date=date(2026, 9, 23), month_target=100000.0,
    )
    return rows, total


class RenderTest(unittest.TestCase):
    def test_daily_markdown(self):
        rows, total = _daily_rows()
        body = build_daily_markdown(
            business_date=date(2026, 9, 23), rows=rows, total=total,
        )
        self.assertIn("【线下整体日报】9月23日（周三）", body)
        self.assertIn("今日合计：**2.0万 元**", body)
        self.assertIn("**一、区域**", body)
        self.assertIn(
            "| 区域 | 今日 | 昨日 | 日环比 | 本周 | 上周 | 周环比 | 月累计 "
            "| 月目标 | 上月累计 | 上月目标 | 本月达成率 | 上月达成率 |", body,
        )
        # 未传 dept_rows 时不出部门段（向后兼容）
        self.assertNotIn("**二、部门**", body)
        self.assertIn(
            "| 杭州 | 2.0万 | 1.0万 | +100.0% | 3.0万 | 0 | -- | 3.0万 "
            "| 10.0万 | 0 | -- | 30.0% | -- |", body)
        self.assertIn(
            "| 绍兴 | 0 | 0 | -- | 0 | 0 | -- | 0 | -- | 0 | -- | -- | -- |",
            body)
        self.assertIn("| **线下整体** |", body)

    def test_weekly_markdown_ranking(self):
        rows = [
            compute_weekly_metrics(
                "hangzhou", "杭州", facts={date(2026, 9, 22): 100.0},
                week_start=date(2026, 9, 21), week_end=date(2026, 9, 27),
            ),
            compute_weekly_metrics(
                "shaoxing", "绍兴", facts={date(2026, 9, 22): 500.0},
                week_start=date(2026, 9, 21), week_end=date(2026, 9, 27),
            ),
        ]
        total = compute_weekly_metrics(
            TOTAL_SCOPE_KEY, "线下整体",
            facts={date(2026, 9, 22): 600.0},
            week_start=date(2026, 9, 21), week_end=date(2026, 9, 27),
        )
        body = build_weekly_markdown(
            week_start=date(2026, 9, 21), week_end=date(2026, 9, 27),
            rows=rows, total=total,
        )
        self.assertIn("【线下整体周报】9月21日（周一） ~ 9月27日（周日）", body)
        # 绍兴 500 > 杭州 100 → 绍兴排第一
        self.assertLess(body.index("| 1 | 绍兴 |"), body.index("| 2 | 杭州 |"))

    def test_monthly_markdown_rate_ranking(self):
        rows = [
            compute_monthly_metrics(
                "hangzhou", "杭州", facts={date(2026, 9, 2): 100.0},
                month_first=date(2026, 9, 1), month_last=date(2026, 9, 30),
                month_target=1000.0,
            ),
            compute_monthly_metrics(
                "shaoxing", "绍兴", facts={date(2026, 9, 2): 500.0},
                month_first=date(2026, 9, 1), month_last=date(2026, 9, 30),
                month_target=None,
            ),
        ]
        total = compute_monthly_metrics(
            TOTAL_SCOPE_KEY, "线下整体",
            facts={date(2026, 9, 2): 600.0},
            month_first=date(2026, 9, 1), month_last=date(2026, 9, 30),
            month_target=1000.0,
        )
        body = build_monthly_markdown(
            month_first=date(2026, 9, 1), rows=rows, total=total,
        )
        self.assertIn("【线下整体月报】2026年9月", body)
        # 有达成率的杭州（10%）排在无目标的绍兴之前（None 垫底）
        self.assertLess(body.index("| 1 | 杭州 |"), body.index("| 2 | 绍兴 |"))


# ---------------------------------------------------------------------------
# 任务（fake conn + fake outbox）
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        return None

    def execute(self, sql, params=None):
        self._conn.sql_log.append(sql)
        if "INSERT INTO `agg_offline_daily`" in sql:
            self._conn.agg_rows.append(params)
            self._rows = []
            return
        if "dim_calendar" in sql:
            self._rows = []
            return
        if "dim_org_assignment_override" in sql:
            self._rows = self._conn.overrides
            return
        if "SELECT DISTINCT `region` FROM `dim_robot_member`" in sql:
            self._rows = [{"region": r} for r in self._conn.regions]
            return
        # 部门维度：目标（p/fd/t）与日销（d/p/fd/s）分开模拟。
        # 用 `department` AS `fd` 判别——它只在部门取数出现，
        # fetch_scope_month_target 没有，避免误截获。
        if "`department` AS `fd`" in sql:
            if "MAX(`monthly_target`)" in sql:
                self._rows = self._conn.dept_targets
            else:
                self._rows = self._conn.dept_rows
            return
        if "dim_robot_member" in sql:
            self._rows = [
                {"region": region, "name": name, "dept_name": dept}
                for (region, name), dept in self._conn.members.items()
            ]
            return
        # 补集人员采集（「其他」桶人员榜）：列形 region/rp/dept/...
        if "`region` AS `region`" in sql:
            self._rows = self._conn.other_rows
            return
        # 补集（region NOT IN 杭/绍/vanke）：region 不进 params
        if "`region` NOT IN" in sql:
            region = "__offline_other__"
        else:
            region = params[0]
        # 锚点由 SQL 片段形态识别（值都在 params[3]）
        if "`responsible_person` = %s" in sql:
            anchor = ("person", params[3])
        elif "`department` = %s" in sql:
            anchor = ("dept", params[3])
        elif "`department` <> %s" in sql:
            anchor = ("dept_not", params[3])
        else:
            anchor = None
        if "MAX(`monthly_target`)" in sql:
            value = self._conn.targets.get((region, anchor))
            self._rows = [{"t": value}] if value is not None else []
        else:
            facts = self._conn.facts.get((region, anchor), {})
            self._rows = [
                {"d": d, "s": amount} for d, amount in facts.items()
            ]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, facts, targets):
        self.facts = facts        # {(region, anchor): {date: amount}}
        self.targets = targets    # {(region, anchor): float|None}
        self.members = {}         # {(region, name): dept_name}（通讯录）
        self.other_rows = []      # 补集人员采集行（「其他」桶）
        self.regions = ()         # dim_robot_member 出现的 region
        self.overrides = []       # dim_org_assignment_override 行
        self.dept_rows = []       # 部门日销行 {d,p,fd,s}
        self.dept_targets = []    # 部门目标行 {p,fd,t}
        self.agg_rows = []
        self.sql_log = []

    def cursor(self):
        return _Cursor(self)


class _Outbox:
    def __init__(self, accept=True):
        self.accept = accept
        self.calls = []

    def enqueue(self, **kwargs):
        self.calls.append(kwargs)
        return self.accept


def _task_conn():
    # 2026-10-16 三桶口径：绍兴=全区（含李树军 142910），其他=省外+总经办
    facts = {
        ("hangzhou", None): {date(2026, 9, 23): 20000.0, date(2026, 9, 22): 10000.0},
        ("shaoxing", None): {date(2026, 9, 23): 5000.0,
                             date(2026, 9, 17): 142910.0},
        ("other", None): {date(2026, 9, 17): 4841587.0},
    }
    targets = {
        ("hangzhou", None): 100000.0,
        ("shaoxing", None): None,
        ("other", None): 3195000.0,   # 省外 100万+总经办 219.5万
    }
    conn = _Conn(facts, targets)
    # 补集人员行（余云涛：offline_extra 遗留行形，rp=板块/dept=责任人）
    conn.other_rows = [
        {"region": "offline_extra", "rp": "省外", "dept": "余云涛",
         "business_date": date(2026, 9, 22), "sales_amount": 1519440.0,
         "monthly_target": 1000000.0},
    ]
    return conn


class TaskTest(unittest.TestCase):
    def test_daily_enqueues_and_upserts_agg(self):
        conn = _task_conn()
        outbox = _Outbox()
        status = run_daily_summary(
            conn, outbox,
            business_date=date(2026, 9, 23), now=datetime(2026, 9, 23, 20, 30),
        )
        self.assertEqual(status, "enqueued")
        # agg 落表：3 桶（杭州/绍兴/其他）+ 整体 = 4 行（2026-10-16 三桶口径）
        self.assertEqual(len(conn.agg_rows), 4)
        scopes = {row[1] for row in conn.agg_rows}
        self.assertEqual(
            scopes,
            {"hangzhou", "shaoxing", "other", TOTAL_SCOPE_KEY},
        )
        by_scope = {row[1]: row for row in conn.agg_rows}
        # 其他桶 9-23 无报数 → 当日 0；月累计=省外+总经办 9-17 合计
        self.assertEqual(by_scope["other"][2], 0.0)
        self.assertEqual(by_scope["other"][9], 4841587.0)
        # 元组索引 10 = month_target（11 为 month_rate）
        self.assertEqual(by_scope["other"][10], 3195000.0)
        # 绍兴含李树军（拆分退役）：9-23 当日 5000，月累计 5000+142910
        self.assertEqual(by_scope["shaoxing"][2], 5000.0)
        self.assertEqual(by_scope["shaoxing"][9], 147910.0)
        # 整体月目标 = 100000 + 3195000（绍兴 None 跳过）
        self.assertEqual(by_scope[TOTAL_SCOPE_KEY][10], 3295000.0)

        self.assertEqual(len(outbox.calls), 1)
        call = outbox.calls[0]
        self.assertEqual(call["region"], "offline_all")
        self.assertEqual(call["kind"], DAILY_KIND)
        self.assertEqual(call["business_date"], date(2026, 9, 23))
        self.assertEqual(call["title"], "线下整体日报")
        self.assertIn("【线下整体日报】9月23日（周三）", call["body_md"])

    def test_daily_second_run_already_sent_but_agg_refreshes(self):
        conn = _task_conn()
        outbox = _Outbox(accept=False)
        status = run_daily_summary(
            conn, outbox,
            business_date=date(2026, 9, 23), now=datetime(2026, 9, 23, 21, 0),
        )
        self.assertEqual(status, "already_sent")
        self.assertEqual(len(conn.agg_rows), 4)

    def test_weekly_uses_previous_full_week(self):
        conn = _task_conn()
        outbox = _Outbox()
        status = run_weekly_summary(
            conn, outbox,
            reference=date(2026, 9, 28), now=datetime(2026, 9, 28, 9, 30),
        )
        self.assertEqual(status, "enqueued")
        call = outbox.calls[0]
        self.assertEqual(call["kind"], WEEKLY_KIND)
        self.assertEqual(call["business_date"], date(2026, 9, 21))
        self.assertEqual(call["title"], "线下整体周报")
        self.assertIn("【线下整体周报】9月21日（周一） ~ 9月27日（周日）", call["body_md"])

    def test_monthly_uses_previous_month(self):
        conn = _task_conn()
        outbox = _Outbox()
        status = run_monthly_summary(
            conn, outbox,
            reference=date(2026, 10, 1), now=datetime(2026, 10, 1, 10, 0),
        )
        self.assertEqual(status, "enqueued")
        call = outbox.calls[0]
        self.assertEqual(call["kind"], MONTHLY_KIND)
        self.assertEqual(call["business_date"], date(2026, 9, 1))
        self.assertEqual(call["title"], "线下整体月报")
        self.assertIn("【线下整体月报】2026年9月", call["body_md"])

    def test_queries_exclude_heji_rows(self):
        """AI 表自带合计行（杭州合计/余杭合计…），查询必须排除，否则双倍计数。"""
        conn = _task_conn()
        outbox = _Outbox()
        run_daily_summary(
            conn, outbox,
            business_date=date(2026, 9, 23), now=datetime(2026, 9, 23, 20, 30),
        )
        select_sql = [s for s in conn.sql_log if "fact_daily_report_offline" in s]
        self.assertTrue(select_sql)
        for sql in select_sql:
            # 代码内为 pymysql % 转义形态（%%合计%%）
            self.assertIn("NOT LIKE '%%合计%%'", sql)

    def test_scope_table_is_config_driven(self):
        """三桶口径：杭州/绍兴/其他（2026-10-20：其他=显式 region='other'）。"""
        keys = [scope for scope, _, _, _ in AGG_SCOPES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(["hangzhou", "shaoxing", "other"], keys)
        anchors = {scope: anchor for scope, _, _, anchor in AGG_SCOPES}
        self.assertEqual(anchors["shaoxing"], None)   # 李树军拆分退役，并入绍兴
        regions = {scope: region for scope, _, region, _ in AGG_SCOPES}
        # 2026-10-20 用户裁决：其他=显式 region='other'（画布权威源），不再补集
        self.assertEqual(regions["other"], "other")
        # 人员榜补集排除集仍含 qudao（_collect_other_people 用，2026-10-17 修复）
        from common.daily_robot.offline_summary import _OFFLINE_OTHER_EXCLUDES
        self.assertEqual(
            set(_OFFLINE_OTHER_EXCLUDES),
            {"hangzhou", "shaoxing", "vanke", "qudao"})

    def test_daily_markdown_contains_other_row(self):
        """「其他」桶成行（省外+总经办并入）；李树军不再单独成行。"""
        conn = _task_conn()
        outbox = _Outbox()
        run_daily_summary(
            conn, outbox,
            business_date=date(2026, 9, 23), now=datetime(2026, 9, 23, 20, 30),
        )
        body = outbox.calls[0]["body_md"]
        self.assertIn("| 其他 |", body)
        self.assertNotIn("| 李树军 |", body)
        # 其他月累计 484.2万（4841587）
        self.assertIn("484.2万", body)


class OverviewRowTest(unittest.TestCase):
    """页面总览行：当日（对昨日）+ 本周累计（对上周同期）+ 本月（对上月同期）。"""

    def test_overview_row_three_periods(self):
        from common.daily_robot.offline_summary import compute_overview_row

        facts = {
            date(2026, 9, 22): 100.0,   # 报告日当日
            date(2026, 9, 21): 50.0,    # 昨日
            date(2026, 9, 17): 20.0,    # 上周（不计入周环比基期）
            date(2026, 9, 15): 30.0,    # 上周同期（9-14~9-15）
            date(2026, 8, 22): 40.0,    # 上月同期（8-1~8-22）
        }
        m = compute_overview_row(
            "hangzhou", "杭州", facts=facts,
            report_day=date(2026, 9, 22), data_date=date(2026, 9, 22),
            month_target=1000.0,
        )
        self.assertEqual(m.day_sales, 100.0)
        self.assertEqual(m.day_base, 50.0)
        self.assertAlmostEqual(m.dod_rate, 1.0)
        # 本周（周一 9-21 起）累计 vs 上周同期（9-14~9-15）
        self.assertEqual(m.week_sales, 150.0)
        self.assertEqual(m.week_base, 30.0)
        self.assertAlmostEqual(m.wow_rate, 4.0)
        # 月累计含全月已填报日；月环比对上月 1~22 日
        self.assertEqual(m.month_completed, 200.0)
        self.assertEqual(m.month_base, 40.0)
        self.assertAlmostEqual(m.mom_rate, 4.0)
        self.assertAlmostEqual(m.month_rate, 0.2)

    def test_overview_row_zero_base_rate_none(self):
        from common.daily_robot.offline_summary import compute_overview_row

        m = compute_overview_row(
            "shaoxing", "绍兴", facts={date(2026, 9, 22): 100.0},
            report_day=date(2026, 9, 22), data_date=date(2026, 9, 22),
            month_target=None,
        )
        # 基期为 0 → 比率 None（页面记 --），但基期值本身保留可展示
        self.assertIsNone(m.dod_rate)
        self.assertEqual(m.day_base, 0.0)
        self.assertIsNone(m.wow_rate)
        self.assertIsNone(m.mom_rate)
        self.assertIsNone(m.month_rate)


class OfflineAllHtmlTest(unittest.TestCase):
    """pages-offline_all：人员总榜（三区合并，无人例外）+ 日/周/月总览表。"""

    def _fake_people(self, region):
        rows = {
            "hangzhou": [
                {"name": "张三", "dept": "杭中", "target": 100.0,
                 "completed": 50.0, "unfilled": 0, "rate": 0.5},
            ],
            "shaoxing": [
                {"name": "李树军", "dept": "线下运营中心", "target": 0,
                 "completed": 142910.0, "unfilled": 0, "rate": None},
            ],
            "offline_extra": [
                {"name": "省外", "dept": "余云涛", "target": 1000000.0,
                 "completed": 1519440.0, "unfilled": 0, "rate": 1.51944},
            ],
        }
        from common.daily_robot.mart_leaderboard import LeaderboardData
        return LeaderboardData(
            business_date=date(2026, 9, 23),
            elapsed=(22,),
            people=rows[region],
            workdays=frozenset({date(2026, 9, 22)}),
        )

    def test_collect_offline_all_people_merge_and_realign(self):
        """播报/页面共享采集：三区合并 + 姓名↔部门互换 + 通讯录部门优先。"""
        conn = _task_conn()
        conn.members[("hangzhou", "张三")] = "杭中（通讯录）"
        with mock.patch(
            "common.daily_robot.mart_leaderboard.mart_collect",
            side_effect=lambda connection, *, region, business_date,
            include_today=False: (
                self._fake_people(region)
            ),
        ):
            people, workdays = collect_offline_all_people(
                conn, business_date=date(2026, 9, 23)
            )

        self.assertEqual(workdays, frozenset({date(2026, 9, 22)}))
        # 排序同 mart_collect 键（rate 降序、None 垫底）
        self.assertEqual(
            [p["name"] for p in people], ["余云涛", "张三", "李树军"]
        )
        by_name = {p["name"]: p for p in people}
        # offline_extra 行「姓名↔部门」归位：责任人余云涛为名、板块省外为部门
        self.assertEqual(by_name["余云涛"]["dept"], "省外")
        # 杭/绍部门以通讯录为准，无匹配保留表内值
        self.assertEqual(by_name["张三"]["dept"], "杭中（通讯录）")
        self.assertEqual(by_name["李树军"]["dept"], "线下运营中心")

    def test_people_merged_no_exception_and_overview_panel(self):
        captured = {}

        def fake_build_html(view, now, elapsed, people, extra_panels=None, **kw):
            captured["elapsed"] = elapsed
            captured["people"] = people
            captured["extra_panels"] = extra_panels
            return "<html>offline_all</html>"

        conn = _task_conn()
        conn.members[("hangzhou", "张三")] = "杭中（通讯录）"
        with mock.patch(
            "common.daily_robot.mart_leaderboard.mart_collect",
            side_effect=lambda connection, *, region, business_date,
            include_today=False: (
                self._fake_people(region)
            ),
        ), mock.patch(
            "common.daily_robot.mart_leaderboard.build_leaderboard_view",
            return_value={"region": {}, "calendar": {}},
        ), mock.patch(
            "common.daily_robot.leaderboard.build_html",
            side_effect=fake_build_html,
        ):
            page = build_offline_all_html(
                conn, None,
                business_date=date(2026, 9, 23),
                now=datetime(2026, 9, 23, 8, 30),
            )

        self.assertEqual(page, "<html>offline_all</html>")

        # 人员总榜：三区合并、排序同 mart_collect 键（rate 降序、None 垫底）
        people = captured["people"]
        self.assertEqual(len(people), 3)
        # offline_extra 行「姓名↔部门」归位：责任人余云涛为名、板块省外为部门
        self.assertEqual(
            [p["name"] for p in people], ["余云涛", "张三", "李树军"]
        )
        by_name = {p["name"]: p for p in people}
        self.assertEqual(by_name["余云涛"]["dept"], "省外")
        # 杭/绍部门以通讯录为准：张三被 dim 覆盖，李树军无 dim 匹配保留表内值
        self.assertEqual(by_name["张三"]["dept"], "杭中（通讯录）")
        self.assertEqual(by_name["李树军"]["dept"], "线下运营中心")
        # 覆盖区域登记=杭/绍（其他桶走补集采集，不走 mart_collect）
        from common.daily_robot.offline_summary import OFFLINE_PEOPLE_REGIONS
        self.assertEqual(
            OFFLINE_PEOPLE_REGIONS, ("hangzhou", "shaoxing")
        )

        # 总览面板：单 panel 单表（日/周/月三期同行，环比带基期，无 tab）
        panels = captured["extra_panels"]
        self.assertEqual(len(panels), 1)
        panel = panels[0]
        self.assertIn('<div class="panel">', panel)
        self.assertNotIn('id="dim-tabs"', panel)
        for col in ("当日", "日环比", "本周累计", "周环比",
                    "月累计", "月目标", "达成率", "月环比"):
            self.assertIn(f"<th>{col}</th>", panel)
        for base_label in ("昨日", "上周同期", "上月同期"):
            self.assertIn(base_label, panel)
        # 报告日锚定 9-22（fake 里 9-23 有数据，T-1 后不可见）
        self.assertIn("报告日 9月22日", panel)
        self.assertIn("本周 9月21日 至 9月22日", panel)
        self.assertNotIn("9月23日", panel)
        # 杭州月累计只含 9-22 的 1.0万（9-23 的 2.0万被 T-1 排除）
        self.assertIn("1.0万", panel)
        self.assertNotIn(">2.0万<", panel)
        # 三桶板块行（杭州/绍兴/其他）+ 整体行（2026-10-16 口径）
        self.assertIn("其他", panel)
        self.assertIn("线下整体", panel)


# ---------------------------------------------------------------------------
# 部门归位覆盖并入（2026-10-19 用户裁决：BI 与画布同真源）
# ---------------------------------------------------------------------------

def _ovr_row(user_id, region, dept_name=None, name=None):
    """dim_org_assignment_override 字典行（dict 形态，_as_override 按 key 取）。"""
    return {
        "id": 1, "user_id": user_id, "name": name, "region": region,
        "dept_id": None, "dept_name": dept_name, "enabled": 1,
        "note": None, "updated_by": "ops", "updated_at": None,
    }


class _DeptMapCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        if "dim_org_assignment_override" in sql:
            if self._conn.boom:
                raise RuntimeError("db down")
            self._rows = self._conn.override_rows
        elif "dim_robot_member" in sql and "`user_id` IN" in sql:
            # 移入在职校验：全部在职
            self._rows = [{"user_id": u} for u in self._conn.active_ids]
        elif "dim_robot_member" in sql:
            self._rows = self._conn.base_rows
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _DeptMapConn:
    def __init__(self, base_rows, override_rows=(), *, boom=False,
                 active_ids=("u9",)):
        self.base_rows = base_rows
        self.override_rows = override_rows
        self.boom = boom
        self.active_ids = active_ids

    def cursor(self):
        return _DeptMapCursor(self)


class DeptMapOverrideTests(unittest.TestCase):
    """2026-10-19 裁决：画布归位覆盖对 BI 部门卡生效（杭州销售部案例）。"""

    _BASE = [
        {"user_id": "u1", "region": "hangzhou", "name": "余发兴",
         "dept_id": "1049728636", "dept_name": "杭中项目部"},
        {"user_id": "u2", "region": "hangzhou", "name": "黄晓怡",
         "dept_id": "1049728636", "dept_name": "杭中项目部"},
    ]

    def test_override_dept_wins_within_region(self):
        from common.daily_robot.offline_summary import fetch_member_dept_map
        conn = _DeptMapConn(self._BASE, [
            _ovr_row("u1", "hangzhou", dept_name="杭州销售部", name="余发兴"),
        ])
        result = fetch_member_dept_map(conn, ("hangzhou",))
        # 余发兴归位到杭州销售部（画布拉线口径），黄晓怡维持通讯录
        self.assertEqual("杭州销售部", result[("hangzhou", "余发兴")])
        self.assertEqual("杭中项目部", result[("hangzhou", "黄晓怡")])

    def test_cross_region_move_out_and_in(self):
        from common.daily_robot.offline_summary import fetch_member_dept_map
        conn = _DeptMapConn(self._BASE, [
            # 李四不在 hangzhou 基名单，覆盖移入 hangzhou·杭州销售部
            _ovr_row("u9", "hangzhou", dept_name="杭州销售部", name="李四"),
            # 黄晓怡移出杭州（region=None 语义=移出）
            _ovr_row("u2", None, name="黄晓怡"),
        ])
        result = fetch_member_dept_map(conn, ("hangzhou",))
        self.assertEqual("杭州销售部", result[("hangzhou", "李四")])
        self.assertNotIn(("hangzhou", "黄晓怡"), result)

    def test_override_read_failure_fails_soft(self):
        from common.daily_robot.offline_summary import fetch_member_dept_map
        conn = _DeptMapConn(self._BASE, boom=True)
        result = fetch_member_dept_map(conn, ("hangzhou",))
        self.assertEqual("杭中项目部", result[("hangzhou", "余发兴")])


class _FactCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        return None

    def execute(self, sql, params=None):
        if "MAX(`monthly_target`)" in sql:
            self._rows = self._conn.target_rows
        else:
            self._rows = self._conn.fact_rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FactConn:
    def __init__(self, fact_rows, target_rows):
        self.fact_rows = fact_rows
        self.target_rows = target_rows

    def cursor(self):
        return _FactCursor(self)


class DeptSectionTests(unittest.TestCase):
    """日报「部门」部分：画布口径聚合 + 门店回退事实表 department。"""

    def test_markdown_two_sections(self):
        rows, total = _daily_rows()
        day = date(2026, 9, 23)
        dept_rows = [
            compute_daily_metrics(
                "杭中项目部", "杭中项目部", facts={day: 15000.0},
                business_date=day, month_target=None),
            compute_daily_metrics(
                "诸暨项目部", "诸暨项目部", facts={day: 5000.0},
                business_date=day, month_target=None),
        ]
        body = build_daily_markdown(
            business_date=day, rows=rows, total=total,
            dept_rows=dept_rows)
        self.assertIn("**一、区域**", body)
        self.assertIn("**二、部门（杭州 / 绍兴）**", body)
        self.assertIn(
            "| 部门 | 今日 | 昨日 | 日环比 | 本周 | 上周 | 周环比 | 月累计 "
            "| 月目标 | 上月累计 | 上月目标 | 本月达成率 | 上月达成率 |", body)
        self.assertIn("| 杭中项目部 | 1.5万 |", body)
        self.assertIn("| 诸暨项目部 | 5,000 |", body)
        # 区域表合计仍是线下整体
        self.assertIn("| **线下整体** | 2.0万 |", body)
        # 部门表不带合计行（2026-10-19 用户裁决去掉「杭州+绍兴」合计）
        dept_section = body.split("**二、部门", 1)[1]
        self.assertNotIn("**线下整体**", dept_section)
        self.assertNotIn("杭州+绍兴", dept_section)

    def test_bi_link_appended(self):
        rows, total = _daily_rows()
        body = build_daily_markdown(
            business_date=date(2026, 9, 23), rows=rows, total=total,
            bi_url="http://203.205.91.241:8300/offline_all.html")
        self.assertIn(
            "📊 [点击查看线下整体 BI 看板]"
            "(<http://203.205.91.241:8300/offline_all.html>)", body)
        # 不传 bi_url 则不出链接行（向后兼容）
        body_no = build_daily_markdown(
            business_date=date(2026, 9, 23), rows=rows, total=total)
        self.assertNotIn("BI 看板", body_no)

    def test_canvas_dept_wins_and_store_falls_back(self):
        day = date(2026, 10, 19)
        conn = _FactConn(
            fact_rows=[
                {"d": day, "p": "余发兴", "fd": "杭中项目部", "s": 100.0},
                {"d": day, "p": "诸暨门店", "fd": "诸暨项目部", "s": 200.0},
            ],
            target_rows=[
                {"p": "余发兴", "fd": "杭中项目部", "t": 1000.0},
                {"p": "诸暨门店", "fd": "诸暨项目部", "t": 124000.0},
            ])
        dept_of = {"余发兴": "杭州销售部"}  # 画布归位覆盖通讯录
        facts = fetch_dept_daily_facts(
            conn, start=date(2026, 10, 1), end=day, dept_of=dept_of)
        self.assertEqual(100.0, facts["杭州销售部"][day])
        self.assertNotIn("杭中项目部", facts)   # 画布口径优先，不被表内部门覆盖
        self.assertEqual(200.0, facts["诸暨项目部"][day])  # 门店回退可归位

        targets = fetch_dept_month_targets(
            conn, year=2026, month=10, dept_of=dept_of)
        self.assertEqual(1000.0, targets["杭州销售部"])
        self.assertEqual(124000.0, targets["诸暨项目部"])

    def test_unmapped_falls_into_placeholder_bucket(self):
        day = date(2026, 10, 19)
        conn = _FactConn(
            fact_rows=[{"d": day, "p": "无名氏", "fd": None, "s": 50.0}],
            target_rows=[])
        facts = fetch_dept_daily_facts(
            conn, start=date(2026, 10, 1), end=day, dept_of={})
        self.assertEqual(50.0, facts["(未归位)"][day])

    def test_strict_canvas_rejects_dirty_department(self):
        """严格只认画布：回退的历史脏部门名落未归位桶，不单独成行。

        用户裁决（2026-10-19）：部门表只出现画布上存在的部门。门店的
        ``department`` 是干净的「诸暨项目部」，在画布集合内 → 正常归位；
        「谢坚钰」（人名当部门）、「滨萧」（旧简写）不在 → 并入未归位桶。
        """
        day = date(2026, 10, 19)
        conn = _FactConn(
            fact_rows=[
                {"d": day, "p": "诸暨门店", "fd": "诸暨项目部", "s": 200.0},
                {"d": day, "p": "谢坚钰", "fd": "谢坚钰", "s": 300.0},
                {"d": day, "p": "旧数据", "fd": "滨萧", "s": 400.0},
            ],
            target_rows=[])
        canvas_names = {"诸暨项目部", "绍兴项目部", "杭州销售部"}
        facts = fetch_dept_daily_facts(
            conn, start=date(2026, 10, 1), end=day, dept_of={},
            canvas_names=canvas_names)
        self.assertEqual(200.0, facts["诸暨项目部"][day])  # 门店：在画布内
        self.assertEqual(700.0, facts[UNMAPPED_DEPT][day])  # 脏值并入未归位
        self.assertNotIn("谢坚钰", facts)
        self.assertNotIn("滨萧", facts)


if __name__ == "__main__":
    unittest.main()
