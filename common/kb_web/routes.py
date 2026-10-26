# -*- coding: utf-8 -*-
"""小易 Web 前端 API 路由组（挂 bi-web，前缀 /kb/api）。

认证：bi_web.auth 的 HMAC session cookie（与看板同一 session 体系，但
/kb 只要求「登录」——任何组织成员免登成功即可，不走 bi_authz 看板授权；
业务数据工具的收窄在 service 层经 kb_web_acl 做）。

免登交换用小易应用凭据（KB_DINGTALK_*）——H5 微应用挂在哪个应用下，
authCode 就必须用哪个应用的 key/secret 换。
"""

import asyncio
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from common.bi_web import auth

#: 在飞重复请求的等待上限/轮询间隔（秒）；测试可 monkeypatch 调 0。
IDEM_WAIT_SECONDS = 90
IDEM_WAIT_INTERVAL = 1.0


def _now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


async def _wait_idem_done(store, userid, sid, content):
    """在飞重复请求：短轮询等首个请求的答案落结果窗（拿不到返回 None）。"""
    deadline = time.monotonic() + IDEM_WAIT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(IDEM_WAIT_INTERVAL)
        done = store.get_idem_done(userid, sid, content)
        if done is not None:
            return done
    return None


def register_kb_routes(app: FastAPI, *, service, store, session_secret,
                       auth_client=None, record_fetcher=None,
                       member_name_lookup=None, session_secure=False,
                       guide_config=None, corp_id=None):
    """注册 /kb/api 路由组。依赖全注入：

    service          KbWebService（问答主链路）
    store            WebSessionStore（会话存取）
    session_secret   HMAC session 密钥（复用 BI_WEB_SESSION_SECRET）
    auth_client      小易应用的 DingTalkAuthClient（免登交换）
    record_fetcher   record_id → AI 表记录 dict（附件签名 URL 现取）
    member_name_lookup  userid → 姓名（bootstrap 显示；None 跳过）
    session_secure   cookie Secure 标志（HTTPS 下置 1）
    guide_config     引导配置 dict（常用问题等，前端 bootstrap 直出）
    """

    def require_userid(request: Request):
        userid = auth.resolve_session_userid(
            request.cookies.get(auth.SESSION_COOKIE), session_secret)
        if userid is None:
            raise HTTPException(status_code=401, detail="unauthenticated")
        return userid

    # ------------------------------------------------------------------
    # 认证
    # ------------------------------------------------------------------
    @app.post("/kb/api/auth/dingtalk")
    async def kb_auth_dingtalk(request: Request):
        if auth_client is None:
            raise HTTPException(status_code=503, detail="unavailable")
        try:
            body = await request.json()
        except Exception:
            body = {}
        auth_code = body.get("authCode") if isinstance(body, dict) else None
        try:
            userid = auth_client.exchange_auth_code(auth_code or "")
        except auth.AuthError:
            raise HTTPException(status_code=401, detail="auth failed")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.issue_session(userid, session_secret),
            max_age=auth.SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=session_secure,
        )
        return response

    # ------------------------------------------------------------------
    # 引导与用户信息
    # ------------------------------------------------------------------
    @app.get("/kb/api/public-config")
    def kb_public_config():
        # 免登前端原料（corpId 非敏感，钉钉容器内公开可见）。
        return {"corp_id": corp_id or ""}

    @app.get("/kb/api/bootstrap")
    def kb_bootstrap(userid=Depends(require_userid)):
        name = None
        if member_name_lookup is not None:
            try:
                name = member_name_lookup(userid)
            except Exception:
                name = None
        return {
            "userid": userid,
            "name": name,
            "guide": guide_config or {},
        }

    # ------------------------------------------------------------------
    # 会话 CRUD
    # ------------------------------------------------------------------
    @app.get("/kb/api/sessions")
    def kb_sessions(userid=Depends(require_userid)):
        return {"sessions": store.list_sessions(userid)}

    @app.post("/kb/api/sessions")
    def kb_create_session(userid=Depends(require_userid)):
        return {"sid": store.create_session(userid)}

    @app.get("/kb/api/sessions/{sid}")
    def kb_session_messages(sid: str, userid=Depends(require_userid)):
        messages = store.get_messages(userid, sid)
        if messages is None:
            raise HTTPException(status_code=404, detail="not found")
        return {"sid": sid, "title": store.get_title(userid, sid),
                "messages": messages}

    @app.delete("/kb/api/sessions/{sid}")
    def kb_delete_session(sid: str, userid=Depends(require_userid)):
        if not store.delete(userid, sid):
            raise HTTPException(status_code=404, detail="not found")
        return {"ok": True}

    @app.patch("/kb/api/sessions/{sid}")
    async def kb_rename_session(sid: str, request: Request,
                                userid=Depends(require_userid)):
        try:
            body = await request.json()
        except Exception:
            body = {}
        title = body.get("title") if isinstance(body, dict) else None
        if not store.rename(userid, sid, title or ""):
            raise HTTPException(status_code=404, detail="not found")
        return {"ok": True}

    # ------------------------------------------------------------------
    # 消息
    # ------------------------------------------------------------------
    @app.post("/kb/api/sessions/{sid}/messages")
    async def kb_send_message(sid: str, request: Request,
                              userid=Depends(require_userid)):
        try:
            body = await request.json()
        except Exception:
            body = {}
        content = (body.get("content") if isinstance(body, dict)
                   else None) or ""
        content = content.strip()
        if not content:
            raise HTTPException(status_code=400, detail="bad request")
        history = store.get_messages(userid, sid)
        if history is None:
            raise HTTPException(status_code=404, detail="not found")

        # 发送幂等（2026-10-26）：前端超时/断连后服务端仍会答完落库，
        # 「重发/双击/弱网重试」的同内容请求直接取结果窗，不重跑 agent、
        # 不重复写会话（否则历史里重复用户消息 + 双倍 LLM 成本）。
        done = store.get_idem_done(userid, sid, content)
        if done is not None:
            return done
        if not store.acquire_idem(userid, sid, content):
            done = await _wait_idem_done(store, userid, sid, content)
            if done is not None:
                return done
            raise HTTPException(
                status_code=409,
                detail="同内容提问正在处理中，请稍后刷新会话查看")
        try:
            # clarify 闭环（2026-10-23）：先 pop 上一轮候选（消费即作废），
            # 本轮再发反问则把新候选存回。
            pending = store.pop_pending(userid, sid)
            result = service.answer(userid, content,
                                    history_messages=history,
                                    pending=pending, audit={"sid": sid})
            pending_candidates = result.pop("pending_candidates", None)
            if pending_candidates:
                store.set_pending(userid, sid, pending_candidates)
            now = _now_text()
            store.append(userid, sid, [
                {"role": "user", "content": content, "ts": now},
                {"role": "assistant", "content": result["reply"], "ts": now,
                 "meta": result["meta"], "attachments": result["attachments"]},
            ], title_hint=content)
        except Exception:
            store.release_idem(userid, sid, content)
            raise
        store.complete_idem(userid, sid, content, result)
        return result

    @app.post("/kb/api/sessions/{sid}/regenerate")
    def kb_regenerate(sid: str, userid=Depends(require_userid)):
        history = store.get_messages(userid, sid)
        if history is None:
            raise HTTPException(status_code=404, detail="not found")
        last_user = next((m for m in reversed(history)
                          if m.get("role") == "user"), None)
        if last_user is None:
            raise HTTPException(status_code=400, detail="bad request")
        result = service.answer(userid, last_user["content"],
                                history_messages=history,
                                audit={"sid": sid})
        # 重答不改变 clarify 状态：状态键不透给前端。
        result.pop("pending_candidates", None)
        now = _now_text()
        store.append(userid, sid, [
            {"role": "assistant", "content": result["reply"], "ts": now,
             "meta": result["meta"], "attachments": result["attachments"],
             "regenerated": True},
        ])
        return result

    # ------------------------------------------------------------------
    # 附件（现取签名 URL → 302；签名 URL 不落前端日志）
    # ------------------------------------------------------------------
    @app.get("/kb/api/attachments/{record_id}/{field}/{index}")
    def kb_attachment(record_id: str, field: str, index: int,
                      userid=Depends(require_userid)):
        if record_fetcher is None:
            raise HTTPException(status_code=503, detail="unavailable")
        try:
            record = record_fetcher(record_id)
            items = ((record or {}).get("fields") or {}).get(field) or []
            url = items[index].get("url") if 0 <= index < len(items) else None
        except Exception:
            url = None
        if not url:
            raise HTTPException(status_code=404, detail="not found")
        return RedirectResponse(url, status_code=302)
