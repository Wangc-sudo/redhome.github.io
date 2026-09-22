# -*- coding: utf-8 -*-
"""日报机器人的 mart 侧任务：remind / check（阶段 4）。

与 :mod:`common.daily_robot.core` 的 ``do_remind`` / ``do_check`` 的区别只在
**数据来源与投递方式**，文案逐字一致：

* 名单来自 ``mart_ops``（``dim_robot_member`` + 事实表反连接，经
  :mod:`common.metrics.daily_report`），不再直连钉钉 AI 表；
* 消息写 ``robot_outbox`` 由 ``dingtalk-gateway`` 投递，不再调钉钉 client；
* 幂等由 outbox 唯一键承载（``region:kind:business_date``），不再用
  ``stateFile``——机器人变为无状态。

有意的行为变化：``missing``（表内有人但通讯录无映射）在 mart 世界恒为空
——未填名单本就出自 ``dim_robot_member``，人人有 user_id。
"""

from dataclasses import dataclass, field
from datetime import date

from common.metrics.daily_report import (
    fetch_filled_names,
    fetch_region_members,
    fetch_workdays,
    unfilled_members,
)


_WEEKDAYS = "一二三四五六日"


class MartTaskError(RuntimeError):
    """任务无法安全执行（如当月日历未物化）。"""


@dataclass(frozen=True)
class TaskOutcome:
    """一次 remind/check 的结果（供 CLI 打印与测试断言）。"""

    status: str  # "enqueued" | "already_sent" | "all_filled" | "rest_day"
    kind: str
    business_date: date
    unfilled: tuple = field(default_factory=tuple)
    enqueued: bool = False


# ---------------------------------------------------------------------------
# 消息构建（与 core.do_remind / do_check 逐字一致）
# ---------------------------------------------------------------------------

def build_reminder_message(*, display, month, day, weekday, unfilled, url, missing=()):
    """18:30 提醒文案。返回 ``(title, body_md)``。"""
    lines = [f"### 📋 销售日报填写提醒（{display} {month}月{day}日 周{weekday}）", ""]
    lines.append(f"以下 **{len(unfilled)}** 位同事还未填写今日销售日报，请尽快填写：")
    lines.append("")
    lines.append(f"**{'、'.join(unfilled)}**")
    lines.append("")
    lines.append("也可直接在群里 **@日报小机器人 + 数字** 报数（如 `@日报小机器人 12800`，报 0 也行）")
    lines.append("")
    lines.append(f"[点此填写]({url})")
    if missing:
        lines.append("")
        lines.append(f"（{'、'.join(missing)} 未在通讯录映射中，无法@，请手动提醒）")
    return "销售日报填写提醒", "\n".join(lines)


def build_check_message(*, display, month, day, unfilled, url, missing=()):
    """20:00 催办文案。返回 ``(title, body_md)``。"""
    lines = [f"### ⏰ 销售日报未填写（{display} {month}月{day}日）", ""]
    lines.append(f"截至 20:00，以下 **{len(unfilled)}** 位同事仍未填写：")
    lines.append("")
    lines.append(f"**{'、'.join(unfilled)}**")
    lines.append("")
    lines.append("已同步 DING 提醒以上人员，请在群里 @日报小机器人 报数或直接填写。")
    lines.append(f"[点此填写]({url})")
    if missing:
        lines.append("")
        lines.append(f"（{'、'.join(missing)} 未在通讯录映射中，无法@，请手动提醒）")
    return "销售日报未填写", "\n".join(lines)


def build_ding_content(*, display, month, day, weekday, url):
    """20:00 催办的 DING 正文（与现行 DING_CMD 的内容段逐字一致）。"""
    return (
        f"【销售日报催办】{display} {month}月{day}日（周{weekday}）：你还未填写今日销售日报，"
        f"请在群里 @日报小机器人 报数或填写表格 {url}"
    )


# ---------------------------------------------------------------------------
# 任务规划
# ---------------------------------------------------------------------------

def _require_workday(connection, business_date):
    """返回当天是否工作日。

    当月**无日历行** = 显式失败：日历未物化（``extract-mart`` 未跑）时不
    允许静默按休息日跳过——这替代了现行 ``today_info`` 的月份校验。
    """
    workdays = fetch_workdays(
        connection, year=business_date.year, month=business_date.month
    )
    if not workdays:
        raise MartTaskError(
            f"dim_calendar 缺少 {business_date:%Y-%m} 的日历行，请先运行 extract-mart"
        )
    return business_date in workdays


def _unfilled(connection, region, business_date, aliases):
    members = fetch_region_members(connection, region=region)
    filled = fetch_filled_names(
        connection, region=region, business_date=business_date
    )
    return unfilled_members(members, filled, aliases=aliases)


def run_remind(connection, outbox, *, region, display, table_url,
               business_date, now, aliases=None):
    """18:30 提醒：未填名单 → outbox（kind='remind'，@未填人）。"""
    if not _require_workday(connection, business_date):
        return TaskOutcome("rest_day", "remind", business_date)

    unfilled_rows = _unfilled(connection, region, business_date, aliases)
    if not unfilled_rows:
        return TaskOutcome("all_filled", "remind", business_date)

    names = [m["name"] for m in unfilled_rows]
    title, body = build_reminder_message(
        display=display,
        month=business_date.month,
        day=business_date.day,
        weekday=_WEEKDAYS[business_date.weekday()],
        unfilled=names,
        url=table_url,
    )
    enqueued = outbox.enqueue(
        region=region,
        kind="remind",
        business_date=business_date,
        title=title,
        body_md=body,
        at_user_ids=[m["user_id"] for m in unfilled_rows],
        created_at=now,
    )
    return TaskOutcome(
        "enqueued" if enqueued else "already_sent",
        "remind",
        business_date,
        unfilled=tuple(names),
        enqueued=enqueued,
    )


def run_check(connection, outbox, *, region, display, table_url,
              business_date, now, cc_user_ids=(), aliases=None):
    """20:00 催办：群消息（@未填人 + cc）+ DING 行（仅未填人）。

    两行 dedupe_key 各自独立（``check`` / ``ding``），任一行已存在只跳过
    自己——比现行单一 state key 更细粒度，部分失败可独立补齐。
    """
    if not _require_workday(connection, business_date):
        return TaskOutcome("rest_day", "check", business_date)

    unfilled_rows = _unfilled(connection, region, business_date, aliases)
    if not unfilled_rows:
        return TaskOutcome("all_filled", "check", business_date)

    names = [m["name"] for m in unfilled_rows]
    weekday = _WEEKDAYS[business_date.weekday()]
    member_ids = [m["user_id"] for m in unfilled_rows]

    title, body = build_check_message(
        display=display,
        month=business_date.month,
        day=business_date.day,
        unfilled=names,
        url=table_url,
    )
    check_enqueued = outbox.enqueue(
        region=region,
        kind="check",
        business_date=business_date,
        title=title,
        body_md=body,
        at_user_ids=[*member_ids, *cc_user_ids],
        created_at=now,
    )
    ding_enqueued = outbox.enqueue(
        region=region,
        kind="ding",
        business_date=business_date,
        title=title,
        body_md=build_ding_content(
            display=display,
            month=business_date.month,
            day=business_date.day,
            weekday=weekday,
            url=table_url,
        ),
        at_user_ids=member_ids,
        created_at=now,
    )

    enqueued = check_enqueued or ding_enqueued
    return TaskOutcome(
        "enqueued" if enqueued else "already_sent",
        "check",
        business_date,
        unfilled=tuple(names),
        enqueued=enqueued,
    )
