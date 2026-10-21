# -*- coding: utf-8 -*-
"""代填授权层（``dim_org_proxy_grant``，2026-10-21 代填人设计）。

用户裁决（2026-10-21）：

* 代填人是**授权**不是归属——代填人（如销售内勤沈聪）不属于被代填区域，
  只被授权替该区域的人填数；与 ``dim_org_assignment_override``（归属）分开。
* 权限模型：**admin**（``bi_authz_grant`` grant_type='admin'）可代填所有；
  **非 admin 指定代填人**只能代填本表授权的范围，闭环面板里下拉选定。
* 代填范围粒度：按**区域**授权（``target_person=''`` 表示整个区域），
  也可收窄到具体人（``target_person=姓名``）。

设计要点（与 org_assignment_override 同款工程约束）：

* 写面 deny-by-default、参数化 SQL、事务 + 审计（``_audit`` 表）；
* ``target_person`` 用**空串**表示「整个区域」（而非 NULL）——保证
  ``(proxy_user_id, target_region, target_person)`` 唯一键真正去重；
* 报数/补签门禁读 ``is_proxy_for``（见 gateway/report_intake），与 admin
  做 OR 合并（admin 全权不变）。
"""
import contextlib
import re
from datetime import datetime, timezone

from common.public_data.db import transaction

_REGION_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_USER_ID_MAX = 64
_NAME_MAX = 128
_NOTE_MAX = 255
_AUDIT_DETAIL_MAX = 255

# ---------------------------------------------------------------------------
# DDL（CREATE TABLE IF NOT EXISTS 使重放幂等）
# ---------------------------------------------------------------------------

_GRANT_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_org_proxy_grant` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `proxy_user_id` VARCHAR(64) NOT NULL,\n"
    "  `proxy_name` VARCHAR(128) DEFAULT NULL,\n"
    "  `target_region` VARCHAR(32) NOT NULL,\n"
    "  # 空串 = 整个区域；非空 = 收窄到该区域下具体人\n"
    "  `target_person` VARCHAR(128) NOT NULL DEFAULT '',\n"
    "  `enabled` TINYINT(1) NOT NULL DEFAULT 1,\n"
    "  `note` VARCHAR(255) DEFAULT NULL,\n"
    "  `updated_by` VARCHAR(64) DEFAULT NULL,\n"
    "  `updated_at` DATETIME(6) DEFAULT NULL,\n"
    "  UNIQUE KEY `uk_proxy_target` "
    "(`proxy_user_id`, `target_region`, `target_person`),\n"
    "  KEY `idx_region_enabled` (`target_region`, `enabled`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_AUDIT_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_org_proxy_grant_audit` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `actor` VARCHAR(64) NOT NULL,\n"
    "  `action` VARCHAR(16) NOT NULL,\n"
    "  `proxy_user_id` VARCHAR(64) NOT NULL,\n"
    "  `proxy_name` VARCHAR(128) DEFAULT NULL,\n"
    "  `target_region` VARCHAR(32) DEFAULT NULL,\n"
    "  `target_person` VARCHAR(128) DEFAULT NULL,\n"
    "  `detail` VARCHAR(255) DEFAULT NULL,\n"
    "  `created_at` DATETIME(6) NOT NULL,\n"
    "  KEY `idx_created_at` (`created_at`),\n"
    "  KEY `idx_proxy` (`proxy_user_id`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

#: 读面列序（fetch_* 的 SELECT 与之逐位一致；供序列行/字典行两用）。
_GRANT_KEYS = (
    "id", "proxy_user_id", "proxy_name", "target_region", "target_person",
    "enabled", "note", "updated_by", "updated_at",
)

_SELECT_COLUMNS = (
    "`id`, `proxy_user_id`, `proxy_name`, `target_region`, `target_person`, "
    "`enabled`, `note`, `updated_by`, `updated_at`"
)


def org_proxy_grant_ddl_statements() -> tuple:
    """迁移语句：代填授权表 + 审计表（建表顺序即返回顺序）。"""
    return (_GRANT_DDL, _AUDIT_DDL)


class OrgProxyGrantError(ValueError):
    """代填授权写面数据无效（消息只含字段名，不含值）。"""


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------

def _utc_now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _fetch_all(connection, sql, params=()):
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall() if hasattr(cursor, "fetchall") else ()
        return list(rows)


def _get(row, key, position):
    if isinstance(row, dict):
        return row.get(key)
    return row[position]


def _text(value, limit):
    if value in (None, ""):
        return None
    return str(value).strip()[:limit] or None


def _as_grant(row):
    """一行 → 规范化 dict（dict 行按 key，序列行按 :data:`_GRANT_KEYS`）。"""
    return {
        "id": _get(row, "id", 0),
        "proxy_user_id": str(_get(row, "proxy_user_id", 1) or "").strip(),
        "proxy_name": _get(row, "proxy_name", 2),
        "target_region": _get(row, "target_region", 3),
        # 空串归一成 None 呈现（「整个区域」语义）
        "target_person": _get(row, "target_person", 4) or None,
        "enabled": bool(_get(row, "enabled", 5)),
        "note": _get(row, "note", 6),
        "updated_by": _get(row, "updated_by", 7),
        "updated_at": _get(row, "updated_at", 8),
    }


def _validate(proxy_user_id, target_region, target_person, note):
    proxy_user_id = (
        proxy_user_id.strip() if isinstance(proxy_user_id, str) else proxy_user_id
    )
    if not proxy_user_id or not isinstance(proxy_user_id, str):
        raise OrgProxyGrantError("proxy_user_id is required")
    if len(proxy_user_id) > _USER_ID_MAX:
        raise OrgProxyGrantError("proxy_user_id is too long")
    target_region = (
        target_region.strip() if isinstance(target_region, str) else target_region
    )
    if not target_region or not _REGION_RE.match(target_region):
        raise OrgProxyGrantError("target_region must be a region key")
    # 空串 = 整个区域（规范化）；非空为具体人姓名
    target_person = (target_person or "").strip()[:_NAME_MAX]
    note = _text(note, _NOTE_MAX)
    return proxy_user_id, target_region, target_person, note


def _insert_audit(connection, actor, action, *, proxy_user_id, proxy_name=None,
                  target_region=None, target_person=None, detail=""):
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(
            "INSERT INTO `dim_org_proxy_grant_audit` "
            "(`actor`, `action`, `proxy_user_id`, `proxy_name`, "
            "`target_region`, `target_person`, `detail`, `created_at`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (actor, action, str(proxy_user_id)[:_USER_ID_MAX],
             _text(proxy_name, _NAME_MAX), _text(target_region, 32),
             _text(target_person, _NAME_MAX), _text(detail, _AUDIT_DETAIL_MAX),
             _utc_now_text()),
        )


# ---------------------------------------------------------------------------
# 写面（事务 + 审计）
# ---------------------------------------------------------------------------

def upsert_proxy_grant(connection, proxy_user_id, target_region, *,
                       target_person="", proxy_name=None, note="", actor):
    """新增/更新一条代填授权（按 ``(proxy_user_id, region, person)`` 天然键）。

    ``target_person`` 空串=整个区域。重复 upsert 即更新并重新启用。
    审计 action='grant'。返回规范化 ``(proxy_user_id, region, person)``。
    """
    proxy_user_id, target_region, target_person, note = _validate(
        proxy_user_id, target_region, target_person, note)
    proxy_name = _text(proxy_name, _NAME_MAX)
    with contextlib.closing(connection.cursor()) as cursor:
        with transaction(connection):
            cursor.execute(
                "INSERT INTO `dim_org_proxy_grant` "
                "(`proxy_user_id`, `proxy_name`, `target_region`, "
                "`target_person`, `enabled`, `note`, `updated_by`, "
                "`updated_at`) "
                "VALUES (%s, %s, %s, %s, 1, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "`proxy_name` = VALUES(`proxy_name`), "
                "`enabled` = 1, `note` = VALUES(`note`), "
                "`updated_by` = VALUES(`updated_by`), "
                "`updated_at` = VALUES(`updated_at`)",
                (proxy_user_id, proxy_name, target_region, target_person,
                 note, actor, _utc_now_text()),
            )
            _insert_audit(
                connection, actor, "grant", proxy_user_id=proxy_user_id,
                proxy_name=proxy_name, target_region=target_region,
                target_person=target_person or None, detail=note or "")
    return proxy_user_id, target_region, (target_person or None)


def set_proxy_grant_enabled(connection, proxy_user_id, target_region,
                            target_person, enabled, *, actor):
    """停用/启用一条代填授权（按天然键）。返回是否命中。"""
    target_person = (target_person or "").strip()
    with contextlib.closing(connection.cursor()) as cursor:
        with transaction(connection):
            cursor.execute(
                "UPDATE `dim_org_proxy_grant` SET `enabled` = %s, "
                "`updated_by` = %s, `updated_at` = %s "
                "WHERE `proxy_user_id` = %s AND `target_region` = %s "
                "AND `target_person` = %s",
                (1 if enabled else 0, actor, _utc_now_text(),
                 proxy_user_id, target_region, target_person),
            )
            if getattr(cursor, "rowcount", 0) != 1:
                return False
            _insert_audit(
                connection, actor,
                "enable" if enabled else "disable",
                proxy_user_id=proxy_user_id, target_region=target_region,
                target_person=target_person or None)
    return True


def delete_proxy_grant(connection, proxy_user_id, target_region,
                       target_person, *, actor):
    """删除一条代填授权（按天然键；审计留存被删行内容）。返回是否命中。"""
    target_person = (target_person or "").strip()
    with contextlib.closing(connection.cursor()) as cursor:
        with transaction(connection):
            cursor.execute(
                f"SELECT {_SELECT_COLUMNS} FROM `dim_org_proxy_grant` "
                "WHERE `proxy_user_id` = %s AND `target_region` = %s "
                "AND `target_person` = %s",
                (proxy_user_id, target_region, target_person),
            )
            raw = cursor.fetchone()
            if raw is None:
                return False
            row = _as_grant(raw)
            cursor.execute(
                "DELETE FROM `dim_org_proxy_grant` "
                "WHERE `proxy_user_id` = %s AND `target_region` = %s "
                "AND `target_person` = %s",
                (proxy_user_id, target_region, target_person),
            )
            _insert_audit(
                connection, actor, "revoke", proxy_user_id=proxy_user_id,
                proxy_name=row["proxy_name"], target_region=target_region,
                target_person=target_person or None, detail=row["note"] or "")
    return True


# ---------------------------------------------------------------------------
# 读面
# ---------------------------------------------------------------------------

def fetch_all_proxy_grants(connection, *, include_disabled=True):
    """代填授权全表（ops-web 闭环面板管理页），按代填人/区域排序。"""
    sql = f"SELECT {_SELECT_COLUMNS} FROM `dim_org_proxy_grant`"
    if not include_disabled:
        sql += " WHERE `enabled` = 1"
    sql += " ORDER BY `proxy_user_id`, `target_region`, `target_person`"
    return [_as_grant(row) for row in _fetch_all(connection, sql)]


def fetch_proxy_targets(connection, proxy_user_id):
    """某代填人的有效授权范围（启用），供闭环面板下拉/门禁。

    返回 ``[{"target_region":..., "target_person":...|None}]``。
    """
    rows = _fetch_all(
        connection,
        f"SELECT {_SELECT_COLUMNS} FROM `dim_org_proxy_grant` "
        "WHERE `proxy_user_id` = %s AND `enabled` = 1 "
        "ORDER BY `target_region`, `target_person`",
        (proxy_user_id,),
    )
    return [
        {"target_region": g["target_region"], "target_person": g["target_person"]}
        for g in (_as_grant(r) for r in rows)
    ]


def is_proxy_for(connection, user_id, *, region, person=None):
    """*user_id* 是否被授权代填 *region*（可选收窄到 *person*）。

    命中条件：存在启用的授权，``target_region`` 相同，且
    ``target_person`` 为空串（整区域）或等于 *person*。
    表未就绪/抖动 fail-closed（返回 False，不冒名放行）。
    """
    if not user_id or not region:
        return False
    person = (person or "").strip()
    try:
        rows = _fetch_all(
            connection,
            "SELECT 1 AS `x` FROM `dim_org_proxy_grant` "
            "WHERE `proxy_user_id` = %s AND `target_region` = %s "
            "AND `enabled` = 1 "
            "AND (`target_person` = '' OR `target_person` = %s) LIMIT 1",
            (user_id, region, person),
        )
    except Exception:
        return False
    return bool(rows)
