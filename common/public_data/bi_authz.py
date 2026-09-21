"""BI 授权（bi_authz）：grant/audit 两表 DDL、授权数据读写与自举 seed。

授权模型（设计稿 2026-09-21 §4）：

* **deny-by-default**——``bi_authz_grant`` 无任何记录的用户准入拒绝；
  ``grant_type='admin'`` 恒准入。首发布 seed 仅 admin 一条记录（自举），
  其余授权全部由 admin 在 ops-web 分配。
* **bi-web 只读不破**——bi-web 对本模块两表只 SELECT（:func:`fetch_grants`）；
  写路径（:func:`upsert_grant` / :func:`delete_grant`，同事务落 audit）只
  存在于 ops-web 与自举 seed 回放（:func:`apply_grant_seed`）。
* 与 ``dim_target`` 不同：grant 是运维数据不是业务口径，不走 seed 版本
  受控——seed 仅用于自举，日常变更全部经 ops-web 并逐行落 audit。

错误纪律同 ``target_seed``：异常消息只含字段名与行索引，绝不含文件内容。
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from common.public_data.db import transaction

_SEED_VERSION = 1

#: 合法的 grant 类型（设计稿 §4.2 列注释：'scope' | 'region' | 'admin'）。
GRANT_TYPES = frozenset({"scope", "region", "admin"})

#: admin 记录的占位 grant_key（admin 无对象维度）。
ADMIN_GRANT_KEY = "-"

# ---------------------------------------------------------------------------
# DDL（设计稿 §4.2 原样采用；CREATE TABLE IF NOT EXISTS 使重放幂等）
# ---------------------------------------------------------------------------

_BI_AUTHZ_GRANT_DDL = (
    "CREATE TABLE IF NOT EXISTS `bi_authz_grant` (\n"
    "  `user_id` VARCHAR(64) NOT NULL,\n"
    "  `grant_type` VARCHAR(16) NOT NULL,\n"
    "  `grant_key` VARCHAR(64) NOT NULL,\n"
    "  `note` VARCHAR(255) DEFAULT NULL,\n"
    "  `updated_by` VARCHAR(64) DEFAULT NULL,\n"
    "  `updated_at` DATETIME(6) DEFAULT NULL,\n"
    "  PRIMARY KEY (`user_id`, `grant_type`, `grant_key`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_BI_AUTHZ_GRANT_AUDIT_DDL = (
    "CREATE TABLE IF NOT EXISTS `bi_authz_grant_audit` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `actor` VARCHAR(64) NOT NULL,\n"
    "  `action` VARCHAR(16) NOT NULL,\n"
    "  `user_id` VARCHAR(64) NOT NULL,\n"
    "  `grant_type` VARCHAR(16) NOT NULL,\n"
    "  `grant_key` VARCHAR(64) NOT NULL,\n"
    "  `note` VARCHAR(255) DEFAULT NULL,\n"
    "  `created_at` DATETIME(6) NOT NULL,\n"
    "  KEY `idx_user` (`user_id`),\n"
    "  KEY `idx_created` (`created_at`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


def bi_authz_ddl_statements() -> tuple:
    """返回 grant + audit 两表 DDL（建表顺序即返回顺序）。"""
    return (_BI_AUTHZ_GRANT_DDL, _BI_AUTHZ_GRANT_AUDIT_DDL)


# ---------------------------------------------------------------------------
# 授权记录与读写（SQL 全部在本模块，两服务共用）
# ---------------------------------------------------------------------------

class BiAuthzError(ValueError):
    """授权数据或自举 seed 无效（消息绝不含文件内容或行值）。"""


@dataclass(frozen=True)
class GrantRecord:
    """一条授权：``(user_id, grant_type, grant_key)`` 即主键。"""

    user_id: str
    grant_type: str
    grant_key: str
    note: str = ""


def _utc_now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def fetch_grants(connection, user_id):
    """返回 *user_id* 的授权 ``(grant_type, grant_key)`` 元组（bi-web 只读面）。

    本模块唯一的 bi-web 读路径；解析与 fail-closed 语义在
    ``common.bi_web.authz``。
    """
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT `grant_type`, `grant_key` FROM `bi_authz_grant` "
            "WHERE `user_id` = %s",
            (user_id,),
        )
        rows = cursor.fetchall() if hasattr(cursor, "fetchall") else ()
        return tuple(
            (row["grant_type"], row["grant_key"])
            if isinstance(row, dict) else (row[0], row[1])
            for row in rows
        )
    finally:
        cursor.close()


def upsert_grant(connection, record, *, actor):
    """写入一条授权并同事务落一行 audit（ops-web 唯一写路径之一）。

    主键冲突时更新 note/updated_by/updated_at（幂等重授不炸）。返回 1。
    """
    now = _utc_now_text()
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "INSERT INTO `bi_authz_grant` "
                "(`user_id`, `grant_type`, `grant_key`, `note`, "
                "`updated_by`, `updated_at`) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "`note` = VALUES(`note`), "
                "`updated_by` = VALUES(`updated_by`), "
                "`updated_at` = VALUES(`updated_at`)",
                (record.user_id, record.grant_type, record.grant_key,
                 record.note, actor, now),
            )
            _insert_audit(cursor, actor, "grant", record, now)
        return 1
    finally:
        cursor.close()


def delete_grant(connection, user_id, grant_type, grant_key, *, actor):
    """收回一条授权并同事务落一行 audit。返回删除的行数（0 或 1）。"""
    now = _utc_now_text()
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "DELETE FROM `bi_authz_grant` "
                "WHERE `user_id` = %s AND `grant_type` = %s AND `grant_key` = %s",
                (user_id, grant_type, grant_key),
            )
            deleted = getattr(cursor, "rowcount", 1)
            _insert_audit(
                cursor, actor, "revoke",
                GrantRecord(user_id, grant_type, grant_key), now,
            )
        return deleted
    finally:
        cursor.close()


def _insert_audit(cursor, actor, action, record, now):
    cursor.execute(
        "INSERT INTO `bi_authz_grant_audit` "
        "(`actor`, `action`, `user_id`, `grant_type`, `grant_key`, "
        "`note`, `created_at`) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (actor, action, record.user_id, record.grant_type,
         record.grant_key, record.note, now),
    )


def fetch_all_grants(connection):
    """grant 现状全表（ops-web 管理页），按 user_id/grant_type 排序。"""
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT `user_id`, `grant_type`, `grant_key`, `note`, "
            "`updated_by`, `updated_at` FROM `bi_authz_grant` "
            "ORDER BY `user_id`, `grant_type`, `grant_key`"
        )
        rows = cursor.fetchall() if hasattr(cursor, "fetchall") else ()
        return tuple(rows)
    finally:
        cursor.close()


def fetch_audit_rows(connection, *, limit=200):
    """audit 流水（ops-web 审计页），最新在前。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise BiAuthzError("audit limit must be 1..1000")
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT `actor`, `action`, `user_id`, `grant_type`, `grant_key`, "
            "`note`, `created_at` FROM `bi_authz_grant_audit` "
            "ORDER BY `id` DESC LIMIT %s",
            (limit,),
        )
        rows = cursor.fetchall() if hasattr(cursor, "fetchall") else ()
        return tuple(rows)
    finally:
        cursor.close()


def fetch_member_status(connection, user_id):
    """返回 ``(name, is_active)``；``dim_robot_member`` 无此人 → ``None``。

    供 ``resolve_viewer`` 的在职校验（设计稿 §4.2：is_active=0 自动失效）。
    """
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT `name`, `is_active` FROM `dim_robot_member` "
            "WHERE `user_id` = %s",
            (user_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return (row["name"], bool(row["is_active"]))
        return (row[0], bool(row[1]))
    finally:
        cursor.close()


def fetch_active_members(connection):
    """在职成员名单（ops-web 授权候选，读 ``dim_robot_member``）。

    返回 ``(user_id, name, region, dept_name)`` 元组，按 region/name 排序。
    """
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT `user_id`, `name`, `region`, `dept_name` "
            "FROM `dim_robot_member` WHERE `is_active` = 1 "
            "ORDER BY `region`, `name`"
        )
        rows = cursor.fetchall() if hasattr(cursor, "fetchall") else ()
        return tuple(
            (row["user_id"], row["name"], row["region"], row["dept_name"])
            if isinstance(row, dict) else (row[0], row[1], row[2], row[3])
            for row in rows
        )
    finally:
        cursor.close()


def validate_grant_fields(user_id, grant_type, grant_key, note="", label="grant"):
    """校验一条授权的四个字段（seed 与 ops-web 表单共用）。

    返回规范化后的 :class:`GrantRecord`；任何字段非法抛
    :class:`BiAuthzError`（消息只含字段名，不含值）。
    """
    if not isinstance(user_id, str) or not user_id.strip():
        raise BiAuthzError(f"{label} needs a non-empty user_id")
    if grant_type not in GRANT_TYPES:
        raise BiAuthzError(f"{label} has an unknown grant_type")
    if not isinstance(grant_key, str) or not grant_key.strip():
        raise BiAuthzError(f"{label} needs a non-empty grant_key")
    if grant_type == "admin" and grant_key != ADMIN_GRANT_KEY:
        raise BiAuthzError(f"{label} admin grant_key must be '{ADMIN_GRANT_KEY}'")
    if note is None:
        note = ""
    if not isinstance(note, str):
        raise BiAuthzError(f"{label} note must be a string")
    return GrantRecord(
        user_id=user_id.strip(),
        grant_type=grant_type,
        grant_key=grant_key.strip(),
        note=note,
    )


# ---------------------------------------------------------------------------
# 自举 seed（docker/integration/ops.seed.yaml；仅首发布 admin 落库用）
# ---------------------------------------------------------------------------

def load_grant_seed(path):
    """解析版本受控的自举 seed，返回 :class:`GrantRecord` 元组。

    形态::

        version: 1
        grants:
          - user_id: "014341566058939427"
            grant_type: admin
            grant_key: "-"
            note: 自举管理员 王城

    ``_说明`` 键在任何层级允许并忽略（seed 文件惯例）。``grants`` 为空
    抛错——空回放会让人觉得 admin 已落库而实际没有。
    """
    import yaml

    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BiAuthzError("ops seed is not a readable YAML file") from exc
    if not isinstance(raw, dict):
        raise BiAuthzError("ops seed must be a mapping")
    if raw.get("version") != _SEED_VERSION:
        raise BiAuthzError(f"ops seed version must be {_SEED_VERSION}")

    grants = raw.get("grants")
    if not isinstance(grants, list) or not grants:
        raise BiAuthzError("ops seed grants must be a non-empty list")

    records = []
    seen = set()
    for index, item in enumerate(grants):
        label = f"grants[{index}]"
        if not isinstance(item, dict):
            raise BiAuthzError(f"ops seed {label} must be a mapping")
        record = validate_grant_fields(
            item.get("user_id"),
            item.get("grant_type"),
            item.get("grant_key"),
            item.get("note", ""),
            label=f"ops seed {label}",
        )
        key = (record.user_id, record.grant_type, record.grant_key)
        if key in seen:
            raise BiAuthzError(
                f"ops seed {label} repeats (user_id, grant_type, grant_key)"
            )
        seen.add(key)
        records.append(record)
    return tuple(records)


def apply_grant_seed(connection, records, *, actor="seed", if_missing=False):
    """把自举 seed 落库：逐条 upsert + 落 audit，一个事务。返回写入条数。

    ``if_missing=False``（默认，与 publish-bi 同款语义）：主键冲突即
    覆盖 note/updated_by/updated_at；``if_missing=True``：已存在的记录
    跳过（也不落 audit——没发生的变更不进流水）。
    """
    written = 0
    now = _utc_now_text()
    cursor = connection.cursor()
    try:
        with transaction(connection):
            for record in records:
                if if_missing:
                    cursor.execute(
                        "INSERT IGNORE INTO `bi_authz_grant` "
                        "(`user_id`, `grant_type`, `grant_key`, `note`, "
                        "`updated_by`, `updated_at`) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (record.user_id, record.grant_type, record.grant_key,
                         record.note, actor, now),
                    )
                    if not getattr(cursor, "rowcount", 1):
                        continue
                else:
                    cursor.execute(
                        "INSERT INTO `bi_authz_grant` "
                        "(`user_id`, `grant_type`, `grant_key`, `note`, "
                        "`updated_by`, `updated_at`) "
                        "VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON DUPLICATE KEY UPDATE "
                        "`note` = VALUES(`note`), "
                        "`updated_by` = VALUES(`updated_by`), "
                        "`updated_at` = VALUES(`updated_at`)",
                        (record.user_id, record.grant_type, record.grant_key,
                         record.note, actor, now),
                    )
                _insert_audit(cursor, actor, "grant", record, now)
                written += 1
        return written
    finally:
        cursor.close()
