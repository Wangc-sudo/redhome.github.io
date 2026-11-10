"""ops-web audit 路由。"""

from common.ops_web.helpers import *
from common.ops_web.services.audit import *


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
    _fact_region_keys = ctx.fact_region_keys
    _collect_records = ctx.collect_records

    @app.get("/grants")
    def grants_page():
        """授权现状页壳（Router 渲染只读授权 + 审计；写面按 2026-10-13 裁决关闭）。"""
        return _spa_shell()

    @app.get("/audit")
    def audit_page():
        """授权审计页壳（Router 渲染）。"""
        return _spa_shell()

    @app.get("/records")
    def records_page():
        """操作记录页壳（Router 渲染）。"""
        return _spa_shell()

    @app.get("/api/records")
    def api_records(request: Request):
        """六源聚合审计读面（JSON；/records 页数据源，2026-10-14 D1）。

        查询：``source``（来源枚举）、``actor``（操作人，子串）、``action``
        （动作，子串）、``from``/``to``（``YYYY-MM-DD``，含端点）。时间
        倒序，最多 200 条。
        """
        require_capability(request, CAPABILITY_AUDIT)
        source_q = (request.query_params.get("source") or "").strip()
        actor_q = (request.query_params.get("actor") or "").strip()
        action_q = (request.query_params.get("action") or "").strip()
        from_q = (request.query_params.get("from") or "").strip()
        to_q = (request.query_params.get("to") or "").strip()
        if source_q not in _RECORD_SOURCES:
            source_q = ""
        try:
            with db_connector() as connection:
                rows = _collect_records(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web records load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if source_q:
            rows = [r for r in rows if r[1] == source_q]
        if actor_q:
            rows = [r for r in rows if actor_q in (r[2] or "")]
        if action_q:
            rows = [r for r in rows if action_q in (r[3] or "")]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", from_q):
            rows = [r for r in rows if _fmt_time(r[0])[:10] >= from_q]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", to_q):
            rows = [r for r in rows if _fmt_time(r[0])[:10] <= to_q]
        records = [
            {"created_at": _fmt_time(r[0]), "source": r[1], "actor": r[2],
             "action": r[3], "target": r[4], "detail": r[5],
             "user_id": r[6] if len(r) > 6 else ""}
            for r in rows[:200]
        ]
        return JSONResponse({
            "status": "ok", "sources": list(_RECORD_SOURCES),
            "records": records,
        })

    # -- 授权只读面（JSON；/grants 页数据源，2026-10-14 D1）------------------
    @app.get("/api/grants")
    def api_grants(request: Request):
        require_admin(request)
        try:
            with db_connector() as connection:
                grants = bi_authz.fetch_all_grants(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web grants load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        # B5「暂无行级数据」标注（fail-soft：探针失败仅缺徽标，不拖垮读面）；
        # offline_all 豁免——B5 聚合授权键，oa 卡展开为三区并集有数据语义。
        empty_row_regions = []
        try:
            with db_connector() as connection:
                vocab = {key for key, _ in _region_scope_pairs(connection)}
                empty_row_regions = sorted(
                    vocab - _fact_region_keys(connection) - {"offline_all"}
                )
        except Exception as exc:
            _LOGGER.warning(
                "ops-web empty-row region probe failed: %s", type(exc).__name__
            )
        rows = []
        for row in grants:
            if isinstance(row, dict):
                get = row.get
            else:
                get = lambda key, r=row: r[(
                    "user_id", "grant_type", "grant_key", "note",
                    "updated_by", "updated_at").index(key)]
            grant_type = get("grant_type")
            rows.append({
                "user_id": get("user_id"),
                "grant_type": grant_type,
                "grant_type_label": _label(_GRANT_TYPE_LABEL, grant_type),
                "grant_key": get("grant_key"),
                "note": get("note"),
                "created_at": _fmt_time(get("updated_at")),
            })
        return JSONResponse({
            "status": "ok",
            "grants": rows,
            "empty_row_regions": empty_row_regions,
        })

    @app.get("/api/audit")
    def api_audit(request: Request):
        require_admin(request)
        try:
            with db_connector() as connection:
                entries = bi_authz.fetch_audit_rows(connection, limit=200)
        except Exception as exc:
            _LOGGER.warning("ops-web audit load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        rows = []
        for row in entries:
            if isinstance(row, dict):
                values = (row.get("actor"), row.get("action"), row.get("user_id"),
                          row.get("grant_type"), row.get("grant_key"),
                          row.get("note"), row.get("created_at"))
            else:
                values = row
            actor, action, user_id, grant_type, grant_key, note, created_at = values
            rows.append({
                "created_at": _fmt_time(created_at),
                "actor": actor,
                "action": action,
                "action_label": _label(_ACTION_LABEL, action),
                "user_id": user_id,
                "grant_type": grant_type,
                "grant_type_label": _label(_GRANT_TYPE_LABEL, grant_type),
                "grant_key": grant_key,
                "note": note,
            })
        return JSONResponse({"status": "ok", "rows": rows})

    # -- 写 API（事务内两写：grant + audit；actor 来自 session）--------------
    @app.post("/api/grants")
    async def grant_create(request: Request):
        viewer = require_admin(request)
        payload = await _json_body(request)
        try:
            record = bi_authz.validate_grant_fields(
                payload.get("user_id"),
                payload.get("grant_type"),
                payload.get("grant_key"),
                payload.get("note", ""),
            )
        except bi_authz.BiAuthzError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                bi_authz.upsert_grant(connection, record, actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning("ops-web grant write failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/grants/delete")
    async def grant_delete(request: Request):
        viewer = require_admin(request)
        payload = await _json_body(request)
        user_id = payload.get("user_id")
        grant_type = payload.get("grant_type")
        grant_key = payload.get("grant_key")
        if (not isinstance(user_id, str) or not user_id
                or grant_type not in bi_authz.GRANT_TYPES
                or not isinstance(grant_key, str) or not grant_key):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                deleted = bi_authz.delete_grant(
                    connection, user_id, grant_type, grant_key,
                    actor=viewer.userid,
                )
        except Exception as exc:
            _LOGGER.warning("ops-web grant delete failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "deleted": deleted})

    @app.post("/api/grants/region")
    async def grant_region(request: Request):
        """按区域一键开通：候选名单读 dim_robot_member，逐行 upsert + audit。"""
        viewer = require_admin(request)
        payload = await _json_body(request)
        region = payload.get("region")
        if not isinstance(region, str) or not region.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        region = region.strip()
        try:
            with db_connector() as connection:
                members = bi_authz.fetch_active_members(connection)
                written = 0
                for user_id, _name, member_region, _dept in members:
                    if member_region != region:
                        continue
                    bi_authz.upsert_grant(
                        connection,
                        bi_authz.GrantRecord(
                            user_id, "region", region, f"按区域批量开通（{region}）"
                        ),
                        actor=viewer.userid,
                    )
                    written += 1
        except Exception as exc:
            _LOGGER.warning("ops-web region grant failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "written": written})
