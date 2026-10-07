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

命名纪律（2026-10-07 运维裁决）：``person_name`` 一律使用**通讯录本名**
（如 NDJX、张瑾萱），不登记花名/昵称（历史上的「习酒酒旗-夏惠敏」等
仅存在于停用审计行）。``aliases`` 列仅用于群昵称与本名不一致时的匹配
容错，不是花名登记处。
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
# v2（2026-10-07，统一管理方案 S2）：编号冻结 + 渠道归属列
# ---------------------------------------------------------------------------

#: 渠道归属列：门店编号按渠道内分配；channel 冗余进名册（fact 表店铺下线后
#: 编号与归属仍可查）。ALTER 无 IF NOT EXISTS，已应用版本靠校验和跳过。
_ROSTER_CHANNEL_DDL = (
    "ALTER TABLE `dim_report_roster`\n"
    "  ADD COLUMN `channel` VARCHAR(32) DEFAULT NULL"
)

_ROSTER_STORE_NO_DDL = (
    "ALTER TABLE `dim_report_roster`\n"
    "  ADD COLUMN `store_no` INT DEFAULT NULL"
)


def report_roster_v2_ddl_statements() -> tuple:
    """v2 增量 DDL：channel / store_no 两列（编号冻结，统一管理方案 §6-A）。"""
    return (_ROSTER_CHANNEL_DDL, _ROSTER_STORE_NO_DDL)


# ---------------------------------------------------------------------------
# v3（2026-10-07，运维裁决）：角色列——负责人（owner）与代填报人（deputy）
# ---------------------------------------------------------------------------

#: 合法角色：owner=负责人（业绩归属）；deputy=代填报人（可填报、不占业绩）。
ROLES = ("owner", "deputy")

#: 角色列：权限取 owner∪deputy，业绩投影只取 owner；存量行默认 owner
#: （与引入角色前的语义一致）。ALTER 无 IF NOT EXISTS，已应用版本靠校验和跳过。
_ROSTER_ROLE_DDL = (
    "ALTER TABLE `dim_report_roster`\n"
    "  ADD COLUMN `role` VARCHAR(16) NOT NULL DEFAULT 'owner'"
)


def report_roster_v3_ddl_statements() -> tuple:
    """v3 增量 DDL：role 列（负责人/代填报人区分，2026-10-07 运维裁决）。"""
    return (_ROSTER_ROLE_DDL,)


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
    channel: str | None = None  # store 类型的渠道归属（v2；缺省写库时反查）
    role: str = "owner"  # owner=负责人 / deputy=代填报人（v3）


def validate_roster_fields(scope, entity_type, entity_key, person_name,
                           aliases=(), note="", channel=None, role="owner"):
    """校验名册表单字段，返回规范化的 :class:`RosterEntry`。

    任何字段非法抛 :class:`ReportRosterError`（消息只含字段名，不含值）。
    aliases 逐条按姓名形态校验、去重保序、不得与 person_name 重复。
    role 仅 owner/deputy（v3；非 store 类型一律按 owner 归一）。
    """
    if scope not in SCOPES:
        raise ReportRosterError("scope must be a registered business scope")
    if entity_type not in ENTITY_TYPES:
        raise ReportRosterError("entity_type must be one of store/person/dept")
    if role not in ROLES:
        raise ReportRosterError("role must be one of owner/deputy")
    if entity_type != "store":
        role = "owner"
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
    if channel is not None:
        from common.public_data.channel_target import CHANNELS
        channel = channel.strip() if isinstance(channel, str) else channel
        if channel not in CHANNELS:
            raise ReportRosterError("channel must be a registered channel word")
    return RosterEntry(
        scope=scope, entity_type=entity_type, entity_key=entity_key,
        person_name=person_name, aliases=tuple(normalized_aliases),
        note=note.strip(), channel=channel, role=role,
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


def write_audit(connection, actor, action, scope, entity_key, detail=""):
    """公开审计入口（如 channel_target 发布快照）；entity_type 推定。

    自包事务——供已在其他事务/其他库完成主写入后独立落一行审计的场景
    （名册内部写路径仍用 ``_insert_audit`` 同事务语义，不经过本函数）。
    """
    entity_type = "store" if scope == "qudao" else "person"
    cursor = connection.cursor()
    try:
        with transaction(connection):
            _insert_audit(
                connection, actor, action,
                RosterEntry(scope, entity_type, entity_key, "*"), detail=detail,
            )
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# 写面（ops-web 唯一写方；每次变更与 audit 同事务）
# ---------------------------------------------------------------------------

def _resolve_channel(connection, entry):
    """store 类型的渠道归属：entry 显式值 > fact 表反查 > NULL。

    允许未归属（channel=NULL）：该链接不派 store_no，build_roster 按
    「未编号排尾」处理；fact 表缺失/异常按无结果降级（不拖垮写面）。
    """
    if entry.entity_type != "store":
        return entry.channel
    if entry.channel:
        return entry.channel
    try:
        rows = _fetch_all(
            connection,
            "SELECT `channel` FROM `fact_channel_store_target` "
            "WHERE `store_name` = %s LIMIT 1",
            (entry.entity_key,),
        )
    except Exception:
        rows = ()
    if rows:
        channel = rows[0]["channel"] if isinstance(rows[0], dict) else rows[0][0]
        if channel:
            return channel
    return None


def _next_store_no(connection, scope, channel):
    """渠道内下一个编号（新店取 max+1；编号冻结后目标变化不再洗牌）。"""
    rows = _fetch_all(
        connection,
        "SELECT MAX(`store_no`) AS `m` FROM `dim_report_roster` "
        "WHERE `scope` = %s AND `entity_type` = 'store' AND `channel` = %s",
        (scope, channel),
    )
    current = rows[0]["m"] if isinstance(rows[0], dict) else rows[0][0]
    return (int(current) if current is not None else 0) + 1


def upsert_roster_entry(connection, entry, *, actor):
    """新增/重启用一条名册（自然键冲突即更新别名/备注并重新启用）。

    store 类型（v2 编号冻结）：channel 缺省经 fact 表反查；该店尚无
    store_no 时按渠道内 max+1 派号（既有店沿用原号）。
    """
    aliases_json = (
        json.dumps(list(entry.aliases), ensure_ascii=False)
        if entry.aliases else None
    )
    channel = _resolve_channel(connection, entry)
    store_no = None
    if entry.entity_type == "store" and channel is not None:
        rows = _fetch_all(
            connection,
            "SELECT `store_no` FROM `dim_report_roster` "
            "WHERE `scope` = %s AND `entity_type` = 'store' "
            "AND `entity_key` = %s AND `store_no` IS NOT NULL LIMIT 1",
            (entry.scope, entry.entity_key),
        )
        if rows:
            store_no = rows[0]["store_no"] if isinstance(rows[0], dict) else rows[0][0]
        if store_no is None:
            store_no = _next_store_no(connection, entry.scope, channel)
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "INSERT INTO `dim_report_roster` "
                "(`scope`, `entity_type`, `entity_key`, `person_name`, "
                "`aliases`, `enabled`, `note`, `updated_by`, `updated_at`, "
                "`channel`, `store_no`, `role`) "
                "VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "`aliases` = VALUES(`aliases`), `enabled` = 1, "
                "`note` = VALUES(`note`), "
                "`updated_by` = VALUES(`updated_by`), "
                "`updated_at` = VALUES(`updated_at`), "
                "`channel` = COALESCE(VALUES(`channel`), `channel`), "
                "`role` = VALUES(`role`)",
                (entry.scope, entry.entity_type, entry.entity_key,
                 entry.person_name, aliases_json, entry.note or None,
                 actor, _utc_now_text(), channel, store_no, entry.role),
            )
            _insert_audit(connection, actor, "add", entry, detail=entry.note)
    finally:
        cursor.close()


def backfill_store_numbers(connection, *, actor):
    """编号冻结回填（v2 随迁移后一次性执行，幂等）。

    channel 从 fact_channel_store_target 反查补齐；store_no 按渠道内
    「当前月目标降序、店名升序」赋 1..n——与 build_roster 现行编号
    逐位一致，切换当天编号不跳变；已编号的跳过。返回 (补渠道数, 派号数)。
    """
    from collections import defaultdict

    fact_rows = _fetch_all(
        connection,
        "SELECT `store_name`, `channel`, `monthly_target` "
        "FROM `fact_channel_store_target`",
    )
    fact_map = {}
    for row in fact_rows:
        store = row["store_name"] if isinstance(row, dict) else row[0]
        fact_map[store] = (
            row["channel"] if isinstance(row, dict) else row[1],
            row["monthly_target"] if isinstance(row, dict) else row[2],
        )
    patched_channel = assigned = 0
    cursor = connection.cursor()
    try:
        with transaction(connection):
            for store, (channel, _target) in fact_map.items():
                cursor.execute(
                    "UPDATE `dim_report_roster` SET `channel` = %s "
                    "WHERE `scope` = 'qudao' AND `entity_type` = 'store' "
                    "AND `entity_key` = %s "
                    "AND (`channel` IS NULL OR `channel` = '')",
                    (channel, store),
                )
                patched_channel += max(getattr(cursor, "rowcount", 0), 0)
            link_rows = _fetch_all(
                connection,
                "SELECT DISTINCT `channel`, `entity_key` "
                "FROM `dim_report_roster` "
                "WHERE `scope` = 'qudao' AND `entity_type` = 'store' "
                "AND `channel` IS NOT NULL AND `store_no` IS NULL",
            )
            by_channel = defaultdict(list)
            for row in link_rows:
                channel = row["channel"] if isinstance(row, dict) else row[0]
                store = row["entity_key"] if isinstance(row, dict) else row[1]
                by_channel[channel].append(store)
            for channel, stores in by_channel.items():
                def _sort_key(store):
                    target = fact_map.get(store, (None, None))[1]
                    if target is None:
                        return (1, 0.0, store)
                    return (0, -float(target), store)

                for number, store in enumerate(sorted(stores, key=_sort_key), 1):
                    cursor.execute(
                        "UPDATE `dim_report_roster` SET `store_no` = %s "
                        "WHERE `scope` = 'qudao' AND `entity_type` = 'store' "
                        "AND `entity_key` = %s AND `store_no` IS NULL",
                        (number, store),
                    )
                    assigned += max(getattr(cursor, "rowcount", 0), 0)
            if patched_channel or assigned:
                _insert_audit(
                    connection, actor, "seed",
                    RosterEntry("qudao", "store", "*", "*"),
                    detail=f"编号冻结回填：补渠道 {patched_channel}、派号 {assigned}",
                )
    finally:
        cursor.close()
    return patched_channel, assigned


def fetch_store_numbers(connection, scope="qudao"):
    """店铺编号映射 ``{entity_key(店名): (channel, store_no)}``（编号冻结用）。"""
    rows = _fetch_all(
        connection,
        "SELECT DISTINCT `entity_key`, `channel`, `store_no` "
        "FROM `dim_report_roster` "
        "WHERE `scope` = %s AND `entity_type` = 'store' "
        "AND `store_no` IS NOT NULL",
        (scope,),
    )
    result = {}
    for row in rows:
        store = row.get("entity_key") if isinstance(row, dict) else row[0]
        if not store:
            continue
        result[store] = (
            row.get("channel") if isinstance(row, dict) else row[1],
            row.get("store_no") if isinstance(row, dict) else row[2],
        )
    return result


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
        "`aliases`, `enabled`, `note`, `updated_by`, `updated_at`, "
        "`channel`, `store_no`, `role` "
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


def fetch_store_owner_map(connection, scope="qudao", roles=ROLES):
    """渠道机器人名册源：``{entity_key(店名): {人名}}``（仅启用行）。

    空 dict = 名册表该 scope 无启用记录，调用方回退 legacy owners_json。
    *roles* 控制计入的角色（v3）：权限面取默认 ``("owner", "deputy")``
    （负责人 ∪ 代填报人皆可填）；业绩投影面取 ``("owner",)``。
    """
    marks = ", ".join(["%s"] * len(roles))
    rows = _fetch_all(
        connection,
        "SELECT `entity_key`, `person_name` FROM `dim_report_roster` "
        "WHERE `scope` = %s AND `entity_type` = 'store' AND `enabled` = 1 "
        f"AND `role` IN ({marks})",
        (scope, *roles),
    )
    owner_map = {}
    for row in rows:
        key = row.get("entity_key") if isinstance(row, dict) else row[0]
        name = row.get("person_name") if isinstance(row, dict) else row[1]
        if not key or not name:  # 非名册行（缺字段）不计入，视同无记录
            continue
        owner_map.setdefault(key, set()).add(name)
    return owner_map


def fetch_store_role_map(connection, scope="qudao"):
    """角色分桶名册源：``{entity_key: {"owners": [...], "deputies": [...]}}``
    （仅启用行，名单各自按姓名排序；展示面用，如 /店铺映射表）。"""
    rows = _fetch_all(
        connection,
        "SELECT `entity_key`, `person_name`, `role` FROM `dim_report_roster` "
        "WHERE `scope` = %s AND `entity_type` = 'store' AND `enabled` = 1",
        (scope,),
    )
    role_map: dict = {}
    for row in rows:
        key = row.get("entity_key") if isinstance(row, dict) else row[0]
        name = row.get("person_name") if isinstance(row, dict) else row[1]
        role = row.get("role") if isinstance(row, dict) else row[2]
        if not key or not name:
            continue
        bucket = role_map.setdefault(key, {"owners": set(), "deputies": set()})
        bucket["deputies" if role == "deputy" else "owners"].add(name)
    return {
        key: {
            "owners": sorted(buckets["owners"]),
            "deputies": sorted(buckets["deputies"]),
        }
        for key, buckets in role_map.items()
    }


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

def sync_store_roster(connection, desired, *, scope="qudao", actor):
    """按显式 desired 映射 ``{店名: {负责人名}}`` 整订名册（通用 diff）。

    desired-current → 增（审计 add，自动派 channel/store_no）；
    current-desired → 停（审计 disable，含已跌出 desired 的店铺链接）。
    desired 即真源：渠道机器人与榜单投影立即跟随。返回 (added, disabled)。
    """
    current_rows = _fetch_all(
        connection,
        "SELECT `id`, `entity_key`, `person_name` FROM `dim_report_roster` "
        "WHERE `scope` = %s AND `entity_type` = 'store' AND `enabled` = 1",
        (scope,),
    )
    added = disabled = 0
    for store, names in desired.items():
        current = {
            (row["person_name"] if isinstance(row, dict) else row[2])
            for row in current_rows
            if (row["entity_key"] if isinstance(row, dict) else row[1]) == store
        }
        for name in sorted(set(names) - current):
            upsert_roster_entry(
                connection,
                RosterEntry(scope, "store", store, name,
                            note="sync_store_roster 对齐"),
                actor=actor,
            )
            added += 1
    for row in current_rows:
        store = row["entity_key"] if isinstance(row, dict) else row[1]
        name = row["person_name"] if isinstance(row, dict) else row[2]
        if name not in desired.get(store, set()):
            set_roster_enabled(
                connection,
                row["id"] if isinstance(row, dict) else row[0],
                False, actor=actor,
            )
            disabled += 1
    return added, disabled


def sync_from_targets(connection, *, scope="qudao", actor):
    """按 ``fact_channel_store_target`` 当前 owners_json 整订名册（门店粒度）。

    S1 路径的对齐入口：desired 从 fact 表 owners_json 读出后委托
    :func:`sync_store_roster`（S2 起名册即真源，本函数仅作回退兜底）。
    返回 (added, disabled)。
    """
    from common.daily_robot.channel_missing import parse_owner_entries

    rows = _fetch_all(
        connection,
        "SELECT `store_name`, `owners_json` FROM `fact_channel_store_target`",
    )
    desired = {}
    for row in rows:
        store = str(
            (row["store_name"] if isinstance(row, dict) else row[0]) or ""
        ).strip()
        if not store:
            continue
        owners_raw = row["owners_json"] if isinstance(row, dict) else row[1]
        names = {
            str(entry.get("name") or "").strip()
            for entry in parse_owner_entries(owners_raw)
        }
        names.discard("")
        if names:
            desired[store] = names
    return sync_store_roster(connection, desired, scope=scope, actor=actor)


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
