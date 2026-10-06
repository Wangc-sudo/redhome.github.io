"""填报人名册（report roster）：谁在哪个业务范围可填报（2026-10-06 运维裁决）。

真源 ``dim_report_roster``（mart_ops，迁移 ``mart-ops-report-roster-v1``），
三个消费面：

* **渠道门店**（``scope='qudao'``，``entity_type='store'``）：店铺 ↔ 负责人，
  是 ``fact_channel_store_target.owners_json`` 的接任名册源——名册表为空时
  渠道机器人回退 legacy owners_json（行为不变），种子导入一次后转正。
* **日报区域**（``scope=region``，``entity_type='person'``）：fail-open
  白名单——该 scope 无启用记录 = 维持现状（dim 成员 + 部门归属门禁）；
  有启用记录 = 必须在册才能报，防止一刀切误伤存量填报人。
* **餐饮/部门**（``scope='dining'``，``entity_type='dept'``）：先登记
  （含陈香梅暂缓项），机器人侧 P3 接入。

写纪律同 ``bi_authz`` / ``ops_control``：SQL 只存在于本模块；ops-web 是
唯一写方，每次变更与同事务的一行 audit 同生共死。月目标仍归
``channel_monthly_target``（AI 表人工维护 → 后续程序维护），名册与目标
自此分表，互不覆盖。
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from common.public_data.db import transaction

#: 合法业务范围（scope）：qudao=渠道门店；其余为日报区域；dining=餐饮/部门。
SCOPES = (
    "qudao", "hangzhou", "shaoxing", "junpin", "vanke", "offline_all", "dining",
)

#: 合法对象类型：store=门店（渠道）、person=人员（日报）、dept=部门（餐饮）。
ENTITY_TYPES = ("store", "person", "dept")

_NAME_RE = re.compile(r"^[^\s,，;；/\\]{1,64}$")
_AUDIT_DETAIL_MAX = 200

# ---------------------------------------------------------------------------
# DDL（CREATE TABLE IF NOT EXISTS 使重放幂等）
# ---------------------------------------------------------------------------

_ROSTER_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_report_roster` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `scope` VARCHAR(32) NOT NULL,\n"
    "  `entity_type` VARCHAR(16) NOT NULL,\n"
    "  `entity_key` VARCHAR(128) NOT NULL,\n"
    "  `person_name` VARCHAR(64) NOT NULL,\n"
    "  `aliases` JSON DEFAULT NULL,\n"
    "  `enabled` TINYINT(1) NOT NULL DEFAULT 1,\n"
    "  `note` VARCHAR(255) DEFAULT NULL,\n"
    "  `updated_by` VARCHAR(64) DEFAULT NULL,\n"
    "  `updated_at` DATETIME(6) DEFAULT NULL,\n"
    "  UNIQUE KEY `uk_roster` (`scope`, `entity_type`, `entity_key`, `person_name`),\n"
    "  KEY `idx_scope_entity` (`scope`, `entity_type`, `enabled`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_AUDIT_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_report_roster_audit` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `actor` VARCHAR(64) NOT NULL,\n"
    "  `action` VARCHAR(16) NOT NULL,\n"
    "  `scope` VARCHAR(32) NOT NULL,\n"
    "  `entity_type` VARCHAR(16) NOT NULL,\n"
    "  `entity_key` VARCHAR(128) NOT NULL,\n"
    "  `person_name` VARCHAR(64) NOT NULL,\n"
    "  `detail` VARCHAR(255) DEFAULT NULL,\n"
    "  `created_at` DATETIME(6) NOT NULL,\n"
    "  KEY `idx_created` (`created_at`),\n"
    "  KEY `idx_scope` (`scope`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


def report_roster_ddl_statements() -> tuple:
    """返回名册 + 审计两表 DDL（建表顺序即返回顺序）。"""
    return (_ROSTER_DDL, _AUDIT_DDL)


# ---------------------------------------------------------------------------
# 写面校验（ops-web 名册表单）
# ---------------------------------------------------------------------------

class ReportRosterError(ValueError):
    """名册写面数据无效（消息只含字段名，不含值）。"""


@dataclass(frozen=True)
class RosterEntry:
    """一条规范化后的名册记录（upsert 的输入）。"""

    scope: str
    entity_type: str
    entity_key: str
    person_name: str
    aliases: tuple = ()
    note: str = ""


def validate_roster_fields(scope, entity_type, entity_key, person_name,
                           aliases=(), note=""):
    """校验名册表单字段，返回规范化的 :class:`RosterEntry`。

    任何字段非法抛 :class:`ReportRosterError`（消息只含字段名，不含值）。
    aliases 逐条按姓名形态校验、去重保序、不得与 person_name 重复。
    """
    if scope not in SCOPES:
        raise ReportRosterError("scope must be a registered business scope")
    if entity_type not in ENTITY_TYPES:
        raise ReportRosterError("entity_type must be one of store/person/dept")
    entity_key = entity_key.strip() if isinstance(entity_key, str) else entity_key
    if not entity_key or not isinstance(entity_key, str) or len(entity_key) > 128:
        raise ReportRosterError("entity_key is required (at most 128 chars)")
    person_name = (
        person_name.strip() if isinstance(person_name, str) else person_name
    )
    if not person_name or not isinstance(person_name, str) \
            or not _NAME_RE.match(person_name):
        raise ReportRosterError("person_name is required (no spaces/commas)")
    if note is None:
        note = ""
    if not isinstance(note, str) or len(note) > 255:
        raise ReportRosterError("note must be a string of at most 255 chars")
    normalized_aliases = []
    for alias in aliases or ():
        alias = alias.strip() if isinstance(alias, str) else alias
        if not alias or not isinstance(alias, str) or not _NAME_RE.match(alias):
            raise ReportRosterError("aliases entries must be names")
        if alias == person_name:
            raise ReportRosterError("aliases must not duplicate person_name")
        if alias not in normalized_aliases:
            normalized_aliases.append(alias)
    if len(normalized_aliases) > 8:
        raise ReportRosterError("aliases supports at most 8 entries")
    return RosterEntry(
        scope=scope, entity_type=entity_type, entity_key=entity_key,
        person_name=person_name, aliases=tuple(normalized_aliases),
        note=note.strip(),
    )


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


def _insert_audit(connection, actor, action, entry, detail=""):
    """同事务落一行名册变更流水（不写 commit，随调用方事务）。"""
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO `dim_report_roster_audit` "
            "(`actor`, `action`, `scope`, `entity_type`, `entity_key`, "
            "`person_name`, `detail`, `created_at`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (actor, action, entry.scope, entry.entity_type, entry.entity_key,
             entry.person_name, (detail or "")[:_AUDIT_DETAIL_MAX] or None,
             _utc_now_text()),
        )
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# 写面（ops-web 唯一写方；每次变更与 audit 同事务）
# ---------------------------------------------------------------------------

def upsert_roster_entry(connection, entry, *, actor):
    """新增/重启用一条名册（自然键冲突即更新别名/备注并重新启用）。"""
    aliases_json = (
        json.dumps(list(entry.aliases), ensure_ascii=False)
        if entry.aliases else None
    )
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "INSERT INTO `dim_report_roster` "
                "(`scope`, `entity_type`, `entity_key`, `person_name`, "
                "`aliases`, `enabled`, `note`, `updated_by`, `updated_at`) "
                "VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "`aliases` = VALUES(`aliases`), `enabled` = 1, "
                "`note` = VALUES(`note`), "
                "`updated_by` = VALUES(`updated_by`), "
                "`updated_at` = VALUES(`updated_at`)",
                (entry.scope, entry.entity_type, entry.entity_key,
                 entry.person_name, aliases_json, entry.note or None,
                 actor, _utc_now_text()),
            )
            _insert_audit(connection, actor, "add", entry, detail=entry.note)
    finally:
        cursor.close()


def set_roster_enabled(connection, roster_id, enabled, *, actor):
    """停用/启用一条名册（按主键 id）。返回是否命中。"""
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "UPDATE `dim_report_roster` SET `enabled` = %s, "
                "`updated_by` = %s, `updated_at` = %s WHERE `id` = %s",
                (1 if enabled else 0, actor, _utc_now_text(), roster_id),
            )
            if getattr(cursor, "rowcount", 0) != 1:
                return False
            cursor.execute(
                "SELECT `scope`, `entity_type`, `entity_key`, `person_name` "
                "FROM `dim_report_roster` WHERE `id` = %s",
                (roster_id,),
            )
            row = cursor.fetchone()
            entry = RosterEntry(
                scope=row["scope"], entity_type=row["entity_type"],
                entity_key=row["entity_key"], person_name=row["person_name"],
            )
            _insert_audit(
                connection, actor, "enable" if enabled else "disable", entry,
            )
            return True
    finally:
        cursor.close()


def delete_roster_entry(connection, roster_id, *, actor):
    """删除一条名册（按主键 id；审计留存被删行内容）。返回是否命中。"""
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "SELECT `scope`, `entity_type`, `entity_key`, `person_name` "
                "FROM `dim_report_roster` WHERE `id` = %s",
                (roster_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            cursor.execute(
                "DELETE FROM `dim_report_roster` WHERE `id` = %s",
                (roster_id,),
            )
            entry = RosterEntry(
                scope=row["scope"], entity_type=row["entity_type"],
                entity_key=row["entity_key"], person_name=row["person_name"],
            )
            _insert_audit(connection, actor, "delete", entry)
            return True
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# 读面
# ---------------------------------------------------------------------------

def fetch_roster(connection, scope=None):
    """ops-web 名册页：全部记录（含停用），可按 scope 过滤，按业务/对象/姓名排序。"""
    if scope is not None and scope not in SCOPES:
        raise ReportRosterError("scope must be a registered business scope")
    sql = (
        "SELECT `id`, `scope`, `entity_type`, `entity_key`, `person_name`, "
        "`aliases`, `enabled`, `note`, `updated_by`, `updated_at` "
        "FROM `dim_report_roster`"
    )
    params = ()
    if scope is not None:
        sql += " WHERE `scope` = %s"
        params = (scope,)
    sql += " ORDER BY `scope`, `entity_type`, `entity_key`, `person_name`"
    return tuple(_fetch_all(connection, sql, params))


def fetch_roster_audit(connection, limit=200):
    """名册变更流水（ops-web 名册页底部展示），最新在前。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ReportRosterError("audit limit must be 1..1000")
    return tuple(_fetch_all(
        connection,
        "SELECT `actor`, `action`, `scope`, `entity_type`, `entity_key`, "
        "`person_name`, `detail`, `created_at` FROM `dim_report_roster_audit` "
        "ORDER BY `id` DESC LIMIT %s",
        (limit,),
    ))


def fetch_store_owner_map(connection, scope="qudao"):
    """渠道机器人名册源：``{entity_key(店名): {负责人名}}``（仅启用行）。

    空 dict = 名册表该 scope 无启用记录，调用方回退 legacy owners_json。
    """
    rows = _fetch_all(
        connection,
        "SELECT `entity_key`, `person_name` FROM `dim_report_roster` "
        "WHERE `scope` = %s AND `entity_type` = 'store' AND `enabled` = 1",
        (scope,),
    )
    owner_map = {}
    for row in rows:
        key = row.get("entity_key") if isinstance(row, dict) else row[0]
        name = row.get("person_name") if isinstance(row, dict) else row[1]
        if not key or not name:  # 非名册行（缺字段）不计入，视同无记录
            continue
        owner_map.setdefault(key, set()).add(name)
    return owner_map


def person_allowed(connection, scope, name, aliases=None):
    """日报区域 fail-open 白名单：该 scope 无启用在册人员 → 放行（维持现状）。

    有启用记录时，*name* 本人或其 aliases 映射后的表内用名须在册。
    """
    rows = _fetch_all(
        connection,
        "SELECT `person_name` FROM `dim_report_roster` "
        "WHERE `scope` = %s AND `entity_type` = 'person' AND `enabled` = 1",
        (scope,),
    )
    # 只认带有效 person_name 的行：缺字段的非名册行过滤后为空，仍按
    # fail-open 处理（与「无启用记录不拦截」语义一致）。
    allowed = set()
    for row in rows:
        value = row.get("person_name") if isinstance(row, dict) else row[0]
        if value:
            allowed.add(value)
    if not allowed:
        return True
    table_name = (aliases or {}).get(name, name)
    return name in allowed or table_name in allowed


# ---------------------------------------------------------------------------
# 种子导入（一次性：渠道月目标表 owners_json → 名册）
# ---------------------------------------------------------------------------

def seed_from_channel_targets(connection, *, actor="seed"):
    """把 ``fact_channel_store_target.owners_json`` 的负责人导入名册。

    INSERT IGNORE 幂等（重跑只补缺失）；返回 (插入数, 跳过数)。导入完成
    后名册表非空，渠道机器人名册源自动从 legacy owners_json 切到本表。
    """
    from common.daily_robot.channel_missing import parse_owner_entries

    rows = _fetch_all(
        connection,
        "SELECT `store_name`, `owners_json` FROM `fact_channel_store_target`",
    )
    inserted = skipped = 0
    cursor = connection.cursor()
    try:
        with transaction(connection):
            for row in rows:
                store = str(
                    (row["store_name"] if isinstance(row, dict) else row[0]) or ""
                ).strip()
                if not store:
                    continue
                owners_raw = row["owners_json"] if isinstance(row, dict) else row[1]
                for entry in parse_owner_entries(owners_raw):
                    name = str(entry.get("name") or "").strip()
                    if not name:
                        continue
                    cursor.execute(
                        "INSERT IGNORE INTO `dim_report_roster` "
                        "(`scope`, `entity_type`, `entity_key`, `person_name`, "
                        "`enabled`, `note`, `updated_by`, `updated_at`) "
                        "VALUES ('qudao', 'store', %s, %s, 1, %s, %s, %s)",
                        (store, name, "owners_json 种子导入", actor,
                         _utc_now_text()),
                    )
                    if getattr(cursor, "rowcount", 0) == 1:
                        inserted += 1
                    else:
                        skipped += 1
            if inserted:
                _insert_audit(
                    connection, actor, "seed",
                    RosterEntry("qudao", "store", "*", "*"),
                    detail=f"种子导入 {inserted} 条（跳过 {skipped} 条已存在）",
                )
    finally:
        cursor.close()
    return inserted, skipped
