"""月目标自动结转 + 月末核对提醒（2026-10-09 运维裁决「结转+月度提醒」）。

背景缺口：``dim_report_target`` 按 ``year_month`` 键控，翻月无新行；
报数快照源（report_intake._monthly_target_for）在 DB 无行时兜底 Nacos
``monthlyTargets`` 旧值，会把上月目标静默当本月用。两条调度管道闭环：

* ``target-rollover``（每月 1 日 06:00）：上月目标幂等结转到当月——
  人工已录入的键不覆盖（ops-web 名册页可提前录下月目标），报数快照
  自此拿到本月口径；
* ``target-remind``（每月 28 日 10:00）：向当月有月目标的区域群提醒
  核对下月目标（双保险），消息附名册页地址。
"""

from common.public_data import report_roster

#: 名册页地址（提醒消息里引导运维核对/录入）。
OPS_WEB_ROSTER_URL = "http://203.205.91.241:18100/roster"

#: scope → 中文名（消息文案；与 ops-web 名册页标签一致）。
SCOPE_LABELS = {
    "hangzhou": "杭州",
    "shaoxing": "绍兴",
    "junpin": "君品雅院",
    "vanke": "万科&大莲花&团购",
    "offline_all": "线下整体",
    "dining": "餐饮/部门",
}

#: 提醒标题（robot_outbox.title）。
REMIND_TITLE = "月目标核对提醒"


def shift_year_month(year_month, months):
    """``YYYY-MM`` 平移 *months* 个月（可负），返回 ``YYYY-MM``。"""
    year, month = (int(part) for part in year_month.split("-"))
    index = year * 12 + (month - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def run_rollover(connection, *, anchor_date, actor="target-rollover",
                 apply=True):
    """结转锚定日所在月的上月目标到当月。

    返回 ``{"from_month", "to_month", "inserted", "skipped"}``；
    ``apply=False`` 为 dry-run（只读不写）。
    """
    to_month = anchor_date.strftime("%Y-%m")
    from_month = shift_year_month(to_month, -1)
    result = report_roster.carry_forward_targets(
        connection, from_month=from_month, to_month=to_month,
        actor=actor, apply=apply,
    )
    return {
        "from_month": from_month,
        "to_month": to_month,
        "inserted": result["inserted"],
        "skipped": result["skipped"],
    }


def build_remind_markdown(*, scope, year_month, next_month, people, total):
    """月末核对提醒正文（markdown）：当月概况 + 结转预告 + 名册页入口。"""
    label = SCOPE_LABELS.get(scope, scope)
    return (
        f"### {REMIND_TITLE}（{label}）\n\n"
        f"- 本月（{year_month}）已登记月目标 **{people}** 人/对象，"
        f"合计 **{total:,.0f}** 元\n"
        f"- 下月（{next_month}）目标将于 **1 日 06:00** 自动按本月结转；"
        f"已在名册页手工录入的下月目标以人工为准，不会被覆盖\n"
        f"- 如需调整，请提前在运维中心名册页核对（页面可切换目标月份）："
        f"{OPS_WEB_ROSTER_URL}"
    )


def run_remind(connection, outbox, *, region_configs, anchor_date, now,
               dedupe_suffix=None):
    """月末提醒：当月有月目标且配置了群机器人的 scope，一群一条。

    返回 ``[(scope, "enqueued" | "already_sent" | "no_robot")]``，
    顺序按 scope 字典序（稳定输出，便于流水核对）；当月无月目标的
    scope 不出现在结果里（无目标可核对，不打扰）。
    """
    year_month = anchor_date.strftime("%Y-%m")
    next_month = shift_year_month(year_month, 1)
    summary = report_roster.fetch_target_month_summary(connection, year_month)
    results = []
    for scope in sorted(summary):
        info = summary[scope]
        if scope not in region_configs:
            results.append((scope, "no_robot"))
            continue
        body = build_remind_markdown(
            scope=scope, year_month=year_month, next_month=next_month,
            people=info["people"], total=info["total"],
        )
        enqueued = outbox.enqueue(
            region=scope,
            kind="target_remind",
            business_date=anchor_date,
            title=REMIND_TITLE,
            body_md=body,
            created_at=now,
            dedupe_suffix=dedupe_suffix,
        )
        results.append((scope, "enqueued" if enqueued else "already_sent"))
    return results
