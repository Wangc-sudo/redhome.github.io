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

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

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
                       guide_config=None, corp_id=None,
                       qr_client=None, state_store=None,
                       qr_redirect_uri=None, member_checker=None,
                       export_service=None, export_authorizer=None):
    """注册 /kb/api 路由组。依赖全注入：

    service          KbWebService（问答主链路）
    store            WebSessionStore（会话存取）
    session_secret   HMAC session 密钥（复用 BI_WEB_SESSION_SECRET）
    auth_client      小易应用的 DingTalkAuthClient（免登交换）
    record_fetcher   record_id → AI 表记录 dict（附件签名 URL 现取）
    member_name_lookup  userid → 姓名（bootstrap 显示；None 跳过）
    session_secure   cookie Secure 标志（HTTPS 下置 1）
    guide_config     引导配置 dict（常用问题等，前端 bootstrap 直出）
    qr_client        扫码登录 DingTalkQrLoginClient（None=不开放扫码登录）
    state_store      OAuthStateStore（state 一次性票据）
    qr_redirect_uri  回调地址（服务端固定配置，不接受前端传参）
    member_checker   userid → bool 在册校验（fail-closed；None=扫码登录不可用）
    """

    def require_userid(request: Request):
        userid = auth.resolve_session_userid(
            request.cookies.get(auth.SESSION_COOKIE), session_secret)
        if userid is None:
            raise HTTPException(status_code=401, detail="unauthenticated")
        return userid

    def export_status(sid, userid, export_id):
        status = export_service.status(
            export_id, userid, f"web:{sid}", authorizer=export_authorizer,
        )
        status["download_url"] = (
            f"/kb/api/sessions/{sid}/exports/{export_id}/download"
        )
        return status

    def start_export(sid, userid, snapshot_id, background_tasks):
        export_id = export_service.create(
            snapshot_id, userid, f"web:{sid}", authorizer=export_authorizer,
        )
        background_tasks.add_task(
            export_service.generate, export_id, authorizer=export_authorizer,
        )
        return export_status(sid, userid, export_id)

    def raise_export_http(exc):
        if isinstance(exc, KeyError):
            raise HTTPException(status_code=404, detail="export not found") from exc
        if isinstance(exc, PermissionError):
            raise HTTPException(status_code=403, detail="export forbidden") from exc
        if isinstance(exc, FileNotFoundError):
            raise HTTPException(status_code=410, detail="export file unavailable") from exc
        if isinstance(exc, RuntimeError):
            raise HTTPException(status_code=409, detail="export not ready") from exc
        raise HTTPException(status_code=400, detail="invalid export request") from exc

    if export_service is not None and callable(getattr(export_service, "recover_pending", None)):
        @app.on_event("startup")
        async def kb_recover_exports():
            async def recover():
                try:
                    await asyncio.to_thread(
                        export_service.recover_pending,
                        authorizer=export_authorizer,
                    )
                except Exception as exc:
                    app.state.kb_export_recovery_error = type(exc).__name__

            app.state.kb_export_recovery = asyncio.create_task(recover())

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

    @app.get("/kb/api/auth/dingtalk/login-url")
    def kb_qr_login_url():
        """拼装钉钉扫码登录页地址（state 一次性票据，消费即作废）。"""
        if qr_client is None or state_store is None or not qr_redirect_uri:
            raise HTTPException(status_code=503, detail="unavailable")
        from common.kb_web.qr_login import build_login_url
        state = state_store.issue()
        return {"login_url": build_login_url(
            qr_client.app_key, qr_redirect_uri, state)}

    @app.get("/kb/api/auth/dingtalk/callback")
    def kb_qr_callback(authCode: str = "", state: str = ""):
        """钉钉扫码授权回调：state 校验 → code 换 userid → 在册 → 落 session。

        失败一律 302 回 /kb/?login_error=<code>（前端映射中文提示），成功
        302 回 /kb/。本端点是浏览器跳转链的一环，不返回 JSON 错误页。
        """
        if (qr_client is None or state_store is None or not qr_redirect_uri
                or member_checker is None):
            raise HTTPException(status_code=503, detail="unavailable")
        if not state_store.consume(state):
            return RedirectResponse("/kb/?login_error=expired",
                                    status_code=302)
        try:
            userid, _union_id = qr_client.exchange_qr_code(authCode or "")
        except auth.AuthError:
            return RedirectResponse("/kb/?login_error=failed",
                                    status_code=302)
        try:
            allowed = member_checker(userid)
        except Exception:
            allowed = False
        if not allowed:
            return RedirectResponse("/kb/?login_error=denied",
                                    status_code=302)
        response = RedirectResponse("/kb/", status_code=302)
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
                              background_tasks: BackgroundTasks,
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
            # 助手提议状态（2026-10-30）：同 pending 消费语义——纯确认词
            # 在 service 内短路为「执行上一轮提议」。
            offer = store.pop_offer(userid, sid)
            from common.kb_qa.export_delivery import (
                is_export_only_request,
                is_export_request,
            )

            export_only = is_export_only_request(content)
            if export_only:
                result = {
                    "reply": "Excel 导出功能暂未开放。",
                    "intent": "EXPORT",
                    "meta": {},
                    "attachments": [],
                }
                if export_service is not None:
                    snapshot_id = export_service.latest_snapshot_id(
                        userid, f"web:{sid}",
                    )
                    if snapshot_id:
                        try:
                            result["meta"]["export"] = start_export(
                                sid, userid, snapshot_id, background_tasks,
                            )
                            result["reply"] = "正在生成 Excel，请在此消息中查看进度。"
                        except Exception:
                            result["reply"] = "导出创建失败，请重新查询后再试。"
                    else:
                        result["reply"] = "暂无可导出的结构化查询结果，请先完成一次数据查询。"
            else:
                result = service.answer(userid, content,
                                        history_messages=history,
                                        pending=pending, offer=offer,
                                        audit={"sid": sid})
                snapshot_id = result["meta"].get("export_snapshot_id")
                if export_service is not None and is_export_request(content) and snapshot_id:
                    try:
                        result["meta"]["export"] = start_export(
                            sid, userid, snapshot_id, background_tasks,
                        )
                        result["reply"] += "\n\n正在生成 Excel，请在此消息中查看进度。"
                    except Exception:
                        result["reply"] += "\n\n导出创建失败，可稍后点击“导出 Excel”重试。"
            pending_candidates = result.pop("pending_candidates", None)
            if pending_candidates:
                store.set_pending(userid, sid, pending_candidates)
            new_offer = result.pop("offer", None)
            if new_offer:
                store.set_offer(userid, sid, new_offer)
            now = _now_text()
            store.append(userid, sid, [
                {"role": "user", "content": content, "ts": now},
                {"role": "assistant", "content": result["reply"], "ts": now,
                 # intent 入 meta：历史卫生层据此过滤 CHITCHAT/HELP 噪声轮。
                 "meta": dict(result["meta"], intent=result["intent"]),
                 "attachments": result["attachments"]},
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
        # 重答不改变 clarify/提议状态：状态键不透给前端、不写回存储。
        result.pop("pending_candidates", None)
        result.pop("offer", None)
        now = _now_text()
        store.append(userid, sid, [
            {"role": "assistant", "content": result["reply"], "ts": now,
             "meta": dict(result["meta"], intent=result["intent"]),
             "attachments": result["attachments"],
             "regenerated": True},
        ])
        return result

    @app.post("/kb/api/sessions/{sid}/exports")
    async def kb_create_export(sid: str, request: Request,
                               background_tasks: BackgroundTasks,
                               userid=Depends(require_userid)):
        if export_service is None:
            raise HTTPException(status_code=503, detail="export unavailable")
        if store.get_messages(userid, sid) is None:
            raise HTTPException(status_code=404, detail="not found")
        try:
            body = await request.json()
        except Exception:
            body = {}
        snapshot_id = body.get("snapshot_id") if isinstance(body, dict) else None
        if not snapshot_id:
            raise HTTPException(status_code=400, detail="snapshot_id required")
        try:
            return start_export(sid, userid, snapshot_id, background_tasks)
        except Exception as exc:
            raise_export_http(exc)

    @app.get("/kb/api/sessions/{sid}/exports/{export_id}")
    def kb_export_status(sid: str, export_id: str,
                         userid=Depends(require_userid)):
        if export_service is None:
            raise HTTPException(status_code=503, detail="export unavailable")
        if store.get_messages(userid, sid) is None:
            raise HTTPException(status_code=404, detail="not found")
        try:
            return export_status(sid, userid, export_id)
        except Exception as exc:
            raise_export_http(exc)

    @app.get("/kb/api/sessions/{sid}/exports/{export_id}/download")
    def kb_download_export(sid: str, export_id: str,
                           userid=Depends(require_userid)):
        if export_service is None:
            raise HTTPException(status_code=503, detail="export unavailable")
        if store.get_messages(userid, sid) is None:
            raise HTTPException(status_code=404, detail="not found")
        try:
            path, file_name = export_service.download(
                export_id, userid, f"web:{sid}", authorizer=export_authorizer,
            )
        except Exception as exc:
            raise_export_http(exc)
        return FileResponse(
            path,
            filename=file_name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

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
