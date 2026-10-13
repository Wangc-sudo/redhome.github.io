# -*- coding: utf-8 -*-
"""组织归属覆盖层（``dim_org_assignment_override``，2026-10-13 P6）。

「成员与授权 / 组织编排」此前是**派生只读**：区域 → 部门 → 成员全部由
版本受控种子（``docker/integration/org.seed.json``）递归展开、每日由
``extract-mart`` 物化进 ``dim_robot_member``；运维要把某人换区域 / 换部门
只能改 git 里的 JSON，且写进 dim 的手工字段会被每日**整体替换**冲掉。

本模块提供**消费侧覆盖**（不改写 ``dim_robot_member``）：

* 覆盖行按 ``user_id`` 唯一键控，记录目标 ``region`` 与 ``dept_id/dept_name``；
* ``region IS NULL``（enabled=1）表示**显式移出全部区域**（不进任何催办 / 统计）；
* 消费口径统一为 ``有效归属 = 种子派生 ∘ 覆盖``，见 :func:`apply_region_overrides`；
* 回退 = 停用 / 删除覆盖行（不引入 Nacos 开关）。

灰度为表驱动（只为目标人员建行）。读面 fail-soft：覆盖表读失败（迁移未
应用 / DB 抖动）→ 记一条 WARNING 并回退种子口径，**绝不拖垮催办**。

写纪律同 ``report_roster`` / ``recipient_override``：SQL 只存在于本模块；
ops-web 是唯一写方，每次变更与同事务的一行 audit 同生共死。
"""

import contextlib
import logging
import re
from datetime import datetime, timezone

from common.public_data.db import transaction

logger = logging.getLogger(__name__)

#: 区域键形态约束（与 org.seed.json 键同风格；不做存在性校验——表驱动）。
_REGION_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

_USER_ID_MAX = 64
_NAME_MAX = 128
_DEPT_ID_MAX = 64
_DEPT_NAME_MAX = 255
_NOTE_MAX = 255
_AUDIT_DETAIL_MAX = 255

# ---------------------------------------------------------------------------
# DDL（CREATE TABLE IF NOT EXISTS 使重放幂等）
# ---------------------------------------------------------------------------

_OVERRIDE_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_org_assignment_override` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `user_id` VARCHAR(64) NOT NULL,\n"
    "  `name` VARCHAR(128) DEFAULT NULL,\n"
    "  `region` VARCHAR(32) DEFAULT NULL,\n"
    "  `dept_id` VARCHAR(64) DEFAULT NULL,\n"
    "  `dept_name` VARCHAR(255) DEFAULT NULL,\n"
    "  `enabled` TINYINT(1) NOT NULL DEFAULT 1,\n"
    "  `note` VARCHAR(255) DEFAULT NULL,\n"
    "  `updated_by` VARCHAR(64) DEFAULT NULL,\n"
    "  `updated_at` DATETIME(6) DEFAULT NULL,\n"
    "  UNIQUE KEY `uk_org_assignment_user` (`user_id`),\n"
    "  KEY `idx_region_enabled` (`region`, `enabled`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_AUDIT_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_org_assignment_override_audit` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `actor` VARCHAR(64) NOT NULL,\n"
    "  `action` VARCHAR(16) NOT NULL,\n"
    "  `user_id` VARCHAR(64) NOT NULL,\n"
    "  `name` VARCHAR(128) DEFAULT NULL,\n"
    "  `region` VARCHAR(32) DEFAULT NULL,\n"
    "  `dept_id` VARCHAR(64) DEFAULT NULL,\n"
    "  `dept_name` VARCHAR(255) DEFAULT NULL,\n"
    "  `detail` VARCHAR(255) DEFAULT NULL,\n"
    "  `created_at` DATETIME(6) NOT NULL,\n"
    "  KEY `idx_created_at` (`created_at`),\n"
    "  KEY `idx_user` (`user_id`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

#: 读面列序（fetch_* 的 SELECT 与之逐位一致；供序列行/字典行两用）。
_OVERRIDE_KEYS = (
    "id", "user_id", "name", "region", "dept_id", "dept_name",
    "enabled", "note", "updated_by", "updated_at",
)


def org_assignment_override_ddl_statements() -> tuple:
    """迁移语句：覆盖表 + 审计表（建表顺序即返回顺序）。"""
    return (_OVERRIDE_DDL, _AUDIT_DDL)


class OrgAssignmentOverrideError(ValueError):
    """覆盖写面数据无效（消息只含字段名，不含值）。"""


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


def _as_override(row):
    """一行 → 规范化 dict（dict 行按 key，序列行按 :data:`_OVERRIDE_KEYS`）。"""
    return {
        "id": _get(row, "id", 0),
        "user_id": str(_get(row, "user_id", 1) or "").strip(),
        "name": _get(row, "name", 2),
        "region": _get(row, "region", 3),
        "dept_id": _get(row, "dept_id", 4),
        "dept_name": _get(row, "dept_name", 5),
        "enabled": int(_get(row, "enabled", 6) or 0),
        "note": _get(row, "note", 7),
        "updated_by": _get(row, "updated_by", 8),
        "updated_at": _get(row, "updated_at", 9),
    }


_SELECT_COLUMNS = (
    "`id`, `user_id`, `name`, `region`, `dept_id`, `dept_name`, "
    "`enabled`, `note`, `updated_by`, `updated_at`"
)


def _insert_audit(connection, actor, action, *, user_id, name=None, region=None,
                  dept_id=None, dept_name=None, detail=""):
    """同事务落一行覆盖变更流水（不写 commit，随调用方事务）。"""
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(
            "INSERT INTO `dim_org_assignment_override_audit` "
            "(`actor`, `action`, `user_id`, `name`, `region`, `dept_id`, "
            "`dept_name`, `detail`, `created_at`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (actor, action, str(user_id)[:_USER_ID_MAX], _text(name, _NAME_MAX),
             _text(region, 32), _text(dept_id, _DEPT_ID_MAX),
             _text(dept_name, _DEPT_NAME_MAX),
             (detail or "")[:_AUDIT_DETAIL_MAX] or None, _utc_now_text()),
        )


# ---------------------------------------------------------------------------
# 读面
# ---------------------------------------------------------------------------

def fetch_overrides(connection):
    """**启用**覆盖行 → ``{user_id: row}``（消费侧合并用）。"""
    rows = _fetch_all(
        connection,
        f"SELECT {_SELECT_COLUMNS} FROM `dim_org_assignment_override` "
        "WHERE `enabled` = 1 ORDER BY `user_id`",
    )
    result = {}
    for row in rows:
        item = _as_override(row)
        if item["user_id"]:
            result[item["user_id"]] = item
    return result


def fetch_all_overrides(connection, *, include_disabled=True):
    """全部覆盖行（默认含停用），按 user_id 升序；供 ops-web 列表展示。"""
    sql = f"SELECT {_SELECT_COLUMNS} FROM `dim_org_assignment_override`"
    if not include_disabled:
        sql += " WHERE `enabled` = 1"
    sql += " ORDER BY `user_id`"
    return [_as_override(row) for row in _fetch_all(connection, sql)]


def fetch_recent_audits(connection, limit=200):
    """覆盖变更流水（最新在前），供「操作记录」归并与抽屉历史展示。"""
    return tuple(_fetch_all(
        connection,
        "SELECT `actor`, `action`, `user_id`, `name`, `region`, `dept_id`, "
        "`dept_name`, `detail`, `created_at` "
        "FROM `dim_org_assignment_override_audit` ORDER BY `id` DESC LIMIT %s",
        (int(limit),),
    ))


# ---------------------------------------------------------------------------
# 纯函数合并（展示与消费共用；不触库）
# ---------------------------------------------------------------------------

def apply_region_overrides(base_rows, overrides, *, region):
    """对 *region* 的**基成员行**套用覆盖，返回有效成员行（按姓名排序）。

    *base_rows* 为该区域的基成员（``dim_robot_member`` 派生，含
    ``user_id/name/region/dept_id/dept_name``）；*overrides* 为
    :func:`fetch_overrides` 的 ``{user_id: row}`` 映射。

    * 覆盖 ``region != region``（含 NULL=移出）→ 从本区域**剔除**；
    * 覆盖 ``region == region``：只改部门（``dept_id/dept_name`` 非空才覆盖），
      并为不在基名单中的 ``user_id`` **补入**（移入本区域）；
    * 无覆盖 → 原样保留。
    """
    out = []
    seen = set()
    for raw in base_rows:
        row = dict(raw)
        uid = str(row.get("user_id") or "").strip()
        seen.add(uid)
        override = overrides.get(uid)
        if override is not None:
            if override["region"] != region:
                continue
            if override["dept_id"]:
                row["dept_id"] = override["dept_id"]
            if override["dept_name"]:
                row["dept_name"] = override["dept_name"]
        row["user_id"] = uid
        row["region"] = region
        out.append(row)

    for uid, override in overrides.items():
        if uid in seen or override["region"] != region:
            continue
        out.append({
            "user_id": uid,
            "name": override["name"] or "",
            "region": region,
            "dept_id": override["dept_id"],
            "dept_name": override["dept_name"],
        })

    out.sort(key=lambda item: (item.get("name") or "", item.get("user_id") or ""))
    return out


def _fetch_active_user_ids(connection, user_ids):
    """在 ``dim_robot_member`` 中**在职**的 user_id 集合（移入合法性校验）。"""
    user_ids = [uid for uid in user_ids if uid]
    if not user_ids:
        return set()
    marks = ", ".join(["%s"] * len(user_ids))
    rows = _fetch_all(
        connection,
        "SELECT `user_id` FROM `dim_robot_member` "
        f"WHERE `is_active` = 1 AND `user_id` IN ({marks})",
        tuple(user_ids),
    )
    return {str(_get(row, "user_id", 0) or "").strip() for row in rows}


def merge_region_members(connection, region, base_rows):
    """消费侧**唯一入口**：基成员行 ∘ 覆盖 → 该区域有效成员行。

    覆盖表读失败（迁移未应用 / DB 抖动）→ 记 WARNING 并回退种子口径；
    移入的 user_id 必须仍在 ``dim_robot_member`` 在职（离职不补入）。
    """
    base_rows = [dict(row) for row in base_rows]
    try:
        overrides = fetch_overrides(connection)
        if not overrides:
            return base_rows
        base_ids = {str(row.get("user_id") or "").strip() for row in base_rows}
        inbound = [
            uid for uid, override in overrides.items()
            if override["region"] == region and uid not in base_ids
        ]
        if inbound:
            active = _fetch_active_user_ids(connection, inbound)
            overrides = {
                uid: override for uid, override in overrides.items()
                if uid not in inbound or uid in active
            }
        return apply_region_overrides(base_rows, overrides, region=region)
    except Exception:
        logger.warning(
            "dim_org_assignment_override 读取失败（迁移未应用？），按种子口径：%s",
            region, exc_info=True,
        )
        return base_rows


# ---------------------------------------------------------------------------
# 写面（ops-web 唯一写方；每次变更与 audit 同事务）
# ---------------------------------------------------------------------------

def _validate(user_id, region, dept_id, dept_name, note):
    user_id = user_id.strip() if isinstance(user_id, str) else user_id
    if not user_id or not isinstance(user_id, str) or len(user_id) > _USER_ID_MAX:
        raise OrgAssignmentOverrideError(
            "user_id is required (at most 64 chars)")
    if region is not None:
        region = region.strip() if isinstance(region, str) else region
        if not region or not isinstance(region, str) \
                or not _REGION_RE.match(region):
            raise OrgAssignmentOverrideError(
                "region must be a region key or null")
    dept_id = _text(dept_id, _DEPT_ID_MAX)
    dept_name = _text(dept_name, _DEPT_NAME_MAX)
    if note is None:
        note = ""
    if not isinstance(note, str) or len(note) > _NOTE_MAX:
        raise OrgAssignmentOverrideError(
            "note must be a string of at most 255 chars")
    return user_id, region, dept_id, dept_name, note.strip()


def upsert_assignment(connection, user_id, region, *, dept_id=None,
                      dept_name=None, name=None, actor, note=""):
    """新增/更新一条归属覆盖（``user_id`` 冲突即更新并重新启用）。

    ``region`` 传 ``None`` 表示**移出全部区域**。审计 action='assign'。
    返回规范化的 ``(user_id, region, dept_id, dept_name)``。
    """
    user_id, region, dept_id, dept_name, note = _validate(
        user_id, region, dept_id, dept_name, note)
    name = _text(name, _NAME_MAX)
    with contextlib.closing(connection.cursor()) as cursor:
        with transaction(connection):
            cursor.execute(
                "INSERT INTO `dim_org_assignment_override` "
                "(`user_id`, `name`, `region`, `dept_id`, `dept_name`, "
                "`enabled`, `note`, `updated_by`, `updated_at`) "
                "VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "`name` = COALESCE(VALUES(`name`), `name`), "
                "`region` = VALUES(`region`), "
                "`dept_id` = VALUES(`dept_id`), "
                "`dept_name` = VALUES(`dept_name`), `enabled` = 1, "
                "`note` = VALUES(`note`), "
                "`updated_by` = VALUES(`updated_by`), "
                "`updated_at` = VALUES(`updated_at`)",
                (user_id, name, region, dept_id, dept_name, note or None,
                 actor, _utc_now_text()),
            )
            _insert_audit(
                connection, actor, "assign", user_id=user_id, name=name,
                region=region, dept_id=dept_id, dept_name=dept_name,
                detail=note or ("移出全部区域" if region is None else ""),
            )
    return user_id, region, dept_id, dept_name


def set_assignment_enabled(connection, user_id, enabled, *, actor):
    """停用/启用一条覆盖（按天然键 user_id）。返回是否命中。"""
    user_id = user_id.strip() if isinstance(user_id, str) else user_id
    if not user_id or not isinstance(user_id, str):
        raise OrgAssignmentOverrideError("user_id is required")
    with contextlib.closing(connection.cursor()) as cursor:
        with transaction(connection):
            cursor.execute(
                "UPDATE `dim_org_assignment_override` SET `enabled` = %s, "
                "`updated_by` = %s, `updated_at` = %s WHERE `user_id` = %s",
                (1 if enabled else 0, actor, _utc_now_text(), user_id),
            )
            if getattr(cursor, "rowcount", 0) != 1:
                return False
            cursor.execute(
                f"SELECT {_SELECT_COLUMNS} FROM `dim_org_assignment_override` "
                "WHERE `user_id` = %s",
                (user_id,),
            )
            row = _as_override(cursor.fetchone())
            _insert_audit(
                connection, actor, "enable" if enabled else "disable",
                user_id=row["user_id"], name=row["name"], region=row["region"],
                dept_id=row["dept_id"], dept_name=row["dept_name"],
            )
            return True


def delete_assignment(connection, user_id, *, actor):
    """删除一条覆盖（按天然键 user_id；审计留存被删行内容）。返回是否命中。"""
    user_id = user_id.strip() if isinstance(user_id, str) else user_id
    if not user_id or not isinstance(user_id, str):
        raise OrgAssignmentOverrideError("user_id is required")
    with contextlib.closing(connection.cursor()) as cursor:
        with transaction(connection):
            cursor.execute(
                f"SELECT {_SELECT_COLUMNS} FROM `dim_org_assignment_override` "
                "WHERE `user_id` = %s",
                (user_id,),
            )
            raw = cursor.fetchone()
            if raw is None:
                return False
            row = _as_override(raw)
            cursor.execute(
                "DELETE FROM `dim_org_assignment_override` WHERE `user_id` = %s",
                (user_id,),
            )
            _insert_audit(
                connection, actor, "delete", user_id=row["user_id"],
                name=row["name"], region=row["region"], dept_id=row["dept_id"],
                dept_name=row["dept_name"],
            )
            return True


def reset_assignments(connection, *, actor):
    """回退总开关：清除**全部**覆盖行（回种子口径）。返回删除行数。"""
    with contextlib.closing(connection.cursor()) as cursor:
        with transaction(connection):
            cursor.execute("DELETE FROM `dim_org_assignment_override`")
            deleted = getattr(cursor, "rowcount", 0) or 0
            _insert_audit(
                connection, actor, "reset", user_id="*",
                detail=f"清除全部归属覆盖 {deleted} 行（回种子口径）",
            )
    return deleted