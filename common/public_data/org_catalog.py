# -*- coding: utf-8 -*-
"""组织目录登记层（``dim_org_region`` / ``dim_org_dept``，2026-10-13 P7）。

「组织编排」的区域集合此前来自常量 ``_ORG_REGION_ORDER``、部门来自
``dim_robot_member`` 派生——**没有任何写路径**：想让一个「尚无成员」的部门
（如「杭州销售部」）出现在树里、并能作为拖拽归属的目标，只能改代码 / 种子。

本模块提供一层**消费侧目录登记**（纯 ops-web 管理）：

* 区域 / 部门即使**没有任何成员**也能登记进树，并可作为成员拖拽的目标；
* 与 ``dim_org_assignment_override`` 同构：表驱动灰度、回退 = 停用 / 删除行；
* **边界**：只覆盖「组织编排树」这一层，不触碰钉钉组织、不改写
  ``dim_robot_member``；一个**全新区域**要让催办 / 榜单 / 报表真正跑起来，
  仍需 Nacos ``REGIONS`` 配置 + ``org.seed.json`` 部门 + ``pipelines`` /
  ``_ROBOT_REGIONS`` 接入（属后续独立工作，本模块不做）。

读面 fail-soft：目录表读失败（迁移未应用 / DB 抖动）→ 记 WARNING 并回
空目录，**绝不拖垮组织树**。写纪律同 ``report_roster`` / ``recipient_override``：
SQL 只存在于本模块；ops-web 是唯一写方，每次变更与同事务一行 audit 同生共死。
"""

import contextlib
import logging
import re
from datetime import datetime, timezone

from common.public_data.db import transaction

logger = logging.getLogger(__name__)

#: 区域键形态约束（与 org.seed.json 键同风格；不做存在性校验——表驱动）。
_REGION_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

_REGION_KEY_MAX = 32
_LABEL_MAX = 64
_DEPT_ID_MAX = 64
_DEPT_NAME_MAX = 255
_ACTOR_MAX = 64
_AUDIT_DETAIL_MAX = 255

# ---------------------------------------------------------------------------
# DDL（CREATE TABLE IF NOT EXISTS 使重放幂等）
# ---------------------------------------------------------------------------

_REGION_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_org_region` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `region_key` VARCHAR(32) NOT NULL,\n"
    "  `label` VARCHAR(64) NOT NULL,\n"
    "  `sort_order` INT NOT NULL DEFAULT 0,\n"
    "  `enabled` TINYINT(1) NOT NULL DEFAULT 1,\n"
    "  `updated_by` VARCHAR(64) DEFAULT NULL,\n"
    "  `updated_at` DATETIME(6) DEFAULT NULL,\n"
    "  UNIQUE KEY `uk_org_region` (`region_key`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_DEPT_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_org_dept` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `region_key` VARCHAR(32) NOT NULL,\n"
    "  `dept_id` VARCHAR(64) DEFAULT NULL,\n"
    "  `dept_name` VARCHAR(255) NOT NULL,\n"
    "  `sort_order` INT NOT NULL DEFAULT 0,\n"
    "  `enabled` TINYINT(1) NOT NULL DEFAULT 1,\n"
    "  `updated_by` VARCHAR(64) DEFAULT NULL,\n"
    "  `updated_at` DATETIME(6) DEFAULT NULL,\n"
    "  UNIQUE KEY `uk_org_dept` (`region_key`, `dept_name`),\n"
    "  KEY `idx_region_enabled` (`region_key`, `enabled`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_AUDIT_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_org_catalog_audit` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `actor` VARCHAR(64) NOT NULL,\n"
    "  `action` VARCHAR(16) NOT NULL,\n"
    "  `target_type` VARCHAR(16) NOT NULL,\n"
    "  `region_key` VARCHAR(32) DEFAULT NULL,\n"
    "  `dept_id` VARCHAR(64) DEFAULT NULL,\n"
    "  `dept_name` VARCHAR(255) DEFAULT NULL,\n"
    "  `detail` VARCHAR(255) DEFAULT NULL,\n"
    "  `created_at` DATETIME(6) NOT NULL,\n"
    "  KEY `idx_created_at` (`created_at`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

#: 读面列序（fetch_catalog 的 SELECT 与之逐位一致；序列行 / 字典行两用）。
_REGION_KEYS = (
    "id", "region_key", "label", "sort_order", "enabled",
    "updated_by", "updated_at",
)
_DEPT_KEYS = (
    "id", "region_key", "dept_id", "dept_name", "sort_order", "enabled",
    "updated_by", "updated_at",
)


def org_catalog_ddl_statements() -> tuple:
    """迁移语句：区域表 + 部门表 + 审计表（建表顺序即返回顺序）。"""
    return (_REGION_DDL, _DEPT_DDL, _AUDIT_DDL)


# ---------------------------------------------------------------------------
# v2（2026-10-14，D2 区域真源）：区域运行属性列 + 区域并集回填
# ---------------------------------------------------------------------------

#: 区域运行属性列——承接此前散落在 ``region_config`` / 常量的运行参数
#: （display/tableUrl/robotCode/openConversationId/remindHour/checkHour/
#: deptOrder/leaderboardUrl/storeExclude），使「区域的一切配置一处定义」。
#: 值可空：真实运行值仍以 Nacos ``region-<region>.yaml`` 为准（避免 DB /
#: Nacos 双源分叉），本次只落结构。ALTER 无 IF NOT EXISTS，已应用版本靠
#: 校验和跳过（同 report_roster v2/v3 纪律）。
_REGION_RUN_ALTERS = (
    "ALTER TABLE `dim_org_region`\n"
    "  ADD COLUMN `display` VARCHAR(64) DEFAULT NULL AFTER `label`",
    "ALTER TABLE `dim_org_region`\n"
    "  ADD COLUMN `table_url` VARCHAR(255) DEFAULT NULL",
    "ALTER TABLE `dim_org_region`\n"
    "  ADD COLUMN `robot_code` VARCHAR(64) DEFAULT NULL",
    "ALTER TABLE `dim_org_region`\n"
    "  ADD COLUMN `open_conversation_id` VARCHAR(128) DEFAULT NULL",
    "ALTER TABLE `dim_org_region`\n"
    "  ADD COLUMN `remind_hour` TINYINT UNSIGNED DEFAULT NULL",
    "ALTER TABLE `dim_org_region`\n"
    "  ADD COLUMN `check_hour` TINYINT UNSIGNED DEFAULT NULL",
    "ALTER TABLE `dim_org_region`\n"
    "  ADD COLUMN `dept_order` JSON DEFAULT NULL",
    "ALTER TABLE `dim_org_region`\n"
    "  ADD COLUMN `leaderboard_url` VARCHAR(255) DEFAULT NULL",
    "ALTER TABLE `dim_org_region`\n"
    "  ADD COLUMN `store_exclude` JSON DEFAULT NULL",
)

#: 区域并集（#1–#6 实测：``report_roster.SCOPES`` ∪ ``app._ORG_REGION_ORDER``
#: ∪ ``_REGION_LABELS`` ∪ scheduler 常量 ∪ ``region_config`` 种子 ∪
#: ``dim_robot_member.region`` 真实值）。顺序即 ``sort_order``：常用业务区
#: 在前、兜底区（hq/other）在后。
_REGION_REGISTRY_ROWS = (
    ("hangzhou", "杭州"),
    ("shaoxing", "绍兴"),
    ("vanke", "万科&大莲花&团购"),
    ("junpin", "君品雅院"),
    ("qudao", "渠道"),
    ("offline_all", "线下整体"),
    ("dining", "餐饮"),
    ("hq", "总部"),
    ("other", "其他"),
)

_REGION_REGISTRY_INSERT = (
    "INSERT IGNORE INTO `dim_org_region` "
    "(`region_key`, `label`, `sort_order`, `enabled`, `updated_by`, "
    "`updated_at`) VALUES "
)


def _region_registry_inserts() -> tuple:
    return tuple(
        _REGION_REGISTRY_INSERT
        + f"('{key}', '{label}', {order}, 1, "
          "'migration:region-registry', NOW(6))"
        for order, (key, label) in enumerate(_REGION_REGISTRY_ROWS)
    )


def org_catalog_region_run_v1_statements() -> tuple:
    """迁移 ``mart-ops-region-registry-v1``：区域运行属性列 + 区域并集回填。

    先 ``ADD COLUMN`` 补齐运行属性（结构先落、值留空）；再把 #1–#6 出现过的
    区域键并集 ``INSERT IGNORE`` 进 ``dim_org_region``（纯追加，**不覆盖**
    已有人工登记行）——使 ``other`` / ``hq`` / ``dining`` 成为合法区域，
    这是「其他区域的人无法录目标」的直接解法。
    """
    return _REGION_RUN_ALTERS + _region_registry_inserts()


class OrgCatalogError(ValueError):
    """目录写面数据无效（消息只含字段名，不含值）。"""


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


def _clean(value, limit):
    """必填文本：去空白、限长；空值抛 :class:`OrgCatalogError`。"""
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > limit:
        raise OrgCatalogError("a required text field is missing or too long")
    return text


def _as_region(row):
    return {
        "id": _get(row, "id", 0),
        "region_key": str(_get(row, "region_key", 1) or "").strip(),
        "label": _get(row, "label", 2),
        "sort_order": int(_get(row, "sort_order", 3) or 0),
        "enabled": int(_get(row, "enabled", 4) or 0),
        "updated_by": _get(row, "updated_by", 5),
        "updated_at": _get(row, "updated_at", 6),
    }


def _as_dept(row):
    return {
        "id": _get(row, "id", 0),
        "region_key": str(_get(row, "region_key", 1) or "").strip(),
        "dept_id": _get(row, "dept_id", 2),
        "dept_name": _get(row, "dept_name", 3),
        "sort_order": int(_get(row, "sort_order", 4) or 0),
        "enabled": int(_get(row, "enabled", 5) or 0),
        "updated_by": _get(row, "updated_by", 6),
        "updated_at": _get(row, "updated_at", 7),
    }


_REGION_COLUMNS = (
    "`id`, `region_key`, `label`, `sort_order`, `enabled`, "
    "`updated_by`, `updated_at`"
)
_DEPT_COLUMNS = (
    "`id`, `region_key`, `dept_id`, `dept_name`, `sort_order`, `enabled`, "
    "`updated_by`, `updated_at`"
)


def _insert_audit(connection, actor, action, *, target_type, region_key=None,
                  dept_id=None, dept_name=None, detail=""):
    """同事务落一行目录变更流水（不写 commit，随调用方事务）。"""
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(
            "INSERT INTO `dim_org_catalog_audit` "
            "(`actor`, `action`, `target_type`, `region_key`, `dept_id`, "
            "`dept_name`, `detail`, `created_at`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (str(actor)[:_ACTOR_MAX], action, target_type,
             _text(region_key, _REGION_KEY_MAX), _text(dept_id, _DEPT_ID_MAX),
             _text(dept_name, _DEPT_NAME_MAX),
             (detail or "")[:_AUDIT_DETAIL_MAX] or None, _utc_now_text()),
        )


# ---------------------------------------------------------------------------
# 读面（fail-soft）
# ---------------------------------------------------------------------------

def fetch_catalog(connection):
    """**启用**目录行 → ``{"regions": [...], "depts": [...]}``。

    读失败（迁移未应用 / DB 抖动）→ 记 WARNING 并回空目录（fail-soft），
    调用方据此回退「无目录」旧行为，不拖垮组织树。
    """
    try:
        region_rows = _fetch_all(
            connection,
            f"SELECT {_REGION_COLUMNS} FROM `dim_org_region` "
            "WHERE `enabled` = 1 ORDER BY `sort_order`, `region_key`",
        )
        dept_rows = _fetch_all(
            connection,
            f"SELECT {_DEPT_COLUMNS} FROM `dim_org_dept` "
            "WHERE `enabled` = 1 "
            "ORDER BY `region_key`, `sort_order`, `dept_name`",
        )
    except Exception:
        logger.warning(
            "dim_org_region/dim_org_dept 读取失败（迁移未应用？），按无目录：",
            exc_info=True,
        )
        return {"regions": [], "depts": []}
    return {
        "regions": [_as_region(row) for row in region_rows],
        "depts": [_as_dept(row) for row in dept_rows],
    }


def fetch_region_keys(connection, *, enabled_only=True):
    """**区域键真源读**（``dim_org_region``）→ 按 ``sort_order`` 排序的元组。

    消费面（2026-10-14 D2 裁决）：名册/目标的合法 scope、组织树区域顺序、
    服务名展示一律由此派生，替换原先散落在 ``report_roster.SCOPES`` /
    ``_ORG_REGION_ORDER`` / ``_REGION_LABELS`` 的硬编码区域清单。

    ``enabled_only=True``（缺省）只取启用区域；读失败（迁移未应用 / DB
    抖动）→ 记 WARNING 并回空元组（fail-soft），调用方据此回退代码镜像。
    """
    sql = "SELECT `region_key` FROM `dim_org_region` "
    if enabled_only:
        sql += "WHERE `enabled` = 1 "
    sql += "ORDER BY `sort_order`, `region_key`"
    try:
        rows = _fetch_all(connection, sql)
    except Exception:
        logger.warning(
            "dim_org_region 区域键读取失败（迁移未应用？），回退代码镜像：",
            exc_info=True,
        )
        return ()
    keys = []
    for row in rows:
        key = row.get("region_key") if isinstance(row, dict) else row[0]
        key = str(key or "").strip()
        if key and key not in keys:
            keys.append(key)
    return tuple(keys)


# ---------------------------------------------------------------------------
# 写面（ops-web 唯一写方；每次变更与 audit 同事务）
# ---------------------------------------------------------------------------

def _validate_region(region_key, label, sort_order):
    region_key = region_key.strip() if isinstance(region_key, str) else region_key
    if not region_key or not isinstance(region_key, str) \
            or not _REGION_RE.match(region_key):
        raise OrgCatalogError("region_key must be a region key")
    label = _clean(label, _LABEL_MAX)
    return region_key, label, _coerce_order(sort_order)


def _coerce_order(sort_order):
    if sort_order in (None, ""):
        return 0
    if isinstance(sort_order, bool) or not isinstance(sort_order, int):
        raise OrgCatalogError("sort_order must be an integer")
    return sort_order


def upsert_region(connection, region_key, label, *, actor, sort_order=0):
    """登记 / 更新一个区域（``region_key`` 冲突即更新并重新启用）。

    审计 action='add_region'。返回规范化的 ``(region_key, label, sort_order)``。
    """
    region_key, label, sort_order = _validate_region(region_key, label, sort_order)
    with contextlib.closing(connection.cursor()) as cursor:
        with transaction(connection):
            cursor.execute(
                "INSERT INTO `dim_org_region` "
                "(`region_key`, `label`, `sort_order`, `enabled`, "
                "`updated_by`, `updated_at`) "
                "VALUES (%s, %s, %s, 1, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "`label` = VALUES(`label`), "
                "`sort_order` = VALUES(`sort_order`), `enabled` = 1, "
                "`updated_by` = VALUES(`updated_by`), "
                "`updated_at` = VALUES(`updated_at`)",
                (region_key, label, sort_order, actor, _utc_now_text()),
            )
            _insert_audit(
                connection, actor, "add_region", target_type="region",
                region_key=region_key, detail=label,
            )
    return region_key, label, sort_order


def upsert_dept(connection, region_key, dept_name, *, dept_id=None, actor,
                sort_order=0):
    """登记 / 更新一个区域下的部门（``(region_key, dept_name)`` 冲突即更新）。

    ``dept_id`` 可选（允许纯占位部门，此时拖拽归属键取部门名）。
    审计 action='add_dept'。返回 ``(region_key, dept_id, dept_name, sort_order)``。
    """
    region_key = region_key.strip() if isinstance(region_key, str) else region_key
    if not region_key or not isinstance(region_key, str) \
            or not _REGION_RE.match(region_key):
        raise OrgCatalogError("region_key must be a region key")
    dept_name = _clean(dept_name, _DEPT_NAME_MAX)
    dept_id = _text(dept_id, _DEPT_ID_MAX)
    sort_order = _coerce_order(sort_order)
    with contextlib.closing(connection.cursor()) as cursor:
        with transaction(connection):
            cursor.execute(
                "INSERT INTO `dim_org_dept` "
                "(`region_key`, `dept_id`, `dept_name`, `sort_order`, "
                "`enabled`, `updated_by`, `updated_at`) "
                "VALUES (%s, %s, %s, %s, 1, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "`dept_id` = VALUES(`dept_id`), "
                "`sort_order` = VALUES(`sort_order`), `enabled` = 1, "
                "`updated_by` = VALUES(`updated_by`), "
                "`updated_at` = VALUES(`updated_at`)",
                (region_key, dept_id, dept_name, sort_order, actor,
                 _utc_now_text()),
            )
            _insert_audit(
                connection, actor, "add_dept", target_type="dept",
                region_key=region_key, dept_id=dept_id, dept_name=dept_name,
            )
    return region_key, dept_id, dept_name, sort_order


def _check_region_key(region_key):
    region_key = region_key.strip() if isinstance(region_key, str) else region_key
    if not region_key or not isinstance(region_key, str) \
            or not _REGION_RE.match(region_key):
        raise OrgCatalogError("region_key must be a region key")
    return region_key


def set_catalog_enabled(connection, target_type, *, region_key, dept_name=None,
                        enabled, actor):
    """停用 / 启用一个区域或部门（软删：``enabled`` 置 0/1）。

    *target_type* 为 ``'region'`` 或 ``'dept'``；停用部门需传 ``dept_name``。
    停用区域时**不级联**停用其下部门（重新启用区域即可恢复）。
    返回受影响行数。
    """
    region_key = _check_region_key(region_key)
    enabled = 1 if enabled else 0
    if target_type == "region":
        sql = ("UPDATE `dim_org_region` SET `enabled` = %s, "
               "`updated_by` = %s, `updated_at` = %s "
               "WHERE `region_key` = %s")
        params = (enabled, actor, _utc_now_text(), region_key)
        action = "enable_region" if enabled else "disable_region"
        dept_name_audit = None
    elif target_type == "dept":
        if not dept_name or not isinstance(dept_name, str) \
                or not dept_name.strip():
            raise OrgCatalogError("dept_name required for dept target")
        dept_name = dept_name.strip()
        sql = ("UPDATE `dim_org_dept` SET `enabled` = %s, "
               "`updated_by` = %s, `updated_at` = %s "
               "WHERE `region_key` = %s AND `dept_name` = %s")
        params = (enabled, actor, _utc_now_text(), region_key, dept_name)
        action = "enable_dept" if enabled else "disable_dept"
        dept_name_audit = dept_name
    else:
        raise OrgCatalogError("target_type must be 'region' or 'dept'")
    with contextlib.closing(connection.cursor()) as cursor:
        with transaction(connection):
            cursor.execute(sql, params)
            affected = cursor.rowcount
            if affected:
                _insert_audit(
                    connection, actor, action, target_type=target_type,
                    region_key=region_key, dept_name=dept_name_audit,
                )
    return affected


def delete_catalog_entry(connection, target_type, *, region_key, dept_name=None,
                         actor):
    """物理删除一个区域或部门。

    删除区域时**级联**删除其下所有部门（孤儿部门无意义）。返回受影响行数。
    """
    region_key = _check_region_key(region_key)
    with contextlib.closing(connection.cursor()) as cursor:
        with transaction(connection):
            if target_type == "region":
                cursor.execute(
                    "DELETE FROM `dim_org_dept` WHERE `region_key` = %s",
                    (region_key,),
                )
                cursor.execute(
                    "DELETE FROM `dim_org_region` WHERE `region_key` = %s",
                    (region_key,),
                )
                affected = cursor.rowcount
                _insert_audit(
                    connection, actor, "delete_region", target_type="region",
                    region_key=region_key,
                )
            elif target_type == "dept":
                if not dept_name or not isinstance(dept_name, str) \
                        or not dept_name.strip():
                    raise OrgCatalogError("dept_name required for dept target")
                dept_name = dept_name.strip()
                cursor.execute(
                    "DELETE FROM `dim_org_dept` "
                    "WHERE `region_key` = %s AND `dept_name` = %s",
                    (region_key, dept_name),
                )
                affected = cursor.rowcount
                if affected:
                    _insert_audit(
                        connection, actor, "delete_dept",
                        target_type="dept", region_key=region_key,
                        dept_name=dept_name,
                    )
            else:
                raise OrgCatalogError("target_type must be 'region' or 'dept'")
    return affected