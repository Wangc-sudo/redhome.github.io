# -*- coding: utf-8 -*-
"""榜单的 mart 侧采集与视图适配（阶段 4）。

``leaderboard.collect`` 的 mart 版：数据来自 ``mart_ops``
（``fact_daily_report_offline`` + ``dim_calendar``，经
:mod:`common.metrics.daily_report`），展示层复用
:mod:`common.daily_robot.leaderboard` 的既有函数，输出与现行逐字一致。

与现行 ``collect`` 的对齐点（均有测试锁定）：

* 跳过 ``合计`` 行；姓名单侧 strip；
* ``dept`` 空 → ``未分组``；``target`` 缺失 → 0、``rate`` 为 ``None``；
* 排序键 ``(-rate(None→-1), -completed, -target)``；
* ``elapsed`` 为已过工作日的日号升序。

注意：展示层的 ``n_total`` 沿用历史 ``range(1, 31)`` 口径（31 天月份
的旧 quirk，与现行生产输出一致，不在本次改动范围）。
"""

from dataclasses import dataclass, field
from datetime import date

from common.calendar_utils import month_days
from common.daily_robot.mart_tasks import MartTaskError
from common.metrics.daily_report import (
    elapsed_workdays,
    fetch_month_facts,
    fetch_workdays,
    summarize_people,
)


@dataclass(frozen=True)
class LeaderboardData:
    """一个区域某天的榜单采集结果（``collect`` 同形 + 工作日集合）。"""

    business_date: date
    elapsed: tuple
    people: tuple
    workdays: frozenset = field(default_factory=frozenset)


def mart_collect(connection, *, region, business_date, include_today=False):
    """``collect`` 的 mart 版。返回 :class:`LeaderboardData`。"""
    workdays = fetch_workdays(
        connection, year=business_date.year, month=business_date.month
    )
    if not workdays:
        raise MartTaskError(
            f"dim_calendar 缺少 {business_date:%Y-%m} 的日历行，请先运行 extract-mart"
        )
    elapsed_set = elapsed_workdays(
        workdays, today=business_date, include_today=include_today
    )

    facts = fetch_month_facts(
        connection,
        region=region,
        year=business_date.year,
        month=business_date.month,
    )
    # 与 collect 对齐：先 strip 姓名（空名由 summarize 忽略）。
    facts = [
        {**row,
         "responsible_person": str(row.get("responsible_person") or "").strip()}
        for row in facts
    ]
    summaries = summarize_people(facts, elapsed_days=elapsed_set)

    dept_by_person = {}
    for row in facts:
        name = row["responsible_person"]
        if name and name not in dept_by_person:
            dept_by_person[name] = (
                str(row.get("department") or "").strip() or "未分组"
            )

    people = []
    for name, summary in summaries.items():
        if "合计" in name:
            continue
        people.append({
            "name": name,
            "dept": dept_by_person.get(name, "未分组"),
            "target": summary.target if summary.target is not None else 0,
            "completed": summary.completed,
            "unfilled": summary.unfilled,
            "rate": summary.rate,
        })
    people.sort(key=lambda p: (
        -(p["rate"] if p["rate"] is not None else -1),
        -p["completed"],
        -p["target"],
    ))
    return LeaderboardData(
        business_date=business_date,
        elapsed=tuple(sorted(d.day for d in elapsed_set)),
        people=tuple(people),
        workdays=frozenset(workdays),
    )


def build_leaderboard_view(region_cfg, workdays, *, year, month):
    """``RegionConfig`` + ``dim_calendar`` → 展示层的 config 同形字典。

    ``calendar.restDays`` 由 ``dim_calendar`` 反推（全月日期 − 工作日），
    复刻现行 config.json 的 ``{month, restDays}`` 形态；``region`` 复刻
    现行 ``region`` 段（deptOrder / deptLabel / broadcastExclude /
    displayName）。
    """
    rest_days = sorted(
        d.day for d in set(month_days(year, month)) - set(workdays)
    )
    return {
        "region": {
            "name": region_cfg.region,
            "displayName": region_cfg.display,
            "deptOrder": list(region_cfg.dept_order),
            "deptLabel": dict(region_cfg.dept_label),
            "broadcastExclude": list(region_cfg.broadcast_exclude),
        },
        "calendar": {"month": month, "restDays": rest_days},
    }
