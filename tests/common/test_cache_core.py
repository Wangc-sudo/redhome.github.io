# -*- coding: utf-8 -*-
"""cache_core 统一语义测试（10-29 方案 D1）。"""

import unittest

from common.cache_core import MemoryStore, get_or_compute


class GetOrComputeTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.calls = []

    def _loader(self, value):
        def load():
            self.calls.append(1)
            return value

        return load

    def test_hit_avoids_reload(self):
        loader = self._loader({"a": 1})
        first = get_or_compute("k", 300, loader, store=self.store, now=100.0)
        second = get_or_compute("k", 300, loader, store=self.store, now=200.0)
        self.assertEqual({"a": 1}, first)
        self.assertEqual({"a": 1}, second)
        self.assertEqual(1, len(self.calls))

    def test_ttl_expiry_reloads(self):
        loader = self._loader([1])
        get_or_compute("k", 300, loader, store=self.store, now=100.0)
        get_or_compute("k", 300, loader, store=self.store, now=399.9)
        self.assertEqual(1, len(self.calls))
        get_or_compute("k", 300, loader, store=self.store, now=400.0)
        self.assertEqual(2, len(self.calls))

    def test_empty_results_not_cached(self):
        for empty in (None, [], {}, (), set()):
            store = MemoryStore()
            calls = []
            loader = lambda: (calls.append(1), empty)[1]  # noqa: E731
            get_or_compute("k", 300, loader, store=store, now=100.0)
            get_or_compute("k", 300, loader, store=store, now=101.0)
            self.assertEqual(2, len(calls), repr(empty))

    def test_loader_exception_not_cached(self):
        def bad():
            self.calls.append(1)
            raise RuntimeError("db down")

        with self.assertRaises(RuntimeError):
            get_or_compute("k", 300, bad, store=self.store, now=100.0)
        value = get_or_compute("k", 300, self._loader("ok"),
                               store=self.store, now=101.0)
        self.assertEqual("ok", value)

    def test_store_failure_fail_open(self):
        class _BrokenStore:
            def get(self, key, *, now=None):
                raise RuntimeError("redis down")

            def set(self, key, value, ttl, *, now=None):
                raise RuntimeError("redis down")

        value = get_or_compute("k", 300, self._loader("ok"),
                               store=_BrokenStore(), now=100.0)
        self.assertEqual("ok", value)

    def test_delete_and_close(self):
        get_or_compute("k", 300, self._loader("v"), store=self.store, now=100.0)
        self.store.delete("k")
        get_or_compute("k", 300, self._loader("v"), store=self.store, now=101.0)
        self.assertEqual(2, len(self.calls))
        self.store.close()
        self.store.close()  # 幂等


class DocsCacheIntegrationTests(unittest.TestCase):
    """docs.py 接入后的行为等价：TTL 内复用、reset 后重载。"""

    def setUp(self):
        from common.kb_qa import docs

        self.docs = docs
        self.calls = []
        docs.reset_cache()

        self._original_load = docs._load
        docs._load = self._fake_load

    def tearDown(self):
        self.docs._load = self._original_load
        self.docs.reset_cache()

    def _fake_load(self, conn):
        self.calls.append(1)
        return [{"doc_id": "d", "doc_title": "t", "chunk_index": 0,
                 "content": "售后退款流程"}]

    def test_ttl_and_reset(self):
        self.docs._index_for(object(), now=1000.0)
        self.docs._index_for(object(), now=1100.0)
        self.assertEqual(1, len(self.calls))
        self.docs.reset_cache()
        self.docs._index_for(object(), now=1101.0)
        self.assertEqual(2, len(self.calls))


if __name__ == "__main__":
    unittest.main()
