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

月天数（2026-10-10 修复）：展示层 ``n_total`` 由
``build_leaderboard_view`` 注入的 ``daysInMonth``（``month_days`` 真实
天数）驱动——旧 ``range(1, 31)`` quirk（31 天月漏第 31 日、2 月虚增
幻影工作日）已消除；无 ``daysInMonth`` 的旧 config 形态回退 30。
"""

import logging
from dataclasses import dataclass, field
from datetime import date

from common.calendar_utils import month_days
from common.daily_robot.mart_tasks import MartTaskError
from common.metrics.daily_report import (
    achievement_rate,
    elapsed_workdays,
    fetch_month_facts,
    fetch_workdays,
    summarize_people,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LeaderboardData:
    """一个区域某天的榜单采集结果（``collect`` 同形 + 工作日集合）。"""

    business_date: date
    elapsed: tuple
    people: tuple
    workdays: frozenset = field(default_factory=frozenset)


def _fetch_rows(connection, sql, params=()):
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        return list(cursor.fetchall())
    finally:
        cursor.close()


def fetch_store_groups(connection, *, region, year_month):
    """名册里「多人共用一个门店/单元，且该门店登记了正目标」的合并组。

    2026-11-18 用户裁决（诸暨门店场景）：诸暨门店 3 人轮班（蒋丽兰
    负责人 + 宣卓萍/郑洁佩 代填人）共担一个门店目标，但个人榜按人名
    取目标 → 三人月目标全是 0。合并规则保守，只命中真正的门店/单元：

    * 名册 ``entity_type='person'`` 且启用，同一 ``entity_key`` ≥ 2 人；
    * 该 ``entity_key`` 在 ``dim_report_target`` 有当月目标行且 > 0。

    部门型名册（杭州 ``杭中/滨萧/余杭``、绍兴 ``诸暨项目部``）的目标
    按人名登记，没有以部门名为键的目标行 → 天然不命中；目标为 0 的
    单元（vanke）维持逐人展示，行为零变化。

    返回 ``{entity_key: {"members": (姓名...), "target": 元}}``
    （目标是 ``dim_report_target`` 的万元原值 ×10000，与 fact 快照同口径）。
    """
    roster = _fetch_rows(
        connection,
        "SELECT `entity_key`, `person_name` FROM `dim_report_roster` "
        "WHERE `scope` = %s AND `entity_type` = 'person' AND `enabled` = 1",
        (region,),
    )
    if not roster:
        return {}
    targets = _fetch_rows(
        connection,
        "SELECT `person_name`, `monthly_target` FROM `dim_report_target` "
        "WHERE `scope` = %s AND `year_month` = %s",
        (region, year_month),
    )
    target_by_name = {}
    for row in targets:
        name = str(row.get("person_name") or "").strip()
        value = row.get("monthly_target")
        if name and value is not None:
            target_by_name[name] = float(value)
    members = {}
    for row in roster:
        key = str(row.get("entity_key") or "").strip()
        name = str(row.get("person_name") or "").strip()
        if key and name:
            members.setdefault(key, []).append(name)
    groups = {}
    for key, names in members.items():
        if len(names) < 2:
            continue
        target_wan = target_by_name.get(key)
        if not target_wan or target_wan <= 0:
            continue
        groups[key] = {"members": tuple(names), "target": target_wan * 10000}
    return groups


def merge_store_groups(people, *, groups):
    """把门店合并组的成员行合并成一行（纯函数）。

    * 组内**无任何事实行** → 不造行（榜单只列有数据的人，维持现状）；
    * ``completed`` 组内求和；``unfilled`` 取组内最小（轮班：任一人填了
      即门店当日已填，不重复计缺勤）；``target`` 取门店目标（元）；
    * ``rate`` 走 ``achievement_rate``（目标缺失或 <=0 → None）；
    * 未命中任何组的行原样保留，排序交由调用方统一处理。
    """
    if not groups:
        return list(people)
    by_name = {p["name"]: p for p in people}
    consumed = set()
    merged = []
    for key, group in groups.items():
        rows = [by_name[name] for name in group["members"] if name in by_name]
        if not rows:
            continue
        consumed.update(row["name"] for row in rows)
        completed = sum(float(row["completed"] or 0) for row in rows)
        target = group["target"]
        merged.append({
            "name": key,
            "dept": rows[0]["dept"],
            "target": target,
            "completed": completed,
            "unfilled": min(int(row["unfilled"] or 0) for row in rows),
            "rate": achievement_rate(completed, target),
            "merged_members": len(rows),
        })
    return [p for p in people if p["name"] not in consumed] + merged


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

    # 归位覆盖并入（2026-10-19 用户裁决「画布与 BI 同真源」）：画布拉线
    # 归口（dim_org_assignment_override）对个人完成率榜/静态榜单页/播报
    # 的部门列同步生效（杭州销售部案例：余发兴归位后个人榜仍显示杭中
    # 项目部 = 只修了聚合卡没修人级卡的漏网）；读取失败 fail-soft 回退
    # 表内值（懒加载避免 offline_summary 环依赖）。
    try:
        from common.daily_robot.offline_summary import fetch_member_dept_map
        dept_map = fetch_member_dept_map(connection, (region,))
        for name in list(dept_by_person):
            mapped = dept_map.get((region, name))
            if mapped:
                dept_by_person[name] = mapped
    except Exception:
        logger.warning(
            "归位部门映射读取失败，按表内值口径：%s", region, exc_info=True)

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
    # 门店合并展示（2026-11-18 用户裁决）：同一门店多人轮班共担一个门店
    # 目标时，榜单按门店出一行（完成额求和 / 目标取门店目标行），否则
    # 个人榜永远 0 目标（诸暨门店 3 人案例）。部门型名册与目标为 0 的
    # 单元不命中，逐人口径零变化；读取失败 fail-soft 退回逐人。
    try:
        groups = fetch_store_groups(
            connection, region=region,
            year_month=business_date.strftime("%Y-%m"),
        )
        people = merge_store_groups(people, groups=groups)
    except Exception:
        logger.warning(
            "门店合并组读取失败，按逐人口径：%s", region, exc_info=True)

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
    ``calendar.daysInMonth`` 为 ``month_days`` 真实月天数（2026-10-10
    修复展示层 ``range(1, 31)`` quirk——31 天月漏算第 31 日、2 月虚增
    幻影工作日）；``region`` 复刻现行 ``region`` 段（deptOrder /
    deptLabel / broadcastExclude / displayName）。
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
        "calendar": {
            "month": month,
            "restDays": rest_days,
            "daysInMonth": len(month_days(year, month)),
        },
    }
