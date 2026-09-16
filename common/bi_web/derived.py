# -*- coding: utf-8 -*-
"""派生指标纯口径（CubeSchema §2）——后端算，前端只渲染。

口径真相源是 ``frontend/bi-ui/CubeSchema.md``（§2 派生口径、§2.3 四级
告警、§5 数据缺陷显式化）；日报机器人侧的同名口径见
``common/metrics/daily_report.py``（月目标一律 ``MAX(monthly_target)``，
绝不跨行 SUM —— melt 陷阱）。

本模块**零 DB 依赖**：输入是标量与日期集合，输出是标量（金额单位跟随调用
方，本模块不做任何万/元换算）。``common/bi_web/queries.py`` 的
``run_kpi_shortfall`` / ``run_anomaly_top`` 只查事实，派生一律调用这里，
所以四级告警可以在单测里被钉死，而不需要一个 MySQL。

数据缺陷（§5）：不可算一律 ``None``，前端渲染「—」并在必要时加
``low_conf`` / 数据质量角标，绝不静默补 0。
"""

from datetime import datetime

#: §2.3 severity 值域（前端 enum domain 必须与此一致）。
SEVERITY_DOMAIN = ("p0", "p1", "p2", "ok")

#: §2.3 p0：``done == 0`` **且** 已过 ≥ 2 个工作日（按工作日计，排除周末）。
P0_MIN_ELAPSED_WORKDAYS = 2

#: §2.3 p1：缺口需达到「剩余工作日产能」的一半。
P1_CAPACITY_FACTOR = 0.5


# ---------------------------------------------------------------------------
# 日历（dim_calendar 的形态适配，纯函数）
# ---------------------------------------------------------------------------

def _as_date(value):
    """``datetime`` → ``date``（dim_calendar 可能返回 datetime）。"""
    if isinstance(value, datetime):
        return value.date()
    return value


def workdays(calendar):
    """取工作日子集：接受 ``dim_calendar`` 行或纯日期序列。

    形态一：``{"business_date": date, "is_workday": 1}`` 行（
    ``daily_report.fetch_workdays`` 已在 SQL 侧过滤，这里只做兜底）；
    形态二：``date`` 序列（调用方自己筛过的工作日）。``business_date``
    或 ``is_workday`` 缺失的行直接跳过 —— 日历缺行时工作日数不可判，
    不能把未知日算成工作日。
    """
    days = set()
    for item in calendar or ():
        if isinstance(item, dict):
            day = _as_date(item.get("business_date"))
            flag = item.get("is_workday")
            if day is None or flag is None or not int(flag):
                continue
        else:
            day = _as_date(item)
            if day is None:
                continue
        days.add(day)
    return frozenset(days)


def total_workdays(calendar):
    """当月总工作日数（``required_daily`` 的分母）。"""
    return len(workdays(calendar))


def elapsed_workdays(calendar, as_of):
    """已过工作日数（含锚点日本身）。

    含锚点日与 ``queries._people_as_of``（``include_today=True``）同口径：
    MTD 已完成额本身就含当天，否则第 1 个工作日整天看不到告警。
    """
    anchor = _as_date(as_of)
    if anchor is None:
        return 0
    return sum(1 for day in workdays(calendar) if day <= anchor)


def remaining_workdays(calendar, as_of):
    """剩余工作日数 = 严格晚于锚点日的工作日（§2.3）。"""
    anchor = _as_date(as_of)
    if anchor is None:
        return 0
    return sum(1 for day in workdays(calendar) if day > anchor)


# ---------------------------------------------------------------------------
# §2 派生指标
# ---------------------------------------------------------------------------

def shortfall(target_should, done):
    """§2.1 缺口 = 当期目标总额 − 已完成（``target/done`` 同单位）。

    * **绝对口径**：``target_should`` 是目标总额，**不乘时间进度** ——
      否则缺口会随时间机械变小、月初误判「没人落后」。
    * ``target_should`` 缺失（无月目标）→ ``None``（§5：前端「—」，不参与
      缺口排序）；``done`` 缺失按 0 计（无事实行 = 未开单）。
    """
    if target_should is None:
        return None
    return float(target_should) - float(done or 0)


def required_daily(target, total_workdays):
    """§2.3 所需日均 = 月目标 ÷ 当月总工作日。

    目标缺失或日历缺失（``total_workdays`` 为 0/None）→ ``None``：分母
    不可信时不做除法，p1 随之不可判（见 :func:`severity`）。
    """
    if target is None or not total_workdays:
        return None
    count = int(total_workdays)
    if count <= 0:
        return None
    return float(target) / count


def rate(done, target):
    """§2.2 完成率 = done ÷ target。

    沿用 ``daily_report.achievement_rate``：目标缺失或 ≤ 0 → ``None``
    （没有目标的人不该按 0% 计）。
    """
    if target is None or target <= 0:
        return None
    return float(done or 0) / float(target)


def mom(current, prev):
    """§2.4 环比：``(本期 − 上期) ÷ |上期|`` + 方向。

    ``prev`` 缺失或为 0（新上月无基数）→ ``None``：前端渲染「—」，绝不
    用 0 假装环比。方向与数值同源（``up``=涨/红、``down``=跌/绿、
    ``flat``=持平/灰），前端不做二次判断。
    """
    if current is None or prev is None or prev == 0:
        return None
    value = (float(current) - float(prev)) / abs(float(prev))
    if value > 0:
        direction = "up"
    elif value < 0:
        direction = "down"
    else:
        direction = "flat"
    return {"value": value, "direction": direction}


def severity(done, shortfall, required_daily, remaining_workdays, elapsed_workdays):
    """§2.3 四级告警 → ``'p0'|'p1'|'p2'|'ok'``（取最高，最先匹配为准）。

    * ``p0``: ``done == 0`` 且已过 ≥ ``P0_MIN_ELAPSED_WORKDAYS`` 个工作日
      （修复了只判 ``done === 0`` 的旧口径漏洞：月初第 1 个工作日不算 p0）；
    * ``p1``: ``shortfall ≥ required_daily × remaining_workdays × 0.5``
      —— 按当前日销，缺口需半个多月的产能才可能补齐；
    * ``p2``: ``shortfall > 0`` 且未达 p0/p1；
    * ``ok``: ``shortfall ≤ 0``（已达标/超额）。

    不可算的护栏（§5）：``shortfall`` 为 ``None``（无目标）→ 返回 ``None``，
    前端渲染「—」；``required_daily`` / ``remaining_workdays`` 为 ``None``
    （日历缺失或已闭月）→ p1 不可判，退到 p2，**不因数据缺失把人标红**。
    """
    if shortfall is None:
        return None
    if shortfall <= 0:
        return "ok"
    if float(done or 0) == 0 and _elapsed(elapsed_workdays) >= P0_MIN_ELAPSED_WORKDAYS:
        return "p0"
    threshold = p1_threshold(required_daily, remaining_workdays)
    if threshold is not None and shortfall >= threshold:
        return "p1"
    return "p2"


def p1_threshold(required_daily, remaining_workdays):
    """§2.3 p1 阈值 = ``required_daily × remaining_workdays × 0.5``。

    ``required_daily`` 或 ``remaining_workdays`` 不可得 → ``None``（不可判）。
    月末最后一个工作日 ``remaining_workdays == 0`` 时阈值为 0：当日已有
    缺口已无产能可补，按「取最高」落到 p1（有意设计，非自动升级漏洞 ——
    月内同一组数据不会因日期推移改变级别，见测试回归用例）。
    """
    if required_daily is None or remaining_workdays is None:
        return None
    return float(required_daily) * int(remaining_workdays) * P1_CAPACITY_FACTOR


def _elapsed(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
