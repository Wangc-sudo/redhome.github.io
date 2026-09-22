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

#: 报数指标词（多板块报数只认这两个，2026-09-22 与运维定稿）。
_METRIC_WORDS = ("零售", "团购")

#: 门店别名（过渡期）：规范名 = 通讯录部门名（板块名与组织架构绑定）；
#: 群里的口语写法映射到规范名，填报习惯统一后可移除。
_STORE_ALIASES = {
    "万科体验馆": "万科体验馆",
    "万科": "万科体验馆",
    "莲荷里体验馆": "莲荷里体验馆",
    "莲荷里": "莲荷里体验馆",
    "酱酒体验馆": "莲荷里体验馆",
    "酱香体验馆": "莲荷里体验馆",
    "酱酒": "莲荷里体验馆",
    "酱香": "莲荷里体验馆",
    "大莲花": "莲荷里体验馆",
}

#: 多板块报数分词：门店词 / 指标词 / 数字，按序扫描（门店词长词优先）。
_TOKEN_RE = re.compile(
    r"(?P<store>" + "|".join(sorted(_STORE_ALIASES, key=len, reverse=True)) + r")"
    r"|(?P<metric>零售|团购)"
    r"|(?P<num>-?\d[\d,]*(?:\.\d+)?)"
)


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


def parse_report_metrics(text):
    """多板块报数解析：``[(门店规范名|None, 指标, 金额), ...]``。

    规则（2026-09-22 与运维定稿）：
    - 指标只认「零售」「团购」；门店词出现后成为后续数字的归属上下文，
      直到下一个门店词；
    - 门店词后直接跟数字 → 该店「零售」（如 ``万科体验馆/7560``）；
    - 无门店词 → 门店为 ``None``（由调用方按报数人部门补齐）；
    - 全文不含任何门店/指标词 → 返回 ``None``（走单金额旧路径）。
    """
    if not text:
        return None
    entries = []
    saw_label = False
    ctx_store = None
    pending_store = False
    pending_metric = None
    for m in _TOKEN_RE.finditer(text.replace("，", ",")):
        if m.group("store"):
            ctx_store = _STORE_ALIASES[m.group("store")]
            pending_store = True
            pending_metric = None
            saw_label = True
        elif m.group("metric"):
            pending_metric = m.group("metric")
            pending_store = False
            saw_label = True
        else:
            num_str = m.group("num").replace(",", "")
            try:
                value = float(num_str)
            except ValueError:
                continue
            value = int(value) if value == int(value) else value
            metric = pending_metric or "零售"
            entries.append((ctx_store, metric, value))
            pending_metric = None
            pending_store = False
    if not saw_label or not entries:
        return None
    return entries


def build_store_denied_reply(name, sender_dept, store):
    return (
        f"⛔ {name} 你好，你的部门是{sender_dept}，不能报{store}的数据哦。\n"
        "如有疑问请联系管理员。"
    )


def build_multi_recorded_reply(*, month, day, weekday, writes):
    """多板块回执。writes = ``[(数据格, 金额, 旧值|None, progress|None), ...]``。"""
    lines = [f"✅ 已记录 {month}月{day}日（周{weekday}）："]
    for cell, value, old_value, progress in writes:
        line = f"{cell}：{value}"
        if old_value is not None:
            line += f"（🔁 覆盖旧值 {old_value}）"
        lines.append(line)
        if progress is not None:
            total_disp, target_disp, ratio_str = progress
            lines.append(f"📊 {cell} 本月累计 {total_disp} / 目标 {target_disp}，完成 {ratio_str}")
    lines.append("祝您下班愉快 🎉")
    return "\n".join(lines)


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

    # 多板块报数（vanke 模型：数据格 = 门店 × {零售,团购}，权限 = 部门归属）。
    entries = parse_report_metrics(text)
    if entries is not None:
        root_dept = region_cfg.dept_order[0] if region_cfg.dept_order else None
        sender_dept = member.get("dept_name")
        writes = []
        for store_label, metric, value in entries:
            store = store_label or sender_dept
            if not store:
                return IntakeOutcome(
                    "no_number", build_format_hint(name),
                    region=region_cfg.region, name=name,
                )
            # 点名门店时：本部门只能报本店；根部门（dept_order[0]）可跨店。
            if (
                store_label
                and sender_dept != root_dept
                and store_label != sender_dept
            ):
                return IntakeOutcome(
                    "not_member",
                    build_store_denied_reply(name, sender_dept, store_label),
                    region=region_cfg.region, name=name,
                )
            cell = f"{store}·{metric}"
            old_value = _write_report(
                connection,
                region=region_cfg.region,
                member=member,
                table_name=cell,
                monthly_target=region_cfg.monthly_targets.get(cell),
                business_date=business_date,
                value=value,
                now=now,
                key_suffix=metric,
            )
            progress = _progress(
                connection,
                region=region_cfg.region,
                table_name=cell,
                business_date=business_date,
                workdays=workdays,
            )
            overwritten = old_value is not None and float(old_value) != float(value)
            writes.append(
                (
                    cell,
                    value,
                    _fmt_amount(old_value) if overwritten else None,
                    progress,
                )
            )
        reply = build_multi_recorded_reply(
            month=now.month,
            day=business_date.day,
            weekday=_WEEKDAYS[business_date.weekday()],
            writes=writes,
        )
        return IntakeOutcome(
            "recorded", reply,
            region=region_cfg.region, name=name,
            value=[w[1] for w in writes],
            overwritten=any(w[2] is not None for w in writes),
        )

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
                  monthly_target, business_date, value, now, key_suffix=None):
    """业务键优先写入。返回旧值（无旧行或旧值为空 → None）。

    *key_suffix*：多板块报数时追加到 ``source_record_id`` 末尾（同一成员
    同日多数据格各一行）；单金额旧路径不传，保持原键格式不变。
    """
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
                f"stream:{region}:{member['user_id']}:{business_date.isoformat()}"
                + (f":{key_suffix}" if key_suffix else ""),
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
