"""ops-web core 路由。"""

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

    @app.get("/healthz")
    def healthz():
        # 顶层运维端点（与 bi-web 同款）：活性与 mart 连通性（SELECT 1）。
        try:
            with db_connector() as connection:
                cursor = connection.cursor()
                try:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                finally:
                    cursor.close()
        except Exception as exc:
            _LOGGER.warning(
                "ops-web healthz mart probe failed: %s", type(exc).__name__
            )
            return JSONResponse({"status": "unhealthy"}, status_code=503)
        return JSONResponse({"status": "ok", "database": "ok"})

    # -- 能力×范围自述（SPA 启动拉取：驱动路由守卫与菜单渲染）---------------
    @app.get("/api/me")
    def api_me(request: Request):
        """当前会话的能力×范围视图（2026-10-14 D4；SPA 启动时调用）。

        返回：全局能力列表 *capabilities*、区域级能力映射
        *scoped_capabilities*、可操作区域 *regions*、全量区域 *all_regions*。
        未准入 → 403（deny-by-default）。
        """
        viewer = _resolve_request_viewer(request)
        if not viewer.admitted:
            _forbidden(request)
        request.state.viewer = viewer
        try:
            with db_connector() as connection:
                registry = [key for key, _ in _region_scope_pairs(connection)]
        except Exception as exc:
            _LOGGER.warning("ops-web me region read failed: %s", type(exc).__name__)
            registry = list(report_roster.SCOPES)
        capabilities = []
        scoped = {}
        for capability in ALL_CAPABILITIES:
            if capability_allows(viewer, capability):
                capabilities.append(capability)
                continue
            keys = scoped_region_keys(viewer, capability)
            if keys:
                scoped[capability] = keys
        return JSONResponse({
            "status": "ok",
            "userid": viewer.userid,
            "name": viewer.name,
            "is_admin": viewer.is_admin,
            "capabilities": capabilities,
            "scoped_capabilities": scoped,
            "regions": visible_region_scopes(viewer, registry),
            "all_regions": registry,
        })

    # -- 页面 ----------------------------------------------------------------
    # 「成员与授权」页已停用（2026-10-13）：在职成员展示与归属管理迁至
    # 「组织编排」SPA（/members）。路由保留防书签 404，页面引导跳转。
    @app.get("/member-grants")
    def members_page():
        """旧书签兼容：与 /grants 同壳（Router 渲染只读授权页）。"""
        return _spa_shell()

    # -- 全 SPA 单壳（Vue3 + Router；2026-10-14 D1 全量迁移）------------------
    def _spa_shell():
        """SPA 单壳：返回 Vite 产物 index.html。

        不设应用层能力闸：未登录 / 无权限由前端启动 ``/api/me`` → 401/403
        → 跳登录；数据 API 仍 fail-closed（deny-by-default）。产物缺失 → 503。
        """
        if not os.path.isfile(ctx.org_spa_index()):
            _LOGGER.warning("ops-web SPA build missing: %s", ctx.org_spa_index())
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return FileResponse(
            ctx.org_spa_index(), media_type="text/html",
            headers={"Cache-Control": "no-cache"})

    @app.get("/")
    def spa_home_page():
        """登录后首页 = SPA（Router 渲染总览）。"""
        return _spa_shell()

    @app.get("/org")
    def spa_org_page():
        """组织编排页壳（Router 渲染）。"""
        return _spa_shell()

    @app.get("/members")
    def members_spa_page():
        """旧书签兼容：与 /、/org 同壳。"""
        return _spa_shell()
    ctx.spa_shell = _spa_shell


def register_system_routes(app, ctx):
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

    # -- 数据质量闭环：viewer 只读，datafix/admin 写；重跑仅创建 pending 请求。
    register_data_quality_routes(
        app, settings=settings, db_connector=db_connector,
        require_viewer=require_viewer,
        require_write=require_data_quality_write,
        source_connector=data_quality_source_connector,
        connect=connect, logger=_LOGGER, error_detail=ErrorDetail,
    )

    # -- 数据新鲜度与知识索引（2026-10-29 自动化闭环 P0）：系统后台只读 +
    # 受控检查。权限与数据质量写面同级（pipelines/datafix/admin）。
    def require_freshness_read(request: Request) -> authz.Viewer:
        return require_capability(request, CAPABILITY_PIPELINES)

    def require_business_freshness_read(request: Request, region: str) -> authz.Viewer:
        viewer = _resolve_request_viewer(request)
        if not (
            capability_allows(viewer, CAPABILITY_ORG, region)
            or capability_allows(viewer, CAPABILITY_ROSTER, region)
        ):
            _forbidden(request)
        request.state.viewer = viewer
        return viewer

    register_freshness_routes(
        app, connector=_mart_connector(settings),
        manual_connector=(
            _database_connector(settings.manual_database)
            if getattr(settings, "manual_database", None) is not None else None
        ),
        require_capability=require_freshness_read,
        business_require_capability=require_business_freshness_read,
        audit_writer=lambda conn, actor, dataset_key, status: (
            ops_control.insert_pipeline_audit(
                conn, actor, "freshness_check", dataset_key, status,
            )
        ),
    )

    # -- 猫超 Windows 机器 API（独立 Bearer 认证，GET 也不走匿名读直通）---
    if price_agent_config_source is not None:
        from common.price_agent.api import register_price_agent_routes
        register_price_agent_routes(
            app,
            db_connector=db_connector,
            config_source=price_agent_config_source,
            token_hash=price_agent_token_hash,
        )

    # -- SPA 单壳 catch-all（注册在最后：未匹配 GET 返回壳；API 等前缀 404）-
    @app.get("/{path:path}")
    def spa_fallback(request: Request, path: str):
        if path.startswith(("api/", "auth/", "static/", "healthz")):
            raise HTTPException(status_code=404, detail="not_found")
        return _spa_shell()
