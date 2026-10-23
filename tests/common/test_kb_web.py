# -*- coding: utf-8 -*-
"""kb_web 模块单元测试（2026-10-23 小易 Web 前端 V1 后端）。

覆盖：会话存储（索引/标题/上限/删除）、ACL（默认拒绝/fail-closed/缓存）、
history 配对、工具 offering 收窄、service 应答（含附件透出与降级）、
路由（401/会话 CRUD/发消息/附件 302）。
"""

import json
import time
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.bi_web import auth
from common.kb_web import acl as acl_module
from common.kb_web import routes as kb_routes
from common.kb_web import service as svc_module
from common.kb_web import sessions as sess_module


# ---------------------------------------------------------------------------
# 会话存储
# ---------------------------------------------------------------------------
class WebSessionStoreTests(unittest.TestCase):
    def _store(self):
        return sess_module.WebSessionStore()  # 无 URL → 进程内兜底

    def test_create_list_append_roundtrip(self):
        store = self._store()
        sid = store.create_session("u1")
        self.assertEqual([], store.get_messages("u1", sid))
        store.append("u1", sid, [
            {"role": "user", "content": "习酒窖藏1988 箱规", "ts": "t"},
            {"role": "assistant", "content": "6 瓶/箱", "ts": "t"},
        ], title_hint="习酒窖藏1988 箱规")
        messages = store.get_messages("u1", sid)
        self.assertEqual(2, len(messages))
        self.assertEqual("习酒窖藏1988 箱规", store.get_title("u1", sid))
        listing = store.list_sessions("u1")
        self.assertEqual(1, len(listing))
        self.assertEqual(sid, listing[0]["sid"])

    def test_missing_session_returns_none(self):
        store = self._store()
        self.assertIsNone(store.get_messages("u1", "nope"))

    def test_user_isolation(self):
        store = self._store()
        sid = store.create_session("u1")
        self.assertIsNone(store.get_messages("u2", sid))
        self.assertEqual([], store.list_sessions("u2"))

    def test_rename_and_delete(self):
        store = self._store()
        sid = store.create_session("u1")
        self.assertTrue(store.rename("u1", sid, "新标题"))
        self.assertEqual("新标题", store.get_title("u1", sid))
        self.assertTrue(store.delete("u1", sid))
        self.assertIsNone(store.get_messages("u1", sid))
        self.assertFalse(store.delete("u1", sid))

    def test_session_cap_evicts_oldest(self):
        store = self._store()
        keep = None
        for _ in range(sess_module.MAX_SESSIONS_PER_USER + 2):
            keep = store.create_session("u1")
        listing = store.list_sessions("u1")
        self.assertEqual(sess_module.MAX_SESSIONS_PER_USER, len(listing))
        self.assertEqual(keep, listing[0]["sid"])  # 最新在前

    def test_message_cap_trims(self):
        store = self._store()
        sid = store.create_session("u1")
        store.append("u1", sid, [
            {"role": "user", "content": f"m{i}", "ts": "t"}
            for i in range(sess_module.MAX_MESSAGES_PER_SESSION + 10)
        ])
        messages = store.get_messages("u1", sid)
        self.assertEqual(sess_module.MAX_MESSAGES_PER_SESSION, len(messages))
        self.assertEqual("m10", messages[0]["content"])  # 最旧被裁


# ---------------------------------------------------------------------------
# ACL
# ---------------------------------------------------------------------------
class _AclCursor:
    def __init__(self, row):
        self._row = row

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return self._row

    def close(self):
        pass


class _AclConn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _AclCursor(self._row)

    def close(self):
        pass


class BusinessAclTests(unittest.TestCase):
    def test_allowed_and_denied(self):
        acl = acl_module.BusinessAcl(
            lambda: _AclConn({"allow_business": 1}))
        self.assertTrue(acl.is_allowed("u1"))
        acl2 = acl_module.BusinessAcl(lambda: _AclConn(None))
        self.assertFalse(acl2.is_allowed("u2"))

    def test_default_deny_and_fail_closed(self):
        acl = acl_module.BusinessAcl(lambda: _AclConn(None))
        self.assertFalse(acl.is_allowed(""))
        broken = acl_module.BusinessAcl(
            lambda: (_ for _ in ()).throw(RuntimeError("db down")))
        self.assertFalse(broken.is_allowed("u1"))

    def test_cache_avoids_repeat_query(self):
        calls = []

        class CountingConn(_AclConn):
            def cursor(self):
                calls.append(1)
                return super().cursor()

        clock = [0.0]
        acl = acl_module.BusinessAcl(lambda: CountingConn(None),
                                     now=lambda: clock[0])
        acl.is_allowed("u1")
        acl.is_allowed("u1")
        self.assertEqual(1, len(calls))
        clock[0] += 61
        acl.is_allowed("u1")
        self.assertEqual(2, len(calls))


# ---------------------------------------------------------------------------
# service：history 配对 / 工具收窄 / 应答
# ---------------------------------------------------------------------------
class WebHistoryTests(unittest.TestCase):
    def test_pairs_and_cap(self):
        messages = [
            {"role": "user", "content": f"q{i}"}
            for i in range(8)
        ]
        # 插入 assistant 使成对
        interleaved = []
        for i in range(8):
            interleaved.append({"role": "user", "content": f"q{i}"})
            interleaved.append({"role": "assistant", "content": f"a{i}"})
        turns = svc_module.web_history_from_messages(interleaved)
        self.assertEqual(6, len(turns))  # 默认 6 轮
        self.assertEqual("q2", turns[0]["q"])
        self.assertEqual("a7", turns[-1]["a"])

    def test_trailing_unpaired_user_excluded(self):
        messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},  # 未答
        ]
        turns = svc_module.web_history_from_messages(messages)
        self.assertEqual(1, len(turns))

    def test_empty(self):
        self.assertEqual([], svc_module.web_history_from_messages(None))


class ToolsForTests(unittest.TestCase):
    def test_business_tools_filtered(self):
        full = svc_module.tools_for(True)
        narrow = svc_module.tools_for(False)
        full_names = {t["function"]["name"] for t in full}
        narrow_names = {t["function"]["name"] for t in narrow}
        self.assertIn("query_sql", full_names)
        self.assertNotIn("query_sql", narrow_names)
        self.assertNotIn("daily_sales", narrow_names)
        self.assertIn("search_products", narrow_names)
        self.assertIn("search_docs", narrow_names)


class _SvcCursor:
    """find_products 最小应答：整词命中一行。"""

    def __init__(self, script):
        self._script = list(script)

    def execute(self, sql, params=None):
        self._result = self._script.pop(0) if self._script else []

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def close(self):
        pass


class _SvcConn:
    def __init__(self, script):
        self._script = script
        self.committed = False

    def cursor(self):
        return _SvcCursor(self._script)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


class _AllowAcl:
    def __init__(self, allow):
        self._allow = allow

    def is_allowed(self, userid):
        return self._allow


class KbWebServiceTests(unittest.TestCase):
    def _row(self):
        return {
            "record_id": "rec1", "code": "SP001", "name": "习酒窖藏1998",
            "brand": "习酒", "category": "酱香", "box_qty": 6,
            "case_qty": 2, "size_mm": "280x190x90", "weight_kg": 1.35,
            "material": "陶瓷", "gift_bag_spec": "双瓶装",
            "barcode_single": "6901234567890", "barcode_case": "6901234567891",
            "attachments": None, "extra": None,
        }

    def test_product_answer_flow(self):
        conn = _SvcConn([[self._row()]])
        service = svc_module.KbWebService(
            connection_factory=lambda: conn, log=lambda _: None)
        result = service.answer("u1", "习酒窖藏1998 箱规", history_messages=[])
        self.assertIn("箱规：6", result["reply"])
        self.assertEqual("PRODUCT_PARAM", result["intent"])
        self.assertIn("latency_ms", result["meta"])
        self.assertTrue(conn.committed)

    def test_failure_degrades_gracefully(self):
        class _BrokenConn:
            def cursor(self):
                raise RuntimeError("down")

            def rollback(self):
                pass

            def close(self):
                pass

        service = svc_module.KbWebService(
            connection_factory=lambda: _BrokenConn(), log=lambda _: None)
        result = service.answer("u1", "任意问题", history_messages=[])
        self.assertIn("稍后再试", result["reply"])
        self.assertEqual("error", result["intent"])

    def test_history_passed_to_llm_render(self):
        calls = []

        class _Llm:
            def chat(self, messages, **kw):
                calls.append(messages)
                return "箱规 6 瓶/箱。【商品资料】"

        conn = _SvcConn([[self._row()]])
        service = svc_module.KbWebService(
            connection_factory=lambda: conn, llm=_Llm(), log=lambda _: None)
        history = [{"role": "user", "content": "摘要酒 箱规"},
                   {"role": "assistant", "content": "4 瓶/箱"}]
        service.answer("u1", "习酒窖藏1998 箱规", history_messages=history)
        prompt = calls[0][0]["content"]
        self.assertIn("摘要酒 箱规", prompt)  # Web 历史进入渲染


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
class _StubService:
    def __init__(self):
        self.calls = []

    def answer(self, userid, content, *, history_messages):
        self.calls.append((userid, content))
        return {"reply": "答案", "intent": "product_param",
                "meta": {"tools": [], "rounds": None, "latency_ms": 5},
                "attachments": []}


def _make_client(store=None, service=None, record_fetcher=None,
                 auth_client=None, secret="test-secret"):
    app = FastAPI()
    kb_routes.register_kb_routes(
        app,
        service=service or _StubService(),
        store=store or sess_module.WebSessionStore(),
        session_secret=secret,
        auth_client=auth_client,
        record_fetcher=record_fetcher,
    )
    return TestClient(app)


def _login_cookie(userid, secret="test-secret"):
    return {auth.SESSION_COOKIE: auth.issue_session(userid, secret)}


class KbRoutesTests(unittest.TestCase):
    def test_requires_login(self):
        client = _make_client()
        self.assertEqual(401, client.get("/kb/api/bootstrap").status_code)
        self.assertEqual(401, client.get("/kb/api/sessions").status_code)

    def test_bootstrap_with_session(self):
        client = _make_client()
        resp = client.get("/kb/api/bootstrap", cookies=_login_cookie("u1"))
        self.assertEqual(200, resp.status_code)
        self.assertEqual("u1", resp.json()["userid"])

    def test_session_lifecycle_and_message(self):
        store = sess_module.WebSessionStore()
        service = _StubService()
        client = _make_client(store=store, service=service)
        cookies = _login_cookie("u1")

        sid = client.post("/kb/api/sessions", cookies=cookies).json()["sid"]
        resp = client.post(f"/kb/api/sessions/{sid}/messages",
                           json={"content": "习酒窖藏1998 箱规"},
                           cookies=cookies)
        self.assertEqual(200, resp.status_code)
        self.assertEqual("答案", resp.json()["reply"])
        self.assertEqual(("u1", "习酒窖藏1998 箱规"), service.calls[0])

        detail = client.get(f"/kb/api/sessions/{sid}",
                            cookies=cookies).json()
        self.assertEqual(2, len(detail["messages"]))
        self.assertEqual("习酒窖藏1998 箱规", detail["title"])

        listing = client.get("/kb/api/sessions", cookies=cookies).json()
        self.assertEqual(1, len(listing["sessions"]))

        self.assertEqual(200, client.delete(f"/kb/api/sessions/{sid}",
                                            cookies=cookies).status_code)
        self.assertEqual(404, client.get(f"/kb/api/sessions/{sid}",
                                         cookies=cookies).status_code)

    def test_other_users_session_404(self):
        store = sess_module.WebSessionStore()
        client = _make_client(store=store)
        sid = client.post("/kb/api/sessions",
                          cookies=_login_cookie("u1")).json()["sid"]
        self.assertEqual(404, client.get(
            f"/kb/api/sessions/{sid}", cookies=_login_cookie("u2")).status_code)

    def test_regenerate_replays_last_user_message(self):
        store = sess_module.WebSessionStore()
        service = _StubService()
        client = _make_client(store=store, service=service)
        cookies = _login_cookie("u1")
        sid = client.post("/kb/api/sessions", cookies=cookies).json()["sid"]
        client.post(f"/kb/api/sessions/{sid}/messages",
                    json={"content": "问一"}, cookies=cookies)
        resp = client.post(f"/kb/api/sessions/{sid}/regenerate",
                           cookies=cookies)
        self.assertEqual(200, resp.status_code)
        self.assertEqual(("u1", "问一"), service.calls[-1])

    def test_empty_content_400(self):
        client = _make_client()
        cookies = _login_cookie("u1")
        sid = client.post("/kb/api/sessions", cookies=cookies).json()["sid"]
        resp = client.post(f"/kb/api/sessions/{sid}/messages",
                           json={"content": "  "}, cookies=cookies)
        self.assertEqual(400, resp.status_code)

    def test_attachment_redirects_to_fresh_url(self):
        fetcher = lambda rid: {"fields": {"细节图": [
            {"url": "https://signed.example/a.jpg", "filename": "a.jpg"}]}}
        client = _make_client(record_fetcher=fetcher)
        resp = client.get("/kb/api/attachments/rec1/细节图/0",
                          cookies=_login_cookie("u1"), follow_redirects=False)
        self.assertEqual(302, resp.status_code)
        self.assertEqual("https://signed.example/a.jpg",
                         resp.headers["location"])

    def test_attachment_404_and_unavailable(self):
        client = _make_client(record_fetcher=lambda rid: {"fields": {}})
        self.assertEqual(404, client.get(
            "/kb/api/attachments/rec1/细节图/0",
            cookies=_login_cookie("u1")).status_code)
        client2 = _make_client(record_fetcher=None)
        self.assertEqual(503, client2.get(
            "/kb/api/attachments/rec1/细节图/0",
            cookies=_login_cookie("u1")).status_code)

    def test_auth_dingtalk_exchange(self):
        class _AuthClient:
            def exchange_auth_code(self, code):
                if code != "good":
                    raise auth.AuthError("bad")
                return "u9"

        client = _make_client(auth_client=_AuthClient())
        bad = client.post("/kb/api/auth/dingtalk", json={"authCode": "bad"})
        self.assertEqual(401, bad.status_code)
        good = client.post("/kb/api/auth/dingtalk", json={"authCode": "good"})
        self.assertEqual(200, good.status_code)
        self.assertIn(auth.SESSION_COOKIE, good.cookies)


if __name__ == "__main__":
    unittest.main()
