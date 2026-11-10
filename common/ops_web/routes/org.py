"""ops-web org 路由。"""

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
    _fleet = lambda: ctx.fleet()

    @app.get("/api/org/overview")
    def org_overview(request: Request):
        """组织树视图：在职成员 ∘ 覆盖 → regions / pool + 覆盖行 + 审计。

        成员读故障 = 503（组织树无从展示）；覆盖读故障 = fail-soft
        （只丢覆盖，回种子口径，不拖垮整页）。
        """
        viewer = _resolve_request_viewer(request)
        if not (capability_allows(viewer, CAPABILITY_ORG)
                or capability_has_scoped(viewer, CAPABILITY_ORG)):
            _forbidden(request)
        request.state.viewer = viewer
        try:
            with db_connector() as connection:
                members = bi_authz.fetch_active_members(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org members load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        rows = []
        try:
            with db_connector() as connection:
                rows = org_assignment_override.fetch_all_overrides(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org override read failed: %s", type(exc).__name__)
        overrides = {row["user_id"]: row for row in rows if row["enabled"]}
        try:
            with db_connector() as connection:
                catalog = org_catalog.fetch_catalog(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org catalog read failed: %s", type(exc).__name__)
            catalog = {"regions": [], "depts": []}
        overview = _build_org_overview(members, overrides, catalog)
        if not capability_allows(viewer, CAPABILITY_ORG):
            # 区域级 org 授权（2026-10-15 闭环修复）：只回授权区域，且不带
            # 「未归类」池与跨区覆盖行——pool 成员无区域可归因，scoped 视角
            # 无权看；否则 org:<r> 授权就等于全量可见，等于没授权。
            allowed = set(scoped_region_keys(viewer, CAPABILITY_ORG))
            overview["regions"] = [
                region for region in overview["regions"]
                if region["key"] in allowed
            ]
            overview["pool"] = []
            rows = [row for row in rows if row.get("region") in allowed]
        overview["overrides"] = [
            dict(row, updated_at=_fmt_time(row.get("updated_at")),
                 region_label=_org_region_label(row.get("region")))
            for row in rows
        ]
        return JSONResponse(overview)

    @app.post("/api/org/assign")
    async def org_assign(request: Request):
        """新增/更新一条归属覆盖（``region`` 为 null = 移出全部区域）。"""
        viewer = _resolve_request_viewer(request)
        request.state.viewer = viewer
        payload = await _json_body(request)
        user_id = payload.get("user_id")
        region = payload.get("region")
        dept_id = payload.get("dept_id")
        dept_name = payload.get("dept_name")
        name = payload.get("name")
        note = payload.get("note") or ""
        if not isinstance(user_id, str) or not user_id.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if region is not None and not isinstance(region, str):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        for value in (dept_id, dept_name, name, note):
            if value is not None and not isinstance(value, str):
                raise HTTPException(
                    status_code=400, detail=ErrorDetail.BAD_REQUEST)
        # 区域级 org 授权（2026-10-15 闭环修复）：``region=None``（移出全部
        # 区域）是跨区动作，只认全局 org；``region=<r>`` 则全局 org 或
        # ``org:<r>`` 均可——与 /api/org/move 的区域校验同一口径。
        if not capability_allows(viewer, CAPABILITY_ORG, region):
            _forbidden(request)
        try:
            with db_connector() as connection:
                org_assignment_override.upsert_assignment(
                    connection, user_id, region, dept_id=dept_id,
                    dept_name=dept_name, name=name, actor=viewer.userid,
                    note=note)
        except org_assignment_override.OrgAssignmentOverrideError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org assign failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/move")
    async def org_move(request: Request):
        """归属搬迁（**原子复合写**，2026-10-15 闭环画布）。

        一次事务内完成：覆盖 upsert（权威表 ``dim_org_assignment_override``）
        + 双侧 ``person`` 型名册同步 + 审计，避免**半移动态**——名册是
        fail-open 白名单，若人已进新区而旧区名册残留 / 新区无名册，会静默
        剥夺或搞错该成员**报数资格**与**当月目标归属**。

        ``to_region=null`` → 移入未归类池。**仅在目标区已存在启用 person
        名册体系**（fail-open 白名单已生效）且该人不在册时才补录；否则不
        动——避免给原本无名册的区凭空造白名单、把 fail-open 区整体翻成
        强制在册。
        """
        from common.public_data.db import transaction

        viewer = _resolve_request_viewer(request)
        if not (capability_allows(viewer, CAPABILITY_ORG)
                or capability_has_scoped(viewer, CAPABILITY_ORG)):
            _forbidden(request)
        request.state.viewer = viewer
        payload = await _json_body(request)
        user_id = payload.get("user_id")
        to_region = payload.get("to_region")
        person_name = payload.get("person_name")
        dept_name = payload.get("dept_name")
        from_region = payload.get("from_region")
        role = payload.get("role") or "owner"
        note = payload.get("note") or ""
        if not isinstance(user_id, str) or not user_id.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if to_region is not None and (
                not isinstance(to_region, str) or not to_region.strip()):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        for value in (person_name, dept_name, from_region, note):
            if value is not None and not isinstance(value, str):
                raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if from_region is not None and not from_region.strip():
            from_region = None
        if to_region is not None and not capability_allows(
                viewer, CAPABILITY_ORG, to_region):
            _forbidden(request)
        # 区域真源校验：目标区须在注册表已登记（注册表空 → fail-soft）。
        try:
            with db_connector() as connection:
                registered = org_catalog.fetch_region_keys(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org move region read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if to_region is not None and registered and to_region not in registered:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)

        from_removed = False
        to_added = False
        try:
            with db_connector() as connection:
                with transaction(connection):
                    org_assignment_override.apply_assignment(
                        connection, user_id, to_region,
                        dept_id=None, dept_name=dept_name, name=person_name,
                        actor=viewer.userid, note=note)
                    # 旧区：删该人启用中的 person 型名册（仅在册才动）。
                    if from_region and from_region != to_region and person_name:
                        hit = _find_person_roster_entry(
                            connection, from_region, person_name)
                        if hit is not None:
                            report_roster.apply_roster_delete(
                                connection, hit["id"], actor=viewer.userid)
                            from_removed = True
                    # 新区：仅当该区已启用 person 名册体系且该人不在册才补录。
                    if to_region and to_region != from_region and person_name:
                        existing = _find_person_roster_entry(
                            connection, to_region, person_name)
                        if existing is None and (
                                report_roster.fetch_scope_person_names(
                                    connection, to_region) is not None):
                            entry = report_roster.validate_roster_fields(
                                to_region, "person",
                                dept_name or _org_region_label(to_region),
                                person_name, role=role, note=note)
                            report_roster.apply_roster_entry(
                                connection, entry, actor=viewer.userid)
                            to_added = True
        except (report_roster.ReportRosterError,
                org_assignment_override.OrgAssignmentOverrideError):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning("ops-web org move failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({
            "status": "ok", "moved": True,
            "roster": {"from_removed": from_removed, "to_added": to_added},
            "audit": True,
        })

    @app.post("/api/org/region/relay")
    async def org_region_relay(request: Request):
        """播报改投测试区开关（2026-10-19 用户裁决「所有播报都可接入
        测试区，区域接入后该区工作都接入」）。

        请求体 ``{region_key, relay: bool}``：relay=true → 该区域群消息
        与 DING 全部改投测试区群（消息带 [测试→原区域] 前缀标记）；
        false → 清除改投。写 ``dim_org_region.relay_to``（DB 驱动，网关
        30s TTL 内生效，无需重启）；审计 action=set_relay。区域须已登记
        且启用，测试区自身不可改投。
        """
        viewer = _resolve_request_viewer(request)
        payload = await _json_body(request)
        region_key = payload.get("region_key")
        relay = payload.get("relay")
        if not isinstance(region_key, str) or not isinstance(relay, bool):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not capability_allows(viewer, CAPABILITY_ORG, region_key):
            _forbidden(request)
        try:
            with db_connector() as connection:
                affected = org_catalog.set_region_relay(
                    connection, region_key, "test" if relay else None,
                    actor=viewer.userid)
        except org_catalog.OrgCatalogError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except Exception as exc:
            _LOGGER.warning("ops-web region relay save failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not affected:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok", "region_key": region_key,
                             "relay_to": "test" if relay else None})

    @app.post("/api/org/region/broadcast")
    async def org_region_broadcast(request: Request):
        """更新区域播报配置（2026-10-26 权威源迁 DB 的页面入口）。

        body：{region_key, fields: {...白名单字段...}}——未出现的字段不动，
        None=清空该列。审计 update_broadcast。
        """
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        region_key = payload.get("region_key")
        fields = payload.get("fields")
        if not isinstance(region_key, str) or not region_key.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(fields, dict) or not fields:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                changed = org_catalog.update_region_broadcast(
                    connection, region_key.strip(), fields,
                    actor=viewer.userid)
        except org_catalog.OrgCatalogError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except Exception as exc:
            _LOGGER.warning("ops-web region broadcast save failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "changed": list(changed)})

    @app.post("/api/org/region/depts")
    async def org_region_depts(request: Request):
        """全量替换区域的顶层部门映射（采集范围，dim_org_region_dept）。

        body：{region_key, dept_ids: [int,...]}——顺序即展示序。审计
        set_region_depts（detail=增删差集）。
        """
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        region_key = payload.get("region_key")
        dept_ids = payload.get("dept_ids")
        if not isinstance(region_key, str) or not region_key.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(dept_ids, list):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                result = org_catalog.replace_region_depts(
                    connection, region_key.strip(), dept_ids,
                    actor=viewer.userid)
        except org_catalog.OrgCatalogError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except Exception as exc:
            _LOGGER.warning("ops-web region depts save failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", **result})

    @app.post("/api/org/region/add")
    async def org_region_add(request: Request):
        """登记一个区域（消费侧组织目录；即使无成员也进树）。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        region_key = payload.get("region_key")
        label = payload.get("label")
        sort_order = payload.get("sort_order")
        if not isinstance(region_key, str) or not region_key.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(label, str) or not label.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if sort_order is not None and (
                isinstance(sort_order, bool) or not isinstance(sort_order, int)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                org_catalog.upsert_region(
                    connection, region_key, label,
                    sort_order=sort_order or 0, actor=viewer.userid)
        except org_catalog.OrgCatalogError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org region add failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/dept/add")
    async def org_dept_add(request: Request):
        """登记一个区域下的部门（消费侧组织目录；即使无成员也进树）。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        region_key = payload.get("region_key")
        dept_name = payload.get("dept_name")
        dept_id = payload.get("dept_id")
        sort_order = payload.get("sort_order")
        if not isinstance(region_key, str) or not region_key.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(dept_name, str) or not dept_name.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if dept_id is not None and not isinstance(dept_id, str):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if sort_order is not None and (
                isinstance(sort_order, bool) or not isinstance(sort_order, int)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                org_catalog.upsert_dept(
                    connection, region_key, dept_name, dept_id=dept_id,
                    sort_order=sort_order or 0, actor=viewer.userid)
        except org_catalog.OrgCatalogError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org dept add failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/region/disable")
    async def org_region_disable(request: Request):
        """停用一个登记区域（软删：enabled=0）。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        region_key = payload.get("region_key")
        enabled = payload.get("enabled", False)
        if not isinstance(region_key, str) or not region_key.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                org_catalog.set_catalog_enabled(
                    connection, "region", region_key=region_key,
                    enabled=enabled, actor=viewer.userid)
        except org_catalog.OrgCatalogError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org region disable failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/region/delete")
    async def org_region_delete(request: Request):
        """物理删除一个登记区域（级联删除其下部门）。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        region_key = payload.get("region_key")
        if not isinstance(region_key, str) or not region_key.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                org_catalog.delete_catalog_entry(
                    connection, "region", region_key=region_key,
                    actor=viewer.userid)
        except org_catalog.OrgCatalogError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org region delete failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/dept/disable")
    async def org_dept_disable(request: Request):
        """停用一个登记部门（软删：enabled=0）。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        region_key = payload.get("region_key")
        dept_name = payload.get("dept_name")
        enabled = payload.get("enabled", False)
        if not isinstance(region_key, str) or not region_key.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(dept_name, str) or not dept_name.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                org_catalog.set_catalog_enabled(
                    connection, "dept", region_key=region_key,
                    dept_name=dept_name, enabled=enabled, actor=viewer.userid)
        except org_catalog.OrgCatalogError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org dept disable failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/dept/delete")
    async def org_dept_delete(request: Request):
        """物理删除一个登记部门。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        region_key = payload.get("region_key")
        dept_name = payload.get("dept_name")
        if not isinstance(region_key, str) or not region_key.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(dept_name, str) or not dept_name.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                org_catalog.delete_catalog_entry(
                    connection, "dept", region_key=region_key,
                    dept_name=dept_name, actor=viewer.userid)
        except org_catalog.OrgCatalogError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org dept delete failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    # ------------------------------------------------------------------
    # 代填授权（dim_org_proxy_grant，2026-10-21 代填人设计）：代填人=授权
    # 替他人填数（非归属）。admin 全权，非 admin 按本表授权范围。
    # ------------------------------------------------------------------
    @app.get("/api/org/proxy")
    def org_proxy_list(request: Request):
        """代填授权全表（闭环面板「代填授权」区块）。"""
        require_capability(request, CAPABILITY_ORG)
        try:
            with db_connector() as connection:
                grants = org_proxy_grant.fetch_all_proxy_grants(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org proxy list failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"grants": grants})

    @app.post("/api/org/proxy/grant")
    async def org_proxy_grant_upsert(request: Request):
        """新增/更新一条代填授权（代填人 + 被代填区域[，被代填人]）。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        proxy_user_id = payload.get("proxy_user_id")
        proxy_name = payload.get("proxy_name")
        target_region = payload.get("target_region")
        target_person = payload.get("target_person") or ""
        note = payload.get("note") or ""
        if not isinstance(proxy_user_id, str) or not proxy_user_id.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(target_region, str) or not target_region.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        for value in (proxy_name, target_person, note):
            if value is not None and not isinstance(value, str):
                raise HTTPException(
                    status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                org_proxy_grant.upsert_proxy_grant(
                    connection, proxy_user_id.strip(), target_region.strip(),
                    target_person=target_person.strip(), proxy_name=proxy_name,
                    note=note, actor=viewer.userid)
        except org_proxy_grant.OrgProxyGrantError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org proxy grant failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/proxy/toggle")
    async def org_proxy_toggle(request: Request):
        """停用/启用一条代填授权。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        proxy_user_id = payload.get("proxy_user_id")
        target_region = payload.get("target_region")
        target_person = payload.get("target_person") or ""
        enabled = bool(payload.get("enabled"))
        if not proxy_user_id or not target_region:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                ok = org_proxy_grant.set_proxy_grant_enabled(
                    connection, proxy_user_id, target_region,
                    target_person, enabled, actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org proxy toggle failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok" if ok else "not_found"})

    @app.post("/api/org/proxy/delete")
    async def org_proxy_delete(request: Request):
        """删除一条代填授权。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        proxy_user_id = payload.get("proxy_user_id")
        target_region = payload.get("target_region")
        target_person = payload.get("target_person") or ""
        if not proxy_user_id or not target_region:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                ok = org_proxy_grant.delete_proxy_grant(
                    connection, proxy_user_id, target_region,
                    target_person, actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org proxy delete failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok" if ok else "not_found"})

    @app.get("/api/org/broadcast-board")
    def org_broadcast_board(request: Request):
        """播报编排板数据（任务×区域拉线矩阵；注册表为真源，前端即后端）。

        2026-10-21 用户裁决「播报编排大模块」：把播报/催办类任务按
        「任务 × 区域」装配成拉线视图；链接=该区域该任务的启用/播报时间。
        部门级拉线预留（暂不开放）。无区域的平台/应用线任务不进板。
        """
        require_capability(request, CAPABILITY_ORG)
        service_ids = _fleet()
        links = []
        for sid in service_ids:
            try:
                cfg = config_source.get_pipeline(sid)
            except Exception as exc:
                _LOGGER.warning(
                    "ops-web broadcast-board config parse failed: %s",
                    type(exc).__name__)
                continue
            region = _service_region(sid)
            if not region:
                continue
            category = resolve_category(sid, cfg.category)
            if category not in ("播报", "催办"):
                continue
            links.append({
                "service_id": sid,
                "region": region,
                "category": category,
                "task_type": _broadcast_task_type(sid),
                "label": _service_label(sid, cfg.description),
                "enabled": bool(cfg.enabled),
                "schedule": cfg.schedule or "",
            })
        # 区域轴：组织目录启用区域（中文名；含画布登记但暂无播报的区域）
        try:
            with db_connector() as connection:
                catalog = org_catalog.fetch_catalog(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web broadcast-board catalog failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        regions = [
            {"key": r.get("region_key"), "label": r.get("label") or r.get("region_key")}
            for r in (catalog.get("regions") or [])
            if r.get("enabled", True)
        ]
        links.sort(key=lambda x: (x["category"], x["label"]))
        return JSONResponse({"links": links, "regions": regions})

    def _org_override_region(user_id):
        """被操作覆盖行所属区域（无该行 → None）；读故障 503（收敛语义）。

        区域级 org 授权（2026-10-15 闭环修复）用：toggle/delete 只带 user_id，
        必须回读该行区域才能判定是否落在授权范围内。
        """
        try:
            with db_connector() as connection:
                rows = org_assignment_override.fetch_all_overrides(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org override read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        for row in rows:
            if row.get("user_id") == user_id:
                return row.get("region")
        return None

    @app.post("/api/org/toggle")
    async def org_toggle(request: Request):
        """停用/启用一条归属覆盖（按天然键 user_id）。"""
        viewer = _resolve_request_viewer(request)
        request.state.viewer = viewer
        payload = await _json_body(request)
        user_id = payload.get("user_id")
        enabled = payload.get("enabled")
        if (not isinstance(user_id, str) or not user_id.strip()
                or not isinstance(enabled, bool)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not capability_allows(viewer, CAPABILITY_ORG):
            if not capability_allows(
                    viewer, CAPABILITY_ORG, _org_override_region(user_id)):
                _forbidden(request)
        try:
            with db_connector() as connection:
                hit = org_assignment_override.set_assignment_enabled(
                    connection, user_id, enabled, actor=viewer.userid)
        except org_assignment_override.OrgAssignmentOverrideError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org toggle failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not hit:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/delete")
    async def org_delete(request: Request):
        """删除一条归属覆盖（审计留存被删行内容）。"""
        viewer = _resolve_request_viewer(request)
        request.state.viewer = viewer
        payload = await _json_body(request)
        user_id = payload.get("user_id")
        if not isinstance(user_id, str) or not user_id.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not capability_allows(viewer, CAPABILITY_ORG):
            if not capability_allows(
                    viewer, CAPABILITY_ORG, _org_override_region(user_id)):
                _forbidden(request)
        try:
            with db_connector() as connection:
                hit = org_assignment_override.delete_assignment(
                    connection, user_id, actor=viewer.userid)
        except org_assignment_override.OrgAssignmentOverrideError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org delete failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not hit:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/reset")
    async def org_reset(request: Request):
        """回退总开关：清除全部归属覆盖（回种子口径）。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        await _json_body(request)
        try:
            with db_connector() as connection:
                deleted = org_assignment_override.reset_assignments(
                    connection, actor=viewer.userid)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org reset failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "deleted": deleted})

    # -- 区域注册表真源读（D2，2026-10-14）-----------------------------------
    @app.get("/api/regions")
    def api_regions(request: Request):
        """区域/部门目录真源读（``dim_org_region`` / ``dim_org_dept``，启用行）。

        组织编排（``org``）与填报名册（``roster``）两域共用；持有二者任一
        能力（全局或区域级）即可读。目录读失败 → 503（收敛失败语义）。
        """
        viewer = _resolve_request_viewer(request)
        if not (capability_allows(viewer, CAPABILITY_ORG)
                or capability_has_scoped(viewer, CAPABILITY_ORG)
                or capability_allows(viewer, CAPABILITY_ROSTER)
                or capability_has_scoped(viewer, CAPABILITY_ROSTER)):
            _forbidden(request)
        request.state.viewer = viewer
        try:
            with db_connector() as connection:
                catalog = org_catalog.fetch_catalog(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web regions load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        # 榜单页派生生效值（2026-10-26 URL 基址 env 化配套）：库存列清空后
        # 播报链接由 PUBLIC_PAGES_BASE 派生——读面透出实际生效值
        # （leaderboard_url_derived），抽屉编辑块据此标注「留空=自动
        # 派生」，避免空字段被误读为链接丢失。
        from common.region_config import leaderboard_url_for
        regions = catalog.get("regions") or []
        for row in regions:
            broadcast = row.get("broadcast")
            if isinstance(broadcast, dict):
                broadcast["leaderboard_url_derived"] = leaderboard_url_for(
                    row.get("region_key") or "",
                    broadcast.get("leaderboard_url") or "")
        # jsonable_encoder（2026-10-26）：播报列/归属映射入库后 catalog 行
        # 带 datetime（updated_at）——直交 JSONResponse 的 json.dumps 会 500
        # （实测）。编码器统一处理 datetime/Decimal。
        return JSONResponse(jsonable_encoder({
            "status": "ok",
            "regions": regions,
            "depts": catalog.get("depts") or [],
            "region_depts": catalog.get("region_depts") or {},
        }))
