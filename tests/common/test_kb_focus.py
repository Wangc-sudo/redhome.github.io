# -*- coding: utf-8 -*-
"""问句理解层（focus）单测：期间/同比/粒度解析、边界直答、聚焦压缩。

方案 §3 A1/A2、§4 B1。全部纯函数 + 假连接，零 IO。
"""

import unittest
from datetime import date, datetime

from common.kb_qa import focus as focus_module
from common.kb_qa.focus import (
    GRANULARITY_CHANNEL,
    GRANULARITY_SKU,
    GRANULARITY_STORE,
    boundary_reply,
    compress_rows,
    fetch_data_start,
    parse_focus,
    parse_period,
    resolve_boundary_reply,
)

_NOW = date(2026, 11, 2)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []
        self._index = 0

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchone(self):
        if isinstance(self._rows, list):
            return self._rows[0] if self._rows else None
        return self._rows

    def fetchall(self):
        return self._rows if isinstance(self._rows, list) else [self._rows]

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows):
        self.cursor_instance = _FakeCursor(rows)

    def cursor(self):
        return self.cursor_instance


class PeriodParseTests(unittest.TestCase):
    def test_this_month(self):
        period = parse_period("本月销售额多少", now=_NOW)
        self.assertEqual(date(2026, 11, 1), period.start)
        self.assertEqual(date(2026, 11, 2), period.end)

    def test_last_month_full_range(self):
        period = parse_period("上月销售额", now=_NOW)
        self.assertEqual(date(2026, 10, 1), period.start)
        self.assertEqual(date(2026, 10, 31), period.end)

    def test_bare_month_falls_in_current_year(self):
        period = parse_period("9月销售及同比", now=_NOW)
        self.assertEqual(date(2026, 9, 1), period.start)
        self.assertEqual(date(2026, 9, 30), period.end)

    def test_bare_month_next_year_rolls_back(self):
        period = parse_period("12月怎么样", now=date(2026, 1, 5))
        self.assertEqual(date(2025, 12, 1), period.start)
        self.assertEqual(date(2025, 12, 31), period.end)

    def test_year_month(self):
        period = parse_period("2025年9月同比", now=_NOW)
        self.assertEqual(date(2025, 9, 1), period.start)
        self.assertEqual(date(2025, 9, 30), period.end)

    def test_yesterday(self):
        period = parse_period("昨天杭州出了多少货", now=_NOW)
        self.assertEqual(date(2026, 11, 1), period.start)
        self.assertEqual(date(2026, 11, 1), period.end)

    def test_recent_days(self):
        period = parse_period("近7天销量", now=_NOW)
        self.assertEqual(date(2026, 10, 27), period.start)
        self.assertEqual(date(2026, 11, 2), period.end)

    def test_explicit_range(self):
        period = parse_period("2026-09-01 到 2026-09-30 销售额", now=_NOW)
        self.assertEqual(date(2026, 9, 1), period.start)
        self.assertEqual(date(2026, 9, 30), period.end)

    def test_unknown_returns_none(self):
        self.assertIsNone(parse_period("习酒窖藏1988 箱规", now=_NOW))
        self.assertIsNone(parse_period("", now=_NOW))


class FocusParseTests(unittest.TestCase):
    def test_yoy_builds_previous_period(self):
        focus = parse_focus("9月销售及同比", now=_NOW)
        self.assertTrue(focus.yoy)
        self.assertEqual(date(2025, 9, 1), focus.yoy_period.start)
        self.assertEqual(date(2025, 9, 30), focus.yoy_period.end)

    def test_no_yoy_keyword(self):
        focus = parse_focus("9月销售额", now=_NOW)
        self.assertFalse(focus.yoy)
        self.assertIsNone(focus.yoy_period)

    def test_granularity(self):
        self.assertEqual(GRANULARITY_SKU,
                         parse_focus("各款销量排名").granularity)
        self.assertEqual(GRANULARITY_SKU,
                         parse_focus("按SKU维度看下销售").granularity)
        self.assertEqual(GRANULARITY_STORE,
                         parse_focus("各店销售额").granularity)
        self.assertEqual(GRANULARITY_CHANNEL,
                         parse_focus("各渠道占比").granularity)
        self.assertIsNone(parse_focus("习酒窖藏1988 箱规").granularity)

    def test_channels_named_in_question(self):
        focus = parse_focus("京东9月销售同比", now=_NOW)
        self.assertEqual(("京东",), focus.channels)

    def test_never_raises(self):
        for text in (None, "", "  ", "同比", "月", "2026-13-01"):
            self.assertIsInstance(parse_focus(text, now=_NOW),
                                  focus_module.Focus)


class BoundaryTests(unittest.TestCase):
    def test_yoy_before_data_start_answers_directly(self):
        focus = parse_focus("9月销售及同比", now=_NOW)
        reply = boundary_reply(focus, date(2026, 8, 22))
        self.assertIsNotNone(reply)
        self.assertIn("2026-08-22", reply)
        self.assertIn("没有去年数据", reply)

    def test_yoy_within_data_start_runs_tool(self):
        # 数据起点 2022-06：2025-09 基期在库内 → 正常跑工具
        focus = parse_focus("9月销售及同比", now=_NOW)
        self.assertIsNone(boundary_reply(focus, date(2022, 6, 1)))

    def test_fail_open_without_data_start(self):
        focus = parse_focus("9月销售及同比", now=_NOW)
        self.assertIsNone(boundary_reply(focus, None))
        self.assertIsNone(boundary_reply(None, date(2026, 8, 22)))

    def test_non_yoy_question_never_short_circuits(self):
        focus = parse_focus("9月销售额", now=_NOW)
        self.assertIsNone(boundary_reply(focus, date(2026, 8, 22)))

    def test_fetch_data_start(self):
        conn = _FakeConn({"min_value": datetime(2026, 8, 22, 9, 30)})
        self.assertEqual(date(2026, 8, 22), fetch_data_start(conn))
        sql, _ = conn.cursor_instance.executed[-1]
        self.assertIn("MIN(`trade_time`)", sql)
        self.assertIn("`fact_order_line`", sql)

    def test_fetch_data_start_fail_open(self):
        conn = _FakeConn({"min_value": None})
        self.assertIsNone(fetch_data_start(conn))

        class _Boom:
            def cursor(self):
                raise RuntimeError("down")
        self.assertIsNone(fetch_data_start(_Boom()))

    def test_resolve_skips_probe_without_yoy(self):
        conn = _FakeConn({"min_value": datetime(2026, 8, 22)})
        self.assertIsNone(resolve_boundary_reply(conn, "9月销售额", now=_NOW))
        self.assertEqual([], conn.cursor_instance.executed)

    def test_resolve_probes_only_for_yoy(self):
        conn = _FakeConn({"min_value": datetime(2026, 8, 22)})
        reply = resolve_boundary_reply(conn, "9月销售及同比", now=_NOW)
        self.assertIn("没有去年数据", reply)
        self.assertEqual(1, len(conn.cursor_instance.executed))


class CompressRowsTests(unittest.TestCase):
    @staticmethod
    def _rows():
        return [
            {"channel_key": "京东", "shop_name": "京东A店",
             "current_sales_amount": 100},
            {"channel_key": "拼多多", "shop_name": "PDD B店",
             "current_sales_amount": 90},
            {"channel_key": "抖音", "shop_name": "抖音C店",
             "current_sales_amount": 80},
            {"channel_key": "猫超", "shop_name": "猫超D店",
             "current_sales_amount": 70},
            {"channel_key": "私域", "shop_name": "私域E店",
             "current_sales_amount": 60},
        ]

    def test_keeps_named_entity_and_top_rows(self):
        rows = self._rows()
        kept, summary = compress_rows(
            rows, question="猫超D店9月销售怎么样", top_n=3)
        names = [row["shop_name"] for row in kept]
        self.assertIn("猫超D店", names)
        self.assertEqual(4, len(kept))          # 命中行 + TOP 3
        self.assertEqual(1, summary["omitted"])
        self.assertEqual(60, summary["total_amount"])
        # 入参（导出用）保持全量
        self.assertEqual(5, len(rows))

    def test_no_entity_small_result_kept_whole(self):
        # 「各渠道对比」这类问句要看全量：没点名对象 + 行数可控 → 不压
        rows = self._rows()
        kept, summary = compress_rows(rows, question="本月各渠道怎么样",
                                      top_n=3)
        self.assertEqual(5, len(kept))
        self.assertIsNone(summary)

    def test_no_entity_large_result_trimmed(self):
        rows = [
            {"channel_key": f"渠道{i}", "shop_name": f"店{i}",
             "current_sales_amount": 100 - i}
            for i in range(20)
        ]
        kept, summary = compress_rows(rows, question="本月销售怎么样", top_n=3)
        self.assertEqual(3, len(kept))
        self.assertEqual(17, summary["omitted"])

    def test_focus_channels_count_as_entity(self):
        rows = self._rows()
        focus = parse_focus("京东", now=_NOW)
        kept, _ = compress_rows(rows, question="销售怎么样", focus=focus,
                               top_n=3)
        self.assertIn("京东A店", [row["shop_name"] for row in kept])

    def test_small_result_untouched(self):
        rows = self._rows()[:2]
        kept, summary = compress_rows(rows, question="销售", top_n=3)
        self.assertEqual(rows, kept)
        self.assertIsNone(summary)

    def test_empty(self):
        self.assertEqual(([], None), compress_rows([], question="销售"))


if __name__ == "__main__":
    unittest.main()
