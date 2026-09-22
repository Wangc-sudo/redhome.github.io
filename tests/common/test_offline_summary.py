# -*- coding: utf-8 -*-
"""offline_summary（线下整体日/周/月报）单测：fake conn + 纯函数断言。"""

import unittest
from datetime import date, datetime

from common.daily_robot.offline_summary import (
    AGG_SCOPES,
    DAILY_KIND,
    MONTHLY_KIND,
    TOTAL_SCOPE_KEY,
    WEEKLY_KIND,
    build_daily_markdown,
    build_monthly_markdown,
    build_weekly_markdown,
    compute_daily_metrics,
    compute_monthly_metrics,
    compute_weekly_metrics,
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
        # 日环比：对前一自然日（9-22）
        self.assertEqual(m.dod_amount, 150.0)
        self.assertAlmostEqual(m.dod_rate, 1.5)
        # 周环比：对上周同星期几（9-16）
        self.assertEqual(m.wow_amount, 200.0)
        self.assertAlmostEqual(m.wow_rate, 4.0)
        # 月累计 = 9-16 + 9-22 + 9-23
        self.assertEqual(m.month_completed, 400.0)
        self.assertEqual(m.month_target, 1000.0)
        self.assertAlmostEqual(m.month_rate, 0.4)
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
        self.assertIn(
            "| 板块 | 今日 | 日环比 | 周环比 | 月累计 | 月目标 | 达成率 |", body,
        )
        self.assertIn("| 杭州 | 2.0万 | +100.0% | -- | 3.0万 | 10.0万 | 30.0% |", body)
        self.assertIn("| 绍兴 | 0 | -- | -- | 0 | -- | -- |", body)
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
        region = params[0]
        person = params[3] if len(params) > 3 else None
        if "MAX(`monthly_target`)" in sql:
            self._rows = [
                {"t": self._conn.targets.get((region, person))}
            ]
        else:
            facts = self._conn.facts.get((region, person), {})
            self._rows = [
                {"d": d, "s": amount} for d, amount in facts.items()
            ]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, facts, targets):
        self.facts = facts        # {(region, person): {date: amount}}
        self.targets = targets    # {(region, person): float|None}
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
    facts = {
        ("hangzhou", None): {date(2026, 9, 23): 20000.0, date(2026, 9, 22): 10000.0},
        ("shaoxing", None): {date(2026, 9, 23): 5000.0},
        ("offline_extra", "省外"): {date(2026, 9, 17): 1519440.0},
        ("offline_extra", "线下总经办"): {date(2026, 9, 17): 3322147.0},
    }
    targets = {
        ("hangzhou", None): 100000.0,
        ("shaoxing", None): None,
        ("offline_extra", "省外"): 1000000.0,
        ("offline_extra", "线下总经办"): 2195000.0,
    }
    return _Conn(facts, targets)


class TaskTest(unittest.TestCase):
    def test_daily_enqueues_and_upserts_agg(self):
        conn = _task_conn()
        outbox = _Outbox()
        status = run_daily_summary(
            conn, outbox,
            business_date=date(2026, 9, 23), now=datetime(2026, 9, 23, 20, 30),
        )
        self.assertEqual(status, "enqueued")
        # agg 落表：4 板块 + 整体 = 5 行
        self.assertEqual(len(conn.agg_rows), 5)
        scopes = {row[1] for row in conn.agg_rows}
        self.assertEqual(
            scopes,
            {"hangzhou", "shaoxing", "shengwai", "zongjingban", TOTAL_SCOPE_KEY},
        )
        # 省外 9-23 无报数 → 0；线下总经办月目标 2195000
        by_scope = {row[1]: row for row in conn.agg_rows}
        self.assertEqual(by_scope["shengwai"][2], 0.0)
        # 元组索引 10 = month_target（11 为 month_rate）
        self.assertEqual(by_scope["zongjingban"][10], 2195000.0)
        # 整体月目标 = 100000 + 1000000 + 2195000（绍兴 None 跳过）
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
        self.assertEqual(len(conn.agg_rows), 5)

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
            self.assertIn("NOT LIKE '%合计%'", sql)

    def test_scope_table_is_config_driven(self):
        keys = [scope for scope, _, _, _ in AGG_SCOPES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn("shengwai", keys)
        self.assertIn("zongjingban", keys)


if __name__ == "__main__":
    unittest.main()
