# -*- coding: utf-8 -*-
"""电商渠道日报（qudao 三段式）——渠道日销快报 + 人员完成率榜。

§1 渠道日销快报：旧渠道日报机器人 ``channel_report.build_markdown`` 的
DB 复刻（表格结构、环比、金额格式化逐字对齐），数据源从 AI 表换为
``mart_ops.fact_channel_daily_sales``（店铺后台导出 → sync → DB，口径不变）。
目标/达成率：region 配置 ``monthlyTargets``（渠道键）给出月目标时，
显示月目标与月累计达成率；未配置 → ``--``（与旧表无目标列时一致）。

§2/§3 人员完成率榜与任务进度：复用 ``render_bc_markdown``（杭州/绍兴同构），
其头部「时间进度 · 整体完成率」即任务进度段。
"""

import contextlib
from datetime import date

#: 渠道展示顺序（沿用旧渠道日报机器人 config 的 channelsOrder）。
CHANNEL_ORDER = ("天猫", "京东", "拼多多", "猫超", "即时零售", "直播", "私域", "抖音")


def _fmt_wan(value):
    """≥1万显示 X.X万，否则原值（与旧 fmt_wan 一致）。"""
    if value is None:
        return "--"
    if abs(value) >= 10000:
        return f"{value / 10000:.1f}万"
    return f"{value:,.0f}"


def _fmt_pct(value):
    return f"{value * 100:.1f}%"


def fetch_channel_daily(connection, *, business_date):
    """某日渠道销售额：``{渠道: 销售额}``（sales_amount NULL 的预填行忽略）。"""
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(
            "SELECT `channel`, SUM(`sales_amount`) AS `s` "
            "FROM `fact_channel_daily_sales` "
            "WHERE `business_date` = %s AND `sales_amount` IS NOT NULL "
            "GROUP BY `channel`",
            (business_date,),
        )
        rows = cursor.fetchall()
    return {
        str(row["channel"]).strip(): float(row["s"] or 0)
        for row in rows
        if row.get("channel")
    }


def fetch_channel_month_facts(connection, *, year, month):
    """当月渠道日销明细：``{渠道: {日期: 销售额}}``（月累计与环比用）。"""
    first, last = date(year, month, 1), date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(
            "SELECT `channel`, `business_date`, SUM(`sales_amount`) AS `s` "
            "FROM `fact_channel_daily_sales` "
            "WHERE `business_date` >= %s AND `business_date` < %s "
            "AND `sales_amount` IS NOT NULL GROUP BY `channel`, `business_date`",
            (first, last),
        )
        rows = cursor.fetchall()
    facts = {}
    for row in rows:
        if row.get("channel"):
            facts.setdefault(str(row["channel"]).strip(), {})[
                row["business_date"]
            ] = float(row["s"] or 0)
    return facts


def _prev_workday_amount(day_facts, business_date, workdays):
    """环比基数：上一工作日的销售额（无 → None）。"""
    prev = [d for d in sorted(workdays) if d < business_date]
    if not prev:
        return None
    return day_facts.get(prev[-1])


def resolve_channel_business_date(month_facts, business_date):
    """渠道业务日回退：*business_date*（不含）之前最近一个有效数据日。

    按日合计非零才算有效（店铺后台导出存在时滞，预填 0 值行不算，
    防止"0 元假日报"）；无有效日返回 ``None``。
    """
    day_totals = {}
    for facts in month_facts.values():
        for day, amount in facts.items():
            if day < business_date:
                day_totals[day] = day_totals.get(day, 0) + amount
    filled_days = sorted(d for d, total in day_totals.items() if total != 0)
    return filled_days[-1] if filled_days else None


def collect_channel_rows(*, month_facts, business_date, workdays, monthly_targets):
    """渠道日销快报的结构化行（markdown / HTML 两渲染器共用，同口径）。

    返回 ``(total, rows)``：*total* 为全渠道当日销售额；*rows* 每行
    ``{channel, sales, mom, mom_txt, target_txt, rate_txt}``，mom 为
    数值（无环比基数时 None），文本列已按旧播报格式渲染。
    """
    today = {c: facts.get(business_date) for c, facts in month_facts.items()}
    today = {c: v for c, v in today.items() if v is not None}
    total = sum(today.values())
    ordered = [c for c in CHANNEL_ORDER if c in today]
    ordered += sorted(c for c in today if c not in CHANNEL_ORDER)

    rows = []
    for channel in ordered:
        sales = today[channel]
        prev = _prev_workday_amount(month_facts.get(channel, {}), business_date, workdays)
        if prev:
            mom = (sales - prev) / abs(prev)
            mom_txt = f"{'+' if mom >= 0 else ''}{_fmt_pct(mom)}"
        else:
            mom = None
            mom_txt = "--"
        target = (monthly_targets or {}).get(channel)
        if target:
            # 月累计按自然日（≤ 业务日，含周末）——对齐 AI 表仪表盘口径
            # （2026-09-23：原按已过工作日累计，比仪表盘系统性偏低）；
            # 环比仍按上一工作日，不受此影响。
            mtd = sum(
                v for d, v in month_facts.get(channel, {}).items()
                if d <= business_date
            )
            target_txt = _fmt_wan(target)
            rate_txt = _fmt_pct(mtd / target)
        else:
            target_txt = rate_txt = "--"
        rows.append({
            "channel": channel,
            "sales": sales,
            "mom": mom,
            "mom_txt": mom_txt,
            "target_txt": target_txt,
            "rate_txt": rate_txt,
        })
    return total, rows


def build_channel_section(*, month_facts, business_date, workdays, monthly_targets):
    """渠道日销快报 markdown（旧播报表格复刻）。

    *month_facts* 见 :func:`fetch_channel_month_facts`；*monthly_targets*
    为渠道键月目标（可空 → 目标/达成率列 ``--``）。
    """
    total, rows = collect_channel_rows(
        month_facts=month_facts,
        business_date=business_date,
        workdays=workdays,
        monthly_targets=monthly_targets,
    )
    lines = [
        f"【渠道日销】全渠道{business_date.month}月{business_date.day}日销售额：**{_fmt_wan(total)} 元**",
        "",
        "| 渠道 | 销售额 | 环比 | 目标 | 达成率 |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['channel']} | {_fmt_wan(row['sales'])} | {row['mom_txt']}"
            f" | {row['target_txt']} | {row['rate_txt']} |"
        )
    return "\n".join(lines)
