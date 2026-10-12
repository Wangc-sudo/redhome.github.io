# -*- coding: utf-8 -*-
"""任务级收件人覆盖层（``dim_recipient_override``，2026-10-12 方案 B）。

ops-web 定时任务页「收受影响人」列此前是**派生只读值**（名册 ∩ 在职成员 /
渠道店铺负责人），运维要针对某条线增删收件人只能改名册或成员表，且会波及
该区域所有任务。本模块提供**任务级覆盖**：

* ``include`` → 追加到 @ 名单（``at_user_ids``）；
* ``exclude`` → 从 @ 名单移除。

**只作用于 @ 名单，不改消息正文**（与既有 ``cc_user_ids`` 用法一致：正文只
列未填人，额外 @ 不写进正文）。

灰度=表驱动（只给目标 ``service_id`` 建行），回退=停用/删除行——不引入新
Nacos 开关。展示（ops-web）与发送（机器人）**共用** :func:`apply_recipient_overrides`
与 :func:`merge_recipient_overrides`，口径不漂移。

写纪律同 ``report_roster`` / ``bi_authz``：SQL 只存在于本模块；ops-web 是
唯一写方，每次变更与同事务的一行 audit 同生共死；服务标识不做业务校验
（表驱动，注册表是唯一真源），只做形态约束。
"""

import contextlib
import logging
import re
from datetime import datetime, timezone

from common.public_data.db import transaction

logger = logging.getLogger(__name__)

#: 合法覆盖模式：include=追加 @ / exclude=移除 @。
MODES = ("include", "exclude")

#: 合法目标形态：user_id=直接钉钉 id / name=姓名（写入时解析成 user_id 快照）。
TARGET_TYPES = ("user_id", "name")

#: 服务标识形态约束（与注册表同风格；不做业务存在性校验——表驱动）。
_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

_VALUE_MAX = 128
_NOTE_MAX = 255
_AUDIT_DETAIL_MAX = 255

# ---------------------------------------------------------------------------
# DDL（CREATE TABLE IF NOT EXISTS 使重放幂等）
# ---------------------------------------------------------------------------

_OVERRIDE_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_recipient_override` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `service_id` VARCHAR(64) NOT NULL,\n"
    "  `mode` VARCHAR(8) NOT NULL,\n"
    "  `target_type` VARCHAR(16) NOT NULL,\n"
    "  `target_value` VARCHAR(128) NOT NULL,\n"
    "  `resolved_user_id` VARCHAR(64) DEFAULT NULL,\n"
    "  `enabled` TINYINT(1) NOT NULL DEFAULT 1,\n"
    "  `note` VARCHAR(255) DEFAULT NULL,\n"
    "  `updated_by` VARCHAR(64) DEFAULT NULL,\n"
    "  `updated_at` DATETIME(6) DEFAULT NULL,\n"
    "  UNIQUE KEY `uk_recipient_override` "
    "(`service_id`, `mode`, `target_type`, `target_value`),\n"
    "  KEY `idx_service_enabled` (`service_id`, `enabled`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_AUDIT_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_recipient_override_audit` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `actor` VARCHAR(64) NOT NULL,\n"
    "  `action` VARCHAR(16) NOT NULL,\n"
    "  `service_id` VARCHAR(64) NOT NULL,\n"
    "  `mode` VARCHAR(8) DEFAULT NULL,\n"
    "  `target_type` VARCHAR(16) DEFAULT NULL,\n"
    "  `target_value` VARCHAR(128) DEFAULT NULL,\n"
    "  `detail` VARCHAR(255) DEFAULT NULL,\n"
    "  `created_at` DATETIME(6) NOT NULL,\n"
    "  KEY `idx_created_at` (`created_at`),\n"
    "  KEY `idx_service` (`service_id`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

#: 读面列序（fetch_overrides 的 SELECT 与之逐位一致；供序列行/字典行两用）。
_OVERRIDE_KEYS = (
    "id", "service_id", "mode", "target_type", "target_value",
    "resolved_user_id", "enabled", "note", "updated_by", "updated_at",
)


def recipient_override_ddl_statements() -> tuple:
    """迁移语句：覆盖表 + 审计表（建表顺序即返回顺序）。"""
    return (_OVERRIDE_DDL, _AUDIT_DDL)


class RecipientOverrideError(ValueError):
    """覆盖写面数据无效（消息只含字段名，不含值）。"""


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------

def _utc_now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _fetch_all(connection, sql, params=()):
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall() if hasattr(cursor, "fetchall") else ()
        return list(rows)
    finally:
        cursor.close()


def _as_override(row):
    """一行 → 规范化 dict（dict 行按 key，序列行按 :data:`_OVERRIDE_KEYS`）。"""
    if isinstance(row, dict):
        get = row.get
    else:
        get = lambda key, r=row: r[_OVERRIDE_KEYS.index(key)]
    value = get("target_value")
    resolved = get("resolved_user_id")
    return {
        "id": get("id"),
        "service_id": get("service_id"),
        "mode": get("mode"),
        "target_type": get("target_type"),
        "target_value": "" if value is None else str(value),
        "resolved_user_id": (
            str(resolved).strip() if resolved not in (None, "") else None
        ),
        "enabled": int(get("enabled") or 0),
        "note": get("note"),
        "updated_by": get("updated_by"),
        "updated_at": get("updated_at"),
    }


def _insert_audit(connection, actor, action, *, service_id, mode=None,
                  target_type=None, target_value=None, detail=""):
    """同事务落一行覆盖变更流水（不写 commit，随调用方事务）。"""
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(
            "INSERT INTO `dim_recipient_override_audit` "
            "(`actor`, `action`, `service_id`, `mode`, `target_type`, "
            "`target_value`, `detail`, `created_at`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (actor, action, service_id, mode, target_type,
             (str(target_value)[:_VALUE_MAX] if target_value else None),
             (detail or "")[:_AUDIT_DETAIL_MAX] or None, _utc_now_text()),
        )


# ---------------------------------------------------------------------------
# 读面
# ---------------------------------------------------------------------------

def fetch_overrides(connection, service_id):
    """某条服务线的**启用**覆盖行（按 id 升序），供展示与发送合并。"""
    rows = _fetch_all(
        connection,
        "SELECT `id`, `service_id`, `mode`, `target_type`, `target_value`, "
        "`resolved_user_id`, `enabled`, `note`, `updated_by`, `updated_at` "
        "FROM `dim_recipient_override` "
        "WHERE `service_id` = %s AND `enabled` = 1 ORDER BY `id`",
        (service_id,),
    )
    return [_as_override(row) for row in rows]


def fetch_service_overrides(connection, service_id, *, include_disabled=True):
    """某条服务线的全部覆盖行（默认含停用），供抽屉列表展示。"""
    sql = (
        "SELECT `id`, `service_id`, `mode`, `target_type`, `target_value`, "
        "`resolved_user_id`, `enabled`, `note`, `updated_by`, `updated_at` "
        "FROM `dim_recipient_override` WHERE `service_id` = %s"
    )
    if not include_disabled:
        sql += " AND `enabled` = 1"
    sql += " ORDER BY `id`"
    return [_as_override(row) for row in _fetch_all(connection, sql, (service_id,))]


def fetch_recent_audits(connection, limit=200):
    """覆盖变更流水（最新在前），供「操作记录」归并与抽屉历史展示。"""
    return tuple(_fetch_all(
        connection,
        "SELECT `actor`, `action`, `service_id`, `mode`, `target_type`, "
        "`target_value`, `detail`, `created_at` "
        "FROM `dim_recipient_override_audit` ORDER BY `id` DESC LIMIT %s",
        (int(limit),),
    ))


# ---------------------------------------------------------------------------
# 纯函数合并（展示与发送共用；不触库）
# ---------------------------------------------------------------------------

def _base_entries(base):
    """基名单元 → ``(user_id, name)``；容忍 ``(uid, name)`` 元组 / dict / 裸 uid。"""
    for item in base:
        if isinstance(item, dict):
            uid, name = item.get("user_id"), item.get("name")
        elif isinstance(item, (tuple, list)):
            uid = item[0] if len(item) > 0 else None
            name = item[1] if len(item) > 1 else None
        else:
            uid, name = item, None
        uid = str(uid).strip() if uid else ""
        name = str(name).strip() if name else ""
        yield uid, name


def apply_recipient_overrides(base, overrides):
    """``base ∪ include − exclude``（纯函数）：去重保序，返回 user_id 列表。

    *base* 为基名单（``(user_id, name)`` 对 / dict / 裸 user_id 皆可）；
    *overrides* 为 :func:`fetch_overrides` 的行。

    * ``include`` 追加：``user_id`` 型直接取其值；``name`` 型取写入时解析的
      ``resolved_user_id`` 快照——**无快照（歧义/无命中）则跳过**（fail-soft，
      绝不 @ 错人）；
    * ``exclude`` 移除：``user_id`` 型命中同 id；``name`` 型命中同名的 base
      行（按姓名剔除，容忍调用方只有姓名、无 id 的场景）。
    """
    entries = []   # [[uid, name], ...]
    index = {}     # identity -> position

    def _add(uid, name):
        key = uid or ("name:" + name if name else "")
        if not key or key in index:
            return
        index[key] = len(entries)
        entries.append([uid, name])

    for uid, name in _base_entries(base):
        _add(uid, name)

    for row in overrides:
        if str(row.get("mode") or "") != "include":
            continue
        value = str(row.get("target_value") or "").strip()
        if not value:
            continue
        if str(row.get("target_type") or "") == "user_id":
            _add(str(row.get("resolved_user_id") or value).strip(), "")
        else:
            resolved = row.get("resolved_user_id")
            if resolved:
                _add(str(resolved).strip(), value)

    removed = set()
    for row in overrides:
        if str(row.get("mode") or "") != "exclude":
            continue
        value = str(row.get("target_value") or "").strip()
        if not value:
            continue
        if str(row.get("target_type") or "") == "user_id":
            if value in index:
                removed.add(value)
        else:
            for key, pos in index.items():
                if entries[pos][1] == value:
                    removed.add(key)

    return [entries[pos][0] for key, pos in index.items()
            if key not in removed and entries[pos][0]]


def merge_recipient_overrides(connection, service_id, base):
    """按服务线取启用覆盖并合并（展示与机器人**唯一入口**）。

    读覆盖失败（迁移未应用 / DB 抖动）→ 记 WARNING 并回退名册口径，
    绝不拖垮催办。
    """
    base = list(base)
    try:
        overrides = fetch_overrides(connection, service_id)
    except Exception:
        logger.warning(
            "dim_recipient_override 读取失败（迁移未应用？），按名册口径：%s",
            service_id, exc_info=True,
        )
        overrides = []
    return apply_recipient_overrides(base, overrides)


# ---------------------------------------------------------------------------
# 姓名 → user_id 解析（写入时快照，复用 channel_missing 三级映射）
# ---------------------------------------------------------------------------

def resolve_targets(connection, rows):
    """把 ``name`` 型目标解析成 ``resolved_user_id`` 快照（写入时调用）。

    返回 ``(resolved_rows, unresolved)``：*resolved_rows* 为补了
    ``resolved_user_id`` 的行（``user_id`` 型原样快照自身）；*unresolved* 为
    无法唯一命中的 name（歧义或无命中，逐条）。name 型无 unionId，映射只走
    姓名精确匹配（沿用 :func:`channel_missing.resolve_at_user_ids` 的歧义不 @
    语义）。
    """
    from common.daily_robot.channel_missing import (
        fetch_name_map,
        resolve_at_user_ids,
    )
    rows = [dict(row) for row in rows]
    name_rows = [r for r in rows if str(r.get("target_type") or "") == "name"]
    name_map = fetch_name_map(connection) if name_rows else {}
    out = []
    unresolved = []
    for row in rows:
        if str(row.get("target_type") or "") == "user_id":
            row["resolved_user_id"] = (
                str(row.get("target_value") or "").strip() or None
            )
            out.append(row)
            continue
        value = str(row.get("target_value") or "").strip()
        resolved, _unmatched = resolve_at_user_ids(
            [{"name": value, "union_id": None}],
            union_map={}, name_map=name_map,
        )
        if resolved:
            row["resolved_user_id"] = resolved[0][0]
        else:
            row["resolved_user_id"] = None
            if value and value not in unresolved:
                unresolved.append(value)
        out.append(row)
    return out, unresolved


# ---------------------------------------------------------------------------
# 写面（ops-web 唯一写方；每次变更与 audit 同事务）
# ---------------------------------------------------------------------------

def _validate(service_id, mode, target_type, target_value, note):
    if not isinstance(service_id, str) or not _SERVICE_RE.match(service_id):
        raise RecipientOverrideError("service_id must be a service identifier")
    if mode not in MODES:
        raise RecipientOverrideError("mode must be one of include/exclude")
    if target_type not in TARGET_TYPES:
        raise RecipientOverrideError("target_type must be one of user_id/name")
    target_value = (
        target_value.strip() if isinstance(target_value, str) else target_value
    )
    if not target_value or not isinstance(target_value, str) \
            or len(target_value) > _VALUE_MAX:
        raise RecipientOverrideError(
            "target_value is required (at most 128 chars)")
    if note is None:
        note = ""
    if not isinstance(note, str) or len(note) > _NOTE_MAX:
        raise RecipientOverrideError("note must be a string of at most 255 chars")
    return target_value, note.strip()


def upsert_override(connection, service_id, mode, target_type, target_value,
                    *, actor, note="", resolved_user_id=None):
    """新增/重启用一条覆盖（自然键冲突即更新快照/备注并重新启用）。

    审计 action='add'。返回规范化后的 ``(service_id, mode, target_type,
    target_value)``。
    """
    target_value, note = _validate(
        service_id, mode, target_type, target_value, note)
    resolved = (
        str(resolved_user_id).strip() if resolved_user_id not in (None, "")
        else None
    )
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "INSERT INTO `dim_recipient_override` "
                "(`service_id`, `mode`, `target_type`, `target_value`, "
                "`resolved_user_id`, `enabled`, `note`, `updated_by`, "
                "`updated_at`) VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "`resolved_user_id` = VALUES(`resolved_user_id`), "
                "`enabled` = 1, `note` = VALUES(`note`), "
                "`updated_by` = VALUES(`updated_by`), "
                "`updated_at` = VALUES(`updated_at`)",
                (service_id, mode, target_type, target_value, resolved,
                 note or None, actor, _utc_now_text()),
            )
            _insert_audit(
                connection, actor, "add", service_id=service_id, mode=mode,
                target_type=target_type, target_value=target_value,
                detail=note or ("解析为 " + resolved if resolved else ""),
            )
    finally:
        cursor.close()
    return service_id, mode, target_type, target_value


def _load_row(cursor, override_id):
    cursor.execute(
        "SELECT `id`, `service_id`, `mode`, `target_type`, `target_value`, "
        "`resolved_user_id`, `enabled`, `note`, `updated_by`, `updated_at` "
        "FROM `dim_recipient_override` WHERE `id` = %s",
        (override_id,),
    )
    row = cursor.fetchone()
    return None if row is None else _as_override(row)


def set_override_enabled(connection, override_id, enabled, *, actor):
    """停用/启用一条覆盖（按主键 id）。返回是否命中。"""
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "UPDATE `dim_recipient_override` SET `enabled` = %s, "
                "`updated_by` = %s, `updated_at` = %s WHERE `id` = %s",
                (1 if enabled else 0, actor, _utc_now_text(), override_id),
            )
            if getattr(cursor, "rowcount", 0) != 1:
                return False
            row = _load_row(cursor, override_id)
            _insert_audit(
                connection, actor, "enable" if enabled else "disable",
                service_id=row["service_id"], mode=row["mode"],
                target_type=row["target_type"],
                target_value=row["target_value"],
            )
            return True
    finally:
        cursor.close()


def delete_override(connection, override_id, *, actor):
    """删除一条覆盖（按主键 id；审计留存被删行内容）。返回是否命中。"""
    cursor = connection.cursor()
    try:
        with transaction(connection):
            row = _load_row(cursor, override_id)
            if row is None:
                return False
            cursor.execute(
                "DELETE FROM `dim_recipient_override` WHERE `id` = %s",
                (override_id,),
            )
            _insert_audit(
                connection, actor, "delete", service_id=row["service_id"],
                mode=row["mode"], target_type=row["target_type"],
                target_value=row["target_value"],
            )
            return True
    finally:
        cursor.close()


def reset_service_overrides(connection, service_id, *, actor):
    """回退总开关：清除某条服务线的全部覆盖行。返回删除行数。"""
    if not isinstance(service_id, str) or not _SERVICE_RE.match(service_id):
        raise RecipientOverrideError("service_id must be a service identifier")
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "DELETE FROM `dim_recipient_override` WHERE `service_id` = %s",
                (service_id,),
            )
            deleted = getattr(cursor, "rowcount", 0) or 0
            _insert_audit(
                connection, actor, "reset", service_id=service_id,
                detail=f"清除该线全部覆盖 {deleted} 行（回名册口径）",
            )
    finally:
        cursor.close()
    return deleted