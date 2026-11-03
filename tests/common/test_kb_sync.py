# -*- coding: utf-8 -*-
"""保留在数仓库的知识同步、文档检索与媒体缓存测试。"""

import io
import json
import sys
import threading
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from common.kb_qa import bm25, docs, media, sync_docs, sync_products, sync_wiki
from common.kb_qa.sync_products import normalize_record, upsert_products


def _product_row(**overrides):
    row = {
        "record_id": "rec1", "code": "SP001", "name": "习酒窖藏1998",
        "brand": "习酒", "category": "酱香", "box_qty": 6, "case_qty": 2,
        "size_mm": "280x190x90", "weight_kg": 1.35, "material": "陶瓷",
        "gift_bag_spec": "双瓶装", "barcode_single": "6901234567890",
        "barcode_case": "6901234567891", "attachments": None, "extra": None,
    }
    row.update(overrides)
    return row


class _FakeCursor:
    """按脚本返回行的假 cursor；记录所有 execute 调用。"""

    def __init__(self, script):
        self._script = list(script)
        self.executed = []
        self.freshness_executed = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        if "FROM `pd_dataset_freshness`" in sql or "FROM `pd_knowledge_index_freshness`" in sql:
            self.freshness_executed.append((sql, params))
            self.rowcount = {"status": "fresh"}
            return
        self.executed.append((sql, params))
        self.rowcount = self._script.pop(0) if self._script else 0

    def fetchall(self):
        value = self.rowcount
        self.rowcount = 0
        return value if isinstance(value, list) else []

    def fetchone(self):
        value = self.rowcount
        self.rowcount = 0
        return value if isinstance(value, (dict, type(None))) else None

    def close(self):
        pass

class _FakeConn:
    def __init__(self, script=()):
        self.cursor_instance = _FakeCursor(script)
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass

class SyncNormalizeTests(unittest.TestCase):
    def test_full_mapping(self):
        record = {
            "id": "rec9",
            "fields": {
                "商品编码": "SP009",
                "货品名称": "摘要酒·珍品版",
                "品牌": "摘要",
                "箱规": 4,
                "重量（kg）": 1.5,
                "单品69码": "690999",
                "细节图": [
                    {"resourceId": "res-1", "filename": "a.jpg", "size": 1024,
                     "type": "image", "url": "https://expired.example/x"},
                ],
                "礼袋实拍": "公式值",
                "自定义备注": "hello",
            },
        }
        row = normalize_record(record)
        self.assertEqual("rec9", row["record_id"])
        self.assertEqual("摘要酒·珍品版", row["name"])
        self.assertEqual(4, row["box_qty"])
        self.assertEqual(1.5, row["weight_kg"])
        # 附件只留元信息（filename/resourceId），URL 不落地
        self.assertEqual(
            [{"name": "a.jpg", "size": 1024, "mime": "image",
              "resource_id": "res-1"}],
            row["attachments"]["细节图"],
        )
        # 公式字段跳过；未映射标量进 extra
        self.assertEqual({"自定义备注": "hello"}, row["extra"])

    def test_nameless_record_skipped(self):
        self.assertIsNone(normalize_record({"id": "r", "fields": {"品牌": "习酒"}}))
        self.assertIsNone(normalize_record({"fields": {}}))

    def test_number_coercion_and_rich_text(self):
        record = {
            "id": "r2",
            "fields": {
                "货品名称": [{"text": "金沙"}, {"text": "回沙"}],
                "箱规": "6",
            },
        }
        row = normalize_record(record)
        self.assertEqual("金沙回沙", row["name"])
        self.assertEqual(6, row["box_qty"])

    def test_upsert_sql_and_params(self):
        conn = _FakeConn([1, 1])
        written = upsert_products(
            conn,
            [_product_row(), _product_row(record_id="rec2", name="另一款")],
        )
        self.assertEqual(2, written)
        sql, params = conn.cursor_instance.executed[0]
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertEqual("rec1", params[0])
        self.assertEqual("习酒窖藏1998", params[2])

    def test_structured_sync_rejects_partial_snapshot(self):
        conn = _FakeConn()
        records = [
            {"id": "rec1", "fields": {"货品名称": "习酒"}},
            {"id": "broken", "fields": {}},
        ]
        with self.assertRaises(sync_products.SourceSnapshotError):
            sync_products.run_sync(records, conn=conn, source_id="base:sheet")
        self.assertEqual(conn.cursor_instance.executed, [])
        self.assertFalse(conn.committed)

    def test_structured_sync_deletion_is_plan_only_by_default(self):
        conn = _FakeConn([[("removed",)], 1])
        stats = sync_products.run_sync(
            [{"id": "rec1", "fields": {"货品名称": "习酒"}}],
            conn=conn, source_id="base:sheet")
        self.assertEqual(stats["rows_written"], 1)
        self.assertEqual(stats["delete_planned"], 1)
        self.assertEqual(stats["deletion_pending"], 1)
        self.assertEqual(stats["status"], "degraded")
        self.assertTrue(conn.committed)
        self.assertFalse(any(
            sql.startswith("DELETE") for sql, _ in conn.cursor_instance.executed))

class MediaCacheTests(unittest.TestCase):
    def test_cache_key_and_type(self):
        self.assertEqual("r:细节图:0", media.cache_key_for("r", "细节图", 0))
        self.assertEqual("image", media.media_type_for("A.JPG"))
        self.assertEqual("file", media.media_type_for("质检报告.pdf"))

    def test_cached_media_hit_and_miss(self):
        hit = _FakeConn([{"media_id": "m1", "media_type": "image",
                          "file_name": "a.jpg"}])
        self.assertEqual("m1", media.cached_media(hit, "k")["media_id"])
        miss = _FakeConn([None])
        self.assertIsNone(media.cached_media(miss, "k"))

    def test_save_media_upsert(self):
        conn = _FakeConn([1])
        media.save_media(conn, "k", "m1", "image", "a.jpg")
        sql, params = conn.cursor_instance.executed[0]
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertEqual(("k", "m1", "image", "a.jpg"), params[:4])

class EnsureMediaIdTests(unittest.TestCase):
    """2026-10-22 事故回归：附件名称键是 filename（不是 name）——
    漏认会把 PNG 判成 file，图片走成「下载卡片」。"""

    def test_filename_key_yields_image_type(self):
        conn = _FakeConn([None])  # 缓存未命中
        with mock.patch.object(media, "download_bytes",
                               return_value=b"png-bytes"), \
             mock.patch.object(media, "upload_media",
                               return_value="@img") as upload:
            media_id, media_type = media.ensure_media_id(
                conn, oapi_token="tok", record_id="r1",
                field_name="质检报告", index=0,
                attachment_meta={"filename": "报告_00.png",
                                 "url": "http://oss/x"})
        self.assertEqual("image", media_type)
        self.assertEqual("@img", media_id)
        # 上传类型也必须是 image（oapi type 参数）
        self.assertEqual("image", upload.call_args[0][1])

    def test_name_key_still_supported(self):
        conn = _FakeConn([None])
        with mock.patch.object(media, "download_bytes", return_value=b"x"), \
             mock.patch.object(media, "upload_media", return_value="@f"):
            _id, media_type = media.ensure_media_id(
                conn, oapi_token="tok", record_id="r1",
                field_name="素材包", index=1,
                attachment_meta={"name": "包.zip", "url": "http://oss/z"})
        self.assertEqual("file", media_type)

class Bm25Tests(unittest.TestCase):
    """纯 Python BM25：中文 bigram + 英文数字连续串分词。"""

    def test_tokenize_chinese_bigram_and_single(self):
        self.assertEqual(["酱香", "香酒", "酱", "香", "酒"], bm25.tokenize("酱香酒"))
        self.assertEqual(["酒"], bm25.tokenize("酒"))

    def test_tokenize_alnum_and_mixed(self):
        self.assertEqual(["ab12", "习酒", "习", "酒", "34"],
                         bm25.tokenize("AB12习酒34"))
        self.assertEqual(["1998"], bm25.tokenize("1998"))

    def test_topk_drops_zero_score_docs(self):
        index = bm25.Bm25Index(["酱香型白酒工艺", "咖啡豆研磨", "酱香酒的介绍"])
        hits = index.topk("酱香酒", k=3)
        docs_hit = [doc for doc, _ in hits]
        self.assertNotIn(1, docs_hit)   # 零分文档不返回
        self.assertEqual(2, docs_hit[0])  # 命中最多者排第一

    def test_topk_empty_index(self):
        self.assertEqual([], bm25.Bm25Index([]).topk("任意"))

class DocSearchTests(unittest.TestCase):
    """kb_doc_chunks 全量读出 + 进程内 TTL 缓存。"""

    def setUp(self):
        docs.reset_cache()

    def tearDown(self):
        docs.reset_cache()

    def _chunk(self, doc_id, title, content):
        return {"doc_id": doc_id, "doc_title": title,
                "chunk_index": 0, "content": content}

    def test_search_ranks_chunks(self):
        conn = _FakeConn([[
            self._chunk("d1", "习酒产品手册", "三同两不同指原料产地工艺相同"),
            self._chunk("d2", "红酒手册", "咖啡豆研磨度"),
        ]])
        hits = docs.search_docs(conn, "三同两不同", k=3, now=1000.0)
        self.assertEqual(1, len(hits))
        self.assertEqual("d1", hits[0]["doc_id"])

    def test_empty_table_returns_empty(self):
        conn = _FakeConn([[]])
        self.assertEqual([], docs.search_docs(conn, "任意", now=1000.0))

    def test_cache_avoids_reload_within_ttl(self):
        conn = _FakeConn([[self._chunk("d1", "手册", "售后退款")]])
        docs.search_docs(conn, "a", now=1000.0)
        docs.search_docs(conn, "b", now=1100.0)     # TTL 内：不重载
        self.assertEqual(1, len(conn.cursor_instance.executed))
        docs.search_docs(conn, "c", now=2000.0)     # TTL 过期：重载
        self.assertEqual(2, len(conn.cursor_instance.executed))

    def test_empty_table_not_cached(self):
        # cache_core 统一语义：空结果不写缓存，空库时每查询重试加载。
        conn = _FakeConn([[]])
        docs.search_docs(conn, "a", now=1000.0)
        docs.search_docs(conn, "b", now=1100.0)
        self.assertEqual(2, len(conn.cursor_instance.executed))

    def test_cache_rebuild_swaps_chunks_and_index_atomically(self):
        old_chunks = [self._chunk("old", "旧手册", "旧内容")]
        new_chunks = [self._chunk("new", "新手册", "新内容")]
        with mock.patch.object(docs, "_load", return_value=old_chunks):
            docs._index_for(_FakeConn(), now=1000.0)  # 播种旧快照
        with mock.patch.object(docs, "_load", return_value=new_chunks), \
             mock.patch.object(docs, "Bm25Index", return_value="new-index"):
            chunks, index = docs._index_for(_FakeConn(), now=1300.0)
        self.assertIs(new_chunks, chunks)
        self.assertEqual("new-index", index)
        cached = docs._store().get(
            f"{docs._CACHE_KEY}:{docs._version()}", now=1301.0)
        self.assertIs(new_chunks, cached[0])
        self.assertEqual("new-index", cached[1])

class ChunkTextTests(unittest.TestCase):
    """手册正文按字切片：固定窗口 + 重叠，末窗保留。"""

    def test_empty_and_blank(self):
        self.assertEqual([], sync_docs.chunk_text(""))
        self.assertEqual([], sync_docs.chunk_text("   "))

    def test_short_text_single_chunk(self):
        self.assertEqual(["你好"], sync_docs.chunk_text("你好"))

    def test_long_text_windows_with_overlap(self):
        text = "".join(str(i % 10) for i in range(25))
        chunks = sync_docs.chunk_text(text, size=10, overlap=2)
        self.assertEqual(text[:10], chunks[0])
        self.assertEqual(10, len(chunks[1]))
        self.assertEqual(chunks[0][8:10], chunks[1][:2])   # 重叠 2 字
        self.assertTrue(chunks[-1].endswith(text[-1]))

    def test_invalid_window_arguments(self):
        with self.assertRaises(ValueError):
            sync_docs.chunk_text("abc", size=5, overlap=5)
        with self.assertRaises(ValueError):
            sync_docs.chunk_text("abc", size=0)

class SyncDocsTests(unittest.TestCase):
    """kb-sync-docs 归一化 / 幂等写入 / 导入统计。"""

    def test_normalize_doc(self):
        self.assertEqual(
            ("d1", "标题", "正文"),
            sync_docs.normalize_doc(
                {"doc_id": "d1", "doc_title": "标题", "content": "正文"}
            ),
        )
        self.assertEqual(("d1", None, "正文"),
                         sync_docs.normalize_doc({"doc_id": "d1", "content": "正文"}))
        self.assertIsNone(sync_docs.normalize_doc({"doc_id": "d1"}))
        self.assertIsNone(sync_docs.normalize_doc({"content": "正文"}))
        self.assertIsNone(
            sync_docs.normalize_doc({"doc_id": "d1", "content": "   "})
        )
        self.assertIsNone(sync_docs.normalize_doc("nope"))

    def test_upsert_chunks_sql_and_params(self):
        conn = _FakeConn([1, 1])
        written = sync_docs.upsert_chunks(conn, "d1", "标题", ["片段一", "片段二"])
        self.assertEqual(2, written)
        sql, params = conn.cursor_instance.executed[0]
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertEqual(("d1", "标题", 0, "片段一"), params[:4])
        self.assertEqual(0, params[2])
        self.assertEqual("片段一", params[3])

    def test_import_docs_counts_and_skips(self):
        conn = _FakeConn([1])
        stats = sync_docs.import_docs(conn, [
            {"doc_id": "d1", "doc_title": "标题", "content": "正文"},
            {"doc_id": "d2", "content": "   "},   # 空正文 → skip
            "bad",                                 # 非 dict → skip
        ])
        self.assertEqual(1, stats["docs"])
        self.assertEqual(1, stats["chunks"])
        self.assertEqual(2, stats["skipped"])

def _block(block_type, index, **body):
    return {"blockType": block_type, "index": index, "id": f"b{index}",
            block_type: body}


class SyncWikiBlocksTests(unittest.TestCase):
    """blocks_to_markdown：钉钉 doc blocks → markdown。"""

    def test_heading_paragraph_lists(self):
        blocks = [
            _block("heading", 0, level="heading-1", text="手册"),
            _block("heading", 1, level="heading-3", text="小节"),
            _block("paragraph", 2, text="正文一段"),
            _block("unorderedList", 3, text="要点A"),
            _block("orderedList", 4, text="步骤一"),
            _block("blockquote", 5, text="引用"),
            _block("unknown", 6, rawType="hr"),
        ]
        md = sync_wiki.blocks_to_markdown(blocks)
        self.assertEqual(md, "# 手册\n\n### 小节\n\n正文一段\n\n- 要点A"
                             "\n\n1. 步骤一\n\n> 引用")

    def test_table_becomes_markdown_with_escaped_pipes(self):
        blocks = [
            _block("table", 0,
                   cells=[["月份", "目标|万元"], ["7月", "160"], ["8月", "223"]],
                   colSize=2, rowSize=3),
        ]
        md = sync_wiki.blocks_to_markdown(blocks)
        self.assertIn("| 月份 | 目标\\|万元 |", md)
        self.assertIn("|---|---|", md)
        self.assertIn("| 7月 | 160 |", md)

    def test_table_uneven_rows_padded_and_empty_dropped(self):
        blocks = [
            _block("table", 0, cells=[["a", "b", "c"], ["x"]],
                   colSize=3, rowSize=2),
            _block("table", 1, cells=[], colSize=0, rowSize=0),
        ]
        md = sync_wiki.blocks_to_markdown(blocks)
        self.assertIn("| a | b | c |", md)
        self.assertIn("| x |  |  |", md)

    def test_blocks_sorted_by_index_and_empty_text_dropped(self):
        blocks = [
            _block("paragraph", 2, text="后"),
            _block("paragraph", 0, text="先"),
            _block("paragraph", 1, text="  "),
        ]
        self.assertEqual(sync_wiki.blocks_to_markdown(blocks), "先\n\n后")

class _FakeWiki:
    """WikiClient 替身：给定文档表与 blocks。"""

    def __init__(self, docs, fail_ids=()):
        self._docs = docs          # {workspace_id: [(node_id, title)]}
        self._fail = set(fail_ids)

    def workspaces(self):
        return [(wid, f"库{wid}") for wid in self._docs]

    def iter_documents(self, workspace_id):
        return iter(self._docs.get(workspace_id, []))

    def document_blocks(self, node_id):
        if node_id in self._fail:
            raise RuntimeError("boom")
        return [_block("paragraph", 0, text=f"{node_id} 的正文内容")]

class SyncWikiRunTests(unittest.TestCase):

    class _Conn:
        def __init__(self, existing=()):
            self.deleted = []
            self.upserted = []
            self.existing = set(existing)
            self.committed = False

        def cursor(self):
            conn = self

            class _Cur:
                def execute(self, sql, params=None):
                    if sql.strip().startswith("SELECT"):
                        self._rows = [(doc_id,) for doc_id in conn.existing]
                    elif sql.strip().startswith("DELETE"):
                        conn.deleted.append(params[0])
                        self.rowcount = 3
                    else:
                        conn.upserted.append(params[:3])
                        self.rowcount = 1

                def fetchall(self):
                    return self._rows

                def close(self):
                    pass

            return _Cur()

        def commit(self):
            self.committed = True

        def rollback(self):
            pass

    def test_collect_skips_failed_and_empty(self):
        wiki = _FakeWiki(
            {"w1": [("n1", "手册A"), ("n2", "手册B")]},
            fail_ids={"n2"},
        )
        docs, stats = sync_wiki.collect_documents(wiki)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["doc_id"], "n1")
        self.assertEqual(docs[0]["doc_title"], "手册A")
        self.assertEqual(docs[0]["workspace"], "库w1")
        self.assertEqual(stats["failed"], 1)

    def test_collect_respects_workspace_allowlist(self):
        wiki = _FakeWiki({"w1": [("n1", "A")], "w2": [("n2", "B")]})
        docs, stats = sync_wiki.collect_documents(wiki, workspace_ids=["w2"])
        self.assertEqual([d["doc_id"] for d in docs], ["n2"])

    def test_run_sync_writes_manifest_and_rebuilds_chunks(self):
        import tempfile

        wiki = _FakeWiki({"w1": [("n1", "手册A")]})
        conn = self._Conn()
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "kb_docs.json")
            stats = sync_wiki.run_sync(wiki=wiki, conn=conn, out_path=out)
            payload = json.loads(Path(out).read_text(encoding="utf-8"))
        self.assertEqual(stats["docs"], 1)
        self.assertEqual(payload[0]["doc_id"], "n1")
        self.assertEqual(payload[0]["doc_title"], "手册A")
        self.assertIn("n1 的正文内容", payload[0]["content"])
        # workspace 字段不进清单（下游 sync_docs/rag 不认它）
        self.assertNotIn("workspace", payload[0])
        self.assertEqual(conn.deleted, ["n1"])
        self.assertTrue(conn.upserted)
        self.assertTrue(conn.committed)

    def test_partial_snapshot_preserves_manifest_and_chunks(self):
        import tempfile

        wiki = _FakeWiki(
            {"w1": [("n1", "手册A"), ("n2", "手册B")]}, fail_ids={"n2"})
        conn = self._Conn()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "kb_docs.json"
            out.write_text("old-manifest", encoding="utf-8")
            with self.assertRaises(sync_wiki.SourceSnapshotError):
                sync_wiki.run_sync(wiki=wiki, conn=conn, out_path=str(out))
            self.assertEqual(out.read_text(encoding="utf-8"), "old-manifest")
        self.assertEqual(conn.deleted, [])
        self.assertEqual(conn.upserted, [])
        self.assertFalse(conn.committed)

    def test_empty_snapshot_is_blocked(self):
        conn = self._Conn()
        with self.assertRaises(sync_wiki.SourceSnapshotError) as raised:
            sync_wiki.run_sync(
                wiki=_FakeWiki({}), conn=conn, out_path="unused.json")
        self.assertEqual(str(raised.exception), "empty_source")
        self.assertFalse(conn.committed)

    def test_deleted_document_is_plan_only_by_default(self):
        import tempfile

        conn = self._Conn(existing={"n1", "removed"})
        with tempfile.TemporaryDirectory() as tmp:
            stats = sync_wiki.run_sync(
                wiki=_FakeWiki({"w1": [("n1", "手册A")]}), conn=conn,
                out_path=str(Path(tmp) / "kb_docs.json"))
        self.assertEqual(stats["delete_planned"], 1)
        self.assertEqual(stats["deletion_pending"], 1)
        self.assertEqual(stats["status"], "degraded")
        self.assertNotIn("removed", conn.deleted)

    def test_deleted_document_reconciles_when_enabled_within_threshold(self):
        import tempfile

        conn = self._Conn(existing={"n1", "removed"})
        with tempfile.TemporaryDirectory() as tmp:
            stats = sync_wiki.run_sync(
                wiki=_FakeWiki({"w1": [("n1", "手册A")]}), conn=conn,
                out_path=str(Path(tmp) / "kb_docs.json"), auto_delete=True,
                delete_max_ratio=1.0)
        self.assertEqual(stats["deletion_pending"], 0)
        self.assertEqual(stats["status"], "completed")
        self.assertIn("removed", conn.deleted)
