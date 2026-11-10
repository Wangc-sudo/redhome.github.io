"""ops-web 应用装配与兼容导出入口。"""

from types import SimpleNamespace

from common.ops_web.helpers import *
from common.ops_web.services.audit import *
from common.ops_web.routes import (
    audit as audit_routes,
    auth as auth_routes,
    calendar as calendar_routes,
    core as core_routes,
    data_fix as data_fix_routes,
    org as org_routes,
    phones as phones_routes,
    pipelines as pipelines_routes,
    roster as roster_routes,
)


def create_app(*, settings, session_secret, db_connector=None, auth_client=None,
               viewer_resolver=None, session_secure=False,
               browser_login_token=None,
               fleet_source=None, config_source=None,
               config_publisher=None, config_deleter=None,
               corp_id=None, agent_id=None, auth_disabled=False,
               data_quality_source_connector=None,
               price_agent_config_source=None, price_agent_token_hash=None) -> FastAPI:
    """创建应用；各业务域路由保持原有 URL、权限与响应契约。"""
    if not session_secret:
        raise ValueError("ops-web requires a session secret")
    if db_connector is None:
        db_connector = _mart_connector(settings)
    if viewer_resolver is None:
        viewer_resolver = authz.ViewerResolver(db_connector)

    app = FastAPI(title="ops-web")
    if os.path.isdir(_ORG_SPA_DIR):
        app.mount("/static", StaticFiles(directory=_ORG_SPA_DIR), name="static")

    ctx = SimpleNamespace(
        settings=settings,
        session_secret=session_secret,
        db_connector=db_connector,
        auth_client=auth_client,
        viewer_resolver=viewer_resolver,
        session_secure=session_secure,
        browser_login_token=browser_login_token,
        fleet_source=fleet_source,
        config_source=config_source,
        config_publisher=config_publisher,
        config_deleter=config_deleter,
        corp_id=corp_id,
        agent_id=agent_id,
        auth_disabled=auth_disabled,
        data_quality_source_connector=data_quality_source_connector,
        price_agent_config_source=price_agent_config_source,
        price_agent_token_hash=price_agent_token_hash,
        connect=lambda database_settings: connect(database_settings),
        fact_region_keys=lambda connection: _fact_region_keys(connection),
        collect_records=lambda *args: _collect_records(*args),
        org_spa_index=lambda: _ORG_SPA_INDEX,
    )

    auth_routes.register_routes(app, ctx)
    core_routes.register_routes(app, ctx)
    org_routes.register_routes(app, ctx)
    audit_routes.register_routes(app, ctx)
    pipelines_routes.register_routes(app, ctx)
    data_fix_routes.register_routes(app, ctx)
    roster_routes.register_routes(app, ctx)
    phones_routes.register_routes(app, ctx)
    calendar_routes.register_routes(app, ctx)
    core_routes.register_system_routes(app, ctx)
    return app


def serve(application):
    import uvicorn
    uvicorn.run(application, host="0.0.0.0", port=8080)


def main():
    """从环境装配并启动；启动失败只打印一行安全消息（同 bi-web 纪律）。"""
    try:
        from common.public_data.pipeline_config import (
            build_config_deleter,
            build_config_publisher,
            build_config_source,
        )
        from common.public_data.scheduler import build_fleet_source
        from common.price_agent.config import build_config_source as build_price_agent_config

        settings = load_settings()
        session_secret = os.environ.get("BI_WEB_SESSION_SECRET") or None
        app_key = (os.environ.get("BI_DINGTALK_APPKEY") or "").strip()
        app_secret = (os.environ.get("BI_DINGTALK_APPSECRET") or "").strip()
        auth_client = (
            auth.DingTalkAuthClient(app_key, app_secret)
            if session_secret and app_key and app_secret
            else None
        )
        session_secure = os.environ.get(
            "BI_WEB_SESSION_SECURE", ""
        ).strip().lower() in ("1", "true", "yes")
        # 浏览器登录通道（2026-10-14）：非空才开放；凭据与其余 BI_* 同纪律
        # ——宿主机 /opt/dops/ops-web.env 注入，不入库、不入 repo。
        browser_login_token = (
            os.environ.get("OPS_WEB_BROWSER_TOKEN") or ""
        ).strip() or None
        corp_id = (os.environ.get("BI_DINGTALK_CORPID") or "").strip() or None
        agent_id = (os.environ.get("BI_DINGTALK_AGENTID") or "").strip() or None
        config_source = build_config_source()
        try:
            fleet_source = build_fleet_source()
        except Exception:
            # 舰队清单地基（seed）缺失不拖垮权限管理面：定时任务页 503。
            _LOGGER.warning("ops-web fleet source unavailable at startup")
            fleet_source = None
        # 认证总开关（2026-10-26 用户裁决，读写分离版）：**缺省读面直通**
        # （查看匿名开放；写操作仍要登录会话+授权）；回开全量认证 =
        # 设 OPS_WEB_AUTH_DISABLED=0（宿主机 ops-web.env），无需改代码。
        auth_disabled = os.environ.get(
            "OPS_WEB_AUTH_DISABLED", "1"
        ).strip().lower() not in ("0", "false", "no")
        application = create_app(
            settings=settings,
            session_secret=session_secret,
            auth_client=auth_client,
            session_secure=session_secure,
            browser_login_token=browser_login_token,
            fleet_source=fleet_source,
            config_source=config_source,
            config_publisher=build_config_publisher(),
            config_deleter=build_config_deleter(),
            corp_id=corp_id,
            auth_disabled=auth_disabled,
            agent_id=agent_id,
            price_agent_config_source=build_price_agent_config(),
            price_agent_token_hash=(
                os.environ.get("PRICE_AGENT_TOKEN_SHA256") or ""
            ).strip().lower() or None,
        )
    except Exception as exc:
        print(f"ops-web startup failed: invalid configuration ({type(exc).__name__})")
        sys.exit(1)
    serve(application)


if __name__ == "__main__":  # pragma: no cover
    main()
