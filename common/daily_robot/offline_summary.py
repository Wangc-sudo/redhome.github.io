# -*- coding: utf-8 -*-
"""线下整体销售汇总（offline_all 汇总群）——日 / 周 / 月报。

数据源：``mart_ops.fact_daily_report_offline``——杭州/绍兴为机器人报数落库，
省外/线下总经办为 AI 表 melt 同步（region='offline_extra'，按
``responsible_person`` 区分板块）。

口径铁律（方案 §5）：
- 月目标 MAX 不 SUM（melt 行重复携带，同人同板块多行只取 MAX）；
- 达成率一律走 ``common.metrics.daily_report.achievement_rate``；
- 日环比 = 当日 ÷ 前一自然日；周环比 = 当日 ÷ 上周同星期几；
  月环比 = 本月 1 日至当日累计 ÷ 上月 1 日至同日日累计；
- 未报 = 0（与 vanke 回填口径一致）。

表格样式对齐渠道日报（``channel_daily`` 的 ``_fmt_wan`` / ``_fmt_pct``）。
"""

import contextlib
from dataclasses import dataclass
from datetime import date, timedelta

from common.calendar_utils import month_days
from common.daily_robot.channel_daily import _fmt_pct, _fmt_wan
from common.metrics.daily_report import achievement_rate

#: 汇总板块：(scope 键, 展示名, fact region 键, 锚点)。
#: 锚点：None=整区域；("person", 名)=按 responsible_person 过滤；
#: ("dept", 部门)=按 department 过滤；("dept_not", 部门)=排除该部门。
#: 新增/拆分板块只改这里（2026-09-23 李树军拆分：绍兴剔除线下运营中心，
#: 李树军板块=线下运营中心——绍兴播报中他本就独立计算）。
AGG_SCOPES = (
    ("hangzhou", "杭州", "hangzhou", None),
    ("shaoxing", "绍兴", "shaoxing", ("dept_not", "线下运营中心")),
    ("shengwai", "省外", "offline_extra", ("person", "省外")),
    ("zongjingban", "线下总经办", "offline_extra", ("person", "线下总经办")),
    ("lishujun", "李树军", "shaoxing", ("dept", "线下运营中心")),
)

TOTAL_SCOPE_KEY = "offline_total"
TOTAL_LABEL = "线下整体"

DAILY_KIND = "offline_daily"
WEEKLY_KIND = "offline_weekly"
MONTHLY_KIND = "offline_monthly"

DAILY_TITLE = "线下整体日报"
WEEKLY_TITLE = "线下整体周报"
MONTHLY_TITLE = "线下整体月报"

_WEEKDAYS = "一二三四五六日"


@dataclass(frozen=True)
class ScopeMetrics:
    """单板块一期（日/周/月）指标；不适用字段为 None。"""

    scope: str
    label: str
    sales: float
    dod_amount: float | None = None
    dod_rate: float | None = None
    wow_amount: float | None = None
    wow_rate: float | None = None
    mom_amount: float | None = None
    mom_rate: float | None = None
    month_completed: float = 0.0
    month_target: float | None = None
    month_rate: float | None = None


# ---------------------------------------------------------------------------
# DB 取数（只读 mart_ops；口径判断全在纯函数里）
# ---------------------------------------------------------------------------

def _anchor_clause(anchor):
    """锚点 → (SQL 片段, 参数)。None 整区域；person/dept/dept_not 见 AGG_SCOPES。"""
    if anchor is None:
        return "", []
    kind, value = anchor
    if kind == "person":
        return " AND `responsible_person` = %s", [value]
    if kind == "dept":
        return " AND `department` = %s", [value]
    if kind == "dept_not":
        return " AND (`department` IS NULL OR `department` <> %s)", [value]
    raise ValueError(f"unknown scope anchor: {anchor!r}")


def fetch_scope_daily_facts(connection, *, region, anchor, start, end):
    """``{date: 销售额}``；anchor=None 整区域，否则按锚点过滤。

    名称含「合计」的行一律排除（与 ``mart_leaderboard`` 同口径）——AI 表
    自带 杭州合计/余杭合计 等合计行，不排则区域汇总双倍计数。
    """
    clause, clause_params = _anchor_clause(anchor)
    sql = (
        "SELECT `business_date` AS `d`, SUM(`sales_amount`) AS `s` "
        "FROM `fact_daily_report_offline` "
        "WHERE `region` = %s AND `sales_amount` IS NOT NULL "
        # %% 转义：pymysql 按 % 格式化 SQL，字面量 %合计% 须双写
        "AND `responsible_person` NOT LIKE '%%合计%%' "
        "AND `business_date` BETWEEN %s AND %s"
        f"{clause}"
        " GROUP BY `business_date`"
    )
    params = [region, start, end, *clause_params]
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    return {row["d"]: float(row["s"] or 0) for row in rows}


def fetch_scope_month_target(connection, *, region, anchor, year, month):
    """该板块当月目标；无 → None。

    口径：先按人取 MAX（melt 行重复携带，绝不可 SUM），再跨人求和
    （板块目标 = 成员目标之和）；「合计」行排除（其目标值是行的冗余
    汇总，混入即虚增）。
    """
    first, last = month_days(year, month)[0], month_days(year, month)[-1]
    clause, clause_params = _anchor_clause(anchor)
    sql = (
        "SELECT `responsible_person` AS `p`, MAX(`monthly_target`) AS `t` "
        "FROM `fact_daily_report_offline` "
        "WHERE `region` = %s AND `monthly_target` IS NOT NULL "
        "AND `responsible_person` NOT LIKE '%%合计%%' "
        "AND `business_date` BETWEEN %s AND %s"
        f"{clause}"
        " GROUP BY `responsible_person`"
    )
    params = [region, first, last, *clause_params]
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    values = [float(row["t"]) for row in rows if row["t"] is not None]
    return sum(values) if values else None


def fetch_member_dept_map(connection, regions):
    """通讯录实名 → 部门名（``dim_robot_member``，人员部门归属的权威源）。

    运维口径（2026-09-23）：对部门把握不准时以通讯录为准——AI 表内
    部门列是手工维护的表内叫法，dim 才是组织真源。返回
    ``{(region, 姓名): 部门名}``（dept_name 为空的行不收录）。
    """
    placeholders = ",".join(["%s"] * len(regions))
    sql = (
        "SELECT `region`, `name`, `dept_name` FROM `dim_robot_member` "
        f"WHERE `region` IN ({placeholders}) AND `is_active` = 1"
    )
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, list(regions))
        rows = cursor.fetchall()
    return {
        (row["region"], row["name"]): row["dept_name"]
        for row in rows
        if row.get("dept_name")
    }


def upsert_agg_daily(connection, *, stat_date, rows, synced_at):
    """写/覆盖 ``agg_offline_daily``（(stat_date, scope) 幂等）。"""
    sql = (
        "INSERT INTO `agg_offline_daily` (`stat_date`, `scope`, `sales_amount`, "
        "`dod_amount`, `dod_rate`, `wow_amount`, `wow_rate`, `mom_amount`, "
        "`mom_rate`, `month_completed`, `month_target`, `month_rate`, "
        "`synced_at`) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE "
        "`sales_amount`=VALUES(`sales_amount`), `dod_amount`=VALUES(`dod_amount`), "
        "`dod_rate`=VALUES(`dod_rate`), `wow_amount`=VALUES(`wow_amount`), "
        "`wow_rate`=VALUES(`wow_rate`), `mom_amount`=VALUES(`mom_amount`), "
        "`mom_rate`=VALUES(`mom_rate`), "
        "`month_completed`=VALUES(`month_completed`), "
        "`month_target`=VALUES(`month_target`), `month_rate`=VALUES(`month_rate`), "
        "`synced_at`=VALUES(`synced_at`)"
    )
    with contextlib.closing(connection.cursor()) as cursor:
        for m in rows:
            cursor.execute(sql, (
                stat_date, m.scope, m.sales,
                m.dod_amount, m.dod_rate, m.wow_amount, m.wow_rate,
                m.mom_amount, m.mom_rate,
                m.month_completed, m.month_target, m.month_rate, synced_at,
            ))


# ---------------------------------------------------------------------------
# 纯口径（无 IO，全部可单测）
# ---------------------------------------------------------------------------

def _diff_rate(current, base):
    """(current-base, (current-base)/abs(base))；base 缺失或 0 → (None, None)。"""
    if not base:
        return None, None
    diff = current - base
    return diff, diff / abs(base)


def previous_week(reference):
    """*reference* 所在周的上一周（周一, 周日）。"""
    monday = reference - timedelta(days=reference.weekday() + 7)
    return monday, monday + timedelta(days=6)


def previous_month(reference):
    """*reference* 所在月的上一月（1 日, 末日）。"""
    last = reference.replace(day=1) - timedelta(days=1)
    return last.replace(day=1), last


def _sum_window(facts, start, end):
    return sum(amount for d, amount in facts.items() if start <= d <= end)


def compute_daily_metrics(scope, label, *, facts, business_date, month_target):
    """日报指标：当日 + 日/周环比 + 月累计/达成 + 月环比。

    *facts* 须覆盖上月 1 日至 *business_date*（缺日 = 0）。
    """
    today = business_date
    sales = facts.get(today, 0.0)
    dod_amount, dod_rate = _diff_rate(sales, facts.get(today - timedelta(days=1)))
    wow_amount, wow_rate = _diff_rate(sales, facts.get(today - timedelta(days=7)))

    month_first = today.replace(day=1)
    month_completed = _sum_window(facts, month_first, today)
    prev_first, prev_last = previous_month(today)
    prev_same_day = min(today.day, prev_last.day)
    prev_completed = _sum_window(
        facts, prev_first, prev_first.replace(day=prev_same_day)
    )
    mom_amount, mom_rate = _diff_rate(month_completed, prev_completed)

    return ScopeMetrics(
        scope=scope,
        label=label,
        sales=sales,
        dod_amount=dod_amount,
        dod_rate=dod_rate,
        wow_amount=wow_amount,
        wow_rate=wow_rate,
        mom_amount=mom_amount,
        mom_rate=mom_rate,
        month_completed=month_completed,
        month_target=month_target,
        month_rate=achievement_rate(month_completed, month_target),
    )


def compute_weekly_metrics(scope, label, *, facts, week_start, week_end):
    """周报指标：周合计 + 周环比（对前一周同区间）。"""
    week_total = _sum_window(facts, week_start, week_end)
    prev_start = week_start - timedelta(days=7)
    prev_end = week_end - timedelta(days=7)
    prev_total = _sum_window(facts, prev_start, prev_end)
    wow_amount, wow_rate = _diff_rate(week_total, prev_total)
    return ScopeMetrics(
        scope=scope, label=label, sales=week_total,
        wow_amount=wow_amount, wow_rate=wow_rate,
    )


def compute_monthly_metrics(scope, label, *, facts, month_first, month_last, month_target):
    """月报指标：月合计 + 月环比（对上月）+ 达成率。"""
    month_total = _sum_window(facts, month_first, month_last)
    prev_first, prev_last = previous_month(month_first)
    prev_total = _sum_window(facts, prev_first, prev_last)
    mom_amount, mom_rate = _diff_rate(month_total, prev_total)
    return ScopeMetrics(
        scope=scope,
        label=label,
        sales=month_total,
        mom_amount=mom_amount,
        mom_rate=mom_rate,
        month_completed=month_total,
        month_target=month_target,
        month_rate=achievement_rate(month_total, month_target),
    )


def merge_facts(facts_list):
    """多板块 facts 逐日求和（线下整体口径）。"""
    merged = {}
    for facts in facts_list:
        for d, amount in facts.items():
            merged[d] = merged.get(d, 0.0) + amount
    return merged


def merge_targets(targets):
    """整体月目标 = 各板块目标之和；全缺 → None。"""
    values = [t for t in targets if t is not None]
    return sum(values) if values else None


# ---------------------------------------------------------------------------
# Markdown 渲染（纯函数）
# ---------------------------------------------------------------------------

def _signed_pct(rate):
    if rate is None:
        return "--"
    return f"{'+' if rate >= 0 else ''}{_fmt_pct(rate)}"


def build_daily_markdown(*, business_date, rows, total):
    d = business_date
    lines = [
        f"【线下整体日报】{d.month}月{d.day}日（周{_WEEKDAYS[d.weekday()]}）",
        f"今日合计：**{_fmt_wan(total.sales)} 元** · "
        f"日环比 {_signed_pct(total.dod_rate)} · 周环比 {_signed_pct(total.wow_rate)}",
        "",
        "| 板块 | 今日 | 日环比 | 周环比 | 月累计 | 月目标 | 达成率 |",
        "|---|---|---|---|---|---|---|",
    ]
    for m in (*rows, total):
        label = f"**{m.label}**" if m.scope == TOTAL_SCOPE_KEY else m.label
        target_txt = _fmt_wan(m.month_target) if m.month_target else "--"
        rate_txt = _fmt_pct(m.month_rate) if m.month_rate is not None else "--"
        lines.append(
            f"| {label} | {_fmt_wan(m.sales)} | {_signed_pct(m.dod_rate)} "
            f"| {_signed_pct(m.wow_rate)} | {_fmt_wan(m.month_completed)} "
            f"| {target_txt} | {rate_txt} |"
        )
    return "\n".join(lines)


def build_weekly_markdown(*, week_start, week_end, rows, total):
    def _label(d):
        return f"{d.month}月{d.day}日（周{_WEEKDAYS[d.weekday()]}）"

    ranked = sorted(rows, key=lambda m: -m.sales)
    lines = [
        f"【线下整体周报】{_label(week_start)} ~ {_label(week_end)}",
        f"全周合计：**{_fmt_wan(total.sales)} 元** · 周环比 {_signed_pct(total.wow_rate)}",
        "",
        "| 排名 | 板块 | 周合计 | 周环比 |",
        "|---|---|---|---|",
    ]
    for rank, m in enumerate(ranked, 1):
        lines.append(
            f"| {rank} | {m.label} | {_fmt_wan(m.sales)} | {_signed_pct(m.wow_rate)} |"
        )
    lines.append(
        f"|  | **{total.label}** | **{_fmt_wan(total.sales)}** "
        f"| {_signed_pct(total.wow_rate)} |"
    )
    return "\n".join(lines)


def build_monthly_markdown(*, month_first, rows, total):
    ranked = sorted(
        rows,
        key=lambda m: (
            -(m.month_rate if m.month_rate is not None else -1),
            -m.sales,
        ),
    )
    lines = [
        f"【线下整体月报】{month_first.year}年{month_first.month}月",
        f"全月合计：**{_fmt_wan(total.sales)} 元** · 月环比 {_signed_pct(total.mom_rate)}",
        "",
        "| 排名 | 板块 | 月合计 | 月目标 | 达成率 | 月环比 |",
        "|---|---|---|---|---|---|",
    ]
    for rank, m in enumerate(ranked, 1):
        target_txt = _fmt_wan(m.month_target) if m.month_target else "--"
        rate_txt = _fmt_pct(m.month_rate) if m.month_rate is not None else "--"
        lines.append(
            f"| {rank} | {m.label} | {_fmt_wan(m.sales)} | {target_txt} "
            f"| {rate_txt} | {_signed_pct(m.mom_rate)} |"
        )
    lines.append(
        f"|  | **{total.label}** | **{_fmt_wan(total.sales)}** "
        f"| {_fmt_wan(total.month_target) if total.month_target else '--'} "
        f"| {_fmt_pct(total.month_rate) if total.month_rate is not None else '--'} "
        f"| {_signed_pct(total.mom_rate)} |"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 任务（connection + outbox 注入，幂等）
# ---------------------------------------------------------------------------

def _fetch_window(connection, *, region, anchor, start, end):
    return fetch_scope_daily_facts(
        connection, region=region, anchor=anchor, start=start, end=end
    )


def run_daily_summary(connection, outbox, *, business_date, now):
    """每日 20:30：板块当日 + 环比 + 月累计 → agg 落表 + outbox。

    返回 ``"enqueued" | "already_sent"``。
    """
    prev_first, _ = previous_month(business_date)
    facts_by_scope = {}
    targets = {}
    for scope, label, region, anchor in AGG_SCOPES:
        facts = _fetch_window(
            connection, region=region, anchor=anchor,
            start=prev_first, end=business_date,
        )
        facts_by_scope[scope] = facts
        targets[scope] = fetch_scope_month_target(
            connection, region=region, anchor=anchor,
            year=business_date.year, month=business_date.month,
        )

    rows = [
        compute_daily_metrics(
            scope, label,
            facts=facts_by_scope[scope],
            business_date=business_date,
            month_target=targets[scope],
        )
        for scope, label, _, _ in AGG_SCOPES
    ]
    total = compute_daily_metrics(
        TOTAL_SCOPE_KEY, TOTAL_LABEL,
        facts=merge_facts([facts_by_scope[s] for s, _, _, _ in AGG_SCOPES]),
        business_date=business_date,
        month_target=merge_targets(targets.values()),
    )

    upsert_agg_daily(
        connection, stat_date=business_date, rows=[*rows, total], synced_at=now,
    )
    body = build_daily_markdown(business_date=business_date, rows=rows, total=total)
    enqueued = outbox.enqueue(
        region="offline_all",
        kind=DAILY_KIND,
        business_date=business_date,
        title=DAILY_TITLE,
        body_md=body,
        created_at=now,
    )
    return "enqueued" if enqueued else "already_sent"


def run_weekly_summary(connection, outbox, *, reference, now):
    """每周一 09:30：上周周报（周合计/周环比/板块排名）→ outbox。"""
    week_start, week_end = previous_week(reference)
    fetch_start = week_start - timedelta(days=7)
    facts_by_scope = {}
    for scope, label, region, anchor in AGG_SCOPES:
        facts_by_scope[scope] = _fetch_window(
            connection, region=region, anchor=anchor,
            start=fetch_start, end=week_end,
        )

    rows = [
        compute_weekly_metrics(
            scope, label, facts=facts_by_scope[scope],
            week_start=week_start, week_end=week_end,
        )
        for scope, label, _, _ in AGG_SCOPES
    ]
    total = compute_weekly_metrics(
        TOTAL_SCOPE_KEY, TOTAL_LABEL,
        facts=merge_facts([facts_by_scope[s] for s, _, _, _ in AGG_SCOPES]),
        week_start=week_start, week_end=week_end,
    )
    body = build_weekly_markdown(
        week_start=week_start, week_end=week_end, rows=rows, total=total,
    )
    enqueued = outbox.enqueue(
        region="offline_all",
        kind=WEEKLY_KIND,
        business_date=week_start,
        title=WEEKLY_TITLE,
        body_md=body,
        created_at=now,
    )
    return "enqueued" if enqueued else "already_sent"


def run_monthly_summary(connection, outbox, *, reference, now):
    """每月 1 日 10:00：上月月报（月合计/月环比/达成率榜）→ outbox。"""
    month_first, month_last = previous_month(reference)
    prev_first, _ = previous_month(month_first)
    facts_by_scope = {}
    targets = {}
    for scope, label, region, anchor in AGG_SCOPES:
        facts_by_scope[scope] = _fetch_window(
            connection, region=region, anchor=anchor,
            start=prev_first, end=month_last,
        )
        targets[scope] = fetch_scope_month_target(
            connection, region=region, anchor=anchor,
            year=month_first.year, month=month_first.month,
        )

    rows = [
        compute_monthly_metrics(
            scope, label,
            facts=facts_by_scope[scope],
            month_first=month_first, month_last=month_last,
            month_target=targets[scope],
        )
        for scope, label, _, _ in AGG_SCOPES
    ]
    total = compute_monthly_metrics(
        TOTAL_SCOPE_KEY, TOTAL_LABEL,
        facts=merge_facts([facts_by_scope[s] for s, _, _, _ in AGG_SCOPES]),
        month_first=month_first, month_last=month_last,
        month_target=merge_targets(targets.values()),
    )
    body = build_monthly_markdown(month_first=month_first, rows=rows, total=total)
    enqueued = outbox.enqueue(
        region="offline_all",
        kind=MONTHLY_KIND,
        business_date=month_first,
        title=MONTHLY_TITLE,
        body_md=body,
        created_at=now,
    )
    return "enqueued" if enqueued else "already_sent"


# ---------------------------------------------------------------------------
# 榜单页（pages-offline_all）：人员总榜 + 日/周/月三维度板块
# ---------------------------------------------------------------------------

#: 人员总榜覆盖的 fact region（每个 region 的全部人员，无人例外；
#: 「合计」行由 mart_collect 统一跳过；新增线下区域在此登记）。
OFFLINE_PEOPLE_REGIONS = ("hangzhou", "shaoxing", "offline_extra")

import html as _html_mod


def _panel(title, body, note=None):
    """与 qudao_panels 同款的 ``<div class="panel">``（页面 CSS 类约定）。"""
    note_html = (
        f'<div class="small muted" style="margin-top:8px">{note}</div>' if note else ""
    )
    return f'<div class="panel">\n  <h2>{title}</h2>\n  {body}\n{note_html}\n</div>'


def _esc(value):
    return _html_mod.escape(str(value if value is not None else ""))


def _rate_cls(rate):
    if rate is None:
        return "muted"
    return "g" if rate >= 0 else "r"


def _scope_trs(rows, total, cells):
    """板块表 <tr> 序列；*cells* 为列函数 fn(m)->[(html, cls), ...]。"""
    trs = []
    for m in (*rows, total):
        label = f"<b>{_esc(m.label)}</b>" if m.scope == TOTAL_SCOPE_KEY else _esc(m.label)
        tds = "".join(
            f'<td class="num {cls}">{content}</td>' for content, cls in cells(m)
        )
        trs.append(f'<tr><td class="strong">{label}</td>{tds}</tr>')
    return "".join(trs)


def _signed_pct_html(rate):
    if rate is None:
        return "--", "muted"
    return f"{'+' if rate >= 0 else ''}{_fmt_pct(rate)}", _rate_cls(rate)


def build_daily_panel(*, rows, total, report_day):
    """日维度表体：各板块当日 + 日环比 + 周环比（报告日=最近有数据自然日）。"""
    head = (
        f'<div class="small muted" style="margin-bottom:8px">'
        f'{report_day.month}月{report_day.day}日合计 '
        f'<b style="color:#1f2329">{_fmt_wan(total.sales)} 元</b></div>'
    )

    def cells(m):
        dod, dod_cls = _signed_pct_html(m.dod_rate)
        wow, wow_cls = _signed_pct_html(m.wow_rate)
        return [(_fmt_wan(m.sales), ""), (_esc(dod), dod_cls), (_esc(wow), wow_cls)]

    return (
        head
        + '<table><thead><tr><th>板块</th><th>当日</th><th>日环比</th>'
          '<th>周环比</th></tr></thead>'
        + f'<tbody>{_scope_trs(rows, total, cells)}</tbody></table>'
        + f'<div class="small muted" style="margin-top:8px">'
          f'数据截至 {report_day.month}月{report_day.day}日（最近有数据自然日）</div>'
    )


def build_weekly_panel(*, rows, total, week_start, business_date):
    """周维度表体：本周（周一至昨日，T-1）累计 + 对上周同期环比。"""
    head = (
        f'<div class="small muted" style="margin-bottom:8px">'
        f'本周 {week_start.month}月{week_start.day}日 至 '
        f'{business_date.month}月{business_date.day}日 · 合计 '
        f'<b style="color:#1f2329">{_fmt_wan(total.sales)} 元</b></div>'
    )

    def cells(m):
        wow, wow_cls = _signed_pct_html(m.wow_rate)
        return [(_fmt_wan(m.sales), ""), (_esc(wow), wow_cls)]

    return (
        head
        + '<table><thead><tr><th>板块</th><th>本周累计</th>'
          '<th>环比上周同期</th></tr></thead>'
        + f'<tbody>{_scope_trs(rows, total, cells)}</tbody></table>'
        + '<div class="small muted" style="margin-top:8px">'
          '本周=周一至昨日（T-1，自然日口径）</div>'
    )


def build_monthly_panel(*, rows, total, business_date):
    """月维度表体：月累计 / 月目标 / 达成率 / 月环比。"""
    head = (
        f'<div class="small muted" style="margin-bottom:8px">'
        f'{business_date.month}月累计合计 '
        f'<b style="color:#1f2329">{_fmt_wan(total.month_completed)} 元</b>'
        + (
            f' · 达成率 <b style="color:#1f2329">{_fmt_pct(total.month_rate)}</b>'
            if total.month_rate is not None else ""
        )
        + "</div>"
    )

    def cells(m):
        mom, mom_cls = _signed_pct_html(m.mom_rate)
        rate_txt = _fmt_pct(m.month_rate) if m.month_rate is not None else "--"
        return [
            (_fmt_wan(m.month_completed), ""),
            (_fmt_wan(m.month_target) if m.month_target else "--", "muted"),
            (rate_txt, ""),
            (_esc(mom), mom_cls),
        ]

    return (
        head
        + '<table><thead><tr><th>板块</th><th>月累计</th><th>月目标</th>'
          '<th>达成率</th><th>月环比</th></tr></thead>'
        + f'<tbody>{_scope_trs(rows, total, cells)}</tbody></table>'
        + '<div class="small muted" style="margin-top:8px">'
          '月环比=本月1日至当日累计 ÷ 上月1日至同日日累计</div>'
    )


#: 维度标签（键, 展示名），顺序即标签顺序。
_DIM_TABS = (("daily", "📅 日维度"), ("weekly", "📆 周维度"), ("monthly", "🗓 月维度"))

_DIM_TABS_SCRIPT = """<style>#dim-tabs .tab{text-decoration:none;color:inherit}</style>
<script>
function swDim(el){
  document.querySelectorAll('#dim-tabs .tab').forEach(function(t){t.classList.remove('on')});
  el.classList.add('on');
  ['daily','weekly','monthly'].forEach(function(k){
    document.getElementById('dim-'+k).style.display = el.dataset.dim===k?'':'none';
  });
  if(history.replaceState){history.replaceState(null,'',el.getAttribute('href'));}
}
(function(){
  if(location.hash){
    var el=document.querySelector('#dim-tabs .tab[href="'+location.hash+'"]');
    if(el){swDim(el);}
  }
})();
</script>"""


def _dim_tabs_panel(bodies):
    """日/月/周标签面板：tab 切换 + 页内锚点（``#dim-daily`` 等可深链接）。

    锚点语义：JS 正常时点击 tab 就地切换并把 hash 写入地址栏（可收藏/
    转发定位到指定维度）；无 JS 时 ``<a href="#dim-xxx">`` 退化为普通
    页内锚点跳转（三段落全部纵向可见，不丢内容）。
    """
    tabs = []
    sections = []
    for i, (key, label) in enumerate(_DIM_TABS):
        on = " on" if i == 0 else ""
        tabs.append(
            f'<a class="tab{on}" data-dim="{key}" href="#dim-{key}" '
            f'onclick="swDim(this);return false;">{label}</a>'
        )
        display = "" if i == 0 else ' style="display:none"'
        sections.append(f'<div id="dim-{key}"{display}>{bodies[key]}</div>')
    return (
        '<div class="panel">\n'
        f'  <div class="tabs" id="dim-tabs">{"".join(tabs)}</div>\n'
        + "\n".join(sections)
        + f"\n{_DIM_TABS_SCRIPT}\n</div>"
    )


def build_offline_panels(connection, *, business_date):
    """日/周/月三维度板块（顺序即页面顺序），**统一 T-1**。

    运维裁决（2026-09-23）：今天的看板看昨天的数据——三板块全部锚定
    ``business_date - 1``（取数窗口、月目标月份、周/月区间、日维度
    报告日上限同移），当日填报进度不进看板。单板块异常 → 占位降级
    （同 qudao_panels 纪律），绝不拖垮整页。
    """
    import logging

    logger = logging.getLogger(__name__)
    data_date = business_date - timedelta(days=1)
    prev_first, _ = previous_month(data_date)
    facts_by_scope = {}
    targets = {}
    for scope, label, region, anchor in AGG_SCOPES:
        facts = fetch_scope_daily_facts(
            connection, region=region, anchor=anchor,
            start=prev_first, end=data_date,
        )
        facts_by_scope[scope] = facts
        targets[scope] = fetch_scope_month_target(
            connection, region=region, anchor=anchor,
            year=data_date.year, month=data_date.month,
        )
    merged = merge_facts([facts_by_scope[s] for s, _, _, _ in AGG_SCOPES])
    total_target = merge_targets(targets.values())

    def daily():
        # 报告日 = data_date 之前（含）最近一个全板块合计非零的自然日
        day_totals = {}
        for facts in facts_by_scope.values():
            for d, amount in facts.items():
                if d <= data_date:
                    day_totals[d] = day_totals.get(d, 0.0) + amount
        filled = sorted(d for d, t in day_totals.items() if t != 0)
        if not filled:
            return '<div class="muted small">本月暂无报数数据</div>'
        report_day = filled[-1]
        rows = [
            compute_daily_metrics(
                scope, label, facts=facts_by_scope[scope],
                business_date=report_day, month_target=targets[scope],
            )
            for scope, label, _, _ in AGG_SCOPES
        ]
        total = compute_daily_metrics(
            TOTAL_SCOPE_KEY, TOTAL_LABEL, facts=merged,
            business_date=report_day, month_target=total_target,
        )
        return build_daily_panel(rows=rows, total=total, report_day=report_day)

    def weekly():
        week_start = data_date - timedelta(days=data_date.weekday())
        rows = []
        for scope, label, _, _ in AGG_SCOPES:
            facts = facts_by_scope[scope]
            week_total = _sum_window(facts, week_start, data_date)
            prev_total = _sum_window(
                facts, week_start - timedelta(days=7),
                data_date - timedelta(days=7),
            )
            wow_amount, wow_rate = _diff_rate(week_total, prev_total)
            rows.append(ScopeMetrics(
                scope=scope, label=label, sales=week_total,
                wow_amount=wow_amount, wow_rate=wow_rate,
            ))
        week_total = _sum_window(merged, week_start, data_date)
        prev_total = _sum_window(
            merged, week_start - timedelta(days=7),
            data_date - timedelta(days=7),
        )
        wow_amount, wow_rate = _diff_rate(week_total, prev_total)
        total = ScopeMetrics(
            scope=TOTAL_SCOPE_KEY, label=TOTAL_LABEL, sales=week_total,
            wow_amount=wow_amount, wow_rate=wow_rate,
        )
        return build_weekly_panel(
            rows=rows, total=total,
            week_start=week_start, business_date=data_date,
        )

    def monthly():
        rows = [
            compute_daily_metrics(
                scope, label, facts=facts_by_scope[scope],
                business_date=data_date, month_target=targets[scope],
            )
            for scope, label, _, _ in AGG_SCOPES
        ]
        total = compute_daily_metrics(
            TOTAL_SCOPE_KEY, TOTAL_LABEL, facts=merged,
            business_date=data_date, month_target=total_target,
        )
        return build_monthly_panel(rows=rows, total=total, business_date=data_date)

    bodies = {}
    for key, build in (("daily", daily), ("weekly", weekly), ("monthly", monthly)):
        try:
            bodies[key] = build()
        except Exception:
            logger.warning("offline_all 板块 %s 生成失败，降级为占位", key, exc_info=True)
            bodies[key] = '<div class="muted small">数据暂缺</div>'
    return [_dim_tabs_panel(bodies)]


def build_offline_all_html(connection, cfg, *, business_date, now):
    """线下整体榜单页：人员总榜（杭/绍等全部线下人员，无人例外）+ 维度标签。

    人员：``OFFLINE_PEOPLE_REGIONS`` 各 region 的 ``mart_collect`` 结果合并
    （与各区域榜单页逐行同口径；「合计」行由 mart_collect 统一跳过），
    排序与 ``mart_collect`` 同键（-rate(None→-1), -completed, -target）。
    人/部门归位（运维口径 2026-09-23，人与部门不要混乱）：
    * offline_extra 行「姓名↔部门」互换——事实行的 responsible_person 是
      板块（省外/线下总经办）、department 才是责任人（余云涛/谢坚钰）；
    * 杭/绍人员部门以通讯录 ``dim_robot_member`` 为准（表内部门是手工
      叫法），无匹配保留表内值兜底。
    板块：日/周/月标签面板（``build_offline_panels``，extra_panels 插入）。
    """
    from common.daily_robot.leaderboard import build_html
    from common.daily_robot.mart_leaderboard import (
        build_leaderboard_view,
        mart_collect,
    )
    from common.metrics.daily_report import elapsed_workdays

    year, month = business_date.year, business_date.month
    people = []
    by_region = {}
    workdays = frozenset()
    for region in OFFLINE_PEOPLE_REGIONS:
        data = mart_collect(connection, region=region, business_date=business_date)
        rows = [dict(p) for p in data.people]
        if region == "offline_extra":
            for p in rows:
                p["name"], p["dept"] = p["dept"], p["name"]
        by_region[region] = rows
        people.extend(rows)
        workdays = data.workdays

    # 部门归属以通讯录为准（无匹配保留表内部门）
    dim_regions = tuple(r for r in OFFLINE_PEOPLE_REGIONS if r != "offline_extra")
    dept_map = fetch_member_dept_map(connection, dim_regions)
    for region in dim_regions:
        for p in by_region[region]:
            p["dept"] = dept_map.get((region, p["name"]), p["dept"])

    people.sort(
        key=lambda p: (
            -(p["rate"] if p["rate"] is not None else -1),
            -p["completed"],
            -p["target"],
        )
    )

    view = build_leaderboard_view(cfg, workdays, year=year, month=month)
    elapsed = sorted({d.day for d in elapsed_workdays(workdays, today=business_date)})
    panels = build_offline_panels(connection, business_date=business_date)
    return build_html(view, now, elapsed, people, extra_panels=panels)
