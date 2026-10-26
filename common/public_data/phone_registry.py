"""公司电信号码台账（phone registry）：号码 ↔ 持卡人 ↔ 平台账号（2026-10-23）。

真源 ``dim_phone_number``（mart_ops，迁移 ``mart-ops-phone-registry-v1``）。

解决的具体痛点：公司电信号码七十余个、一人多卡（陈典潮 5 张、吴昱洁 6 张），
店铺/平台账号登录与修改都要手机验证码，原台账只有电信开户名（``小户名``），
与真实持卡人不符（如 15158204818 电信登记「陈宝华」、实际在李总手机），
导致收验证码时找不到该找谁。

三张表：

* ``dim_phone_number``：号码台账主体，业务键 = ``phone_no``。
  ``actual_user`` 为空且 ``needs_claim=1`` 即待认领（推断不出持卡人）；
  座机（``phone_type='固话'``）不参与认领。
* ``dim_phone_binding``：号码 ↔ 平台账号绑定（一对多），回答「这个店铺
  账号的验证码发给哪个号」。
* ``dim_phone_number_audit``：变更审计，与写同事务。

数据纪律（源台账 20261012 版的实测问题，导入时已处理）：

* **台账不存任何密码**——原 Excel 的 Sheet3 含辛选/飞瓜/微信公众号等明文
  口令，导入脚本一律剔除，本模块也不提供密码列。
* ``account_name`` 是电信开户名，``actual_user`` 是真实持卡人，两者常不相等，
  检索一律以 ``actual_user`` 为准。
* 人名写法不一致（号码明细「吴昱洁」/ 宽带信息「吴煜洁」）在导入时统一，
  后续以钉钉通讯录本名为准。

写纪律同 ``report_roster``：SQL 只存在于本模块；每次变更与同事务的一行
audit 同生共死。
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from common.public_data.db import transaction

#: 合法号码类型。号段优先于套餐判定：0571 座机在原台账里被误标成「副卡」。
PHONE_TYPES = ("主卡", "副卡", "固话", "未知")

#: 号码状态（源台账口径，未填即 NULL，不臆造）。
PHONE_STATUSES = ("正常", "停机", "空号", "工信部关停", "未填")

_AUDIT_DETAIL_MAX = 200

_PHONE_RE = re.compile(r"^[0-9+\-]{5,32}$")


class PhoneRegistryError(ValueError):
    """台账写入校验失败（消息只含字段名，不含值）。"""


# ---------------------------------------------------------------------------
# DDL（CREATE TABLE IF NOT EXISTS 使重放幂等）
# ---------------------------------------------------------------------------

_PHONE_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_phone_number` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `phone_no` VARCHAR(32) NOT NULL,\n"
    "  `phone_type` VARCHAR(16) NOT NULL,\n"
    "  `plan_name` VARCHAR(32) DEFAULT NULL,\n"
    "  `account_name` VARCHAR(64) DEFAULT NULL,\n"
    "  `actual_user` VARCHAR(64) DEFAULT NULL,\n"
    "  `owner` VARCHAR(64) DEFAULT NULL,\n"
    "  `dept` VARCHAR(64) DEFAULT NULL,\n"
    "  `corp_owner` VARCHAR(128) DEFAULT NULL,\n"
    "  `physical_device` VARCHAR(64) DEFAULT NULL,\n"
    "  `status` VARCHAR(16) DEFAULT NULL,\n"
    "  `broadband_account` VARCHAR(64) DEFAULT NULL,\n"
    "  `broadband_status` VARCHAR(16) DEFAULT NULL,\n"
    "  `address` VARCHAR(255) DEFAULT NULL,\n"
    "  `note` VARCHAR(255) DEFAULT NULL,\n"
    "  `needs_claim` TINYINT(1) NOT NULL DEFAULT 0,\n"
    "  `enabled` TINYINT(1) NOT NULL DEFAULT 1,\n"
    "  `updated_by` VARCHAR(64) DEFAULT NULL,\n"
    "  `updated_at` DATETIME(6) DEFAULT NULL,\n"
    "  UNIQUE KEY `uk_phone` (`phone_no`),\n"
    "  KEY `idx_user` (`actual_user`, `enabled`),\n"
    "  KEY `idx_dept` (`dept`, `enabled`),\n"
    "  KEY `idx_claim` (`needs_claim`, `phone_type`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_BINDING_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_phone_binding` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `phone_no` VARCHAR(32) NOT NULL,\n"
    "  `channel_category` VARCHAR(32) NOT NULL,\n"
    "  `platform_account` VARCHAR(255) NOT NULL,\n"
    "  `note` VARCHAR(255) DEFAULT NULL,\n"
    "  `enabled` TINYINT(1) NOT NULL DEFAULT 1,\n"
    "  `updated_by` VARCHAR(64) DEFAULT NULL,\n"
    "  `updated_at` DATETIME(6) DEFAULT NULL,\n"
    "  UNIQUE KEY `uk_binding` (`phone_no`, `channel_category`, "
    "`platform_account`(191)),\n"
    "  KEY `idx_account` (`platform_account`(64))\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_AUDIT_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_phone_number_audit` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `actor` VARCHAR(64) NOT NULL,\n"
    "  `action` VARCHAR(16) NOT NULL,\n"
    "  `phone_no` VARCHAR(32) NOT NULL,\n"
    "  `detail` VARCHAR(255) DEFAULT NULL,\n"
    "  `created_at` DATETIME(6) NOT NULL,\n"
    "  KEY `idx_created` (`created_at`),\n"
    "  KEY `idx_phone` (`phone_no`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


def phone_registry_ddl_statements() -> tuple:
    """号码台账 + 账号绑定 + 审计三表 DDL（建表顺序即返回顺序）。"""
    return (_PHONE_DDL, _BINDING_DDL, _AUDIT_DDL)


# ---------------------------------------------------------------------------
# 记录模型与校验
# ---------------------------------------------------------------------------


@dataclass
class PhoneRecord:
    """一条号码台账记录（upsert 的输入）。"""

    phone_no: str
    phone_type: str = "未知"
    plan_name: str = ""
    account_name: str = ""
    actual_user: str = ""
    owner: str = ""
    dept: str = ""
    corp_owner: str = ""
    physical_device: str = ""
    status: str = ""
    broadband_account: str = ""
    broadband_status: str = ""
    address: str = ""
    note: str = ""
    needs_claim: bool = False
    enabled: bool = True


def validate_phone_record(phone_no, phone_type="未知", **kwargs) -> PhoneRecord:
    """校验并规范化一条号码记录。

    任何字段非法抛 :class:`PhoneRegistryError`（消息只含字段名，不含值）。
    座机（``固话``）强制 ``needs_claim=False``——座机不找人。
    """
    no = str(phone_no or "").strip()
    if not _PHONE_RE.match(no):
        raise PhoneRegistryError("phone_no 非法")
    kind = str(phone_type or "未知").strip()
    if kind not in PHONE_TYPES:
        raise PhoneRegistryError("phone_type 非法")
    allowed = {f for f in PhoneRecord.__dataclass_fields__ if f != "phone_no"}
    unknown = sorted(set(kwargs) - allowed)
    if unknown:
        raise PhoneRegistryError("未知字段: %s" % ",".join(unknown))
    record = PhoneRecord(phone_no=no, phone_type=kind, **kwargs)
    if kind == "固话":
        record.needs_claim = False
    return record


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _fetch_all(connection, sql, params=()):
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall() if hasattr(cursor, "fetchall") else ()
        return list(rows)
    finally:
        cursor.close()


def _insert_audit(connection, actor, action, phone_no, detail=""):
    """同事务落一行变更流水（不写 commit，随调用方事务）。"""
    cursor = connection.cursor()
    try:
        cursor.execute(
            "INSERT INTO `dim_phone_number_audit` "
            "(`actor`, `action`, `phone_no`, `detail`, `created_at`) "
            "VALUES (%s, %s, %s, %s, %s)",
            (actor, action, phone_no,
             (detail or "")[:_AUDIT_DETAIL_MAX] or None, _utc_now_text()),
        )
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# 写路径
# ---------------------------------------------------------------------------

_WRITE_SQL = (
    "INSERT INTO `dim_phone_number` "
    "(`phone_no`, `phone_type`, `plan_name`, `account_name`, `actual_user`, "
    "`owner`, `dept`, `corp_owner`, `physical_device`, `status`, "
    "`broadband_account`, `broadband_status`, `address`, `note`, "
    "`needs_claim`, `enabled`, `updated_by`, `updated_at`) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
    "%s, %s, %s, %s) "
    "ON DUPLICATE KEY UPDATE "
    "`phone_type` = VALUES(`phone_type`), "
    "`plan_name` = VALUES(`plan_name`), "
    "`account_name` = VALUES(`account_name`), "
    "`actual_user` = VALUES(`actual_user`), "
    "`owner` = VALUES(`owner`), "
    "`dept` = VALUES(`dept`), "
    "`corp_owner` = VALUES(`corp_owner`), "
    "`physical_device` = VALUES(`physical_device`), "
    "`status` = VALUES(`status`), "
    "`broadband_account` = VALUES(`broadband_account`), "
    "`broadband_status` = VALUES(`broadband_status`), "
    "`address` = VALUES(`address`), "
    "`note` = VALUES(`note`), "
    "`needs_claim` = VALUES(`needs_claim`), "
    "`enabled` = VALUES(`enabled`), "
    "`updated_by` = VALUES(`updated_by`), "
    "`updated_at` = VALUES(`updated_at`)"
)


def _values_of(record: PhoneRecord, actor, now_text):
    return (
        record.phone_no,
        record.phone_type,
        record.plan_name or None,
        record.account_name or None,
        record.actual_user or None,
        record.owner or None,
        record.dept or None,
        record.corp_owner or None,
        record.physical_device or None,
        record.status or None,
        record.broadband_account or None,
        record.broadband_status or None,
        record.address[:255] if record.address else None,
        record.note[:255] if record.note else None,
        1 if record.needs_claim else 0,
        1 if record.enabled else 0,
        actor,
        now_text,
    )


def upsert_phone_number(connection, record, *, actor):
    """新增/更新一条号码（自然键 = phone_no），自包事务。"""
    with transaction(connection):
        _write_phone(connection, record, actor=actor)


def apply_phone_number(connection, record, *, actor):
    """**无事务**号码写，供调用方自有事务组合使用。"""
    _write_phone(connection, record, actor=actor)


def _write_phone(connection, record, *, actor):
    now_text = _utc_now_text()
    cursor = connection.cursor()
    try:
        cursor.execute(_WRITE_SQL, _values_of(record, actor, now_text))
    finally:
        cursor.close()
    _insert_audit(connection, actor, "upsert", record.phone_no,
                  detail=(record.note or "")[:_AUDIT_DETAIL_MAX])


def replace_phone_registry(connection, records, *, actor):
    """整体重放号码台账（单事务 DELETE + 批量 INSERT），幂等。

    语义同 ``target_seed.replace_dim_target``：表内容永远与传入数据集一致。
    适用于从源台账 Excel/CSV 全量重建；日常单条修改走
    :func:`upsert_phone_number`。
    """
    now_text = _utc_now_text()
    with transaction(connection):
        cursor = connection.cursor()
        try:
            cursor.execute("DELETE FROM `dim_phone_number`")
            if records:
                cursor.executemany(
                    "INSERT IGNORE INTO `dim_phone_number` "
                    "(`phone_no`, `phone_type`, `plan_name`, `account_name`, "
                    "`actual_user`, `owner`, `dept`, `corp_owner`, "
                    "`physical_device`, `status`, `broadband_account`, "
                    "`broadband_status`, `address`, `note`, `needs_claim`, "
                    "`enabled`, `updated_by`, `updated_at`) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s, %s, %s, %s)",
                    [_values_of(r, actor, now_text) for r in records],
                )
            _insert_audit(connection, actor, "seed", "*",
                          detail="全量重放 %d 条号码" % len(records))
        finally:
            cursor.close()


_BINDING_WRITE_SQL = (
    "INSERT INTO `dim_phone_binding` "
    "(`phone_no`, `channel_category`, `platform_account`, `note`, "
    "`enabled`, `updated_by`, `updated_at`) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
    "ON DUPLICATE KEY UPDATE "
    "`note` = VALUES(`note`), "
    "`enabled` = VALUES(`enabled`), "
    "`updated_by` = VALUES(`updated_by`), "
    "`updated_at` = VALUES(`updated_at`)"
)


def replace_phone_bindings(connection, bindings, *, actor):
    """整体重放账号绑定（单事务 DELETE + 批量 INSERT），幂等。

    ``bindings`` 元素为 ``(phone_no, channel_category, platform_account,
    note)`` 四元组。
    """
    now_text = _utc_now_text()
    with transaction(connection):
        cursor = connection.cursor()
        try:
            cursor.execute("DELETE FROM `dim_phone_binding`")
            if bindings:
                cursor.executemany(
                    "INSERT IGNORE INTO `dim_phone_binding` "
                    "(`phone_no`, `channel_category`, `platform_account`, "
                    "`note`, `enabled`, `updated_by`, `updated_at`) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    [(b[0], b[1], b[2][:255], b[3] or None, 1, actor, now_text)
                     for b in bindings],
                )
            _insert_audit(connection, actor, "seed", "*",
                          detail="全量重放 %d 条账号绑定" % len(bindings))
        finally:
            cursor.close()


def upsert_phone_binding(connection, phone_no, channel_category,
                         platform_account, note="", *, actor):
    """新增/更新一条账号绑定（自然键 = 号码+类目+账号），自包事务。"""
    phone = str(phone_no or "").strip()
    category = str(channel_category or "").strip()
    account = str(platform_account or "").strip()
    if not _PHONE_RE.match(phone):
        raise PhoneRegistryError("phone_no 非法")
    if not category or len(category) > 32:
        raise PhoneRegistryError("channel_category 非法")
    if not account:
        raise PhoneRegistryError("platform_account 非法")
    with transaction(connection):
        cursor = connection.cursor()
        try:
            cursor.execute(
                _BINDING_WRITE_SQL,
                (phone, category, account[:255], (note or "")[:255] or None,
                 1, actor, _utc_now_text()),
            )
        finally:
            cursor.close()
        _insert_audit(connection, actor, "binding_save", phone,
                      detail="%s / %s" % (category, account[:120]))


def set_binding_enabled(connection, binding_id, enabled, *, actor):
    """停用/启用一条绑定（按主键 id）。返回受影响行数（0 = 不存在）。"""
    with transaction(connection):
        cursor = connection.cursor()
        try:
            cursor.execute(
                "UPDATE `dim_phone_binding` SET `enabled` = %s, "
                "`updated_by` = %s, `updated_at` = %s WHERE `id` = %s",
                (1 if enabled else 0, actor, _utc_now_text(),
                 int(binding_id)),
            )
            changed = max(getattr(cursor, "rowcount", 0), 0)
        finally:
            cursor.close()
        if changed:
            _insert_audit(connection, actor,
                          "enable" if enabled else "disable",
                          "binding#%d" % int(binding_id),
                          detail="绑定%s" % ("启用" if enabled else "停用"))
        return changed


def set_phone_enabled(connection, phone_no, enabled, *, actor):
    """停用/启用一个号码（停用的号码不进任何读面）。返回受影响行数。"""
    with transaction(connection):
        cursor = connection.cursor()
        try:
            cursor.execute(
                "UPDATE `dim_phone_number` SET `enabled` = %s, "
                "`updated_by` = %s, `updated_at` = %s WHERE `phone_no` = %s",
                (1 if enabled else 0, actor, _utc_now_text(),
                 str(phone_no or "").strip()),
            )
            changed = max(getattr(cursor, "rowcount", 0), 0)
        finally:
            cursor.close()
        if changed:
            _insert_audit(connection, actor,
                          "enable" if enabled else "disable",
                          str(phone_no or "").strip(),
                          detail="号码%s" % ("启用" if enabled else "停用"))
        return changed


def set_phone_user(connection, phone_no, user, *, actor):
    """认领：把号码的实际持卡人改成 ``user``（业务方确认后调用）。

    认领成功后 ``needs_claim`` 归零。返回受影响行数（0 = 号码不存在）。
    """
    with transaction(connection):
        cursor = connection.cursor()
        try:
            cursor.execute(
                "UPDATE `dim_phone_number` SET `actual_user` = %s, "
                "`needs_claim` = 0, `updated_by` = %s, `updated_at` = %s "
                "WHERE `phone_no` = %s",
                (user or None, actor, _utc_now_text(), phone_no),
            )
            changed = max(getattr(cursor, "rowcount", 0), 0)
        finally:
            cursor.close()
        if changed:
            _insert_audit(connection, actor, "claim", phone_no,
                          detail="认领持卡人：%s" % (user or "(清空)"))
        return changed


# ---------------------------------------------------------------------------
# 读路径
# ---------------------------------------------------------------------------


def fetch_phone_owner(connection, phone_no):
    """按号码反查持卡人。

    返回 dict（含 actual_user / dept / physical_device / status），
    查不到返回 ``None``。这是「收到验证码该找谁」的主查询。
    """
    rows = _fetch_all(
        connection,
        "SELECT `phone_no`, `phone_type`, `actual_user`, `account_name`, "
        "`owner`, `dept`, `physical_device`, `status`, `needs_claim`, `note` "
        "FROM `dim_phone_number` WHERE `phone_no` = %s AND `enabled` = 1",
        (str(phone_no or "").strip(),),
    )
    if not rows:
        return None
    row = rows[0]
    if not isinstance(row, dict):
        keys = ("phone_no", "phone_type", "actual_user", "account_name",
                "owner", "dept", "physical_device", "status", "needs_claim",
                "note")
        row = dict(zip(keys, row))
    return row


def fetch_phones_by_user(connection, user):
    """按人查名下所有号码（号码升序）。"""
    return _fetch_all(
        connection,
        "SELECT `phone_no`, `phone_type`, `physical_device`, `status`, "
        "`dept`, `note` FROM `dim_phone_number` "
        "WHERE `actual_user` = %s AND `enabled` = 1 ORDER BY `phone_no`",
        (user,),
    )


def fetch_binding_phones(connection, keyword):
    """按平台账号关键词反查接收验证码的号码。

    回答「XX 店铺账号要验证码，发给哪个号」。返回行内含该号持卡人。
    """
    like = "%%%s%%" % str(keyword or "").strip()
    return _fetch_all(
        connection,
        "SELECT b.`platform_account`, b.`channel_category`, b.`phone_no`, "
        "p.`actual_user`, p.`physical_device`, p.`status` "
        "FROM `dim_phone_binding` b "
        "LEFT JOIN `dim_phone_number` p ON p.`phone_no` = b.`phone_no` "
        "WHERE b.`enabled` = 1 AND b.`platform_account` LIKE %s "
        "ORDER BY b.`platform_account`, b.`phone_no`",
        (like,),
    )


def fetch_bindings_by_phone(connection, phone_no):
    """查某个号码绑定了哪些平台账号（换号/销户前的影响面评估）。"""
    return _fetch_all(
        connection,
        "SELECT `channel_category`, `platform_account`, `note` "
        "FROM `dim_phone_binding` "
        "WHERE `phone_no` = %s AND `enabled` = 1 "
        "ORDER BY `channel_category`, `platform_account`",
        (str(phone_no or "").strip(),),
    )


def fetch_unclaimed(connection):
    """待认领清单：推断不出持卡人的手机号（座机不进）。"""
    return _fetch_all(
        connection,
        "SELECT `phone_no`, `phone_type`, `account_name`, `dept`, `owner`, "
        "`status`, `note` FROM `dim_phone_number` "
        "WHERE `needs_claim` = 1 AND `enabled` = 1 "
        "ORDER BY `dept`, `phone_no`",
    )


def fetch_phone_list(connection, keyword="", unclaimed_only=False):
    """台账页列表读面：全字段（含 enabled / needs_claim / 更新时间）。

    ``keyword`` 命中号码/持卡人/开户名/归属人/部门/物理手机/备注任一；
    ``unclaimed_only=True`` 只看待认领手机号。按号码升序。
    """
    sql = (
        "SELECT `phone_no`, `phone_type`, `plan_name`, `account_name`, "
        "`actual_user`, `owner`, `dept`, `corp_owner`, `physical_device`, "
        "`status`, `broadband_account`, `broadband_status`, `address`, "
        "`note`, `needs_claim`, `enabled`, `updated_by`, `updated_at` "
        "FROM `dim_phone_number`"
    )
    clauses, params = [], []
    kw = str(keyword or "").strip()
    if kw:
        like = "%%%s%%" % kw
        clauses.append(
            "(`phone_no` LIKE %s OR `actual_user` LIKE %s OR "
            "`account_name` LIKE %s OR `owner` LIKE %s OR `dept` LIKE %s OR "
            "`physical_device` LIKE %s OR `note` LIKE %s)")
        params.extend([like] * 7)
    if unclaimed_only:
        clauses.append("`needs_claim` = 1 AND `enabled` = 1")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY `phone_no`"
    return _fetch_all(connection, sql, tuple(params))


def fetch_bindings(connection, keyword=""):
    """账号绑定列表读面（含主键 id 与持卡人信息，含停用行）。

    ``keyword`` 命中平台账号/渠道类目/号码任一。按类目、账号、号码排序。
    """
    sql = (
        "SELECT b.`id`, b.`phone_no`, b.`channel_category`, "
        "b.`platform_account`, b.`note`, b.`enabled`, b.`updated_by`, "
        "b.`updated_at`, p.`actual_user`, p.`physical_device`, p.`status` "
        "FROM `dim_phone_binding` b "
        "LEFT JOIN `dim_phone_number` p ON p.`phone_no` = b.`phone_no`"
    )
    params = ()
    kw = str(keyword or "").strip()
    if kw:
        like = "%%%s%%" % kw
        sql += (" WHERE b.`platform_account` LIKE %s OR "
                "b.`channel_category` LIKE %s OR b.`phone_no` LIKE %s")
        params = (like, like, like)
    sql += (" ORDER BY b.`channel_category`, b.`platform_account`, "
            "b.`phone_no`")
    return _fetch_all(connection, sql, params)


def fetch_phone_audit(connection, limit=200):
    """号码台账变更流水（操作记录页第八源）。时间倒序。"""
    return _fetch_all(
        connection,
        "SELECT `actor`, `action`, `phone_no`, `detail`, `created_at` "
        "FROM `dim_phone_number_audit` "
        "ORDER BY `created_at` DESC, `id` DESC LIMIT %s",
        (int(limit),),
    )


def summarize_registry(connection):
    """台账体检：总数 / 待认领 / 按持卡人分布 / 失效号码数。"""
    total = _fetch_all(
        connection, "SELECT COUNT(*) AS c FROM `dim_phone_number`")
    unclaimed = _fetch_all(
        connection,
        "SELECT COUNT(*) AS c FROM `dim_phone_number` "
        "WHERE `needs_claim` = 1 AND `enabled` = 1",
    )
    by_user = _fetch_all(
        connection,
        "SELECT `actual_user`, COUNT(*) AS c FROM `dim_phone_number` "
        "WHERE `enabled` = 1 GROUP BY `actual_user` ORDER BY c DESC",
    )
    dead = _fetch_all(
        connection,
        "SELECT COUNT(*) AS c FROM `dim_phone_number` "
        "WHERE `enabled` = 1 AND (`status` IN ('停机', '空号', '工信部关停'))",
    )

    def _count(rows):
        if not rows:
            return 0
        row = rows[0]
        return int(row["c"] if isinstance(row, dict) else row[0])

    return {
        "total": _count(total),
        "unclaimed": _count(unclaimed),
        "dead": _count(dead),
        "by_user": [
            (row["actual_user"] if isinstance(row, dict) else row[0],
             int(row["c"] if isinstance(row, dict) else row[1]))
            for row in by_user
        ],
    }
