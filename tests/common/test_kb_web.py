# -*- coding: utf-8 -*-
"""kb_web 模块单元测试（2026-10-23 小易 Web 前端 V1 后端）。

覆盖：会话存储（索引/标题/上限/删除）、ACL（默认拒绝/fail-closed/缓存）、
history 配对、工具 offering 收窄、service 应答（含附件透出与降级）、
路由（401/会话 CRUD/发消息/附件 302）。
"""

import json
import time
import unittest
import urllib.parse

from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.bi_web import auth
from common.kb_web import acl as acl_module
from common.kb_web import qr_login as qr_module
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

    def test_pending_roundtrip_and_consumed(self):
        """clarify 候选（2026-10-23）：存→取→消费即作废。"""
        store = self._store()
        sid = store.create_session("u1")
        self.assertIsNone(store.pop_pending("u1", sid))
        store.set_pending("u1", sid, [{"record_id": "r1", "name": "习酒"}])
        self.assertEqual(1, len(store.pop_pending("u1", sid)))
        self.assertIsNone(store.pop_pending("u1", sid))

    def test_delete_session_clears_pending(self):
        store = self._store()
        sid = store.create_session("u1")
        store.set_pending("u1", sid, [{"record_id": "r1", "name": "习酒"}])
        store.delete("u1", sid)
        self.assertIsNone(store.pop_pending("u1", sid))


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

    def test_pending_choice_hits_candidate(self):
        """clarify Web 接线（2026-10-23）：候选 +「第一个」→ 参数卡，
        候选回填供改选。"""
        conn = _SvcConn([[], [self._row()]])   # 编码精确 miss → 品名精确命中
        service = svc_module.KbWebService(
            connection_factory=lambda: conn, log=lambda _: None)
        pending = [{"record_id": "rec1", "name": "习酒窖藏1998",
                    "code": "SP001"}]
        result = service.answer("u1", "第一个", history_messages=[],
                                pending=pending)
        self.assertIn("箱规：6", result["reply"])
        self.assertEqual(1, len(result["pending_candidates"]))

    def test_clarify_returns_pending_candidates(self):
        conn = _SvcConn([[
            self._row(),
            {**self._row(), "record_id": "rec2", "name": "习酒窖藏1988"},
        ]])
        service = svc_module.KbWebService(
            connection_factory=lambda: conn, log=lambda _: None)
        result = service.answer("u1", "习酒窖藏", history_messages=[])
        self.assertIn("你要问的是哪一个", result["reply"])
        self.assertEqual(2, len(result["pending_candidates"]))

    def test_business_query_open_for_all(self):
        """2026-10-24 用户裁决「解除业务限制」：无白名单也走 agent 链路
        （无 LLM 时落 agent 不可用兜底，绝不是「通道未开放」话术）。"""
        conn = _SvcConn([])
        service = svc_module.KbWebService(
            connection_factory=lambda: conn,
            acl=_AllowAcl(False), log=lambda _: None)
        result = service.answer("u1", "杭州昨天销售额", history_messages=[])
        self.assertEqual("SALES_QUERY", result["intent"])
        self.assertNotIn("还没开放", result["reply"])

    def test_business_query_not_blocked_for_authorized(self):
        """白名单用户不受阻（agent 链路灯照旧）。"""
        conn = _SvcConn([])
        service = svc_module.KbWebService(
            connection_factory=lambda: conn,
            acl=_AllowAcl(True), log=lambda _: None)
        result = service.answer("u1", "杭州昨天销售额", history_messages=[])
        # 无 LLM 时 SALES_QUERY 落 agent 不可用兜底（不是未开放话术）
        self.assertNotIn("还没开放", result["reply"])

    def test_product_query_not_blocked_for_unauthorized(self):
        """非白名单用户的商品问句不受影响（商品工具不在收窄面）。"""
        conn = _SvcConn([[self._row()]])
        service = svc_module.KbWebService(
            connection_factory=lambda: conn,
            acl=_AllowAcl(False), log=lambda _: None)
        result = service.answer("u1", "习酒窖藏1998 箱规", history_messages=[])
        self.assertIn("箱规：6", result["reply"])

    def test_meta_tokens_and_history_rounds(self):
        """2026-10-26 上下文/token 可见性：meta 带 tokens 差值与轮数。"""
        class _UsageLlm:
            def __init__(self):
                self._usage = {"prompt_tokens": 100, "completion_tokens": 10}

            def usage_snapshot(self):
                return dict(self._usage)

            def chat(self, messages, **kw):
                self._usage["prompt_tokens"] += 60
                self._usage["completion_tokens"] += 20
                return "箱规 6 瓶/箱。【商品资料】"

        conn = _SvcConn([[self._row()]])
        service = svc_module.KbWebService(
            connection_factory=lambda: conn, llm=_UsageLlm(), log=lambda _: None)
        history = [{"role": "user", "content": "摘要酒 箱规"},
                   {"role": "assistant", "content": "4 瓶/箱"}]
        result = service.answer("u1", "习酒窖藏1998 箱规",
                                history_messages=history)
        self.assertEqual({"prompt": 60, "completion": 20},
                         result["meta"]["tokens"])
        self.assertEqual(1, result["meta"]["history_rounds"])
        self.assertFalse(result["meta"]["history_truncated"])

    def test_meta_history_truncated_over_max_turns(self):
        """历史超过上限：history_truncated=True、轮数封顶、无 llm 时
        tokens=None。"""
        conn = _SvcConn([[self._row()]])
        service = svc_module.KbWebService(
            connection_factory=lambda: conn, log=lambda _: None)
        history = []
        for i in range(8):
            history.append({"role": "user", "content": f"问{i}"})
            history.append({"role": "assistant", "content": f"答{i}"})
        result = service.answer("u1", "习酒窖藏1998 箱规",
                                history_messages=history)
        self.assertEqual(svc_module.DEFAULT_MAX_TURNS,
                         result["meta"]["history_rounds"])
        self.assertTrue(result["meta"]["history_truncated"])
        self.assertIsNone(result["meta"]["tokens"])


class _AuditConn:
    """审计写入记录器：接住 INSERT 并记住参数。"""

    def __init__(self):
        self.inserts = []
        self.committed = False

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.inserts.append((sql, params))

    def commit(self):
        self.committed = True

    def close(self):
        pass


class KbWebAuditTests(unittest.TestCase):
    """Web 链路问答落 kb_qa_audit（2026-10-26，与群内同表同口径）。"""

    def _row(self):
        return {
            "record_id": "rec1", "code": "SP001", "name": "习酒窖藏1998",
            "brand": "习酒", "category": "酱香", "box_qty": 6,
            "case_qty": 2, "size_mm": "280x190x90", "weight_kg": 1.35,
            "material": "陶瓷", "gift_bag_spec": "双瓶装",
            "barcode_single": "6901234567890", "barcode_case": "6901234567891",
            "attachments": None, "extra": None,
        }

    def test_audit_written_on_success(self):
        answer_conn = _SvcConn([[self._row()]])
        audit_conn = _AuditConn()
        conns = iter([answer_conn, audit_conn])
        service = svc_module.KbWebService(
            connection_factory=lambda: next(conns), log=lambda _: None)
        result = service.answer("u1", "习酒窖藏1998 箱规",
                                history_messages=[], audit={"sid": "s1"})
        self.assertEqual("PRODUCT_PARAM", result["intent"])
        self.assertEqual(1, len(audit_conn.inserts))
        sql, params = audit_conn.inserts[0]
        self.assertIn("kb_qa_audit", sql)
        self.assertTrue(params[0].startswith("web:s1:"))  # msg_id
        self.assertEqual("web:s1", params[1])             # conversation_id
        self.assertEqual("u1", params[2])                 # sender_uid
        self.assertIn("PRODUCT_PARAM", params)            # intent
        self.assertIn(0, params)                          # failed=0
        self.assertTrue(audit_conn.committed)

    def test_audit_skipped_without_context(self):
        made = []

        def factory():
            conn = _SvcConn([[self._row()]])
            made.append(conn)
            return conn

        service = svc_module.KbWebService(
            connection_factory=factory, log=lambda _: None)
        service.answer("u1", "习酒窖藏1998 箱规", history_messages=[])
        # 只有问答主连接，无审计连接
        self.assertEqual(1, len(made))

    def test_audit_marks_failed_on_error(self):
        class _BrokenConn:
            def cursor(self):
                raise RuntimeError("down")

            def rollback(self):
                pass

            def close(self):
                pass

        audit_conn = _AuditConn()
        conns = iter([_BrokenConn(), audit_conn])
        service = svc_module.KbWebService(
            connection_factory=lambda: next(conns), log=lambda _: None)
        result = service.answer("u1", "任意问题", history_messages=[],
                                audit={"sid": "s1"})
        self.assertEqual("error", result["intent"])
        self.assertEqual(1, len(audit_conn.inserts))
        _sql, params = audit_conn.inserts[0]
        self.assertEqual(1, params[-2])       # failed=1
        self.assertIsNone(params[11])         # answer_excerpt NULL

    def test_audit_write_error_never_breaks_answer(self):
        answer_conn = _SvcConn([[self._row()]])

        class _AuditBroken:
            def cursor(self):
                raise RuntimeError("audit db down")

        conns = iter([answer_conn, _AuditBroken()])
        service = svc_module.KbWebService(
            connection_factory=lambda: next(conns), log=lambda _: None)
        result = service.answer("u1", "习酒窖藏1998 箱规",
                                history_messages=[], audit={"sid": "s1"})
        self.assertIn("箱规：6", result["reply"])


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
class _StubService:
    def __init__(self):
        self.calls = []

    def answer(self, userid, content, *, history_messages, pending=None,
               audit=None):
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

    def test_pending_roundtrip_across_messages(self):
        """clarify 闭环路由链路（2026-10-23）：第一轮存候选、第二轮收到、
        响应不透出状态键。"""
        seen = []

        class _ClarifyStub:
            def answer(self, userid, content, *, history_messages,
                       pending=None, audit=None):
                seen.append(pending)
                return {"reply": "r", "intent": "PRODUCT_PARAM",
                        "meta": {"tools": [], "rounds": None,
                                 "latency_ms": 1},
                        "attachments": [],
                        "pending_candidates": [
                            {"record_id": "r1", "name": "A", "code": ""}]}

        store = sess_module.WebSessionStore()
        client = _make_client(store=store, service=_ClarifyStub())
        cookies = _login_cookie("u1")
        sid = client.post("/kb/api/sessions", cookies=cookies).json()["sid"]

        resp1 = client.post(f"/kb/api/sessions/{sid}/messages",
                            json={"content": "习酒窖藏"}, cookies=cookies)
        self.assertEqual(200, resp1.status_code)
        self.assertNotIn("pending_candidates", resp1.json())
        self.assertIsNone(seen[0])  # 第一轮无 pending

        resp2 = client.post(f"/kb/api/sessions/{sid}/messages",
                            json={"content": "第一个"}, cookies=cookies)
        self.assertEqual(200, resp2.status_code)
        self.assertNotIn("pending_candidates", resp2.json())
        # 第二轮拿到上一轮存的候选
        self.assertEqual(1, len(seen[1]))
        self.assertEqual("r1", seen[1][0]["record_id"])

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


# ---------------------------------------------------------------------------
# 发送幂等（2026-10-26）：在飞锁 + 结果窗
# ---------------------------------------------------------------------------
class IdemStoreTests(unittest.TestCase):
    def test_acquire_complete_roundtrip(self):
        store = sess_module.WebSessionStore()
        self.assertTrue(store.acquire_idem("u1", "s1", "问一"))
        # 锁在飞：同键再抢失败
        self.assertFalse(store.acquire_idem("u1", "s1", "问一"))
        self.assertIsNone(store.get_idem_done("u1", "s1", "问一"))
        store.complete_idem("u1", "s1", "问一", {"reply": "答案"})
        # 答完：锁已放（可再抢），结果窗命中
        self.assertEqual({"reply": "答案"},
                         store.get_idem_done("u1", "s1", "问一"))
        self.assertTrue(store.acquire_idem("u1", "s1", "问一"))

    def test_release_allows_retry(self):
        store = sess_module.WebSessionStore()
        self.assertTrue(store.acquire_idem("u1", "s1", "问一"))
        store.release_idem("u1", "s1", "问一")
        self.assertTrue(store.acquire_idem("u1", "s1", "问一"))
        self.assertIsNone(store.get_idem_done("u1", "s1", "问一"))

    def test_key_isolation_by_sid_and_content(self):
        store = sess_module.WebSessionStore()
        self.assertTrue(store.acquire_idem("u1", "s1", "问一"))
        # 不同会话 / 不同内容互不冲突
        self.assertTrue(store.acquire_idem("u1", "s2", "问一"))
        self.assertTrue(store.acquire_idem("u1", "s1", "问二"))

    def test_backend_error_fails_open(self):
        class _BrokenStore(sess_module.WebSessionStore):
            def _kv(self):
                raise RuntimeError("redis down")

        store = _BrokenStore()
        self.assertTrue(store.acquire_idem("u1", "s1", "问一"))
        self.assertIsNone(store.get_idem_done("u1", "s1", "问一"))
        # 写/删路径异常均被吞（不抛）
        store.complete_idem("u1", "s1", "问一", {"reply": "x"})
        store.release_idem("u1", "s1", "问一")


class IdemRouteTests(unittest.TestCase):
    def _setup(self):
        store = sess_module.WebSessionStore()
        service = _StubService()
        client = _make_client(store=store, service=service)
        cookies = _login_cookie("u1")
        sid = client.post("/kb/api/sessions", cookies=cookies).json()["sid"]
        return store, service, client, cookies, sid

    def test_duplicate_send_returns_cached_without_rerun(self):
        store, service, client, cookies, sid = self._setup()
        body = {"content": "习酒窖藏1998 箱规"}
        resp1 = client.post(f"/kb/api/sessions/{sid}/messages",
                            json=body, cookies=cookies)
        self.assertEqual(200, resp1.status_code)
        resp2 = client.post(f"/kb/api/sessions/{sid}/messages",
                            json=body, cookies=cookies)
        self.assertEqual(200, resp2.status_code)
        self.assertEqual(resp1.json(), resp2.json())
        # 重发不重跑 agent、不重复写会话
        self.assertEqual(1, len(service.calls))
        detail = client.get(f"/kb/api/sessions/{sid}", cookies=cookies).json()
        self.assertEqual(2, len(detail["messages"]))

    def test_different_content_passes_through(self):
        store, service, client, cookies, sid = self._setup()
        client.post(f"/kb/api/sessions/{sid}/messages",
                    json={"content": "问一"}, cookies=cookies)
        client.post(f"/kb/api/sessions/{sid}/messages",
                    json={"content": "问二"}, cookies=cookies)
        self.assertEqual(2, len(service.calls))
        detail = client.get(f"/kb/api/sessions/{sid}", cookies=cookies).json()
        self.assertEqual(4, len(detail["messages"]))

    def test_in_flight_without_done_returns_409(self):
        store, service, client, cookies, sid = self._setup()
        # 模拟同内容请求在飞（锁被另一请求持有且迟迟未答完）
        self.assertTrue(store.acquire_idem("u1", sid, "问一"))
        old_wait, old_interval = (kb_routes.IDEM_WAIT_SECONDS,
                                  kb_routes.IDEM_WAIT_INTERVAL)
        kb_routes.IDEM_WAIT_SECONDS = 0.01
        kb_routes.IDEM_WAIT_INTERVAL = 0
        try:
            resp = client.post(f"/kb/api/sessions/{sid}/messages",
                               json={"content": "问一"}, cookies=cookies)
        finally:
            kb_routes.IDEM_WAIT_SECONDS = old_wait
            kb_routes.IDEM_WAIT_INTERVAL = old_interval
        self.assertEqual(409, resp.status_code)
        self.assertEqual(0, len(service.calls))
        detail = client.get(f"/kb/api/sessions/{sid}", cookies=cookies).json()
        self.assertEqual(0, len(detail["messages"]))

    def test_in_flight_done_during_wait_returns_cached(self):
        store, service, client, cookies, sid = self._setup()
        # 锁被另一请求持有；首次查窗为空、等待期间结果窗出现
        self.assertTrue(store.acquire_idem("u1", sid, "问一"))
        calls = {"n": 0}

        def late_done(u, s, c):
            calls["n"] += 1
            return None if calls["n"] == 1 else {"reply": "缓存答案"}

        store.get_idem_done = late_done
        old_wait, old_interval = (kb_routes.IDEM_WAIT_SECONDS,
                                  kb_routes.IDEM_WAIT_INTERVAL)
        kb_routes.IDEM_WAIT_SECONDS = 5
        kb_routes.IDEM_WAIT_INTERVAL = 0
        try:
            resp = client.post(f"/kb/api/sessions/{sid}/messages",
                               json={"content": "问一"}, cookies=cookies)
        finally:
            kb_routes.IDEM_WAIT_SECONDS = old_wait
            kb_routes.IDEM_WAIT_INTERVAL = old_interval
        self.assertEqual(200, resp.status_code)
        self.assertEqual("缓存答案", resp.json()["reply"])
        self.assertEqual(0, len(service.calls))


# ---------------------------------------------------------------------------
# 扫码登录（2026-10-26，钉钉 OAuth2.0 授权链）
# ---------------------------------------------------------------------------
class QrLoginClientTests(unittest.TestCase):
    def _client(self, responses):
        calls = []

        def fake(url, method="GET", body=None, headers=None, timeout=15):
            calls.append({"url": url, "method": method,
                          "body": body, "headers": headers})
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        return (qr_module.DingTalkQrLoginClient("ak", "sk",
                                                request_json=fake), calls)

    def test_exchange_success(self):
        client, calls = self._client([
            {"accessToken": "user-token"},
            {"unionId": "union-1"},
            {"errcode": 0, "result": {"userid": "u100"}},
        ])
        userid, union_id = client.exchange_qr_code("code-1")
        self.assertEqual(("u100", "union-1"), (userid, union_id))
        # 三段外呼形态：用户 token → users/me（个人 token 头）→ getbyunionid
        self.assertEqual("POST", calls[0]["method"])
        self.assertIn("/v1.0/oauth2/userAccessToken", calls[0]["url"])
        self.assertEqual("authorization_code",
                         calls[0]["body"]["grantType"])
        self.assertEqual({"x-acs-dingtalk-access-token": "user-token"},
                         calls[1]["headers"])
        self.assertIn("/topapi/user/getbyunionid", calls[2]["url"])
        self.assertIn("access_token=test-access-token", calls[2]["url"])

    def test_blank_code_raises(self):
        client, calls = self._client([])
        with self.assertRaises(auth.AuthError):
            client.exchange_qr_code("")
        self.assertEqual([], calls)  # 不外呼

    def test_user_token_missing_raises(self):
        client, _calls = self._client([{}])
        with self.assertRaises(auth.AuthError):
            client.exchange_qr_code("code-1")

    def test_transport_error_retried_then_raises(self):
        client, calls = self._client([RuntimeError("boom"),
                                      RuntimeError("boom")])
        with self.assertRaises(auth.AuthError):
            client.exchange_qr_code("code-1")
        self.assertEqual(2, len(calls))  # 3s×1 重试纪律

    def test_union_id_missing_raises(self):
        client, _calls = self._client([
            {"accessToken": "user-token"},
            {"nick": "某用户"},
        ])
        with self.assertRaises(auth.AuthError):
            client.exchange_qr_code("code-1")

    def test_getbyunionid_errcode_raises(self):
        client, _calls = self._client([
            {"accessToken": "user-token"},
            {"unionId": "union-1"},
            {"errcode": 60121, "errmsg": "no such user"},
        ])
        with self.assertRaises(auth.AuthError):
            client.exchange_qr_code("code-1")

    def test_error_messages_are_generic(self):
        client, _calls = self._client([RuntimeError("boom"),
                                       RuntimeError("boom")])
        try:
            client.exchange_qr_code("secret-code")
            self.fail("expected AuthError")
        except auth.AuthError as exc:
            self.assertNotIn("secret-code", str(exc))
            self.assertNotIn("sk", str(exc))

    def test_failure_logs_step_and_diagcode(self):
        """分步诊断日志（2026-10-26 排障驱动）：步骤名+异常类型进日志。"""
        client, _calls = self._client([RuntimeError("boom"),
                                       RuntimeError("boom")])
        with self.assertLogs("common.kb_web.qr_login", level="WARNING") as cm:
            with self.assertRaises(auth.AuthError):
                client.exchange_qr_code("code-1")
        self.assertIn("userAccessToken", cm.output[0])
        self.assertIn("RuntimeError", cm.output[0])
        self.assertNotIn("code-1", cm.output[0])  # 授权码不进日志

    def test_getbyunionid_errcode_logged(self):
        client, _calls = self._client([
            {"accessToken": "user-token"},
            {"unionId": "union-1"},
            {"errcode": 60121, "errmsg": "no such user"},
        ])
        with self.assertLogs("common.kb_web.qr_login", level="WARNING") as cm:
            with self.assertRaises(auth.AuthError):
                client.exchange_qr_code("code-1")
        self.assertIn("getbyunionid", cm.output[0])
        self.assertIn("60121", cm.output[0])


class OAuthStateStoreTests(unittest.TestCase):
    def test_issue_consume_roundtrip(self):
        store = qr_module.OAuthStateStore()
        state = store.issue()
        self.assertTrue(state)
        self.assertTrue(store.consume(state))
        self.assertFalse(store.consume(state))  # 消费即作废

    def test_unknown_or_blank_state_rejected(self):
        store = qr_module.OAuthStateStore()
        self.assertFalse(store.consume("never-issued"))
        self.assertFalse(store.consume(""))
        self.assertFalse(store.consume(None))

    def test_backend_error_fail_closed_on_consume(self):
        class _BrokenStore(qr_module.OAuthStateStore):
            def _kv(self):
                raise RuntimeError("redis down")

        store = _BrokenStore()
        state = store.issue()  # 签发 fail-open
        self.assertTrue(state)
        self.assertFalse(store.consume(state))  # 消费 fail-closed

    def test_build_login_url_encodes_params(self):
        url = qr_module.build_login_url(
            "ak", "https://dt.yyhlzj.com/kb/api/auth/dingtalk/callback",
            "state-1")
        self.assertTrue(url.startswith(qr_module.LOGIN_AUTHORIZE_URL + "?"))
        query = dict(urllib.parse.parse_qsl(
            urllib.parse.urlparse(url).query))
        self.assertEqual(
            "https://dt.yyhlzj.com/kb/api/auth/dingtalk/callback",
            query["redirect_uri"])
        self.assertEqual("code", query["response_type"])
        self.assertEqual("ak", query["client_id"])
        self.assertEqual("openid", query["scope"])
        self.assertEqual("state-1", query["state"])
        self.assertEqual("consent", query["prompt"])


class _StubQrClient:
    def __init__(self, userid="u100", fail=False):
        self.app_key = "ak"
        self._userid = userid
        self._fail = fail
        self.calls = []

    def exchange_qr_code(self, code):
        self.calls.append(code)
        if self._fail:
            raise auth.AuthError("dingtalk qr login failed")
        return self._userid, "union-1"


class QrLoginRouteTests(unittest.TestCase):
    _REDIRECT_URI = "https://dt.yyhlzj.com/kb/api/auth/dingtalk/callback"

    def _app(self, *, qr_client=None, state_store=None,
             qr_redirect_uri=None, member_checker=None):
        app = FastAPI()
        kb_routes.register_kb_routes(
            app,
            service=_StubService(),
            store=sess_module.WebSessionStore(),
            session_secret="test-secret",
            qr_client=qr_client,
            state_store=state_store,
            qr_redirect_uri=qr_redirect_uri,
            member_checker=member_checker,
        )
        return app

    def _enabled(self, qr_client=None, member_checker=None):
        """装配扫码登录全依赖的 TestClient，返回 (client, state_store)。"""
        state_store = qr_module.OAuthStateStore()
        app = self._app(
            qr_client=qr_client or _StubQrClient(),
            state_store=state_store,
            qr_redirect_uri=self._REDIRECT_URI,
            member_checker=(member_checker
                            if member_checker is not None
                            else lambda userid: True),
        )
        return TestClient(app), state_store

    def _callback(self, tc, state, code="code-1"):
        return tc.get(f"/kb/api/auth/dingtalk/callback"
                      f"?authCode={code}&state={state}",
                      follow_redirects=False)

    def test_login_url_issued_with_state(self):
        tc, _store = self._enabled()
        resp = tc.get("/kb/api/auth/dingtalk/login-url")
        self.assertEqual(200, resp.status_code)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(
            resp.json()["login_url"]).query))
        self.assertEqual("ak", query["client_id"])
        self.assertEqual(self._REDIRECT_URI, query["redirect_uri"])
        self.assertTrue(query["state"])

    def test_login_url_unavailable_without_qr_deps(self):
        tc = TestClient(self._app())  # 全缺省 None
        self.assertEqual(
            503, tc.get("/kb/api/auth/dingtalk/login-url").status_code)

    def test_callback_success_issues_session_and_redirects(self):
        tc, store = self._enabled(qr_client=_StubQrClient(userid="u100"))
        resp = self._callback(tc, store.issue())
        self.assertEqual(302, resp.status_code)
        self.assertEqual("/kb/", resp.headers["location"])
        cookie = resp.headers["set-cookie"]
        self.assertIn(f"{auth.SESSION_COOKIE}=", cookie)
        token = cookie.split(f"{auth.SESSION_COOKIE}=")[1].split(";")[0]
        self.assertEqual(
            "u100", auth.resolve_session_userid(token, "test-secret"))

    def test_callback_unknown_state_redirects_expired(self):
        tc, _store = self._enabled()
        resp = self._callback(tc, "nope")
        self.assertEqual(302, resp.status_code)
        self.assertEqual("/kb/?login_error=expired", resp.headers["location"])

    def test_callback_state_consumed_once(self):
        tc, store = self._enabled()
        state = store.issue()
        self.assertEqual("/kb/", self._callback(tc, state).headers["location"])
        # 重放同一 state → expired（防重放）
        self.assertEqual("/kb/?login_error=expired",
                         self._callback(tc, state).headers["location"])

    def test_callback_exchange_failure_redirects_failed(self):
        tc, store = self._enabled(qr_client=_StubQrClient(fail=True))
        resp = self._callback(tc, store.issue(), code="bad")
        self.assertEqual("/kb/?login_error=failed", resp.headers["location"])

    def test_callback_non_member_denied(self):
        tc, store = self._enabled(member_checker=lambda userid: False)
        resp = self._callback(tc, store.issue())
        self.assertEqual("/kb/?login_error=denied", resp.headers["location"])
        self.assertNotIn("set-cookie", resp.headers)

    def test_callback_member_checker_error_fail_closed(self):
        def broken(userid):
            raise RuntimeError("db down")

        tc, store = self._enabled(member_checker=broken)
        resp = self._callback(tc, store.issue())
        self.assertEqual("/kb/?login_error=denied", resp.headers["location"])

    def test_callback_unavailable_without_member_checker(self):
        store_state = qr_module.OAuthStateStore()
        tc = TestClient(self._app(
            qr_client=_StubQrClient(), state_store=store_state,
            qr_redirect_uri=self._REDIRECT_URI))
        self.assertEqual(
            503, self._callback(tc, store_state.issue()).status_code)


if __name__ == "__main__":
    unittest.main()
