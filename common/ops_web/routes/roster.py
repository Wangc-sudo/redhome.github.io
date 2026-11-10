"""ops-web roster 路由。"""

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

    @app.get("/roster")
    def roster_page():
        """填报名册页壳（Router 渲染）。"""
        return _spa_shell()

    @app.get("/api/roster")
    def api_roster(request: Request):
        """名册 + 区域个人月目标读面。

        ``?month=YYYY-MM`` 指定目标月份（缺省当月）。仅返回 viewer 授权
        （或全局）区域的记录与目标（D4 区域分权）；qudao 渠道门店目标
        不在本 API（另一存储、另一概念）。
        """
        viewer = _resolve_request_viewer(request)
        if not (capability_allows(viewer, CAPABILITY_ROSTER)
                or capability_has_scoped(viewer, CAPABILITY_ROSTER)):
            _forbidden(request)
        request.state.viewer = viewer
        current_month = _current_year_month()
        selected_month = (request.query_params.get("month") or current_month)
        if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", selected_month):
            selected_month = current_month
        try:
            with db_connector() as connection:
                entries = report_roster.fetch_roster(connection)
                audits = report_roster.fetch_roster_audit(connection, limit=20)
                scope_pairs = _region_scope_pairs(connection)
                scopes = [key for key, _ in scope_pairs]
                visible_scopes = visible_region_scopes(viewer, scopes)
                targets = {
                    scope: report_roster.fetch_person_targets(
                        connection, scope, selected_month)
                    for scope in visible_scopes if scope != "qudao"
                }
        except Exception as exc:
            _LOGGER.warning("ops-web roster load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        # 画布区域卡「完成率」分子（2026-10-26 用户裁决「播报回显完成
        # 百分比添加到面板」）：当月完成额按名册在册人名归集到 scope
        # （人名匹配而非 region——代填落群区域也能归到本人，与播报回执
        # 「本月累计/目标，完成 X%」同口径）。恒为当月（current_month），
        # 不随名册页翻月。读失败 fail-soft 略过（完成率格显示「—」，
        # 不拖垮名册读面）。
        completed_person = {}
        try:
            with db_connector() as connection:
                completed_person = report_roster.fetch_month_completed_by_person(
                    connection, current_month)
        except Exception as exc:
            _LOGGER.warning("ops-web roster completed read failed: %s",
                            type(exc).__name__)
        labels = dict(scope_pairs)
        allowed = set(visible_scopes)
        completed = {}
        for row in entries:
            if (row.get("scope") not in allowed
                    or row.get("entity_type") != "person"
                    or not row.get("enabled")):
                continue
            name = row.get("person_name")
            if not name:
                continue
            scope = row["scope"]
            completed[scope] = completed.get(scope, 0.0) \
                + completed_person.get(name, 0.0)
        # updated_at / created_at 是 DB DATETIME（pymysql 返回 datetime 对象），
        # 不经 _fmt_time 归一直接进 JSONResponse 会 json.dumps 失败 → 整接口
        # 500、/roster 页数据面全挂（2026-10-14 带会话线上实测）。出口统一为
        # 北京时间字符串，与 records / overrides 同口径。
        roster = [
            dict(row, scope_label=labels.get(row.get("scope"))
                 or _label(_SCOPE_LABEL, row.get("scope")),
                 updated_at=_fmt_time(row.get("updated_at")))
            for row in entries if row.get("scope") in allowed
        ]
        audits = [
            dict(row, created_at=_fmt_time(row.get("created_at")))
            for row in audits
        ]
        return JSONResponse({
            "status": "ok",
            "month": selected_month,
            "regions": [
                {"region_key": key, "label": label}
                for key, label in scope_pairs if key in allowed
            ],
            "roster": roster,
            "targets": targets,
            "completed": completed,
            "audits": audits,
        })

    @app.get("/api/roster/targets")
    def api_roster_targets(request: Request):
        """某区域当月个人月目标读面（``dim_report_target``，万元）。

        ``?scope=<region>&month=YYYY-MM``（缺省当月）。区域分权：仅该
        scope 的全局 / 区域级 roster 授权可读；qudao 走门店目标表不在此。
        """
        viewer = _resolve_request_viewer(request)
        scope = (request.query_params.get("scope") or "").strip()
        if not capability_allows(viewer, CAPABILITY_ROSTER, scope):
            _forbidden(request)
        request.state.viewer = viewer
        if not scope or scope == "qudao":
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        month = (request.query_params.get("month") or _current_year_month())
        if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
            month = _current_year_month()
        try:
            with db_connector() as connection:
                registered = org_catalog.fetch_region_keys(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web roster region read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if registered and scope not in registered:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                targets = report_roster.fetch_person_targets(
                    connection, scope, month)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web roster targets load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({
            "status": "ok", "scope": scope, "month": month,
            "targets": [
                {"person_name": name, "monthly_target": value}
                for name, value in sorted(targets.items())
            ],
        })

    @app.get("/api/roster/qudao")
    def api_roster_qudao(request: Request):
        """渠道门店一体读面（JSON；替代 SSR 名册页 qudao 段，2026-10-14 D1）。

        门店目标走 raw 通道（单位元，与区域个人月目标「万元」不同口径）；
        含负责人/代填报人名册链接、店铺编号与渠道。
        """
        viewer = _resolve_request_viewer(request)
        if not capability_allows(viewer, CAPABILITY_ROSTER, "qudao"):
            _forbidden(request)
        request.state.viewer = viewer
        try:
            with db_connector() as connection:
                entries = report_roster.fetch_roster(connection)
                targets = channel_target.fetch_store_targets(connection)
                numbers = report_roster.fetch_store_numbers(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web roster qudao load failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        rows = [row for row in entries if row.get("scope") == "qudao"]
        store_rows = {}
        for row in rows:
            store_rows.setdefault(row["entity_key"], []).append(row)
        all_stores = sorted(set(store_rows) | set(targets.keys()))
        stores = []
        for store in all_stores:
            channel, target = targets.get(
                store, (numbers.get(store, ("", None))[0], None))
            links = store_rows.get(store, [])
            stores.append({
                "store": store,
                "channel": channel or "",
                "target": target,
                "store_no": numbers.get(store, (None, None))[1],
                "owners": sorted(r["person_name"] for r in links
                                 if r.get("role") != "deputy"),
                "deputies": sorted(r["person_name"] for r in links
                                   if r.get("role") == "deputy"),
                "links": [dict(r, updated_at=_fmt_time(r.get("updated_at")))
                          for r in sorted(
                              links,
                              key=lambda r: (
                                  1 if r.get("role") == "deputy" else 0,
                                  r["person_name"], int(r["id"])))],
            })
        return JSONResponse({"status": "ok", "stores": stores})

    @app.post("/api/roster/add")
    async def roster_add(request: Request):
        viewer = _resolve_request_viewer(request)
        payload = await _json_body(request)
        aliases = payload.get("aliases") or []
        if not isinstance(aliases, list):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        scope = payload.get("scope")
        # 区域分权（D4）：全局 roster 或该 scope 的区域级授权。
        if not capability_allows(viewer, CAPABILITY_ROSTER, scope):
            _forbidden(request)
        try:
            entry = report_roster.validate_roster_fields(
                scope,
                payload.get("entity_type"),
                payload.get("entity_key"),
                payload.get("person_name"),
                aliases=aliases,
                note=payload.get("note", ""),
                role=payload.get("role") or "owner",
            )
        except report_roster.ReportRosterError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                registered = org_catalog.fetch_region_keys(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web roster region read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        # 区域真源校验（D2）：scope 必须是注册表已登记区域；注册表空 → fail-soft。
        if registered and scope not in registered:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                report_roster.upsert_roster_entry(
                    connection, entry, actor=viewer.userid
                )
        except Exception as exc:
            _LOGGER.warning("ops-web roster add failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/roster/toggle")
    async def roster_toggle(request: Request):
        viewer = require_capability(request, CAPABILITY_ROSTER)
        payload = await _json_body(request)
        roster_id = payload.get("id")
        enabled = payload.get("enabled")
        if (isinstance(roster_id, bool) or not isinstance(roster_id, int)
                or not isinstance(enabled, bool)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                hit = report_roster.set_roster_enabled(
                    connection, roster_id, enabled, actor=viewer.userid
                )
        except Exception as exc:
            _LOGGER.warning("ops-web roster toggle failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not hit:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/roster/delete")
    async def roster_delete(request: Request):
        viewer = require_capability(request, CAPABILITY_ROSTER)
        payload = await _json_body(request)
        roster_id = payload.get("id")
        if isinstance(roster_id, bool) or not isinstance(roster_id, int):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                hit = report_roster.delete_roster_entry(
                    connection, roster_id, actor=viewer.userid
                )
        except Exception as exc:
            _LOGGER.warning("ops-web roster delete failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not hit:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/roster/region-targets")
    async def roster_region_targets(request: Request):
        """保存某区域当月个人月目标（dim_report_target，「所有数据入库」）：
        逐条校验+upsert+审计；空白（null）= 清除该人当月目标。"""
        viewer = _resolve_request_viewer(request)
        payload = await _json_body(request)
        scope = payload.get("scope")
        rows = payload.get("rows")
        # 区域分权（D4）：全局 roster 或该 scope 的区域级授权。
        if not capability_allows(viewer, CAPABILITY_ROSTER, scope):
            _forbidden(request)
        if (not isinstance(scope, str)
                or scope not in report_roster.SCOPES
                or scope == "qudao" or not isinstance(rows, list)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                registered = org_catalog.fetch_region_keys(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web roster region read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        # 区域真源校验（D2）：scope 必须是注册表已登记区域；注册表空 → fail-soft。
        if registered and scope not in registered:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        # 目标月份（2026-10-09 起可指定下月提前录入；缺省当月，非法回退当月）
        year_month = payload.get("year_month") or _current_year_month()
        if not isinstance(year_month, str) or not re.fullmatch(
                r"20\d{2}-(0[1-9]|1[0-2])", year_month):
            year_month = _current_year_month()
        try:
            entries = [
                report_roster.validate_target_fields(
                    scope,
                    (row or {}).get("person_name") if isinstance(row, dict)
                    else None,
                    year_month,
                    (row or {}).get("monthly_target")
                    if isinstance(row, dict) else None,
                )
                for row in rows
            ]
        except report_roster.ReportRosterError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                for entry in entries:
                    report_roster.upsert_report_target(
                        connection, entry, actor=viewer.userid
                    )
        except Exception as exc:
            _LOGGER.warning(
                "ops-web region targets save failed: %s", type(exc).__name__
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "saved": len(entries)})

    @app.post("/api/roster/publish-snapshot")
    async def roster_publish_snapshot(request: Request):
        """发布月度目标快照（S2）：目标覆盖 raw + 名册投影 owners + 触发提取。"""
        viewer = require_admin(request)
        payload = await _json_body(request)
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        # 渠道缺省时从名册编号表反查（页面只对有把握的店带 channel）。
        try:
            with db_connector() as connection:
                numbers = report_roster.fetch_store_numbers(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web publish preflight failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, dict) and not (row.get("channel") or "").strip():
                store = row.get("store_name")
                row["channel"] = numbers.get(store, ("", None))[0]
        try:
            target_rows = channel_target.validate_target_rows(rows)
        except channel_target.ChannelTargetError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as mart_connection:
                raw_connection = connect(settings.dingtalk_database)
                try:
                    deleted, inserted, missing = channel_target.publish_snapshot(
                        raw_connection, target_rows, actor=viewer.userid,
                        roster_connection=mart_connection,
                    )
                finally:
                    raw_connection.close()
                request_id = ops_control.insert_run_request(
                    mart_connection, "extract-mart", viewer.userid
                )
        except Exception as exc:
            _LOGGER.warning("ops-web publish snapshot failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({
            "status": "ok", "deleted": deleted, "inserted": inserted,
            "missing_owners": missing, "extract_request_id": request_id,
        })
