"""BI common 查询域；SQL 与返回契约自原 queries.py 等价迁移。"""



import ast


import textwrap


import threading


import time


from datetime import date, datetime, timedelta


from decimal import Decimal


from common.bi_web import derived, fin_derived


from common.calendar_utils import month_days


from common.daily_robot.mart_leaderboard import mart_collect


from common.daily_robot.mart_tasks import MartTaskError


from common.metrics.daily_report import elapsed_workdays, fetch_workdays


_UNIT = "元"


_GRANULARITY = ("day", "week", "month", "year")


def granularity_options(connection) -> list:
    """静态值域：与查询型 source 同签名，供 option_sets.contains() 校验。"""
    return list(_GRANULARITY)


def _fetch_scalar(connection, sql, params=None) -> Decimal:
    """Run one total query and return its single value as Decimal."""
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    finally:
        cursor.close()
    if not row:
        return Decimal(0)
    value = next(iter(row.values()))
    return Decimal(0) if value is None else Decimal(value)


def _fetch_one(connection, sql, params=None):
    """Run one query and return its single raw value (may be a date/None)."""
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    finally:
        cursor.close()
    if not row:
        return None
    return next(iter(row.values()))


def _fetch_rows(connection, sql, params=None):
    """Run one row query and return its rows as list of dicts."""
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    finally:
        cursor.close()
    return [dict(row) for row in rows]


def _mmdd(business_date) -> str:
    """Format a DATE-like value as ``MM-DD`` (chart axis labels)."""
    if isinstance(business_date, datetime):
        business_date = business_date.date()
    return business_date.strftime("%m-%d")


def month_bounds(month):
    """'YYYY-MM' → (月首, 月末) 日期对（纯日历计算，复用 calendar_utils）。

    带参 SQL 用 ``business_date BETWEEN %s AND %s`` 而非
    ``DATE_FORMAT(business_date, '%Y-%m') = %s``：避开字面 ``%`` 的
    pymysql 双写（窗口小、两写一处即可），且区间谓词可走索引。
    """
    year, _, number = month.partition("-")
    days = month_days(int(year), int(number))
    return days[0], days[-1]


def _align_series_rows(rows, name_key, *, axis, workdays, as_of,
                       label_fmt="%m-%d", bucket="day",
                       date_key="business_date") -> dict:
    """按完整日期窗口对齐各名称序列，逐点判定 ok/zero/missing（三态）。

    ``axis`` 为完整日期窗口（date 列表，升序），由调用方按粒度生成——
    不再从 rows 反推（全区域都没数据那天也必须进轴）；``workdays`` 为
    ``dim_calendar`` 工作日集合（复用 :func:`month_workdays`，不另写
    SQL）；``as_of`` 为锚点日（当前月=今天，历史月=月末，
    :func:`_shortfall_as_of` 同口径；T+1 未入仓/未来的落点标
    missing）。逐点规则（2026-09-18 执行提示词 §4.2，按序判定）：

    1. ``d > as_of``（未来 / T+1 未入仓）→ data ``None``，missing；
    2. 该系列无行 且 该日无任何系列的行 → data ``None``，missing；
    3. 该系列无行 但 该日有其他系列的行 → data ``0.0``，missing
       （该区域未上报，不是真实 0）；
    4. 有行且 ``total > 0`` → data float，ok；
    5. 有行且 ``total == 0``（或 None）且覆盖段内有营业日 → ``0.0``，zero；
    6. 有行且 ``total == 0`` 且覆盖段内无营业日（含日历未覆盖）→ ``0.0``，ok。

    周/月档位的「该日」读作桶起始日；营业日判定取「桶 ∩ (-∞, as_of]
    内是否有工作日」，日档位退化为 ``d in workdays`` 本身。
    ``dim_calendar`` 未覆盖的月份 workdays 为空 → 一律降级 ok（宁可
    漏报，不可误报红）。``label_fmt``/``bucket`` 为粒度档位参数，日档
    缺省即旧口径（MM-DD 标签、按日判定）；``date_key`` 为行内日期键，
    周/月档传 ``"bucket_start"``（SQL 分桶表达式别名，B1：别名不再与
    真实列同名，避免 MySQL GROUP BY 解析回真实列导致桶内按天散行）。
    同 (name, date_key) 多行一律 Decimal 累加——即使上游分桶失效，
    桶内散行也求和而不是「后者覆盖前者」（桶聚合的第二道防线）。
    """
    days_with_rows = {_as_date(row[date_key]) for row in rows}
    totals = {}
    for row in rows:
        name = row[name_key] or ""
        by_day = totals.setdefault(name, {})
        key = _as_date(row[date_key])
        by_day[key] = by_day.get(key, Decimal(0)) + Decimal(row["total"] or 0)
    series = []
    status = []
    for name in sorted(totals):
        by_day = totals[name]
        data = []
        states = []
        for point in axis:
            value, state = _series_point(
                by_day, point, days_with_rows, workdays, as_of, bucket
            )
            data.append(value)
            states.append(state)
        series.append({"name": name, "data": data})
        status.append(states)
    return {
        "dates": [point.strftime(label_fmt) for point in axis],
        "series": series,
        "status": status,
    }


def _as_date(value):
    """DATE 列取值归一为 ``date``（驱动偶发返回 ``datetime``）。"""
    if isinstance(value, datetime):
        return value.date()
    return value


def _bucket_end(day, bucket):
    """桶覆盖区间终点：周=周日，月=月末，日=当天。"""
    if bucket == "week":
        return day + timedelta(days=6)
    if bucket == "month":
        return month_days(day.year, day.month)[-1]
    return day


def _series_point(by_day, point, days_with_rows, workdays, as_of, bucket):
    """单点三态判定（规则表见 :func:`_align_series_rows` docstring）。"""
    if as_of is None or point > as_of:
        return None, "missing"
    if point not in by_day:
        if point in days_with_rows:
            return 0.0, "missing"
        return None, "missing"
    total = by_day[point]
    if total is not None and Decimal(total) > 0:
        return float(total), "ok"
    covered_end = min(_bucket_end(point, bucket), as_of)
    if any(point <= workday <= covered_end for workday in workdays):
        return 0.0, "zero"
    return 0.0, "ok"


def _day_axis(first_day, last_day, anchor):
    """日档位轴：月首 → min(月末, 锚点日)（§4.6：月内日序列）。"""
    end = min(last_day, anchor)
    days = []
    point = first_day
    while point <= end:
        days.append(point)
        point += timedelta(days=1)
    return days


def _week_axis(anchor):
    """周档位轴：锚点所在自然周起向前 12 个周一（§4.6：month=窗口截止月）。"""
    last_monday = anchor - timedelta(days=anchor.weekday())
    return [
        last_monday - timedelta(days=7 * offset) for offset in range(11, -1, -1)
    ]


def _month_axis(first_day):
    """月档位轴：截止月（月初）起向前 12 个自然月，升序。"""
    year, month = first_day.year, first_day.month
    for _ in range(11):
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    months = []
    for _ in range(12):
        months.append(date(year, month, 1))
        year, month = (year, month + 1) if month < 12 else (year + 1, 1)
    return months


def _window_workdays(connection, first_day, last_day) -> set:
    """窗口工作日集合：跨月窗口逐月取 :func:`month_workdays` 的并集。"""
    workdays = set()
    year, month = first_day.year, first_day.month
    while (year, month) <= (last_day.year, last_day.month):
        cursor = date(year, month, 1)
        workdays |= month_workdays(connection, first_day=cursor, last_day=cursor)
        year, month = (year, month + 1) if month < 12 else (year + 1, 1)
    return workdays


def _daily_three_state(connection, rows, name_key, first_day, last_day,
                       *, anchor) -> dict:
    """日档位三态装配：锚点轴 + 营业日集合 + as_of 下发。

    ``anchor`` 由调用方给定——线下趋势卡传真实数据水位
    （:func:`_offline_trend_anchor`），渠道趋势卡传
    :func:`_shortfall_as_of`（当前月=今天，B2 批次不动渠道口径）；
    轴 = 月首→min(月末, 锚点)，锚点为 None（空表）时轴为空、
    as_of 为 None。
    """
    axis = _day_axis(first_day, last_day, anchor) if anchor is not None else []
    workdays = _window_workdays(connection, first_day, axis[-1] if axis else last_day)
    aligned = _align_series_rows(
        rows, name_key, axis=axis, workdays=workdays, as_of=anchor
    )
    aligned["as_of"] = anchor.isoformat() if anchor is not None else None
    return aligned


def _optional_region_filter(region):
    """可选 region 过滤：返回 (SQL 片段, 追加参数元组)；片段为空即全区域。"""
    if region:
        return "AND region = %s ", (region,)
    return "", ()


ROW_SCOPE_PARAM = "_scope"


def scope_from_params(params):
    """params → ``frozenset`` 或 ``None``（纯函数）。

    键缺失 → ``None``：不启用行级过滤（Bearer/旧部署，行为逐字节不变）。
    键存在 → frozenset（空串即空集）；形态损坏（非字符串）按空集
    fail-closed，绝不退化为「全区域可见」。
    """
    raw = params.get(ROW_SCOPE_PARAM)
    if raw is None:
        return None
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(part for part in raw.split(",") if part)


def scope_fingerprint(scope):
    """``frozenset``/``None`` → params 指纹串（``None`` → 不写键的哨兵）。"""
    if scope is None:
        return None
    return ",".join(sorted(scope))


def _carry_scope(params, target):
    """把 scope 指纹转抄进派生 params（锚定卡委派通用函数时用）。

    hz/sx/oa 卡把 ``{"region": 钉死值, "month": ...}`` 交给通用取数函数，
    若不转抄，scope 会在委派边界丢失 → 锚定卡反而绕过行级过滤。
    """
    raw = params.get(ROW_SCOPE_PARAM)
    if raw is not None:
        target[ROW_SCOPE_PARAM] = raw
    return target


def _scope_region_filter(scope, region=None):
    """行级 scope + 可选显式 region → ``(SQL 片段, 参数元组)``（纯函数）。

    * ``scope is None`` → 与 :func:`_optional_region_filter` 完全同形
      （旧调用路径零变化；片段为空时 SQL 仍无 ``%s``，StaticSqlTests
      锁定的静态语句不破）；
    * ``scope == frozenset()`` → ``AND 1 = 0``（空集铁律：一行不可见）；
    * 非空 → ``AND region IN (%s, ...)``，值一律参数化、按 sorted 稳定
      （绝不字符串插值，设计稿 §6）。

    片段只允许拼进**已参数化**（字面 ``%`` 双写）的 SQL：``%s`` 占位
    由 pymysql ``sql % params`` 消费，与既有 region 片段同款。
    """
    fragments, params = [], []
    if region:
        fragments.append("AND region = %s ")
        params.append(region)
    if scope is not None:
        if not scope:
            fragments.append("AND 1 = 0 ")
        else:
            placeholders = ", ".join(["%s"] * len(scope))
            fragments.append(f"AND region IN ({placeholders}) ")
            params.extend(sorted(scope))
    return "".join(fragments), tuple(params)


def _scope_allows_regions(scope, *regions):
    """scope 是否覆盖给定区域集合（供无法下推 SQL 的合并卡做 fail-closed 门）。

    ``scope is None``（未启用行级）→ True；否则要求 *regions* 全部在
    scope 内——合并采集（三区 merge 后已不可按区域拆分）**不能**只凭
    交集放行：少给一个区域的授权就会多暴露该区域的行。
    """
    if scope is None:
        return True
    return all(one in scope for one in regions)


def _append_region_sql(sql, fragment):
    """把 :func:`_scope_region_filter` 的片段追加到**已参数化** SQL 末尾。

    片段为空（scope=None 且无显式 region）→ 原样返回，静态语句/绑定参数
    逐字节不变；否则追加成新的 ``AND`` 谓词（片段一律 %s 占位）。
    """
    stripped = fragment.strip()
    return sql if not stripped else f"{sql}\n    {stripped}"


def _dod_totals(connection, sql, latest, scope=None):
    """近 7 自然日窗口的按日 Σsales 映射（缺数日不在映射中）。

    行级 scope（M3）：*sql* 必须是已参数化语句（字面 ``%`` 双写），
    scope 非 None 时在末尾追加参数化 ``region IN`` 片段。
    """
    first_day = latest - timedelta(days=6)
    fragment, scope_params = _scope_region_filter(scope)
    rows = _fetch_rows(
        connection, _append_region_sql(sql, fragment), (first_day, latest) + scope_params
    )
    return {row["business_date"]: row["total"] for row in rows}


def _dod_core(totals, latest):
    """由按日 Σsales 映射组装环比核：value/prev/delta_pct/trend7。"""
    def _num(value):
        return None if value is None else float(value)

    value = _num(totals.get(latest))
    prev = _num(totals.get(latest - timedelta(days=1)))
    delta_pct = None
    if prev not in (None, 0) and value is not None:
        delta_pct = (value - prev) / prev
    trend7 = [
        {
            "date": (latest - timedelta(days=offset)).isoformat(),
            "value": _num(totals.get(latest - timedelta(days=offset))),
        }
        for offset in range(6, -1, -1)
    ]
    return {
        "value": value,
        "date": latest.isoformat(),
        "prev": prev,
        "delta_pct": delta_pct,
        "trend7": trend7,
    }


def _run_dod(core):
    """⑭ 环比卡载荷：空表给出全空核（value 0，其余 None，trend7 空）。"""
    payload = {
        "chart": "scalar",
        "value": 0.0,
        "date": None,
        "prev": None,
        "delta_pct": None,
        "trend7": [],
        "unit": _UNIT,
    }
    if core is not None:
        payload.update(core)
    return payload


def month_workdays(connection, *, first_day, last_day) -> set:
    """当月工作日集合（``dim_calendar.is_workday=1``，日报机器人同口径）。"""
    return fetch_workdays(connection, year=first_day.year, month=first_day.month)


def _shortfall_as_of(first_day, last_day):
    """锚点日：当前月=今天（含当天），历史月=月末（``_people_as_of`` 同款）。"""
    today = datetime.now().date()
    if first_day <= today <= last_day:
        return today
    return last_day


def _qd_jsonable(value):
    """行值归一为 JSON 可编码类型：Decimal→float、date/datetime→ISO 串。

    旧页 fetch_* 多处直接回传 pymysql 原始行（库存快照等含 DECIMAL，日期
    列含 date/datetime），而 app 层的 JSONResponse 编码失败会落到设计好的
    500 card_error（2026-10-13 实测 table_qd_stock_urgent/oversold）。这里
    统一在载荷出口归一，None 原样透传（不静默补 0）。
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value

__all__ = tuple(name for name in globals() if not name.startswith("__"))
