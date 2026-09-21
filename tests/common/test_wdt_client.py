"""WdtClient 限流/超时退避重试的守门测试（2026-09-18 回补实测驱动）。"""
import io
import json
import unittest
import urllib.error
from unittest import mock

from common.wdt.client import WdtClient, WdtError


def _response(payload):
    """伪造 urlopen 的上下文管理器响应。"""
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return _Resp(json.dumps(payload).encode("utf-8"))


def _client(**overrides):
    kwargs = {"rate_limit_wait": 0, "sleep": lambda _s: None}
    kwargs.update(overrides)
    return WdtClient("sid", "key", "secret:salt", **kwargs)


class RateLimitRetryTests(unittest.TestCase):
    def test_retries_on_status_100_rate_limit_then_succeeds(self):
        limited = {"status": 100, "message": "超过每分钟最大调用频率限制,请稍后重试"}
        ok = {"status": 0, "data": {"order": [{"trade_no": "t1"}]}}
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(req)
            return _response(limited if len(calls) < 3 else ok)

        sleeps = []
        client = _client(sleep=sleeps.append)
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            data = client.call("sales.TradeQuery.queryWithDetail", {}, page_size=100)

        self.assertEqual(0, data["status"])
        self.assertEqual(3, len(calls))
        self.assertEqual(2, len(sleeps))

    def test_gives_up_after_retry_budget_exhausted(self):
        limited = {"status": 100, "message": "超过每分钟最大调用频率限制,请稍后重试"}
        client = _client(rate_limit_retries=2)
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, timeout: _response(limited),
        ):
            with self.assertRaises(WdtError):
                client.call("m", {})

    def test_status_100_without_rate_limit_message_raises_immediately(self):
        # status=100 也可能是参数错误，不能一律退避（会掩盖真错误）
        bad = {"status": 100, "message": "start_time 格式错误"}
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(req)
            return _response(bad)

        client = _client()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(WdtError):
                client.call("m", {})
        self.assertEqual(1, len(calls))

    def test_retries_on_url_timeout_then_succeeds(self):
        ok = {"status": 0, "data": {}}
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(req)
            if len(calls) == 1:
                raise urllib.error.URLError("timed out")
            return _response(ok)

        client = _client()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            data = client.call("m", {})
        self.assertEqual(0, data["status"])
        self.assertEqual(2, len(calls))

    def test_url_error_exhausts_budget_then_raises(self):
        client = _client(rate_limit_retries=1)
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("timed out"),
        ):
            with self.assertRaises(urllib.error.URLError):
                client.call("m", {})

    def test_default_wait_is_one_minute_plus(self):
        client = WdtClient("sid", "key", "secret:salt")
        self.assertGreaterEqual(client.rate_limit_wait, 60)
        self.assertGreaterEqual(client.rate_limit_retries, 1)


if __name__ == "__main__":
    unittest.main()
