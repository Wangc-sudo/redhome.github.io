# -*- coding: utf-8 -*-
"""对话状态层测试（2026-10-30 上下文架构调优）。

覆盖：offers 纯函数（提议识别/确认识别/确认改写）、群内双后端 offer
存取、Web 历史卫生（噪声轮/TTL）、Web 会话 offer 存取、service 确认
短路、routes 提议状态闭环与消息 meta.intent。

事故锚点：助手提议「需要我把绍兴、万科、渠道等其他区域一起拉出来做个
排名吗？」，用户回「可以」——必须执行为区域排名，不得被历史里的
「窖藏1988 质检报告」旧话题带偏。
"""

import time
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.bi_web import auth
from common.kb_qa import context as ctx_module
from common.kb_qa import offers
from common.kb_web import routes as kb_routes
from common.kb_web import service as svc_module
from common.kb_web import sessions as sess_module

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


class WebHistoryHygieneTests(unittest.TestCase):
    @staticmethod
    def _ts(now, seconds_ago):
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(now - seconds_ago))

    def test_noise_turns_filtered(self):
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "我是小易…",
             "meta": {"intent": "CHITCHAT"}},
            {"role": "user", "content": "杭州昨天销售额"},
            {"role": "assistant", "content": "102,808 元",
             "meta": {"intent": "agent"}},
        ]
        turns = svc_module.web_history_from_messages(messages)
        self.assertEqual(1, len(turns))
        self.assertEqual("杭州昨天销售额", turns[0]["q"])

    def test_stale_turns_filtered_by_ttl(self):
        now = time.time()
        messages = [
            {"role": "user", "content": "有质检报告吗1988"},
            {"role": "assistant", "content": "附件清单…",
             "ts": self._ts(now, 16 * 3600)},  # 昨天下午
            {"role": "user", "content": "杭州昨天销售额"},
            {"role": "assistant", "content": "102,808 元",
             "ts": self._ts(now, 40)},          # 刚刚
        ]
        turns = svc_module.web_history_from_messages(messages, now=now)
        self.assertEqual(1, len(turns))
        self.assertEqual("杭州昨天销售额", turns[0]["q"])

    def test_legacy_messages_without_ts_or_meta_kept(self):
        messages = [
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "旧答案"},  # 无 ts 无 meta
        ]
        turns = svc_module.web_history_from_messages(messages)
        self.assertEqual(1, len(turns))

    def test_cap_applies_after_cleaning(self):
        now = time.time()
        messages = []
        for i in range(8):
            messages.append({"role": "user", "content": f"q{i}"})
            messages.append({"role": "assistant", "content": f"a{i}",
                             "ts": self._ts(now, 30)})
        turns = svc_module.web_history_from_messages(messages, now=now)
        self.assertEqual(svc_module.DEFAULT_MAX_TURNS, len(turns))
        self.assertEqual("q2", turns[0]["q"])


class WebSessionOfferTests(unittest.TestCase):
    def test_roundtrip_and_delete_cleanup(self):
        store = sess_module.WebSessionStore()
        sid = store.create_session("u1")
        self.assertIsNone(store.pop_offer("u1", sid))
        store.set_offer("u1", sid, {"question": OFFER_QUESTION,
                                    "action": OFFER_ACTION})
        self.assertEqual(OFFER_ACTION, store.pop_offer("u1", sid)["action"])
        self.assertIsNone(store.pop_offer("u1", sid))
        store.set_offer("u1", sid, {"action": OFFER_ACTION})
        store.delete("u1", sid)
        self.assertIsNone(store.pop_offer("u1", sid))


class _AnswerConn:
    def __init__(self):
        self.committed = False

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


class _HandlerStub:
    """记录进入问答链路的任务文本，回复带一句提议。"""

    def __init__(self):
        self.texts = []

    def _answer(self, conn, text, history=None, conversation_id=None,
                meta=None, pending=None):
        self.texts.append(text)
        return "需要我把绍兴也拉出来吗？", "agent", None


def _service_with_stub(handler):
    service = svc_module.KbWebService(
        connection_factory=lambda: _AnswerConn(), log=lambda _: None)
    service._build_handler = lambda userid: handler
    return service


class WebServiceOfferTests(unittest.TestCase):
    _HISTORY = [
        {"role": "user", "content": "杭州昨天销售额"},
        {"role": "assistant", "content": "102,808 元"},
    ]

    def test_confirm_rewrites_to_offer_action(self):
        handler = _HandlerStub()
        service = _service_with_stub(handler)
        result = service.answer(
            "u1", "可以", history_messages=self._HISTORY,
            offer={"question": OFFER_QUESTION, "action": OFFER_ACTION})
        self.assertIn(OFFER_ACTION, handler.texts[0])
        self.assertIn("杭州昨天销售额", handler.texts[0])
        self.assertIn("用户已确认", handler.texts[0])
        self.assertNotEqual("可以", handler.texts[0])
        # 本轮回复的新提议透出给调用方存回
        self.assertEqual("把绍兴也拉出来", result["offer"]["action"])

    def test_substantive_text_not_rewritten(self):
        handler = _HandlerStub()
        service = _service_with_stub(handler)
        service.answer("u1", "杭州昨天销售额", history_messages=[],
                       offer={"question": OFFER_QUESTION,
                              "action": OFFER_ACTION})
        self.assertEqual("杭州昨天销售额", handler.texts[0])

    def test_confirm_without_offer_passes_through(self):
        handler = _HandlerStub()
        service = _service_with_stub(handler)
        service.answer("u1", "可以", history_messages=[])
        self.assertEqual("可以", handler.texts[0])

    def test_reject_not_rewritten(self):
        handler = _HandlerStub()
        service = _service_with_stub(handler)
        service.answer("u1", "不用", history_messages=[],
                       offer={"question": OFFER_QUESTION,
                              "action": OFFER_ACTION})
        self.assertEqual("不用", handler.texts[0])


class _OfferRouteService:
    """路由级替身：记录收到的 offer，回复末尾总带一句提议。"""

    def __init__(self):
        self.calls = []

    def answer(self, userid, content, *, history_messages, pending=None,
               offer=None, audit=None):
        self.calls.append({"content": content, "offer": offer})
        return {"reply": "需要我把绍兴也拉出来吗？", "intent": "agent",
                "meta": {"tools": [], "rounds": None, "latency_ms": 5},
                "attachments": [],
                "offer": {"question": "需要我把绍兴也拉出来吗？",
                          "action": "把绍兴也拉出来"}}


class WebRoutesOfferTests(unittest.TestCase):
    def _client(self, store, service):
        app = FastAPI()
        kb_routes.register_kb_routes(
            app, service=service, store=store, session_secret="test-secret")
        return TestClient(app)

    @staticmethod
    def _cookies():
        return {auth.SESSION_COOKIE: auth.issue_session("u1", "test-secret")}

    def test_offer_roundtrip_across_messages(self):
        store = sess_module.WebSessionStore()
        service = _OfferRouteService()
        client = self._client(store, service)
        cookies = self._cookies()
        sid = client.post("/kb/api/sessions", cookies=cookies).json()["sid"]

        resp1 = client.post(f"/kb/api/sessions/{sid}/messages",
                            json={"content": "杭州昨天销售额"},
                            cookies=cookies)
        self.assertEqual(200, resp1.status_code)
        self.assertNotIn("offer", resp1.json())  # 状态不透前端
        self.assertIsNone(service.calls[0]["offer"])

        resp2 = client.post(f"/kb/api/sessions/{sid}/messages",
                            json={"content": "可以"}, cookies=cookies)
        self.assertEqual(200, resp2.status_code)
        # 第二轮收到上一轮存回的提议（消费语义）
        self.assertEqual("把绍兴也拉出来",
                         service.calls[1]["offer"]["action"])

    def test_assistant_message_meta_records_intent(self):
        store = sess_module.WebSessionStore()
        client = self._client(store, _OfferRouteService())
        cookies = self._cookies()
        sid = client.post("/kb/api/sessions", cookies=cookies).json()["sid"]
        client.post(f"/kb/api/sessions/{sid}/messages",
                    json={"content": "杭州昨天销售额"}, cookies=cookies)
        messages = store.get_messages("u1", sid)
        self.assertEqual("agent", messages[-1]["meta"]["intent"])


if __name__ == "__main__":
    unittest.main()
