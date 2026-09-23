"""broadcast 查询层测试（渠道播报板块与 BI 共用）。"""

import unittest
from datetime import date, datetime

from common.broadcast.queries import (
    fetch_hot_items,
    fetch_inventory_snapshot,
    fetch_order_risk,
    fetch_purchase_inbound,
    fetch_stock_alerts,
    is_wine,
    platform_of,
)


class _Cursor:
    def __init__(self, router):
        self._router = router
        self._rows = []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._rows = self._router(sql, params)

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _Conn:
    def __init__(self, router):
        self.cursor_obj = _Cursor(router)

    def cursor(self):
        return self.cursor_obj


_SNAP_ROWS = [
    {"spec_no": "习酒493", "goods_name": "53°100ml习酒", "available_qty": 8.0,
     "stock_qty": 20.0, "qty_7days": 10.0, "qty_month": 40.0,
     "purchase_intransit_qty": 6.0, "daily_avg": 2.0, "days_left": 4.0,
     "moving_window_days": 15, "is_moving": 1, "stock_state": "URGENT"},
    {"spec_no": "女儿红41", "goods_name": "2.5L*6女儿红陈年老酒", "available_qty": -3.0,
     "stock_qty": 0.0, "qty_7days": None, "qty_month": None,
     "purchase_intransit_qty": 0.0, "daily_avg": 1.0, "days_left": -3.0,
     "moving_window_days": 15, "is_moving": 1, "stock_state": "OVERSOLD"},
    {"spec_no": "包材A", "goods_name": "礼盒包材", "available_qty": 5.0,
     "stock_qty": 5.0, "qty_7days": 1.0, "qty_month": 2.0,
     "purchase_intransit_qty": 0.0, "daily_avg": 0.1, "days_left": 50.0,
     "moving_window_days": 15, "is_moving": 1, "stock_state": "HEALTHY"},
    {"spec_no": "茅台1935", "goods_name": "500ml茅台1935", "available_qty": 60.0,
     "stock_qty": 60.0, "qty_7days": 5.0, "qty_month": 20.0,
     "purchase_intransit_qty": 0.0, "daily_avg": 1.0, "days_left": 60.0,
     "moving_window_days": 15, "is_moving": 1, "stock_state": "HEALTHY"},
]


def _router(snapshot_date=date(2026, 9, 23), snap_rows=_SNAP_ROWS,
            sales_30d=None, purchase_rows=(), risk_rows=(), pdd_count=0):
    sales_30d = sales_30d if sales_30d is not None else {
        "习酒493": 400.0, "女儿红41": 350.0, "茅台1935": 500.0, "包材A": 999.0,
    }

    def route(sql, params):
        if "MAX(`business_date`)" in sql:
            return [{"d": snapshot_date}]
        if "FROM `fact_inventory_sku_daily`" in sql and "MAX" not in sql:
            return snap_rows
        if "FROM `fact_sales_daily`" in sql:
            return [{"spec_no": k, "qty": v} for k, v in sales_30d.items()]
        if "FROM `fact_purchase_inbound`" in sql:
            return purchase_rows
        if "GROUP BY `shop_name`, `receiver_area_norm`" in sql:
            return risk_rows
        if "`channel_name` = '拼多多'" in sql:
            return [{"n": pdd_count}]
        raise AssertionError(f"unexpected SQL: {sql}")

    return route


class PureFunctionTests(unittest.TestCase):

    def test_is_wine(self):
        self.assertTrue(is_wine("53°100ml习酒"))
        self.assertTrue(is_wine("2.5L*6女儿红陈年老酒"))
        self.assertFalse(is_wine("赠品酒杯"))
        self.assertFalse(is_wine("礼盒包材"))
        self.assertFalse(is_wine(None))

    def test_platform_of(self):
        self.assertEqual(platform_of("习水村-抖音习酒旗舰店"), "抖音")
        self.assertEqual(platform_of("杭易-拼多多致中和旗舰店"), "拼多多")
        self.assertEqual(platform_of("习水村-天猫超市"), "天猫")
        self.assertEqual(platform_of("杭州习水村酒业有限公司"), "杭州习水村酒业有限公司")


class InventorySnapshotTests(unittest.TestCase):

    def test_latest_date_and_stale_flag(self):
        conn = _Conn(_router(snapshot_date=date(2026, 9, 22)))
        data = fetch_inventory_snapshot(conn, today=date(2026, 9, 23))
        self.assertEqual(data["as_of"], date(2026, 9, 22))
        self.assertTrue(data["stale"])
        self.assertEqual(len(data["rows"]), 4)

    def test_empty_snapshot(self):
        conn = _Conn(_router(snapshot_date=None, snap_rows=[]))
        data = fetch_inventory_snapshot(conn)
        self.assertIsNone(data["as_of"])
        self.assertTrue(data["stale"])
        self.assertEqual(data["rows"], [])


class HotItemsTests(unittest.TestCase):

    def test_wine_filter_top_and_danger_fill(self):
        conn = _Conn(_router())
        data = fetch_hot_items(conn, today=date(2026, 9, 23), top_n=1)
        names = [x["spec_no"] for x in data["items"]]
        # Top1 = 茅台1935（30天销 500）；包材被酒类过滤（虽销量 999）
        # 危险品保底：女儿红41（OVERSOLD, 350≥300）+ 习酒493（URGENT, 400≥300）
        self.assertEqual(names[0], "女儿红41")       # 断货排最前
        self.assertIn("习酒493", names)
        self.assertIn("茅台1935", names)
        self.assertNotIn("包材A", names)
        self.assertEqual(data["oversold_count"], 1)
        self.assertEqual(data["danger_count"], 1)
        # 覆盖率 = 监控名单 30 天销量 / 酒类合计（分母不含包材）
        self.assertAlmostEqual(data["coverage_pct"], (400 + 350 + 500) / (400 + 350 + 500))

    def test_danger_threshold_excludes_low_sales(self):
        conn = _Conn(_router(sales_30d={"习酒493": 100.0, "女儿红41": 50.0, "茅台1935": 500.0}))
        data = fetch_hot_items(conn, today=date(2026, 9, 23), top_n=1)
        names = [x["spec_no"] for x in data["items"]]
        # Top1 = 茅台1935；习酒493/女儿红41 虽危险但 30 天销量 < 300 → 不保底
        self.assertEqual(names, ["茅台1935"])

    def test_empty_snapshot_short_circuits(self):
        conn = _Conn(_router(snapshot_date=None, snap_rows=[]))
        data = fetch_hot_items(conn)
        self.assertEqual(data["items"], [])
        self.assertIsNone(data["as_of"])


class StockAlertTests(unittest.TestCase):

    def test_urgent_and_oversold_split(self):
        conn = _Conn(_router())
        data = fetch_stock_alerts(conn, today=date(2026, 9, 23))
        self.assertEqual([r["spec_no"] for r in data["urgent"]], ["习酒493"])
        self.assertEqual([r["spec_no"] for r in data["oversold"]], ["女儿红41"])
        self.assertEqual(data["moving_window_days"], 15)


class PurchaseInboundTests(unittest.TestCase):

    def test_groups_by_warehouse_and_merges_specs(self):
        purchase_rows = [
            {"order_no": "RK-1", "spec_no": "习酒493", "goods_name": "53°100ml习酒",
             "warehouse_no": "01", "warehouse_name": "习水村", "qty": 24.0,
             "stockin_time": datetime(2026, 9, 22, 10, 0)},
            {"order_no": "RK-2", "spec_no": "习酒493", "goods_name": "53°100ml习酒",
             "warehouse_no": "01", "warehouse_name": "习水村", "qty": 6.0,
             "stockin_time": datetime(2026, 9, 22, 11, 0)},
            {"order_no": "RK-3", "spec_no": "女儿红41", "goods_name": "2.5L女儿红",
             "warehouse_no": "12", "warehouse_name": "杭易", "qty": 100.0,
             "stockin_time": datetime(2026, 9, 22, 12, 0)},
        ]
        conn = _Conn(_router(purchase_rows=purchase_rows))
        now = datetime(2026, 9, 23, 8, 30)
        data = fetch_purchase_inbound(conn, now=now)

        sql, params = conn.cursor_obj.executed[-1]
        self.assertIn("`status` = %s", sql)
        self.assertEqual(params[0], "80")
        self.assertEqual(len(data["groups"]), 2)
        xishuicun = next(g for g in data["groups"] if g["warehouse_name"] == "习水村")
        self.assertEqual(xishuicun["order_count"], 2)
        # 同 SKU 跨单合并
        self.assertEqual(xishuicun["specs"][0]["qty"], 30.0)
        self.assertEqual(data["total_qty"], 130.0)
        self.assertEqual(data["total_orders"], 3)

    def test_empty_window(self):
        conn = _Conn(_router())
        data = fetch_purchase_inbound(conn, now=datetime(2026, 9, 23, 8, 30))
        self.assertEqual(data["groups"], [])
        self.assertEqual(data["total_qty"], 0.0)


class OrderRiskTests(unittest.TestCase):

    def test_groups_and_pdd_count(self):
        from datetime import timedelta

        risk_rows = [
            {"shop_name": "习水村-抖音习酒旗舰店", "area": "江西省吉安市新干县", "n": 5},
            {"shop_name": "习水村-抖音习酒旗舰店", "area": "湖北省武汉市汉阳区", "n": 3},
            {"shop_name": "习水村-视频号习酒白酒旗舰店", "area": "广东省深圳市龙华区", "n": 4},
        ]
        conn = _Conn(_router(risk_rows=risk_rows, pdd_count=7))
        yesterday = date.today() - timedelta(days=1)
        data = fetch_order_risk(conn, business_date=yesterday)

        sql, params = conn.cursor_obj.executed[0]
        self.assertIn("COUNT(DISTINCT `trade_no`)", sql)
        self.assertIn("`receiver_area_norm` IS NOT NULL", sql)
        self.assertEqual(params[-1], 3)  # threshold
        self.assertEqual(len(data["groups"]), 2)
        douyin = data["groups"][0]
        self.assertEqual(douyin["platform"], "抖音")
        self.assertEqual(douyin["areas"][0]["area"], "江西省吉安市新干县")  # 单数降序
        self.assertEqual(data["pdd_no_area_count"], 7)
        self.assertFalse(data["stale"])  # 昨日口径即最新

        older = fetch_order_risk(conn, business_date=yesterday - timedelta(days=2))
        self.assertTrue(older["stale"])  # 更早的日期标记延迟


if __name__ == "__main__":
    unittest.main()
