"""Tests for the WDT order-line / product-mirror projections (B1 阶段二)."""

import json
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from common.public_data.extract_mart import MartExtractError
from common.public_data.extract_order_line import (
    _DIM_PRODUCT_MIRROR_COLUMNS,
    _EXCLUDED_TRADE_STATUSES,
    _ORDER_LINE_COLUMNS,
    _TRADE_METHOD,
    _UNMATCHED_BRAND,
    project_dim_product_mirror,
    project_order_lines,
)
from common.public_data.mart_extract_schema import dataset_by_name
from tests.common import test_public_data_extract_mart as helpers

_NOW, _RUN_ID, _Ctx = helpers._NOW, helpers._RUN_ID, helpers._Ctx


def _trade(trade_no="JY20260901001", status="110", details=(), trade_time="2026-09-01 10:00:00", shop_name="习水村-抖音习酒旗舰店"):
    return {
        "source_record_id": trade_no,
        "payload_json": json.dumps({
            "trade_no": trade_no,
            "trade_status": status,
            "trade_time": trade_time,
            "shop_name": shop_name,
            "detail_list": list(details),
        }, ensure_ascii=False),
    }


def _detail(spec_no="SP-001", goods_name="某商品", num="2", paid="199.00"):
    return {"spec_no": spec_no, "goods_name": goods_name, "num": num, "paid": paid}


class DimProductMirrorProjectionTests(unittest.TestCase):

    def test_mirror_maps_columns_and_stamps_technicals(self):
        repo = Mock()
        ds = dataset_by_name("wdt_dim_product_mirror")
        raw = [{
            "spec_no": "SP-001",
            "barcode": "6901234567890",
            "goods_id": "1024",
            "goods_no": "G-001",
            "goods_name": "某白酒 500ml",
            "spec_name": "500ml*6",
            "brand_name": "某品牌",
            "series_name": "经典系列",
            "class_name": "白酒",
            "retail_price": Decimal("399.00"),
            "wholesale_price": Decimal("299.00"),
            "is_deleted": 0,
            "raw_json": '{"spec_no": "SP-001"}',
        }]
        repo.read_wdt_table.return_value = raw
        repo.replace_table.return_value = 1

        result = project_dim_product_mirror(repo, ds, _RUN_ID, _NOW)

        repo.read_wdt_table.assert_called_once_with("dim_product")
        table, columns, rows = repo.replace_table.call_args.args
        self.assertEqual(table, "dim_product")
        self.assertEqual(
            set(columns),
            set(_DIM_PRODUCT_MIRROR_COLUMNS) | {"synced_at", "sync_run_id"},
        )
        self.assertEqual(rows[0]["spec_no"], "SP-001")
        self.assertEqual(rows[0]["brand_name"], "某品牌")
        self.assertEqual(rows[0]["raw_json"], '{"spec_no": "SP-001"}')
        self.assertEqual(rows[0]["synced_at"], _NOW)
        self.assertEqual(rows[0]["sync_run_id"], _RUN_ID)
        self.assertNotIn("synced_at", raw[0])
        self.assertEqual(result["records_read"], 1)
        self.assertEqual(result["records_new"], 1)
        self.assertEqual(result["_record_ids"], ["SP-001"])

    def test_mirror_skips_rows_without_spec_no(self):
        repo = Mock()
        ds = dataset_by_name("wdt_dim_product_mirror")
        repo.read_wdt_table.return_value = [
            {"spec_no": "", "goods_name": "无主键"},
            {"spec_no": None},
            {"spec_no": "SP-002", "raw_json": None},
        ]
        repo.replace_table.return_value = 1

        result = project_dim_product_mirror(repo, ds, _RUN_ID, _NOW)

        rows = repo.replace_table.call_args.args[2]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["spec_no"], "SP-002")
        self.assertEqual(result["_record_ids"], ["SP-002"])


class OrderLineProjectionTests(unittest.TestCase):

    def _repo(self, trades=(), brands=()):
        repo = Mock()
        repo.read_wdt_trades.return_value = list(trades)
        repo.read_mart_table.return_value = [
            {"spec_no": spec, "brand_name": brand} for spec, brand in brands
        ]
        repo.replace_table.side_effect = lambda _t, _c, rows: len(rows)
        return repo

    def test_channel_name_is_normalized_from_the_shop_name(self):
        ds = dataset_by_name("wdt_order_line_fact")
        cases = (
            ("天猫超市-习水村酒类专营店", "猫超"),
            ("习水村-拼多多习酒旗舰店", "拼多多"),
            ("杭州习水村酒业有限公司", "其他"),  # 主体店无平台词
        )
        for shop_name, channel in cases:
            with self.subTest(shop_name=shop_name):
                repo = self._repo(
                    trades=[_trade(details=[_detail()], shop_name=shop_name)],
                    brands=(("SP-001", "某品牌"),),
                )

                project_order_lines(repo, ds, _RUN_ID, _NOW)

                _, _, rows = repo.replace_table.call_args.args
                self.assertEqual(shop_name, rows[0]["shop_name"])
                self.assertEqual(channel, rows[0]["channel_name"])

    def test_expands_detail_list_and_looks_up_brand(self):
        ds = dataset_by_name("wdt_order_line_fact")
        repo = self._repo(
            trades=[_trade(details=[
                _detail(spec_no="SP-001", paid="199.00"),
                _detail(spec_no="SP-404", goods_name="未知商品", num="1", paid="9.9"),
            ])],
            brands=(("SP-001", "某品牌"),),
        )

        result = project_order_lines(repo, ds, _RUN_ID, _NOW)

        repo.read_wdt_trades.assert_called_once_with(_TRADE_METHOD)
        repo.read_mart_table.assert_called_once_with(
            "dim_product", columns=("spec_no", "brand_name")
        )
        table, columns, rows = repo.replace_table.call_args.args
        self.assertEqual(table, "fact_order_line")
        self.assertEqual(
            set(columns),
            set(_ORDER_LINE_COLUMNS) | {"synced_at", "sync_run_id"},
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["trade_no"], "JY20260901001")
        self.assertEqual(rows[0]["line_no"], 1)
        self.assertEqual(rows[0]["brand_name"], "某品牌")
        self.assertEqual(rows[0]["shop_name"], "习水村-抖音习酒旗舰店")
        self.assertEqual(rows[0]["channel_name"], "抖音")
        self.assertEqual(rows[0]["paid_amount"], Decimal("199.00"))
        self.assertEqual(rows[0]["quantity"], Decimal("2"))
        self.assertEqual(rows[0]["platform_subsidy"], Decimal("0"))
        self.assertEqual(rows[0]["shop_subsidy"], Decimal("0"))
        self.assertEqual(rows[1]["line_no"], 2)
        self.assertEqual(rows[1]["brand_name"], _UNMATCHED_BRAND)
        self.assertEqual(rows[0]["synced_at"], _NOW)
        self.assertEqual(result["records_read"], 1)
        self.assertEqual(result["records_new"], 2)
        self.assertEqual(
            result["_record_ids"], ["JY20260901001:1", "JY20260901001:2"],
        )

    def test_excluded_statuses_are_dropped(self):
        ds = dataset_by_name("wdt_order_line_fact")
        trades = [
            _trade(trade_no=f"T-{status}", status=status, details=[_detail()])
            for status in sorted(_EXCLUDED_TRADE_STATUSES)
        ]
        trades.append(_trade(trade_no="T-keep", status="95", details=[_detail()]))
        repo = self._repo(trades=trades)

        result = project_order_lines(repo, ds, _RUN_ID, _NOW)

        rows = repo.replace_table.call_args.args[2]
        self.assertEqual([row["trade_no"] for row in rows], ["T-keep"])
        self.assertEqual(result["records_new"], 1)

    def test_payload_without_trade_no_or_spec_no_is_skipped(self):
        ds = dataset_by_name("wdt_order_line_fact")
        repo = self._repo(trades=[
            {"source_record_id": "x1", "payload_json": json.dumps({
                "trade_status": "110", "detail_list": [_detail()],
            })},
            _trade(trade_no="T-2", details=[
                {"goods_name": "缺规格", "num": "1", "paid": "1"},
                "not-a-dict",
                _detail(spec_no="SP-9"),
            ]),
            {"source_record_id": "x3", "payload_json": "not json {"},
            {"source_record_id": "x4", "payload_json": None},
        ])

        result = project_order_lines(repo, ds, _RUN_ID, _NOW)

        rows = repo.replace_table.call_args.args[2]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_no"], "T-2")
        self.assertEqual(rows[0]["line_no"], 1)
        self.assertEqual(rows[0]["spec_no"], "SP-9")
        self.assertEqual(result["records_new"], 1)

    def test_decimal_and_datetime_parsing_are_lenient(self):
        ds = dataset_by_name("wdt_order_line_fact")
        repo = self._repo(trades=[_trade(
            trade_time="not a time",
            details=[_detail(num="abc", paid="")],
        )])

        project_order_lines(repo, ds, _RUN_ID, _NOW)

        rows = repo.replace_table.call_args.args[2]
        self.assertIsNone(rows[0]["trade_time"])
        self.assertEqual(rows[0]["quantity"], Decimal("0"))
        self.assertEqual(rows[0]["paid_amount"], Decimal("0"))

    def test_epoch_millis_trade_time_is_converted_to_cn_naive(self):
        from common.public_data.extract_order_line import _CN_TZ

        ds = dataset_by_name("wdt_order_line_fact")
        repo = self._repo(trades=[_trade(
            trade_time=1788147469000,
            details=[_detail()],
        )])

        project_order_lines(repo, ds, _RUN_ID, _NOW)

        rows = repo.replace_table.call_args.args[2]
        from datetime import datetime as _dt
        expected = _dt.fromtimestamp(1788147469, tz=_CN_TZ).replace(tzinfo=None)
        self.assertEqual(rows[0]["trade_time"], expected)
        self.assertEqual((expected.year, expected.month), (2026, 8))


class WdtRepositoryTests(unittest.TestCase):
    _repo = helpers.MartExtractRepositoryTests._repo

    def test_read_wdt_table_queries_the_wdt_connection(self):
        repo = self._repo()
        wdt = helpers._FakeConnection(rows=[{"spec_no": "SP-1"}])
        repo._wdt = wdt

        rows = repo.read_wdt_table("dim_product")

        sql, _ = wdt.cursor_instance.executed[0]
        self.assertIn("FROM `dim_product`", sql)
        self.assertEqual(rows, [{"spec_no": "SP-1"}])

    def test_read_wdt_table_rejects_unregistered_tables(self):
        repo = self._repo()
        repo._wdt = helpers._FakeConnection()
        with self.assertRaises(MartExtractError):
            repo.read_wdt_table("wdt_records")
        with self.assertRaises(MartExtractError):
            repo.read_wdt_table("unknown")
        self.assertEqual(repo._wdt.cursor_instance.executed, [])

    def test_read_wdt_trades_filters_by_method(self):
        repo = self._repo()
        wdt = helpers._FakeConnection(rows=[{"source_record_id": "T-1"}])
        repo._wdt = wdt

        rows = repo.read_wdt_trades(_TRADE_METHOD)

        sql, params = wdt.cursor_instance.executed[0]
        self.assertIn("FROM `wdt_records`", sql)
        self.assertEqual(params, (_TRADE_METHOD,))
        self.assertEqual(rows, [{"source_record_id": "T-1"}])
        with self.assertRaises(MartExtractError):
            repo.read_wdt_trades("")

    def test_read_mart_table_is_whitelisted_to_mirrors(self):
        repo = self._repo()
        repo.read_mart_table("dim_product")
        sql, _ = self.mart.cursor_instance.executed[0]
        self.assertIn("FROM `dim_product`", sql)
        with self.assertRaises(MartExtractError):
            repo.read_mart_table("fact_order_line")

    def test_replace_table_accepts_order_line_kinds(self):
        repo = self._repo()
        ds = dataset_by_name("wdt_order_line_fact")
        columns = list(_ORDER_LINE_COLUMNS) + ["synced_at", "sync_run_id"]
        written = repo.replace_table(
            ds.target_table, columns,
            [{"trade_no": "T-1", "line_no": 1}],
        )
        self.assertEqual(written, 1)
        self.assertIn(
            "DELETE FROM `fact_order_line`",
            self.mart.cursor_instance.executed[0][0],
        )

        ds = dataset_by_name("wdt_dim_product_mirror")
        columns = list(_DIM_PRODUCT_MIRROR_COLUMNS) + ["synced_at", "sync_run_id"]
        written = repo.replace_table(
            ds.target_table, columns, [{"spec_no": "SP-1"}],
        )
        self.assertEqual(written, 1)

    def test_replace_table_rejects_bad_columns_for_new_kinds(self):
        repo = self._repo()
        ds = dataset_by_name("wdt_order_line_fact")
        with self.assertRaises(MartExtractError):
            repo.replace_table(ds.target_table, ["trade_no"], [])
        with self.assertRaises(MartExtractError):
            repo.replace_table(
                ds.target_table,
                list(_ORDER_LINE_COLUMNS) + ["synced_at", "sync_run_id", "extra"],
                [],
            )
        self.assertEqual(self.mart.cursor_instance.executed, [])


class OrderLineServiceTests(unittest.TestCase):
    _service = helpers.MartExtractServiceTests._service

    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_wdt_datasets_run_in_registration_order(self, _lock):
        service = self._service(datasets=[
            dataset_by_name("wdt_dim_product_mirror"),
            dataset_by_name("wdt_order_line_fact"),
        ])
        self.repository.read_wdt_table.return_value = [
            {"spec_no": "SP-1", "brand_name": "某品牌", "raw_json": "{}"},
        ]
        self.repository.read_mart_table.return_value = [
            {"spec_no": "SP-1", "brand_name": "某品牌"},
        ]
        self.repository.read_wdt_trades.return_value = [
            _trade(details=[_detail(spec_no="SP-1")]),
        ]
        self.repository.replace_table.side_effect = lambda _t, _c, rows: len(rows)

        result = service.extract()

        self.assertEqual(len(result.datasets), 2)
        summaries = self.mart_repository.save_dataset_summary.call_args_list
        self.assertEqual(summaries[0].kwargs["dataset_name"], "wdt_dim_product_mirror")
        self.assertEqual(summaries[1].kwargs["dataset_name"], "wdt_order_line_fact")
        self.assertEqual(summaries[1].kwargs["raw_records_written"], 1)
        self.mart_repository.mark_completed.assert_called_once()
        self.repository.upsert_fact.assert_not_called()

    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_order_line_write_failure_marks_run_failed(self, _lock):
        service = self._service(datasets=[dataset_by_name("wdt_order_line_fact")])
        self.repository.read_wdt_trades.return_value = [
            _trade(details=[_detail()]),
        ]
        self.repository.read_mart_table.return_value = []
        self.repository.replace_table.side_effect = RuntimeError("write failed")

        with self.assertRaises(RuntimeError):
            service.extract()

        self.mart_repository.mark_failed.assert_called_once()
        self.mart_repository.mark_completed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
