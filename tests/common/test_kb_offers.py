# -*- coding: utf-8 -*-
"""对话状态层测试（2026-10-30 上下文架构调优）。

覆盖：offers 纯函数（提议识别/确认识别/确认改写）、群内双后端 offer
存取。

事故锚点：助手提议「需要我把绍兴、万科、渠道等其他区域一起拉出来做个
排名吗？」，用户回「可以」——必须执行为区域排名，不得被历史里的
「窖藏1988 质检报告」旧话题带偏。
"""

import unittest

from common.kb_qa import context as ctx_module
from common.kb_qa import offers

OFFER_QUESTION = "需要我把绍兴、万科、渠道等其他区域一起拉出来做个排名吗？"
OFFER_ACTION = "把绍兴、万科、渠道等其他区域一起拉出来做个排名"


class OfferDetectTests(unittest.TestCase):
    def test_incident_sentence(self):
        offer = offers.detect_offer(
            "杭州区域最近一天的数据如下：\n- 销售额：102,808.00 元\n\n"
            + OFFER_QUESTION)
        self.assertEqual(OFFER_QUESTION, offer["question"])
        self.assertEqual(OFFER_ACTION, offer["action"])

    def test_variant_patterns(self):
        self.assertEqual("再查一下绍兴",
                         offers.detect_offer("要我帮你再查一下绍兴吗？")
                         ["action"])
        self.assertEqual("把数据导出",
                         offers.detect_offer("要不要把数据导出吗？")
                         ["action"])
        self.assertEqual("整理成表格",
                         offers.detect_offer("我可以整理成表格吗？")
                         ["action"])
        # 云上真实回复形态（1030 e2e 暴露）：「需要看一下…吗？」
        self.assertEqual(
            "看一下绍兴、渠道或其他区域做对比",
            offers.detect_offer("需要看一下绍兴、渠道或其他区域做对比吗？")
            ["action"])

    def test_non_offer_returns_none(self):
        self.assertIsNone(offers.detect_offer("销售额 102,808 元。"))
        self.assertIsNone(offers.detect_offer(""))
        self.assertIsNone(offers.detect_offer(None))
        # HELP 帮助文案（可以直接问我：…）不是提议
        self.assertIsNone(offers.detect_offer(
            "我是小易，可以直接问我：\n1. 商品参数与筛选\n2. 手册问答"))

    def test_only_last_line_matters(self):
        # 提议不在末尾（后面还有正文）→ 不识别
        self.assertIsNone(offers.detect_offer(
            OFFER_QUESTION + "\n以上供参考。"))


class ParseAckTests(unittest.TestCase):
    def test_confirm_words(self):
        for text in ("可以", "好的", "好", "嗯", "行", "OK", "可以。",
                     " 好的！", "是的", "继续"):
            self.assertEqual("confirm", offers.parse_ack(text), text)

    def test_reject_words(self):
        for text in ("不用", "先不要", "不需要", "不了"):
            self.assertEqual("reject", offers.parse_ack(text), text)

    def test_substantive_text_is_not_ack(self):
        for text in ("可以，顺便看下绍兴", "杭州昨天销售额", "", "第一个"):
            self.assertIsNone(offers.parse_ack(text), text)


class BuildConfirmedTextTests(unittest.TestCase):
    def test_with_last_question(self):
        text = offers.build_confirmed_text(
            {"action": OFFER_ACTION}, "杭州昨天销售额")
        self.assertIn(OFFER_ACTION, text)
        self.assertIn("杭州昨天销售额", text)
        self.assertIn("用户已确认", text)

    def test_without_last_question(self):
        self.assertEqual(OFFER_ACTION,
                         offers.build_confirmed_text({"action": OFFER_ACTION}))


class GroupOfferStoreTests(unittest.TestCase):
    def test_memory_roundtrip_consumed(self):
        store = ctx_module.ConversationStore()
        key = store.key("c1", "u1")
        self.assertIsNone(store.pop_offer(key))
        store.set_offer(key, {"question": OFFER_QUESTION,
                              "action": OFFER_ACTION})
        self.assertEqual(OFFER_ACTION, store.pop_offer(key)["action"])
        self.assertIsNone(store.pop_offer(key))  # 消费即作废

    def test_memory_ttl_expires(self):
        clock = [0.0]
        store = ctx_module.ConversationStore(now=lambda: clock[0])
        key = store.key("c1", "u1")
        store.set_offer(key, {"action": OFFER_ACTION})
        clock[0] = ctx_module.DEFAULT_TTL_SECONDS + 1
        self.assertIsNone(store.pop_offer(key))

    def test_redis_roundtrip(self):
        class _FakeRedis:
            def __init__(self):
                self.data = {}

            def setex(self, key, ttl, value):
                self.data[key] = value

            def get(self, key):
                return self.data.get(key)

            def delete(self, key):
                self.data.pop(key, None)

        client = _FakeRedis()
        store = ctx_module.RedisConversationStore(client=client)
        key = store.key("c1", "u1")
        self.assertIsNone(store.pop_offer(key))
        store.set_offer(key, {"question": OFFER_QUESTION,
                              "action": OFFER_ACTION})
        self.assertTrue(any(k.startswith("kbqa:offer:") for k in client.data))
        self.assertEqual(OFFER_ACTION, store.pop_offer(key)["action"])
        self.assertIsNone(store.pop_offer(key))

    def test_redis_failure_fail_open(self):
        class _Broken:
            def get(self, key):
                raise RuntimeError("down")

        store = ctx_module.RedisConversationStore(client=_Broken())
        self.assertIsNone(store.pop_offer(store.key("c1", "u1")))


if __name__ == "__main__":
    unittest.main()
