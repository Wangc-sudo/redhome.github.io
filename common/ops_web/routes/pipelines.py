"""ops-web pipelines 路由。"""

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

    def _fleet():
        """舰队清单；未接线或读取失败 -> 503（页面与写 API 共用）。"""
        if fleet_source is None or config_source is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            return tuple(fleet_source())
        except Exception as exc:
            _LOGGER.warning("ops-web fleet load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)

    def _pipelines_view():
        """定时任务视图装配（JSON；/api/pipelines 数据源，2026-10-14 D1）。

        注册表（Nacos）为真源：单条畸形按默认配置兜底不拖垮整页；最近运行取
        ``ops_control.fetch_recent_requests``（id DESC 首见即最新）；催办行
        「收受影响人」fail-soft（名册/通讯录读失败只该列为空口径）。
        返回 ``(groups, group_order, supported_categories)``。
        """
        service_ids = _fleet()
        configs = []
        for service_id in service_ids:
            try:
                configs.append(config_source.get_pipeline(service_id))
            except Exception as exc:
                _LOGGER.warning(
                    "ops-web pipeline config parse failed: %s", type(exc).__name__)
                configs.append(PipelineConfig(service_id))
        try:
            with db_connector() as connection:
                recent = ops_control.fetch_recent_requests(connection, limit=100)
        except Exception as exc:
            _LOGGER.warning("ops-web run requests load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        latest = {}
        for row in recent:  # fetch 按 id DESC，首见即最新
            sid = row.get("service_id") if isinstance(row, dict) else row[1]
            if sid not in latest:
                latest[sid] = row
        # 收受影响人（催办行）：附属信息 fail-soft——名册/通讯录读取故障只影响
        # 本列，不拖垮整页。
        reminder_cells = {}
        reminder_ids = [
            config.service_id for config in configs
            if resolve_category(config.service_id, config.category) == "催办"
        ]
        if reminder_ids:
            members_by_region = {}
            roster_by_region = {}
            uid_to_name = {}
            overrides_by_service = {}
            qudao_owners = None
            try:
                with db_connector() as connection:
                    robot_regions = {
                        _service_region(sid) for sid in reminder_ids
                        if sid.startswith("robot-")
                    }
                    if robot_regions:
                        members = bi_authz.fetch_active_members(connection)
                        try:
                            org_overrides = \
                                org_assignment_override.fetch_overrides(
                                    connection)
                        except Exception as exc:
                            _LOGGER.warning(
                                "ops-web org assignment override load "
                                "failed: %s", type(exc).__name__)
                            org_overrides = {}
                        members_by_region = _effective_members_by_region(
                            members, org_overrides)
                        for uid, name, _reg, _dept in members:
                            uid_to_name.setdefault(uid, name)
                        for region in robot_regions:
                            roster_by_region[region] = \
                                report_roster.fetch_scope_person_names(
                                    connection, region)
                    if any(not sid.startswith("robot-")
                           and _service_region(sid) == "qudao"
                           for sid in reminder_ids):
                        qudao_owners = report_roster.fetch_store_owner_map(
                            connection, "qudao", roles=("owner",))
                    for sid in reminder_ids:
                        try:
                            overrides_by_service[sid] = \
                                recipient_override.fetch_overrides(
                                    connection, sid)
                        except Exception as exc:
                            _LOGGER.warning(
                                "ops-web recipient override load failed: %s",
                                type(exc).__name__)
                            overrides_by_service[sid] = ()
                for sid in reminder_ids:
                    reminder_cells[sid] = _reminder_targets_cell(
                        sid, members_by_region=members_by_region,
                        roster_by_region=roster_by_region,
                        qudao_owners=qudao_owners,
                        overrides=overrides_by_service.get(sid, ()),
                        uid_to_name=uid_to_name)
            except Exception as exc:
                _LOGGER.warning(
                    "ops-web reminder targets load failed: %s",
                    type(exc).__name__)
                for sid in reminder_ids:
                    reminder_cells[sid] = (
                        "<span class=\"hint\">读取失败</span>")
        buckets = {}
        for config in configs:
            request_row = latest.get(config.service_id)
            latest_out = None
            if request_row is not None:
                if isinstance(request_row, dict):
                    get = request_row.get
                else:
                    get = lambda key, r=request_row: r[
                        ("id", "service_id", "requested_by", "status", "note",
                         "exit_code", "created_at", "finished_at").index(key)]
                latest_out = {
                    "status": get("status"),
                    "status_label": _label(_RUN_STATUS_LABEL, get("status")),
                    "requested_by": get("requested_by"),
                    "created_at": _fmt_time(get("created_at")),
                }
            category = resolve_category(config.service_id, config.category)
            buckets.setdefault(_service_region(config.service_id), []).append({
                "service_id": config.service_id,
                "service_name": _service_name(config.service_id),
                "service_label": _service_label(
                    config.service_id, config.description),
                "category_label": _category_label(config),
                "is_reminder": category == "催办",
                "reminder_html": reminder_cells.get(config.service_id),
                "enabled": bool(config.enabled),
                "schedule": config.schedule or "",
                "schedule_zh": _cron_zh(config.schedule) or "",
                "depends_on": list(config.depends_on or []),
                "params": dict(config.params or {}),
                "alertable": config.service_id in ALERT_SERVICES,
                "description": config.description or "",
                "disabled_reason": config.disabled_reason or "",
                "review_after": config.review_after or "",
                "has_command": bool(has_command(config.service_id)),
                "latest": latest_out,
            })
        # 分组顺序：城市区域 → 跨区域线 → 未登记区域 → 平台/无群（恒置末位）。
        ordered = [r for r in _GROUP_ORDER if buckets.get(r)]
        ordered += sorted(
            r for r in buckets
            if r is not None and r not in _GROUP_ORDER and buckets[r])
        if buckets.get(None):
            ordered.append(None)
        groups = [
            {"region": region,
             "title": _pipeline_group_title(region),
             "count": len(buckets[region]),
             "services": buckets[region]}
            for region in ordered
        ]
        return groups, list(_GROUP_ORDER), list(SUPPORTED_CATEGORIES)

    @app.get("/api/pipelines")
    def api_pipelines(request: Request):
        """定时任务读面（JSON；替代 SSR /pipelines 页数据，2026-10-14 D1）。"""
        require_capability(request, CAPABILITY_PIPELINES)
        groups, group_order, supported = _pipelines_view()
        return JSONResponse({
            "status": "ok",
            "groups": groups,
            "group_order": group_order,
            "supported_categories": supported,
        })

    @app.get("/pipelines")
    def pipelines_page():
        """定时任务页壳（Router 渲染）。"""
        return _spa_shell()

    @app.get("/api/pipelines/audit")
    def api_pipelines_audit(request: Request):
        """定时任务审计读面（JSON；替代 SSR /pipelines/audit 页，2026-10-14 D1）。"""
        require_capability(request, CAPABILITY_PIPELINES)
        try:
            with db_connector() as connection:
                entries = ops_control.fetch_pipeline_audit(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline audit load failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        rows = []
        for entry in entries:
            if isinstance(entry, dict):
                values = (entry.get("actor"), entry.get("action"),
                          entry.get("service_id"), entry.get("detail"),
                          entry.get("created_at"))
            else:
                values = entry
            actor, action, service_id, detail, created_at = values
            rows.append({
                "created_at": _fmt_time(created_at),
                "actor": actor,
                "action": action,
                "action_label": _label(_ACTION_LABEL, action),
                "service_id": service_id,
                "service_name": _service_name(service_id),
                "detail": detail,
            })
        return JSONResponse({"status": "ok", "rows": rows})

    @app.get("/pipelines/audit")
    def pipeline_audit_page():
        """任务审计页壳（Router 渲染）。"""
        return _spa_shell()

    @app.get("/api/pipelines/history")
    def api_pipelines_history(request: Request):
        """定时任务执行流水读面（JSON；替代 SSR /pipelines/history 页）。"""
        require_capability(request, CAPABILITY_PIPELINES)
        service_filter = (request.query_params.get("service_id") or "").strip()
        try:
            with db_connector() as connection:
                entries = ops_control.fetch_run_history(
                    connection, service_id=service_filter or None)
        except ops_control.OpsControlError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except Exception as exc:
            _LOGGER.warning("ops-web run history load failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        history_keys = ("id", "service_id", "trigger_type", "status",
                        "exit_code", "note", "started_at", "finished_at")
        rows = []
        for row in entries:
            if isinstance(row, dict):
                get = row.get
            else:
                get = lambda key, r=row: r[history_keys.index(key)]
            rows.append({
                "service_id": get("service_id"),
                "service_name": _service_name(get("service_id")),
                "trigger_type": get("trigger_type"),
                "trigger_label": _label(_TRIGGER_LABEL, get("trigger_type")),
                "status": get("status"),
                "status_label": _label(_RUN_STATUS_LABEL, get("status")),
                "exit_code": get("exit_code"),
                "started_at": _fmt_time(get("started_at")),
                "finished_at": _fmt_time(get("finished_at")),
                "note": get("note"),
            })
        return JSONResponse({
            "status": "ok", "service_id": service_filter, "rows": rows})

    @app.get("/pipelines/history")
    def pipeline_history_page():
        """执行流水页壳（Router 渲染）。"""
        return _spa_shell()

    @app.post("/api/pipelines/toggle")
    async def pipeline_toggle(request: Request):
        """启停（2026-10-17 治理 P0）：停用必须带原因（reason），可选复查日
        （review_after，YYYY-MM-DD）；启用自动清空治理字段。重复停用同态
        请求=治理元数据补录（enabled 不变、字段更新，审计照常）。"""
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        if config_publisher is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        enabled = payload.get("enabled")
        if (not ops_control.SERVICE_ID_RE.match(service_id or "")
                or not isinstance(enabled, bool)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if service_id not in _fleet():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        reason = (payload.get("reason") or "").strip()
        review_after = (payload.get("review_after") or "").strip()
        if not enabled and not reason:
            # 停用不带原因=没有上下文的死配置（治理门禁）
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if review_after:
            try:
                from datetime import date as _date
                _date.fromisoformat(review_after)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config = config_source.get_pipeline(service_id)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            config_publisher(replace(
                config, enabled=enabled,
                disabled_reason="" if enabled else reason,
                review_after="" if enabled else review_after,
            ))
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline publish failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        detail = config.schedule or ""
        if not enabled:
            detail = f"{reason}"
            if review_after:
                detail += f"（复查 {review_after}）"
        try:
            with db_connector() as connection:
                ops_control.insert_pipeline_audit(
                    connection, viewer.userid,
                    "enable" if enabled else "disable", service_id,
                    detail=detail,
                )
        except Exception as exc:
            # 发布已生效而审计没落库：变更存在但流水缺失，必须显眼。
            _LOGGER.error(
                "ops-web pipeline audit write failed AFTER publish: %s",
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/pipelines/archive")
    async def pipeline_archive(request: Request):
        """归档（2026-10-19 治理 P2 裁决）：Nacos 摘除条目 + 审计留痕。

        前置门禁：**只允许归档已停用的服务**（先停用后归档，防误删活线）。
        归档语义 = Nacos 摘除 + seed 删条目（代码 PR）+ git 历史即档案；
        响应带 ``seed_registered`` 提示——seed 里的条目不在本端点职责内，
        须同 PR 删除，否则下次 ``publish-pipelines --if-missing`` 会复活。
        """
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        if config_deleter is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        if not ops_control.SERVICE_ID_RE.match(service_id or ""):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if service_id not in _fleet():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config = config_source.get_pipeline(service_id)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if config.enabled:
            # 活线不可归档：先走停用（带原因）再归档
            raise HTTPException(status_code=409, detail=ErrorDetail.CONFLICT)
        seed_path = (os.environ.get("PUBLIC_DATA_PIPELINE_SEED") or "").strip()
        seed_registered = False
        if seed_path:
            try:
                from common.public_data.pipeline_config import load_seed
                seed_registered = service_id in load_seed(seed_path)
            except Exception:
                _LOGGER.warning("ops-web seed probe failed: archive anyway")
        try:
            config_deleter(service_id)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline archive failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            with db_connector() as connection:
                ops_control.insert_pipeline_audit(
                    connection, viewer.userid, "archive", service_id,
                    detail=config.disabled_reason or "",
                )
        except Exception as exc:
            # 删除已生效而审计没落库：变更存在但流水缺失，必须显眼。
            _LOGGER.error(
                "ops-web pipeline audit write failed AFTER archive: %s",
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok",
                             "seed_registered": seed_registered})

    @app.post("/api/pipelines/update")
    async def pipeline_schedule_update(request: Request):
        """修改既有线的播报时间（read-modify-write，只换 cron 的分/时两段）。

        与 toggle 同型：读注册表 → 改 → 整条回写 → 审计同生共死。请求体
        ``{service_id, times:[{hour, minute}, ...]}``（1..3 个 HH:MM 时点，
        任意分 0-59）；保留原 cron 的日/月/周（取首段尾段），因此「工作日
        18:00」「每月 1 日」这类形态不被抹平成每天；多时点回写为 ``;`` 分隔
        的多段 cron（调度器按并集触发）。
        """
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        if config_publisher is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        times = payload.get("times")
        if (not ops_control.SERVICE_ID_RE.match(service_id or "")
                or not isinstance(times, list)
                or not 1 <= len(times) <= 3):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        clean = []
        for item in times:
            if not isinstance(item, dict):
                raise HTTPException(status_code=400,
                                    detail=ErrorDetail.BAD_REQUEST)
            hour, minute = item.get("hour"), item.get("minute")
            if (not isinstance(hour, int) or isinstance(hour, bool)
                    or not isinstance(minute, int) or isinstance(minute, bool)
                    or not 0 <= hour <= 23 or not 0 <= minute <= 59):
                raise HTTPException(status_code=400,
                                    detail=ErrorDetail.BAD_REQUEST)
            clean.append((hour, minute))
        clean = sorted(set(clean))
        if service_id not in _fleet():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config = config_source.get_pipeline(service_id)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        # 尾段取现有 cron 首段(分 时 日 月 周)的第 2/3/4 字段，保留 日/月/周；
        # 非 5 段（空/常驻/异常）回落每天，避免拼出非法表达式。
        fields = (config.schedule or "").split(";")[0].split()
        if len(fields) == 5:
            tail = (fields[2], fields[3], fields[4])
        else:
            tail = ("*", "*", "*")
        new_cron = ";".join(
            f"{minute} {hour} {tail[0]} {tail[1]} {tail[2]}"
            for hour, minute in clean
        )
        try:
            # 只借它做 croniter 校验；返回值会丢 sources/reads，不用于发布。
            ops_control.validate_pipeline_fields(
                service_id, config.kind, new_cron,
                enabled=config.enabled, description=config.description,
                depends_on=config.depends_on, category=config.category,
            )
        except ops_control.OpsControlError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config_publisher(replace(config, schedule=new_cron))
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline publish failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            with db_connector() as connection:
                ops_control.insert_pipeline_audit(
                    connection, viewer.userid, "schedule", service_id,
                    detail=new_cron,
                )
        except Exception as exc:
            _LOGGER.error(
                "ops-web pipeline audit write failed AFTER publish: %s",
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "schedule": new_cron})

    @app.post("/api/pipelines/params")
    async def pipeline_params_update(request: Request):
        """修改既有线的线级参数 ``params``（read-modify-write + 审计）。

        巡检告警线的阈值/冷却即落此：请求体 ``{service_id, params:{...}}``，
        ``params`` 传 ``{}`` 即清空。只覆盖 ``params`` 字段，其余运行态
        （enabled/schedule/description…）原样保留，与改定时同型。
        """
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        if config_publisher is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        if not ops_control.SERVICE_ID_RE.match(service_id or ""):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if service_id not in _fleet():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        # 必须显式给 dict（清空传 {}）；缺键/None/非映射一律 400，
        # 避免客户端漏传把既有 params 静默清空。
        raw_params = payload.get("params")
        if not isinstance(raw_params, dict):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            params = ops_control.validate_pipeline_params(raw_params)
        except ops_control.OpsControlError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config = config_source.get_pipeline(service_id)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            config_publisher(replace(config, params=params))
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline publish failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        detail = "; ".join(
            f"{key}={value}" for key, value in sorted(params.items())
        ) or "cleared"
        try:
            with db_connector() as connection:
                ops_control.insert_pipeline_audit(
                    connection, viewer.userid, "params", service_id,
                    detail=detail,
                )
        except Exception as exc:
            _LOGGER.error(
                "ops-web pipeline audit write failed AFTER publish: %s",
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "params": params})

    @app.post("/api/pipelines/deps")
    async def pipeline_deps_update(request: Request):
        """修改既有线的依赖 ``depends_on``（画布拉线：模块口→模块口）。

        请求体 ``{service_id, depends_on:[<service_id>, ...]}``——全量覆盖
        该线的依赖集（画布侧做增量加减后整体提交）；依赖必须都在舰队内且
        不等于自身；重复自动去重（保序）。read-modify-write + 审计，
        与改定时/改参数同型。
        """
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        if config_publisher is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        depends_on = payload.get("depends_on")
        if (not ops_control.SERVICE_ID_RE.match(service_id or "")
                or not isinstance(depends_on, list)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        fleet = set(_fleet())
        if service_id not in fleet:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        clean = []
        for dep in depends_on:
            if (not isinstance(dep, str)
                    or not ops_control.SERVICE_ID_RE.match(dep)
                    or dep not in fleet
                    or dep == service_id):
                raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
            if dep not in clean:
                clean.append(dep)
        try:
            config = config_source.get_pipeline(service_id)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            # 借校验器过一遍（croniter 等全字段），返回值不用于发布。
            ops_control.validate_pipeline_fields(
                service_id, config.kind, config.schedule,
                enabled=config.enabled, description=config.description,
                depends_on=clean, category=config.category,
            )
        except ops_control.OpsControlError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config_publisher(replace(config, depends_on=clean))
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline publish failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            with db_connector() as connection:
                ops_control.insert_pipeline_audit(
                    connection, viewer.userid, "deps", service_id,
                    detail=",".join(clean) or "cleared",
                )
        except Exception as exc:
            _LOGGER.error(
                "ops-web pipeline audit write failed AFTER publish: %s",
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "depends_on": clean})

    @app.get("/api/pipelines/health")
    def api_pipelines_health(request: Request):
        """舰队健康读面（治理抽屉用）：每服务最近一次真实 cron 运行。

        真源 ``pd_ops_run_history``（调度器落表）；注意与
        ``pd_ops_run_request``（手动「运行一次」请求）区分——治理的僵尸
        检测必须用前者，2026-10-17 P1 误报事故即混用所致。
        返回 ``{service_id: {"started_at", "status"}}``。
        """
        require_capability(request, CAPABILITY_PIPELINES)
        try:
            with db_connector() as connection:
                rows = ops_control.fetch_run_history(connection, limit=500)
        except Exception as exc:
            _LOGGER.warning("ops-web run history load failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        latest = {}
        for row in rows:  # id DESC，首见即最新
            get = (row.get if isinstance(row, dict)
                   else None)
            if get is None:
                sid, status, started = row[1], row[3], row[6]
            else:
                sid, status, started = (
                    get("service_id"), get("status"), get("started_at"))
            if sid not in latest:
                latest[sid] = {
                    "started_at": _fmt_time(started),
                    "status": status,
                }
        return JSONResponse({"status": "ok", "latest": latest})

    @app.get("/api/pipelines/fleet-audit-latest")
    def api_pipelines_fleet_audit_latest(request: Request):
        """最近一次舰队月检快照（治理抽屉「上次月检快照」节数据源）。

        真源 ``pd_ops_fleet_audit``（fleet-audit 管道每月 2 日落一行）；
        从未跑过 → ``snapshot=None``（前端显示「未跑过」而非报错）。
        """
        require_capability(request, CAPABILITY_PIPELINES)
        try:
            with db_connector() as connection:
                snapshot = fleet_audit.fetch_latest_snapshot(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web fleet-audit snapshot load failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "snapshot": snapshot})

    @app.get("/api/pipelines/sla")
    def api_pipelines_sla(request: Request):
        """当日 SLA 迟到读面（治理抽屉「SLA 迟到」节；2026-10-19 治理 P2
        尾巴，用户裁决「不要播报」——只进本读面与月检快照，不发群）。

        判定在服务端（fleet_audit.collect_sla_status，与月检同一函数），
        前端纯渲染，避免 JS/Python 双口径漂移。返回
        ``{violations: [{service_id, deadline}], checked_at}``。
        """
        require_capability(request, CAPABILITY_PIPELINES)
        service_ids = _fleet()
        try:
            violations, now_hm = fleet_audit.collect_sla_status(
                lambda _db: db_connector(), settings.mart_database,
                fleet_source=lambda: service_ids,
                config_source=config_source,
                has_command=has_command,
            )
        except Exception as exc:
            _LOGGER.warning("ops-web sla check failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({
            "status": "ok",
            "checked_at": now_hm,
            "violations": violations,
        })

    @app.post("/api/pipelines/add")
    async def pipeline_add(request: Request):
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        if config_publisher is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        payload = await _json_body(request)
        depends_on = payload.get("depends_on") or []
        if not isinstance(depends_on, list):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config = ops_control.validate_pipeline_fields(
                payload.get("service_id"),
                payload.get("kind"),
                payload.get("schedule"),
                enabled=payload.get("enabled", True),
                description=payload.get("description", ""),
                depends_on=depends_on,
                category=payload.get("category") or None,
                params=payload.get("params"),
            )
        except ops_control.OpsControlError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if config.service_id in _fleet():
            # 已存在的线走「操作」列开关；重复新增会静默覆盖 Nacos 条目。
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config_publisher(config)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline publish failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            with db_connector() as connection:
                ops_control.insert_pipeline_audit(
                    connection, viewer.userid, "add", config.service_id,
                    detail=config.schedule or "",
                )
        except Exception as exc:
            _LOGGER.error(
                "ops-web pipeline audit write failed AFTER publish: %s",
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/pipelines/run-once")
    async def pipeline_run_once(request: Request):
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        if not ops_control.SERVICE_ID_RE.match(service_id or ""):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not has_command(service_id):
            # 无命令模板的 id 触发不了（C 方案：允许注册但不允许空跑）。
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                if ops_control.has_pending_request(connection, service_id):
                    raise HTTPException(
                        status_code=400, detail=ErrorDetail.BAD_REQUEST
                    )
                request_id = ops_control.insert_run_request(
                    connection, service_id, viewer.userid,
                )
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning("ops-web run-once write failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "request_id": request_id})

    @app.get("/api/recipients")
    def api_recipients(request: Request):
        """收件人总览读面（JSON；替代 SSR /recipients 页数据，2026-10-14 D1）。

        只读展示口径；舰队清单/只读上下文均 fail-soft（单块故障只缺该块）。
        """
        require_admin(request)
        configs = []
        scope_pairs = [(scope, _label(_SCOPE_LABEL, scope))
                       for scope in report_roster.SCOPES]
        try:
            if fleet_source is not None and config_source is not None:
                for service_id in _fleet():
                    try:
                        configs.append(config_source.get_pipeline(service_id))
                    except Exception as exc:
                        _LOGGER.warning(
                            "ops-web recipients config parse failed: %s",
                            type(exc).__name__)
                        configs.append(PipelineConfig(service_id))
        except Exception as exc:
            _LOGGER.warning("ops-web recipients fleet load failed: %s",
                            type(exc).__name__)
            configs = []
        scope_tasks = _scope_backlinks(configs)
        reminder_ids = sorted({sid for ids in scope_tasks.values()
                               for sid in ids})
        overrides_by_service = {}
        context = None
        context_error = False
        try:
            with db_connector() as connection:
                members = bi_authz.fetch_active_members(connection)
                try:
                    org_overrides = org_assignment_override.fetch_overrides(
                        connection)
                except Exception as exc:
                    _LOGGER.warning(
                        "ops-web org assignment override load failed: %s",
                        type(exc).__name__)
                    org_overrides = {}
                members_by_region = _effective_members_by_region(
                    members, org_overrides)
                uid_to_name = {}
                for uid, name, _reg, _dept in members:
                    uid_to_name.setdefault(uid, name)
                scope_pairs = _region_scope_pairs(connection)
                roster_by_region = {
                    scope: report_roster.fetch_scope_person_names(
                        connection, scope)
                    for scope, _label_ in scope_pairs if scope != "qudao"
                }
                qudao_owners = report_roster.fetch_store_owner_map(
                    connection, "qudao", roles=("owner",))
                for service_id in reminder_ids:
                    try:
                        overrides_by_service[service_id] = \
                            recipient_override.fetch_overrides(
                                connection, service_id)
                    except Exception as exc:
                        _LOGGER.warning(
                            "ops-web recipients override load failed: %s",
                            type(exc).__name__)
                        overrides_by_service[service_id] = ()
            context = {
                "members_by_region": members_by_region,
                "roster_by_region": roster_by_region,
                "qudao_owners": qudao_owners,
                "uid_to_name": uid_to_name,
            }
        except Exception as exc:
            _LOGGER.warning("ops-web recipients context load failed: %s",
                            type(exc).__name__)
            context_error = True
        sections = []
        for scope, scope_label in scope_pairs:
            if context is None:
                sections.append({
                    "scope": scope, "label": scope_label,
                    "html": "<p class=\"hint\">名单读取失败，请稍后重试。</p>"})
                continue
            try:
                html = _recipients_scope_html(
                    scope, scope_tasks.get(scope) or [],
                    overrides_by_service=overrides_by_service, **context)
            except Exception as exc:
                _LOGGER.warning("ops-web recipients scope failed: %s",
                                type(exc).__name__)
                html = "<p class=\"hint\">读取失败，请稍后重试。</p>"
            sections.append({"scope": scope, "label": scope_label, "html": html})
        return JSONResponse({
            "status": "ok",
            "rules_html": _RECIPIENT_RULES_HTML,
            "sections": sections,
            "context_error": context_error,
        })

    @app.get("/recipients")
    def recipients_page():
        """收件人总览页壳（Router 渲染）。"""
        return _spa_shell()

    _service_id_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

    @app.get("/api/pipelines/recipient-override")
    async def recipient_override_get(request: Request):
        """某条服务线的覆盖行（含停用）+ 基名单 +「为什么不在名单」归因。

        抽屉据此展示「有效名单 = 基名单 ∪ include − exclude」三层来源
        （方案 A，2026-10-12；归因为 P2 展示口径）。
        """
        require_capability(request, CAPABILITY_PIPELINES)
        service_id = (request.query_params.get("service_id") or "").strip()
        if not _service_id_re.match(service_id):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                rows = recipient_override.fetch_service_overrides(
                    connection, service_id)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient override read failed: %s",
                type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        # updated_at 是 DB DATETIME（pymysql 返回 datetime 对象），直接进
        # JSONResponse 会 json.dumps 失败 → 500（2026-10-12 线上实测：抽屉报
        # 「读取异常，请稍后重试」）。出口统一用 _fmt_time 归一为北京时间字符串，
        # 与页面其它时间展示口径一致。
        overrides = [
            dict(row, updated_at=_fmt_time(row.get("updated_at")))
            for row in rows
        ]
        # 基名单/归因是附属信息：读取故障只让抽屉少「来源」与「口径说明」
        # 区块，不给整页 503（与 /pipelines 列 fail-soft 同口径）。一次装载
        # 上下文，base 与 explain 共用，避免重复读库。
        base, base_source, explain = [], "", []
        try:
            with db_connector() as connection:
                context = _load_recipient_context(connection, service_id)
            info = _recipient_base(service_id, **context)
            base = [{"user_id": uid, "name": name}
                    for uid, name in info["pairs"]]
            base_source = _base_source_label(info)
            explain = _recipient_explain(
                service_id, base_pairs=info["pairs"], overrides=overrides,
                **context)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient base load failed: %s", type(exc).__name__)
        return JSONResponse(
            {"status": "ok", "service_id": service_id, "base": base,
             "base_source": base_source, "explain": explain,
             "overrides": overrides})

    @app.post("/api/pipelines/recipient-override/add")
    async def recipient_override_add(request: Request):
        """新增/重启用一条覆盖：先解析姓名快照，再落库（同事务审计）。"""
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        mode = payload.get("mode")
        target_type = payload.get("target_type")
        target_value = payload.get("target_value")
        note = payload.get("note") or ""
        if (not isinstance(service_id, str)
                or not _service_id_re.match(service_id.strip())
                or mode not in recipient_override.MODES
                or target_type not in recipient_override.TARGET_TYPES
                or not isinstance(target_value, str) or not target_value.strip()
                or not isinstance(note, str)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        unresolved = []
        resolved = None
        try:
            with db_connector() as connection:
                resolved_rows, unresolved = recipient_override.resolve_targets(
                    connection,
                    [{"target_type": target_type,
                      "target_value": target_value}],
                )
                resolved = resolved_rows[0].get("resolved_user_id")
                recipient_override.upsert_override(
                    connection, service_id.strip(), mode, target_type,
                    target_value, actor=viewer.userid, note=note,
                    resolved_user_id=resolved,
                )
        except recipient_override.RecipientOverrideError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient override add failed: %s",
                type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse(
            {"status": "ok", "resolved_user_id": resolved,
             "unresolved": unresolved})

    @app.post("/api/pipelines/recipient-override/toggle")
    async def recipient_override_toggle(request: Request):
        """停用/启用一条覆盖（按主键 id）。"""
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        payload = await _json_body(request)
        override_id = payload.get("id")
        enabled = payload.get("enabled")
        if (isinstance(override_id, bool) or not isinstance(override_id, int)
                or not isinstance(enabled, bool)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                hit = recipient_override.set_override_enabled(
                    connection, override_id, enabled, actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient override toggle failed: %s",
                type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not hit:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/pipelines/recipient-override/delete")
    async def recipient_override_delete(request: Request):
        """删除一条覆盖（审计留存被删行内容）。"""
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        payload = await _json_body(request)
        override_id = payload.get("id")
        if isinstance(override_id, bool) or not isinstance(override_id, int):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                hit = recipient_override.delete_override(
                    connection, override_id, actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient override delete failed: %s",
                type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not hit:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/pipelines/recipient-override/reset")
    async def recipient_override_reset(request: Request):
        """回退总开关：清空某条服务线全部覆盖（回名册口径）。"""
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        if not isinstance(service_id, str) \
                or not _service_id_re.match(service_id.strip()):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                deleted = recipient_override.reset_service_overrides(
                    connection, service_id.strip(), actor=viewer.userid)
        except recipient_override.RecipientOverrideError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient override reset failed: %s",
                type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "deleted": deleted})
    ctx.fleet = _fleet
