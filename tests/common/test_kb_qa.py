# -*- coding: utf-8 -*-
"""kb_qa 模块单元测试：路由 / 商品查询渲染 / 同步归一化 / 媒体缓存 / 问答主流程。"""

import asyncio
import json
import time
import unittest
from pathlib import Path
from unittest import mock

from dingtalk_stream import AckMessage

from common.kb_qa import bm25, docs, products, sync_docs
from common.kb_qa import llm as llm_module
from common.kb_qa import media
from common.kb_qa import agent as agent_module
from common.kb_qa import context as ctx_module
from common.kb_qa import rag_docs, sync_wiki
from common.kb_qa.app import (
    KbStreamHandler,
    _merge_doc_hits,
    callback_message_id,
    parse_conversation_allowlist,
)
from common.kb_qa.llm import LlmError
from common.kb_qa.products import (
    find_products,
    render_clarify,
    render_filter_answer,
    render_not_found,
    render_param_answer,
)
from common.kb_qa.router import (
    CHITCHAT,
    DOC_QA,
    PRODUCT_ASSET,
    PRODUCT_FILTER,
    PRODUCT_PARAM,
    classify,
    extract_keyword,
)
from common.kb_qa.sync_products import normalize_record, upsert_products


class _FakeCursor:
    """按脚本返回行的假 cursor；记录所有 execute 调用。"""

    def __init__(self, script):
        self._script = list(script)
        self.executed = []
        self.rowcount = 0

    def execute(self, sql, params=None):
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


class RouterTests(unittest.TestCase):
    def test_param_intent(self):
        intent = classify("习酒窖藏1998 箱规多少")
        self.assertEqual(PRODUCT_PARAM, intent.category)
        self.assertEqual("习酒窖藏1998", intent.keyword)

    def test_asset_intent(self):
        intent = classify("发我摘要酒的细节图")
        self.assertEqual(PRODUCT_ASSET, intent.category)
        self.assertEqual("摘要酒", intent.keyword)

    def test_filter_intent(self):
        intent = classify("有哪些酱香酒")
        self.assertEqual(PRODUCT_FILTER, intent.category)
        self.assertEqual("酱香酒", intent.keyword)

    def test_doc_intent(self):
        intent = classify("金沙摘要酒怎么跟客户介绍")
        self.assertEqual(DOC_QA, intent.category)
        self.assertEqual("金沙摘要酒 跟客户", intent.keyword)

    def test_bare_product_name_falls_to_param(self):
        intent = classify("习酒窖藏1998")
        self.assertEqual(PRODUCT_PARAM, intent.category)

    def test_chitchat(self):
        self.assertEqual(CHITCHAT, classify("你好").category)
        self.assertEqual(CHITCHAT, classify("").category)

    def test_keyword_strips_noise(self):
        self.assertEqual("摘要酒", extract_keyword("请问摘要酒的重量是多少呢？"))

    def test_keyword_strips_brackets(self):
        # 2026-10-10 验收事故：用户按指引带直角引号发送，残留括号全 miss
        self.assertEqual(
            "习酒窖藏1998 验收", extract_keyword("「习酒窖藏1998 箱规」验收")
        )


class ProductQueryTests(unittest.TestCase):
    def test_exact_barcode_short_circuits(self):
        conn = _FakeConn([[_product_row()]])
        rows = find_products(conn, "6901234567890")
        self.assertEqual(1, len(rows))
        # 命中后不再发后续查询
        self.assertEqual(1, len(conn.cursor_instance.executed))

    def test_like_fallback_after_two_misses(self):
        conn = _FakeConn([[], [], [_product_row()]])
        rows = find_products(conn, "窖藏")
        self.assertEqual(1, len(rows))
        sql = conn.cursor_instance.executed[2][0]
        self.assertIn("LIKE", sql)
        # 2026-10-10 盲区修复：包含检索覆盖品名/品牌/大类三字段
        self.assertIn("`brand` LIKE", sql)
        self.assertIn("`category` LIKE", sql)
        params = conn.cursor_instance.executed[2][1]
        self.assertEqual(3, params.count("%窖藏%"))
        # LIKE 特殊字符已转义
        conn2 = _FakeConn([[], [], []])
        find_products(conn2, "100%_")
        params2 = conn2.cursor_instance.executed[2][1]
        self.assertEqual("%100\\%\\_%", params2[0])

    def test_empty_keyword_no_query(self):
        conn = _FakeConn()
        self.assertEqual([], find_products(conn, "  "))
        self.assertEqual(0, len(conn.cursor_instance.executed))

    def test_token_fallback_after_full_miss(self):
        # 整词三段全 miss → 分词「习酒窖藏1998」第一段即中
        conn = _FakeConn([[], [], [], [_product_row()]])
        rows = find_products(conn, "习酒窖藏1998 验收")
        self.assertEqual(1, len(rows))
        self.assertEqual(4, len(conn.cursor_instance.executed))

    def test_all_tokens_miss_returns_empty(self):
        # 整词 3 次 + 分词两个各 3 次全 miss
        conn = _FakeConn([[], [], [], [], [], [], [], [], []])
        self.assertEqual([], find_products(conn, "不存在 的词"))

    def test_render_param_answer_skips_null_fields(self):
        text = render_param_answer(_product_row(gift_bag_spec=None))
        self.assertIn("习酒窖藏1998", text)
        self.assertIn("箱规：6", text)
        self.assertIn("【商品资料】", text)
        self.assertNotIn("礼袋规格", text)

    def test_render_filter_and_clarify(self):
        rows = [_product_row(), _product_row(record_id="rec2", name="习酒窖藏1988")]
        self.assertIn("匹配到 2 款", render_filter_answer(rows, "习酒"))
        clarify = render_clarify("习酒", rows)
        self.assertIn("1. 习酒窖藏1998", clarify)
        self.assertIn("2. 习酒窖藏1988", clarify)
        self.assertIn("换个说法", render_not_found("不存在"))

    def test_render_filter_truncation_hint(self):
        rows = [_product_row(record_id=f"r{i}", name=f"商品{i}") for i in range(8)]
        text = render_filter_answer(rows, "酱香酒")
        self.assertIn("仅列前 8 款", text)


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


class HandlerAnswerTests(unittest.TestCase):
    def _handler(self, script):
        conn = _FakeConn(script)
        handler = KbStreamHandler(connection_factory=lambda: conn, log=lambda _: None)
        return handler, conn

    def test_param_answer(self):
        handler, _ = self._handler([[_product_row()]])
        reply, intent, matched = handler._answer(
            handler._connection_factory(), "习酒窖藏1998 箱规"
        )
        self.assertEqual(PRODUCT_PARAM, intent)
        self.assertEqual("rec1", matched)
        self.assertIn("箱规：6", reply)

    def test_multi_match_clarifies(self):
        rows = [_product_row(), _product_row(record_id="rec2", name="习酒窖藏1988")]
        handler, _ = self._handler([rows])
        reply, intent, matched = handler._answer(
            handler._connection_factory(), "习酒 箱规"
        )
        self.assertIn("你要问的是哪一个", reply)
        self.assertIsNone(matched)

    def test_doc_fallback_and_help(self):
        handler, _ = self._handler([])
        reply, intent, _ = handler._answer(
            handler._connection_factory(), "摘要酒怎么介绍"
        )
        self.assertEqual(DOC_QA, intent)
        self.assertIn("接入中", reply)
        reply2, intent2, _ = handler._answer(
            handler._connection_factory(), "你好"
        )
        self.assertEqual(CHITCHAT, intent2)
        self.assertIn("产品资料助手", reply2)

    def test_asset_lists_attachments(self):
        row = _product_row(attachments={"细节图": [{"name": "a.jpg"}]})
        handler, _ = self._handler([[row]])
        reply, intent, matched = handler._answer(
            handler._connection_factory(), "发我习酒细节图"
        )
        self.assertEqual(PRODUCT_ASSET, intent)
        self.assertIn("细节图：1 个文件", reply)

    def test_asset_attachments_stored_as_json_string(self):
        # 2026-10-21 事故回归：DB 里 attachments 是 json.dumps 的字符串，
        # ASSET 分支 str.items() 炸 AttributeError（「质检报告」必炸）。
        row = _product_row(
            attachments=json.dumps({"质检报告": [{"name": "r.pdf"}]},
                                   ensure_ascii=False))
        handler, _ = self._handler([[row]])
        reply, intent, matched = handler._answer(
            handler._connection_factory(), "习酒质检报告"
        )
        self.assertEqual(PRODUCT_ASSET, intent)
        self.assertIn("质检报告：1 个文件", reply)

    def test_decode_json_fields(self):
        rows = [{"attachments": '{"a": [1]}', "extra": None},
                {"attachments": "not-json", "extra": {"b": 2}},
                {"attachments": None}]
        out = products._decode_json_fields(rows)
        self.assertEqual({"a": [1]}, out[0]["attachments"])
        self.assertIsNone(out[0]["extra"])
        self.assertIsNone(out[1]["attachments"])   # 坏 JSON 置 None
        self.assertEqual({"b": 2}, out[1]["extra"])  # dict 原样
        self.assertIsNone(out[2]["attachments"])

    def test_conversation_allowlist_parsing(self):
        self.assertEqual((), parse_conversation_allowlist(None))
        self.assertEqual(("a", "b"), parse_conversation_allowlist(" a ,,b "))


class _FakeHeaders:
    """模拟 dingtalk_stream.frames.Headers（对象形态，非 dict）。"""

    def __init__(self, message_id):
        self.message_id = message_id


class _FakeCallback:
    def __init__(self, message_id, text, conversation_id="cid1"):
        self.headers = _FakeHeaders(message_id)
        self.data = {
            "conversationId": conversation_id,
            "msgtype": "text",
            "text": {"content": text},
            "senderStaffId": "u1",
            "senderNick": "测试",
            "isAdmin": False,
        }


class MessageIdExtractionTests(unittest.TestCase):
    """2026-10-10 首跑事故回归：headers 是 Headers 对象不是 dict。"""

    def test_object_headers(self):
        callback = _FakeCallback("msg-1", "hi")
        self.assertEqual("msg-1", callback_message_id(callback))

    def test_dict_headers_and_missing(self):
        self.assertEqual(
            "m2", callback_message_id(
                type("C", (), {"headers": {"messageId": "m2"}})()
            )
        )
        self.assertIsNone(callback_message_id(type("C", (), {})()))
        self.assertIsNone(callback_message_id(type("C", (), {"headers": {}})()))


class ProcessEndToEndTests(unittest.TestCase):
    def test_process_answers_and_audits(self):
        import asyncio

        conn = _FakeConn([1, [_product_row()], 1])
        logs = []
        handler = KbStreamHandler(
            connection_factory=lambda: conn, log=logs.append
        )
        callback = _FakeCallback("msg-42", "习酒窖藏1998 箱规")
        status, _ = asyncio.run(handler.process(callback))
        self.assertEqual(AckMessage.STATUS_OK, status)
        self.assertTrue(conn.committed)
        executed_sql = "\n".join(sql for sql, _ in conn.cursor_instance.executed)
        # 审计占位（去重）→ 商品查询 → 审计回填，三步都发生
        self.assertIn("INSERT IGNORE INTO `kb_qa_audit`", executed_sql)
        self.assertIn("SELECT * FROM `kb_products`", executed_sql)
        self.assertIn("UPDATE `kb_qa_audit`", executed_sql)
        self.assertNotIn("answer failed", " ".join(logs))

    def test_duplicate_delivery_skipped(self):
        import asyncio

        conn = _FakeConn([0])  # INSERT IGNORE rowcount=0 = 重复投递
        handler = KbStreamHandler(
            connection_factory=lambda: conn, log=lambda _: None
        )
        callback = _FakeCallback("msg-dup", "习酒窖藏1998 箱规")
        asyncio.run(handler.process(callback))
        # 只有占位插入，无后续查询
        self.assertEqual(1, len(conn.cursor_instance.executed))


class _FakeReplyClient:
    def __init__(self, fail=False):
        self.calls = []
        self._fail = fail

    def send_group_markdown(self, robot_code, conv_id, title, text,
                            at_user_ids=None):
        if self._fail:
            raise RuntimeError("errcode visible")
        self.calls.append((robot_code, conv_id, title, text, at_user_ids))


class _FakeIncoming:
    conversation_id = "cidX=="
    sender_staff_id = "u1"


class ReplyChannelTests(unittest.TestCase):
    """2026-10-10 二次事故：回复改走 groupMessages（errcode 可见）。"""

    def test_reply_via_group_messages(self):
        client = _FakeReplyClient()
        logs = []
        handler = KbStreamHandler(
            connection_factory=lambda: _FakeConn(), log=logs.append,
            reply_client=client, robot_code="robot1",
        )
        handler._safe_reply(_FakeIncoming(), "答案")
        self.assertEqual(
            [("robot1", "cidX==", "产品资料助手", "答案", ["u1"])],
            client.calls,
        )
        self.assertIn("reply sent", " ".join(logs))

    def test_reply_failure_logged_not_raised(self):
        client = _FakeReplyClient(fail=True)
        logs = []
        handler = KbStreamHandler(
            connection_factory=lambda: _FakeConn(), log=logs.append,
            reply_client=client, robot_code="robot1",
        )
        handler._safe_reply(_FakeIncoming(), "答案")  # 不抛
        self.assertIn("reply failed", " ".join(logs))

    def test_fallback_to_session_webhook_without_client(self):
        handler = KbStreamHandler(connection_factory=lambda: _FakeConn(),
                                  log=lambda _: None)
        sent = []
        handler.reply_text = lambda text, incoming: sent.append(text)
        handler._safe_reply(_FakeIncoming(), "答案")
        self.assertEqual(["答案"], sent)


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
        conn = _FakeConn([[]])
        docs.search_docs(conn, "a", now=1000.0)
        docs.search_docs(conn, "b", now=1100.0)     # TTL 内：不重载
        self.assertEqual(1, len(conn.cursor_instance.executed))
        docs.search_docs(conn, "c", now=2000.0)     # TTL 过期：重载
        self.assertEqual(2, len(conn.cursor_instance.executed))


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


class LlmClientTests(unittest.TestCase):
    """天翼 DeepSeek 客户端：OpenAI 兼容 + fail-open 配置。"""

    def test_chat_returns_stripped_content(self):
        captured = {}

        def request(payload):
            captured.update(payload)
            return {"choices": [{"message": {"content": " 答案 "}}]}

        client = llm_module.LlmClient("http://x", "k", "m", request=request)
        self.assertEqual("答案",
                         client.chat([{"role": "user", "content": "hi"}]))
        self.assertEqual("m", captured["model"])

    def test_chat_wraps_failure_as_llm_error(self):
        def request(payload):
            raise RuntimeError("boom")

        client = llm_module.LlmClient("http://x", "k", "m", request=request)
        with self.assertRaises(LlmError):
            client.chat([{"role": "user", "content": "hi"}])

    def test_from_environment_missing_returns_none(self):
        self.assertIsNone(llm_module.from_environment({}))
        self.assertIsNone(
            llm_module.from_environment({"KB_LLM_BASE_URL": "u"})
        )
        self.assertIsNone(llm_module.from_environment(
            {"KB_LLM_BASE_URL": "u", "KB_LLM_API_KEY": "k"}
        ))

    def test_from_environment_full_builds_client(self):
        client = llm_module.from_environment({
            "KB_LLM_BASE_URL": "u", "KB_LLM_API_KEY": "k",
            "KB_LLM_MODEL": "m", "KB_LLM_TIMEOUT": "9",
        })
        self.assertIsInstance(client, llm_module.LlmClient)


class LlmCallPointTests(unittest.TestCase):
    """三个业务调用点的提示词装配与解析（无网络）。"""

    def _client(self, response):
        prompts = []

        def request(payload):
            prompts.append(payload["messages"][0]["content"])
            return {"choices": [{"message": {"content": response}}]}

        return llm_module.LlmClient("u", "k", "m", request=request), prompts

    def test_extract_query_parses_json(self):
        client, _ = self._client(
            '前缀 {"category": "DOC_QA", "keyword": " 三同两不同 "} 后缀'
        )
        self.assertEqual(("DOC_QA", "三同两不同"),
                         llm_module.extract_query(client, "三同两不同是啥"))

    def test_extract_query_bad_json_raises(self):
        client, _ = self._client("不是 JSON")
        with self.assertRaises(LlmError):
            llm_module.extract_query(client, "问题")

    def test_extract_query_missing_fields_raises(self):
        client, _ = self._client('{"category": "DOC_QA"}')
        with self.assertRaises(LlmError):
            llm_module.extract_query(client, "问题")

    def test_render_product_answer_injects_row_fields(self):
        client, prompts = self._client("口语化答案")
        rows = [_product_row(), _product_row(extra_field="不进上下文")]
        self.assertEqual(
            "口语化答案",
            llm_module.render_product_answer(client, "箱规", rows),
        )
        self.assertIn("习酒窖藏1998", prompts[0])
        self.assertIn("box_qty", prompts[0])

    def test_render_doc_answer_marks_source(self):
        client, prompts = self._client("答案")
        chunks = [{"doc_title": "习酒产品手册", "content": "三同两不同…"}]
        self.assertEqual(
            "答案", llm_module.render_doc_answer(client, "问题", chunks)
        )
        self.assertIn("习酒产品手册", prompts[0])


class _ScriptedLlm:
    """脚本化假 LLM：每次 chat 依次弹出一个响应（元素可为 LlmError 实例）。"""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.prompts = []

    def chat(self, messages, **kwargs):
        self.prompts.append(messages[0]["content"])
        value = self._responses.pop(0) if self._responses else ""
        if isinstance(value, BaseException):
            raise value
        return value


class HandlerP2Tests(unittest.TestCase):
    """app.py P2 接线：LLM 渲染 / 二次解析挽回 / DOC_QA 三态，全 fail-open。"""

    def _handler(self, script=None, *, llm=None, doc_search=None):
        conn = _FakeConn(script or [])
        handler = KbStreamHandler(
            connection_factory=lambda: conn, log=lambda _: None,
            llm=llm, doc_search=doc_search,
        )
        return handler, conn

    def test_product_llm_answer_used(self):
        llm = _ScriptedLlm("箱规是 6 瓶/箱。【商品资料】")
        handler, _ = self._handler([[_product_row()]], llm=llm)
        reply, intent, matched = handler._answer(
            handler._connection_factory(), "习酒窖藏1998 箱规"
        )
        self.assertEqual(PRODUCT_PARAM, intent)
        self.assertEqual("rec1", matched)
        self.assertIn("箱规是 6 瓶", reply)

    def test_product_llm_failure_falls_back_to_template(self):
        llm = _ScriptedLlm(LlmError("model_not_activated"))
        handler, _ = self._handler([[_product_row()]], llm=llm)
        reply, _, _ = handler._answer(
            handler._connection_factory(), "习酒窖藏1998 箱规"
        )
        self.assertIn("箱规：6", reply)
        self.assertIn("【商品资料】", reply)

    def test_reparse_recovers_product_miss(self):
        llm = _ScriptedLlm(
            '{"category": "PRODUCT_PARAM", "keyword": "习酒窖藏1998"}',
            "口语化答案",
        )
        # 首次整词 3 段 + 分词 3 段全 miss → 二次解析换关键词 → 首段即命中
        handler, _ = self._handler(
            [[], [], [], [], [], [], [_product_row()]], llm=llm
        )
        reply, _, matched = handler._answer(
            handler._connection_factory(), "习酒窖藏1998 箱规"
        )
        self.assertEqual("rec1", matched)
        self.assertEqual("口语化答案", reply)

    def test_miss_without_llm_returns_not_found(self):
        handler, _ = self._handler([[], [], []])
        reply, _, matched = handler._answer(
            handler._connection_factory(), "习酒窖藏1998 箱规"
        )
        self.assertIsNone(matched)
        self.assertIn("换个说法", reply)

    def test_reparse_failure_returns_not_found(self):
        handler, _ = self._handler([[], [], []], llm=_ScriptedLlm(LlmError("bad")))
        reply, _, matched = handler._answer(
            handler._connection_factory(), "习酒窖藏1998 箱规"
        )
        self.assertIsNone(matched)
        self.assertIn("换个说法", reply)

    def test_multi_param_still_clarifies_with_llm(self):
        rows = [_product_row(), _product_row(record_id="rec2", name="习酒窖藏1988")]
        llm = _ScriptedLlm("不该被调用")
        handler, _ = self._handler([rows], llm=llm)
        reply, _, _ = handler._answer(handler._connection_factory(), "习酒 箱规")
        self.assertIn("你要问的是哪一个", reply)
        self.assertEqual([], llm.prompts)

    def _doc_chunks(self):
        return [{"doc_id": "d1", "doc_title": "习酒产品手册",
                 "chunk_index": 0, "content": "三同两不同指的是原料、产地、工艺相同。"}]

    def test_doc_qa_with_llm(self):
        llm = _ScriptedLlm("三同两不同是指……【引自《习酒产品手册》】")
        handler, _ = self._handler(
            [], llm=llm, doc_search=lambda conn, q, k=3: self._doc_chunks()
        )
        reply, intent, _ = handler._answer(
            handler._connection_factory(), "三同两不同是啥"
        )
        self.assertEqual(DOC_QA, intent)
        self.assertIn("引自《习酒产品手册》", reply)

    def test_doc_qa_without_llm_returns_excerpt(self):
        handler, _ = self._handler(
            [], doc_search=lambda conn, q, k=3: self._doc_chunks()
        )
        reply, intent, _ = handler._answer(
            handler._connection_factory(), "三同两不同是啥"
        )
        self.assertEqual(DOC_QA, intent)
        self.assertIn("习酒产品手册", reply)
        self.assertIn("三同两不同指的是", reply)

    def test_doc_qa_llm_failure_returns_excerpt(self):
        handler, _ = self._handler(
            [], llm=_ScriptedLlm(LlmError("boom")),
            doc_search=lambda conn, q, k=3: self._doc_chunks(),
        )
        reply, _, _ = handler._answer(
            handler._connection_factory(), "三同两不同是啥"
        )
        self.assertIn("习酒产品手册", reply)

    def test_doc_qa_no_chunks_falls_back(self):
        handler, _ = self._handler([], doc_search=lambda conn, q, k=3: [])
        reply, _, _ = handler._answer(
            handler._connection_factory(), "三同两不同是啥"
        )
        self.assertIn("接入中", reply)


class VectorRecallTests(unittest.TestCase):
    """向量召回接线（2026-10-20）：合并去重 + fail-open 降级。"""

    def _handler(self, *, doc_search, vector_search):
        conn = _FakeConn([])
        return KbStreamHandler(
            connection_factory=lambda: conn, log=lambda _: None,
            doc_search=doc_search, vector_search=vector_search,
        )

    def _bm25(self):
        return [{"doc_title": "手册A", "chunk_index": 0,
                 "content": "三同两不同指的是原料相同。"}]

    def test_merge_bm25_first_then_vector_dedup(self):
        bm25 = [{"doc_title": "A", "content": "同一段内容" * 10},
                {"doc_title": "B", "content": "另一段" * 20}]
        vector = [{"doc_title": "A", "content": "同一段内容" * 10},  # 重复
                  {"doc_title": "C", "content": "向量独有的语义命中" * 8}]
        merged = _merge_doc_hits(bm25, vector)
        self.assertEqual([m["doc_title"] for m in merged], ["A", "B", "C"])

    def test_merge_respects_limit(self):
        bm25 = [{"doc_title": f"D{i}", "content": f"内容{i}"} for i in range(3)]
        vector = [{"doc_title": f"V{i}", "content": f"向量{i}"} for i in range(6)]
        self.assertEqual(len(_merge_doc_hits(bm25, vector)), 5)

    def test_handler_merges_vector_hits(self):
        handler = self._handler(
            doc_search=lambda conn, q, k=3: self._bm25(),
            vector_search=lambda q: [{"doc_title": "手册B", "chunk_index": 2,
                                      "content": "向量召回的语义切片内容"}],
        )
        reply, intent, _ = handler._answer(
            handler._connection_factory(), "三同两不同是啥")
        self.assertEqual(intent, DOC_QA)
        self.assertIn("三同两不同指的是", reply)      # BM25 摘录在
        self.assertIn("向量召回的语义切片内容", reply)  # 向量补充也在

    def test_handler_degrades_when_vector_search_raises(self):
        handler = self._handler(
            doc_search=lambda conn, q, k=3: self._bm25(),
            vector_search=lambda q: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        reply, _, _ = handler._answer(
            handler._connection_factory(), "三同两不同是啥")
        self.assertIn("三同两不同指的是", reply)  # 纯 BM25 摘录，无异常外泄

    def test_handler_vector_only_when_bm25_empty(self):
        handler = self._handler(
            doc_search=lambda conn, q, k=3: [],
            vector_search=lambda q: [{"doc_title": "手册B", "chunk_index": 0,
                                      "content": "只有向量命中的内容"}],
        )
        reply, _, _ = handler._answer(
            handler._connection_factory(), "三同两不同是啥")
        self.assertIn("只有向量命中的内容", reply)

    def test_from_environment_missing_key_returns_none(self):
        self.assertIsNone(rag_docs.from_environment({}))
        self.assertIsNone(rag_docs.from_environment(
            {"RAG_EMBED_PROVIDER": "openai", "RAG_EMBED_API_KEY": "k"}))

    def test_from_environment_ctaigw_returns_callable(self):
        search = rag_docs.from_environment(
            {"RAG_EMBED_PROVIDER": "ctaigw", "RAG_EMBED_API_KEY": "k"})
        self.assertTrue(callable(search))

    def test_index_of_parses_chunk_suffix(self):
        self.assertEqual(rag_docs._index_of("doc:abc:0007"), 7)
        self.assertEqual(rag_docs._index_of("garbage"), -1)

    def _fake_store(self, milvus, hits=None, error=None):
        """store 替身塞进 sys.modules（真 store import 依赖 pymilvus，
        本地测试环境没有——函数内延迟 import 会优先命中 sys.modules）。"""
        import sys
        import types

        def search(client, vector, *, limit=8, kind=None):
            if error:
                raise error
            return hits or []

        fake = types.SimpleNamespace(
            connect=mock.Mock(return_value=milvus),
            search=mock.Mock(side_effect=search),
            DEFAULT_PATH="/x/rag.db",
        )
        return mock.patch.dict(sys.modules,
                               {"common.rag.store": fake})

    def test_search_vector_closes_milvus_connection(self):
        # Milvus Lite LOCK 是排他 flock：kb-gateway 与 scheduler 灌库共享
        # rag.db，连接必须即用即放（2026-10-21 LOCK 冲突复盘）
        milvus = mock.Mock()
        hits = [{"id": "doc:a:0000", "score": 0.9, "kind": "text",
                 "name": "手册", "text": "内容", "image_ref": ""}]
        env = {"RAG_EMBED_PROVIDER": "ctaigw", "RAG_EMBED_API_KEY": "k",
               "RAG_EMBED_MODEL": "qwen3-vl-embedding"}
        with self._fake_store(milvus, hits=hits), \
             mock.patch.object(rag_docs.embed, "embed_text",
                               return_value=[0.1, 0.2]):
            result = rag_docs.search_vector("q", api_key="k", environ=env)
        self.assertEqual(1, len(result))
        milvus.close.assert_called_once()

    def test_search_vector_closes_even_when_search_raises(self):
        milvus = mock.Mock()
        with self._fake_store(milvus, error=RuntimeError("boom")), \
             mock.patch.object(rag_docs.embed, "embed_text",
                               return_value=[0.1]):
            with self.assertRaises(RuntimeError):
                rag_docs.search_vector(
                    "q", api_key="k",
                    environ={"RAG_EMBED_PROVIDER": "ctaigw",
                             "RAG_EMBED_API_KEY": "k"})
        milvus.close.assert_called_once()

    def test_answer_failure_logs_exception_type(self):
        # 2026-10-21 时段性故障无类型信息无法回溯：except 必须记类型
        logs = []
        conn = _FakeConn([1])
        handler = KbStreamHandler(
            connection_factory=lambda: conn, log=logs.append,
            doc_search=lambda conn, q, k=3: (_ for _ in ()).throw(
                ZeroDivisionError("boom")))
        callback = _FakeCallback("m-fail", "三同两不同是啥")
        status, _ = asyncio.run(handler.process(callback))
        self.assertEqual(AckMessage.STATUS_OK, status)
        self.assertTrue(
            any("answer failed: ZeroDivisionError" in m for m in logs))
        self.assertTrue(conn.rolled_back)
        # 位置信息（文件:行号）帮助定位复现不出的线上异常
        self.assertTrue(any(" at " in m and ".py:" in m for m in logs))


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
        def __init__(self):
            self.deleted = []
            self.upserted = []

        def cursor(self):
            conn = self

            class _Cur:
                def execute(self, sql, params=None):
                    if sql.strip().startswith("DELETE"):
                        conn.deleted.append(params[0])
                        self.rowcount = 3
                    else:
                        conn.upserted.append(params[:3])
                        self.rowcount = 1

                def close(self):
                    pass

            return _Cur()

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
        self.assertEqual(conn.deleted, ["n1"])   # 先删后灌
        self.assertTrue(conn.upserted)           # 切片已写


class ConversationStoreTests(unittest.TestCase):
    """对话上下文：存取 / TTL / 容量 / 键隔离（2026-10-21 对话层）。"""

    def _store(self, now):
        return ctx_module.ConversationStore(ttl_seconds=600, now=now)

    def test_record_and_history_roundtrip(self):
        store = self._store(time.monotonic)
        key = store.key("cid1", "u1")
        store.record(key, question="习酒箱规", answer="6 瓶/箱",
                     category="product_param", record_id="r1")
        history = store.history(key)
        self.assertEqual(1, len(history))
        self.assertEqual("习酒箱规", history[0]["q"])
        self.assertEqual("r1", history[0]["record_id"])

    def test_history_expires_after_ttl(self):
        clock = [1000.0]
        store = self._store(lambda: clock[0])
        key = store.key("cid1", "u1")
        store.record(key, question="q", answer="a")
        clock[0] += 601
        self.assertEqual([], store.history(key))

    def test_max_turns_evicts_oldest(self):
        store = ctx_module.ConversationStore(max_turns=2)
        key = store.key("cid1", "u1")
        for i in range(4):
            store.record(key, question=f"q{i}", answer=f"a{i}")
        history = store.history(key)
        self.assertEqual(2, len(history))
        self.assertEqual("q2", history[0]["q"])

    def test_keys_are_isolated_per_sender(self):
        store = self._store(time.monotonic)
        store.record(store.key("cid1", "u1"), question="q1", answer="a1")
        self.assertEqual([], store.history(store.key("cid1", "u2")))
        self.assertEqual([], store.history(store.key("cid2", "u1")))

    def test_silent_turn_not_recorded(self):
        store = self._store(time.monotonic)
        key = store.key("cid1", "u1")
        store.record(key, question="q", answer=None)
        self.assertEqual([], store.history(key))

    def test_format_history_recent_first(self):
        history = [{"q": "q1", "a": "a1"}, {"q": "q2", "a": "a2"}]
        text = ctx_module.format_history(history)
        self.assertLess(text.index("q2"), text.index("q1"))
        self.assertEqual("", ctx_module.format_history([]))


class _RecordingLlm:
    """捕获 messages 的 LLM 替身。"""

    def __init__(self, reply="答案"):
        self.reply = reply
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return self.reply


class LlmHistoryTests(unittest.TestCase):
    """LLM 三个调用点：history 进 prompt。"""

    def test_extract_query_includes_history(self):
        llm = _RecordingLlm(reply='{"category": "PRODUCT_PARAM", '
                                  '"keyword": "习酒窖藏1998"}')
        llm_module.extract_query(
            llm, "价格呢",
            history=[{"q": "习酒窖藏1998 箱规", "a": "6 瓶/箱"}])
        prompt = llm.calls[0][0]["content"]
        self.assertIn("习酒窖藏1998 箱规", prompt)   # 历史段进 prompt
        self.assertIn("价格呢", prompt)

    def test_render_doc_answer_includes_history(self):
        llm = _RecordingLlm()
        chunks = [{"doc_title": "手册", "content": "内容", "chunk_index": 0}]
        llm_module.render_doc_answer(
            llm, "那周期呢", chunks,
            history=[{"q": "茅台工艺", "a": "一年一个周期"}])
        prompt = llm.calls[0][0]["content"]
        self.assertIn("茅台工艺", prompt)


class ConversationFlowTests(unittest.TestCase):
    """process 级两轮对话：第一轮落历史，第二轮带历史（指代追问）。"""

    def _handler(self, script, llm, store):
        conn = _FakeConn(script)
        return KbStreamHandler(
            connection_factory=lambda: conn, log=lambda _: None,
            llm=llm, context_store=store,
        )

    def test_second_turn_sees_first_turn_history(self):
        store = ctx_module.ConversationStore()
        llm = _RecordingLlm(reply="箱规 6 瓶/箱。【商品资料】")
        # script：claim INSERT=1 → find_products SELECT=商品行
        handler = self._handler([1, [_product_row()]], llm, store)
        asyncio.run(handler.process(
            _FakeCallback("m1", "习酒窖藏1998 箱规")))
        key = store.key("cid1", "u1")
        self.assertEqual(1, len(store.history(key)))  # 第一轮已记录

        llm2 = _RecordingLlm(reply='{"category": "PRODUCT_PARAM", '
                                   '"keyword": "习酒窖藏1998"}')
        # 第二轮：「价格呢」规则 miss → 二次解析带历史
        handler._llm = llm2
        # claim=1 → find miss([]) → reparse 后 find 命中
        conn2 = _FakeConn([1, [], [_product_row()]])
        handler._connection_factory = lambda: conn2
        asyncio.run(handler.process(_FakeCallback("m2", "价格呢")))
        extract_prompt = llm2.calls[0][0]["content"]
        self.assertIn("习酒窖藏1998 箱规", extract_prompt)  # 历史进入二次解析

    def test_render_receives_history(self):
        store = ctx_module.ConversationStore()
        llm = _RecordingLlm(reply="第一答")
        handler = self._handler([1, [_product_row()]], llm, store)
        asyncio.run(handler.process(_FakeCallback("m1", "习酒窖藏1998 箱规")))
        conn2 = _FakeConn([1, [_product_row()]])
        handler._connection_factory = lambda: conn2
        asyncio.run(handler.process(_FakeCallback("m2", "习酒窖藏1998 价格")))
        render_prompt = llm.calls[-1][0]["content"]
        self.assertIn("习酒窖藏1998 箱规", render_prompt)  # 渲染也带历史


class AgentToolExecutorTests(unittest.TestCase):
    """白名单工具执行器（2026-10-21 工具调用 agent）。"""

    def _executor(self, script, **kw):
        return agent_module.ToolExecutor(_FakeConn(script), **kw)

    def test_unknown_tool(self):
        executor = self._executor([])
        result = json.loads(executor.execute("drop_table", {}))
        self.assertIn("unknown tool", result["error"])

    def test_daily_sales_rejects_bad_scope_and_date(self):
        # script: DISTINCT scope 返回两个合法区域
        executor = self._executor([[{"scope": "hangzhou"},
                                    {"scope": "shaoxing"}]])
        result = json.loads(executor.execute(
            "daily_sales", {"scope": "hangzhou; DROP TABLE kb_products"}))
        self.assertIn("unknown scope", result["error"])
        self.assertIn("hangzhou", result["valid_scopes"])
        executor2 = self._executor([[{"scope": "hangzhou"}]])
        result = json.loads(executor2.execute(
            "daily_sales", {"scope": "hangzhou", "date": "10-21"}))
        self.assertIn("YYYY-MM-DD", result["error"])

    def test_daily_sales_uses_placeholders(self):
        executor = self._executor([
            [{"scope": "hangzhou"}],                       # DISTINCT scope
            {"stat_date": "2026-10-20", "scope": "hangzhou",
             "sales_amount": 12.5, "dod_rate": 0.1},       # 主查询 fetchone
        ])
        result = json.loads(executor.execute(
            "daily_sales", {"scope": "hangzhou", "date": "2026-10-20"}))
        self.assertTrue(result["found"])
        self.assertEqual(12.5, result["sales_amount"])
        sql, params = executor._conn.cursor_instance.executed[-1]
        self.assertIn("`scope`=%s", sql)          # 占位符，无拼接
        self.assertEqual(("hangzhou", "2026-10-20"), params)

    def test_channel_sales_filters_and_groups(self):
        executor = self._executor([
            [{"channel": "京东"}],                          # DISTINCT channel
            [{"channel": "京东", "business_date": "2026-10-20",
              "sales_amount": 8.0}],                        # 主查询 fetchall
        ])
        result = json.loads(executor.execute(
            "channel_sales", {"channel": "京东", "date": "2026-10-20"}))
        self.assertEqual(1, result["count"])
        sql, params = executor._conn.cursor_instance.executed[-1]
        self.assertIn("`business_date`=%s", sql)
        self.assertIn("`channel`=%s", sql)
        self.assertEqual(["2026-10-20", "京东"], list(params))

    def test_search_docs_merges_bm25_and_vector(self):
        executor = self._executor(
            [],
            doc_search=lambda conn, q, k=2: [
                {"doc_title": "手册", "content": "BM25 切片"}],
            vector_search=lambda q: [
                {"doc_title": "手册", "content": "向量切片"}],
        )
        result = json.loads(executor.execute(
            "search_docs", {"query": "工艺周期"}))
        self.assertEqual(2, result["count"])
        self.assertEqual(2, len(result["chunks"]))


class AgentLoopTests(unittest.TestCase):
    """agent loop：决策→工具→作答；异常与降级。"""

    class _Executor:
        def __init__(self):
            self.calls = []

        def execute(self, name, arguments):
            self.calls.append((name, arguments))
            return json.dumps({"found": True, "sales_amount": 12.5},
                              ensure_ascii=False)

    def _llm(self, responses):
        state = {"i": 0}

        class _Llm:
            def chat_with_tools(self, messages, *, tools, **kw):
                response = responses[min(state["i"], len(responses) - 1)]
                state["i"] += 1
                return response

            def chat(self, messages, **kw):
                return "最终答案"

        return _Llm()

    def test_tool_call_then_answer(self):
        responses = [
            {"content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "daily_sales",
                             "arguments": '{"scope": "hangzhou"}'}}]},
            {"content": "杭州 10-20 销售 12.5 万。", "tool_calls": []},
        ]
        executor = self._Executor()
        answer = agent_module.run_agent(self._llm(responses),
                                        "杭州昨天卖了多少", executor=executor)
        self.assertEqual("杭州 10-20 销售 12.5 万。", answer)
        self.assertEqual([("daily_sales", {"scope": "hangzhou"})],
                         executor.calls)

    def test_direct_answer_without_tools(self):
        responses = [{"content": "你好，有什么可以帮你？", "tool_calls": []}]
        executor = self._Executor()
        answer = agent_module.run_agent(self._llm(responses), "你好",
                                        executor=executor)
        self.assertIn("你好", answer)
        self.assertEqual([], executor.calls)

    def test_llm_error_returns_none(self):
        class _ErrLlm:
            def chat_with_tools(self, messages, *, tools, **kw):
                raise LlmError("boom")

        answer = agent_module.run_agent(_ErrLlm(), "q",
                                        executor=self._Executor())
        self.assertIsNone(answer)

    def test_rounds_exhausted_falls_back_to_final_chat(self):
        responses = [
            {"content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "daily_sales", "arguments": "{}"}}]},
            {"content": None, "tool_calls": [{
                "id": "c2", "type": "function",
                "function": {"name": "channel_sales", "arguments": "{}"}}]},
        ]
        answer = agent_module.run_agent(self._llm(responses), "q",
                                        executor=self._Executor())
        self.assertEqual("最终答案", answer)

    def test_history_enters_system_prompt(self):
        captured = []

        class _Llm:
            def chat_with_tools(self, messages, *, tools, **kw):
                captured.append(messages)
                return {"content": "答", "tool_calls": []}

        agent_module.run_agent(
            _Llm(), "那杭州呢", executor=self._Executor(),
            history=[{"q": "绍兴销售", "a": "8 万"}])
        self.assertIn("绍兴销售", captured[0][0]["content"])


class DsmlParsingTests(unittest.TestCase):
    """DSML 文本工具调用（2026-10-21 用户收到乱码原文事件）。"""

    # 用户实际收到的原文样本
    SAMPLE = ('<｜｜DSML｜｜ calls> <｜｜DSML｜｜ invoke name="search_docs"> '
              '<｜｜DSML｜｜ parameter name="keyword" string="true">礼袋 尺寸'
              '</｜｜DSML｜｜ parameter> </｜｜DSML｜｜ invoke> '
              '</｜｜DSML｜｜ calls>|||')

    def test_parse_real_user_sample(self):
        calls = agent_module.parse_dsml_calls(self.SAMPLE)
        self.assertEqual(1, len(calls))
        function = calls[0]["function"]
        self.assertEqual("search_docs", function["name"])
        self.assertEqual({"keyword": "礼袋 尺寸"},
                         json.loads(function["arguments"]))
        self.assertEqual("function", calls[0]["type"])
        self.assertTrue(calls[0]["id"])

    def test_parse_multiple_invokes(self):
        content = ('<｜｜DSML｜｜ calls>'
                   '<｜｜DSML｜｜ invoke name="daily_sales">'
                   '<｜｜DSML｜｜ parameter name="scope" string="true">hz'
                   '</｜｜DSML｜｜ parameter></｜｜DSML｜｜ invoke>'
                   '<｜｜DSML｜｜ invoke name="search_products">'
                   '<｜｜DSML｜｜ parameter name="keyword" string="true">习酒'
                   '</｜｜DSML｜｜ parameter></｜｜DSML｜｜ invoke>'
                   '</｜｜DSML｜｜ calls>')
        calls = agent_module.parse_dsml_calls(content)
        self.assertEqual(["daily_sales", "search_products"],
                         [c["function"]["name"] for c in calls])

    def test_no_dsml_returns_empty(self):
        self.assertEqual([], agent_module.parse_dsml_calls("正常回答"))
        self.assertEqual([], agent_module.parse_dsml_calls(""))
        self.assertEqual([], agent_module.parse_dsml_calls(None))

    def test_agent_executes_dsml_call_instead_of_leaking_it(self):
        responses = [
            {"content": self.SAMPLE, "tool_calls": []},      # DSML 偶发
            {"content": "礼袋尺寸是 30×40cm。【引自手册】", "tool_calls": []},
        ]
        executor = AgentLoopTests._Executor()
        answer = agent_module.run_agent(
            AgentLoopTests()._llm(responses), "圆习酒礼袋尺寸",
            executor=executor)
        self.assertIn("礼袋尺寸", answer)
        # DSML 被解析成真实工具调用（name=search_docs, keyword=礼袋 尺寸）
        self.assertEqual([("search_docs", {"keyword": "礼袋 尺寸"})],
                         executor.calls)

    def test_dsml_residue_returns_none_instead_of_leaking(self):
        # 残缺 DSML（解析不出调用）→ None 降级，乱码不到用户
        responses = [
            {"content": "<｜｜DSML｜｜ calls> 残缺的标记", "tool_calls": []},
        ]
        answer = agent_module.run_agent(
            AgentLoopTests()._llm(responses), "q",
            executor=AgentLoopTests._Executor())
        self.assertIsNone(answer)


class ChatWithToolsTests(unittest.TestCase):
    """LlmClient.chat_with_tools：payload 与响应解析。"""

    def test_payload_and_parse(self):
        captured = {}

        def fake_request(payload):
            captured.update(payload)
            return {"choices": [{"message": {
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "daily_sales",
                                             "arguments": "{}"}}]}}]}

        client = llm_module.LlmClient("http://x", "k", "m",
                                      request=fake_request)
        result = client.chat_with_tools(
            [{"role": "user", "content": "q"}], tools=agent_module.TOOLS)
        self.assertIn("tools", captured)
        self.assertIsNone(result["content"])
        self.assertEqual("daily_sales",
                         result["tool_calls"][0]["function"]["name"])


class AgentAnswerRoutingTests(unittest.TestCase):
    """app._answer：规则 miss 进 agent；闲聊精确词不进。"""

    def test_rule_miss_goes_to_agent(self):
        seen = []

        def runner(conn, text, history):
            seen.append(text)
            return "杭州昨天 12.5 万。"

        conn = _FakeConn([])
        handler = KbStreamHandler(
            connection_factory=lambda: conn, log=lambda _: None,
            agent_runner=runner)
        reply, intent, _ = handler._answer(conn, "今天杭州出了多少货")
        self.assertEqual("agent", intent)
        self.assertIn("12.5", reply)
        self.assertEqual(["今天杭州出了多少货"], seen)

    def test_chitchat_exact_skips_agent(self):
        seen = []
        conn = _FakeConn([])
        handler = KbStreamHandler(
            connection_factory=lambda: conn, log=lambda _: None,
            agent_runner=lambda conn, text, history: seen.append(text))
        reply, intent, _ = handler._answer(conn, "你好")
        self.assertEqual(CHITCHAT, intent)
        self.assertEqual([], seen)

    def test_agent_none_falls_back_to_not_found(self):
        # 商品全 miss + agent 无答案 → not_found（业务问句被兜底成商品直查）
        conn = _FakeConn([])
        handler = KbStreamHandler(
            connection_factory=lambda: conn, log=lambda _: None,
            agent_runner=lambda conn, text, history: None)
        reply, intent, _ = handler._answer(conn, "今天杭州出了多少货")
        self.assertEqual(PRODUCT_PARAM, intent)
        self.assertIn("没有找到", reply)

    def test_chitchat_miss_agent_none_falls_back_to_help(self):
        # 规则完全未命中 + agent 无答案 → HELP_TEXT
        conn = _FakeConn([])
        handler = KbStreamHandler(
            connection_factory=lambda: conn, log=lambda _: None,
            agent_runner=lambda conn, text, history: None)
        reply, intent, _ = handler._answer(conn, "嗯")
        self.assertEqual(CHITCHAT, intent)


if __name__ == "__main__":
    unittest.main()
