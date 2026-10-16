"""受权限管控的数据修正通道（``dim_data_fix_audit``，2026-10-16）。

背景：运维修数（如李树军 fact 行 region 归位 shaoxing→other）此前只能
裸写 SQL——无痕、无权限门。本模块把这类 UPDATE 收编成**类型化修正 API**：
只支持登记的修正类型（第一期：线下事实行人员归属归位），
``apply=False`` 预览（命中行数/金额/日期范围）、``apply=True`` 同事务
UPDATE + 审计落库。自由 SQL 永不开口子——新修正类型=代码评审+登记。

审计表 ``dim_data_fix_audit`` 落 mart_ops，进 ops-web 操作记录聚合
（第七源「数据修正」）。
"""

import contextlib
import uuid
from datetime import datetime, timezone

from common.public_data.db import transaction

#: 合法修正类型（第一期）。
FIX_TYPES = ("offline_person_region_realign",)

_DATA_FIX_AUDIT_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_data_fix_audit` (\n"
    "  `audit_id` CHAR(36) NOT NULL,\n"
    "  `actor` VARCHAR(64) NOT NULL,\n"
    "  `action` VARCHAR(32) NOT NULL,\n"
    "  `fix_type` VARCHAR(64) NOT NULL,\n"
    "  `target` VARCHAR(255) NOT NULL DEFAULT '',\n"
    "  `detail` VARCHAR(500) NOT NULL DEFAULT '',\n"
    "  `row_count` INT UNSIGNED NOT NULL DEFAULT 0,\n"
    "  `created_at` DATETIME(6) NOT NULL,\n"
    "  PRIMARY KEY (`audit_id`),\n"
    "  KEY `idx_data_fix_audit_created` (`created_at`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


def data_fix_ddl_statements() -> tuple[str, ...]:
    """``dim_data_fix_audit`` 建表语句（注册进 live_migrations，mart 侧）。"""
    return (_DATA_FIX_AUDIT_DDL,)


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _insert_audit(connection, *, actor, action, fix_type, target, detail,
                  row_count):
    sql = (
        "INSERT INTO `dim_data_fix_audit` "
        "(`audit_id`, `actor`, `action`, `fix_type`, `target`, `detail`, "
        " `row_count`, `created_at`) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
    )
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, (
            uuid.uuid4().hex, actor, action, fix_type, target[:255],
            detail[:500], row_count, _utc_now_text(),
        ))


def preview_person_region_realign(connection, *, person_name, from_region,
                                  to_region):
    """预览归位影响面：命中行数/金额合计/日期范围（只读）。"""
    sql = (
        "SELECT COUNT(*) AS `n`, COALESCE(SUM(`sales_amount`), 0) AS `amt`, "
        "MIN(`business_date`) AS `d0`, MAX(`business_date`) AS `d1` "
        "FROM `fact_daily_report_offline` "
        "WHERE `responsible_person` = %s AND `region` = %s"
    )
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, (person_name, from_region))
        row = cursor.fetchone()
    get = row.get if isinstance(row, dict) else None
    if get is None:
        row = {"n": row[0], "amt": row[1], "d0": row[2], "d1": row[3]}
        get = row.get
    return {
        "person_name": person_name,
        "from_region": from_region,
        "to_region": to_region,
        "row_count": int(get("n") or 0),
        "sales_amount": float(get("amt") or 0),
        "date_from": str(get("d0") or ""),
        "date_to": str(get("d1") or ""),
    }


def realign_offline_person_region(connection, *, person_name, from_region,
                                  to_region, actor):
    """人员归属归位：fact 行 region 从 from_region 改到 to_region（写+审计）。

    返回预览同形的统计（row_count 为实际更新行数）。零命中时**不写审计**
    （空操作不算修正）。
    """
    stats = preview_person_region_realign(
        connection, person_name=person_name, from_region=from_region,
        to_region=to_region)
    if stats["row_count"] == 0:
        return stats
    with transaction(connection):
        with contextlib.closing(connection.cursor()) as cursor:
            cursor.execute(
                "UPDATE `fact_daily_report_offline` SET `region` = %s "
                "WHERE `responsible_person` = %s AND `region` = %s",
                (to_region, person_name, from_region),
            )
            updated = cursor.rowcount
        _insert_audit(
            connection, actor=actor, action="realign",
            fix_type="offline_person_region_realign",
            target=person_name,
            detail=(f"{from_region}→{to_region}，{updated} 行"
                    f"（{stats['date_from']}~{stats['date_to']}，"
                    f"Σ{stats['sales_amount']:.2f}）"),
            row_count=updated,
        )
    stats["row_count"] = int(updated)
    return stats


def fetch_recent_audits(connection, *, limit=50):
    """数据修正审计读面（created_at 倒序）。"""
    sql = (
        "SELECT `actor`, `action`, `fix_type`, `target`, `detail`, "
        "`row_count`, `created_at` FROM `dim_data_fix_audit` "
        "ORDER BY `created_at` DESC LIMIT %s"
    )
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, (int(limit),))
        return cursor.fetchall()
