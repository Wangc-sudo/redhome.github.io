import unittest
from datetime import datetime, timedelta
from unittest import mock

from common.public_data.product_catalog import _fetch_all_goods
from common.wdt.client import WdtError


class _RecordingClient:
    def __init__(self, responses=None):
        self.calls = []
        self._responses = list(responses) if responses is not None else [[]]

    def call_paged(self, method, params, page_size=None, max_pages=None):
        self.calls.append((method, params, page_size, max_pages))
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


class _ThrottlingClient:
    def __init__(self, failures):
        self.calls = 0
        self.failures = failures

    def call_paged(self, method, params, page_size=None, max_pages=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise WdtError(
                'WDT goods.Goods.queryWithSpec 失败: {"status": 100, '
                '"message": "超过每分钟最大调用频率限制,请稍后重试"}'
            )
        return []


class FetchAllGoodsTests(unittest.TestCase):
    def test_uses_flagship_method_with_mandatory_time_window_params(self):
        client = _RecordingClient()

        _fetch_all_goods(client, end=datetime(2015, 1, 15))

        self.assertTrue(client.calls)
        for method, params, page_size, max_pages in client.calls:
            self.assertEqual("goods.Goods.queryWithSpec", method)
            self.assertEqual(1, params["hide_deleted"])
            self.assertIn("start_time", params)
            self.assertIn("end_time", params)
            self.assertEqual(100, page_size)
            self.assertEqual(200, max_pages)

    def test_windows_are_contiguous_at_most_thirty_days_and_end_at_end(self):
        client = _RecordingClient()

        _fetch_all_goods(client, end=datetime(2015, 3, 5))

        fmt = "%Y-%m-%d %H:%M:%S"
        windows = [
            (
                datetime.strptime(params["start_time"], fmt),
                datetime.strptime(params["end_time"], fmt),
            )
            for _method, params, _ps, _mp in client.calls
        ]
        self.assertEqual(datetime(2015, 1, 1), windows[0][0])
        self.assertEqual(datetime(2015, 3, 5), windows[-1][1])
        for start, end in windows:
            self.assertLess(start, end)
            self.assertLessEqual(end - start, timedelta(days=30))
        for (_, prev_end), (next_start, _) in zip(windows, windows[1:]):
            self.assertEqual(prev_end, next_start)

    def test_dedups_goods_by_id_keeping_latest_window_snapshot(self):
        stale = {"goods_id": 7, "goods_name": "旧快照", "spec_list": []}
        newest = {"goods_id": 7, "goods_name": "新快照", "spec_list": []}
        other = {"goods_id": 8, "goods_name": "另一切片", "spec_list": []}
        client = _RecordingClient(responses=[[stale], [newest, other]])

        goods = _fetch_all_goods(client, end=datetime(2015, 2, 15))

        self.assertEqual(2, len(goods))
        by_id = {g["goods_id"]: g for g in goods}
        self.assertEqual("新快照", by_id[7]["goods_name"])
        self.assertEqual("另一切片", by_id[8]["goods_name"])

    def test_retries_window_call_after_rate_limit_and_backs_off(self):
        client = _ThrottlingClient(failures=2)

        with mock.patch("common.public_data.product_catalog.time.sleep") as sleep:
            goods = _fetch_all_goods(client, end=datetime(2015, 1, 15))

        self.assertEqual([], goods)
        self.assertGreaterEqual(client.calls, 3)
        self.assertEqual(2, sleep.call_count)
        for delay in (call.args[0] for call in sleep.call_args_list):
            self.assertGreaterEqual(delay, 1)

    def test_gives_up_after_too_many_consecutive_rate_limits(self):
        client = _ThrottlingClient(failures=99)

        with mock.patch("common.public_data.product_catalog.time.sleep") as sleep:
            with self.assertRaisesRegex(WdtError, "频率"):
                _fetch_all_goods(client, end=datetime(2015, 1, 15))

        self.assertLessEqual(sleep.call_count, 10)

    def test_non_rate_limit_errors_propagate_without_retry(self):
        class _BrokenClient:
            def call_paged(self, *_args, **_kwargs):
                raise WdtError('WDT goods.Goods.queryWithSpec 失败: {"status": 99}')

        with mock.patch("common.public_data.product_catalog.time.sleep") as sleep:
            with self.assertRaisesRegex(WdtError, "status\": 99"):
                _fetch_all_goods(_BrokenClient(), end=datetime(2015, 1, 15))

        sleep.assert_not_called()
