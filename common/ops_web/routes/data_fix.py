"""ops-web data_fix 路由。"""

from common.ops_web.helpers import *
from common.ops_web.services.audit import _audit_get


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

    @app.post("/api/data-fix/realign")
    async def data_fix_realign(request: Request):
        """线下事实行人员归属归位（preview/apply 一体）。

        请求体 ``{person_name, from_region, to_region, apply?}``：
        ``apply`` 缺省 false = 预览（命中行数/金额/日期范围，零写入）；
        true = 同事务 UPDATE + 审计。区域必须都在区域注册表内且不相同；
        零命中不落审计（空操作不算修正）。
        """
        viewer = require_capability(request, CAPABILITY_DATAFIX)
        payload = await _json_body(request)
        person_name = (payload.get("person_name") or "").strip()
        from_region = (payload.get("from_region") or "").strip()
        to_region = (payload.get("to_region") or "").strip()
        apply = bool(payload.get("apply"))
        if not person_name or not from_region or not to_region:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if from_region == to_region:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                known = set(org_catalog.fetch_region_keys(connection))
                if from_region not in known or to_region not in known:
                    raise HTTPException(
                        status_code=400, detail=ErrorDetail.BAD_REQUEST)
                if apply:
                    stats = data_fix.realign_offline_person_region(
                        connection, person_name=person_name,
                        from_region=from_region, to_region=to_region,
                        actor=viewer.userid)
                else:
                    stats = data_fix.preview_person_region_realign(
                        connection, person_name=person_name,
                        from_region=from_region, to_region=to_region)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning("ops-web data fix failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "applied": apply, **stats})

    @app.get("/api/data-fix/audits")
    def api_data_fix_audits(request: Request):
        """数据修正审计读面（JSON，created_at 倒序最多 50 条）。"""
        require_capability(request, CAPABILITY_DATAFIX)
        try:
            with db_connector() as connection:
                rows = data_fix.fetch_recent_audits(connection, limit=50)
        except Exception as exc:
            _LOGGER.warning("ops-web data fix audit load failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        get = lambda row: _audit_get(row, ("actor", "action", "fix_type",
                                           "target", "detail", "row_count",
                                           "created_at"))
        return JSONResponse({
            "status": "ok",
            "audits": [{
                "created_at": _fmt_time(get(row)("created_at")),
                "actor": get(row)("actor"),
                "action": get(row)("action"),
                "action_label": _label(_ACTION_LABEL, get(row)("action")),
                "fix_type": get(row)("fix_type"),
                "fix_type_label": _label(_FIX_TYPE_LABEL, get(row)("fix_type")),
                "target": get(row)("target"),
                "detail": get(row)("detail"),
                "row_count": get(row)("row_count"),
            } for row in rows],
        })
