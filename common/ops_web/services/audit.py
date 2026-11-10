"""操作记录聚合读面。"""

from common.ops_web.helpers import *

def _audit_get(row, keys):
    """审计行取值：dict 按 key，序列按位置（与各页 unpack 顺序一致）。"""
    if isinstance(row, dict):
        return row.get
    return lambda key, r=row, ks=keys: r[ks.index(key)]


def _collect_records(connection):
    """5 处审计（授权/任务/名册/日历/收件人覆盖）+ 定时运行流水 → 统一行。

    每行形状 (时间, 来源, 操作人, 动作, 对象, 详情[, user_id])，供「操作记录」页
    归一展示（任务3 2026-10-12：运维改完一处即可查到全部变更）。第 7 位仅
    「授权」来源带（2026-10-15 闭环修复）：该页回源深链要 ``/grants?user=``
    的账号过滤，此前被丢在元组外，深链恒空过滤。
    """
    records = []

    for row in bi_authz.fetch_audit_rows(connection):
        get = _audit_get(row, ("actor", "action", "user_id", "grant_type",
                               "grant_key", "note", "created_at"))
        records.append((
            get("created_at"), "授权", get("actor") or "",
            _label(_ACTION_LABEL, get("action")),
            get("grant_key") or "",
            " ".join(x for x in (_label(_GRANT_TYPE_LABEL, get("grant_type")),
                                 get("note")) if x),
            get("user_id") or "",
        ))

    for row in ops_control.fetch_pipeline_audit(connection):
        get = _audit_get(row, ("actor", "action", "service_id", "detail",
                               "created_at"))
        records.append((
            get("created_at"), "定时任务", get("actor") or "",
            _label(_ACTION_LABEL, get("action")),
            get("service_id") or "", get("detail") or "",
        ))

    for row in report_roster.fetch_roster_audit(connection, limit=200):
        get = _audit_get(row, ("actor", "action", "scope", "entity_key",
                               "person_name", "detail", "created_at"))
        records.append((
            get("created_at"), "填报名册", get("actor") or "",
            _label(_ACTION_LABEL, get("action")),
            " ".join(x for x in (_label(_SCOPE_LABEL, get("scope")),
                                 get("entity_key")) if x),
            " ".join(x for x in (get("person_name"), get("detail")) if x),
        ))

    for row in calendar_store.fetch_recent_audits(connection, limit=200):
        get = _audit_get(row, ("actor", "action", "business_date", "year",
                               "month", "detail", "created_at"))
        biz, yr, mon = get("business_date"), get("year"), get("month")
        when = (biz.isoformat() if isinstance(biz, date)
                else (f"{yr}-{int(mon):02d}" if yr and mon else "—"))
        records.append((
            get("created_at"), "播报日历", get("actor") or "",
            _label(_ACTION_LABEL, get("action")), when, get("detail") or "",
        ))

    # 覆盖审计是本期新表：未迁移/读失败只跳过该来源，不整页 503
    # （与 /pipelines 覆盖列的 fail-soft 口径一致）。
    try:
        override_audits = recipient_override.fetch_recent_audits(
            connection, limit=200)
    except Exception as exc:
        _LOGGER.warning("ops-web recipient override audit load failed: %s",
                        type(exc).__name__)
        override_audits = ()
    for row in override_audits:
        get = _audit_get(row, ("actor", "action", "service_id", "mode",
                               "target_type", "target_value", "detail",
                               "created_at"))
        target = " ".join(x for x in (
            _label(_MODE_LABEL, get("mode")),
            _label(_TARGET_TYPE_LABEL, get("target_type")),
            get("target_value")) if x)
        records.append((
            get("created_at"), "收件人覆盖", get("actor") or "",
            _label(_ACTION_LABEL, get("action")),
            " ".join(x for x in (get("service_id"), target) if x),
            get("detail") or "",
        ))

    # 数据修正审计（第七源，2026-10-16）：fail-soft 同覆盖审计
    # （未迁移/读失败只跳过该来源，不拖垮整页）。
    try:
        data_fix_audits = data_fix.fetch_recent_audits(connection, limit=200)
    except Exception as exc:
        _LOGGER.warning("ops-web data fix audit load failed: %s",
                        type(exc).__name__)
        data_fix_audits = ()
    for row in data_fix_audits:
        get = _audit_get(row, ("actor", "action", "fix_type", "target",
                               "detail", "row_count", "created_at"))
        records.append((
            get("created_at"), "数据修正", get("actor") or "",
            _label(_ACTION_LABEL, get("action")),
            " ".join(x for x in (
                _label(_FIX_TYPE_LABEL, get("fix_type")), get("target")) if x),
            get("detail") or "",
        ))

    # 号码台账审计（第八源，2026-10-26）：fail-soft 同覆盖审计。
    try:
        phone_audits = phone_registry.fetch_phone_audit(connection, limit=200)
    except Exception as exc:
        _LOGGER.warning("ops-web phone audit load failed: %s",
                        type(exc).__name__)
        phone_audits = ()
    for row in phone_audits:
        get = _audit_get(row, ("actor", "action", "phone_no", "detail",
                               "created_at"))
        records.append((
            get("created_at"), "号码台账", get("actor") or "",
            _label(_PHONE_ACTION_LABEL, get("action")),
            get("phone_no") or "", get("detail") or "",
        ))

    for row in ops_control.fetch_run_history(connection):
        get = _audit_get(row, ("id", "service_id", "trigger_type", "status",
                               "exit_code", "note", "started_at",
                               "finished_at"))
        exit_code = get("exit_code")
        records.append((
            get("started_at"), "定时运行", "系统",
            _label(_RUN_STATUS_LABEL, get("status")),
            get("service_id") or "",
            " ".join(x for x in (
                _label(_TRIGGER_LABEL, get("trigger_type")),
                f"退出码 {exit_code}" if exit_code is not None else "",
                get("note")) if x),
        ))

    records.sort(key=lambda r: _fmt_time(r[0]), reverse=True)
    return records

__all__ = tuple(name for name in globals() if not name.startswith("__"))
