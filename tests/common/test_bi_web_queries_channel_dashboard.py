"""test_bi_web_queries 的 channel_dashboard 域测试；断言自原文件等价迁移。"""

from tests.common.bi_web_query_test_support import *


class QdDashboardTests(unittest.TestCase):
    """渠道销售BI看板（qd-channel-sales，2026-10-13 裁决）：

    汇总/趋势/排行/店铺走渠道线事实表 ``fact_channel_daily_sales``；
    未填报卡复用 ``channel_missing``，与渠道催办逐字同口径；全卡 date 锚定。
    """

    def _calendar_rows(self):
        return [
            {"business_date": date(2026, 8, 3)},
            {"business_date": date(2026, 8, 4)},
            {"business_date": date(2026, 8, 5)},
        ]

    def test_kpi_qd_today_sums_channel_fact_on_anchor(self):
        connection = FakeConnection(
            rowsets={"fact_channel_daily_sales": [{"s": Decimal("123")}]}
        )
        payload = bi_web_queries.run_kpi_qd_today(
            connection, {"date": "2026-08-04"})
        self.assertEqual(123.0, payload["value"])
        self.assertEqual("2026-08-04", payload["date"])
        sql, params = connection.executed[-1]
        self.assertIn("fact_channel_daily_sales", sql)
        self.assertEqual((date(2026, 8, 4),), params)

    def test_kpi_qd_mtd_derives_month_from_date_anchor(self):
        connection = FakeConnection(
            rowsets={"fact_channel_daily_sales": [{"s": Decimal("80")}]}
        )
        payload = bi_web_queries.run_kpi_qd_mtd(
            connection, {"date": "2026-08-04"})
        self.assertEqual(80.0, payload["value"])
        # 渠道线无 BI 月目标源：只给金额，不给达成率
        self.assertNotIn("target", payload)
        self.assertNotIn("rate", payload)
        sql, params = connection.executed[-1]
        self.assertEqual((date(2026, 8, 1), date(2026, 8, 31)), params)
        self.assertIn("CURDATE()", sql)  # 未来日截断

    def test_kpi_qd_progress_shares_company_calendar(self):
        connection = FakeConnection(
            rowsets={"dim_calendar": self._calendar_rows()}
        )
        payload = bi_web_queries.run_kpi_qd_progress(
            connection, {"date": "2026-08-04"})
        self.assertEqual(2.0, payload["value"])
        self.assertEqual(3.0, payload["target"])
        self.assertAlmostEqual(2 / 3, payload["rate"])

    def test_trend_qd_daily_delegates_to_channel_month(self):
        from unittest import mock

        connection = FakeConnection()
        with mock.patch.object(
            bi_web_channel_dashboard, "run_trend_channel_daily",
            return_value={"chart": "line"},
        ) as trend:
            payload = bi_web_queries.run_trend_qd_daily(
                connection, {"date": "2026-08-04"})
        self.assertEqual({"chart": "line"}, payload)
        trend.assert_called_once_with(connection, {"month": "2026-08"})

    def test_bar_qd_channel_delegates_to_channel_mtd(self):
        from unittest import mock

        connection = FakeConnection()
        with mock.patch.object(
            bi_web_channel_dashboard, "run_bar_channel_mtd",
            return_value={"chart": "bar"},
        ) as bar:
            payload = bi_web_queries.run_bar_qd_channel(
                connection, {"date": "2026-08-04"})
        self.assertEqual({"chart": "bar"}, payload)
        bar.assert_called_once_with(connection, {"month": "2026-08"})

    def test_table_qd_store_mtd_delegates_to_store_mtd(self):
        from unittest import mock

        connection = FakeConnection()
        with mock.patch.object(
            bi_web_channel_dashboard, "run_table_store_mtd",
            return_value={"chart": "table"},
        ) as table:
            payload = bi_web_queries.run_table_qd_store_mtd(
                connection, {"date": "2026-08-04"})
        self.assertEqual({"chart": "table"}, payload)
        table.assert_called_once_with(connection, {"month": "2026-08"})

    def _unfilled_connection(self):
        return _QdUnfilledFakeConnection(
            rowsets={
                "fact_channel_store_target": [
                    {"store_name": "天猫旗舰店", "channel": "天猫",
                     "owners_json": None},
                    {"store_name": "京东自营店", "channel": "京东",
                     "owners_json": None},
                ],
                "fact_channel_daily_sales": [
                    # 天猫旗舰店当日已填（非 0）→ 不计缺；京东自营店当日无行
                    {"store_name": "天猫旗舰店", "channel": "天猫",
                     "sales_amount": Decimal("50"), "responsible_person": None,
                     "business_date": date(2026, 10, 9)},
                ],
            }
        )

    def test_table_qd_unfilled_today_roster_bound(self):
        payload = bi_web_queries.run_table_qd_unfilled_today(
            self._unfilled_connection(), {"date": "2026-10-09"})
        self.assertEqual("2026-10-09", payload["date"])
        self.assertEqual(
            ["rank", "store", "channel"],
            [column["key"] for column in payload["columns"]],
        )
        self.assertEqual(
            [{"rank": 1, "store": "京东自营店", "channel": "京东"}],
            payload["rows"],
        )

    def test_table_qd_unfilled_today_null_row_is_missing(self):
        # 显式 NULL 行与「无行」同属缺报（与催办判定逐字一致）
        connection = _QdUnfilledFakeConnection(
            rowsets={
                "fact_channel_store_target": [
                    {"store_name": "猫超店", "channel": "猫超",
                     "owners_json": None},
                ],
                "fact_channel_daily_sales": [
                    {"store_name": "猫超店", "channel": "猫超",
                     "sales_amount": None, "responsible_person": None,
                     "business_date": date(2026, 10, 9)},
                ],
            }
        )
        payload = bi_web_queries.run_table_qd_unfilled_today(
            connection, {"date": "2026-10-09"})
        self.assertEqual(
            [{"rank": 1, "store": "猫超店", "channel": "猫超"}],
            payload["rows"],
        )

    def test_table_qd_unfilled_today_absent_day_all_missing(self):
        # 锚点日无任何事实行 → 名册全店皆缺（排序按门店名）
        payload = bi_web_queries.run_table_qd_unfilled_today(
            self._unfilled_connection(), {"date": "2026-10-08"})
        self.assertEqual(
            ["京东自营店", "天猫旗舰店"],
            [row["store"] for row in payload["rows"]],
        )

    # —— 旧页 13 个渠道专属面板迁移卡 smoke（2026-10-13，共 21 卡）——
    # 面板 1-8 不新写 SQL：mock 掉 common.broadcast.queries 的既有 fetch_*，
    # 只验证载荷映射（列定义 + 行值 + date）不丢信息、不二次推导。

    def _assert_table(self, payload, anchor_iso="2026-08-04"):
        self.assertEqual("table", payload["chart"])
        self.assertEqual(anchor_iso, payload["date"])
        self.assertIn("columns", payload)
        self.assertIn("rows", payload)

    def test_table_qd_monthly_kpi_maps_months(self):
        from unittest import mock

        from common.broadcast import queries as bq

        month = {
            "ym": "2026-08", "gmv": 1.0, "mom": 0.1, "orders": 2,
            "aov": 3.0, "buyers": 4, "new_buyers": 1, "repeat_rate": 0.75,
            "cancel_orders": 0, "cancel_rate": 0.0, "refund_amt": 0.5,
            "refund_rate_amt": 0.5, "net_gmv": 0.5, "promo": 0.1, "roi": 5.0,
        }
        with mock.patch.object(
            bq, "fetch_monthly_kpi", return_value={"months": [month]}
        ):
            payload = bi_web_queries.run_table_qd_monthly_kpi(
                FakeConnection(), {"date": "2026-08-04"})
        self._assert_table(payload)
        self.assertEqual("月份", payload["columns"][0]["title"])
        self.assertEqual([{"ym": "2026-08", "gmv": 1.0, "mom": 0.1,
                           "orders": 2, "aov": 3.0, "buyers": 4,
                           "new_buyers": 1, "repeat_rate": 0.75,
                           "cancel_orders": 0, "cancel_rate": 0.0,
                           "refund_amt": 0.5, "refund_rate_amt": 0.5,
                           "net_gmv": 0.5, "promo": 0.1, "roi": 5.0}],
                         payload["rows"])

    def _expense_data(self):
        return {
            "months": ["2026-08"],
            "rows": [
                {"channel": "天猫", "total": 10.0,
                 "cells": {"2026-08": {"total": 10.0, "rate": 0.2}}},
            ],
            "empty_months": [],
            "unmapped_stores": [],
        }

    def test_table_qd_expense_maps_channel_cells(self):
        from unittest import mock

        from common.broadcast import queries as bq

        with mock.patch.object(
            bq, "fetch_expense_summary", return_value=self._expense_data()
        ):
            payload = bi_web_queries.run_table_qd_expense(
                FakeConnection(), {"date": "2026-08-04"})
        self._assert_table(payload)
        self.assertEqual(
            ["channel", "2026-08", "total"],
            [c["key"] for c in payload["columns"]],
        )
        self.assertEqual(
            [{"channel": "天猫", "2026-08": 10.0, "total": 10.0}],
            payload["rows"],
        )

    def test_table_qd_expense_rate_maps_channel_cells(self):
        from unittest import mock

        from common.broadcast import queries as bq

        with mock.patch.object(
            bq, "fetch_expense_summary", return_value=self._expense_data()
        ):
            payload = bi_web_queries.run_table_qd_expense_rate(
                FakeConnection(), {"date": "2026-08-04"})
        self._assert_table(payload)
        self.assertEqual(
            [{"channel": "天猫", "2026-08": 0.2}], payload["rows"])
        self.assertEqual("percent", payload["columns"][1]["format"])

    def _bill_data(self):
        return {
            "months": ["2026-08"],
            "by_category": [{"category": "店铺扣点", "total": 6.0}],
            "by_platform": [{"platform": "天猫", "total": 6.0, "share": 0.6}],
            "top_items": [{"item": "佣金", "total": 4.0, "share": 0.4}],
            "other_items": {"count": 3, "total": 2.0},
            "grand_total": 10.0,
            "empty_months": [],
        }

    def test_table_qd_bill_category_share(self):
        from unittest import mock

        from common.broadcast import queries as bq

        with mock.patch.object(
            bq, "fetch_bill_breakdown", return_value=self._bill_data()
        ):
            payload = bi_web_queries.run_table_qd_bill_category(
                FakeConnection(), {"date": "2026-08-04"})
        self._assert_table(payload)
        self.assertEqual(
            [{"category": "店铺扣点", "total": 6.0, "share": 0.6}],
            payload["rows"],
        )

    def test_table_qd_bill_platform_maps_share(self):
        from unittest import mock

        from common.broadcast import queries as bq

        with mock.patch.object(
            bq, "fetch_bill_breakdown", return_value=self._bill_data()
        ):
            payload = bi_web_queries.run_table_qd_bill_platform(
                FakeConnection(), {"date": "2026-08-04"})
        self._assert_table(payload)
        self.assertEqual(
            [{"platform": "天猫", "total": 6.0, "share": 0.6}],
            payload["rows"],
        )

    def test_table_qd_bill_items_appends_other_count(self):
        from unittest import mock

        from common.broadcast import queries as bq

        with mock.patch.object(
            bq, "fetch_bill_breakdown", return_value=self._bill_data()
        ):
            payload = bi_web_queries.run_table_qd_bill_items(
                FakeConnection(), {"date": "2026-08-04"})
        self._assert_table(payload)
        self.assertEqual(
            [{"item": "佣金", "total": 4.0, "share": 0.4},
             {"item": "其他 3 项", "total": 2.0, "share": 0.2}],
            payload["rows"],
        )

    def test_table_qd_bill_items_skips_other_when_zero(self):
        from unittest import mock

        from common.broadcast import queries as bq

        data = self._bill_data()
        data["other_items"] = {"count": 0, "total": 0.0}
        with mock.patch.object(bq, "fetch_bill_breakdown", return_value=data):
            payload = bi_web_queries.run_table_qd_bill_items(
                FakeConnection(), {"date": "2026-08-04"})
        self.assertEqual(1, len(payload["rows"]))

    def _product_data(self):
        return {
            "series": [{"series": "酱香", "gmv": 5.0, "share": 0.5}],
            "price_bands": [{"band": "300-500", "gmv": 5.0, "share": 0.5}],
            "top_products": [{"goods_name": "XX", "spec_no": "S1",
                              "series_name": "酱香", "gmv": 3.0, "share": 0.3}],
            "total_gmv": 10.0,
            "top10_share": 0.9,
            "hhi": 0.2,
        }

    def test_table_qd_product_cards_map_structure(self):
        from unittest import mock

        from common.broadcast import queries as bq

        with mock.patch.object(
            bq, "fetch_product_structure", return_value=self._product_data()
        ):
            series = bi_web_queries.run_table_qd_product_series(
                FakeConnection(), {"date": "2026-08-04"})
            bands = bi_web_queries.run_table_qd_product_bands(
                FakeConnection(), {"date": "2026-08-04"})
            top = bi_web_queries.run_table_qd_product_top(
                FakeConnection(), {"date": "2026-08-04"})
            summary = bi_web_queries.run_table_qd_product_summary(
                FakeConnection(), {"date": "2026-08-04"})
        for payload in (series, bands, top, summary):
            self._assert_table(payload)
        self.assertEqual(
            [{"series": "酱香", "gmv": 5.0, "share": 0.5}], series["rows"])
        self.assertEqual(
            [{"band": "300-500", "gmv": 5.0, "share": 0.5}], bands["rows"])
        self.assertEqual("XX", top["rows"][0]["goods_name"])
        self.assertEqual(
            [{"total_gmv": 10.0, "top10_share": 0.9, "hhi": 0.2}],
            summary["rows"],
        )

    def test_table_qd_product_top_falls_back_to_spec_no(self):
        from unittest import mock

        from common.broadcast import queries as bq

        data = self._product_data()
        data["top_products"][0]["goods_name"] = None
        with mock.patch.object(bq, "fetch_product_structure", return_value=data):
            payload = bi_web_queries.run_table_qd_product_top(
                FakeConnection(), {"date": "2026-08-04"})
        self.assertEqual("S1", payload["rows"][0]["goods_name"])

    def test_table_qd_refund_reason_maps(self):
        from unittest import mock

        from common.broadcast import queries as bq

        data = {"reasons": [{"reason": "质量问题", "amount": 1.0,
                             "orders": 2, "share": 0.5}]}
        with mock.patch.object(bq, "fetch_refund_reason", return_value=data):
            payload = bi_web_queries.run_table_qd_refund_reason(
                FakeConnection(), {"date": "2026-08-04"})
        self._assert_table(payload)
        self.assertEqual(data["reasons"], payload["rows"])

    def test_table_qd_gmv_factors_maps_contrib(self):
        from unittest import mock

        from common.broadcast import queries as bq

        data = {"months": [{
            "ym": "2026-08", "gmv": 10.0, "buyers": 4, "qty": 8,
            "qty_per_buyer": 2.0, "unit_price": 1.25,
            "contrib": {"buyers": 0.1, "qty_per_buyer": 0.2,
                        "unit_price": 0.3},
        }, {
            "ym": "2026-07", "gmv": 9.0, "buyers": 3, "qty": 6,
            "qty_per_buyer": 2.0, "unit_price": 1.5, "contrib": None,
        }]}
        with mock.patch.object(bq, "fetch_gmv_factors", return_value=data):
            payload = bi_web_queries.run_table_qd_gmv_factors(
                FakeConnection(), {"date": "2026-08-04"})
        self._assert_table(payload)
        self.assertEqual(0.1, payload["rows"][0]["c_buyers"])
        self.assertIsNone(payload["rows"][1]["c_buyers"])

    def test_table_qd_cohort_appends_weighted_average_row(self):
        from unittest import mock

        from common.broadcast import queries as bq

        data = {
            "periods": [0, 1],
            "cohorts": [{"cohort": "2026-07", "size": 10,
                         "retention": {0: 1.0, 1: 0.5}}],
            "avg_retention": {0: 1.0, 1: 0.6},
            "last_ym": "2026-08",
        }
        with mock.patch.object(bq, "fetch_cohort_retention", return_value=data):
            payload = bi_web_queries.run_table_qd_cohort(
                FakeConnection(), {"date": "2026-08-04"})
        self._assert_table(payload)
        self.assertEqual("加权平均", payload["rows"][-1]["cohort"])
        self.assertEqual(0.6, payload["rows"][-1]["m1"])
        self.assertIsNone(payload["rows"][-1]["size"])

    def _channel_conn(self, month_facts, channel_date, targets):
        from unittest import mock

        connection = FakeConnection()
        patches = [
            mock.patch(
                "common.daily_robot.channel_daily.fetch_channel_month_facts",
                return_value=month_facts,
            ),
            mock.patch(
                "common.daily_robot.channel_daily.resolve_channel_business_date",
                return_value=channel_date,
            ),
            mock.patch(
                "common.metrics.daily_report.fetch_channel_monthly_targets",
                return_value=targets,
            ),
        ]
        return connection, patches

    def test_table_qd_channel_daily_empty_when_no_business_date(self):
        connection, patches = self._channel_conn({}, None, {})
        for patch in patches:
            patch.start()
        try:
            payload = bi_web_queries.run_table_qd_channel_daily(
                connection, {"date": "2026-08-04"})
        finally:
            for patch in patches:
                patch.stop()
        self._assert_table(payload)
        self.assertEqual([], payload["rows"])

    def test_table_qd_channel_daily_reuses_collect_rows(self):
        from unittest import mock

        connection, patches = self._channel_conn(
            {"天猫": {date(2026, 8, 1): 5.0, date(2026, 8, 2): 7.0}},
            date(2026, 8, 2),
            {"天猫": 20.0},
        )
        collected = (12.0, [{"channel": "天猫", "sales": 7.0, "mom": 0.4,
                             "mom_txt": "+40.0%", "target_txt": "--",
                             "rate_txt": "--"}])
        patches.append(mock.patch(
            "common.daily_robot.channel_daily.collect_channel_rows",
            return_value=collected,
        ))
        for patch in patches:
            patch.start()
        try:
            payload = bi_web_queries.run_table_qd_channel_daily(
                connection, {"date": "2026-08-04"})
        finally:
            for patch in patches:
                patch.stop()
        self._assert_table(payload)
        # 表体不含「全渠道」行（避免前端 wan 列合计 footer 重复计一次）；
        # 合计见 run_table_qd_notes 的文字披露。
        tianmao = payload["rows"][0]
        self.assertEqual("天猫", tianmao["channel"])
        self.assertEqual(7.0, tianmao["sales"])
        self.assertEqual(20.0, tianmao["target"])
        self.assertAlmostEqual(12.0 / 20.0, tianmao["rate"])

    def test_table_qd_hot_items_translates_state(self):
        from unittest import mock

        from common.broadcast import queries as bq

        data = {"items": [
            {"goods_name": "A", "spec_no": "S1", "available_qty": -1,
             "days_left": 0, "qty_30d": 9, "purchase_intransit_qty": 1,
             "stock_state": "OVERSOLD"},
            {"goods_name": "B", "spec_no": "S2", "available_qty": 2,
             "days_left": 3, "qty_30d": 4, "purchase_intransit_qty": 0,
             "stock_state": "URGENT"},
            {"goods_name": "C", "spec_no": "S3", "available_qty": 9,
             "days_left": 30, "qty_30d": 1, "purchase_intransit_qty": 0,
             "stock_state": "OK"},
        ]}
        with mock.patch.object(bq, "fetch_hot_items", return_value=data):
            payload = bi_web_queries.run_table_qd_hot_items(
                FakeConnection(), {"date": "2026-08-04"})
        self._assert_table(payload)
        self.assertEqual(
            ["断货", "偏低", "正常"],
            [row["state"] for row in payload["rows"]],
        )

    def test_table_qd_stock_oversold_splits_oversold_and_zero(self):
        from unittest import mock

        from common.broadcast import queries as bq

        data = {
            "urgent": [{"goods_name": "U", "spec_no": "SU",
                        "available_qty": 1, "daily_avg": 0.5,
                        "days_left": 2}],
            "oversold": [{"goods_name": "O", "spec_no": "SO",
                          "available_qty": -3, "daily_avg": 1.0}],
            "zero_stock": [{"goods_name": "Z", "spec_no": "SZ",
                            "daily_avg": 2.0}],
        }
        with mock.patch.object(bq, "fetch_stock_alerts", return_value=data):
            urgent = bi_web_queries.run_table_qd_stock_urgent(
                FakeConnection(), {"date": "2026-08-04"})
            oversold = bi_web_queries.run_table_qd_stock_oversold(
                FakeConnection(), {"date": "2026-08-04"})
        self._assert_table(urgent)
        self.assertEqual(data["urgent"], urgent["rows"])
        self._assert_table(oversold)
        self.assertEqual(
            [{"goods_name": "O", "spec_no": "SO", "qty": 3.0,
              "daily_avg": 1.0, "state": "超卖"},
             {"goods_name": "Z", "spec_no": "SZ", "qty": 0,
              "daily_avg": 2.0, "state": "零库存在售"}],
            oversold["rows"],
        )

    def test_qd_table_normalizes_decimal_and_date_for_json(self):
        """载荷出口归一 Decimal/日期——旧页 fetch_* 直传 pymysql 原始行，
        Decimal 非 JSON 可编码类型，app 层会落 500 card_error
        （2026-10-13 实测 table_qd_stock_urgent/oversold）。"""
        import json

        payload = bi_web_queries._qd_table(
            [{"key": "v", "title": "值"}],
            [{"v": Decimal("1.25"), "d": date(2026, 8, 4),
              "t": datetime(2026, 8, 4, 10, 30), "n": None}],
            date(2026, 8, 4),
        )
        self.assertEqual(
            {"v": 1.25, "d": "2026-08-04",
             "t": "2026-08-04T10:30:00", "n": None},
            payload["rows"][0],
        )
        json.dumps(payload)  # 归一后必须可编码

    def test_table_qd_purchase_inbound_flattens_groups(self):
        from unittest import mock

        from common.broadcast import queries as bq

        data = {"groups": [{
            "warehouse_name": "总仓",
            "specs": [{"spec_no": "S1", "goods_name": "A", "qty": 5}],
        }]}
        with mock.patch.object(bq, "fetch_purchase_inbound", return_value=data) as f:
            payload = bi_web_queries.run_table_qd_purchase_inbound(
                FakeConnection(), {"date": "2026-08-04"})
        self._assert_table(payload)
        # 忽略日期筛选：取数用真实时钟（now=datetime.now()）。
        self.assertIsNotNone(f.call_args.kwargs["now"])
        self.assertEqual(
            [{"warehouse": "总仓", "spec_no": "S1",
              "goods_name": "A", "qty": 5}], payload["rows"])

    def test_table_qd_order_risk_flattens_areas_with_anchor_minus_one(self):
        from unittest import mock

        from common.broadcast import queries as bq

        data = {
            "groups": [{"shop_name": "店A", "platform": "天猫",
                        "areas": [{"area": "杭州", "count": 6}]}],
            "incomplete_groups": [{"shop_name": "店B", "platform": "拼多多",
                                   "areas": [{"area": "", "count": 3}]}],
            "threshold": 5,
            "pdd_no_area_count": 2,
        }
        with mock.patch.object(bq, "fetch_order_risk", return_value=data) as f:
            payload = bi_web_queries.run_table_qd_order_risk(
                FakeConnection(), {"date": "2026-08-04"})
        self._assert_table(payload)
        self.assertEqual(date(2026, 8, 3), f.call_args.kwargs["business_date"])
        self.assertEqual("", payload["rows"][0]["mark"])
        self.assertEqual(
            "地区字段不全（仅供参考）", payload["rows"][1]["mark"])

    def test_table_qd_notes_renders_sections(self):
        from unittest import mock

        from common.broadcast import queries as bq

        with (
            mock.patch.object(bq, "fetch_expense_summary", return_value={
                "months": ["2026-08"], "rows": [],
                "empty_months": [], "unmapped_stores": []}),
            mock.patch.object(bq, "fetch_bill_breakdown", return_value={
                "by_category": [], "by_platform": [], "top_items": [],
                "other_items": {"count": 0, "total": 0.0},
                "grand_total": 0.0, "empty_months": []}),
            mock.patch.object(bq, "fetch_cohort_retention", return_value={
                "periods": [0], "cohorts": [],
                "avg_retention": {0: 1.0}, "last_ym": "2026-08"}),
            mock.patch.object(bq, "fetch_hot_items", return_value={
                "items": [], "moving_window_days": 15}),
            mock.patch.object(bq, "fetch_stock_alerts", return_value={
                "urgent": [], "oversold": [], "zero_stock": [],
                "moving_window_days": 15}),
            mock.patch.object(bq, "fetch_purchase_inbound", return_value={
                "groups": [], "total_qty": 0.0, "total_lines": 0,
                "total_specs": 0, "total_orders": 0}),
            mock.patch.object(bq, "fetch_order_risk", return_value={
                "groups": [], "incomplete_groups": [], "threshold": 5,
                "pdd_no_area_count": 0}),
            mock.patch(
                "common.daily_robot.channel_daily.fetch_channel_month_facts",
                return_value={}),
            mock.patch(
                "common.daily_robot.channel_daily."
                "resolve_channel_business_date",
                return_value=None),
        ):
            payload = bi_web_queries.run_table_qd_notes(
                FakeConnection(), {"date": "2026-08-04"})
        self._assert_table(payload)
        self.assertEqual(["section", "note"],
                         [c["key"] for c in payload["columns"]])
        sections = [row["section"] for row in payload["rows"]]
        for expected in ("月度核心指标", "费用/盈利", "账单费用拆解",
                         "商品结构", "退款原因", "GMV 三因子",
                         "Cohort 留存", "渠道日销快报",
                         "热卖品监控", "库存补货提醒", "采购入库（近24小时）",
                         "订单风险防控"):
            self.assertIn(expected, sections)
