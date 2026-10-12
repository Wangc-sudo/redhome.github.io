# -*- coding: utf-8 -*-
"""common.rag 单元测试：embed / llm / 索引管道 / 检索编排，外部 API 全部打桩。"""

import base64
import os
import unittest
from unittest import mock

from common.kb_qa import media
from common.rag import embed, index_products, llm, qa


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
                jina_key="k", base_id="b", sheet_id="s", dim=text_dim)
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
                jina_key="k", base_id="b", sheet_id="s", dim=2)
        self.assertEqual(stats["text"], 1)
        self.assertEqual(stats["image"], 0)
        self.assertEqual(stats["image_failed"], 2)

    def test_main_skips_without_jina_key(self):
        """缺 JINA_API_KEY 时静默跳过（不触发 live 门禁/外部调用）。"""
        env = {k: v for k, v in os.environ.items() if k != "JINA_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("builtins.print") as out:
            index_products.main([])
        printed = " ".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("status=skipped", printed)
        self.assertIn("reason=no_jina_key", printed)


class QaTest(unittest.TestCase):

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
        self.assertEqual(result, {"answer": "答案", "hits": hits})

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

    def test_answer_question_no_hits(self):
        with mock.patch.object(embed, "embed_text",
                               mock.Mock(return_value=[0.1])):
            result = qa.answer_question("q", milvus="m",
                                        store=self._store([]),
                                        jina_key="j", deepseek_key="d")
        self.assertEqual(result, {"answer": None, "hits": []})


if __name__ == "__main__":
    unittest.main()
