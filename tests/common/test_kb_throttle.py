# -*- coding: utf-8 -*-
"""D6 入口去重限流与文档预暖测试。"""

import asyncio
import os
import unittest
from unittest import mock

from common.cache_core import MemoryStore
from common.cache_core.redis_store import RedisStore
from common.kb_qa import throttle
from common.kb_qa.app import KbStreamHandler, _prewarm_docs


class _BrokenStore:
    def set_nx(self, key, value, ttl):
        raise RuntimeError("redis down")


class ThrottleTests(unittest.TestCase):
    def test_dedupe_same_sender_and_event_within_window(self):
        store = MemoryStore()
        self.assertFalse(throttle.is_duplicate("u1", "m1", store=store))
        self.assertTrue(throttle.is_duplicate("u1", "m1", store=store))
        self.assertFalse(throttle.is_duplicate("u1", "m2", store=store))
        self.assertFalse(throttle.is_duplicate("u2", "m1", store=store))

    def test_dedupe_default_on_and_can_be_disabled(self):
        store = MemoryStore()
        throttle.is_duplicate("u1", "m1", store=store)
        self.assertTrue(throttle.is_duplicate("u1", "m1", store=store))
        self.assertFalse(throttle.is_duplicate(
            "u1", "m1", store=store, environ={"KB_DEDUP_ENABLE": "0"}))

    def test_dedupe_requires_sender_and_event(self):
        store = MemoryStore()
        self.assertFalse(throttle.is_duplicate("", "m1", store=store))
        self.assertFalse(throttle.is_duplicate("u1", None, store=store))

    def test_min_interval_default_off_and_opt_in(self):
        store = MemoryStore()
        self.assertFalse(throttle.below_min_interval("u1", store=store))
        self.assertFalse(throttle.below_min_interval("u1", store=store))
        env = {"KB_MIN_INTERVAL_ENABLE": "1"}
        self.assertFalse(throttle.below_min_interval(
            "u1", store=store, environ=env))
        self.assertTrue(throttle.below_min_interval(
            "u1", store=store, environ=env))
        self.assertFalse(throttle.below_min_interval(
            "u2", store=store, environ=env))

    def test_store_failure_fail_open(self):
        self.assertFalse(throttle.is_duplicate(
            "u1", "m1", store=_BrokenStore()))
        self.assertFalse(throttle.below_min_interval(
            "u1", store=_BrokenStore(),
            environ={"KB_MIN_INTERVAL_ENABLE": "1"}))


class SetNxTests(unittest.TestCase):
    def test_memory_set_nx_expiry(self):
        store = MemoryStore()
        self.assertTrue(store.set_nx("k", "1", 10, now=100.0))
        self.assertFalse(store.set_nx("k", "1", 10, now=109.9))
        self.assertTrue(store.set_nx("k", "1", 10, now=110.0))

    def test_redis_set_nx_uses_atomic_nx_and_ttl(self):
        class _Client:
            def __init__(self):
                self.calls = []

            def set(self, key, value, nx=False, ex=None):
                self.calls.append((key, value, nx, ex))
                return True

        client = _Client()
        store = RedisStore.__new__(RedisStore)
        store._client = client
        self.assertTrue(store.set_nx("k", "1", 10))
        self.assertEqual([("k", '"1"', True, 10)], client.calls)


class _FakeHeaders:
    def __init__(self, message_id):
        self.message_id = message_id


class _FakeCallback:
    def __init__(self, message_id):
        self.headers = _FakeHeaders(message_id)
        self.data = {
            "conversationId": "cid1",
            "msgtype": "text",
            "text": {"content": "你好"},
            "senderStaffId": "u1",
        }


class HandlerThrottleTests(unittest.TestCase):
    def test_duplicate_stops_before_db_claim(self):
        store = MemoryStore()
        store.set_nx("kbqa:dedupe:dingtalk:u1:m-dup", "1", 10)
        factory = mock.Mock()
        logs = []
        handler = KbStreamHandler(
            connection_factory=factory, log=logs.append,
            throttle_store=store)
        asyncio.run(handler.process(_FakeCallback("m-dup")))
        factory.assert_not_called()
        self.assertIn("kb qa: duplicate_message", logs)

    def test_min_interval_stops_before_db_claim(self):
        store = MemoryStore()
        store.set_nx("kbqa:interval:u1", "1", 10)
        factory = mock.Mock()
        logs = []
        handler = KbStreamHandler(
            connection_factory=factory, log=logs.append,
            throttle_store=store)
        with mock.patch.dict(os.environ, {"KB_MIN_INTERVAL_ENABLE": "1"}):
            asyncio.run(handler.process(_FakeCallback("m-new")))
        factory.assert_not_called()
        self.assertIn("kb qa: limited_message", logs)


class _Conn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class PrewarmTests(unittest.TestCase):
    def test_prewarm_loads_index_and_logs_observation(self):
        conn = _Conn()
        logs = []
        with mock.patch("common.kb_qa.app.docs.prewarm", return_value=333) as warm:
            _prewarm_docs(lambda: conn, log=logs.append, environ={})
        warm.assert_called_once_with(conn)
        self.assertTrue(conn.closed)
        self.assertIn("chunks=333", logs[0])
        self.assertIn("elapsed_ms=", logs[0])

    def test_prewarm_disabled_does_not_touch_db(self):
        factory = mock.Mock()
        _prewarm_docs(factory, environ={"KB_PREWARM_ENABLE": "0"})
        factory.assert_not_called()

    def test_prewarm_failure_does_not_block_startup(self):
        logs = []
        _prewarm_docs(
            mock.Mock(side_effect=RuntimeError("db down")),
            log=logs.append, environ={})
        self.assertEqual(["kb qa: docs prewarm failed: RuntimeError"], logs)


if __name__ == "__main__":
    unittest.main()
