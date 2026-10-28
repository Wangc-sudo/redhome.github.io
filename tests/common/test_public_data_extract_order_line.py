"""Tests for the WDT order-line / product-mirror projections (B1 阶段二)."""

import hashlib
import json
import unittest
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import Mock, patch

from common.public_data.extract_mart import MartExtractError
from common.public_data.extract_order_line import (
    _DIM_PRODUCT_MIRROR_COLUMNS,
    _EXCLUDED_TRADE_STATUSES,
    _ORDER_CANCEL_COLUMNS,
    _ORDER_LINE_COLUMNS,
    _TRADE_METHOD,
    _UNMATCHED_BRAND,
    project_dim_product_mirror,
    project_order_cancels,
    project_order_lines,
)
from common.public_data.mart_extract_schema import dataset_by_name
from tests.common import test_public_data_extract_mart as helpers

_NOW, _RUN_ID, _Ctx = helpers._NOW, helpers._RUN_ID, helpers._Ctx


def _trade(trade_no="JY20260901001", status="110", details=(),
           trade_time="2026-09-01 10:00:00",
           shop_name="习水村-抖音习酒旗舰店", created=None, customer_id=None):
    payload = {
        "trade_no": trade_no,
        "trade_status": status,
        "trade_time": trade_time,
        "shop_name": shop_name,
        "detail_list": list(details),
    }
    if created is not None:
        payload["created"] = created
    if customer_id is not None:
        payload["customer_id"] = customer_id
    return {
        "source_record_id": trade_no,
        "payload_json": json.dumps(payload, ensure_ascii=False),
    }


def _detail(spec_no="SP-001", goods_name="某商品", num="2", paid="199.00"):
    return {"spec_no": spec_no, "goods_name": goods_name, "num": num, "paid": paid}


def _spec_required_rule():
    return {
        "dataset": "wdt_order_line_fact", "rule_code": "WDT_SPEC_NO_REQUIRED",
        "rule_type": "required", "field_name": "spec_no", "params": {},
        "severity": "error", "enabled": 1, "version": 1,
    }


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

    def _repo(self, trades=(), brands=(), batches=None):
        repo = Mock()
        if batches is None:
            batches = [list(trades)]
        repo.read_wdt_trade_batches.return_value = iter(batches)
        repo.read_mart_table.return_value = [
            {"spec_no": spec, "brand_name": brand} for spec, brand in brands
        ]
        return helpers._capture_replace_batches(repo)

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

                rows = repo.expanded_rows
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

        repo.read_wdt_trade_batches.assert_called_once_with(_TRADE_METHOD)
        repo.read_mart_table.assert_called_once_with(
            "dim_product", columns=("spec_no", "brand_name")
        )
        table, columns, _ = repo.replace_table_batches.call_args.args
        rows = repo.expanded_rows
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
        self.assertIsNone(rows[0]["buyer_id"])  # 夹具未带 customer_id
        self.assertEqual(rows[0]["synced_at"], _NOW)
        self.assertEqual(result["records_read"], 1)
        self.assertEqual(result["records_new"], 2)
        self.assertEqual(result["records_skipped"], 0)
        self.assertEqual(result["skip_reasons"], {})
        self.assertRegex(result["_record_id_digest"], r"^[0-9a-f]{64}$")
        self.assertNotIn("_record_ids", result)

    def test_buyer_id_is_taken_from_the_trade_level_customer_id(self):
        # 买家标识 = WDT 交易级 customer_id（2026-10-12 实测确认）；
        # 拼多多按隐私协议不返回 → None（查询层显式计数）。
        ds = dataset_by_name("wdt_order_line_fact")
        repo = self._repo(batches=[
            [_trade(details=[_detail()], customer_id="C-1001")],
            [_trade(trade_no="JY-2", details=[_detail()])],
        ])

        result = project_order_lines(repo, ds, _RUN_ID, _NOW)

        rows = repo.expanded_rows
        self.assertEqual([len(batch) for batch in repo.expanded_batches], [1, 1])
        self.assertEqual(rows[0]["buyer_id"], "C-1001")
        self.assertIsNone(rows[1]["buyer_id"])
        self.assertEqual(result["records_read"], 2)
        self.assertEqual(result["records_new"], 2)

    def test_excluded_statuses_are_dropped(self):
        ds = dataset_by_name("wdt_order_line_fact")
        trades = [
            _trade(trade_no=f"T-{status}", status=status, details=[_detail()])
            for status in sorted(_EXCLUDED_TRADE_STATUSES)
        ]
        trades.append(_trade(trade_no="T-keep", status="95", details=[_detail()]))
        repo = self._repo(trades=trades)

        result = project_order_lines(repo, ds, _RUN_ID, _NOW)

        rows = repo.expanded_rows
        self.assertEqual([row["trade_no"] for row in rows], ["T-keep"])
        self.assertEqual(result["records_read"], len(trades))
        self.assertEqual(result["records_new"], 1)
        self.assertEqual(result["records_skipped"], 0)
        self.assertEqual(result["skip_reasons"], {})

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

        rows = repo.expanded_rows
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_no"], "T-2")
        self.assertEqual(rows[0]["line_no"], 1)
        self.assertEqual(rows[0]["spec_no"], "SP-9")
        self.assertEqual(result["records_read"], 4)
        self.assertEqual(result["records_new"], 1)
        self.assertEqual(result["records_skipped"], 5)
        self.assertEqual(result["skip_reasons"], {
            "missing_trade_no": 1,
            "missing_spec_no": 1,
            "invalid_detail": 1,
            "invalid_payload": 2,
        })
        self.assertRegex(result["_record_id_digest"], r"^[0-9a-f]{64}$")

    def test_decimal_and_datetime_parsing_are_lenient(self):
        ds = dataset_by_name("wdt_order_line_fact")
        repo = self._repo(trades=[_trade(
            trade_time="not a time",
            details=[_detail(num="abc", paid="")],
        )])

        project_order_lines(repo, ds, _RUN_ID, _NOW)

        rows = repo.expanded_rows
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

        rows = repo.expanded_rows
        from datetime import datetime as _dt
        expected = _dt.fromtimestamp(1788147469, tz=_CN_TZ).replace(tzinfo=None)
        self.assertEqual(rows[0]["trade_time"], expected)
        self.assertEqual((expected.year, expected.month), (2026, 8))

    def test_full_projection_keeps_legacy_unbounded_behavior(self):
        ds = dataset_by_name("wdt_order_line_fact")
        repo = self._repo(batches=[
            [_trade(trade_no="T-old", trade_time="2020-01-01 00:00:00",
                    details=[_detail()])],
            [_trade(trade_no="T-null-time", trade_time="not a time",
                    details=[_detail(spec_no="SP-2")])],
        ])

        result = project_order_lines(repo, ds, _RUN_ID, _NOW)

        self.assertEqual(result["records_read"], 2)
        self.assertEqual(result["records_new"], 2)
        self.assertEqual(
            [row["trade_no"] for row in repo.expanded_rows],
            ["T-old", "T-null-time"],
        )
        self.assertIsNone(repo.expanded_rows[1]["trade_time"])
        repo.replace_table_batches.assert_called_once()
        repo.replace_table_window_batches.assert_not_called()
        repo.window_row_counts.assert_not_called()

    def test_window_dry_run_requires_both_boundaries_before_reading(self):
        ds = dataset_by_name("wdt_order_line_fact")
        repo = Mock()
        windows = (
            (None, None),
            (date(2026, 10, 26), None),
            (None, date(2026, 10, 26)),
        )
        for window_start, window_end in windows:
            with self.subTest(window_start=window_start, window_end=window_end):
                with self.assertRaises(ValueError):
                    project_order_lines(
                        repo, ds, None, None, window_start=window_start,
                        window_end=window_end, dry_run=True, rules=[], overrides={},
                    )
        repo.read_mart_table.assert_not_called()
        repo.read_wdt_trade_batches.assert_not_called()

    def test_window_dry_run_reuses_cached_raw_batches(self):
        ds = dataset_by_name("wdt_order_line_fact")
        cached = _trade(
            trade_no="T-cached", trade_time="2026-10-26 10:00:00",
            details=[_detail()],
        )
        database_repo = self._repo(batches=[[cached]])
        cached_repo = self._repo(batches=[])
        for repo in (database_repo, cached_repo):
            repo.window_row_counts.return_value = {
                "before_count": 5, "window_before": 2,
            }
        kwargs = {
            "window_start": date(2026, 10, 26),
            "window_end": date(2026, 10, 26),
            "dry_run": True, "rules": [_spec_required_rule()], "overrides": {},
        }

        database_result = project_order_lines(
            database_repo, ds, None, None, **kwargs,
        )
        cached_result = project_order_lines(
            cached_repo, ds, None, None, raw_batches=[[cached]], **kwargs,
        )

        cached_repo.read_wdt_trade_batches.assert_not_called()
        self.assertEqual(database_result, cached_result)
        self.assertEqual(1, cached_result["records_read"])
        self.assertEqual(0, cached_result["records_new"])
        self.assertEqual(1, cached_result["_replay_diff"]["window_after"])
        self.assertEqual(0, cached_result["_replay_diff"]["records_written"])

    def test_cached_raw_batches_cannot_be_used_for_formal_replay(self):
        repo = Mock()

        with self.assertRaisesRegex(ValueError, "dry-run"):
            project_order_lines(
                repo, dataset_by_name("wdt_order_line_fact"), _RUN_ID, _NOW,
                window_start=date(2026, 10, 26), window_end=date(2026, 10, 26),
                dry_run=False, rules=[], overrides={}, raw_batches=[],
            )

        repo.read_mart_table.assert_not_called()
        repo.read_wdt_trade_batches.assert_not_called()
        repo.replace_table_window_batches.assert_not_called()

    def test_window_replay_isolates_batches_and_applies_spec_overrides(self):
        ds = dataset_by_name("wdt_order_line_fact")
        repo = self._repo(
            batches=[
                [
                    _trade(
                        trade_no="T-1", trade_time="2026-10-26 23:59:59",
                        details=[
                            _detail(spec_no="", num="1.25", paid="10.10"),
                            _detail(spec_no="SP-2", num="2", paid="20.00"),
                        ],
                    ),
                    _trade(
                        trade_no="T-before", trade_time="2026-10-25 23:59:59",
                        details=[_detail(spec_no="SP-OUT")],
                    ),
                ],
                [
                    _trade(
                        trade_no="T-2", trade_time="2026-10-26 00:00:00",
                        details=[_detail(spec_no="SP-3", paid="0.01")],
                    ),
                    _trade(
                        trade_no="T-after", trade_time="2026-10-27 00:00:00",
                        details=[_detail(spec_no="SP-OUT")],
                    ),
                ],
            ],
            brands=(
                ("SP-FIX", "修复品牌"), ("SP-2", "品牌二"), ("SP-3", "品牌三"),
            ),
        )
        published_batches = []

        def replace_window(_table, _columns, batches, **_kwargs):
            for batch in batches:
                rows = list(batch)
                published_batches.append(rows)
            written = sum(len(rows) for rows in published_batches)
            return {
                "before_count": 7, "window_before": 2,
                "window_after": written, "after_count": 5 + written,
                "records_written": written,
            }

        repo.replace_table_window_batches.side_effect = replace_window
        result = project_order_lines(
            repo, ds, _RUN_ID, _NOW,
            window_start=date(2026, 10, 26), window_end=date(2026, 10, 26),
            rules=[_spec_required_rule()],
            overrides={("T-1#1", "spec_no"): "SP-FIX"},
        )

        self.assertEqual([len(rows) for rows in published_batches], [2, 1])
        rows = [row for batch in published_batches for row in batch]
        self.assertEqual([row["trade_no"] for row in rows], ["T-1", "T-1", "T-2"])
        self.assertEqual(
            [row["spec_no"] for row in rows], ["SP-FIX", "SP-2", "SP-3"],
        )
        self.assertEqual(rows[0]["brand_name"], "修复品牌")
        self.assertEqual(rows[0]["quantity"], Decimal("1.25"))
        self.assertEqual(rows[0]["paid_amount"], Decimal("10.10"))
        self.assertEqual(rows[2]["paid_amount"], Decimal("0.01"))
        self.assertTrue(all(row["sync_run_id"] == _RUN_ID for row in rows))
        self.assertFalse(any("source_record_id" in row for row in rows))
        self.assertEqual(result["records_read"], 2)
        self.assertEqual(result["records_new"], 3)
        self.assertEqual(result["_replay_diff"]["records_written"], 3)
        repo.replace_table_batches.assert_not_called()
        repo.window_row_counts.assert_not_called()

    def test_quality_gate_failure_drops_staging_without_rename(self):
        ds = dataset_by_name("wdt_order_line_fact")
        mart = helpers._FakeConnection()
        repo = helpers.MartExtractRepository(Mock(), mart)
        repo.read_mart_table = Mock(return_value=[])
        repo.read_wdt_trade_batches = Mock(return_value=iter([[
            _trade(
                trade_no="T-bad", trade_time="2026-10-26 10:00:00",
                details=[_detail(spec_no="")],
            ),
        ]]))
        repo.window_row_counts = Mock(return_value={
            "before_count": 3, "window_before": 1,
        })

        with self.assertRaisesRegex(MartExtractError, "quality_gate_blocked"):
            project_order_lines(
                repo, ds, _RUN_ID, _NOW,
                window_start=date(2026, 10, 26), window_end=date(2026, 10, 26),
                rules=[_spec_required_rule()], overrides={},
            )

        executed = [sql for sql, _ in mart.cursor_instance.executed]
        self.assertFalse(any(sql.startswith("RENAME TABLE") for sql in executed))
        self.assertTrue(any(
            sql.startswith("DROP TABLE IF EXISTS `__extract_s_")
            for sql in executed
        ))
        self.assertEqual(mart.cursor_instance.executemany_calls, [])


class OrderCancelProjectionTests(unittest.TestCase):
    """BI 阶段 2：已取消单（status=4）按单保留，供月度取消率。"""

    def _repo(self, trades=(), batches=None):
        repo = Mock()
        if batches is None:
            batches = [list(trades)]
        repo.read_wdt_trade_batches.return_value = iter(batches)
        return helpers._capture_replace_batches(repo)

    def test_keeps_only_cancelled_trades_one_row_per_trade(self):
        ds = dataset_by_name("wdt_order_cancel_fact")
        repo = self._repo(trades=[
            _trade(trade_no="T-4", status="4", created="2026-09-03 09:00:00",
                   shop_name="习水村-拼多多习酒旗舰店"),
            _trade(trade_no="T-keep", status="110", details=[_detail()]),
            _trade(trade_no="T-5", status="5", details=[_detail()]),
            _trade(trade_no="T-24", status="24", details=[_detail()]),
        ])

        result = project_order_cancels(repo, ds, _RUN_ID, _NOW)

        repo.read_wdt_trade_batches.assert_called_once_with(_TRADE_METHOD)
        table, columns, _ = repo.replace_table_batches.call_args.args
        rows = repo.expanded_rows
        self.assertEqual(table, "fact_order_cancel_line")
        self.assertEqual(
            set(columns),
            set(_ORDER_CANCEL_COLUMNS) | {"synced_at", "sync_run_id"},
        )
        # 每单一行：status=4 保留且不展明细，正常单/5/24 剔除。
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_no"], "T-4")
        self.assertEqual(rows[0]["channel_name"], "拼多多")
        self.assertEqual(rows[0]["shop_name"], "习水村-拼多多习酒旗舰店")
        self.assertEqual(rows[0]["cancel_time"], datetime(2026, 9, 3, 9, 0, 0))
        self.assertEqual(rows[0]["synced_at"], _NOW)
        self.assertEqual(result["records_read"], 4)
        self.assertEqual(result["records_new"], 1)
        self.assertEqual(result["records_skipped"], 0)
        self.assertEqual(result["skip_reasons"], {})
        self.assertRegex(result["_record_id_digest"], r"^[0-9a-f]{64}$")
        self.assertNotIn("_record_ids", result)

    def test_cancel_time_falls_back_to_modified_when_created_missing(self):
        ds = dataset_by_name("wdt_order_cancel_fact")
        trade = _trade(trade_no="T-4", status="4")
        payload = json.loads(trade["payload_json"])
        payload["modified"] = "2026-09-03 10:00:00"
        trade["payload_json"] = json.dumps(payload, ensure_ascii=False)
        repo = self._repo(trades=[trade])

        project_order_cancels(repo, ds, _RUN_ID, _NOW)

        rows = repo.expanded_rows
        self.assertEqual(rows[0]["cancel_time"], datetime(2026, 9, 3, 10, 0, 0))

    def test_bad_payloads_and_missing_trade_no_are_skipped(self):
        ds = dataset_by_name("wdt_order_cancel_fact")
        repo = self._repo(trades=[
            {"source_record_id": "x1", "payload_json": "not json {"},
            {"source_record_id": "x2", "payload_json": None},
            {"source_record_id": "x3", "payload_json": json.dumps(
                {"trade_status": "4"}
            )},  # status=4 但缺 trade_no
        ])

        result = project_order_cancels(repo, ds, _RUN_ID, _NOW)

        self.assertEqual(repo.expanded_rows, [])
        self.assertEqual(result["records_read"], 3)
        self.assertEqual(result["records_new"], 0)
        self.assertEqual(result["records_skipped"], 3)
        self.assertEqual(result["skip_reasons"], {
            "invalid_payload": 2,
            "missing_trade_no": 1,
        })
        self.assertEqual(
            result["_record_id_digest"], hashlib.sha256(b"").hexdigest(),
        )


class _KeysetCursor(helpers._RecordingCursor):
    def __init__(self, responses):
        super().__init__()
        self.responses = [list(response) for response in responses]
        self.remaining = []

    def execute(self, sql, params=None):
        super().execute(sql, params)
        self.remaining = self.responses.pop(0) if self.responses else []

    def fetchone(self):
        return self.remaining.pop(0) if self.remaining else None


class WdtRepositoryTests(unittest.TestCase):
    _repo = helpers.MartExtractRepositoryTests._repo

    def _wdt_repo(self, responses):
        repo = self._repo()
        wdt = helpers._FakeConnection()
        wdt.cursor_instance = _KeysetCursor(responses)
        repo._wdt = wdt
        return repo, wdt

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

    def test_read_wdt_trade_batches_uses_ss_cursor_and_keyset(self):
        from pymysql.cursors import SSDictCursor

        first = {"source_record_id": "T-1", "payload_json": "{}"}
        second = {"source_record_id": "T-2", "payload_json": "{}"}
        third = {"source_record_id": "T-3", "payload_json": "{}"}
        repo, wdt = self._wdt_repo([[first, second], [third]])

        batches = list(repo.read_wdt_trade_batches(
            _TRADE_METHOD, batch_size=2,
        ))

        self.assertEqual(batches, [[first, second], [third]])
        self.assertEqual(wdt.cursor_classes, [SSDictCursor, SSDictCursor])
        executed = wdt.cursor_instance.executed
        self.assertEqual(len(executed), 2)
        for sql, _ in executed:
            self.assertIn("FROM `wdt_records`", sql)
            self.assertIn("`source_record_id` > %s", sql)
            self.assertIn("ORDER BY `source_record_id` LIMIT %s", sql)
        self.assertEqual([params for _, params in executed], [
            (_TRADE_METHOD, "", 2),
            (_TRADE_METHOD, "T-2", 2),
        ])
        with self.assertRaises(MartExtractError):
            list(repo.read_wdt_trade_batches(""))

    def test_read_wdt_trade_batches_byte_limit_does_not_lose_pending_rows(self):
        rows = [
            {"source_record_id": str(number), "payload_json": "aaaa"}
            for number in (1, 2, 3)
        ]
        repo, wdt = self._wdt_repo([[rows[0], rows[1]], [rows[2]], []])

        batches = list(repo.read_wdt_trade_batches(
            _TRADE_METHOD, batch_size=10, max_batch_bytes=6,
        ))

        self.assertEqual(batches, [[row] for row in rows])
        self.assertEqual([
            params for _, params in wdt.cursor_instance.executed
        ], [
            (_TRADE_METHOD, "", 10),
            (_TRADE_METHOD, "2", 10),
            (_TRADE_METHOD, "3", 10),
        ])

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

        ds = dataset_by_name("wdt_order_cancel_fact")
        columns = list(_ORDER_CANCEL_COLUMNS) + ["synced_at", "sync_run_id"]
        written = repo.replace_table(
            ds.target_table, columns, [{"trade_no": "T-4"}],
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
        self.repository.read_wdt_trade_batches.return_value = iter([[
            _trade(details=[_detail(spec_no="SP-1")]),
        ]])
        self.repository.replace_table.side_effect = lambda _t, _c, rows: len(rows)
        helpers._capture_replace_batches(self.repository)

        result = service.extract()

        self.assertEqual(len(result.datasets), 2)
        self.repository.read_wdt_trade_batches.assert_called_once_with(_TRADE_METHOD)
        self.repository.replace_table_batches.assert_called_once()
        self.assertEqual(len(self.repository.expanded_rows), 1)
        summaries = self.mart_repository.save_dataset_summary.call_args_list
        self.assertEqual(summaries[0].kwargs["dataset_name"], "wdt_dim_product_mirror")
        self.assertEqual(summaries[1].kwargs["dataset_name"], "wdt_order_line_fact")
        self.assertEqual(summaries[1].kwargs["raw_records_written"], 1)
        self.assertRegex(
            summaries[1].kwargs["record_id_digest"], r"^[0-9a-f]{64}$",
        )
        self.mart_repository.mark_completed.assert_called_once()
        self.repository.upsert_fact.assert_not_called()

    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_order_line_write_failure_marks_run_failed(self, _lock):
        service = self._service(datasets=[dataset_by_name("wdt_order_line_fact")])
        self.repository.read_wdt_trade_batches.return_value = iter([[
            _trade(details=[_detail()]),
        ]])
        self.repository.read_mart_table.return_value = []
        helpers._capture_replace_batches(
            self.repository, error=RuntimeError("write failed"),
        )

        with self.assertRaisesRegex(RuntimeError, "write failed"):
            service.extract()

        self.mart_repository.mark_failed.assert_called_once()
        self.mart_repository.mark_completed.assert_not_called()

    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_wdt_window_dry_run_has_no_sync_run_or_writes(self, _lock):
        service = self._service(datasets=[dataset_by_name("wdt_order_line_fact")])
        service._new_run_id = Mock(return_value=_RUN_ID)
        self.repository.read_mart_table.return_value = []
        self.repository.read_wdt_trade_batches.return_value = iter([[
            _trade(trade_time="2026-10-26 10:00:00", details=[_detail()]),
        ]])
        self.repository.window_row_counts.return_value = {
            "before_count": 4, "window_before": 1,
        }

        result = service.replay_dataset(
            "wdt_order_line_fact", window_start=date(2026, 10, 26),
            window_end=date(2026, 10, 26), dry_run=True, rules=[], overrides={},
        )

        self.assertIsNone(result["sync_run_id"])
        self.assertEqual(result["records_read"], 1)
        self.assertEqual(result["records_written"], 0)
        self.assertEqual(result["diff"]["window_after"], 1)
        self.assertEqual(result["diff"]["after_count"], 4)
        service._new_run_id.assert_not_called()
        self.mart_repository.start_run.assert_not_called()
        self.mart_repository.save_dataset_summary.assert_not_called()
        self.mart_repository.mark_raw_committed.assert_not_called()
        self.mart_repository.mark_completed.assert_not_called()
        self.repository.replace_table_batches.assert_not_called()
        self.repository.replace_table_window_batches.assert_not_called()
        executed = [sql for sql, _ in self.mart_connection.cursor_instance.executed]
        self.assertFalse(any(sql.startswith("RENAME TABLE") for sql in executed))

    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_wdt_formal_replay_returns_published_count_and_run_id(self, _lock):
        service = self._service(datasets=[dataset_by_name("wdt_order_line_fact")])
        self.repository.read_mart_table.return_value = []
        self.repository.read_wdt_trade_batches.return_value = iter([[
            _trade(trade_time="2026-10-26 10:00:00", details=[_detail()]),
        ]])
        published = []

        def replace_window(_table, _columns, batches, **_kwargs):
            published.extend(row for batch in batches for row in batch)
            return {
                "before_count": 4, "window_before": 1,
                "window_after": len(published), "after_count": 3 + len(published),
                "records_written": len(published),
            }

        self.repository.replace_table_window_batches.side_effect = replace_window
        result = service.replay_dataset(
            "wdt_order_line_fact", window_start=date(2026, 10, 26),
            window_end=date(2026, 10, 26), dry_run=False, rules=[], overrides={},
        )

        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["sync_run_id"], _RUN_ID)
        self.assertEqual(result["sync_run_id"], _RUN_ID)
        self.assertEqual(result["records_written"], 1)
        self.mart_repository.start_run.assert_called_once()
        self.mart_repository.mark_raw_committed.assert_called_once_with(_RUN_ID)
        self.assertEqual(
            self.mart_repository.save_dataset_summary.call_args.kwargs[
                "raw_records_written"
            ],
            1,
        )
        self.mart_repository.mark_completed.assert_called_once()
        self.mart_repository.mark_failed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
