# -*- coding: utf-8 -*-
"""sales_report 运营报表工具单测（2026-11-02 真机 badcase 驱动）。

背景：「帮我做一个10月份的运营周报」走 7 次 query_sql、52 秒轮尽兜底。
报表是已知模式 → 预定义 SQL 一次拿全。测试用路由假连接断言
「SQL 形态 + 参数 + 环比口径 + fail-soft」，不连真库。
"""

import unittest
from datetime import date, datetime

from common.kb_qa.agent import TOOLS, ToolExecutor, _SYSTEM_PROMPT
from common.kb_qa.query_export import DEFAULT_ALLOWED_TOOLS
from common.kb_qa.skills import business_tool_names


class _RouterCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []
        self._fail_next = False

    def execute(self, sql, params=()):
        self._conn.executed.append((" ".join(sql.split()), params))
        if self._conn.fail_on and self._conn.fail_on in sql:
            raise RuntimeError("boom")
        self._rows = list(self._conn.route(sql, params) or [])

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RouterConn:
    def __init__(self, route, fail_on=None):
        self.route = route
        self.fail_on = fail_on
        self.executed = []
        self.cursor_instance = _RouterCursor(self)

    def cursor(self):
        return self.cursor_instance

    @property
    def sqls(self):
        return [sql for sql, _ in self.executed]


_RULES = [{
    "id": 1, "channel_key": "京东", "channel_label": "京东",
    "panel": "channel", "business_line": "online",
    "match_type": "shop", "match_mode": "contains",
    "match_value": "京东", "priority": 1, "note": "", "enabled": 1,
}]


def _route(totals=None, channels=None, stores=None, skus=None,
           inventory=None, rules=_RULES):
    def route(sql, params):
        if "COUNT(DISTINCT CASE" in sql:
            return [totals or {
                "cur_amount": 1000, "prev_amount": 500,
                "cur_qty": 10, "prev_qty": 8,
                "cur_orders": 4, "prev_orders": 3}]
        if "GROUP BY `shop_name`, `buyer_nick`" in sql:
            return channels if channels is not None else [
                {"shop_name": "杭易-京东购喝旗舰店", "buyer_nick": "b1",
                 "amount": 600, "orders": 3},
                {"shop_name": "未知散店", "buyer_nick": "b2",
                 "amount": 400, "orders": 1}]
        if "GROUP BY `shop_name` ORDER BY" in sql:
            return stores if stores is not None else [
                {"shop_name": "杭易-京东购喝旗舰店", "orders": 3,
                 "quantity": 6, "amount": 600}]
        if "GROUP BY `spec_no`, `goods_name`" in sql:
            return skus if skus is not None else [
                {"spec_no": "习酒759", "goods_name": "习酒窖藏1988",
                 "quantity": 6, "amount": 600}]
        if "fact_inventory_sku_daily" in sql:
            return inventory if inventory is not None else [
                {"stock_state": "DEAD", "sku_count": 2,
                 "available_qty": 10.0}]
        if "dim_channel_map" in sql:
            return rules
        return []
    return route


def _executor(route, now=None, fail_on=None):
    conn = _RouterConn(route, fail_on=fail_on)
    return ToolExecutor(conn, question="帮我做一个10月份的运营周报",
                        now=now), conn


class RegistrationTests(unittest.TestCase):
    def test_registered_in_tools_prompt_and_web(self):
        names = [t["function"]["name"] for t in TOOLS]
        self.assertIn("sales_report", names)
        self.assertIn("sales_report", _SYSTEM_PROMPT)
        self.assertIn("sales_report", business_tool_names())

    def test_not_exportable_v1(self):
        # 多段报表的导出行语义未定（V1 先不出 xlsx），但必须在 Web 业务面
        self.assertNotIn("sales_report", DEFAULT_ALLOWED_TOOLS)


class PeriodTests(unittest.TestCase):
    def test_full_month_and_prev_calendar_month(self):
        executor, conn = _executor(_route(),
                                   now=lambda: datetime(2026, 11, 2, 12, 0))
        result = executor._tool_sales_report({"month": "2026-10"})
        self.assertEqual({"start": "2026-10-01", "end": "2026-10-31"},
                         result["period"])
        # 整月报表环比=上一自然月（9-01~9-30），不是错位 31 天窗口
        self.assertEqual({"start": "2026-09-01", "end": "2026-09-30"},
                         {k: result["compare_period"][k] for k in ("start", "end")})
        # 两期都进了总量 SQL 的参数
        totals_params = conn.executed[0][1]
        self.assertEqual(date(2026, 10, 1), totals_params[0])
        self.assertEqual(date(2026, 9, 1), totals_params[2])

    def test_current_month_truncates_to_today(self):
        executor, _ = _executor(_route(),
                                now=lambda: datetime(2026, 11, 2, 12, 0))
        result = executor._tool_sales_report({"month": "2026-11"})
        self.assertEqual("2026-11-02", result["period"]["end"])
        # 非整月 → 环比=紧邻等长上一段（本期 11-01~11-02 两天 → 10-30~10-31）
        self.assertEqual("2026-10-30", result["compare_period"]["start"])
        self.assertEqual("2026-10-31", result["compare_period"]["end"])

    def test_bad_month_rejected(self):
        executor, _ = _executor(_route())
        self.assertIn("error",
                      executor._tool_sales_report({"month": "2026/10"}))

    def test_future_month_rejected(self):
        executor, _ = _executor(_route(),
                                now=lambda: datetime(2026, 11, 2, 12, 0))
        result = executor._tool_sales_report({"month": "2026-12"})
        self.assertIn("error", result)


class ReportTests(unittest.TestCase):
    def test_five_sections_assembled(self):
        executor, conn = _executor(_route(),
                                   now=lambda: datetime(2026, 11, 2, 12, 0))
        result = executor._tool_sales_report({"month": "2026-10"})
        self.assertEqual(1000.0, result["totals"]["paid_amount"])
        self.assertEqual(500.0, result["compare_totals"]["paid_amount"])
        self.assertEqual(1.0, result["mom_rate"])  # (1000-500)/500
        # 渠道归集走 dim_channel_map：京东 600 + 未映射 400
        by_key = {row["channel_key"]: row for row in result["channels"]}
        self.assertEqual(600.0, by_key["京东"]["paid_amount"])
        self.assertEqual(400.0, by_key["未映射"]["paid_amount"])
        self.assertEqual(0.6, by_key["京东"]["share"])
        self.assertEqual("杭易-京东购喝旗舰店",
                         result["top_stores"][0]["shop_name"])
        self.assertEqual("习酒759", result["top_skus"][0]["spec_no"])
        self.assertEqual("DEAD",
                         result["inventory_states"][0]["stock_state"])
        # 四条事实查询全部带本期日期过滤（大表铁律）
        for sql, params in conn.executed[:4]:
            self.assertIn("trade_time", sql)
            self.assertIn(date(2026, 10, 1), params)

    def test_mom_rate_none_when_prev_zero(self):
        executor, _ = _executor(_route(totals={
            "cur_amount": 100, "prev_amount": 0,
            "cur_qty": 1, "prev_qty": 0, "cur_orders": 1, "prev_orders": 0}))
        result = executor._tool_sales_report({"month": "2026-10"})
        self.assertIsNone(result["mom_rate"])

    def test_inventory_section_fail_soft(self):
        executor, _ = _executor(_route(), fail_on="fact_inventory_sku_daily")
        result = executor._tool_sales_report({"month": "2026-10"})
        self.assertIsNone(result["inventory_states"])
        self.assertEqual(1000.0, result["totals"]["paid_amount"])  # 主表不拖垮

    def test_explicit_range(self):
        executor, conn = _executor(_route())
        result = executor._tool_sales_report(
            {"start_date": "2026-10-01", "end_date": "2026-10-07"})
        self.assertEqual({"start": "2026-10-01", "end": "2026-10-07"},
                         result["period"])
        # 周报跨度 7 天 → 环比 9-24~9-30
        self.assertEqual("2026-09-24", result["compare_period"]["start"])


if __name__ == "__main__":
    unittest.main()
