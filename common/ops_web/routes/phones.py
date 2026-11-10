"""ops-web phones 路由。"""

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
    _resolve_request_viewer = ctx.resolve_request_viewer
    _forbidden = ctx.forbidden
    require_admin = ctx.require_admin
    require_capability = ctx.require_capability
    require_viewer = ctx.require_viewer
    require_data_quality_write = ctx.require_data_quality_write
    _spa_shell = ctx.spa_shell

    @app.get("/phones")
    def phones_page():
        """号码台账页壳（Router 渲染）。"""
        return _spa_shell()

    @app.get("/api/phones")
    def api_phones(request: Request):
        """号码台账读面：摘要 + 号码列表。

        ``?keyword=`` 命中号码/持卡人/开户名/部门/物理手机/备注任一；
        ``?unclaimed=1`` 只看待认领。
        """
        viewer = _resolve_request_viewer(request)
        if not capability_allows(viewer, CAPABILITY_PHONE):
            _forbidden(request)
        request.state.viewer = viewer
        keyword = (request.query_params.get("keyword") or "").strip()
        unclaimed = (request.query_params.get("unclaimed") or "") == "1"
        try:
            with db_connector() as connection:
                phones = phone_registry.fetch_phone_list(
                    connection, keyword=keyword, unclaimed_only=unclaimed)
                summary = phone_registry.summarize_registry(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web phones load failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({
            "status": "ok",
            "summary": {
                "total": summary["total"],
                "unclaimed": summary["unclaimed"],
                "dead": summary["dead"],
            },
            "phones": [
                dict(row, updated_at=_fmt_time(row.get("updated_at")))
                for row in phones
            ],
        })

    @app.get("/api/phones/bindings")
    def api_phone_bindings(request: Request):
        """账号绑定读面：``?keyword=`` 命中平台账号/渠道类目/号码任一。

        回答「XX 店铺账号的验证码发到哪个号、谁在拿这台手机」。
        """
        viewer = _resolve_request_viewer(request)
        if not capability_allows(viewer, CAPABILITY_PHONE):
            _forbidden(request)
        request.state.viewer = viewer
        keyword = (request.query_params.get("keyword") or "").strip()
        try:
            with db_connector() as connection:
                bindings = phone_registry.fetch_bindings(
                    connection, keyword=keyword)
        except Exception as exc:
            _LOGGER.warning("ops-web phone bindings load failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({
            "status": "ok",
            "bindings": [
                dict(row, updated_at=_fmt_time(row.get("updated_at")))
                for row in bindings
            ],
        })

    @app.post("/api/phones/claim")
    async def phone_claim(request: Request):
        """认领：确认号码的实际持卡人（needs_claim 归零）。"""
        viewer = require_capability(request, CAPABILITY_PHONE)
        payload = await request.json()
        phone_no = payload.get("phone_no")
        user = (payload.get("actual_user") or "").strip()
        if not isinstance(phone_no, str) or not phone_no.strip() or not user:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                changed = phone_registry.set_phone_user(
                    connection, phone_no.strip(), user, actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning("ops-web phone claim failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not changed:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/phones/save")
    async def phone_save(request: Request):
        """新增/更新一条号码（自然键 = phone_no）。"""
        viewer = require_capability(request, CAPABILITY_PHONE)
        payload = await request.json()
        try:
            record = phone_registry.validate_phone_record(
                payload.get("phone_no"),
                phone_type=payload.get("phone_type") or "未知",
                plan_name=payload.get("plan_name") or "",
                account_name=payload.get("account_name") or "",
                actual_user=(payload.get("actual_user") or "").strip(),
                owner=payload.get("owner") or "",
                dept=payload.get("dept") or "",
                corp_owner=payload.get("corp_owner") or "",
                physical_device=payload.get("physical_device") or "",
                status=payload.get("status") or "",
                note=payload.get("note") or "",
                needs_claim=not (payload.get("actual_user") or "").strip(),
            )
        except phone_registry.PhoneRegistryError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                phone_registry.upsert_phone_number(
                    connection, record, actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning("ops-web phone save failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/phones/toggle")
    async def phone_toggle(request: Request):
        """停用/启用一个号码。"""
        viewer = require_capability(request, CAPABILITY_PHONE)
        payload = await request.json()
        phone_no = payload.get("phone_no")
        if not isinstance(phone_no, str) or not phone_no.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                changed = phone_registry.set_phone_enabled(
                    connection, phone_no.strip(),
                    bool(payload.get("enabled")), actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning("ops-web phone toggle failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not changed:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/phones/binding/save")
    async def phone_binding_save(request: Request):
        """新增/更新一条账号绑定（号码+类目+账号为自然键）。"""
        viewer = require_capability(request, CAPABILITY_PHONE)
        payload = await request.json()
        try:
            with db_connector() as connection:
                phone_registry.upsert_phone_binding(
                    connection,
                    payload.get("phone_no"),
                    payload.get("channel_category"),
                    payload.get("platform_account"),
                    note=payload.get("note") or "",
                    actor=viewer.userid,
                )
        except phone_registry.PhoneRegistryError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except Exception as exc:
            _LOGGER.warning("ops-web phone binding save failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/phones/binding/toggle")
    async def phone_binding_toggle(request: Request):
        """停用/启用一条账号绑定（按主键 id）。"""
        viewer = require_capability(request, CAPABILITY_PHONE)
        payload = await request.json()
        binding_id = payload.get("id")
        if not isinstance(binding_id, int) or binding_id <= 0:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                changed = phone_registry.set_binding_enabled(
                    connection, binding_id, bool(payload.get("enabled")),
                    actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning("ops-web phone binding toggle failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not changed:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})
