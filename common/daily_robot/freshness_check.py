# -*- coding: utf-8 -*-
"""L3 数据新鲜度看门狗（A3）：断言关键事实表存在 T-1 业务日行。

覆盖两条「在运转」的渠道线事实表：

* ``fact_channel_daily_sales`` —— 渠道日销（AI 表格 → ``extract-channel``）；
* ``fact_daily_report_offline`` ``region='qudao'`` —— 渠道日报
  （``fact_channel_daily_sales`` → ``extract_ecom_people`` 投影）。

新鲜度口径（2026-10-15 裁决）：判 **存在 ``business_date = T-1`` 的行**，
**禁用 ``max(business_date) >= T-1``** —— 换月后 AI 表格含未来日期
（10 月 max 已达 10-31），max 口径会永远绿灯、掩盖真实缺口。

缺则经 C5 巡检告警通道发到运维群（``kind='alert'``，``region='ops'``）；
T-1 非工作日（``dim_calendar.is_workday=0``）**抑制告警但照常检查**——
日报按工作日口径、周末/节假日缺行属正常，抑制只省打扰不改判；日历未
覆盖（无行）按工作日处理（fail-open，宁可查也不误判为休息）。
"""

import contextlib
from dataclasses import dataclass

from common.daily_robot.inspection_alert import Finding, emit

#: 服务线标识（注册表 / 调度命令表 / ops-web 参数入口共用）。
SERVICE_ID = "freshness-check"


@dataclass(frozen=True)
class FreshnessCheck:
    """一条新鲜度断言：目标表（可选 region 过滤）+ 缺失时的严重度/文案。"""

    code: str
    table: str
    region: str | None
    severity: str
    label: str


#: 巡检清单（两表）：渠道日销是事实源（缺＝上游断，critical）；
#: 渠道日报由其派生（缺＝投影断，warning）。
CHECKS = (
    FreshnessCheck(
        "channel_daily_sales_missing", "fact_channel_daily_sales",
        None, "critical", "渠道日销",
    ),
    FreshnessCheck(
        "channel_daily_report_missing", "fact_daily_report_offline",
        "qudao", "warning", "渠道日报",
    ),
)


def fetch_is_workday(connection, business_date):
    """``dim_calendar`` 工作日判定：``True``/``False``/``None``（无行＝未覆盖）。"""
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(
            "SELECT `is_workday` FROM `dim_calendar` WHERE `business_date` = %s",
            (business_date,),
        )
        row = cursor.fetchone()
    if not row:
        return None
    value = row.get("is_workday") if isinstance(row, dict) else row[0]
    return None if value is None else bool(value)


def table_has_business_date(connection, table, business_date, region=None):
    """目标表在 *business_date* 是否存在行（``region`` 非空则加过滤）。"""
    sql = f"SELECT 1 AS `hit` FROM `{table}` WHERE `business_date` = %s"
    params = [business_date]
    if region is not None:
        sql += " AND `region` = %s"
        params.append(region)
    sql += " LIMIT 1"
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, tuple(params))
        return cursor.fetchone() is not None


def inspect(connection, *, business_date, checks=CHECKS):
    """逐表断言 T-1 存在性 → 缺失发现元组（全部健康则空）。"""
    findings = []
    for check in checks:
        if table_has_business_date(
            connection, check.table, business_date, check.region
        ):
            continue
        scope = f"（region={check.region}）" if check.region else ""
        findings.append(Finding(
            code=check.code,
            severity=check.severity,
            text=(
                f"{check.label}表 `{check.table}`{scope} 缺 "
                f"{business_date.isoformat()}（T-1）行——上游采集/提取可能中断。"
            ),
        ))
    return tuple(findings)


def run_freshness_check(connection, outbox, *, business_date, now,
                        service_id=SERVICE_ID, params=None, checks=CHECKS,
                        dedupe_override=None, dry=False,
                        skip_workday_gate=False):
    """巡检一次：断言两表 → 达阈值入队告警。返回结果字典（供 handler 打印）。

    * 永远执行 :func:`inspect`（便于 ``--dry`` 核对）；
    * 仅当 T-1 是休息日（``is_workday=False``）时**抑制入队**，
      状态记 ``suppressed_non_workday``；``skip_workday_gate=True``
      可强制照常入队（人工核对任意日期时用）；
    * ``dry=True`` 只报告不入队。
    """
    findings = inspect(connection, business_date=business_date, checks=checks)
    result = {
        "status": "ok",
        "business_date": business_date.isoformat(),
        "findings": len(findings),
    }
    if dry:
        result["status"] = "dry"
        return result
    if (findings and not skip_workday_gate
            and fetch_is_workday(connection, business_date) is False):
        result["status"] = "suppressed_non_workday"
        return result
    result["status"] = emit(
        outbox,
        service_id=service_id or SERVICE_ID,
        findings=findings,
        business_date=business_date,
        now=now,
        params=params,
        override_suffix=dedupe_override,
    )
    return result
