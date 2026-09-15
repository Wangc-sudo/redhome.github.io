# -*- coding: utf-8 -*-
"""Stream 报数落库（spec §9：报数 → gateway → 直接落 ``mart_ops``）。

``listener.ReportHandler`` 的 mart 版：门禁、解析、回执文案与现行逐字
一致，差别只在**落点与身份来源**：

* 身份来自 ``dim_robot_member``（``sender_staff_id`` 直查），不再维护
  「姓名 ↔ userId」白名单文件；
* 数值直写 ``fact_daily_report_offline``，不再回写钉钉 AI 表（其降级为
  非真源）；
* 月累计/完成率走共享口径 :mod:`common.metrics.daily_report`（含当天），
  不再就地求和。

**写入规则（业务键优先）**：按 ``(region, 表内用名, business_date)`` 查找
既有事实行——存在则就地更新 ``sales_amount``（保留其目标列与 PK），不
存在才插入 ``stream:{region}:{user_id}:{date}`` 主键的新行。这样切换
窗口内不会产生同人同日的重复行；切换完成后日报的 AI 表同步退役，
extract 不再重放这些行，冲突面整体消失。

**回执错误文案的有意改动**：处理异常的回执不再插值异常原文（现行
``f"⚠️ 处理报数时出错：{e}"`` 可能泄露内部细节），改为通用提示。
"""

import contextlib
import re
from dataclasses import dataclass
from datetime import date, datetime

from common.daily_robot.mart_tasks import MartTaskError
from common.metrics.daily_report import (
    elapsed_workdays,
    fetch_member,
    fetch_month_facts,
    fetch_workdays,
    summarize_people,
)


_WEEKDAYS = "一二三四五六日"

#: stream 来源写入的占位 run id（全零）：报数落库不属于任何同步 run，
#: 用它与 sync/extract 产生的行区分，便于审计与排查。
STREAM_RUN_ID = "00000000-0000-0000-0000-000000000000"

_AMOUNT_RE = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)")


@dataclass(frozen=True)
class IntakeOutcome:
    """一条报数消息的处理结果（reply 为给发送者的回执文本）。"""

    status: str  # recorded | not_workday | not_member | no_number
    reply: str
    region: str | None = None
    name: str | None = None
    value: object = None
    overwritten: bool = False


# ---------------------------------------------------------------------------
# 解析与回执文案（与 listener.py 逐字一致）
# ---------------------------------------------------------------------------

def parse_report_amount(text):
    """从报数文本提取金额；无法识别返回 ``None``。

    规则与现行 listener 一致：全角逗号归一、千分位逗号剔除、取文本中
    第一个数字（允许负数与小数），整数值返回 ``int``。
    """
    match = _AMOUNT_RE.search((text or "").replace("，", ","))
    if not match:
        return None
    num_str = match.group(1).replace(",", "")
    try:
        value = float(num_str)
    except ValueError:
        return None
    return int(value) if value == int(value) else value


def build_not_member_reply():
    return (
        "⛔ 报数功能仅限销售日报责任人使用。\n"
        "如需填写日报请联系管理员，或在表格中直接填写。"
    )


def build_not_workday_reply():
    return "今天不是销售日报工作日，无需报数～"


def build_format_hint(name):
    return (
        f"{name} 你好～报数格式：@提醒事项 数字\n"
        f"例如：@提醒事项 12800（当天无销量报 0）"
    )


def build_error_reply():
    return "⚠️ 处理报数时出错，请稍后再试，或直接在表格中填写。"


def build_recorded_reply(*, month, day, weekday, value, old_value, progress):
    """✅ 回执。old_value 为已格式化的旧值（或 None）；progress 为
    ``(total_disp, target_disp, ratio_str)`` 或 None。"""
    lines = [f"✅ 已记录 {month}月{day}日（周{weekday}）销量：{value}"]
    if old_value is not None:
        lines.append(f"🔁 已覆盖你之前填报的 {old_value}")
    if progress is not None:
        total_disp, target_disp, ratio_str = progress
        lines.append(f"📊 本月累计 {total_disp} / 目标 {target_disp}，完成 {ratio_str}")
    lines.append("祝您下班愉快 🎉")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 按群路由（spec §9：单连接、conversationId → region）
# ---------------------------------------------------------------------------

def region_for_conversation(region_configs, conversation_id):
    """按 ``open_conversation_id`` 反查区域配置；未登记返回 ``None``。"""
    if not conversation_id:
        return None
    for cfg in region_configs.values():
        if cfg.open_conversation_id == conversation_id:
            return cfg
    return None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def handle_report(connection, *, region_cfg, text, sender_uid, now):
    """处理一条报数消息。不写连接 commit（由调用方提交，与其他流一致）。"""
    business_date = now.date() if isinstance(now, datetime) else now
    workdays = fetch_workdays(connection, year=now.year, month=now.month)
    if not workdays:
        raise MartTaskError(
            f"dim_calendar 缺少 {now:%Y-%m} 的日历行，请先运行 extract-mart"
        )
    if business_date not in workdays:
        return IntakeOutcome("not_workday", build_not_workday_reply(),
                             region=region_cfg.region)

    member = fetch_member(connection, user_id=sender_uid)
    if member is None or member["region"] != region_cfg.region:
        return IntakeOutcome("not_member", build_not_member_reply(),
                             region=region_cfg.region)

    name = member["name"]
    value = parse_report_amount(text)
    if value is None:
        return IntakeOutcome("no_number", build_format_hint(name),
                             region=region_cfg.region, name=name)

    table_name = region_cfg.aliases.get(name, name)
    old_value = _write_report(
        connection,
        region=region_cfg.region,
        member=member,
        table_name=table_name,
        # 无 AI 表区域的月目标快照（有表区域为 None，目标由表行携带）。
        monthly_target=region_cfg.monthly_targets.get(table_name),
        business_date=business_date,
        value=value,
        now=now,
    )

    progress = _progress(
        connection,
        region=region_cfg.region,
        table_name=table_name,
        business_date=business_date,
        workdays=workdays,
    )
    overwritten = old_value is not None and float(old_value) != float(value)
    reply = build_recorded_reply(
        month=now.month,
        day=business_date.day,
        weekday=_WEEKDAYS[business_date.weekday()],
        value=value,
        old_value=_fmt_amount(old_value) if overwritten else None,
        progress=progress,
    )
    return IntakeOutcome(
        "recorded", reply,
        region=region_cfg.region, name=name, value=value,
        overwritten=overwritten,
    )


# ---------------------------------------------------------------------------
# 写入与进度
# ---------------------------------------------------------------------------

def _fetch_one(connection, sql, params):
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    if row is None:
        return None
    return dict(row) if isinstance(row, dict) else row


def _write_report(connection, *, region, member, table_name,
                  monthly_target, business_date, value, now):
    """业务键优先写入。返回旧值（无旧行或旧值为空 → None）。"""
    existing = _fetch_one(
        connection,
        "SELECT `source_record_id`, `sales_amount` "
        "FROM `fact_daily_report_offline` "
        "WHERE `region` = %s AND `responsible_person` = %s "
        "AND `business_date` = %s "
        "ORDER BY (`monthly_target` IS NULL) LIMIT 1",
        (region, table_name, business_date),
    )
    with contextlib.closing(connection.cursor()) as cursor:
        if existing is not None:
            cursor.execute(
                "UPDATE `fact_daily_report_offline` "
                "SET `sales_amount` = %s, `synced_at` = %s "
                "WHERE `source_record_id` = %s",
                (value, now, existing["source_record_id"]),
            )
            return existing.get("sales_amount")

        cursor.execute(
            "INSERT INTO `fact_daily_report_offline` "
            "(`source_record_id`, `region`, `responsible_person`, "
            "`department`, `business_date`, `sales_amount`, `monthly_target`, "
            "`synced_at`, `sync_run_id`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                f"stream:{region}:{member['user_id']}:{business_date.isoformat()}",
                region,
                table_name,
                member.get("dept_name"),
                business_date,
                value,
                monthly_target,
                now,
                STREAM_RUN_ID,
            ),
        )
    return None


def _progress(connection, *, region, table_name, business_date, workdays):
    """本月累计 / 目标 / 完成率（含当天；共享口径）。无有效目标 → None。"""
    elapsed = elapsed_workdays(workdays, today=business_date, include_today=True)
    facts = fetch_month_facts(
        connection, region=region,
        year=business_date.year, month=business_date.month,
    )
    rows = [
        row for row in facts
        if str(row.get("responsible_person") or "").strip() == table_name
    ]
    summary = summarize_people(rows, elapsed_days=elapsed).get(table_name)
    if summary is None or summary.rate is None:
        return None
    total_disp = _fmt_amount(summary.completed)
    target_disp = _fmt_amount(summary.target)
    return total_disp, target_disp, f"{summary.rate * 100:.1f}%"


def _fmt_amount(value):
    number = float(value)
    if number == int(number):
        return str(int(number))
    return str(round(number, 2))
