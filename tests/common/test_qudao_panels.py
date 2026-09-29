"""qudao 榜单页渠道板块测试（渲染 + 装配 fail-open）。"""

import unittest
from datetime import date, datetime
from unittest.mock import Mock, patch

from common.broadcast import qudao_panels
from common.daily_robot.leaderboard import build_html

_WORKDAYS = {date(2026, 9, d) for d in range(1, 24)}


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cursor(self._rows)


class ChannelDailyPanelTests(unittest.TestCase):

    def _conn(self):
        return _Conn([
            {"channel": "天猫", "business_date": date(2026, 9, 21), "s": 20000.0},
            {"channel": "天猫", "business_date": date(2026, 9, 22), "s": 10000.0},
            {"channel": "京东", "business_date": date(2026, 9, 22), "s": 12000.0},
        ])

    def test_renders_table_with_fallback_date(self):
        html = qudao_panels.build_channel_daily_panel(
            self._conn(),
            business_date=date(2026, 9, 23),
            workdays=_WORKDAYS,
            monthly_targets={"天猫": 60000},
        )
        self.assertIn("🛒 渠道日销快报", html)
        self.assertIn("全渠道9月22日销售额", html)
        self.assertIn("2.2万 元", html)
        self.assertIn("天猫", html)
        self.assertIn("-50.0%", html)          # 天猫环比
        self.assertIn("50.0%", html)           # 天猫达成率（3万/6万）
        self.assertIn("店铺后台导出 · DB", html)

    def test_empty_month_falls_back_to_placeholder(self):
        html = qudao_panels.build_channel_daily_panel(
            _Conn([]),
            business_date=date(2026, 9, 23),
            workdays=_WORKDAYS,
            monthly_targets={},
        )
        self.assertIn("暂无", html)

    def test_t1_watermark_note_and_fresh_anchor_has_no_banner(self):
        html = qudao_panels.build_channel_daily_panel(
            self._conn(),
            business_date=date(2026, 9, 23),
            workdays=_WORKDAYS,
            monthly_targets={},
        )
        # 锚点 = T-1（9/22）数据到齐：无 ⚠️ 横幅，水位标注 10:31 采集
        self.assertIn("数据截至 9月22日（T+1 10:31 采集", html)
        self.assertNotIn("昨日数据未到齐", html)

    def test_stale_anchor_shows_warning_banner(self):
        # 9/23 跑页但最新有效数据停在 9/21 → 昨日未到齐，显著标注
        conn = _Conn([
            {"channel": "天猫", "business_date": date(2026, 9, 21), "s": 20000.0},
        ])
        html = qudao_panels.build_channel_daily_panel(
            conn,
            business_date=date(2026, 9, 23),
            workdays=_WORKDAYS,
            monthly_targets={},
        )
        self.assertIn("⚠️ 昨日数据未到齐，当前显示 9 月 21 日", html)
        self.assertIn("数据截至 9月21日（T+1 10:31 采集", html)


class ItemPanelTests(unittest.TestCase):

    def test_hot_items_panel(self):
        data = {
            "as_of": date(2026, 9, 22), "stale": True,
            "items": [
                {"spec_no": "女儿红41", "goods_name": "2.5L女儿红", "available_qty": -3.0,
                 "days_left": -3.0, "purchase_intransit_qty": 0.0,
                 "stock_state": "OVERSOLD", "qty_30d": 350.0},
                {"spec_no": "习酒493", "goods_name": "53°100ml习酒", "available_qty": 8.0,
                 "days_left": 4.0, "purchase_intransit_qty": 6.0,
                 "stock_state": "URGENT", "qty_30d": 400.0},
            ],
            "watch_count": 2, "danger_count": 1, "oversold_count": 1,
            "coverage_pct": 0.85,
        }
        with patch("common.broadcast.queries.fetch_hot_items", return_value=data):
            html = qudao_panels.build_hot_items_panel(Mock(), today=date(2026, 9, 23))
        self.assertIn("🔥 热卖品监控", html)
        self.assertIn("监控 2 个SKU", html)
        self.assertIn("覆盖近30天销量 85%", html)
        self.assertIn("‼️断货", html)
        self.assertIn("⚠️偏低", html)
        self.assertIn("延迟", html)            # stale 标记
        self.assertIn("<b>4天</b>", html)      # ≤7 天加粗

    def test_stock_alert_panel(self):
        data = {
            "as_of": date(2026, 9, 23), "stale": False, "moving_window_days": 15,
            "urgent": [{"spec_no": "习酒493", "goods_name": "53°100ml习酒",
                        "available_qty": 8.0, "daily_avg": 2.0, "days_left": 4.0}],
            "oversold": [{"spec_no": "女儿红41", "goods_name": "2.5L女儿红",
                          "available_qty": -3.0}],
        }
        with patch("common.broadcast.queries.fetch_stock_alerts", return_value=data):
            html = qudao_panels.build_stock_alert_panel(Mock(), today=date(2026, 9, 23))
        self.assertIn("📦 库存补货提醒", html)
        self.assertIn("预计可售≤7天（1个）", html)
        self.assertIn("已超卖（1个）", html)
        self.assertIn("动销窗口 近15天", html)

    def test_stock_alert_panel_all_clear(self):
        data = {"as_of": date(2026, 9, 23), "stale": False,
                "moving_window_days": 15, "urgent": [], "oversold": []}
        with patch("common.broadcast.queries.fetch_stock_alerts", return_value=data):
            html = qudao_panels.build_stock_alert_panel(Mock(), today=date(2026, 9, 23))
        self.assertIn("当前无紧急补货与超卖", html)

    def test_purchase_panel(self):
        data = {
            "as_of": datetime(2026, 9, 23, 8, 30), "stale": False,
            "groups": [{
                "warehouse_name": "习水村", "order_count": 2, "line_count": 3,
                "specs": [{"spec_no": "习酒493", "goods_name": "53°100ml习酒", "qty": 30.0}],
                "total_qty": 30.0,
            }],
            "total_lines": 3, "total_specs": 1, "total_orders": 2, "total_qty": 30.0,
        }
        with patch("common.broadcast.queries.fetch_purchase_inbound", return_value=data):
            html = qudao_panels.build_purchase_inbound_panel(
                Mock(), now=datetime(2026, 9, 23, 8, 30)
            )
        self.assertIn("🏭 采购入库（近24小时）", html)
        self.assertIn("习水村", html)
        self.assertIn("（2张单）", html)
        self.assertIn("×30", html)
        self.assertIn("合计 30 件", html)

    def test_purchase_panel_empty(self):
        data = {"as_of": datetime(2026, 9, 23, 8, 30), "stale": False,
                "groups": [], "total_lines": 0, "total_specs": 0,
                "total_orders": 0, "total_qty": 0.0}
        with patch("common.broadcast.queries.fetch_purchase_inbound", return_value=data):
            html = qudao_panels.build_purchase_inbound_panel(
                Mock(), now=datetime(2026, 9, 23, 8, 30)
            )
        self.assertIn("近24小时无已完成入库", html)

    def test_order_risk_panel(self):
        data = {
            "as_of": date(2026, 9, 22), "stale": False, "threshold": 3,
            "groups": [{
                "shop_name": "习水村-抖音习酒旗舰店", "platform": "抖音",
                "areas": [{"area": "江西省吉安市新干县", "count": 5}], "total": 5,
            }],
            "pdd_no_area_count": 7,
        }
        with patch("common.broadcast.queries.fetch_order_risk", return_value=data):
            html = qudao_panels.build_order_risk_panel(
                Mock(), business_date=date(2026, 9, 23)
            )
        self.assertIn("🚨 订单风险防控（9月22日）", html)
        self.assertIn("≥3 单判定风险", html)
        self.assertIn("拼多多地区字段受隐私协议限制（7单未计入）", html)
        self.assertIn("江西省吉安市新干县", html)

    def test_order_risk_panel_clear(self):
        data = {"as_of": date(2026, 9, 22), "stale": False, "threshold": 3,
                "groups": [], "pdd_no_area_count": 0}
        with patch("common.broadcast.queries.fetch_order_risk", return_value=data):
            html = qudao_panels.build_order_risk_panel(
                Mock(), business_date=date(2026, 9, 23)
            )
        self.assertIn("昨日未发现同店同地区集中下单", html)

    def test_stock_alert_panel_zero_stock_section(self):
        # 零库存在售（available=0 但在卖）与真超卖（available<0）分开展示
        data = {
            "as_of": date(2026, 9, 23), "stale": False, "moving_window_days": 15,
            "urgent": [],
            "oversold": [{"spec_no": "女儿红41", "goods_name": "2.5L女儿红",
                          "available_qty": -3.0}],
            "zero_stock": [{"spec_no": "习酒278", "goods_name": "1.5l习酒古韵",
                            "available_qty": 0.0, "daily_avg": 5.0}],
        }
        with patch("common.broadcast.queries.fetch_stock_alerts", return_value=data):
            html = qudao_panels.build_stock_alert_panel(Mock(), today=date(2026, 9, 23))
        self.assertIn("已超卖（1个）", html)
        self.assertIn("零库存在售（1个", html)
        self.assertIn("已剔除赠品/服务卡等非商品行", html)

    def test_order_risk_panel_incomplete_groups(self):
        # 地区区级缺失的分组降级展示，不参与风险判定；店名/platform 相同不重复
        data = {
            "as_of": date(2026, 9, 22), "stale": False, "threshold": 3,
            "groups": [],
            "incomplete_groups": [{
                "shop_name": "杭州习水村酒业有限公司", "platform": "杭州习水村酒业有限公司",
                "areas": [{"area": "浙江省杭州市-", "count": 802}], "total": 802,
            }],
            "pdd_no_area_count": 0,
        }
        with patch("common.broadcast.queries.fetch_order_risk", return_value=data):
            html = qudao_panels.build_order_risk_panel(
                Mock(), business_date=date(2026, 9, 23)
            )
        self.assertIn("昨日未发现同店同地区集中下单", html)  # 正常组为空仍报平安
        self.assertIn("不参与风险判定，仅供参考", html)
        self.assertIn("浙江省杭州市-", html)
        self.assertNotIn("杭州习水村酒业有限公司-杭州习水村酒业有限公司", html)

    def test_hot_items_panel_window_note(self):
        data = {
            "as_of": date(2026, 9, 23), "stale": False, "moving_window_days": 15,
            "items": [
                {"spec_no": "习酒761", "goods_name": "习酒窖藏1988浙江", "available_qty": 3.0,
                 "days_left": None, "purchase_intransit_qty": 60.0,
                 "stock_state": "HEALTHY", "qty_30d": 1260.0},
            ],
            "watch_count": 1, "danger_count": 0, "oversold_count": 0,
            "coverage_pct": 0.5,
        }
        with patch("common.broadcast.queries.fetch_hot_items", return_value=data):
            html = qudao_panels.build_hot_items_panel(Mock(), today=date(2026, 9, 23))
        self.assertIn("近15天无动销", html)   # 窗口口径注释
        self.assertIn("30天销为近30天窗口", html)


class AssemblyTests(unittest.TestCase):

    def test_panel_failure_degrades_to_placeholder(self):
        conn = _Conn([])
        with patch(
            "common.broadcast.qudao_panels.build_hot_items_panel",
            side_effect=RuntimeError("boom"),
        ):
            panels = qudao_panels.build_qudao_panels(
                conn,
                business_date=date(2026, 9, 23),
                now=datetime(2026, 9, 23, 8, 30),
                workdays=_WORKDAYS,
                monthly_targets={},
            )
        self.assertEqual(len(panels), 5)
        degraded = [p for p in panels if "数据暂缺" in p]
        # 热卖品（boom）+ 渠道（空连接无数据走"暂无"占位文案不同）→ 至少热卖品一个
        self.assertTrue(any("热卖品监控" in p for p in degraded))
        # 其余板块照常产出
        self.assertTrue(any("采购入库" in p for p in panels))


class BuildHtmlSlotTests(unittest.TestCase):

    def _view(self):
        return {
            "region": {"name": "qudao", "displayName": "渠道", "deptOrder": [],
                       "deptLabel": {}, "broadcastExclude": []},
            "calendar": {"month": 9, "restDays": []},
        }

    def test_extra_panels_inserted_before_tabs(self):
        people = [{"name": "张三", "dept": "渠道", "target": 1000,
                   "completed": 500.0, "unfilled": 0, "rate": 0.5}]
        page = build_html(
            self._view(), datetime(2026, 9, 23, 8, 30), [1, 2], people,
            extra_panels=['<div class="panel">\n  <h2>🛒 测试板块</h2>\n</div>'],
        )
        self.assertIn("🛒 测试板块", page)
        # 插在「每日播报」之后、榜单 tabs 之前
        self.assertLess(page.index("每日播报（部门维度）"), page.index("🛒 测试板块"))
        self.assertLess(page.index("🛒 测试板块"), page.index('<div class="tabs">'))

    def test_no_extra_panels_keeps_page_identical_shape(self):
        people = [{"name": "张三", "dept": "渠道", "target": 1000,
                   "completed": 500.0, "unfilled": 0, "rate": 0.5}]
        with_panels = build_html(
            self._view(), datetime(2026, 9, 23, 8, 30), [1, 2], people,
        )
        self.assertIn("每日播报（部门维度）", with_panels)
        self.assertNotIn("🛒", with_panels)


if __name__ == "__main__":
    unittest.main()
