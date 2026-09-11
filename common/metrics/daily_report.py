# -*- coding: utf-8 -*-
"""销售日报共享口径（spec §9/§10）——robot 与 pages-leaderboard 共同 import。

口径单点，只活在这里，可单测、不污染 mart schema；DB 侧只做结构性补全
（``dim_robot_member`` × 日期 LEFT JOIN 事实表）。与既有三处实现
（``core.recalc_totals`` / ``listener._calc_progress`` / ``leaderboard.collect``）
逐位同口径：

* **达成率 = Σ(已过工作日 sales_amount) ÷ 月目标**；月目标取
  ``MAX(monthly_target)``——melt 后每行重复携带月目标，**绝不可 SUM**
  （spec §10「melt 陷阱」）。
* **单位零换算**：日值与月目标同单位，派生照抄比值，不做单位归一。
* **空值 = 未填**：``sales_amount`` 为 ``None``（或无事实行）计未填，不
  计入分子；显式的 ``0`` 是已填。
* **已过工作日**：严格早于 ``today``；``include_today`` 时含当天
  （``leaderboard.collect`` 同口径）。
* 与既有一致使用 ``float`` 运算；校验容差沿用 0.005（check_data）/
  0.0005（recalc）。
"""

import contextlib
from dataclasses import dataclass
from datetime import date

from common.calendar_utils import month_days


FACT_TABLE = "fact_daily_report_offline"
CALENDAR_TABLE = "dim_calendar"
MEMBER_TABLE = "dim_robot_member"


@dataclass(frozen=True)
class PersonSummary:
    """一个人一个月的达成聚合（纯口径产出，不含展示格式化）。"""

    name: str
    completed: float
    target: float | None
    unfilled: int
    rate: float | None


# ---------------------------------------------------------------------------
# 纯口径（无 IO，全部可单测）
# ---------------------------------------------------------------------------

def achievement_rate(completed, target):
    """``completed / target``；target 缺失或 <= 0 时返回 ``None``。

    与 ``leaderboard.collect`` 的个人口径一致：没有目标的人不出现在
    达成率排序里，而不是按 0% 计。
    """
    if target is None or target <= 0:
        return None
    return completed / target


def elapsed_workdays(workdays, *, today, include_today=False):
    """从工作日集合中筛出**已过**工作日（``date`` 集合）。

    ``today`` 接受 ``datetime.date`` 或 ``datetime.datetime``。
    """
    if not isinstance(today, date):
        raise TypeError("today must be a date")
    return {
        day for day in workdays
        if day < today or (include_today and day == today)
    }


def summarize_people(rows, *, elapsed_days):
    """把一月的事实行按人聚合为 ``{责任人: PersonSummary}``。

    *rows* 是映射迭代，每项含 ``responsible_person`` / ``business_date`` /
    ``sales_amount`` / ``monthly_target``（即 :func:`fetch_month_facts`
    的行形）。同一人同一天出现多行时**最后一行生效**（月表一人一天一
    行，重复即数据异常，不做隐式求和）。
    """
    elapsed_days = set(elapsed_days)
    by_person = {}
    for row in rows:
        name = row.get("responsible_person")
        if not name:
            continue
        by_person.setdefault(name, []).append(row)

    summaries = {}
    for name, person_rows in by_person.items():
        by_day = {}
        for row in person_rows:
            day = row.get("business_date")
            if day in elapsed_days:
                by_day[day] = row.get("sales_amount")

        completed = 0.0
        unfilled = 0
        for day in elapsed_days:
            value = by_day.get(day)
            if value is None:
                unfilled += 1
            else:
                completed += float(value)

        targets = [
            row.get("monthly_target")
            for row in person_rows
            if row.get("monthly_target") is not None
        ]
        target = float(max(targets)) if targets else None

        summaries[name] = PersonSummary(
            name=name,
            completed=completed,
            target=target,
            unfilled=unfilled,
            rate=achievement_rate(completed, target),
        )
    return summaries


def unfilled_members(members, filled_names, *, aliases=None):
    """返回未填的成员行，保持 *members* 传入顺序。

    *members* 是 ``dim_robot_member`` 行（至少含 ``name``）；
    *filled_names* 是当天已有事实行的责任人名（**表内用名**）。
    *aliases* 为「通讯录实名 → 表内用名」映射——``dim`` 存实名、事实表
    存表内用名，两者不一致时必须经映射比较，否则永远判未填。
    """
    aliases = aliases or {}
    filled = set(filled_names)
    return [
        member for member in members
        if aliases.get(member["name"], member["name"]) not in filled
    ]


# ---------------------------------------------------------------------------
# 结构性补全查询（只读 mart_ops；口径判断全部在上面的纯函数里）
# ---------------------------------------------------------------------------

def _fetch_all(connection, sql, params):
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    return [dict(row) for row in rows]


def _month_range(year, month):
    days = month_days(year, month)
    return days[0], days[-1]


def fetch_workdays(connection, *, year, month):
    """返回该年月的 ``dim_calendar`` 工作日集合（``date`` set）。"""
    first, last = _month_range(year, month)
    rows = _fetch_all(
        connection,
        "SELECT `business_date` FROM `dim_calendar` "
        "WHERE `is_workday` = 1 AND `business_date` BETWEEN %s AND %s",
        (first, last),
    )
    return {row["business_date"] for row in rows}


def fetch_month_facts(connection, *, region, year, month):
    """返回该区域该年月的事实行（一人多行，每日一行）。"""
    first, last = _month_range(year, month)
    return _fetch_all(
        connection,
        "SELECT `responsible_person`, `business_date`, `sales_amount`, "
        "`monthly_target` "
        f"FROM `{FACT_TABLE}` "
        "WHERE `region` = %s AND `business_date` BETWEEN %s AND %s",
        (region, first, last),
    )


def fetch_region_members(connection, *, region):
    """返回该区域在册成员（``dim_robot_member``，按姓名排序）。"""
    return _fetch_all(
        connection,
        "SELECT `user_id`, `name`, `region`, `dept_id`, `dept_name` "
        f"FROM `{MEMBER_TABLE}` "
        "WHERE `region` = %s AND `is_active` = 1 ORDER BY `name`",
        (region,),
    )


def fetch_filled_names(connection, *, region, business_date):
    """返回该区域当天已有事实行的责任人名集合（**表内用名**）。"""
    rows = _fetch_all(
        connection,
        "SELECT DISTINCT `responsible_person` "
        f"FROM `{FACT_TABLE}` "
        "WHERE `region` = %s AND `business_date` = %s "
        "AND `responsible_person` IS NOT NULL",
        (region, business_date),
    )
    return {row["responsible_person"] for row in rows}


def fetch_unfilled_members(connection, *, region, business_date):
    """成员 × 日期 LEFT JOIN 事实表：当天无事实行的在册成员。

    这是 spec §9 的「DB 侧结构性补全」。注意连接键是姓名：若存在
    「通讯录实名 ≠ 表内用名」的成员，应改用
    :func:`fetch_region_members` + :func:`fetch_filled_names` +
    :func:`unfilled_members`（带 aliases）的组合。
    """
    return _fetch_all(
        connection,
        "SELECT m.`user_id`, m.`name` "
        f"FROM `{MEMBER_TABLE}` m "
        f"LEFT JOIN `{FACT_TABLE}` f "
        "  ON f.`responsible_person` = m.`name` "
        "  AND f.`region` = %s "
        "  AND f.`business_date` = %s "
        "WHERE m.`region` = %s AND m.`is_active` = 1 "
        "AND f.`source_record_id` IS NULL "
        "ORDER BY m.`name`",
        (region, business_date, region),
    )
