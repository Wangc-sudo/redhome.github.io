# -*- coding: utf-8 -*-
"""SKU 维度查询工具（sku_sales / sku_inventory）单测。

方案 §5 C1-C7（2026-11-02）。预定义 SQL 由路由假连接记录，断言
「SQL 形态 + 参数 + 口径字段」，不连真库。
"""

import unittest
from datetime import date
from unittest import mock

from common.kb_qa.agent import TOOLS, ToolExecutor, _SQL_TABLES, _SYSTEM_PROMPT
from common.kb_qa.focus import parse_focus
from common.kb_qa.query_export import DEFAULT_ALLOWED_TOOLS


class _RouterCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def execute(self, sql, params=()):
        self._conn.executed.append((" ".join(sql.split()), params))
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
    def __init__(self, route):
        self.route = route
        self.executed = []
        self.cursor_instance = _RouterCursor(self)

    def cursor(self):
        return self.cursor_instance

    @property
    def sqls(self):
        return [sql for sql, _ in self.executed]


def _product_rows(*names):
    return [{"code": f"P{i}", "name": name} for i, name in enumerate(names)]


class RegistrationTests(unittest.TestCase):
    def test_tools_registered(self):
        names = [t["function"]["name"] for t in TOOLS]
        self.assertIn("sku_sales", names)
        self.assertIn("sku_inventory", names)

    def test_inventory_table_whitelisted(self):
        self.assertIn("fact_inventory_sku_daily", _SQL_TABLES)
        self.assertIn("呆滞", _SQL_TABLES["fact_inventory_sku_daily"])

    def test_prompt_routes_and_disciplines(self):
        self.assertIn("sku_sales", _SYSTEM_PROMPT)
        self.assertIn("sku_inventory", _SYSTEM_PROMPT)
        self.assertIn("答复纪律", _SYSTEM_PROMPT)

    def test_export_and_web_whitelist(self):
        self.assertIn("sku_sales", DEFAULT_ALLOWED_TOOLS)
        self.assertIn("sku_inventory", DEFAULT_ALLOWED_TOOLS)
        from common.kb_web.service import _BUSINESS_TOOLS
        self.assertIn("sku_sales", _BUSINESS_TOOLS)
        self.assertIn("sku_inventory", _BUSINESS_TOOLS)


class SkuSalesTests(unittest.TestCase):
    def _executor(self, rows, total=None, products=()):
        def route(sql, params):
            if "kb_products" in sql:
                return _product_rows(*products)
            if "COUNT(DISTINCT" in sql:
                return [total or {"order_count": 2, "quantity": 5,
                                  "paid_amount": 300}]
            return rows
        conn = _RouterConn(route)
        question = "9月哪款卖得好"
        return ToolExecutor(conn, focus=parse_focus(question),
                            question=question), conn

    def test_default_period_is_month_to_date(self):
        executor, conn = self._executor([
            {"spec_no": "S1", "goods_name": "习酒窖藏1988", "quantity": 2,
             "paid_amount": 200},
            {"spec_no": "S2", "goods_name": "金沙摘要", "quantity": 1,
             "paid_amount": 100},
        ])
        result = executor._tool_sku_sales({})
        self.assertNotIn("error", result)
        first_sql, first_params = conn.executed[0]
        self.assertIn("COUNT(DISTINCT `trade_no`)", first_sql)
        self.assertEqual(date.today().replace(day=1), first_params[0])
        self.assertEqual(2, result["count"])
        self.assertEqual(300, result["total"]["paid_amount"])
        self.assertAlmostEqual(0.666667, result["rows"][0]["share"], places=5)
        self.assertEqual(2, len(result["_export_rows"]))

    def test_keyword_uses_single_product_name(self):
        executor, conn = self._executor([], products=("习酒窖藏1988",))
        executor._tool_sku_sales({"keyword": "习酒窖藏1988"})
        like_params = [p for sql, params in conn.executed
                       for p in params if isinstance(p, str) and "%" in p]
        self.assertTrue(any("习酒窖藏1988" in p for p in like_params))

    def test_multi_spec_listed_per_spec_not_blocked(self):
        """命中多规格：不反问（云上实测「习酒窖藏1988」对应 5+ 规格），
        按关键词检索并在结果里标注 multiple_specs，逐规格列出不合并。"""
        executor, conn = self._executor(
            [{"spec_no": "S1", "goods_name": "习酒窖藏1988 500ml",
              "quantity": 1, "paid_amount": 100}],
            products=("习酒窖藏1988 500ml", "习酒窖藏1988 100ml"))
        result = executor._tool_sku_sales({"keyword": "习酒窖藏1988"})
        self.assertTrue(result["multiple_specs"])
        self.assertEqual(2, len(result["candidates"]))
        self.assertEqual(1, len(result["rows"]))     # 仍给出答案
        # 用原文 LIKE 检索（不是拿第一个商品名去猜）
        like_params = [p for _, params in conn.executed
                       for p in params if isinstance(p, str) and "%" in p]
        self.assertTrue(any(p == "%习酒窖藏1988%" for p in like_params))

    def test_single_product_uses_full_name(self):
        executor, conn = self._executor(
            [], products=("53°500ml*6习酒窖藏1988",))
        executor._tool_sku_sales({"keyword": "习酒窖藏1988"})
        like_params = [p for _, params in conn.executed
                       for p in params if isinstance(p, str) and "%" in p]
        self.assertTrue(any("53°500ml*6习酒窖藏1988" in p for p in like_params))

    def test_channel_and_shop_filters(self):
        executor, conn = self._executor([])
        executor._tool_sku_sales({"channel": "京东", "shop_name": "购喝"})
        sql = " ".join(conn.sqls)
        self.assertIn("`channel_name`=%s", sql)
        self.assertIn("`shop_name` LIKE %s", sql)
        params = [p for _, params in conn.executed for p in params]
        self.assertIn("京东", params)
        self.assertIn("%购喝%", params)

    def test_period_pair_required(self):
        executor, _ = self._executor([])
        self.assertIn("error", executor._tool_sku_sales(
            {"start_date": "2026-09-01"}))
        self.assertIn("error", executor._tool_sku_sales(
            {"start_date": "2026/09/01", "end_date": "2026-09-30"}))

    def test_snapshot_date_exposed(self):
        executor, _ = self._executor([])
        result = executor._tool_sku_sales(
            {"start_date": "2026-09-01", "end_date": "2026-09-30"})
        self.assertEqual("2026-09-30", result["snapshot_date"])


class SkuInventoryTests(unittest.TestCase):
    SNAPSHOT = date(2026, 11, 1)

    def _executor(self, rows, overview=None, products=()):
        def route(sql, params):
            if "kb_products" in sql:
                return _product_rows(*products)
            if "MAX(`business_date`)" in sql:
                return [{"d": self.SNAPSHOT}]
            if "GROUP BY `stock_state`" in sql:
                return overview or []
            return rows
        return ToolExecutor(_RouterConn(route), question="哪些呆滞")

    def test_snapshot_and_state_note(self):
        executor = self._executor([
            {"spec_no": "S1", "goods_name": "习酒窖藏1988",
             "stock_state": "DEAD", "available_qty": 30,
             "purchase_intransit_qty": 0, "qty_7days": 0, "qty_month": 0,
             "daily_avg": 0, "days_left": None},
        ])
        result = executor._tool_sku_inventory({"state": "DEAD"})
        self.assertEqual("2026-11-01", result["snapshot_date"])
        self.assertIn("DEAD=近 30 天零出库", result["state_note"])
        self.assertEqual("DEAD", result["rows"][0]["stock_state"])
        self.assertEqual("2026-11-01", result["_export_params"]["snapshot_date"])

    def test_overview_without_keyword(self):
        executor = self._executor(
            [], overview=[{"stock_state": "DEAD", "sku_count": 120,
                           "available_qty": 5000}])
        result = executor._tool_sku_inventory({})
        self.assertEqual(1, len(result["overview"]))
        self.assertEqual(120, result["overview"][0]["sku_count"])

    def test_unknown_state_rejected(self):
        result = self._executor([])._tool_sku_inventory({"state": "BOOM"})
        self.assertIn("error", result)
        self.assertIn("URGENT", result["valid_states"])

    def test_multi_spec_listed_per_spec_not_blocked(self):
        executor = self._executor(
            [], products=("习酒窖藏1988 500ml", "习酒窖藏1988 100ml"))
        result = executor._tool_sku_inventory({"keyword": "习酒窖藏1988"})
        self.assertTrue(result["multiple_specs"])
        self.assertEqual(2, len(result["candidates"]))

    def test_no_snapshot_fails_soft(self):
        conn = _RouterConn(lambda sql, params: [])
        self.assertIn("error", ToolExecutor(conn)._tool_sku_inventory({}))


class ChannelYoyFocusTests(unittest.TestCase):
    """工具结果聚焦压缩（方案 B1）：喂 LLM 的行数收敛，导出仍全量。"""

    @staticmethod
    def _source_rows(count):
        return [{
            "shop_name": f"店{i}", "buyer_nick": "抬头",
            "current_sales_amount": 100 - i,
            "previous_sales_amount": 50,
            "current_row_count": 3, "previous_row_count": 2,
        } for i in range(count)]

    def _executor(self, count, question):
        rules = [{
            "id": 1, "channel_key": "京东", "channel_label": "京东",
            "panel": "channel", "business_line": "online",
            "match_type": "shop", "match_mode": "contains",
            "match_value": "店", "priority": 1, "note": "", "enabled": 1,
        }]

        def route(sql, params):
            if "dim_channel_map" in sql:
                return rules
            return self._source_rows(count)
        return ToolExecutor(_RouterConn(route), question=question,
                            focus=parse_focus(question))

    def test_large_result_compressed_with_others(self):
        executor = self._executor(20, "本月店铺销售怎么样")
        result = executor._tool_channel_yoy({"group_by": "store"})
        self.assertEqual(3, len(result["rows"]))
        self.assertEqual(17, result["others"]["omitted"])
        self.assertEqual(20, result["count"])
        self.assertEqual(20, len(result["_export_rows"]))

    def test_named_shop_kept_first(self):
        executor = self._executor(20, "店15 本月销售怎么样")
        result = executor._tool_channel_yoy({"group_by": "store"})
        self.assertEqual("店15", result["rows"][0]["shop_name"])

    def test_small_result_kept_whole(self):
        executor = self._executor(5, "本月渠道怎么样")
        result = executor._tool_channel_yoy({"group_by": "store"})
        self.assertEqual(5, len(result["rows"]))
        self.assertNotIn("others", result)

    def test_unknown_shop_returns_error_with_hint(self):
        executor = self._executor(3, "本月销售")
        result = executor._tool_channel_yoy(
            {"group_by": "store", "shop_name": "不存在的店"})
        self.assertIn("error", result)
        self.assertEqual(3, len(result["shops"]))


if __name__ == "__main__":
    unittest.main()
