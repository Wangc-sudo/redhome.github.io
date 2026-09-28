"""渠道播报 DB 化投影器测试（2026-09-23）。"""

import json
import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock

from common.public_data.extract_channel_ops import (
    WAREHOUSE_MAP,
    _business_date_of,
    project_inventory_snapshot,
    project_purchase_inbound,
    project_sales_daily,
)
from common.public_data.mart_extract_schema import dataset_by_name

_RUN_ID = "00000000-0000-0000-0000-0000000000c1"
_NOW = datetime(2026, 9, 23, 8, 0)
_BUSINESS_DATE = date(2026, 9, 23)


def _stock_spec(spec_no="习酒493", warehouse_no="01", **overrides):
    payload = {
        "rec_id": 10077,
        "spec_no": spec_no,
        "warehouse_no": warehouse_no,
        "goods_name": "53°100ml习酒",
        "available_send_stock": 12.0,
        "stock_num": 20.0,
        "purchase_num": 6.0,
        "defect": False,
    }
    payload.update(overrides)
    return {"source_record_id": str(payload["rec_id"]), "payload_json": json.dumps(payload, ensure_ascii=False)}


def _purchase(order_no="RK2609220011", details=(), **overrides):
    payload = {
        "order_no": order_no,
        "purchase_no": "CG20260920001",
        "warehouse_no": "01",
        "status": 80,
        "check_time": 1787448301000,
        "details_list": list(details),
    }
    payload.update(overrides)
    return {"source_record_id": order_no, "payload_json": json.dumps(payload, ensure_ascii=False)}


def _purchase_detail(spec_no="习酒493", num="24", spec_name="53°100ml习酒"):
    return {"spec_no": spec_no, "spec_name": spec_name, "num": num, "goods_name": "fallback名"}


class BusinessDateTests(unittest.TestCase):
    """synced_at（UTC 口径）→ 北京业务日（2026-09-23 生产实锤回归）。"""

    def test_utc_midnight_maps_to_beijing_same_morning(self):
        # 北京 09-23 08:00 = UTC 09-23 00:00 → 业务日必须是 09-23
        self.assertEqual(
            _business_date_of(datetime(2026, 9, 23, 0, 5, tzinfo=timezone.utc)),
            date(2026, 9, 23),
        )

    def test_utc_late_evening_rolls_to_next_beijing_day(self):
        # UTC 09-22 23:52 = 北京 09-23 07:52
        self.assertEqual(
            _business_date_of(datetime(2026, 9, 22, 23, 52, tzinfo=timezone.utc)),
            date(2026, 9, 23),
        )

    def test_naive_treated_as_utc(self):
        self.assertEqual(
            _business_date_of(datetime(2026, 9, 22, 23, 52)),
            date(2026, 9, 23),
        )


class SalesDailyProjectorTests(unittest.TestCase):

    def test_full_replace_from_stockout_aggregate(self):
        ds = dataset_by_name("wdt_sales_daily")
        repo = Mock()
        repo.read_stockout_daily_sales.return_value = [
            {"business_date": date(2026, 9, 22), "spec_no": "习酒493",
             "warehouse_no": "01", "qty_sold": 30.0},
            {"business_date": date(2026, 9, 22), "spec_no": "习酒493",
             "warehouse_no": "12", "qty_sold": 5.0},
        ]
        repo.replace_sales_daily.side_effect = lambda _d, _c, rows: len(rows)

        result = project_sales_daily(repo, ds, _RUN_ID, _NOW)

        _ds, columns, rows = repo.replace_sales_daily.call_args.args
        self.assertIn("business_date", columns)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["qty_sold"], 30.0)
        self.assertEqual(rows[0]["sync_run_id"], _RUN_ID)
        self.assertEqual(result["records_new"], 2)
        self.assertEqual(
            result["_record_ids"][0], "2026-09-22:习酒493:01"
        )


class InventorySnapshotProjectorTests(unittest.TestCase):

    def _repo(self, records=(), sales=None):
        repo = Mock()
        repo.read_wdt_trades.return_value = list(records)
        repo.read_sales_window_sums.return_value = dict(sales or {})
        repo.replace_inventory_day.side_effect = lambda _d, _c, rows, _bd: len(rows)
        return repo

    def test_merges_warehouses_and_classifies_state(self):
        ds = dataset_by_name("wdt_inventory_sku_daily")
        records = [
            _stock_spec(warehouse_no="01", available_send_stock=10.0, stock_num=15.0,
                        purchase_num=4.0, num_7days=7.0, num_month=30.0),
            _stock_spec(warehouse_no="12", available_send_stock=-2.0, stock_num=5.0,
                        purchase_num=2.0, num_7days=3.0, num_month=10.0),
            _stock_spec(spec_no="次品X", warehouse_no="01", defect=True),   # 次品剔除
            _stock_spec(spec_no="外仓Y", warehouse_no="99"),                # 非监控仓剔除
        ]
        # 窗口 15 天销 30 → daily_avg=2；available=8 → days_left=4 ≤7 → URGENT
        repo = self._repo(records, sales={"习酒493": (30.0, 60.0)})

        result = project_inventory_snapshot(repo, ds, _RUN_ID, _NOW)

        repo.read_sales_window_sums.assert_called_once_with(
            _BUSINESS_DATE, window_days=15, active_days=30
        )
        _ds, _c, rows, business_date = repo.replace_inventory_day.call_args.args
        self.assertEqual(business_date, _BUSINESS_DATE)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["spec_no"], "习酒493")
        self.assertEqual(row["warehouse_scope"], "ALL")
        self.assertEqual(row["available_qty"], 8.0)          # 10 + (-2) 跨仓合并
        self.assertEqual(row["stock_qty"], 20.0)
        self.assertEqual(row["purchase_intransit_qty"], 6.0)
        self.assertEqual(row["qty_7days"], 10.0)             # mask=1 桶值求和
        self.assertEqual(row["qty_month"], 40.0)
        self.assertEqual(row["daily_avg"], 2.0)              # 30 / 15
        self.assertEqual(row["days_left"], 4.0)              # 8 / 2
        self.assertEqual(row["moving_window_days"], 15)
        self.assertEqual(row["is_moving"], 1)
        self.assertEqual(row["stock_state"], "URGENT")
        self.assertEqual(result["records_new"], 1)

    def test_state_matrix(self):
        ds = dataset_by_name("wdt_inventory_sku_daily")
        records = [
            _stock_spec(spec_no="超卖A", available_send_stock=-3.0),
            _stock_spec(spec_no="滞销B", available_send_stock=-5.0),
            _stock_spec(spec_no="健康C", available_send_stock=100.0),
            _stock_spec(spec_no="死库D", available_send_stock=50.0),
        ]
        sales = {
            "超卖A": (10.0, 20.0),   # 仍在卖 + 负库存 → OVERSOLD
            # 滞销B：30 天无动销 → DEAD（负库存也不算超卖）
            "健康C": (15.0, 30.0),   # daily_avg=1, days_left=100 → HEALTHY
        }
        repo = self._repo(records, sales=sales)

        project_inventory_snapshot(repo, ds, _RUN_ID, _NOW)

        rows = {r["spec_no"]: r for r in repo.replace_inventory_day.call_args.args[2]}
        self.assertEqual(rows["超卖A"]["stock_state"], "OVERSOLD")
        # 超卖时 days_left 为负（缺口相当于 N 天销量），投影期如实落库
        self.assertEqual(rows["超卖A"]["days_left"], -4.5)  # -3 / (10/15)
        self.assertEqual(rows["滞销B"]["stock_state"], "DEAD")
        self.assertEqual(rows["滞销B"]["is_moving"], 0)
        self.assertEqual(rows["健康C"]["stock_state"], "HEALTHY")
        self.assertEqual(rows["健康C"]["days_left"], 100.0)
        self.assertEqual(rows["死库D"]["stock_state"], "DEAD")

    def test_missing_mask_fields_stay_none(self):
        ds = dataset_by_name("wdt_inventory_sku_daily")
        repo = self._repo([_stock_spec()], sales={"习酒493": (0.0, 5.0)})

        project_inventory_snapshot(repo, ds, _RUN_ID, _NOW)

        row = repo.replace_inventory_day.call_args.args[2][0]
        self.assertIsNone(row["qty_7days"])   # 存量无 mask 的行保持 None
        self.assertIsNone(row["qty_month"])
        self.assertIsNone(row["daily_avg"])   # 窗口无动销 → None（不伪报 0）
        self.assertEqual(row["stock_state"], "HEALTHY")  # 30 天兜底有动销 + 库存 12

    def test_bad_payloads_are_skipped(self):
        ds = dataset_by_name("wdt_inventory_sku_daily")
        repo = self._repo([
            {"source_record_id": "x", "payload_json": "not json {"},
            _stock_spec(spec_no=""),
            _stock_spec(spec_no="有效Z"),
        ])

        result = project_inventory_snapshot(repo, ds, _RUN_ID, _NOW)

        rows = repo.replace_inventory_day.call_args.args[2]
        self.assertEqual([r["spec_no"] for r in rows], ["有效Z"])
        self.assertEqual(result["records_new"], 1)


class PurchaseInboundProjectorTests(unittest.TestCase):

    def _repo(self, orders=()):
        repo = Mock()
        repo.read_wdt_trades.return_value = list(orders)
        repo.upsert_purchase_inbound.side_effect = lambda _d, _c, rows: len(rows)
        return repo

    def test_expands_details_and_maps_columns(self):
        ds = dataset_by_name("wdt_purchase_inbound")
        repo = self._repo([_purchase(details=[
            _purchase_detail(),
            _purchase_detail(spec_no="女儿红41", num="12", spec_name=""),
        ])])

        result = project_purchase_inbound(repo, ds, _RUN_ID, _NOW)

        _ds, columns, rows = repo.upsert_purchase_inbound.call_args.args
        self.assertIn("stockin_time", columns)
        self.assertEqual(len(rows), 2)
        row = rows[0]
        self.assertEqual(row["order_no"], "RK2609220011")
        self.assertEqual(row["purchase_no"], "CG20260920001")
        self.assertEqual(row["warehouse_name"], WAREHOUSE_MAP["01"])
        self.assertEqual(row["goods_name"], "53°100ml习酒")
        self.assertEqual(str(row["qty"]), "24")
        self.assertEqual(row["status"], "80")
        self.assertEqual((row["stockin_time"].year, row["stockin_time"].month), (2026, 8))
        # spec_name 为空回退 goods_name
        self.assertEqual(rows[1]["goods_name"], "fallback名")
        self.assertEqual(result["records_new"], 2)
        self.assertEqual(result["_record_ids"], ["RK2609220011:习酒493", "RK2609220011:女儿红41"])

    def test_unknown_warehouse_keeps_raw_no(self):
        ds = dataset_by_name("wdt_purchase_inbound")
        repo = self._repo([_purchase(warehouse_no="07", details=[_purchase_detail()])])

        project_purchase_inbound(repo, ds, _RUN_ID, _NOW)

        row = repo.upsert_purchase_inbound.call_args.args[2][0]
        self.assertEqual(row["warehouse_name"], "07")

    def test_bad_payloads_are_skipped(self):
        ds = dataset_by_name("wdt_purchase_inbound")
        repo = self._repo([
            {"source_record_id": "x", "payload_json": "not json {"},
            _purchase(order_no="", details=[_purchase_detail()]),
            _purchase(order_no="RK-ok", details=[{"num": "1"}, _purchase_detail(spec_no="SP-1")]),
        ])

        result = project_purchase_inbound(repo, ds, _RUN_ID, _NOW)

        rows = repo.upsert_purchase_inbound.call_args.args[2]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["spec_no"], "SP-1")
        self.assertEqual(result["records_new"], 1)


if __name__ == "__main__":
    unittest.main()
