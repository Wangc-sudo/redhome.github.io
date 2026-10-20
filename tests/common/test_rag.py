# -*- coding: utf-8 -*-
"""common.rag 单元测试：embed / llm / 索引管道 / 检索编排，外部 API 全部打桩。"""

import base64
import json
import os
import unittest
from unittest import mock

from common.kb_qa import media
from common.rag import embed, index_docs, index_products, llm, qa


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
        """缺 embedding 凭据时静默跳过（不触发 live 门禁/外部调用）。"""
        env = {k: v for k, v in os.environ.items()
               if k not in ("JINA_API_KEY", "RAG_EMBED_API_KEY")}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("builtins.print") as out:
            index_products.main([])
        printed = " ".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("status=skipped", printed)
        self.assertIn("reason=no_embed_key", printed)

    def test_ctaigw_provider_passes_signed_url_without_download(self):
        """ctaigw 图片分支直接传签名 URL（网关实测支持 http URL），不下载字节。"""
        records = [
            _record("r1", "习酒", {"细节图": [_img("a.jpg", "http://oss/a")]})]
        store = _FakeStore(2)
        captured = []

        def fake_embed(items, key, **kw):
            captured.append(list(items))
            return [[0.1, 0.2] for _ in items]

        with mock.patch.object(embed, "embed_items",
                               mock.Mock(side_effect=fake_embed)), \
             mock.patch.object(media, "download_bytes") as dl:
            index_products.run_index(
                client=_FakeClient(records), milvus=object(), store=store,
                api_key="k", base_id="b", sheet_id="s", dim=2,
                provider=embed.PROVIDER_CTAIGW)
        dl.assert_not_called()
        image_batch = [b for b in captured if any("image" in i for i in b)]
        self.assertEqual(image_batch, [[{"image": "http://oss/a"}]])


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
    """index_docs 用的 store 替身：记录写入与前缀删除。"""

    def __init__(self, dim):
        self.dim = dim
        self.items = []
        self.deleted = []

    def ensure_collection(self, milvus, dim):
        assert dim == self.dim
        return dim

    def delete_prefix(self, milvus, prefix):
        self.deleted.append(prefix)
        return 1

    def upsert_items(self, milvus, items):
        self.items.extend(items)
        return len(items)


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

    def test_indexes_doc_with_chunk_ids_and_clears_prefix(self):
        payload = [{"doc_id": "jd-q3", "doc_title": "京东Q3总结及Q4规划",
                    "content": "## 摘要\n销售额 500 万。\n## Q4 打法\n三条主线。"}]
        stats, store, _ = self._run(payload)
        self.assertEqual(stats["docs"], 1)
        self.assertEqual(stats["chunks"], 2)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(stats["written"], 2)
        self.assertEqual(store.deleted, ["doc:jd-q3:"])
        ids = [item["id"] for item in store.items]
        self.assertEqual(ids, ["doc:jd-q3:0000", "doc:jd-q3:0001"])
        self.assertTrue(all(i["kind"] == "text" for i in store.items))
        self.assertTrue(all(i["name"] == "京东Q3总结及Q4规划" for i in store.items))
        self.assertTrue(all(i["image_ref"] == "" for i in store.items))
        self.assertTrue(store.items[0]["text"].startswith("[摘要] "))

    def test_skips_invalid_records(self):
        payload = [{"doc_id": "bad id", "content": "x"},
                   {"doc_id": "no-content"},
                   "garbage"]
        stats, store, _ = self._run(payload)
        self.assertEqual(stats, {"docs": 0, "chunks": 0, "skipped": 3,
                                 "written": 0})
        self.assertEqual(store.items, [])


class IndexDocsMainTest(unittest.TestCase):

    def test_skips_without_embed_key(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("JINA_API_KEY", "RAG_EMBED_API_KEY")}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("builtins.print") as out:
            index_docs.main([])
        printed = " ".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("status=skipped", printed)
        self.assertIn("reason=no_embed_key", printed)

    def test_skips_without_input_file(self):
        with mock.patch.dict(os.environ, {"RAG_EMBED_API_KEY": "k"}), \
             mock.patch("builtins.print") as out:
            index_docs.main(["--file", "/nonexistent/kb_docs.json"])
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
            index_docs.main(["--file", "/nonexistent/kb_docs.json"])
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

    def test_default_model_per_provider(self):
        self.assertEqual(embed.default_model("ctaigw"), "qwen3-vl-embedding")
        self.assertEqual(embed.default_model("jina"), "jina-clip-v2")
        self.assertIsNone(embed.default_model("openai"))


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
