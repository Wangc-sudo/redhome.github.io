# -*- coding: utf-8 -*-
"""工作日历 DB 裁决层（``dim_calendar_override``）+ ``dim_calendar`` 派生合并。

两层模型（2026-10-10 运维裁决「后端太复杂，要给非技术同学用」）：

* **基线**：JSON 种子承载历史月（2025-01~2026-12，法定假 + 大小休锚点）
  + 规则兜底（``calendar_utils.synthesize_fallback_months``，周日休 +
  每月第 3 个周六大休）；
* **裁决层（DB，本模块）**：``dim_calendar_override`` 按日记上班/休息
  覆盖——``source='holiday_cn'``（运维中心「导入法定节假日」一键写入）
  与 ``source='manual'``（「工作日历」页点日期写入）——非技术同学全程
  页面操作，不碰 git/CLI/PR。

``dim_calendar`` = 基线 ∪ override（逐日 override 赢）：extract-mart 每日
重建时合并（``apply_overrides``），ops-web 操作后即时重写受影响月份
（``rewrite_month``）——两条路径同函数合并，口径不漂移；override 行
独立于派生表，extract 的全量替换永不冲掉人工裁决与法定导入。
"""

import contextlib
import uuid
from datetime import datetime, timezone

from common.calendar_utils import load_calendar_seed, month_days
from common.public_data.calendar_import import (
    fetch_holiday_cn,
    map_days_to_overrides,
)
from common.public_data.db import transaction

#: 合法 override 来源：holiday_cn=法定节假日导入 / manual=页面人工裁决。
OVERRIDE_SOURCES = ("holiday_cn", "manual")

_OVERRIDE_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_calendar_override` (\n"
    "  `business_date` DATE NOT NULL,\n"
    "  `is_workday` TINYINT(1) NOT NULL,\n"
    "  `source` VARCHAR(16) NOT NULL,\n"
    "  `note` VARCHAR(255) DEFAULT NULL,\n"
    "  `updated_by` VARCHAR(64) DEFAULT NULL,\n"
    "  `updated_at` DATETIME(6) DEFAULT NULL,\n"
    "  PRIMARY KEY (`business_date`),\n"
    "  KEY `idx_source` (`source`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_AUDIT_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_calendar_override_audit` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `actor` VARCHAR(64) NOT NULL,\n"
    "  `action` VARCHAR(16) NOT NULL,\n"
    "  `business_date` DATE DEFAULT NULL,\n"
    "  `year` SMALLINT DEFAULT NULL,\n"
    "  `month` TINYINT DEFAULT NULL,\n"
    "  `detail` VARCHAR(255) DEFAULT NULL,\n"
    "  `created_at` DATETIME(6) NOT NULL,\n"
    "  KEY `idx_created_at` (`created_at`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

#: 2026-10 运维裁决随迁移落库（原 JSON broadcastAdjust 迁移到 DB 裁决层，
#: 最终 rest 与现网逐位一致）：10-07 国庆假改上班、10-31 改大休。
_SEED_2026_10_ADJUST = (
    "INSERT IGNORE INTO `dim_calendar_override` "
    "(`business_date`, `is_workday`, `source`, `note`, `updated_by`, "
    " `updated_at`) VALUES "
    "('2026-10-07', 1, 'manual', "
    "'2026-10-07 运维裁决：国庆假改上班（线下填报工作日清单为准）', "
    "'migration', UTC_TIMESTAMP(6)), "
    "('2026-10-31', 0, 'manual', "
    "'2026-10-07 运维裁决：小休周六改大休', "
    "'migration', UTC_TIMESTAMP(6))"
)


def calendar_override_ddl_statements() -> tuple:
    """迁移语句：override 表 + 审计表 + 2026-10 裁决行（幂等）。"""
    return (_OVERRIDE_DDL, _AUDIT_DDL, _SEED_2026_10_ADJUST)


def _utc_now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _insert_audit(connection, actor, action, *, business_date=None,
                  year=None, month=None, detail=""):
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(
            "INSERT INTO `dim_calendar_override_audit` "
            "(`actor`, `action`, `business_date`, `year`, `month`, "
            " `detail`, `created_at`) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (actor, action, business_date, year, month, detail or None,
             _utc_now_text()),
        )


def fetch_overrides(connection, start=None, end=None):
    """读裁决层：``{business_date: (is_workday, source, note)}``。

    *start* / *end*（``date``）给定时只取闭区间（extract 按行集窗口取）。
    """
    sql = ("SELECT `business_date`, `is_workday`, `source`, `note` "
           "FROM `dim_calendar_override`")
    params = ()
    if start is not None and end is not None:
        sql += " WHERE `business_date` BETWEEN %s AND %s"
        params = (start, end)
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    overrides = {}
    for row in rows:
        get = row.get if isinstance(row, dict) else None
        if get is not None:
            day, is_workday, source, note = (
                get("business_date"), get("is_workday"),
                get("source"), get("note"),
            )
        else:
            day, is_workday, source, note = row
        overrides[day] = (int(is_workday), source, note)
    return overrides


def apply_overrides(rows, overrides):
    """基线行集合并裁决层（纯函数）：override 逐日赢。

    *rows* 为 ``[(business_date, is_workday, source, note)]``（extract 与
    ops-web 共用），*overrides* 为 :func:`fetch_overrides` 的映射。返回
    新列表（顺序与 *rows* 一致）。
    """
    merged = []
    for business_date, is_workday, source, note in rows:
        override = overrides.get(business_date)
        if override is not None:
            is_workday, source, note = override
        merged.append((business_date, is_workday, source, note))
    return merged


def merge_overrides(connection, rows):
    """按行集日期窗口取裁决层并合并（extract 专用便捷封装）。"""
    if not rows:
        return rows
    days = [business_date for business_date, *_ in rows]
    return apply_overrides(rows, fetch_overrides(connection, min(days), max(days)))


def upsert_override(connection, business_date, is_workday, *, actor,
                    note="", source="manual"):
    """页面点日期：写/改一天的 上班(1)/休息(0) 覆盖（审计 action=upsert）。"""
    if source not in OVERRIDE_SOURCES:
        raise ValueError("source must be one of " + ",".join(OVERRIDE_SOURCES))
    with transaction(connection):
        with contextlib.closing(connection.cursor()) as cursor:
            cursor.execute(
                "INSERT INTO `dim_calendar_override` "
                "(`business_date`, `is_workday`, `source`, `note`, "
                " `updated_by`, `updated_at`) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "`is_workday` = VALUES(`is_workday`), "
                "`source` = VALUES(`source`), `note` = VALUES(`note`), "
                "`updated_by` = VALUES(`updated_by`), "
                "`updated_at` = VALUES(`updated_at`)",
                (business_date, int(is_workday), source, note or None,
                 actor, _utc_now_text()),
            )
        _insert_audit(
            connection, actor, "upsert", business_date=business_date,
            detail=f"{'上班' if is_workday else '休息'}（{source}）{note}",
        )


def delete_override(connection, business_date, *, actor):
    """恢复规则：删一天的覆盖行（审计 action=reset）。返回是否删到。"""
    with transaction(connection):
        with contextlib.closing(connection.cursor()) as cursor:
            cursor.execute(
                "DELETE FROM `dim_calendar_override` WHERE `business_date` = %s",
                (business_date,),
            )
            hit = cursor.rowcount > 0
        if hit:
            _insert_audit(
                connection, actor, "reset", business_date=business_date,
                detail="恢复规则",
            )
    return hit


def reset_month_overrides(connection, year, month, *, actor):
    """本月恢复规则：清除该月 manual 覆盖（法定导入 holiday_cn 行不动）。

    返回清除行数（审计 action=reset_month）。
    """
    with transaction(connection):
        with contextlib.closing(connection.cursor()) as cursor:
            cursor.execute(
                "DELETE FROM `dim_calendar_override` "
                "WHERE `source` = 'manual' "
                "AND YEAR(`business_date`) = %s AND MONTH(`business_date`) = %s",
                (int(year), int(month)),
            )
            deleted = cursor.rowcount
        _insert_audit(
            connection, actor, "reset_month", year=int(year), month=int(month),
            detail=f"清除人工裁决 {deleted} 行（法定导入保留）",
        )
    return deleted


def import_year_overrides(connection, year, *, actor, fetcher=None):
    """导入法定节假日到裁决层：该年 holiday_cn 行整年替换（manual 不动）。

    返回 ``{"deleted", "inserted", "holidays", "makeup"}``（审计
    action=import）。
    """
    days = (fetcher or fetch_holiday_cn)(year)
    overrides = map_days_to_overrides(year, days)
    holidays = sum(1 for _, is_workday, _ in overrides if not is_workday)
    makeup = len(overrides) - holidays
    with transaction(connection):
        with contextlib.closing(connection.cursor()) as cursor:
            cursor.execute(
                "DELETE FROM `dim_calendar_override` "
                "WHERE `source` = 'holiday_cn' AND YEAR(`business_date`) = %s",
                (int(year),),
            )
            deleted = cursor.rowcount
            cursor.executemany(
                "INSERT INTO `dim_calendar_override` "
                "(`business_date`, `is_workday`, `source`, `note`, "
                " `updated_by`, `updated_at`) "
                "VALUES (%s, %s, 'holiday_cn', %s, %s, %s)",
                [
                    (day, is_workday, name, actor, _utc_now_text())
                    for day, is_workday, name in overrides
                ],
            )
        _insert_audit(
            connection, actor, "import", year=int(year),
            detail=f"holiday-cn 导入：+{len(overrides)}（休 {holidays} / "
                   f"调休上班 {makeup}）/-{deleted}",
        )
    return {
        "deleted": deleted, "inserted": len(overrides),
        "holidays": holidays, "makeup": makeup,
    }


def rewrite_month(connection, seed_path, year, month, *, today=None,
                  strict=True):
    """按当前基线 + 裁决层重写 ``dim_calendar`` 单月（ops-web 操作后即时
    生效；下轮 extract 全量重建同口径，无漂移窗口）。

    返回写入行数。基线 = ``load_calendar_seed(seed_path)``（含规则兜底），
    种子未覆盖该月时走兜底行（source=rule_fallback），override 仍叠加。
    兜底窗口外的月份：``strict=True``（默认）拒写不静默造口径；
    ``strict=False`` 跳过返回 0（跨年导入场景——窗口随 extract 每日滚动，
    出窗月份由后续重建自然覆盖，override 行早已在库里）。
    """
    baseline = {
        (y, m): (rest_days, source)
        for y, m, rest_days, source in load_calendar_seed(
            seed_path, today=today
        )
    }
    rest_days, source = baseline.get((int(year), int(month)), (None, None))
    if rest_days is None:
        if not strict:
            return 0
        raise ValueError(f"{year}-{month:02d} 不在日历基线覆盖范围")
    rest = set(rest_days)
    rows = [
        (day, 0 if day.day in rest else 1, source, None)
        for day in month_days(year, month)
    ]
    rows = merge_overrides(connection, rows)

    run_id = f"ops-calendar-{uuid.uuid4()}"
    synced_at = _utc_now_text()
    with transaction(connection):
        with contextlib.closing(connection.cursor()) as cursor:
            cursor.execute(
                "DELETE FROM `dim_calendar` "
                "WHERE YEAR(`business_date`) = %s AND MONTH(`business_date`) = %s",
                (int(year), int(month)),
            )
            cursor.executemany(
                "INSERT INTO `dim_calendar` "
                "(`business_date`, `is_workday`, `source`, `note`, "
                " `synced_at`, `sync_run_id`) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    (day, is_workday, src, note, synced_at, run_id)
                    for day, is_workday, src, note in rows
                ],
            )
    return len(rows)


def fetch_month_days(connection, year, month):
    """页面月历视图：``dim_calendar`` 当月逐日 ``[(business_date,
    is_workday, source, note)]``（空列表=未覆盖）。"""
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(
            "SELECT `business_date`, `is_workday`, `source`, `note` "
            "FROM `dim_calendar` "
            "WHERE YEAR(`business_date`) = %s AND MONTH(`business_date`) = %s "
            "ORDER BY `business_date`",
            (int(year), int(month)),
        )
        rows = cursor.fetchall()
    result = []
    for row in rows:
        get = row.get if isinstance(row, dict) else None
        if get is not None:
            result.append((
                get("business_date"), int(get("is_workday")),
                get("source"), get("note"),
            ))
        else:
            day, is_workday, src, note = row
            result.append((day, int(is_workday), src, note))
    return result


def fetch_recent_audits(connection, limit=20):
    """页面底部「最近修正记录」：按时间倒序 *limit* 条。"""
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(
            "SELECT `actor`, `action`, `business_date`, `year`, `month`, "
            "`detail`, `created_at` FROM `dim_calendar_override_audit` "
            "ORDER BY `id` DESC LIMIT %s",
            (int(limit),),
        )
        rows = cursor.fetchall()
    return rows
