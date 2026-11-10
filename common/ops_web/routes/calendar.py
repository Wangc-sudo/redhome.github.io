"""ops-web calendar 路由。"""

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

    def _calendar_seed_path():
        seed_path = getattr(settings, "calendar_seed_path", None)
        if seed_path is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return seed_path

    @app.get("/calendar")
    def calendar_page():
        """播报日历页壳（Router 渲染）。"""
        return _spa_shell()

    @app.get("/api/calendar")
    def api_calendar(request: Request):
        """播报日历读面（JSON；替代 SSR /calendar 页数据，2026-10-14 D1）。"""
        require_capability(request, CAPABILITY_CALENDAR)
        month = (request.query_params.get("month") or "").strip()
        if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
            month = _current_year_month()
        year, mon = (int(part) for part in month.split("-"))
        try:
            with db_connector() as connection:
                days = calendar_store.fetch_month_days(connection, year, mon)
                audits = calendar_store.fetch_recent_audits(connection, limit=20)
        except Exception as exc:
            _LOGGER.warning("ops-web calendar load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        cells = {day.day: (day, is_workday, source, note)
                 for day, is_workday, source, note in days}
        today = datetime.now(_BJT).date()
        import calendar as _pycal
        out_days = []
        for day_no in range(1, _pycal.monthrange(year, mon)[1] + 1):
            info = cells.get(day_no)
            if info is None:
                out_days.append({"day": day_no, "is_workday": None,
                                 "source": None, "note": "", "is_today": False})
                continue
            day, is_workday, source, note = info
            out_days.append({
                "day": day_no,
                "is_workday": bool(is_workday),
                "source": source,
                "note": note or "",
                "is_today": day == today,
            })
        audit_rows = []
        for entry in audits:
            if isinstance(entry, dict):
                get = entry.get
            else:
                get = lambda key, r=entry: r[(
                    "actor", "action", "business_date", "year", "month",
                    "detail", "created_at").index(key)]
            biz_date = get("business_date")
            yr, mon2 = get("year"), get("month")
            when = biz_date.isoformat() if isinstance(biz_date, date) else (
                f"{yr}-{int(mon2):02d}" if yr and mon2 else "—")
            audit_rows.append({
                "created_at": _fmt_time(get("created_at")),
                "actor": get("actor"),
                "action": get("action"),
                "action_label": _label(_ACTION_LABEL, get("action")),
                "when": when,
                "detail": get("detail"),
            })
        return JSONResponse({
            "status": "ok",
            "month": month,
            "year": year,
            "month_no": mon,
            "first_weekday": date(year, mon, 1).weekday(),
            "days": out_days,
            "audits": audit_rows,
            "prev_month": _shift_year_month(month, -1),
            "next_month": _shift_year_month(month, 1),
            "today_iso": today.isoformat(),
        })

    @app.post("/api/calendar/toggle")
    async def calendar_toggle(request: Request):
        """点日期改 上班/休息：写 manual 覆盖 + 即时重写当月 + 审计。"""
        viewer = require_capability(request, CAPABILITY_CALENDAR)
        payload = await _json_body(request)
        note = payload.get("note") or ""
        is_workday = payload.get("is_workday")
        try:
            day = date.fromisoformat(str(payload.get("date")))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(is_workday, bool) \
                or not isinstance(note, str) or len(note) > 255:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                calendar_store.upsert_override(
                    connection, day, is_workday,
                    actor=viewer.userid, note=note,
                )
                calendar_store.rewrite_month(
                    connection, _calendar_seed_path(), day.year, day.month,
                )
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web calendar toggle failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/calendar/reset-month")
    async def calendar_reset_month(request: Request):
        """本月恢复规则：清除 manual 覆盖（回到全播报基线）+ 即时重写。"""
        viewer = require_capability(request, CAPABILITY_CALENDAR)
        payload = await _json_body(request)
        month = str(payload.get("month") or "")
        if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        year, mon = (int(part) for part in month.split("-"))
        try:
            with db_connector() as connection:
                deleted = calendar_store.reset_month_overrides(
                    connection, year, mon, actor=viewer.userid
                )
                calendar_store.rewrite_month(
                    connection, _calendar_seed_path(), year, mon
                )
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web calendar reset failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "deleted": deleted})
