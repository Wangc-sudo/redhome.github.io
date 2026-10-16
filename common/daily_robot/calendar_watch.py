# -*- coding: utf-8 -*-
"""播报日历覆盖看门（calendar-watch，每月 25 日 10:00）。

背景缺口：``dim_calendar`` 由版本受控种子 + 规则兜底重建——若兜底机制
失效（翻年/翻月跌出窗口），当月无行 → ``mart_collect`` 抛
``MartTaskError`` → 榜单/催办/报数全线**静默**失败（robot_error 只落
日志）。本管道把「覆盖缺口」显式发到群里，闭环最后一公里。

2026-10-16 纯播报口径：基线每天都是播报日（rest=∅），休息日只来自
人工裁决——规则兜底不再是「口径失真」来源，故只保留零行告警：

* P0 当月零行（兜底机制失效才会出现，防御性保留）；
* P1 下月零行（兜底窗口外才会出现，防御性保留）。

无问题 → 只打印不入队（不打扰）。去重键 ``offline_all:calendar_watch:
<date>``（按月跑天然幂等）；``--force`` 手动触发绕开去重。
"""

import contextlib

#: 告警群（线下整体汇总群，运维日常盯的群）。
WATCH_REGION = "offline_all"
WATCH_KIND = "calendar_watch"
WATCH_TITLE = "播报日历覆盖告警"

#: 消息里的处置指引。
_FIX_HINT = (
    "处置：排查 extract-mart 日历物化（docker/integration/"
    "calendar.seed.json 覆盖月 + 规则兜底窗口），修复后下轮 "
    "extract-mart 自动重建 dim_calendar。"
)


def _shift_year_month(year_month, months):
    """``YYYY-MM`` 平移 *months* 个月（可负）。"""
    year, month = (int(part) for part in year_month.split("-"))
    index = year * 12 + (month - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def fetch_calendar_coverage(connection, year_months):
    """``{YYYY-MM: {"rows": n}}``（dim_calendar 口径）。"""
    coverage = {}
    with contextlib.closing(connection.cursor()) as cursor:
        for year_month in year_months:
            cursor.execute(
                "SELECT COUNT(*) AS `n` FROM `dim_calendar` "
                "WHERE DATE_FORMAT(`business_date`, '%%Y-%%m') = %s",
                (year_month,),
            )
            row = cursor.fetchone() or {}
            coverage[year_month] = {"rows": int(row.get("n") or 0)}
    return coverage


def classify_issues(coverage, *, current_month, next_month):
    """覆盖快照 → 问题列表 ``[(级别, 文案)]``（级别 P0/P1 升序即文案序）。"""
    issues = []
    current = coverage.get(current_month, {"rows": 0})
    following = coverage.get(next_month, {"rows": 0})
    if current["rows"] == 0:
        issues.append((
            "P0",
            f"当月（{current_month}）dim_calendar 零行——规则兜底未生效，"
            "榜单/催办/报数将失败，请立即排查 extract-mart。",
        ))
    if following["rows"] == 0:
        issues.append((
            "P1",
            f"下月（{next_month}）dim_calendar 零行——翻月将跌出兜底窗口，"
            "请在月底前排查日历基线覆盖。",
        ))
    return issues


def build_watch_markdown(*, issues):
    """告警正文（markdown）：分级问题列表 + 处置指引。"""
    lines = [f"### {WATCH_TITLE}", ""]
    for level, text in issues:
        lines.append(f"- **{level}** {text}")
    lines.append("")
    lines.append(_FIX_HINT)
    return "\n".join(lines)


def run_watch(connection, outbox, *, anchor_date, now, dedupe_suffix=None):
    """检查当月/下月覆盖，异常发线下整体群。

    返回 ``("ok", 0)`` 或 ``("alerted", 问题数, "enqueued"|"already_sent")``。
    """
    current_month = anchor_date.strftime("%Y-%m")
    next_month = _shift_year_month(current_month, 1)
    coverage = fetch_calendar_coverage(connection, (current_month, next_month))
    issues = classify_issues(
        coverage, current_month=current_month, next_month=next_month
    )
    if not issues:
        return ("ok", 0)
    enqueued = outbox.enqueue(
        region=WATCH_REGION,
        kind=WATCH_KIND,
        business_date=anchor_date,
        title=WATCH_TITLE,
        body_md=build_watch_markdown(issues=issues),
        created_at=now,
        dedupe_suffix=dedupe_suffix,
    )
    return ("alerted", len(issues), "enqueued" if enqueued else "already_sent")
