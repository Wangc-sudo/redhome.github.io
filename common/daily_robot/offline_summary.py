# -*- coding: utf-8 -*-
"""线下整体销售汇总（offline_all 汇总群）——日 / 周 / 月报。

数据源：``mart_ops.fact_daily_report_offline``——杭州/绍兴为机器人报数落库；
「其他」=补集桶（region ∉ 杭/绍/vanke：省外/线下总经办等自动落桶，
2026-10-16 运维裁决 线下总体=杭州+绍兴+其他，vanke 不算）。

口径铁律（方案 §5）：
- 月目标 MAX 不 SUM（melt 行重复携带，同人同板块多行只取 MAX）；
- 达成率一律走 ``common.metrics.daily_report.achievement_rate``；
- 日环比 = 当日 ÷ 前一自然日；周环比 = 当日 ÷ 上周同星期几；
  月环比 = 本月 1 日至当日累计 ÷ 上月 1 日至同日日累计；
  （页面总览表例外：周环比 = 本周累计 ÷ 上周同期累计，见
  ``compute_overview_row``——与群消息口径不同，勿混用）；
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
#: 板块锚点：None=整区域（或整补集）。
#: 2026-10-16 运维裁决：线下总体=杭州+绍兴+其他 三桶——「其他」=fact 里
#: region ∉ {hangzhou, shaoxing, vanke} 的补集（省外/线下总经办/未来新板块
#: 自动落桶，零维护）；原 offline_extra 板块锚点（省外/总经办）与李树军
#: 拆分（dept_not 线下运营中心）随之退役——李树军并入绍兴行。
#: vanke 不算线下总体（同日裁决，vanke 独立观察）。
OFFLINE_OTHER_REGION = "__offline_other__"

#: 补集排除的 region（核心桶 + vanke + qudao——2026-10-17 实锤：渠道线
#: 日报行（region=qudao，10 月 2194 万）也落在本表，不排则「其他」桶
#: 虚增渠道销售额；渠道线走渠道事实表/渠道看板，与线下三桶互斥）。
_OFFLINE_OTHER_EXCLUDES = ("hangzhou", "shaoxing", "vanke", "qudao")

AGG_SCOPES = (
    ("hangzhou", "杭州", "hangzhou", None),
    ("shaoxing", "绍兴", "shaoxing", None),
    ("other", "其他", OFFLINE_OTHER_REGION, None),
)

TOTAL_SCOPE_KEY = "offline_total"
TOTAL_LABEL = "线下整体"

#: 部门表只保留这些区域的部门（2026-10-19 用户裁决：部门只看杭州、绍兴）。
DEPT_TABLE_REGIONS = ("hangzhou", "shaoxing")
#: 部门表合计行标签——部门表不含「其他」区域，合计=杭州+绍兴（≠线下整体）。
DEPT_TABLE_TOTAL_LABEL = "杭州+绍兴"

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
    # 13 列扩展（2026-10-19 用户裁决）：昨日/本周/上周/上月累计/上月目标/上月达成率
    yesterday_sales: float = 0.0
    week_completed: float = 0.0
    prev_week_completed: float = 0.0
    prev_month_completed: float = 0.0
    prev_month_target: float | None = None
    prev_month_rate: float | None = None


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


def _region_clause(region):
    """region 过滤片段：普通区域=等值；``OFFLINE_OTHER_REGION``=补集排除。"""
    if region == OFFLINE_OTHER_REGION:
        placeholders = ", ".join(["%s"] * len(_OFFLINE_OTHER_EXCLUDES))
        return f"`region` NOT IN ({placeholders})", list(_OFFLINE_OTHER_EXCLUDES)
    return "`region` = %s", [region]


def fetch_scope_daily_facts(connection, *, region, anchor, start, end):
    """``{date: 销售额}``；anchor=None 整区域，否则按锚点过滤。

    名称含「合计」的行一律排除（与 ``mart_leaderboard`` 同口径）——AI 表
    自带 杭州合计/余杭合计 等合计行，不排则区域汇总双倍计数。
    """
    region_sql, region_params = _region_clause(region)
    clause, clause_params = _anchor_clause(anchor)
    sql = (
        "SELECT `business_date` AS `d`, SUM(`sales_amount`) AS `s` "
        "FROM `fact_daily_report_offline` "
        f"WHERE {region_sql} AND `sales_amount` IS NOT NULL "
        # %% 转义：pymysql 按 % 格式化 SQL，字面量 %合计% 须双写
        "AND `responsible_person` NOT LIKE '%%合计%%' "
        "AND `business_date` BETWEEN %s AND %s"
        f"{clause}"
        " GROUP BY `business_date`"
    )
    params = [*region_params, start, end, *clause_params]
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
    region_sql, region_params = _region_clause(region)
    clause, clause_params = _anchor_clause(anchor)
    sql = (
        "SELECT `responsible_person` AS `p`, MAX(`monthly_target`) AS `t` "
        "FROM `fact_daily_report_offline` "
        f"WHERE {region_sql} AND `monthly_target` IS NOT NULL "
        "AND `responsible_person` NOT LIKE '%%合计%%' "
        "AND `business_date` BETWEEN %s AND %s"
        f"{clause}"
        " GROUP BY `responsible_person`"
    )
    params = [*region_params, first, last, *clause_params]
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    values = [float(row["t"]) for row in rows if row["t"] is not None]
    return sum(values) if values else None


def fetch_member_dept_map(connection, regions):
    """通讯录实名 → 部门名，并入归位覆盖（``dim_org_assignment_override``）。

    运维口径（2026-09-23）：对部门把握不准时以通讯录为准——AI 表内
    部门列是手工维护的表内叫法，dim 才是组织真源。2026-10-19 用户裁决
    升级：画布拉线归口（归位覆盖层）对 BI 部门卡同步生效——画布与 BI
    同真源（merge_region_members，含同区改部门/跨区移出/移入补入）。
    覆盖读取失败 fail-soft 回退纯通讯录口径（merge 内部已处理）。
    返回 ``{(region, 姓名): 部门名}``（dept_name 为空的行不收录）。
    """
    from common.public_data import org_assignment_override

    placeholders = ",".join(["%s"] * len(regions))
    sql = (
        "SELECT `user_id`, `region`, `name`, `dept_id`, `dept_name` "
        "FROM `dim_robot_member` "
        f"WHERE `region` IN ({placeholders}) AND `is_active` = 1"
    )
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, list(regions))
        base = cursor.fetchall()
    by_region = {}
    for row in base:
        get = row.get if isinstance(row, dict) else None
        region = get("region") if get else row[1]
        by_region.setdefault(region, []).append(
            row if get else {
                "user_id": row[0], "region": row[1], "name": row[2],
                "dept_id": row[3], "dept_name": row[4],
            })
    result = {}
    for region in regions:
        merged = org_assignment_override.merge_region_members(
            connection, region, by_region.get(region, ()))
        for row in merged:
            name = row.get("name")
            dept = row.get("dept_name")
            if name and dept:
                result[(region, name)] = dept
    return result


def fetch_all_member_regions(connection):
    """通讯录里出现过的全部 region（供画布归位映射覆盖跨区移入）。"""
    sql = "SELECT DISTINCT `region` FROM `dim_robot_member` " \
          "WHERE `region` IS NOT NULL AND `region` <> ''"
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall()
    out = []
    for row in rows:
        get = row.get if isinstance(row, dict) else None
        value = get("region") if get else row[0]
        if value:
            out.append(str(value))
    return tuple(out)


UNMAPPED_DEPT = "(未归位)"


def _resolve_dept(person, fact_dept, dept_of, canvas_names):
    """部门归属：画布归位优先；回退的事实表部门名须属于画布部门集合。

    被 ``dept_of``（画布归位）覆盖的人直接采用其部门；未覆盖的人回退事实表
    ``department``，但**仅当该名字在画布部门集合 ``canvas_names`` 内**才认——
    门店由此归位（「诸暨门店」→诸暨项目部、「绍兴门店」→绍兴项目部）。
    历史脏值（人名当部门、旧简写，如「谢坚钰」「滨萧」）一律落
    ``UNMAPPED_DEPT``，由调用方决定是否展示。

    用户裁决（2026-10-19）：部门严格只认画布，画布上没有的一律不展示。
    """
    dept = dept_of.get(person)
    if dept:
        return dept
    if fact_dept and (canvas_names is None or fact_dept in canvas_names):
        return fact_dept
    return UNMAPPED_DEPT


def fetch_dept_daily_facts(connection, *, start, end, dept_of,
                           canvas_names=None):
    """线下整体口径按部门取日销：``{dept: {date: amount}}``。

    部门取**画布口径**（``dept_of``=``fetch_member_dept_map`` 的姓名→部门，
    即基成员 ∘ 归位覆盖后的结果，与闭环画布同源）；回退走 ``_resolve_dept``
    （严格只认画布，脏值落 ``UNMAPPED_DEPT``）。

    区域口径与 ``AGG_SCOPES`` 三桶一致：杭+绍+补集（排除 vanke/qudao）。
    """
    sql = (
        "SELECT `business_date` AS `d`, `responsible_person` AS `p`, "
        "`department` AS `fd`, `sales_amount` AS `s` "
        "FROM `fact_daily_report_offline` "
        "WHERE COALESCE(`region`, '') NOT IN ('vanke', 'qudao') "
        "AND `sales_amount` IS NOT NULL "
        "AND `responsible_person` NOT LIKE '%%合计%%' "
        "AND `business_date` BETWEEN %s AND %s"
    )
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, [start, end])
        rows = cursor.fetchall()
    result = {}
    for row in rows:
        get = row.get if isinstance(row, dict) else None
        person = get("p") if get else row[1]
        fact_dept = get("fd") if get else row[2]
        day = get("d") if get else row[0]
        amount = get("s") if get else row[3]
        dept = _resolve_dept(person, fact_dept, dept_of, canvas_names)
        bucket = result.setdefault(dept, {})
        bucket[day] = bucket.get(day, 0.0) + float(amount or 0)
    return result


def fetch_dept_month_targets(connection, *, year, month, dept_of,
                             canvas_names=None):
    """按部门汇总当月目标；口径同 ``fetch_scope_month_target``。

    先按人取 MAX（melt 行重复携带，绝不可 SUM），再按部门求和；「合计」行
    排除（其目标值是行的冗余汇总）。
    """
    first, last = month_days(year, month)[0], month_days(year, month)[-1]
    sql = (
        "SELECT `responsible_person` AS `p`, `department` AS `fd`, "
        "MAX(`monthly_target`) AS `t` "
        "FROM `fact_daily_report_offline` "
        "WHERE COALESCE(`region`, '') NOT IN ('vanke', 'qudao') "
        "AND `monthly_target` IS NOT NULL "
        "AND `responsible_person` NOT LIKE '%%合计%%' "
        "AND `business_date` BETWEEN %s AND %s "
        "GROUP BY `responsible_person`, `department`"
    )
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, [first, last])
        rows = cursor.fetchall()
    targets = {}
    for row in rows:
        get = row.get if isinstance(row, dict) else None
        person = get("p") if get else row[0]
        fact_dept = get("fd") if get else row[1]
        target = get("t") if get else row[2]
        if target is None:
            continue
        dept = _resolve_dept(person, fact_dept, dept_of, canvas_names)
        targets[dept] = targets.get(dept, 0.0) + float(target)
    return targets


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


def compute_daily_metrics(scope, label, *, facts, business_date, month_target,
                          prev_month_target=None):
    """日报指标（13 列）：今日/昨日/日环比、本周/上周/周环比、
    月累计/月目标/本月达成率、上月累计/上月目标/上月达成率。

    口径（2026-10-19 用户裁决）：本周=周一至今；上周=上周同期（上周一至
    上周同星期几），周环比按「本周 vs 上周同期」的周口径；上月累计=上月
    同期（上月 1 日至上月同日），上月达成率=上月累计/上月目标。

    *facts* 须覆盖上月 1 日至 *business_date*（缺日 = 0）。
    """
    today = business_date
    sales = facts.get(today, 0.0)
    yesterday_sales = facts.get(today - timedelta(days=1), 0.0)
    dod_amount, dod_rate = _diff_rate(
        sales, facts.get(today - timedelta(days=1)))

    # 本周（周一至今）/ 上周同期（上周一至上周同星期几）；周环比按周口径
    week_start = today - timedelta(days=today.weekday())
    week_completed = _sum_window(facts, week_start, today)
    prev_week_start = week_start - timedelta(days=7)
    prev_week_end = today - timedelta(days=7)
    prev_week_completed = _sum_window(facts, prev_week_start, prev_week_end)
    wow_amount, wow_rate = _diff_rate(
        week_completed, prev_week_completed if prev_week_completed else None)

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
        yesterday_sales=yesterday_sales,
        dod_amount=dod_amount,
        dod_rate=dod_rate,
        week_completed=week_completed,
        prev_week_completed=prev_week_completed,
        wow_amount=wow_amount,
        wow_rate=wow_rate,
        mom_amount=mom_amount,
        mom_rate=mom_rate,
        month_completed=month_completed,
        month_target=month_target,
        month_rate=achievement_rate(month_completed, month_target),
        prev_month_completed=prev_completed,
        prev_month_target=prev_month_target,
        prev_month_rate=achievement_rate(prev_completed, prev_month_target),
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


def _scope_table(*, header, items, total):
    """板块/部门共用的 13 列表体（纯 markdown 表格，同「完成率榜」样式）。

    列：名称 | 今日 昨日 日环比 | 本周 上周 周环比 | 月累计 月目标 |
    上月累计 上月目标 | 本月达成率 上月达成率（口径见 ``compute_daily_metrics``）。
    """
    lines = [
        f"| {header} | 今日 | 昨日 | 日环比 | 本周 | 上周 | 周环比 | 月累计 "
        "| 月目标 | 上月累计 | 上月目标 | 本月达成率 | 上月达成率 |",
        "|:--|--:|--:|:--:|--:|--:|:--:|--:|--:|--:|--:|:--:|:--:|",
    ]
    for m in (*items, total):
        label = f"**{m.label}**" if m.scope == TOTAL_SCOPE_KEY else m.label
        month_target = _fmt_wan(m.month_target) if m.month_target else "--"
        prev_target = (
            _fmt_wan(m.prev_month_target) if m.prev_month_target else "--")
        month_rate = (
            _fmt_pct(m.month_rate) if m.month_rate is not None else "--")
        prev_rate = (
            _fmt_pct(m.prev_month_rate)
            if m.prev_month_rate is not None else "--")
        lines.append(
            f"| {label} | {_fmt_wan(m.sales)} | {_fmt_wan(m.yesterday_sales)} "
            f"| {_signed_pct(m.dod_rate)} | {_fmt_wan(m.week_completed)} "
            f"| {_fmt_wan(m.prev_week_completed)} | {_signed_pct(m.wow_rate)} "
            f"| {_fmt_wan(m.month_completed)} | {month_target} "
            f"| {_fmt_wan(m.prev_month_completed)} | {prev_target} "
            f"| {month_rate} | {prev_rate} |"
        )
    return lines


def build_daily_markdown(*, business_date, rows, total, dept_rows=None,
                         dept_total=None):
    """日报正文：**区域**与**部门**两部分。

    两部分是同一批数据的两种切法，各自合计都等于线下整体（**不是相加**）。
    部门部分按闭环画布口径（``fetch_dept_daily_facts``），缺失时省略。
    """
    d = business_date
    lines = [
        f"【线下整体日报】{d.month}月{d.day}日（周{_WEEKDAYS[d.weekday()]}）",
        f"今日合计：**{_fmt_wan(total.sales)} 元** · "
        f"日环比 {_signed_pct(total.dod_rate)} · 周环比 {_signed_pct(total.wow_rate)}",
        "",
        "**一、区域**",
        "",
        *_scope_table(header="区域", items=rows, total=total),
    ]
    if dept_rows:
        lines += [
            "",
            "**二、部门**",
            "",
            *_scope_table(
                header="部门",
                items=dept_rows,
                total=dept_total or compute_dept_total_fallback(dept_rows),
            ),
        ]
    return "\n".join(lines)


def compute_dept_total_fallback(dept_rows):
    """部门合计兜底（无独立合计行时由行相加；仅用于渲染，不落库）。"""
    sales = sum(m.sales for m in dept_rows)
    completed = sum(m.month_completed for m in dept_rows)
    target_values = [m.month_target for m in dept_rows if m.month_target]
    month_target = sum(target_values) if target_values else None
    rate = achievement_rate(completed, month_target) if month_target else None
    return ScopeMetrics(
        scope=TOTAL_SCOPE_KEY,
        label="线下整体",
        sales=sales,
        month_completed=completed,
        month_target=month_target,
        month_rate=rate,
    )


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


def run_daily_summary(connection, outbox, *, business_date, now,
                      dedupe_suffix=None):
    """每日 20:30：板块当日 + 环比 + 月累计 → agg 落表 + outbox。

    返回 ``"enqueued" | "already_sent"``。``dedupe_suffix``（run-once
    手动触发 `--force` 传入）另起去重键强制重发。
    """
    prev_first, _ = previous_month(business_date)
    prev_year, prev_month = prev_first.year, prev_first.month
    facts_by_scope = {}
    targets = {}
    prev_targets = {}
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
        prev_targets[scope] = fetch_scope_month_target(
            connection, region=region, anchor=anchor,
            year=prev_year, month=prev_month,
        )

    rows = [
        compute_daily_metrics(
            scope, label,
            facts=facts_by_scope[scope],
            business_date=business_date,
            month_target=targets[scope],
            prev_month_target=prev_targets[scope],
        )
        for scope, label, _, _ in AGG_SCOPES
    ]
    total = compute_daily_metrics(
        TOTAL_SCOPE_KEY, TOTAL_LABEL,
        facts=merge_facts([facts_by_scope[s] for s, _, _, _ in AGG_SCOPES]),
        business_date=business_date,
        month_target=merge_targets(targets.values()),
        prev_month_target=merge_targets(prev_targets.values()),
    )

    # 部门部分（闭环画布口径，仅杭州、绍兴区域）：基成员 ∘ 归位覆盖 →
    # 姓名→部门，再聚合。用户裁决（2026-10-19）：部门表只看杭州、绍兴。
    dept_map = fetch_member_dept_map(
        connection, fetch_all_member_regions(connection))
    dept_of = {name: dept for (_, name), dept in dept_map.items()}
    canvas_names = set(dept_map.values())  # 严格只认画布：回退值须在此集合内
    dept_region = {}
    for (region, name), dept in dept_map.items():
        dept_region.setdefault(dept, region)
    dept_facts = {
        dept: facts
        for dept, facts in fetch_dept_daily_facts(
            connection, start=prev_first, end=business_date, dept_of=dept_of,
            canvas_names=canvas_names,
        ).items()
        if dept_region.get(dept) in DEPT_TABLE_REGIONS
    }
    dept_targets = fetch_dept_month_targets(
        connection, year=business_date.year, month=business_date.month,
        dept_of=dept_of, canvas_names=canvas_names,
    )
    dept_prev_targets = fetch_dept_month_targets(
        connection, year=prev_year, month=prev_month,
        dept_of=dept_of, canvas_names=canvas_names,
    )
    dept_rows = sorted(
        (
            compute_daily_metrics(
                dept, dept,
                facts=facts,
                business_date=business_date,
                month_target=dept_targets.get(dept),
                prev_month_target=dept_prev_targets.get(dept),
            )
            for dept, facts in dept_facts.items()
        ),
        key=lambda m: -m.sales,
    )
    # 未归位桶（历史脏部门名）无量时不展示；有量则保留，避免悄悄丢数。
    dept_rows = [
        m for m in dept_rows
        if not (m.scope == UNMAPPED_DEPT and not m.sales
                and not m.month_completed)
    ]
    dept_total = compute_daily_metrics(
        TOTAL_SCOPE_KEY, DEPT_TABLE_TOTAL_LABEL,
        facts=merge_facts(dept_facts.values()),
        business_date=business_date,
        month_target=merge_targets(dept_targets.get(d) for d in dept_facts),
        prev_month_target=merge_targets(
            dept_prev_targets.get(d) for d in dept_facts),
    ) if dept_rows else None

    upsert_agg_daily(
        connection, stat_date=business_date, rows=[*rows, total], synced_at=now,
    )
    body = build_daily_markdown(
        business_date=business_date, rows=rows, total=total,
        dept_rows=dept_rows, dept_total=dept_total,
    )
    enqueued = outbox.enqueue(
        region="offline_all",
        kind=DAILY_KIND,
        business_date=business_date,
        title=DAILY_TITLE,
        body_md=body,
        created_at=now,
        dedupe_suffix=dedupe_suffix,
    )
    return "enqueued" if enqueued else "already_sent"


def run_weekly_summary(connection, outbox, *, reference, now,
                       dedupe_suffix=None):
    """每周一 09:30：上周周报（周合计/周环比/板块排名）→ outbox。

    ``dedupe_suffix``（run-once 手动触发 `--force` 传入）强制重发。
    """
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
        dedupe_suffix=dedupe_suffix,
    )
    return "enqueued" if enqueued else "already_sent"


def run_monthly_summary(connection, outbox, *, reference, now,
                        dedupe_suffix=None):
    """每月 1 日 10:00：上月月报（月合计/月环比/达成率榜）→ outbox。

    ``dedupe_suffix``（run-once 手动触发 `--force` 传入）强制重发。
    """
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
        dedupe_suffix=dedupe_suffix,
    )
    return "enqueued" if enqueued else "already_sent"


# ---------------------------------------------------------------------------
# 榜单页（pages-offline_all）：人员总榜 + 日/周/月三维度板块
# ---------------------------------------------------------------------------

#: 人员总榜覆盖：杭/绍两区走 ``mart_collect``（与各区榜单页逐行同口径），
#: 其余线下人员走补集（region ∉ 杭/绍/vanke，当月有填报才进榜，零填报不造
#: 幽灵行；目标/达成率=None 不参与排序——补集人员无名册语义，纯汇总桶）。
OFFLINE_PEOPLE_REGIONS = ("hangzhou", "shaoxing")

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


@dataclass(frozen=True)
class OverviewRow:
    """单板块日/周/月三期同排行（页面总览表）。"""

    scope: str
    label: str
    day_sales: float
    day_base: float
    dod_rate: float | None
    week_sales: float
    week_base: float
    wow_rate: float | None
    month_completed: float
    month_base: float
    mom_rate: float | None
    month_target: float | None
    month_rate: float | None


def compute_overview_row(scope, label, *, facts, report_day, data_date, month_target):
    """总览行：当日（对昨日）+ 本周累计（对上周同期）+ 本月累计（对上月同期）。

    *facts* 须覆盖上月 1 日至 *data_date*（缺日 = 0）；当日列锚定
    *report_day*（最近有数据自然日），周/月列锚定 *data_date*（T-1）。
    """
    day_sales = facts.get(report_day, 0.0)
    day_base = facts.get(report_day - timedelta(days=1), 0.0)
    _, dod_rate = _diff_rate(day_sales, day_base)

    week_start = data_date - timedelta(days=data_date.weekday())
    week_sales = _sum_window(facts, week_start, data_date)
    week_base = _sum_window(
        facts, week_start - timedelta(days=7), data_date - timedelta(days=7)
    )
    _, wow_rate = _diff_rate(week_sales, week_base)

    month_first = data_date.replace(day=1)
    month_completed = _sum_window(facts, month_first, data_date)
    prev_first, prev_last = previous_month(data_date)
    prev_same_day = min(data_date.day, prev_last.day)
    month_base = _sum_window(facts, prev_first, prev_first.replace(day=prev_same_day))
    _, mom_rate = _diff_rate(month_completed, month_base)

    return OverviewRow(
        scope=scope,
        label=label,
        day_sales=day_sales,
        day_base=day_base,
        dod_rate=dod_rate,
        week_sales=week_sales,
        week_base=week_base,
        wow_rate=wow_rate,
        month_completed=month_completed,
        month_base=month_base,
        mom_rate=mom_rate,
        month_target=month_target,
        month_rate=achievement_rate(month_completed, month_target),
    )


def _cmp_cell(rate, base_label, base):
    """环比单元格：百分比 + 对比基期小字（基期 0 → 比率记 --，基期仍可见）。"""
    pct, cls = _signed_pct_html(rate)
    return f'{pct}<div class="small muted">{base_label} {_fmt_wan(base)}</div>', cls


def build_overview_panel(*, rows, total, report_day, week_start, data_date):
    """日/周/月三期总览单表：板块 ×（当日 / 本周 / 本月，环比右侧带基期）。"""
    head = (
        f'<div class="small muted" style="margin-bottom:8px">'
        f'报告日 {report_day.month}月{report_day.day}日（最近有数据自然日）'
        f' · 本周 {week_start.month}月{week_start.day}日 至 '
        f'{data_date.month}月{data_date.day}日'
        f' · 当日合计 <b style="color:#1f2329">{_fmt_wan(total.day_sales)} 元</b></div>'
    )

    def cells(m):
        dod, dod_cls = _cmp_cell(m.dod_rate, "昨日", m.day_base)
        wow, wow_cls = _cmp_cell(m.wow_rate, "上周同期", m.week_base)
        mom, mom_cls = _cmp_cell(m.mom_rate, "上月同期", m.month_base)
        rate_txt = _fmt_pct(m.month_rate) if m.month_rate is not None else "--"
        return [
            (_fmt_wan(m.day_sales), ""),
            (dod, dod_cls),
            (_fmt_wan(m.week_sales), ""),
            (wow, wow_cls),
            (_fmt_wan(m.month_completed), ""),
            (_fmt_wan(m.month_target) if m.month_target else "--", "muted"),
            (rate_txt, ""),
            (mom, mom_cls),
        ]

    return (
        head
        # 窄屏（手机）9 列放不下：容器内横向滚动，不撑破面板
        + '<div style="overflow-x:auto">'
        + '<table><thead><tr><th>板块</th><th>当日</th><th>日环比</th>'
          '<th>本周累计</th><th>周环比</th><th>月累计</th><th>月目标</th>'
          '<th>达成率</th><th>月环比</th></tr></thead>'
        + f'<tbody>{_scope_trs(rows, total, cells)}</tbody></table></div>'
        + '<div class="small muted" style="margin-top:8px">'
          '日环比=当日÷昨日（自然日）；周环比=本周累计÷上周同期；'
          '月环比=本月1日至报告日累计÷上月1日至同日累计；'
          '环比下方小字为对比基期（基期为 0 时比率记 --）；'
          f'数据截至 {data_date.month}月{data_date.day}日（T-1）</div>'
    )


def build_offline_panels(connection, *, business_date):
    """日/周/月三期总览表（单 panel 单表），**统一 T-1**。

    运维裁决（2026-09-23）：今天的看板看昨天的数据——取数窗口、月目标
    月份、周/月区间、当日列报告日上限全部锚定 ``business_date - 1``，
    当日填报进度不进看板。2026-10-08 运维反馈：日/周/月分 tab 后环比
    只有百分比没有对比基期，且日维度"周环比"（当日÷上周同日）与周维度
    "环比上周同期"（周累计÷上周同期累计）口径不同方向相反，观感错位——
    合并为单表，环比下方直接标注对比基期。板块异常 → 占位降级
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

    def overview():
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
        week_start = data_date - timedelta(days=data_date.weekday())
        rows = [
            compute_overview_row(
                scope, label, facts=facts_by_scope[scope],
                report_day=report_day, data_date=data_date,
                month_target=targets[scope],
            )
            for scope, label, _, _ in AGG_SCOPES
        ]
        total = compute_overview_row(
            TOTAL_SCOPE_KEY, TOTAL_LABEL, facts=merged,
            report_day=report_day, data_date=data_date,
            month_target=total_target,
        )
        return build_overview_panel(
            rows=rows, total=total, report_day=report_day,
            week_start=week_start, data_date=data_date,
        )

    try:
        body = overview()
    except Exception:
        logger.warning("offline_all 总览板块生成失败，降级为占位", exc_info=True)
        body = '<div class="muted small">数据暂缺</div>'
    return [f'<div class="panel">\n{body}\n</div>']


def _collect_other_people(connection, *, business_date, elapsed_days):
    """补集人员行（「其他」桶）：当月 region ∉ 杭/绍/vanke 的事实按人聚合。

    与 ``mart_collect`` 同 ``summarize_people`` 口径（同 elapsed_days）。
    offline_extra 行「姓名↔部门」互换保留（其 responsible_person 是板块名、
    department 才是责任人——AI 表时代遗留行形）；补集人员无名册语义：
    目标缺省 0、达成率 None（排序沉底，不参与达成率比较）。
    """
    from common.metrics.daily_report import summarize_people

    first = month_days(business_date.year, business_date.month)[0]
    placeholders = ", ".join(["%s"] * len(_OFFLINE_OTHER_EXCLUDES))
    sql = (
        "SELECT `region` AS `region`, `responsible_person` AS `rp`, "
        "`department` AS `dept`, `business_date` AS `business_date`, "
        "`sales_amount` AS `sales_amount`, `monthly_target` AS `monthly_target` "
        "FROM `fact_daily_report_offline` "
        f"WHERE `region` NOT IN ({placeholders}) "
        "AND `sales_amount` IS NOT NULL "
        "AND `responsible_person` NOT LIKE '%%合计%%' "
        "AND `business_date` BETWEEN %s AND %s"
    )
    params = [*_OFFLINE_OTHER_EXCLUDES, first, business_date]
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    facts = []
    dept_by_person = {}
    for row in rows:
        get = row.get if isinstance(row, dict) else None
        if get is None:  # tuple 行（兜底）：按 SELECT 列序取值
            region, rp, dept = row[0], row[1], row[2]
        else:
            region, rp, dept = get("region"), get("rp"), get("dept")
        if region == "offline_extra":
            name, dept = (dept or "").strip(), (rp or "").strip()
        else:
            name, dept = (rp or "").strip(), (dept or "").strip()
        if not name:
            continue
        if get is None:
            business_date_, sales_amount_, monthly_target_ = row[3], row[4], row[5]
        else:
            business_date_ = get("business_date")
            sales_amount_ = get("sales_amount")
            monthly_target_ = get("monthly_target")
        facts.append({
            "responsible_person": name,
            "business_date": business_date_,
            "sales_amount": sales_amount_,
            "monthly_target": monthly_target_,
        })
        dept_by_person.setdefault(name, dept or "其他")

    summaries = summarize_people(facts, elapsed_days=elapsed_days)
    return [
        {
            "name": name,
            "dept": dept_by_person.get(name, "其他"),
            "target": summary.target if summary.target is not None else 0,
            "completed": summary.completed,
            "unfilled": summary.unfilled,
            "rate": summary.rate,
        }
        for name, summary in summaries.items()
        if "合计" not in name
    ]


def collect_offline_all_people(connection, *, business_date, include_today=False):
    """线下整体人员总榜采集（页面与 08:30 群播报同口径，无人例外）。

    线下整体无 ``region='offline_all'`` 的事实行——人员为
    ``OFFLINE_PEOPLE_REGIONS`` 各 region 的 ``mart_collect`` 结果合并
    （与各区域榜单页逐行同口径；「合计」行由 mart_collect 统一跳过），
    排序与 ``mart_collect`` 同键（-rate(None→-1), -completed, -target）。
    人/部门归位（运维口径 2026-09-23，人与部门不要混乱）：
    * offline_extra 行「姓名↔部门」互换——事实行的 responsible_person 是
      板块（省外/线下总经办）、department 才是责任人（余云涛/谢坚钰）；
    * 杭/绍人员部门以通讯录 ``dim_robot_member`` 为准（表内部门是手工
      叫法），无匹配保留表内值兜底。

    ``include_today`` 透传 mart_collect（BI 看板实时口径含当天已报；
    页面/播报缺省 False 维持 T-1）。返回 ``(people, workdays)``。
    """
    from common.daily_robot.mart_leaderboard import mart_collect
    from common.metrics.daily_report import elapsed_workdays, fetch_workdays

    people = []
    by_region = {}
    workdays = frozenset()
    for region in OFFLINE_PEOPLE_REGIONS:
        data = mart_collect(
            connection, region=region, business_date=business_date,
            include_today=include_today,
        )
        rows = [dict(p) for p in data.people]
        by_region[region] = rows
        people.extend(rows)
        workdays = data.workdays

    # 补集（「其他」桶）：当月有填报且 region ∉ 杭/绍/vanke 的人员，
    # 与 mart_collect 同 summarize_people 口径；无目标 → rate=None 沉底。
    if not workdays:
        workdays = fetch_workdays(
            connection, year=business_date.year, month=business_date.month
        )
    elapsed_set = elapsed_workdays(
        workdays, today=business_date, include_today=include_today
    )
    people.extend(_collect_other_people(
        connection, business_date=business_date, elapsed_days=elapsed_set
    ))

    # 部门归属以通讯录为准（无匹配保留表内部门）
    dept_map = fetch_member_dept_map(connection, OFFLINE_PEOPLE_REGIONS)
    for region in OFFLINE_PEOPLE_REGIONS:
        for p in by_region[region]:
            p["dept"] = dept_map.get((region, p["name"]), p["dept"])

    people.sort(
        key=lambda p: (
            -(p["rate"] if p["rate"] is not None else -1),
            -p["completed"],
            -p["target"],
        )
    )
    return people, workdays


def build_offline_all_html(connection, cfg, *, business_date, now):
    """线下整体榜单页：人员总榜（``collect_offline_all_people``）+ 维度标签。

    板块：日/周/月三期总览表（``build_offline_panels``，extra_panels 插入）。
    """
    from common.daily_robot.leaderboard import build_html
    from common.daily_robot.mart_leaderboard import build_leaderboard_view
    from common.metrics.daily_report import elapsed_workdays

    year, month = business_date.year, business_date.month
    people, workdays = collect_offline_all_people(
        connection, business_date=business_date
    )
    view = build_leaderboard_view(cfg, workdays, year=year, month=month)
    elapsed = sorted({d.day for d in elapsed_workdays(workdays, today=business_date)})
    panels = build_offline_panels(connection, business_date=business_date)
    return build_html(view, now, elapsed, people, extra_panels=panels)
