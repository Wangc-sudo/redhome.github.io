"""Tests for the WDT stock-flow projections（出库 / 退货行事实，2026-09-21）."""

import json
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from common.public_data.extract_mart import MartExtractError
from common.public_data.extract_stock_flow import (
    _REFUND_LINE_COLUMNS,
    _REFUND_METHOD,
    _STOCKOUT_LINE_COLUMNS,
    _STOCKOUT_METHOD,
    project_refund_lines,
    project_stockout_lines,
)
from common.public_data.mart_extract_schema import dataset_by_name
from tests.common import test_public_data_extract_mart as helpers

_NOW, _RUN_ID, _Ctx = helpers._NOW, helpers._RUN_ID, helpers._Ctx


def _stockout(order_no="CK2162992", details=(), shop_name="习水村-抖音习酒旗舰店"):
    return {
        "source_record_id": order_no,
        "payload_json": json.dumps({
            "order_no": order_no,
            "consign_time": "2026-09-20 18:23:38",
            "status": "110",
            "warehouse_no": "01",
            "warehouse_name": "杭州主仓",
            "shop_name": shop_name,
            "trade_no": "JY20260920001",
            "logistics_no": "SF123456",
            "logistics_name": "顺丰",
            "details_list": list(details),
        }, ensure_ascii=False),
    }


def _stockout_detail(spec_no="习酒493", num="2", paid="199.00"):
    return {
        "spec_no": spec_no,
        "goods_name": "53°100ml习酒",
        "brand_name": "习酒",
        "num": num,
        "sell_price": "99.50",
        "paid": paid,
    }


def _refund(order_no="RK2609200130", details=(), shop_name="习水村-拼多多习酒专卖店"):
    return {
        "source_record_id": order_no,
        "payload_json": json.dumps({
            "order_no": order_no,
            "refund_no": "TK2609180153",
            "check_time": 1789865948000,
            "process_status": "90",
            "warehouse_no": "01",
            "shop_name": shop_name,
            "reason": "质量问题",
            "logistics_no": "YT987654",
            "details_list": list(details),
        }, ensure_ascii=False),
    }


def _refund_detail(spec_no="习酒493", num="3"):
    return {
        "spec_no": spec_no,
        "goods_name": "53°100ml习酒",
        "brand_name": "习酒",
        "num": num,
        "stockin_num": num,
        "refund_amount": "150.00",
        "actual_refund_amount": "150.00",
    }


class StockoutLineProjectionTests(unittest.TestCase):

    def _repo(self, orders=()):
        repo = Mock()
        repo.read_wdt_trades.return_value = list(orders)
        repo.replace_table.side_effect = lambda _t, _c, rows: len(rows)
        return repo

    def test_expands_details_list_and_maps_columns(self):
        ds = dataset_by_name("wdt_stockout_line_fact")
        repo = self._repo(orders=[_stockout(details=[
            _stockout_detail(),
            _stockout_detail(spec_no="习酒123", num="1", paid="9.9"),
        ])])

        result = project_stockout_lines(repo, ds, _RUN_ID, _NOW)

        repo.read_wdt_trades.assert_called_once_with(_STOCKOUT_METHOD)
        table, columns, rows = repo.replace_table.call_args.args
        self.assertEqual(table, "fact_stockout_line")
        self.assertEqual(
            set(columns),
            set(_STOCKOUT_LINE_COLUMNS) | {"synced_at", "sync_run_id"},
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["order_no"], "CK2162992")
        self.assertEqual(rows[0]["line_no"], 1)
        self.assertEqual(rows[0]["consign_time"].year, 2026)
        self.assertEqual(rows[0]["warehouse_no"], "01")
        self.assertEqual(rows[0]["channel_name"], "抖音")
        self.assertEqual(rows[0]["brand_name"], "习酒")
        self.assertEqual(rows[0]["quantity"], Decimal("2"))
        self.assertEqual(rows[0]["sell_price"], Decimal("99.50"))
        self.assertEqual(rows[0]["paid_amount"], Decimal("199.00"))
        self.assertEqual(rows[1]["line_no"], 2)
        self.assertEqual(rows[0]["synced_at"], _NOW)
        self.assertEqual(rows[0]["sync_run_id"], _RUN_ID)
        self.assertEqual(result["records_read"], 1)
        self.assertEqual(result["records_new"], 2)
        self.assertEqual(result["_record_ids"], ["CK2162992:1", "CK2162992:2"])

    def test_missing_brand_falls_back_to_unmatched(self):
        ds = dataset_by_name("wdt_stockout_line_fact")
        detail = _stockout_detail()
        del detail["brand_name"]
        repo = self._repo(orders=[_stockout(details=[detail])])

        project_stockout_lines(repo, ds, _RUN_ID, _NOW)

        rows = repo.replace_table.call_args.args[2]
        self.assertEqual(rows[0]["brand_name"], "未匹配")

    def test_bad_payload_and_missing_keys_are_skipped(self):
        ds = dataset_by_name("wdt_stockout_line_fact")
        repo = self._repo(orders=[
            {"source_record_id": "x1", "payload_json": "not json {"},
            {"source_record_id": "x2", "payload_json": json.dumps({
                "details_list": [_stockout_detail()],  # 无 order_no
            })},
            _stockout(order_no="CK-ok", details=[
                {"goods_name": "缺规格"},  # 无 spec_no
                "not-a-dict",
                _stockout_detail(spec_no="SP-9"),
            ]),
        ])

        result = project_stockout_lines(repo, ds, _RUN_ID, _NOW)

        rows = repo.replace_table.call_args.args[2]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["order_no"], "CK-ok")
        self.assertEqual(rows[0]["spec_no"], "SP-9")
        self.assertEqual(result["records_new"], 1)


class RefundLineProjectionTests(unittest.TestCase):

    def _repo(self, orders=()):
        repo = Mock()
        repo.read_wdt_trades.return_value = list(orders)
        repo.replace_table.side_effect = lambda _t, _c, rows: len(rows)
        return repo

    def test_expands_details_list_and_maps_columns(self):
        ds = dataset_by_name("wdt_refund_line_fact")
        repo = self._repo(orders=[_refund(details=[_refund_detail()])])

        result = project_refund_lines(repo, ds, _RUN_ID, _NOW)

        repo.read_wdt_trades.assert_called_once_with(_REFUND_METHOD)
        table, columns, rows = repo.replace_table.call_args.args
        self.assertEqual(table, "fact_refund_line")
        self.assertEqual(
            set(columns),
            set(_REFUND_LINE_COLUMNS) | {"synced_at", "sync_run_id"},
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["order_no"], "RK2609200130")
        self.assertEqual(row["refund_no"], "TK2609180153")
        self.assertEqual((row["check_time"].year, row["check_time"].month), (2026, 9))
        self.assertEqual(row["process_status"], "90")
        self.assertEqual(row["channel_name"], "拼多多")
        self.assertEqual(row["reason"], "质量问题")
        self.assertEqual(row["quantity"], Decimal("3"))
        self.assertEqual(row["stockin_quantity"], Decimal("3"))
        self.assertEqual(row["refund_amount"], Decimal("150.00"))
        self.assertEqual(row["actual_refund_amount"], Decimal("150.00"))
        self.assertEqual(row["synced_at"], _NOW)
        self.assertEqual(result["records_read"], 1)
        self.assertEqual(result["records_new"], 1)
        self.assertEqual(result["_record_ids"], ["RK2609200130:1"])

    def test_check_time_falls_back_to_created_time(self):
        ds = dataset_by_name("wdt_refund_line_fact")
        order = _refund(details=[_refund_detail()])
        payload = json.loads(order["payload_json"])
        del payload["check_time"]
        payload["created_time"] = 1789865948000
        order["payload_json"] = json.dumps(payload)
        repo = self._repo(orders=[order])

        project_refund_lines(repo, ds, _RUN_ID, _NOW)

        rows = repo.replace_table.call_args.args[2]
        self.assertIsNotNone(rows[0]["check_time"])

    def test_lenient_decimal_and_bad_rows(self):
        ds = dataset_by_name("wdt_refund_line_fact")
        repo = self._repo(orders=[_refund(details=[
            {"spec_no": "SP-1", "num": "abc", "refund_amount": ""},
        ])])

        project_refund_lines(repo, ds, _RUN_ID, _NOW)

        rows = repo.replace_table.call_args.args[2]
        self.assertEqual(rows[0]["quantity"], Decimal("0"))
        self.assertEqual(rows[0]["refund_amount"], Decimal("0"))
        self.assertEqual(rows[0]["brand_name"], "未匹配")


class StockFlowReplaceTableTests(unittest.TestCase):
    _repo = helpers.MartExtractRepositoryTests._repo

    def test_replace_table_accepts_stock_flow_kinds(self):
        repo = self._repo()
        ds = dataset_by_name("wdt_stockout_line_fact")
        columns = list(_STOCKOUT_LINE_COLUMNS) + ["synced_at", "sync_run_id"]
        written = repo.replace_table(
            ds.target_table, columns, [{"order_no": "CK-1", "line_no": 1}],
        )
        self.assertEqual(written, 1)
        self.assertIn(
            "DELETE FROM `fact_stockout_line`",
            self.mart.cursor_instance.executed[0][0],
        )

        ds = dataset_by_name("wdt_refund_line_fact")
        columns = list(_REFUND_LINE_COLUMNS) + ["synced_at", "sync_run_id"]
        written = repo.replace_table(
            ds.target_table, columns, [{"order_no": "RK-1", "line_no": 1}],
        )
        self.assertEqual(written, 1)
        self.assertIn(
            "DELETE FROM `fact_refund_line`",
            self.mart.cursor_instance.executed[-1][0],
        )

    def test_replace_table_rejects_bad_columns_for_stock_flow(self):
        repo = self._repo()
        ds = dataset_by_name("wdt_stockout_line_fact")
        with self.assertRaises(MartExtractError):
            repo.replace_table(ds.target_table, ["order_no"], [])
        ds = dataset_by_name("wdt_refund_line_fact")
        with self.assertRaises(MartExtractError):
            repo.replace_table(
                ds.target_table,
                list(_REFUND_LINE_COLUMNS) + ["synced_at", "sync_run_id", "extra"],
                [],
            )
        self.assertEqual(self.mart.cursor_instance.executed, [])


class StockFlowServiceTests(unittest.TestCase):
    _service = helpers.MartExtractServiceTests._service

    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_stock_flow_datasets_run_after_order_line(self, _lock):
        service = self._service(datasets=[
            dataset_by_name("wdt_stockout_line_fact"),
            dataset_by_name("wdt_refund_line_fact"),
        ])
        self.repository.read_wdt_trades.side_effect = [
            [_stockout(details=[_stockout_detail()])],
            [_refund(details=[_refund_detail()])],
        ]
        self.repository.replace_table.side_effect = lambda _t, _c, rows: len(rows)

        result = service.extract()

        self.assertEqual(len(result.datasets), 2)
        summaries = self.mart_repository.save_dataset_summary.call_args_list
        self.assertEqual(summaries[0].kwargs["dataset_name"], "wdt_stockout_line_fact")
        self.assertEqual(summaries[1].kwargs["dataset_name"], "wdt_refund_line_fact")
        self.assertEqual(summaries[0].kwargs["raw_records_written"], 1)
        self.mart_repository.mark_completed.assert_called_once()

    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_stock_flow_write_failure_marks_run_failed(self, _lock):
        service = self._service(datasets=[dataset_by_name("wdt_stockout_line_fact")])
        self.repository.read_wdt_trades.return_value = [
            _stockout(details=[_stockout_detail()]),
        ]
        self.repository.replace_table.side_effect = RuntimeError("write failed")

        with self.assertRaises(RuntimeError):
            service.extract()

        self.mart_repository.mark_failed.assert_called_once()
        self.mart_repository.mark_completed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
