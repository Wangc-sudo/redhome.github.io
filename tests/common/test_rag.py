# -*- coding: utf-8 -*-
"""common.rag 单元测试：embed / llm / 索引管道 / 检索编排，外部 API 全部打桩。"""

import base64
import json
import os
import re
import unittest
from unittest import mock

from common.kb_qa import media
from common.rag import embed, freshness, index_docs, index_products, index_state, llm, qa


def _fake_post_factory(vectors, *, strict_count=False):
    """构造 embed._post 替身：按 input 数量返回对应向量。

    strict_count=True 时只返回 len(vectors) 条（用于数量不符报错路径）。
    """
    def _post(payload, api_key):
        if strict_count:
            return [
                {"index": i, "embedding": list(vec)}
                for i, vec in enumerate(vectors)
            ]
        return [
            {"index": i, "embedding": list(vectors[i % len(vectors)])}
            for i in range(len(payload["input"]))
        ]
    return _post


class EmbedTest(unittest.TestCase):

    def test_embed_items_batches_text_and_image(self):
        payload_seen = {}

        def fake_post(payload, api_key):
            payload_seen.update(payload)
            return [
                {"index": 0, "embedding": [1.0, 0.0]},
                {"index": 1, "embedding": [0.0, 1.0]},
            ]

        with mock.patch.object(embed, "_post", fake_post):
            vectors = embed.embed_items(
                [{"text": "习酒"}, {"image": "data:image/png;base64,xx"}],
                "key", dimensions=2)
        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(payload_seen["model"], embed.DEFAULT_MODEL)
        self.assertEqual(payload_seen["dimensions"], 2)

    def test_embed_items_rejects_count_mismatch(self):
        with mock.patch.object(
                embed, "_post",
                _fake_post_factory([[1.0]], strict_count=True)), \
             self.assertRaises(embed.EmbedError):
            embed.embed_items([{"text": "a"}, {"text": "b"}], "key")

    def test_embed_items_aligns_by_index_not_response_order(self):
        response = [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]
        with mock.patch.object(embed, "_post", return_value=response):
            vectors = embed.embed_items(
                [{"text": "a"}, {"text": "b"}], "key", dimensions=2)
        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])

    def test_embed_items_rejects_missing_index(self):
        with mock.patch.object(
                embed, "_post", return_value=[{"embedding": [1.0, 0.0]}]), \
             self.assertRaises(embed.EmbedError):
            embed.embed_items([{"text": "a"}], "key")

    def test_embed_items_rejects_duplicate_index(self):
        response = [
            {"index": 0, "embedding": [1.0, 0.0]},
            {"index": 0, "embedding": [0.0, 1.0]},
        ]
        with mock.patch.object(embed, "_post", return_value=response), \
             self.assertRaises(embed.EmbedError):
            embed.embed_items([{"text": "a"}, {"text": "b"}], "key")

    def test_embed_items_rejects_bad_dimension_and_non_finite_values(self):
        bad_vectors = (
            [1.0],
            [float("nan"), 0.0],
            [float("inf"), 0.0],
            ["not-a-number", 0.0],
        )
        for vector in bad_vectors:
            with self.subTest(vector=vector):
                response = [{"index": 0, "embedding": vector}]
                with mock.patch.object(embed, "_post", return_value=response), \
                     self.assertRaises(embed.EmbedError):
                    embed.embed_items([{"text": "a"}], "key", dimensions=2)

    def test_embed_items_rejects_inconsistent_actual_dimensions(self):
        response = [
            {"index": 0, "embedding": [1.0, 0.0]},
            {"index": 1, "embedding": [1.0]},
        ]
        with mock.patch.object(embed, "_post", return_value=response), \
             self.assertRaises(embed.EmbedError):
            embed.embed_items([{"text": "a"}, {"text": "b"}], "key")

    def test_embed_text_and_image_bytes(self):
        seen = []

        def fake_post(payload, api_key):
            seen.append(payload)
            return [{"index": 0, "embedding": [0.5, 0.5]}]

        with mock.patch.object(embed, "_post", fake_post):
            self.assertEqual(embed.embed_text("酒", "key"), [0.5, 0.5])
            self.assertEqual(
                embed.embed_image_bytes(b"\x89PNG", "key", mime="image/png"),
                [0.5, 0.5])
        # 第二次调用（图片）应走 data URI。
        self.assertTrue(seen[1]["input"][0]["image"]
                        .startswith("data:image/png;base64,"))

    def test_to_data_uri(self):
        uri = embed.to_data_uri(b"abc", "image/jpeg")
        self.assertEqual(uri, "data:image/jpeg;base64,"
                              + base64.b64encode(b"abc").decode())

    def test_cosine(self):
        self.assertAlmostEqual(embed.cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(embed.cosine([1, 0], [0, 1]), 0.0)
        self.assertEqual(embed.cosine([0, 0], [1, 1]), 0.0)


class LLMTest(unittest.TestCase):

    def test_build_context_marks_image_and_truncates(self):
        hits = [
            {"kind": "text", "name": "习酒", "text": "酱香型", "score": 0.9},
            {"kind": "image", "name": "习酒", "image_ref": "r1/细节图/0",
             "score": 0.8},
        ]
        context = llm.build_context(hits)
        self.assertIn("[资料] 习酒：酱香型", context)
        self.assertIn("[图片] 习酒（r1/细节图/0）", context)
        short = llm.build_context(hits, max_chars=10)
        self.assertEqual(short, "")

    def test_answer_returns_none_without_hits(self):
        self.assertIsNone(llm.answer("问题", [], "key"))

    def test_answer_posts_openai_format(self):
        def fake_post(base, payload, api_key):
            self.assertEqual(base, llm.DEFAULT_BASE)
            self.assertEqual(payload["model"], llm.DEFAULT_MODEL)
            self.assertEqual(payload["messages"][0]["role"], "system")
            self.assertIn("问题", payload["messages"][1]["content"])
            return {"choices": [{"message": {"content": "答案"}}]}

        with mock.patch.object(llm, "_post", fake_post):
            result = llm.answer(
                "问题", [{"kind": "text", "name": "n", "text": "t"}], "key")
        self.assertEqual(result, "答案")

    def test_answer_rejects_empty_choices(self):
        with mock.patch.object(llm, "_post", lambda *a: {"choices": []}), \
             self.assertRaises(llm.LLMError):
            llm.answer("q", [{"kind": "text", "name": "n", "text": "t"}], "k")


def _record(record_id, name, attachments):
    fields = {"货品名称": name}
    fields.update(attachments)
    return {"id": record_id, "fields": fields}


def _img(name, url):
    return {"filename": name, "size": 10, "type": "image/jpeg", "url": url}


class BuildTextTest(unittest.TestCase):

    def test_joins_core_fields(self):
        row = {"code": "C1", "name": "习酒", "brand": "习酒", "category": "白酒",
               "box_qty": 6, "case_qty": None, "size_mm": None,
               "weight_kg": None, "material": None, "gift_bag_spec": None,
               "barcode_single": None, "barcode_case": None}
        self.assertEqual(index_products.build_text(row),
                         "编码:C1；名称:习酒；品牌:习酒；大类:白酒；箱规:6")


class IterImageAttachmentsTest(unittest.TestCase):

    def test_keeps_only_images_with_url(self):
        record = _record("r1", "习酒", {
            "细节图": [
                _img("a.jpg", "http://oss/a"),
                {"filename": "b.pdf", "type": "application/pdf",
                 "url": "http://oss/b"},
                _img("c.png", None),  # 无 URL 跳过
            ],
            "质检报告": [{"filename": "d.pdf", "url": "http://oss/d"}],
        })
        found = index_products.iter_image_attachments(record)
        self.assertEqual(len(found), 1)
        field, index, meta = found[0]
        self.assertEqual((field, index), ("细节图", 0))
        self.assertEqual(meta["url"], "http://oss/a")
        self.assertEqual(meta["mime"], "image/jpeg")


class _FakeClient:
    def __init__(self, records):
        self._records = records

    def list_records(self, base_id, sheet_id):
        return list(self._records)


class _FakeStore:
    def __init__(self, dim):
        self.dim = dim
        self.items = None

    def ensure_collection(self, milvus, dim):
        assert dim == self.dim
        return dim

    def upsert_items(self, milvus, items):
        self.items = list(items)
        return len(items)


class RunIndexTest(unittest.TestCase):

    def _run(self, records, *, image_bytes=b"\x89PNG", text_dim=2):
        store = _FakeStore(text_dim)
        vectors = [[1.0, 0.0], [0.0, 1.0], [0.7, 0.7]]

        def fake_embed(items, key, **kw):
            return [list(vectors[i % len(vectors)]) for i in range(len(items))]

        with mock.patch.object(embed, "embed_items",
                               mock.Mock(side_effect=fake_embed)), \
             mock.patch.object(media, "download_bytes",
                               mock.Mock(return_value=image_bytes)):
            stats = index_products.run_index(
                client=_FakeClient(records), milvus=object(), store=store,
                api_key="k", base_id="b", sheet_id="s", dim=text_dim)
        return stats, store

    def test_indexes_text_and_image(self):
        records = [
            _record("r1", "习酒", {"细节图": [_img("a.jpg", "http://oss/a")]}),
            _record("r2", "茅台", {}),
        ]
        stats, store = self._run(records)
        self.assertEqual(stats["records_read"], 2)
        self.assertEqual(stats["text"], 2)
        self.assertEqual(stats["image"], 1)
        self.assertEqual(stats["image_failed"], 0)
        self.assertEqual(stats["written"], 3)
        ids = {item["id"] for item in store.items}
        self.assertEqual(ids, {"product:r1", "product:r2",
                               "product-img:r1:细节图:0"})
        image_item = next(i for i in store.items if i["kind"] == "image")
        self.assertEqual(image_item["name"], "习酒")
        self.assertEqual(image_item["image_ref"], "r1/细节图/0")

    def test_embed_failure_skips_images_of_that_product(self):
        records = [_record("r1", "习酒", {"细节图": [_img("a.jpg", "http://a"),
                                                  _img("b.jpg", "http://b")]})]

        def flaky_embed(items, key, **kw):
            if any("image" in item for item in items):
                raise embed.EmbedError("boom")
            return [[1.0, 0.0] for _ in items]

        store = _FakeStore(2)
        with mock.patch.object(embed, "embed_items",
                               mock.Mock(side_effect=flaky_embed)), \
             mock.patch.object(media, "download_bytes",
                               mock.Mock(return_value=b"x")):
            stats = index_products.run_index(
                client=_FakeClient(records), milvus=object(), store=store,
                api_key="k", base_id="b", sheet_id="s", dim=2)
        self.assertEqual(stats["text"], 1)
        self.assertEqual(stats["image"], 0)
        self.assertEqual(stats["image_failed"], 2)

    def test_main_skips_without_embed_key(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("JINA_API_KEY", "RAG_EMBED_API_KEY")}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("builtins.print") as out:
            with self.assertRaises(SystemExit) as raised:
                index_products.main([])
        self.assertEqual(raised.exception.code, 1)
        printed = " ".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("status=skipped", printed)
        self.assertIn("reason=no_embed_key", printed)

    def test_ctaigw_provider_uses_signed_url_and_downloads_for_hash(self):
        """ctaigw 用签名 URL embedding，但仍下载实际字节计算内容哈希。"""
        records = [
            _record("r1", "习酒", {"细节图": [_img("a.jpg", "http://oss/a")]})]
        store = _FakeStore(2)
        captured = []

        def fake_embed(items, key, **kw):
            captured.append(list(items))
            return [[0.1, 0.2] for _ in items]

        with mock.patch.object(embed, "embed_items",
                               mock.Mock(side_effect=fake_embed)), \
             mock.patch.object(media, "download_bytes", return_value=b"x") as dl:
            index_products.run_index(
                client=_FakeClient(records), milvus=object(), store=store,
                api_key="k", base_id="b", sheet_id="s", dim=2,
                provider=embed.PROVIDER_CTAIGW)
        dl.assert_called_once_with("http://oss/a")
        image_batch = [b for b in captured if any("image" in i for i in b)]
        self.assertEqual(image_batch, [[{"image": "http://oss/a"}]])


class _IncrementalStore:
    def __init__(self, dim):
        self.dim = dim
        self.items = {}

    def ensure_collection(self, milvus, dim):
        assert dim == self.dim
        return dim

    def upsert_items(self, milvus, items):
        for item in items:
            self.items[item["id"]] = dict(item)
        return len(items)

    def delete_ids(self, milvus, ids):
        deleted = 0
        for item_id in ids:
            deleted += self.items.pop(item_id, None) is not None
        return deleted

    def stage_items(self, milvus, items, *, run_id):
        return [
            {"staged_id": f"__stage__:{run_id}:{index}", "item": item}
            for index, item in enumerate(items)
        ]

    def verify_staged_items(self, milvus, staged, *, dim):
        return True

    def publish_staged_items(self, milvus, staged):
        return self.upsert_items(milvus, [entry["item"] for entry in staged])

    def discard_staged_items(self, milvus, staged):
        return len(staged)


class _FakeStateRepository:
    def __init__(self):
        self.rows = {}
        self.preload_calls = 0
        self.complete_checks = []

    def preload(self):
        self.preload_calls += 1
        return {key: dict(value) for key, value in self.rows.items()}

    def save_batch(self, rows, *, run_id):
        for row in rows:
            value = dict(row)
            value["last_seen_run"] = run_id
            self.rows[value["item_id"]] = value
        return len(rows)

    def reconcile(self, run_id, *, complete):
        self.complete_checks.append(complete)
        if not complete:
            return []
        return sorted(
            item_id for item_id, row in self.rows.items()
            if row["last_seen_run"] != run_id)

    def delete_ids(self, item_ids):
        for item_id in item_ids:
            self.rows.pop(item_id, None)
        return len(item_ids)


class IncrementalIndexTest(unittest.TestCase):
    def _run(self, records, store, state, embed_mock, download_mock=None,
             *, provider=embed.DEFAULT_PROVIDER, model=embed.DEFAULT_MODEL,
             auto_delete=False, delete_max_count=100, delete_max_ratio=0.1):
        download_mock = download_mock or mock.Mock(return_value=b"image")
        with mock.patch.object(embed, "embed_items", embed_mock), \
             mock.patch.object(media, "download_bytes", download_mock):
            stats = index_products.run_index(
                client=_FakeClient(records), milvus=object(), store=store,
                api_key="k", base_id="b", sheet_id="s", dim=2,
                provider=provider, model=model, state_repository=state,
                auto_delete=auto_delete, delete_max_count=delete_max_count,
                delete_max_ratio=delete_max_ratio)
        return stats

    @staticmethod
    def _vectors(items, key, **kw):
        return [[0.1, 0.2] for _ in items]

    def test_second_snapshot_has_zero_embedding_calls_and_ignores_signed_url(self):
        first_records = [
            _record("r1", "习酒", {"细节图": [_img("a.jpg", "http://oss/a")]})]
        second_records = [
            _record("r1", "习酒", {"细节图": [_img("a.jpg", "http://oss/b")]})]
        store, state = _IncrementalStore(2), _FakeStateRepository()
        embed_mock = mock.Mock(side_effect=self._vectors)
        download_mock = mock.Mock(return_value=b"same-image")
        first = self._run(
            first_records, store, state, embed_mock, download_mock,
            provider=embed.PROVIDER_CTAIGW, model="qwen3-vl-embedding")
        self.assertEqual(first["api_calls"], 2)
        embed_mock.reset_mock()

        second = self._run(
            second_records, store, state, embed_mock, download_mock,
            provider=embed.PROVIDER_CTAIGW, model="qwen3-vl-embedding")
        self.assertEqual(embed_mock.call_count, 0)
        self.assertEqual(download_mock.call_count, 2)
        self.assertEqual(second["unchanged"], 2)
        self.assertEqual(second["text_embedded"], 0)
        self.assertEqual(second["image_embedded"], 0)
        self.assertEqual(second["api_calls"], 0)
        self.assertEqual(second["written"], 0)
        self.assertEqual(state.preload_calls, 2)

    def test_changed_image_bytes_reembed_only_image(self):
        records = [_record(
            "r1", "习酒", {"细节图": [_img("a.jpg", "http://oss/a")]})]
        store, state = _IncrementalStore(2), _FakeStateRepository()
        embed_mock = mock.Mock(side_effect=self._vectors)
        download_mock = mock.Mock(side_effect=[b"before", b"after"])
        self._run(records, store, state, embed_mock, download_mock)
        embed_mock.reset_mock()

        stats = self._run(records, store, state, embed_mock, download_mock)
        self.assertEqual(embed_mock.call_count, 1)
        self.assertIn("image", embed_mock.call_args.args[0][0])
        self.assertEqual(stats["unchanged"], 1)
        self.assertEqual(stats["text_embedded"], 0)
        self.assertEqual(stats["image_embedded"], 1)

    def test_model_change_does_not_use_stale_vector(self):
        records = [_record("r1", "习酒", {})]
        store, state = _IncrementalStore(2), _FakeStateRepository()
        embed_mock = mock.Mock(side_effect=self._vectors)
        self._run(records, store, state, embed_mock)
        embed_mock.reset_mock()

        stats = self._run(records, store, state, embed_mock, model="other-model")
        self.assertEqual(embed_mock.call_count, 1)
        self.assertEqual(stats["unchanged"], 0)
        self.assertEqual(stats["text_embedded"], 1)

    def test_failed_update_keeps_last_good_vector_and_marks_degraded(self):
        store, state = _IncrementalStore(2), _FakeStateRepository()
        embed_mock = mock.Mock(side_effect=self._vectors)
        self._run([_record("r1", "旧名称", {})], store, state, embed_mock)
        old_vector = list(store.items["product:r1"]["vector"])
        old_success = state.rows["product:r1"]["last_success_at"]
        embed_mock.side_effect = embed.EmbedError("boom")

        stats = self._run(
            [_record("r1", "新名称", {})], store, state, embed_mock)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["written"], 0)
        self.assertEqual(store.items["product:r1"]["vector"], old_vector)
        row = state.rows["product:r1"]
        self.assertEqual(row["status"], index_state.STATUS_DEGRADED)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["last_success_at"], old_success)
        self.assertEqual(state.complete_checks[-1], False)

    def test_publish_failure_keeps_last_good_vector(self):
        store, state = _IncrementalStore(2), _FakeStateRepository()
        embed_mock = mock.Mock(side_effect=self._vectors)
        self._run([_record("r1", "旧名称", {})], store, state, embed_mock)
        old_vector = list(store.items["product:r1"]["vector"])
        with mock.patch.object(
                store, "publish_staged_items", side_effect=RuntimeError("disk")):
            stats = self._run(
                [_record("r1", "新名称", {})], store, state, embed_mock)
        self.assertEqual(1, stats["publish_failed"])
        self.assertEqual(0, stats["written"])
        self.assertEqual(old_vector, store.items["product:r1"]["vector"])
        self.assertEqual(index_state.STATUS_DEGRADED, state.rows["product:r1"]["status"])
        self.assertEqual(state.complete_checks[-1], False)

    def test_invalid_vector_never_reaches_store(self):
        store, state = _IncrementalStore(2), _FakeStateRepository()
        embed_mock = mock.Mock(return_value=[[float("nan"), 0.2]])
        stats = self._run(
            [_record("r1", "习酒", {})], store, state, embed_mock)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["written"], 0)
        self.assertEqual(store.items, {})
        self.assertEqual(
            state.rows["product:r1"]["status"], index_state.STATUS_DEGRADED)

    def test_failed_image_batch_is_isolated_by_single_image_retries(self):
        records = [_record("r1", "习酒", {"细节图": [
            _img("bad.jpg", "http://oss/bad"),
            _img("good.jpg", "http://oss/good"),
        ]})]
        store, state = _IncrementalStore(2), _FakeStateRepository()
        single_calls = []

        def flaky_embed(items, key, **kw):
            if not any("image" in item for item in items):
                return [[0.1, 0.2] for _ in items]
            if len(items) > 1:
                raise embed.EmbedError("batch failed")
            single_calls.append(items[0]["image"])
            if len(single_calls) == 1:
                raise embed.EmbedError("bad image")
            return [[0.3, 0.4]]

        stats = self._run(
            records, store, state, mock.Mock(side_effect=flaky_embed))
        self.assertEqual(stats["image"], 1)
        self.assertEqual(stats["image_failed"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["retried"], 2)
        self.assertEqual(stats["api_calls"], 4)
        image_ids = [key for key, item in store.items.items()
                     if item["kind"] == "image"]
        self.assertEqual(image_ids, ["product-img:r1:细节图:1"])

    def test_complete_snapshot_deletes_missing_ids(self):
        store, state = _IncrementalStore(2), _FakeStateRepository()
        embed_mock = mock.Mock(side_effect=self._vectors)
        records = [_record("r1", "一", {}), _record("r2", "二", {})]
        self._run(records, store, state, embed_mock)
        embed_mock.reset_mock()

        stats = self._run([records[0]], store, state, embed_mock,
                          auto_delete=True, delete_max_ratio=1.0)
        self.assertEqual(embed_mock.call_count, 0)
        self.assertEqual(stats["deleted"], 1)
        self.assertNotIn("product:r2", store.items)
        self.assertNotIn("product:r2", state.rows)

        empty = self._run([], store, state, embed_mock)
        self.assertEqual(empty["status"], "skipped")
        self.assertEqual(empty["deleted"], 0)
        self.assertIn("product:r1", store.items)

    def test_deletion_is_plan_only_by_default(self):
        store, state = _IncrementalStore(2), _FakeStateRepository()
        embed_mock = mock.Mock(side_effect=self._vectors)
        records = [_record("r1", "一", {}), _record("r2", "二", {})]
        self._run(records, store, state, embed_mock)

        stats = self._run([records[0]], store, state, embed_mock)
        self.assertEqual(stats["delete_planned"], 1)
        self.assertEqual(stats["deleted"], 0)
        self.assertEqual(stats["status"], "degraded")
        self.assertEqual(stats["reason"], "deletion_pending")
        self.assertIn("product:r2", store.items)
        self.assertIn("product:r2", state.rows)

    def test_deletion_threshold_blocks_even_when_enabled(self):
        store, state = _IncrementalStore(2), _FakeStateRepository()
        embed_mock = mock.Mock(side_effect=self._vectors)
        records = [_record("r1", "一", {}), _record("r2", "二", {})]
        self._run(records, store, state, embed_mock)

        stats = self._run([records[0]], store, state, embed_mock,
                          auto_delete=True, delete_max_ratio=0.1)
        self.assertEqual(stats["deleted"], 0)
        self.assertEqual(stats["status"], "degraded")
        self.assertEqual(stats["reason"], "delete_threshold")
        self.assertIn("product:r2", store.items)

    def test_incomplete_snapshot_never_deletes_missing_ids(self):
        store, state = _IncrementalStore(2), _FakeStateRepository()
        embed_mock = mock.Mock(side_effect=self._vectors)
        records = [_record("r1", "一", {}), _record("r2", "二", {})]
        self._run(records, store, state, embed_mock)
        embed_mock.reset_mock()

        stats = self._run(
            [records[0], {"id": "broken", "fields": {}}],
            store, state, embed_mock)
        self.assertEqual(stats["status"], "degraded")
        self.assertEqual(stats["reason"], "incomplete_source")
        self.assertEqual(stats["deleted"], 0)
        self.assertIn("product:r2", store.items)
        self.assertEqual(state.complete_checks[-1], False)


class IndexStateRepositoryTest(unittest.TestCase):
    def test_ddl_contains_required_state_fields(self):
        ddl = "\n".join(index_state.ddl_statements())
        for field in (
                "item_id", "record_id", "kind", "content_hash", "model",
                "dim", "last_seen_run", "last_success_at", "status",
                "attempts", "last_error"):
            self.assertIn(f"`{field}`", ddl)

    def test_state_ddl_is_registered_as_independent_mart_migration(self):
        from common.public_data import live_migrations

        migrations = {
            version: (target, statements)
            for version, target, statements in live_migrations._MIGRATIONS
        }
        self.assertIn("mart-ops-rag-index-state-v1", migrations)
        target, statements = migrations["mart-ops-rag-index-state-v1"]
        self.assertEqual("mart", target)
        self.assertEqual(index_state.ddl_statements(), statements)

    def test_save_batch_uses_one_executemany_not_row_sql(self):
        connection = mock.Mock()
        cursor = connection.cursor.return_value
        repository = index_state.RagIndexStateRepository(connection)
        rows = [
            {"item_id": f"product:r{i}", "record_id": f"r{i}",
             "kind": "text", "content_hash": "a" * 64, "model": "m",
             "dim": 2, "last_success_at": None,
             "status": index_state.STATUS_DEGRADED, "attempts": 1,
             "last_error": "EmbedError"}
            for i in range(3)
        ]
        self.assertEqual(repository.save_batch(rows, run_id="run"), 3)
        cursor.executemany.assert_called_once()
        cursor.execute.assert_not_called()
        self.assertEqual(len(cursor.executemany.call_args.args[1]), 3)
        connection.commit.assert_called_once_with()


class CleanMarkdownTest(unittest.TestCase):

    def test_strips_dingtalk_rich_text_noise(self):
        raw = ('<span style="color: rgb(33, 33, 33);">**购喝**</span>'
               '<span style="color: rgb(33, 33, 33);">：同比 \\+1%，21&nbsp;万</span>')
        self.assertEqual(index_docs.clean_markdown(raw), "**购喝**：同比 +1%，21 万")

    def test_drops_table_rule_lines(self):
        # 分隔行零语义，被固定窗口切成独立残片时会霸占 top-k
        raw = "| **月份** | **目标** |\n|---|---|\n| 7月 | 160 |"
        self.assertEqual(index_docs.clean_markdown(raw),
                         "| **月份** | **目标** |\n| 7月 | 160 |")

    def test_collapses_blank_lines(self):
        self.assertEqual(index_docs.clean_markdown("a\n\n\n\nb"), "a\n\nb")


class SplitSectionsTest(unittest.TestCase):

    def test_builds_hierarchy_path_and_drops_empty_parent(self):
        md = ("# 京东\n"
              "## Q3\n"
              "正文一\n"
              "### 1.1 指标\n"
              "正文二\n")
        self.assertEqual(index_docs.split_sections(md), [
            ("京东 > Q3", "正文一"),
            ("京东 > Q3 > 1.1 指标", "正文二"),
        ])

    def test_strips_bold_markers_in_heading(self):
        # 钉钉导出通篇 **粗体标题**，标题要作为路径前缀进 embedding，符号必须清掉
        self.assertEqual(
            index_docs.split_sections("## **市场判断**\n正文")[0][0],
            "市场判断")

    def test_flat_document_without_heading(self):
        self.assertEqual(index_docs.split_sections("只有一段话"),
                         [("", "只有一段话")])


class BuildChunksTest(unittest.TestCase):

    def test_prefixes_heading_path_and_slides_long_section(self):
        md = "## 摘要\n" + "甲" * 1500
        chunks = index_docs.build_chunks(md, size=600, overlap=80)
        self.assertTrue(chunks)
        self.assertTrue(all(c.startswith("[摘要] ") for c in chunks))
        self.assertTrue(all(len(c) <= 600 + len("[摘要] ") for c in chunks))
        # 切片内容应连续覆盖原文（相邻两片有 80 字重叠）
        self.assertIn("甲" * 80, chunks[1])

    def test_truncates_to_text_limit(self):
        chunks = index_docs.build_chunks("乙" * 5000, size=600,
                                         text_limit=100)
        self.assertTrue(chunks)
        self.assertTrue(all(len(c) <= 100 for c in chunks))

    def test_empty_content_yields_nothing(self):
        self.assertEqual(index_docs.build_chunks("   "), [])


class NormalizeDocTest(unittest.TestCase):

    def test_accepts_clean_doc_id(self):
        self.assertEqual(
            index_docs.normalize_doc(
                {"doc_id": "amweZ92PV6wQbnYGHq5KD5mqJxEKBD6p",
                 "doc_title": "京东Q3总结", "content": "正文"}),
            ("amweZ92PV6wQbnYGHq5KD5mqJxEKBD6p", "京东Q3总结", "正文"))

    def test_rejects_doc_id_with_space_or_like_wildcard(self):
        for bad in ("a b", "", 'a"b', "a%", None):
            self.assertIsNone(
                index_docs.normalize_doc({"doc_id": bad, "content": "x"}))

    def test_rejects_missing_content(self):
        self.assertIsNone(index_docs.normalize_doc({"doc_id": "ok"}))
        self.assertIsNone(index_docs.normalize_doc("not a dict"))


class _DocStore:
    def __init__(self, dim):
        self.dim = dim
        self.items = []
        self.deleted = []

    def ensure_collection(self, milvus, dim):
        assert dim == self.dim
        return dim

    def list_ids(self, milvus, prefix):
        return [item["id"] for item in self.items
                if item["id"].startswith(prefix)]

    def delete_ids(self, milvus, ids):
        ids = set(ids)
        before = len(self.items)
        self.deleted.extend(sorted(ids))
        self.items = [item for item in self.items if item["id"] not in ids]
        return before - len(self.items)

    def upsert_items(self, milvus, items):
        by_id = {item["id"]: item for item in self.items}
        by_id.update({item["id"]: item for item in items})
        self.items = list(by_id.values())
        return len(items)

    def search(self, milvus, vector, *, limit, kind):
        return [{"id": item["id"]} for item in reversed(self.items[-limit:])]


class IndexDocsRunTest(unittest.TestCase):

    def _run(self, payload, *, dim=2):
        store = _DocStore(dim)

        def fake_embed(items, key, **kw):
            return [[0.1, 0.2] for _ in items]

        with mock.patch.object(embed, "embed_items",
                               mock.Mock(side_effect=fake_embed)):
            stats = index_docs.run_index(
                payload, milvus=object(), store=store, api_key="k", dim=dim)
        return stats, store, fake_embed

    def test_indexes_doc_with_versioned_chunk_ids(self):
        payload = [{"doc_id": "jd-q3", "doc_title": "京东Q3总结及Q4规划",
                    "content": "## 摘要\n销售额 500 万。\n## Q4 打法\n三条主线。"}]
        stats, store, _ = self._run(payload)
        self.assertEqual(stats["docs"], 1)
        self.assertEqual(stats["chunks"], 2)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(stats["written"], 2)
        ids = [item["id"] for item in store.items]
        self.assertEqual(len(ids), 2)
        self.assertTrue(all(re.match(r"doc:jd-q3:[0-9a-f]{20}:000[01]$", item_id)
                            for item_id in ids))
        self.assertTrue(all(i["kind"] == "text" for i in store.items))
        self.assertTrue(all(i["name"] == "京东Q3总结及Q4规划" for i in store.items))
        self.assertTrue(all(i["image_ref"] == "" for i in store.items))
        self.assertTrue(store.items[0]["text"].startswith("[摘要] "))

    def test_skips_invalid_records(self):
        payload = [{"doc_id": "bad id", "content": "x"},
                   {"doc_id": "no-content"},
                   "garbage"]
        stats, store, _ = self._run(payload)
        self.assertEqual(stats["docs"], 0)
        self.assertEqual(stats["chunks"], 0)
        self.assertEqual(stats["skipped"], 3)
        self.assertEqual(stats["written"], 0)
        self.assertEqual(stats["status"], "skipped")
        self.assertEqual(store.items, [])

    def test_embedding_failure_keeps_last_good_version(self):
        old_payload = [{"doc_id": "jd-q3", "doc_title": "旧版", "content": "旧正文"}]
        _, store, _ = self._run(old_payload)
        old_ids = {item["id"] for item in store.items}
        with mock.patch.object(
                embed, "embed_items", side_effect=embed.EmbedError("boom")):
            stats = index_docs.run_index(
                [{"doc_id": "jd-q3", "doc_title": "新版", "content": "新正文"}],
                milvus=object(), store=store, api_key="k", dim=2)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["status"], "degraded")
        self.assertEqual(stats["written"], 0)
        self.assertEqual({item["id"] for item in store.items}, old_ids)

    def test_unchanged_document_does_not_embed_again(self):
        payload = [{"doc_id": "jd-q3", "doc_title": "手册", "content": "正文"}]
        store = _DocStore(2)
        with mock.patch.object(
                embed, "embed_items",
                side_effect=lambda items, key, **kw: [[0.1, 0.2] for _ in items]
        ) as embedded:
            first = index_docs.run_index(
                payload, milvus=object(), store=store, api_key="k", dim=2)
            second = index_docs.run_index(
                payload, milvus=object(), store=store, api_key="k", dim=2)
        self.assertEqual(embedded.call_count, 1)
        self.assertEqual(first["unchanged"], 0)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(second["written"], 0)


class IndexDocsMainTest(unittest.TestCase):

    def test_skips_without_embed_key(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("JINA_API_KEY", "RAG_EMBED_API_KEY")}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("builtins.print") as out:
            with self.assertRaises(SystemExit) as raised:
                index_docs.main([])
        self.assertEqual(raised.exception.code, 1)
        printed = " ".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("status=skipped", printed)
        self.assertIn("reason=no_embed_key", printed)

    def test_skips_without_input_file(self):
        with mock.patch.dict(os.environ, {"RAG_EMBED_API_KEY": "k"}), \
             mock.patch("builtins.print") as out:
            with self.assertRaises(SystemExit) as raised:
                index_docs.main(["--file", "/nonexistent/kb_docs.json"])
        self.assertEqual(raised.exception.code, 1)
        printed = " ".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("status=skipped", printed)
        self.assertIn("reason=no_input", printed)

    def test_openai_provider_requires_base_url_and_model(self):
        env = {"RAG_EMBED_API_KEY": "k", "RAG_EMBED_PROVIDER": "openai"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit):
                index_docs.main(["--file", "/nonexistent/kb_docs.json"])

    def test_ctaigw_provider_passes_whitelist(self):
        # ctaigw 应通过白名单走到 no_input（而非 SystemExit 未知 provider）
        env = {"RAG_EMBED_API_KEY": "k", "RAG_EMBED_PROVIDER": "ctaigw"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("builtins.print") as out:
            with self.assertRaises(SystemExit) as raised:
                index_docs.main(["--file", "/nonexistent/kb_docs.json"])
        self.assertEqual(raised.exception.code, 1)
        printed = " ".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("reason=no_input", printed)


class EmbedProviderTest(unittest.TestCase):
    """provider=openai：请求形态与响应解析（不联网，stub HTTP）。"""

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _call(self, items, response, **kw):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            captured["auth"] = req.get_header("Authorization")
            return self._Resp(response)

        with mock.patch.object(embed.urllib.request, "urlopen",
                               mock.Mock(side_effect=fake_urlopen)):
            vectors = embed.embed_items(
                items, "k", model="m", provider="openai",
                base_url="https://gw.example/v1", **kw)
        return vectors, captured

    def test_posts_string_array_and_parses_data(self):
        vectors, captured = self._call(
            [{"text": "甲"}, {"text": "乙"}],
            {"data": [{"index": 0, "embedding": [0.1, 0.2]},
                      {"index": 1, "embedding": [0.3, 0.4]}]})
        self.assertEqual(vectors, [[0.1, 0.2], [0.3, 0.4]])
        self.assertEqual(captured["url"], "https://gw.example/v1/embeddings")
        self.assertEqual(captured["body"]["input"], ["甲", "乙"])
        self.assertEqual(captured["auth"], "Bearer k")

    def test_accepts_output_embeddings_shape(self):
        vectors, _ = self._call(
            [{"text": "甲"}],
            {"output": {"embeddings": [{"embedding": [0.5, 0.6]}]}})
        self.assertEqual(vectors, [[0.5, 0.6]])

    def test_rejects_image_items_and_bad_shape(self):
        with self.assertRaises(embed.EmbedError):
            self._call([{"image": "http://x/y.png"}], {"data": []})
        with self.assertRaises(embed.EmbedError):
            self._call([{"text": "甲"}], {"unexpected": True})

    def test_rejects_actual_dimension_mismatch(self):
        with self.assertRaises(embed.EmbedError):
            self._call(
                [{"text": "甲"}],
                {"data": [{"index": 0, "embedding": [0.1]}]}, dimensions=2)

    def test_requires_base_url(self):
        with self.assertRaises(embed.EmbedError):
            embed.embed_items([{"text": "甲"}], "k", model="m",
                              provider="openai")


class EmbedCtaigwTest(unittest.TestCase):
    """provider=ctaigw：天翼网关实测契约（2026-10-20）。"""

    def _call(self, items, response, **kw):
        captured = {}

        class _Resp:
            def read(self):
                return json.dumps(response).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return _Resp()

        with mock.patch.object(embed.urllib.request, "urlopen",
                               mock.Mock(side_effect=fake_urlopen)):
            vectors = embed.embed_items(
                items, "k", model="qwen3-vl-embedding",
                provider="ctaigw", **kw)
        return vectors, captured

    def test_posts_contents_with_fusion_disabled(self):
        vectors, captured = self._call(
            [{"text": "甲"}, {"image": "http://oss/a.jpg"}, {"text": "乙"}],
            {"output": {"embeddings": [
                {"index": 0, "embedding": [0.1], "type": "vl"},
                {"index": 1, "embedding": [0.2], "type": "vl"},
                {"index": 2, "embedding": [0.3], "type": "vl"}]}})
        self.assertEqual(vectors, [[0.1], [0.2], [0.3]])
        self.assertEqual(captured["url"], embed.CTAIGW_DEFAULT_URL)
        self.assertEqual(captured["body"]["input"]["contents"],
                         [{"text": "甲"}, {"image": "http://oss/a.jpg"},
                          {"text": "乙"}])
        self.assertEqual(captured["body"]["parameters"],
                         {"enable_fusion": False})

    def test_sorts_entries_by_index(self):
        vectors, _ = self._call(
            [{"text": "甲"}, {"text": "乙"}],
            {"output": {"embeddings": [
                {"index": 1, "embedding": [0.2]},
                {"index": 0, "embedding": [0.1]}]}})
        self.assertEqual(vectors, [[0.1], [0.2]])

    def test_rejects_unknown_item_and_bad_shape(self):
        with self.assertRaises(embed.EmbedError):
            self._call([{"video": "http://x/v.mp4"}],
                       {"output": {"embeddings": []}})
        with self.assertRaises(embed.EmbedError):
            self._call([{"text": "甲"}], {"unexpected": True})
        with self.assertRaises(embed.EmbedError):
            # 数量不符（融合成一条却没声明）必须 fail-fast
            self._call([{"text": "甲"}, {"text": "乙"}],
                       {"output": {"embeddings": [{"index": 0,
                                                   "embedding": [0.1]}]}})

    def test_rejects_actual_dimension_mismatch(self):
        with self.assertRaises(embed.EmbedError):
            self._call(
                [{"text": "甲"}],
                {"output": {"embeddings": [
                    {"index": 0, "embedding": [0.1], "type": "vl"}]}},
                dimensions=2)

    def test_default_model_per_provider(self):
        self.assertEqual(embed.default_model("ctaigw"), "qwen3-vl-embedding")
        self.assertEqual(embed.default_model("jina"), "jina-clip-v2")
        self.assertIsNone(embed.default_model("openai"))

    def test_max_batch_per_provider(self):
        # ctaigw 实测 22 条即 HTTP 400（2026-10-20 商品灌库两次全灭于此）
        self.assertEqual(embed.max_batch("ctaigw"), 16)
        self.assertEqual(embed.max_batch("jina"), 64)

    def test_max_image_batch(self):
        # 图片上限比文本更严：ctaigw 实测 8 张 OK、16 张 400
        self.assertEqual(embed.max_image_batch("ctaigw"), 8)
        self.assertEqual(embed.max_image_batch("jina"), 8)

    def test_run_index_defaults_text_batch_by_provider(self):
        """ctaigw 下文本分批 ≤16（64 的 jina 默认值会把整批打挂）。"""
        records = [_record(f"r{i}", f"商品{i}", {}) for i in range(20)]
        store = _FakeStore(2)
        batches = []

        def fake_embed(items, key, **kw):
            batches.append(len(items))
            return [[0.1, 0.2] for _ in items]

        with mock.patch.object(embed, "embed_items",
                               mock.Mock(side_effect=fake_embed)):
            index_products.run_index(
                client=_FakeClient(records), milvus=object(), store=store,
                api_key="k", base_id="b", sheet_id="s", dim=2,
                provider=embed.PROVIDER_CTAIGW)
        self.assertTrue(batches)
        self.assertTrue(all(n <= 16 for n in batches))

    def test_images_are_batched_across_products(self):
        """图片跨商品合批：20 个商品各 1 图 → 2 批（16+4），而非 20 次调用。

        原逐行设计下某张图网关侧挂起会拖死整轮（2026-10-20 实测卡死
        28 分钟）；合批后单批失败只损失该批。
        """
        records = [
            _record(f"r{i}", f"商品{i}",
                    {"细节图": [_img(f"{i}.jpg", f"http://oss/{i}")]})
            for i in range(20)
        ]
        store = _FakeStore(2)
        image_calls = []

        def fake_embed(items, key, **kw):
            if any("image" in i for i in items):
                image_calls.append(len(items))
            return [[0.1, 0.2] for _ in items]

        with mock.patch.object(embed, "embed_items",
                               mock.Mock(side_effect=fake_embed)), \
             mock.patch.object(media, "download_bytes", return_value=b"x"):
            stats = index_products.run_index(
                client=_FakeClient(records), milvus=object(), store=store,
                api_key="k", base_id="b", sheet_id="s", dim=2,
                provider=embed.PROVIDER_CTAIGW)
        # 图片上限 8（实测 16 张/批 HTTP 400，1232 张图曾全灭于此）
        self.assertEqual(image_calls, [8, 8, 4])
        self.assertEqual(stats["image"], 20)
        image_ids = [i["id"] for i in store.items if i["kind"] == "image"]
        self.assertEqual(len(image_ids), 20)


class QaTest(unittest.TestCase):

    def setUp(self):
        qa.clear_query_vector_cache()

    def _store(self, hits):
        return mock.Mock(search=mock.Mock(return_value=hits))

    def test_retrieve_embeds_question_then_searches(self):
        hits = [{"kind": "text", "name": "n", "text": "t", "score": 0.9}]
        with mock.patch.object(embed, "embed_text",
                               mock.Mock(return_value=[0.1, 0.2])) as et:
            result = qa.retrieve("习酒图片", milvus="m", store=self._store(hits),
                                 jina_key="k", limit=3)
        self.assertEqual(result, hits)
        et.assert_called_once()

    def test_answer_question_full_path(self):
        hits = [{"kind": "text", "name": "n", "text": "t", "score": 0.9}]
        with mock.patch.object(embed, "embed_text",
                               mock.Mock(return_value=[0.1])), \
             mock.patch.object(llm, "answer", mock.Mock(return_value="答案")):
            result = qa.answer_question("q", milvus="m",
                                        store=self._store(hits),
                                        jina_key="j", deepseek_key="d")
        self.assertEqual(result["answer"], "答案")
        self.assertEqual(result["hits"], hits)
        self.assertFalse(result["metrics"]["no_hit"])
        self.assertFalse(result["metrics"]["fallback"])
        self.assertEqual(result["metrics"]["top_score"], 0.9)

    def test_answer_question_llm_failure_degrades(self):
        hits = [{"kind": "text", "name": "n", "text": "t"}]
        with mock.patch.object(embed, "embed_text",
                               mock.Mock(return_value=[0.1])), \
             mock.patch.object(llm, "answer",
                               mock.Mock(side_effect=llm.LLMError("x"))):
            result = qa.answer_question("q", milvus="m",
                                        store=self._store(hits),
                                        jina_key="j", deepseek_key="d")
        self.assertIsNone(result["answer"])
        self.assertEqual(result["hits"], hits)
        self.assertFalse(result["metrics"]["no_hit"])
        self.assertTrue(result["metrics"]["fallback"])

    def test_answer_question_no_hits(self):
        with mock.patch.object(embed, "embed_text",
                               mock.Mock(return_value=[0.1])), \
             mock.patch.object(llm, "answer") as answer:
            result = qa.answer_question("q", milvus="m",
                                        store=self._store([]),
                                        jina_key="j", deepseek_key="d")
        self.assertIsNone(result["answer"])
        self.assertEqual(result["hits"], [])
        answer.assert_not_called()
        self.assertTrue(result["metrics"]["no_hit"])
        self.assertTrue(result["metrics"]["fallback"])
        self.assertEqual(result["metrics"]["llm"], 0.0)

    def test_dedupe_keeps_best_product_text_and_limits_images(self):
        hits = [
            {"id": "product-img:r1:细节图:0", "kind": "image", "score": 0.8},
            {"id": "product:r1", "kind": "text", "text": "old", "score": 0.7},
            {"id": "product-img:r1:细节图:1", "kind": "image", "score": 0.95},
            {"id": "product:r1", "kind": "text", "text": "best", "score": 0.9},
            {"id": "product:r2", "kind": "text", "text": "other", "score": 0.75},
            {"id": "doc:d1:0000", "kind": "text", "text": "doc", "score": 0.85},
        ]
        result = qa.dedupe_product_hits(hits, max_images_per_product=1)
        r1_text = [hit for hit in result if hit["id"] == "product:r1"]
        r1_images = [hit for hit in result
                     if hit["id"].startswith("product-img:r1:")]
        self.assertEqual(len(result), 4)
        self.assertEqual([hit["text"] for hit in r1_text], ["best"])
        self.assertEqual(
            [hit["id"] for hit in r1_images], ["product-img:r1:细节图:1"])

    def test_low_score_and_low_margin_gates_skip_llm(self):
        cases = (
            ("low-score", {"min_score": 0.8}, 0.5),
            ("low-margin", {"min_margin": 0.1}, 0.9),
        )
        for question, gate, expected_top in cases:
            with self.subTest(question=question):
                qa.clear_query_vector_cache()
                hits = [
                    {"id": "doc:a:0000", "kind": "text",
                     "score": expected_top},
                    {"id": "doc:b:0000", "kind": "text",
                     "score": expected_top - 0.05},
                ]
                with mock.patch.object(
                        embed, "embed_text", return_value=[0.1]), \
                     mock.patch.object(llm, "answer") as answer:
                    result = qa.answer_question(
                        question, milvus="m", store=self._store(hits),
                        jina_key="j", deepseek_key="d", **gate)
                answer.assert_not_called()
                self.assertEqual(result["hits"], [])
                self.assertEqual(result["metrics"]["top_score"], expected_top)
                self.assertTrue(result["metrics"]["no_hit"])
                self.assertTrue(result["metrics"]["fallback"])
                self.assertFalse(result["metrics"]["gate_passed"])

    def test_query_cache_key_uses_normalized_question_model_and_dim(self):
        store = self._store([])
        with mock.patch.object(
                embed, "embed_text", return_value=[0.1, 0.2]) as embed_text:
            first = qa.answer_question(
                "  习酒\n图片 ", milvus="m", store=store,
                jina_key="j", deepseek_key="d", model="m", dimensions=2)
            second = qa.answer_question(
                "习酒 图片", milvus="m", store=store,
                jina_key="j", deepseek_key="d", model="m", dimensions=2)
            qa.answer_question(
                "习酒 图片", milvus="m", store=store,
                jina_key="j", deepseek_key="d", model="m", dimensions=3)
            qa.answer_question(
                "习酒 图片", milvus="m", store=store,
                jina_key="j", deepseek_key="d", model="other", dimensions=2)
        self.assertEqual(embed_text.call_count, 3)
        self.assertEqual(embed_text.call_args_list[0].args[0], "习酒 图片")
        self.assertFalse(first["metrics"]["cache_hit"])
        self.assertTrue(second["metrics"]["cache_hit"])


if __name__ == "__main__":
    unittest.main()
