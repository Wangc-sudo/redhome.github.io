"""ops-web auth 路由。"""

from common.ops_web.helpers import *


def register_routes(app, ctx):
    settings = ctx.settings
    session_secret = ctx.session_secret
    db_connector = ctx.db_connector
    auth_client = ctx.auth_client
    viewer_resolver = ctx.viewer_resolver
    session_secure = ctx.session_secure
    browser_login_token = ctx.browser_login_token
    fleet_source = ctx.fleet_source
    config_source = ctx.config_source
    config_publisher = ctx.config_publisher
    config_deleter = ctx.config_deleter
    corp_id = ctx.corp_id
    agent_id = ctx.agent_id
    auth_disabled = ctx.auth_disabled
    data_quality_source_connector = ctx.data_quality_source_connector
    price_agent_config_source = ctx.price_agent_config_source
    price_agent_token_hash = ctx.price_agent_token_hash
    connect = ctx.connect

    def _resolve_request_viewer(request: Request) -> authz.Viewer:
        """session → Viewer（各闸共用的解析段；未登录 401/302，解析异常 403）。"""
        if auth_disabled and request.method in ("GET", "HEAD", "OPTIONS"):
            # 读写分离（2026-10-26 用户裁决）：认证关闭只放读面——查看匿名
            # 直通；写方法落入下方正常会话解析：未登录 401（/api/ 前缀）、
            # 登录后按既有授权（admin/能力×范围）判定，审计记真实 userid。
            # 登录端点（/auth/*-login 等）不调闸，天然匿名可达。
            request.state.viewer = _OPEN_VIEWER
            return _OPEN_VIEWER
        raw = request.cookies.get(auth.SESSION_COOKIE)
        userid = (
            auth.resolve_session_userid(raw, session_secret) if raw else None
        )
        if userid is None:
            accept = request.headers.get("accept", "")
            if "application/json" in accept or request.url.path.startswith("/api/"):
                raise HTTPException(status_code=401, detail=ErrorDetail.UNAUTHORIZED)
            raise HTTPException(
                status_code=302, headers={"Location": "/auth/entry?reason=login"}
            )
        try:
            return viewer_resolver.resolve(userid)
        except Exception as exc:
            # 权限链 fail-closed（铁律 2）：解析异常 = 拒绝。
            _LOGGER.warning("ops-web viewer resolve failed: %s", type(exc).__name__)
            raise HTTPException(status_code=403, detail=ErrorDetail.FORBIDDEN)

    def _forbidden(request: Request):
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=403, detail=ErrorDetail.FORBIDDEN)
        raise HTTPException(
            status_code=302,
            headers={"Location": "/auth/entry?reason=forbidden"},
        )

    def require_admin(request: Request) -> authz.Viewer:
        """应用层闸：session 有效 + admin；其余一律提示页/403。"""
        viewer = _resolve_request_viewer(request)
        if not viewer.is_admin:
            _forbidden(request)
        request.state.viewer = viewer
        return viewer

    def require_capability(request: Request, capability, region=None
                           ) -> authz.Viewer:
        """能力闸（2026-10-14 D4 能力×范围）：admin 或持 ``<capability>`` /
        ``<capability>:<region>`` 授权；其余提示页 / 403。"""
        viewer = _resolve_request_viewer(request)
        if not capability_allows(viewer, capability, region):
            _forbidden(request)
        request.state.viewer = viewer
        return viewer

    def require_viewer(request: Request) -> authz.Viewer:
        """已准入 viewer 的只读闸；写面另走 capability，不能仅靠菜单隐藏。"""
        viewer = _resolve_request_viewer(request)
        if not viewer.admitted:
            _forbidden(request)
        request.state.viewer = viewer
        return viewer

    def require_data_quality_write(request: Request) -> authz.Viewer:
        return require_capability(request, CAPABILITY_DATAFIX)

    # -- 认证路由（与 bi-web 同一套实现，铁律 7）----------------------------
    @app.post("/auth/dingtalk")
    async def auth_dingtalk(request: Request):
        if auth_client is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            payload = await request.json()
        except Exception:
            payload = None
        auth_code = payload.get("authCode") if isinstance(payload, dict) else None
        if not isinstance(auth_code, str) or not auth_code:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            userid = auth_client.exchange_auth_code(auth_code)
        except auth.AuthError as exc:
            _LOGGER.warning("ops-web auth exchange failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        _LOGGER.info("ops-web login: %s", auth.mask_userid(userid))
        response = JSONResponse({"status": "ok"})
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.issue_session(userid, session_secret),
            max_age=auth.SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=session_secure,
        )
        return response

    @app.get("/auth/entry")
    def auth_entry():
        return FileResponse(auth.AUTH_ENTRY_PAGE)

    @app.get("/auth/login-options")
    def auth_login_options():
        """入口页据此决定是否渲染「浏览器登录」表单（2026-10-14）。

        bi-web 无本路由 → 404 → 表单保持隐藏，共享入口页对 bi-web 零变化。
        """
        return JSONResponse(
            {
                "dingtalk": auth_client is not None,
                "browser": bool(browser_login_token),
                # 工号+密码通道（2026-10-19）：凭据表存在即开放，与是否配
                # 运维口令无关——两者是并列的浏览器登录方式。
                "password": True,
            }
        )

    @app.post("/auth/browser-login")
    async def auth_browser_login(request: Request):
        """浏览器登录（2026-10-14）：运维口令 + 钉钉账号 → 同一枚 session。

        未配口令 = 通道未开放（503）；口令或账号任一不合规一律 401（不区分
        分支，防探测）。**签发会话 ≠ 获得权限**：账号可见性仍由
        ``ViewerResolver`` 逐请求对 ``bi_authz`` 校验（fail-closed）。
        """
        if not browser_login_token:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            payload = await request.json()
        except Exception:
            payload = None
        user_id = payload.get("user_id") if isinstance(payload, dict) else None
        token = payload.get("token") if isinstance(payload, dict) else None
        if (
            not isinstance(user_id, str)
            or not user_id.strip()
            or not isinstance(token, str)
            or not token
        ):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        user_id = user_id.strip()
        # 按人口令优先（2026-10-19 过渡期裁决）：**已签发按人口令的人，全局
        # 共享口令对其失效**——这是解掉「拿到共享口令即可冒用 admin」的那一
        # 步。未签发者仍走共享口令，作为尚未换发期的逃生口。
        try:
            with db_connector() as connection:
                if ops_token.is_issued(connection, user_id):
                    if not ops_token.check_token(connection, user_id, token):
                        _LOGGER.warning("ops-web browser login rejected (personal)")
                        raise HTTPException(
                            status_code=401, detail=ErrorDetail.UNAUTHORIZED)
                    ops_token.touch_used(connection, user_id, token)
                    _LOGGER.info("ops-web browser login (personal): %s",
                                 auth.mask_userid(user_id))
                    response = JSONResponse({"status": "ok"})
                    response.set_cookie(
                        auth.SESSION_COOKIE,
                        auth.issue_session(user_id, session_secret),
                        max_age=auth.SESSION_TTL_SECONDS,
                        httponly=True,
                        samesite="lax",
                        secure=session_secure,
                    )
                    return response
        except HTTPException:
            raise
        except Exception as exc:
            # 按人口令面不可用（表未迁移/DB 异常）→ 不静默放行共享口令，
            # 直接 503，避免退化成可冒用的老路径。
            _LOGGER.warning("ops-web personal token check failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not browser_login_token or not hmac.compare_digest(
                token, browser_login_token):
            _LOGGER.warning("ops-web browser login rejected")
            raise HTTPException(status_code=401, detail=ErrorDetail.UNAUTHORIZED)
        _LOGGER.info("ops-web browser login: %s", auth.mask_userid(user_id))
        response = JSONResponse({"status": "ok"})
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.issue_session(user_id, session_secret),
            max_age=auth.SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=session_secure,
        )
        return response

    @app.post("/auth/password-login")
    async def auth_password_login(request: Request):
        """工号 + 密码登录（2026-10-19 用户裁决「用户名=工号、密码=系统
        生成的随机初始密码」）。

        与 browser-login 签发**同一枚** session cookie；**签发会话 ≠ 获得
        权限**——权限仍由 ``bi_authz`` 逐请求 fail-closed 判定，不因登录
        方式改变。

        防探测：工号不存在 / 密码错误 / 已被锁定**一律 401 同一形态**
        （18100 对公网开放，不能让探测者区分账号是否存在）。
        """
        try:
            payload = await request.json()
        except Exception:
            payload = None
        job_number = (
            payload.get("job_number") if isinstance(payload, dict) else None)
        password = payload.get("password") if isinstance(payload, dict) else None
        if (not isinstance(job_number, str) or not isinstance(password, str)
                or not password):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                resolved = ops_credential.resolve_user_by_job_number(
                    connection, job_number)
                if resolved is None:
                    _LOGGER.info("ops-web password login: job number not found")
                    raise HTTPException(
                        status_code=401, detail=ErrorDetail.UNAUTHORIZED)
                user_id, _name = resolved
                row = ops_credential.fetch_credential(connection, user_id)
                if row is None or row.algo != ops_credential.ALGO:
                    raise HTTPException(
                        status_code=401, detail=ErrorDetail.UNAUTHORIZED)
                if ops_credential.is_locked(row.locked_until):
                    raise HTTPException(
                        status_code=401, detail=ErrorDetail.UNAUTHORIZED)
                ok = ops_credential.verify_password(
                    password,
                    salt_hex=row.salt_hex,
                    iterations=row.iterations,
                    expected_hash=row.password_hash,
                )
                if not ok:
                    ops_credential.register_failure(connection, user_id)
                    _LOGGER.info("ops-web password login rejected: %s",
                                 auth.mask_userid(user_id))
                    raise HTTPException(
                        status_code=401, detail=ErrorDetail.UNAUTHORIZED)
                ops_credential.clear_failures(connection, user_id)
                must_change = row.must_change
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning("ops-web password login failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        _LOGGER.info("ops-web password login: %s", auth.mask_userid(user_id))
        response = JSONResponse({"status": "ok", "must_change": must_change})
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.issue_session(user_id, session_secret),
            max_age=auth.SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=session_secure,
        )
        return response

    @app.post("/auth/change-password")
    async def auth_change_password(request: Request):
        """改自己的密码（需已登录；初始密码首登强制改，改完清 must_change）。

        必须校验旧密码——防止「会话被劫持后直接改密接管账号」。
        """
        viewer = _resolve_request_viewer(request)
        if not viewer.admitted:
            _forbidden(request)
        try:
            payload = await request.json()
        except Exception:
            payload = None
        old = payload.get("old_password") if isinstance(payload, dict) else None
        new = payload.get("new_password") if isinstance(payload, dict) else None
        if not isinstance(old, str) or not isinstance(new, str):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            ops_credential.validate_password(new)
        except ops_credential.CredentialError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                row = ops_credential.fetch_credential(connection, viewer.userid)
                if row is None:
                    raise HTTPException(
                        status_code=401, detail=ErrorDetail.UNAUTHORIZED)
                if not ops_credential.verify_password(
                        old, salt_hex=row.salt_hex, iterations=row.iterations,
                        expected_hash=row.password_hash):
                    raise HTTPException(
                        status_code=401, detail=ErrorDetail.UNAUTHORIZED)
                ops_credential.set_password(
                    connection, viewer.userid, new, actor=viewer.userid)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning("ops-web change password failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    def _issue_or_reset_credential(viewer, user_id, *, reset: bool):
        """admin 为某人生成/重置初始密码（明文只返回一次，不落库不进日志）。

        错误词汇只取既有五项——不引入 NOT_FOUND，缺凭据的重置一律 400。
        """
        if not isinstance(user_id, str) or not user_id:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                if reset and not ops_credential.fetch_credential(
                        connection, user_id):
                    raise HTTPException(
                        status_code=400, detail=ErrorDetail.BAD_REQUEST)
                password = ops_credential.issue_initial_password(
                    connection, user_id, actor=viewer.userid)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning("ops-web credential issue failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({
            "status": "ok",
            "user_id": user_id,
            "password": password,          # 明文只此一次，库里只有哈希
            "must_change": True,
        })

    @app.post("/api/credentials/issue")
    async def api_credential_issue(request: Request):
        """admin：为某人开通登录（生成随机初始密码，首登强制改密）。"""
        viewer = require_admin(request)
        payload = await _json_body(request)
        return _issue_or_reset_credential(
            viewer, payload.get("user_id"), reset=False)

    @app.post("/api/credentials/reset")
    async def api_credential_reset(request: Request):
        """admin：重置某人密码（忘记密码/口令泄漏后的补救通道）。"""
        viewer = require_admin(request)
        payload = await _json_body(request)
        return _issue_or_reset_credential(
            viewer, payload.get("user_id"), reset=True)

    @app.post("/api/tokens/issue")
    async def api_token_issue(request: Request):
        """admin：为某人签发按人登录口令；**明文只返回一次**（库里只有哈希）。

        签发后，全局共享口令对该 ``user_id`` 立即失效——冒用面就此关闭。
        """
        viewer = require_admin(request)
        payload = await _json_body(request)
        user_id = payload.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                token = ops_token.issue_token(
                    connection, user_id, actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning("ops-web token issue failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({
            "status": "ok", "user_id": user_id, "token": token,
        })

    @app.post("/api/tokens/revoke")
    async def api_token_revoke(request: Request):
        """admin：吊销某人**全部**按人口令（口令泄漏后的止血动作）。

        吊销后该人退回全局共享口令——若共享口令也已外发，应同时轮换它。
        """
        viewer = require_admin(request)
        payload = await _json_body(request)
        user_id = payload.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                count = ops_token.revoke_tokens(
                    connection, user_id, actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning("ops-web token revoke failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "revoked": count})

    @app.get("/api/tokens")
    async def api_token_list(request: Request):
        """admin：口令台账（**永不含明文/哈希**，只有人与时间）。"""
        require_admin(request)
        try:
            with db_connector() as connection:
                rows = ops_token.list_tokens(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web token list failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "items": rows})

    @app.get("/auth/logout")
    def auth_logout():
        response = RedirectResponse("/auth/entry?reason=loggedout")
        response.delete_cookie(auth.SESSION_COOKIE)
        return response

    @app.get("/auth/jsapi-config")
    def auth_jsapi_config(url: str = ""):
        # dd.config 签名原料（与 bi-web 同款，设计稿 §5.3）：agentId/corpId +
        # 随机 nonceStr/timeStamp + 对当前页 URL 的 SHA1 签名。与 /auth/dingtalk
        # 同属认证路由；url 仅参与签名、不回显，scheme 非 http/https 直接 400。
        if auth_client is None or not corp_id or not agent_id:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        nonce = auth.make_nonce()
        timestamp = int(time.time() * 1000)
        try:
            ticket = auth_client.jsapi_ticket()
        except auth.AuthError as exc:
            _LOGGER.warning(
                "ops-web jsapi ticket fetch failed: %s", type(exc).__name__
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse(
            {
                "agentId": agent_id,
                "corpId": corp_id,
                "timeStamp": timestamp,
                "nonceStr": nonce,
                "signature": auth.build_jsapi_signature(
                    ticket, nonce, timestamp, url
                ),
            }
        )

    ctx.resolve_request_viewer = _resolve_request_viewer
    ctx.forbidden = _forbidden
    ctx.require_admin = require_admin
    ctx.require_capability = require_capability
    ctx.require_viewer = require_viewer
    ctx.require_data_quality_write = require_data_quality_write
