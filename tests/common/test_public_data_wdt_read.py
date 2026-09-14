import unittest
from datetime import datetime, timezone

from common.public_data.manifest import WdtDataset
from common.public_data.wdt_read import PaginationLimitExceeded, WdtReadError, WdtReadGateway


class WdtReadGatewayTests(unittest.TestCase):
    def _dataset(self, **changes):
        values = {
            "dataset": "trade-window",
            "method": "sales.TradeQuery.queryWithDetail",
            "target_table": "wdt_records",
            "record_id_path": "trade_no",
            "page_size": 2,
            "max_pages": 2,
            "window_start": datetime(2026, 9, 1, tzinfo=timezone.utc),
            "window_end": datetime(2026, 9, 1, 1, 40, tzinfo=timezone.utc),
            "max_window_minutes": 50,
            "params": {"time_type": "2"},
        }
        values.update(changes)
        return WdtDataset(**values)

    def test_uses_only_allowlisted_method_and_non_overlapping_fifty_minute_windows(self):
        calls = []
        gateway = WdtReadGateway(call=lambda method, params, **page: calls.append((method, params, page)) or {"data": {"order": []}})

        records = gateway.read_dataset(self._dataset())

        self.assertEqual([], records)
        self.assertEqual(3, len(calls))
        self.assertEqual({"sales.TradeQuery.queryWithDetail"}, {call[0] for call in calls})
        self.assertEqual([0, 0, 0], [call[2]["page_no"] for call in calls])
        self.assertEqual("2026-09-01 00:00:00", calls[0][1]["start_time"])
        self.assertEqual("2026-09-01 00:50:00", calls[0][1]["end_time"])
        self.assertEqual("2026-09-01 01:40:00", calls[-1][1]["end_time"])

    def test_rejects_unknown_response_shape_full_final_page_and_missing_stable_id(self):
        dataset = self._dataset(window_end=datetime(2026, 9, 1, 0, 50, tzinfo=timezone.utc))
        for response, message in (
            ({"data": {"unexpected": []}}, "row list"),
            ({"data": {"order": [{"trade_no": "one"}, {"trade_no": "two"}]}}, "max_pages"),
            ({"data": {"order": [{"trade_no": ""}]}}, "record_id_path"),
        ):
            with self.subTest(message=message):
                gateway = WdtReadGateway(call=lambda *_args, **_kwargs: response)
                expected = PaginationLimitExceeded if message == "max_pages" else WdtReadError
                with self.assertRaisesRegex(expected, message):
                    gateway.read_dataset(dataset)

    def test_rejects_unallowlisted_method_without_invoking_transport(self):
        calls = []
        gateway = WdtReadGateway(call=lambda *args, **kwargs: calls.append((args, kwargs)))
        dataset = self._dataset(method="stock.adjust")

        with self.assertRaisesRegex(WdtReadError, "method"):
            gateway.read_dataset(dataset)

        self.assertEqual([], calls)

    def test_duplicate_id_in_same_page(self):
        dataset = self._dataset(
            window_end=datetime(2026, 9, 1, 0, 50, tzinfo=timezone.utc),
        )
        response = {"data": {"order": [{"trade_no": "DUP"}, {"trade_no": "DUP"}]}}
        gateway = WdtReadGateway(call=lambda *_a, **_kw: response)

        with self.assertRaisesRegex(WdtReadError, "duplicate"):
            gateway.read_dataset(dataset)

    def test_boundary_windows_are_contiguous_no_gap_no_overlap(self):
        calls = []
        dataset = self._dataset(
            window_end=datetime(2026, 9, 1, 1, 30, tzinfo=timezone.utc),
        )
        gateway = WdtReadGateway(
            call=lambda method, params, **page: calls.append((method, params, page))
            or {"data": {"order": []}}
        )

        gateway.read_dataset(dataset)

        windows = [(c[1]["start_time"], c[1]["end_time"]) for c in calls]
        self.assertEqual(2, len(windows))
        self.assertEqual("2026-09-01 00:00:00", windows[0][0])
        self.assertEqual("2026-09-01 00:50:00", windows[0][1])
        self.assertEqual("2026-09-01 00:50:00", windows[1][0])
        self.assertEqual("2026-09-01 01:30:00", windows[1][1])

    def test_goods_list_response_key(self):
        dataset = self._dataset(
            method="wms.StockSpec.search2",
            record_id_path="spec_no",
            window_end=datetime(2026, 9, 1, 0, 40, tzinfo=timezone.utc),
        )
        response = {"data": {"goods_list": [{"spec_no": "S1"}]}}
        gateway = WdtReadGateway(call=lambda *_a, **_kw: response)

        records = gateway.read_dataset(dataset)

        self.assertEqual(1, len(records))
        record, stable_id = records[0]
        self.assertEqual("S1", stable_id)
        self.assertEqual({"spec_no": "S1"}, record)

    def test_time_boxed_false_makes_single_call_without_window_params(self):
        calls = []
        dataset = self._dataset(
            method="goods.Goods.queryWithSpec",
            record_id_path="goods_id",
            time_boxed=False,
            params={},
        )
        gateway = WdtReadGateway(
            call=lambda method, params, **page: calls.append(dict(params))
            or {"data": {"goods_list": [{"goods_id": "G1"}]}}
        )

        records = gateway.read_dataset(dataset)

        self.assertEqual(1, len(calls))
        self.assertNotIn("start_time", calls[0])
        self.assertNotIn("end_time", calls[0])
        self.assertEqual(1, len(records))
        self.assertEqual("G1", records[0][1])

    def test_detail_list_response_key_with_composite_id(self):
        dataset = self._dataset(
            method="wms.StockSpec.search2",
            record_id_path="spec_no,warehouse_no",
            page_size=10,
            window_end=datetime(2026, 9, 1, 0, 40, tzinfo=timezone.utc),
        )
        response = {"data": {"detail_list": [
            {"spec_no": "S1", "warehouse_no": "W1"},
            {"spec_no": "S1", "warehouse_no": "W2"},
        ]}}
        gateway = WdtReadGateway(call=lambda *_a, **_kw: response)

        records = gateway.read_dataset(dataset)

        self.assertEqual({"S1|W1", "S1|W2"}, {sid for _, sid in records})

    def test_composite_record_id_path_rejects_empty_part(self):
        dataset = self._dataset(
            method="wms.StockSpec.search2",
            record_id_path="spec_no,warehouse_no",
            window_end=datetime(2026, 9, 1, 0, 40, tzinfo=timezone.utc),
        )
        response = {"data": {"detail_list": [{"spec_no": "S1", "warehouse_no": ""}]}}
        gateway = WdtReadGateway(call=lambda *_a, **_kw: response)

        with self.assertRaisesRegex(WdtReadError, "record_id_path"):
            gateway.read_dataset(dataset)

    def test_numeric_record_id_is_coerced_to_string(self):
        dataset = self._dataset(
            method="wms.StockSpec.search2",
            record_id_path="rec_id",
            page_size=10,
            window_end=datetime(2026, 9, 1, 0, 40, tzinfo=timezone.utc),
        )
        response = {"data": {"detail_list": [{"rec_id": 18943, "spec_no": "S1"}]}}
        gateway = WdtReadGateway(call=lambda *_a, **_kw: response)

        records = gateway.read_dataset(dataset)

        self.assertEqual(1, len(records))
        self.assertEqual("18943", records[0][1])
