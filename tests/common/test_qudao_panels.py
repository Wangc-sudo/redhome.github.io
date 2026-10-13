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


class MonthlyKpiPanelTests(unittest.TestCase):

    def test_renders_monthly_table_with_available_metrics(self):
        data = {
            "months": [
                {"ym": "2026-08", "gmv": 300000.0, "mom": None, "orders": 1500,
                 "aov": 200.0, "buyers": 1200, "new_buyers": 300,
                 "repeat_rate": 0.75, "cancel_orders": 40, "cancel_rate": 0.026,
                 "refund_amt": 12000.0, "refund_rate_amt": 0.04,
                 "refund_orders": 60, "refund_rate_cnt": 0.04,
                 "net_gmv": 288000.0, "promo": 50000.0, "roi": 6.0},
                {"ym": "2026-09", "gmv": 360000.0, "mom": 0.2, "orders": 1800,
                 "aov": 200.0, "buyers": 1500, "new_buyers": 260,
                 "repeat_rate": 0.827, "cancel_orders": 30, "cancel_rate": 0.016,
                 "refund_amt": 9000.0, "refund_rate_amt": 0.025,
                 "refund_orders": 45, "refund_rate_cnt": 0.025,
                 "net_gmv": 351000.0, "promo": 60000.0, "roi": 6.0},
            ],
            "as_of": date(2026, 9, 23),
            "data_start": datetime(2026, 8, 22, 10, 0, 0),
            "buyer_null_share": 0.18,
        }
        with patch("common.broadcast.queries.fetch_monthly_kpi", return_value=data):
            html = qudao_panels.build_monthly_kpi_panel(Mock(), today=date(2026, 9, 23))
        self.assertIn("📊 月度核心指标看板", html)
        self.assertIn("2026-08", html)
        self.assertIn("2026-09", html)
        self.assertIn("GMV(剔除取消)", html)
        self.assertIn("净额(扣退款)", html)
        self.assertIn("+20.0%", html)          # 9 月环比
        self.assertIn("NA", html)              # 8 月首月无环比
        self.assertIn("6.00", html)            # ROI
        # 买家资产/取消列（BI 阶段 2）
        self.assertIn("<th>活跃买家</th>", html)
        self.assertIn("<th>新客</th>", html)
        self.assertIn("<th>复购率</th>", html)
        self.assertIn("<th>取消单</th>", html)
        self.assertIn("<th>取消率</th>", html)
        self.assertIn("1,200", html)           # 8 月活跃买家
        self.assertIn("75.0%", html)           # 8 月复购率
        # 残月与买家缺失显式标注（不臆造）
        self.assertIn("同比", html)
        self.assertIn("残月(8月22日起)", html)
        self.assertIn("买家ID缺失行占比 18%", html)
        self.assertIn("取消率=取消单÷(付款订单+取消单)", html)

    def test_empty_data_degrades_to_placeholder(self):
        with patch("common.broadcast.queries.fetch_monthly_kpi",
                   return_value={"months": [], "as_of": date(2026, 9, 23)}):
            html = qudao_panels.build_monthly_kpi_panel(Mock(), today=date(2026, 9, 23))
        self.assertIn("暂无订单数据", html)


class ExpensePanelTests(unittest.TestCase):

    def _data(self):
        return {
            "months": ["2026-08", "2026-09"],
            "rows": [
                {"channel": "天猫", "total": 15000.0, "cells": {
                    "2026-08": {"commission": 5000.0, "recharge": 3000.0,
                                "total": 8000.0, "gmv": 160000.0, "rate": 0.05},
                    "2026-09": {"commission": 6000.0, "recharge": 1000.0,
                                "total": 7000.0, "gmv": 140000.0, "rate": 0.05},
                }},
                {"channel": "未挂渠道", "total": 500.0, "cells": {
                    "2026-08": {"commission": 500.0, "recharge": 0.0,
                                "total": 500.0, "gmv": None, "rate": None},
                    "2026-09": {"commission": 0.0, "recharge": 0.0,
                                "total": 0.0, "gmv": None, "rate": None},
                }},
            ],
            "unmapped_stores": ["得物（天猫）", "十全酒美（拼多多）"],
            "empty_months": ["2026-09"],
            "as_of": date(2026, 9, 23),
        }

    def test_renders_channel_month_table_with_rates_and_markers(self):
        with patch("common.broadcast.queries.fetch_expense_summary",
                   return_value=self._data()):
            html = qudao_panels.build_expense_panel(Mock(), today=date(2026, 9, 23))
        self.assertIn("💰 费用/盈利面板", html)
        self.assertIn("<th>2026-08</th>", html)
        self.assertIn("<th>2026-09</th>", html)
        self.assertIn("近2月合计", html)
        self.assertIn("8,000", html)              # 08 月天猫费用合计
        self.assertIn("5.0% · 充3,000", html)     # 费率 + 推广充值标注
        self.assertIn("—", html)                  # 无 GMV 费率占位
        # 未挂渠道恒排最后
        self.assertGreater(
            html.index('<td class="strong">未挂渠道</td>'),
            html.index('<td class="strong">天猫</td>'),
        )
        # 显式标注，不静默
        self.assertIn("⚠️ 财务未填：2026-09", html)
        self.assertIn("得物（天猫）", html)
        self.assertIn("seeds/store_channel_mapping.json", html)
        self.assertIn("应缴/实缴待财务确认", html)

    def test_empty_data_degrades_to_placeholder(self):
        with patch("common.broadcast.queries.fetch_expense_summary",
                   return_value={"months": [], "rows": [], "unmapped_stores": [],
                                 "empty_months": [], "as_of": date(2026, 9, 23)}):
            html = qudao_panels.build_expense_panel(Mock(), today=date(2026, 9, 23))
        self.assertIn("暂无费用数据", html)


class ProductStructurePanelTests(unittest.TestCase):

    def _data(self):
        return {
            "months": ["2026-08", "2026-09"],
            "total_gmv": 2100.0,
            "series": [
                {"series": "未分类", "gmv": 1400.0, "share": 0.667},
                {"series": "经典系列", "gmv": 700.0, "share": 0.333},
            ],
            "price_bands": [
                {"band": "0-199", "gmv": 100.0, "share": 0.048},
                {"band": "200-399", "gmv": 600.0, "share": 0.286},
                {"band": "400-799", "gmv": 1000.0, "share": 0.476},
                {"band": "800-1499", "gmv": 0.0, "share": 0.0},
                {"band": "1500+", "gmv": 0.0, "share": 0.0},
                {"band": "单价未知", "gmv": 400.0, "share": 0.19},
            ],
            "top_products": [
                {"spec_no": "S2", "goods_name": "酒B", "series_name": "未分类",
                 "gmv": 1000.0, "share": 0.476},
            ],
            "top10_share": 1.0,
            "hhi": 0.347,
            "as_of": date(2026, 9, 23),
        }

    def test_renders_series_bands_and_concentration(self):
        with patch("common.broadcast.queries.fetch_product_structure",
                   return_value=self._data()):
            html = qudao_panels.build_product_structure_panel(
                Mock(), today=date(2026, 9, 23)
            )
        self.assertIn("🧱 商品结构分析", html)
        self.assertIn("系列结构", html)
        self.assertIn("经典系列", html)
        self.assertIn("价格带分布", html)
        self.assertIn("400-799元", html)
        self.assertIn("Top10 单品", html)
        self.assertIn("酒B", html)
        self.assertIn("HHI", html)
        # 显式口径与不静默桶
        self.assertIn("未分类", html)
        self.assertIn("单价未知", html)
        self.assertIn("件均价=paid_amount÷quantity", html)

    def test_empty_data_degrades_to_placeholder(self):
        with patch("common.broadcast.queries.fetch_product_structure",
                   return_value={"months": ["2026-09"], "total_gmv": 0.0,
                                 "series": [], "price_bands": [],
                                 "top_products": [], "top10_share": None,
                                 "hhi": None, "as_of": date(2026, 9, 23)}):
            html = qudao_panels.build_product_structure_panel(
                Mock(), today=date(2026, 9, 23)
            )
        self.assertIn("暂无订单数据", html)


class RefundReasonPanelTests(unittest.TestCase):

    def test_renders_reason_table_with_unfilled_bucket(self):
        data = {
            "months": ["2026-08", "2026-09"],
            "total_amount": 400.0,
            "reasons": [
                {"reason": "七天无理由退换", "amount": 300.0, "orders": 3,
                 "share": 0.75},
                {"reason": "未填写", "amount": 100.0, "orders": 1, "share": 0.25},
            ],
            "as_of": date(2026, 9, 23),
        }
        with patch("common.broadcast.queries.fetch_refund_reason",
                   return_value=data):
            html = qudao_panels.build_refund_reason_panel(
                Mock(), today=date(2026, 9, 23)
            )
        self.assertIn("↩️ 退款原因分析", html)
        self.assertIn("七天无理由退换", html)
        self.assertIn("未填写", html)
        self.assertIn("75.0%", html)
        self.assertIn("近2月退款合计", html)
        self.assertIn("check_time", html)

    def test_empty_data_degrades_to_placeholder(self):
        with patch("common.broadcast.queries.fetch_refund_reason",
                   return_value={"months": ["2026-09"], "total_amount": 0.0,
                                 "reasons": [], "as_of": date(2026, 9, 23)}):
            html = qudao_panels.build_refund_reason_panel(
                Mock(), today=date(2026, 9, 23)
            )
        self.assertIn("暂无退款数据", html)


class GmvFactorPanelTests(unittest.TestCase):

    def test_renders_factors_and_contributions(self):
        data = {
            "months": [
                {"ym": "2026-08", "gmv": 300000.0, "buyers": 1200, "qty": 1500.0,
                 "unit_price": 200.0, "qty_per_buyer": 1.25, "contrib": None},
                {"ym": "2026-09", "gmv": 360000.0, "buyers": 1500, "qty": 1800.0,
                 "unit_price": 200.0, "qty_per_buyer": 1.2,
                 "contrib": {"buyers": 60000.0, "qty_per_buyer": -9000.0,
                             "unit_price": 9000.0, "delta": 60000.0}},
            ],
            "as_of": date(2026, 9, 23),
        }
        with patch("common.broadcast.queries.fetch_gmv_factors",
                   return_value=data):
            html = qudao_panels.build_gmv_factor_panel(
                Mock(), today=date(2026, 9, 23)
            )
        self.assertIn("🧮 GMV 三因子拆解", html)
        self.assertIn("<th>买家数</th>", html)
        self.assertIn("<th>人均件数</th>", html)
        self.assertIn("<th>件均价</th>", html)
        self.assertIn("1,200", html)               # 8 月买家数
        self.assertIn("恒等式 GMV=买家数×人均件数×件均价", html)
        self.assertIn("连环替代法", html)
        self.assertIn("高估人均件数/件均价", html)  # 买家缺失说明
        self.assertIn("+6.0万", html)              # 9 月买家数贡献

    def test_empty_data_degrades_to_placeholder(self):
        with patch("common.broadcast.queries.fetch_gmv_factors",
                   return_value={"months": [], "as_of": date(2026, 9, 23)}):
            html = qudao_panels.build_gmv_factor_panel(
                Mock(), today=date(2026, 9, 23)
            )
        self.assertIn("暂无订单数据", html)


class CohortRetentionPanelTests(unittest.TestCase):

    def _data(self):
        return {
            "cohorts": [
                {"cohort": "2026-08", "size": 100,
                 "retention": {0: 1.0, 1: 0.4}},
                {"cohort": "2026-09", "size": 50,
                 "retention": {0: 1.0}},
            ],
            "periods": [0, 1],
            "avg_retention": {0: 1.0, 1: 0.4},
            "last_ym": "2026-09",
            "as_of": date(2026, 9, 23),
        }

    def test_renders_matrix_with_offsets_and_average(self):
        with patch("common.broadcast.queries.fetch_cohort_retention",
                   return_value=self._data()):
            html = qudao_panels.build_cohort_retention_panel(
                Mock(), today=date(2026, 9, 23)
            )
        self.assertIn("🔁 Cohort 留存矩阵", html)
        self.assertIn("<th>M0</th>", html)
        self.assertIn("<th>M1</th>", html)
        self.assertIn("2026-08", html)
        self.assertIn("100%", html)               # M0 恒定
        self.assertIn("40%", html)                # M1 留存
        self.assertIn("加权平均", html)
        self.assertIn("留存率(Mk)=首购后第 k 月", html)
        self.assertIn("未满 k 月的队列不计入", html)

    def test_empty_data_degrades_to_placeholder(self):
        with patch("common.broadcast.queries.fetch_cohort_retention",
                   return_value={"cohorts": [], "periods": [],
                                 "avg_retention": {}, "last_ym": None,
                                 "as_of": date(2026, 9, 23)}):
            html = qudao_panels.build_cohort_retention_panel(
                Mock(), today=date(2026, 9, 23)
            )
        self.assertIn("暂无买家数据", html)


class BaijiuNodePanelTests(unittest.TestCase):

    def test_renders_node_elasticity(self):
        data = {
            "nodes": [
                {"node": "国庆", "days": 7, "gmv": 700000.0,
                 "daily_avg": 100000.0, "elasticity": 2.0},
            ],
            "baseline_daily": 50000.0,
            "baseline_days": 30,
            "window": ["2026-05", "2026-09", "2026-10"],
            "has_calendar": True,
            "has_data": True,
            "as_of": date(2026, 10, 13),
        }
        with patch("common.broadcast.queries.fetch_baijiu_node_elasticity",
                   return_value=data):
            html = qudao_panels.build_baijiu_node_panel(
                Mock(), today=date(2026, 10, 13)
            )
        self.assertIn("🍶 白酒节点弹性", html)
        self.assertIn("国庆", html)
        self.assertIn("2.00×", html)
        self.assertIn("平日基线日均 GMV", html)
        self.assertIn("弹性=节点日均 GMV÷平日（非节点）日均 GMV", html)

    def test_missing_node_marker_shows_data_gap(self):
        data = {
            "nodes": [], "baseline_daily": 50000.0, "baseline_days": 30,
            "window": ["2026-09", "2026-10"], "has_calendar": True,
            "has_data": True, "as_of": date(2026, 10, 13),
        }
        with patch("common.broadcast.queries.fetch_baijiu_node_elasticity",
                   return_value=data):
            html = qudao_panels.build_baijiu_node_panel(
                Mock(), today=date(2026, 10, 13)
            )
        self.assertIn("数据缺失", html)
        self.assertIn("dim_calendar 未含节点标记", html)
        self.assertIn("导入法定节假日", html)

    def test_no_order_data_degrades_to_placeholder(self):
        data = {"nodes": [], "baseline_daily": None, "baseline_days": 0,
                "window": [], "has_calendar": False, "has_data": False,
                "as_of": date(2026, 10, 13)}
        with patch("common.broadcast.queries.fetch_baijiu_node_elasticity",
                   return_value=data):
            html = qudao_panels.build_baijiu_node_panel(
                Mock(), today=date(2026, 10, 13)
            )
        self.assertIn("暂无订单数据", html)


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
        self.assertEqual(len(panels), 12)
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
