"""broadcast 查询层测试（渠道播报板块与 BI 共用）。"""

import unittest
from datetime import date, datetime
from unittest.mock import Mock, patch

from common.broadcast.queries import (
    fetch_bill_breakdown,
    fetch_expense_summary,
    fetch_hot_items,
    fetch_inventory_snapshot,
    fetch_monthly_kpi,
    fetch_order_risk,
    fetch_product_structure,
    fetch_purchase_inbound,
    fetch_refund_reason,
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


class ExpenseSummaryTests(unittest.TestCase):
    """费用/盈利面板查询（2026-10-12）：渠道×月 费用/充值/GMV/费率。"""

    def test_aggregates_by_channel_and_month_with_gmv_rate(self):
        expense = [
            {"month": "2026-08", "channel": "天猫", "source": "store_commission", "amt": 5000.0},
            {"month": "2026-08", "channel": "天猫", "source": "promotion_recharge", "amt": 3000.0},
            {"month": "2026-08", "channel": None, "source": "store_commission", "amt": 500.0},
            {"month": "2026-07", "channel": "京东", "source": "store_commission", "amt": 900.0},
        ]
        unmapped = [{"store": "得物", "platform": "天猫"}]
        gmv = [{"y": 2026, "m": 8, "channel": "天猫", "gmv": 160000.0}]
        with patch("common.broadcast.queries._query",
                   side_effect=[expense, unmapped, gmv]):
            data = fetch_expense_summary(
                Mock(), months_back=2, today=date(2026, 9, 23)
            )
        self.assertEqual(data["months"], ["2026-08", "2026-09"])
        tm = data["rows"][0]
        self.assertEqual(tm["channel"], "天猫")
        self.assertEqual(tm["cells"]["2026-08"]["total"], 8000.0)
        self.assertEqual(tm["cells"]["2026-08"]["rate"], 0.05)
        # 9 月在窗口内但财务未填 → 显式标注而不是静默
        self.assertEqual(data["empty_months"], ["2026-09"])
        # 窗口外的 07 月不进展示也不污染 empty 判定
        self.assertNotIn("2026-07", data["months"])
        # 未挂渠道桶（mart_channel IS NULL）+ 恒排最后
        unmapped_row = data["rows"][-1]
        self.assertEqual(unmapped_row["channel"], "未挂渠道")
        self.assertEqual(unmapped_row["cells"]["2026-08"]["total"], 500.0)
        self.assertEqual(data["unmapped_stores"], ["得物（天猫）"])

    def test_rate_is_none_without_gmv_and_rows_sorted_by_total(self):
        expense = [
            {"month": "2026-09", "channel": "京东", "source": "store_commission", "amt": 100.0},
            {"month": "2026-09", "channel": "拼多多", "source": "store_commission", "amt": 900.0},
        ]
        with patch("common.broadcast.queries._query", side_effect=[expense, [], []]):
            data = fetch_expense_summary(Mock(), months_back=1, today=date(2026, 9, 23))
        jd = next(r for r in data["rows"] if r["channel"] == "京东")
        self.assertIsNone(jd["cells"]["2026-09"]["rate"])  # 无 GMV 不臆造费率
        self.assertEqual(data["rows"][0]["channel"], "拼多多")
        self.assertEqual(data["empty_months"], [])


class BillBreakdownTests(unittest.TestCase):
    """账单费用拆解查询（BI 阶段 4）：类别/平台/项目 + 窗口填充。"""

    def test_grand_total_sorting_shares_and_empty_months(self):
        by_category = [
            {"k": "店铺扣点", "amt": 6000.0},
            {"k": "开票填写负数", "amt": -1500.0},
            {"k": None, "amt": 100.0},
        ]
        by_platform = [
            {"k": "天猫", "amt": 4000.0},
            {"k": "抖音", "amt": -500.0},
            {"k": None, "amt": 100.0},
        ]
        by_items = [
            {"k": "佣金", "amt": 3000.0},
            {"k": "平台服务费", "amt": -800.0},
        ]
        filled = [{"month": "2026-09"}]
        with patch("common.broadcast.queries._query",
                   side_effect=[by_category, by_platform, by_items, filled]):
            data = fetch_bill_breakdown(
                Mock(), months_back=2, today=date(2026, 9, 23)
            )
        # 合计 = 所有费用类别合计（= 平台合计），作占比分母
        self.assertEqual(data["grand_total"], 4600.0)
        # 类别：绝对值降序，None 标签恒排最后
        self.assertEqual(
            [c["category"] for c in data["by_category"]],
            ["店铺扣点", "开票填写负数", None],
        )
        # 平台：负值（冲销）按绝对值参与排序，None 恒排最后
        self.assertEqual(
            [p["platform"] for p in data["by_platform"]], ["天猫", "抖音", None]
        )
        self.assertAlmostEqual(data["by_platform"][0]["share"], 4000.0 / 4600.0)
        self.assertAlmostEqual(data["top_items"][0]["share"], 3000.0 / 4600.0)
        self.assertEqual(data["top_items"][1]["item"], "平台服务费")
        self.assertEqual(data["other_items"], {"count": 0, "total": 0.0})
        # 窗口填充：窗口内未填月显式标注，窗口外月份不参与
        self.assertEqual(data["months"], ["2026-08", "2026-09"])
        self.assertEqual(data["filled_months"], ["2026-09"])
        self.assertEqual(data["empty_months"], ["2026-08"])
        self.assertEqual(data["as_of"], date(2026, 9, 23))

    def test_top_items_truncated_with_other_bucket(self):
        by_category = [{"k": "店铺扣点", "amt": 100.0}]
        by_platform = [{"k": "天猫", "amt": 100.0}]
        # 12 个项目，金额 20..9 递减 → Top10 后剩 2 项（10+9）
        by_items = [
            {"k": f"item{i}", "amt": float(21 - i)} for i in range(1, 13)
        ]
        filled = [{"month": "2026-09"}, {"month": "2026-05"}]
        with patch("common.broadcast.queries._query",
                   side_effect=[by_category, by_platform, by_items, filled]):
            data = fetch_bill_breakdown(
                Mock(), months_back=2, today=date(2026, 9, 23)
            )
        self.assertEqual(len(data["top_items"]), 10)   # BILL_TOP_ITEMS
        self.assertEqual(data["top_items"][0]["item"], "item1")
        self.assertEqual(data["other_items"], {"count": 2, "total": 19.0})
        # 窗口外 2026-05 不计入 filled/empty 判定
        self.assertEqual(data["filled_months"], ["2026-09"])
        self.assertEqual(data["empty_months"], ["2026-08"])

    def test_share_is_none_when_grand_total_zero(self):
        with patch("common.broadcast.queries._query",
                   side_effect=[
                       [{"k": "店铺扣点", "amt": 0.0}],
                       [{"k": "天猫", "amt": 0.0}],
                       [{"k": "佣金", "amt": 0.0}],
                       [],
                   ]):
            data = fetch_bill_breakdown(Mock(), months_back=1,
                                        today=date(2026, 9, 23))
        self.assertEqual(data["grand_total"], 0.0)
        self.assertIsNone(data["by_platform"][0]["share"])   # 不除零、不臆造
        self.assertIsNone(data["top_items"][0]["share"])
        self.assertEqual(data["empty_months"], ["2026-09"])


class MonthlyKpiTests(unittest.TestCase):
    """月度 KPI 查询（BI 阶段 2）：买家资产 + 取消率 + 残月元数据。"""

    def test_buyers_new_repeat_cancel_and_meta(self):
        gmv = [
            {"y": 2026, "m": 7, "gmv": 100.0, "orders": 10, "buyers": 8},
            {"y": 2026, "m": 8, "gmv": 200.0, "orders": 15, "buyers": 12},
        ]
        first = [
            {"buyer": "A", "first_ym": 202607},
            {"buyer": "B", "first_ym": 202607},
            {"buyer": "C", "first_ym": 202608},
            {"buyer": "D", "first_ym": 202608},
        ]
        cancels = [{"y": 2026, "m": 8, "cancels": 3}]
        meta = [{"data_start": datetime(2026, 8, 22, 10, 0, 0),
                 "n": 1000, "buyer_null": 200}]
        refund = [{"y": 2026, "m": 8, "refund_amt": 20.0, "refund_orders": 2}]
        promo = [{"y": 2026, "m": 8, "promo": 50.0}]
        with patch("common.broadcast.queries._query",
                   side_effect=[gmv, first, cancels, meta, refund, promo]):
            data = fetch_monthly_kpi(Mock(), months_back=2)
        self.assertEqual([m["ym"] for m in data["months"]],
                         ["2026-07", "2026-08"])
        self.assertEqual(data["data_start"], datetime(2026, 8, 22, 10, 0, 0))
        self.assertEqual(data["buyer_null_share"], 0.2)
        aug = data["months"][1]
        self.assertEqual(aug["buyers"], 12)
        self.assertEqual(aug["new_buyers"], 2)          # 首购月=2026-08
        self.assertAlmostEqual(aug["repeat_rate"], 10 / 12)
        self.assertEqual(aug["cancel_orders"], 3)
        self.assertAlmostEqual(aug["cancel_rate"], 3 / 18)  # ÷(付款15+取消3)
        self.assertAlmostEqual(aug["roi"], 4.0)
        jul = data["months"][0]
        self.assertEqual(jul["cancel_orders"], 0)
        self.assertEqual(jul["cancel_rate"], 0.0)       # 无取消单

    def test_buyer_metrics_are_none_without_buyers(self):
        gmv = [{"y": 2026, "m": 9, "gmv": 50.0, "orders": 5, "buyers": 0}]
        meta = [{"data_start": None, "n": 0, "buyer_null": 0}]
        with patch("common.broadcast.queries._query",
                   side_effect=[gmv, [], [], meta, [], []]):
            data = fetch_monthly_kpi(Mock(), months_back=1)
        row = data["months"][0]
        self.assertIsNone(row["repeat_rate"])           # 无买家不臆造复购率
        self.assertIsNone(data["buyer_null_share"])
        self.assertIsNone(data["data_start"])


class ProductStructureTests(unittest.TestCase):
    """商品结构查询（BI 阶段 5 子集）：系列/价格带/集中度。"""

    def test_series_bands_top_and_hhi(self):
        spec_rows = [
            {"spec_no": "S1", "goods_name": "酒A", "gmv": 600.0, "qty": 3.0},
            {"spec_no": "S2", "goods_name": "酒B", "gmv": 1000.0, "qty": 2.0},
            {"spec_no": "S3", "goods_name": "酒C", "gmv": 400.0, "qty": 0},
            {"spec_no": "S4", "goods_name": "酒D", "gmv": 100.0, "qty": 1.0},
        ]
        series_rows = [
            {"spec_no": "S1", "series_name": "经典系列"},
            {"spec_no": "S2", "series_name": None},
            {"spec_no": "S4", "series_name": "经典系列"},
        ]
        with patch("common.broadcast.queries._query",
                   side_effect=[spec_rows, series_rows]):
            data = fetch_product_structure(Mock(), today=date(2026, 9, 23))
        self.assertEqual(data["total_gmv"], 2100.0)
        # 系列结构：NULL/缺失归「未分类」（S2 无系列 + S3 不在 dim_product）
        series = {s["series"]: s["gmv"] for s in data["series"]}
        self.assertEqual(series, {"未分类": 1400.0, "经典系列": 700.0})
        # 价格带：件均价分档（S1=200/件，S2=500/件，S4=100/件；S3 零件数）
        bands = {b["band"]: b["gmv"] for b in data["price_bands"]}
        self.assertEqual(bands["0-199"], 100.0)
        self.assertEqual(bands["200-399"], 600.0)
        self.assertEqual(bands["400-799"], 1000.0)
        self.assertEqual(bands["单价未知"], 400.0)
        # 集中度：单品数<10 时 Top10 占满；HHI=Σ占比²
        self.assertAlmostEqual(data["top10_share"], 1.0)
        self.assertAlmostEqual(
            data["hhi"],
            (1000 / 2100) ** 2 + (600 / 2100) ** 2
            + (400 / 2100) ** 2 + (100 / 2100) ** 2,
        )

    def test_empty_window_returns_zero_total(self):
        with patch("common.broadcast.queries._query", side_effect=[[], []]):
            data = fetch_product_structure(Mock(), today=date(2026, 9, 23))
        self.assertEqual(data["total_gmv"], 0.0)
        self.assertIsNone(data["top10_share"])
        self.assertIsNone(data["hhi"])


class RefundReasonTests(unittest.TestCase):
    """退款原因查询（BI 阶段 5 子集）：窗口内 reason 聚合。"""

    def test_aggregates_with_share_sorted_by_amount(self):
        rows = [
            {"reason": "七天无理由退换", "amt": 300.0, "orders": 3},
            {"reason": "未填写", "amt": 100.0, "orders": 1},  # SQL COALESCE 已归桶
        ]
        with patch("common.broadcast.queries._query", side_effect=[rows]):
            data = fetch_refund_reason(Mock(), today=date(2026, 9, 23))
        self.assertEqual(data["total_amount"], 400.0)
        self.assertEqual(data["reasons"][0]["reason"], "七天无理由退换")
        self.assertAlmostEqual(data["reasons"][0]["share"], 0.75)
        self.assertEqual(data["reasons"][1]["orders"], 1)

    def test_empty_returns_zero_total(self):
        with patch("common.broadcast.queries._query", side_effect=[[]]):
            data = fetch_refund_reason(Mock(), today=date(2026, 9, 23))
        self.assertEqual(data["total_amount"], 0.0)
        self.assertEqual(data["reasons"], [])


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
        self.assertEqual(data["zero_stock"], [])
        self.assertEqual(data["moving_window_days"], 15)

    def test_zero_stock_split_and_goods_filter(self):
        # 零库存在售（available=0）拆出 zero_stock；赠品/服务卡被剔除，
        # 但无酒类关键词的真酒（舍之道）必须保留（排除法，不做白名单）
        rows = _SNAP_ROWS + [
            {"spec_no": "习酒278", "goods_name": "1.5l习酒古韵", "available_qty": 0.0,
             "stock_qty": 0.0, "qty_7days": None, "qty_month": None,
             "purchase_intransit_qty": 0.0, "daily_avg": 5.0, "days_left": 0.0,
             "moving_window_days": 15, "is_moving": 1, "stock_state": "OVERSOLD"},
            {"spec_no": "赠品110", "goods_name": "服务升级卡", "available_qty": 0.0,
             "stock_qty": 0.0, "qty_7days": None, "qty_month": None,
             "purchase_intransit_qty": 0.0, "daily_avg": 1.0, "days_left": 0.0,
             "moving_window_days": 15, "is_moving": 1, "stock_state": "OVERSOLD"},
            {"spec_no": "舍得33", "goods_name": "舍之道（天道礼盒装）", "available_qty": 8.0,
             "stock_qty": 8.0, "qty_7days": 10.0, "qty_month": 40.0,
             "purchase_intransit_qty": 0.0, "daily_avg": 2.0, "days_left": 4.0,
             "moving_window_days": 15, "is_moving": 1, "stock_state": "URGENT"},
        ]
        conn = _Conn(_router(snap_rows=rows))
        data = fetch_stock_alerts(conn, today=date(2026, 9, 23))
        self.assertEqual([r["spec_no"] for r in data["oversold"]], ["女儿红41"])
        self.assertEqual([r["spec_no"] for r in data["zero_stock"]], ["习酒278"])
        urgent_specs = [r["spec_no"] for r in data["urgent"]]
        self.assertIn("舍得33", urgent_specs)  # 无关键词真酒不误伤
        self.assertNotIn("赠品110", urgent_specs + [r["spec_no"] for r in data["zero_stock"]])


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

    def test_incomplete_area_groups_split(self):
        from datetime import timedelta

        risk_rows = [
            {"shop_name": "习水村-抖音习酒旗舰店", "area": "江西省吉安市新干县", "n": 5},
            {"shop_name": "杭州习水村酒业有限公司", "area": "浙江省杭州市-", "n": 802},
        ]
        conn = _Conn(_router(risk_rows=risk_rows))
        yesterday = date.today() - timedelta(days=1)
        data = fetch_order_risk(conn, business_date=yesterday)
        # 区级缺失（"…-"）分组不参与风险判定
        self.assertEqual(len(data["groups"]), 1)
        self.assertEqual(data["groups"][0]["shop_name"], "习水村-抖音习酒旗舰店")
        self.assertEqual(len(data["incomplete_groups"]), 1)
        self.assertEqual(data["incomplete_groups"][0]["areas"][0]["count"], 802)


if __name__ == "__main__":
    unittest.main()
