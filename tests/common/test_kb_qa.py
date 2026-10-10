# -*- coding: utf-8 -*-
"""kb_qa 模块单元测试：路由 / 商品查询渲染 / 同步归一化 / 媒体缓存 / 问答主流程。"""

import unittest

from common.kb_qa import media
from common.kb_qa.app import KbStreamHandler, parse_conversation_allowlist
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

    def cursor(self):
        return self.cursor_instance


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
        # LIKE 特殊字符已转义
        conn2 = _FakeConn([[], [], []])
        find_products(conn2, "100%_")
        params = conn2.cursor_instance.executed[2][1]
        self.assertEqual("%100\\%\\_%", params[0])

    def test_empty_keyword_no_query(self):
        conn = _FakeConn()
        self.assertEqual([], find_products(conn, "  "))
        self.assertEqual(0, len(conn.cursor_instance.executed))

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

    def test_conversation_allowlist_parsing(self):
        self.assertEqual((), parse_conversation_allowlist(None))
        self.assertEqual(("a", "b"), parse_conversation_allowlist(" a ,,b "))


if __name__ == "__main__":
    unittest.main()
