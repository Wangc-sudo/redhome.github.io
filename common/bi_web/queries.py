"""Read-only chart queries for the L1 cockpit (plan Task 3).

This is the code-owned half of the ``SQL lives in code, layout lives in
Nacos`` split, so every口径 is reviewable and unit-testable:

* every offline ``Σ sales_amount`` excludes the 合计 summary rows with
  ``responsible_person NOT LIKE '%合计%'`` (the table carries per-region
  合计 rows alongside the person rows -- summing without the filter
  doubles every figure);
* every fact query truncates the pre-filled future rows with
  ``business_date <= CURDATE()``;
* the channel fact table has no 合计 rows and no region column, so its
  queries only truncate;
* the annual-progress denominator is the two-line scope of ``dim_target``
  (offline + channel; the restaurant target has no fact table and is
  deliberately excluded -- the seed note records it).

Stage-A SQL is fully static; stage B adds parameterized queries (the
month / region / channel windowed lookups) that bind values as ``%s``
placeholders with a params tuple -- a value is never spliced into the SQL
text, and URL values are validated against the dimension tables upstream
in the app layer.  Money unit is 元.  The low-level queries return
:class:`~decimal.Decimal`; the ``run_*`` card functions convert to
``float`` for JSON payloads.
"""

import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal

from common.calendar_utils import month_days
from common.daily_robot.mart_leaderboard import mart_collect
from common.daily_robot.mart_tasks import MartTaskError

#: Money unit for every chart payload (元; the front end formats 万).
_UNIT = "元"

_OFFLINE_MTD_SQL = (
    "SELECT COALESCE(SUM(sales_amount), 0) "
    "FROM fact_daily_report_offline "
    "WHERE business_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01') "
    "AND business_date <= CURDATE() "
    "AND responsible_person NOT LIKE '%合计%'"
)

_OFFLINE_ANNUAL_SQL = (
    "SELECT COALESCE(SUM(sales_amount), 0) "
    "FROM fact_daily_report_offline "
    "WHERE business_date >= MAKEDATE(YEAR(CURDATE()), 1) "
    "AND business_date <= CURDATE() "
    "AND responsible_person NOT LIKE '%合计%'"
)

_CHANNEL_MTD_SQL = (
    "SELECT COALESCE(SUM(sales_amount), 0) "
    "FROM fact_channel_daily_sales "
    "WHERE business_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01') "
    "AND business_date <= CURDATE()"
)

_CHANNEL_ANNUAL_SQL = (
    "SELECT COALESCE(SUM(sales_amount), 0) "
    "FROM fact_channel_daily_sales "
    "WHERE business_date >= MAKEDATE(YEAR(CURDATE()), 1) "
    "AND business_date <= CURDATE()"
)

_ANNUAL_TARGET_SQL = (
    "SELECT COALESCE(SUM(annual_target), 0) "
    "FROM dim_target "
    "WHERE scope = 'line' "
    "AND scope_key IN ('offline', 'channel') "
    "AND year = YEAR(CURDATE())"
)

_REGION_DAILY_SQL = (
    "SELECT business_date, region, SUM(sales_amount) AS total "
    "FROM fact_daily_report_offline "
    "WHERE business_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01') "
    "AND business_date <= CURDATE() "
    "AND responsible_person NOT LIKE '%合计%' "
    "GROUP BY business_date, region"
)

_CHANNEL_RANKING_SQL = (
    "SELECT channel, SUM(sales_amount) AS total "
    "FROM fact_channel_daily_sales "
    "WHERE business_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01') "
    "AND business_date <= CURDATE() "
    "GROUP BY channel "
    "ORDER BY total DESC"
)

_REGION_OPTIONS_SQL = (
    "SELECT DISTINCT region FROM fact_daily_report_offline ORDER BY region"
)

_CHANNEL_OPTIONS_SQL = (
    "SELECT DISTINCT channel FROM fact_channel_daily_sales ORDER BY channel"
)

_MONTH_OPTIONS_SQL = (
    "SELECT DISTINCT DATE_FORMAT(business_date, '%Y-%m') AS month "
    "FROM fact_daily_report_offline "
    "UNION "
    "SELECT DISTINCT DATE_FORMAT(business_date, '%Y-%m') "
    "FROM fact_channel_daily_sales "
    "UNION "
    "SELECT DATE_FORMAT(CURDATE(), '%Y-%m') "
    "ORDER BY 1 DESC"
)


def region_options(connection) -> list:
    """筛选器「区域」下拉：DISTINCT region，升序（SQL 全静态）。"""
    return [row["region"] for row in _fetch_rows(connection, _REGION_OPTIONS_SQL)
            if row["region"]]


def channel_options(connection) -> list:
    """筛选器「渠道」下拉：DISTINCT channel，升序（SQL 全静态）。"""
    return [row["channel"] for row in _fetch_rows(connection, _CHANNEL_OPTIONS_SQL)
            if row["channel"]]


def month_options(connection) -> list:
    """筛选器「月份」下拉：两事实表 DISTINCT 月 ∪ 当前月，降序。

    只出现有数据的月份（数据积累后自动出新选项）；当前月恒在列，
    无数据也允许选择（空结果而非 400，spec §8）。

    business_date 为 NULL 的行（事实表允许 NULL）产出 None 月，须过滤，
    与 region_options/channel_options 的空值处理一致。
    """
    return [
        row["month"]
        for row in _fetch_rows(connection, _MONTH_OPTIONS_SQL)
        if row["month"]
    ]


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


def offline_mtd_total(connection) -> Decimal:
    """线下本月累计销售 (①): month-to-date offline sales, 合计 rows excluded."""
    return _fetch_scalar(connection, _OFFLINE_MTD_SQL)


def offline_annual_total(connection) -> Decimal:
    """线下年累计: year-to-date offline sales, same 合计 exclusion."""
    return _fetch_scalar(connection, _OFFLINE_ANNUAL_SQL)


def channel_mtd_total(connection) -> Decimal:
    """电商渠道本月累计销售 (③): month-to-date channel sales."""
    return _fetch_scalar(connection, _CHANNEL_MTD_SQL)


def channel_annual_total(connection) -> Decimal:
    """电商渠道年累计: year-to-date channel sales."""
    return _fetch_scalar(connection, _CHANNEL_ANNUAL_SQL)


def annual_target_total(connection) -> Decimal:
    """年度目标分母: the offline + channel two-line target for this year.

    Only ``scope = 'line'`` rows with ``scope_key`` offline/channel are
    summed (760,210,000 元量级 with the repository seed); the restaurant
    target has no fact table and is deliberately not counted.
    """
    return _fetch_scalar(connection, _ANNUAL_TARGET_SQL)


def _align_series_rows(rows, name_key) -> dict:
    """按升序日期轴对齐各名称序列并零填充（区域/渠道趋势共用核心）。"""
    dates = sorted({row["business_date"] for row in rows})
    index_of = {business_date: index for index, business_date in enumerate(dates)}
    by_name = {}
    for row in rows:
        name = row[name_key] or ""
        index = index_of[row["business_date"]]
        by_name.setdefault(name, [Decimal(0)] * len(dates))[index] = Decimal(
            row["total"] or 0
        )
    return {
        "dates": [_mmdd(business_date) for business_date in dates],
        "series": [
            {"name": name, "data": by_name[name]} for name in sorted(by_name)
        ],
    }


def region_daily_series(connection) -> dict:
    return _align_series_rows(_fetch_rows(connection, _REGION_DAILY_SQL), "region")


def channel_mtd_ranking(connection) -> dict:
    """Channel totals for the current month, descending (SQL ORDER BY)."""
    cursor = connection.cursor()
    try:
        cursor.execute(_CHANNEL_RANKING_SQL)
        rows = cursor.fetchall()
    finally:
        cursor.close()
    return {
        "categories": [row["channel"] or "" for row in rows],
        "values": [Decimal(row["total"] or 0) for row in rows],
    }


def _optional_region_filter(region):
    """可选 region 过滤：返回 (SQL 片段, 追加参数元组)；片段为空即全区域。"""
    if region:
        return "AND region = %s ", (region,)
    return "", ()


def region_mtd_total(connection, *, region=None, first_day, last_day) -> Decimal:
    """选中区域（缺省全区域）某自然月 Σsales（截断未来 + 排除合计行）。

    带参 SQL 的字面 ``%`` 双写（pymysql ``sql % params`` 约定）：
    ``'%%合计%%'`` 格式化后 MySQL 收到 ``'%合计%'``。
    """
    region_sql, region_params = _optional_region_filter(region)
    sql = (
        "SELECT COALESCE(SUM(sales_amount), 0) "
        "FROM fact_daily_report_offline "
        "WHERE business_date BETWEEN %s AND %s "
        f"{region_sql}"
        "AND business_date <= CURDATE() "
        "AND responsible_person NOT LIKE '%%合计%%'"
    )
    return _fetch_scalar(connection, sql, (first_day, last_day) + region_params)


def region_month_target(connection, *, region=None, first_day, last_day) -> Decimal:
    """区域月目标 = Σ各人员 MAX(monthly_target)（melt 陷阱：绝不跨行 SUM）。

    与 ``summarize_people`` 同口径取**全月行** MAX：月目标是月级常量，
    预填未来行不影响 MAX，故不做 CURDATE 截断（与取数查询刻意不对称）。
    """
    region_sql, region_params = _optional_region_filter(region)
    sql = (
        "SELECT COALESCE(SUM(mx), 0) "
        "FROM ("
        "SELECT responsible_person, MAX(monthly_target) AS mx "
        "FROM fact_daily_report_offline "
        "WHERE business_date BETWEEN %s AND %s "
        f"{region_sql}"
        "AND responsible_person NOT LIKE '%%合计%%' "
        "GROUP BY responsible_person"
        ") t"
    )
    return _fetch_scalar(connection, sql, (first_day, last_day) + region_params)


def department_mtd_ranking(connection, *, region=None, first_day, last_day) -> dict:
    """选中区域（缺省全区域）某自然月按部门 Σsales 降序；空部门→未分组。"""
    region_sql, region_params = _optional_region_filter(region)
    sql = (
        "SELECT department, SUM(sales_amount) AS total "
        "FROM fact_daily_report_offline "
        "WHERE business_date BETWEEN %s AND %s "
        f"{region_sql}"
        "AND business_date <= CURDATE() "
        "AND responsible_person NOT LIKE '%%合计%%' "
        "GROUP BY department "
        "ORDER BY total DESC"
    )
    rows = _fetch_rows(connection, sql, (first_day, last_day) + region_params)
    return {
        "categories": [row["department"] or "未分组" for row in rows],
        "values": [Decimal(row["total"] or 0) for row in rows],
    }


def region_month_daily_series(connection, *, region=None, first_day, last_day) -> dict:
    """参数化的区域日销序列（L2 用）；有 region 单系列、无则全区域系列。

    L1 无参路径仍走静态 ``region_daily_series``（StaticSqlTests 锁定）。
    """
    region_sql, region_params = _optional_region_filter(region)
    sql = (
        "SELECT business_date, region, SUM(sales_amount) AS total "
        "FROM fact_daily_report_offline "
        "WHERE business_date BETWEEN %s AND %s "
        f"{region_sql}"
        "AND business_date <= CURDATE() "
        "AND responsible_person NOT LIKE '%%合计%%' "
        "GROUP BY business_date, region"
    )
    rows = _fetch_rows(connection, sql, (first_day, last_day) + region_params)
    return _align_series_rows(rows, "region")


_OFFLINE_DOD_LATEST_SQL = (
    "SELECT MAX(business_date) AS d "
    "FROM fact_daily_report_offline "
    "WHERE business_date <= CURDATE() "
    "AND responsible_person NOT LIKE '%合计%'"
)

_CHANNEL_DOD_LATEST_SQL = (
    "SELECT MAX(business_date) AS d "
    "FROM fact_channel_daily_sales "
    "WHERE business_date <= CURDATE()"
)

_OFFLINE_DOD_WINDOW_SQL = (
    "SELECT business_date, SUM(sales_amount) AS total "
    "FROM fact_daily_report_offline "
    "WHERE business_date BETWEEN %s AND %s "
    "AND responsible_person NOT LIKE '%%合计%%' "
    "GROUP BY business_date"
)

_CHANNEL_DOD_WINDOW_SQL = (
    "SELECT business_date, SUM(sales_amount) AS total "
    "FROM fact_channel_daily_sales "
    "WHERE business_date BETWEEN %s AND %s "
    "GROUP BY business_date"
)


def _dod_totals(connection, sql, latest):
    """近 7 自然日窗口的按日 Σsales 映射（缺数日不在映射中）。"""
    first_day = latest - timedelta(days=6)
    rows = _fetch_rows(connection, sql, (first_day, latest))
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


def offline_dod(connection):
    """⑭ 线下日环比核：最新数据日 Σsales、前一自然日、近 7 自然日。"""
    latest = _fetch_one(connection, _OFFLINE_DOD_LATEST_SQL)
    if latest is None:
        return None
    return _dod_core(_dod_totals(connection, _OFFLINE_DOD_WINDOW_SQL, latest), latest)


def channel_dod(connection):
    """⑭ 电商日环比核：同口径，源 fact_channel_daily_sales（无合计行）。"""
    latest = _fetch_one(connection, _CHANNEL_DOD_LATEST_SQL)
    if latest is None:
        return None
    return _dod_core(_dod_totals(connection, _CHANNEL_DOD_WINDOW_SQL, latest), latest)


_CHANNEL_MONTH_RANKING_SQL = (
    "SELECT channel, SUM(sales_amount) AS total "
    "FROM fact_channel_daily_sales "
    "WHERE business_date BETWEEN %s AND %s "
    "AND business_date <= CURDATE() "
    "GROUP BY channel "
    "ORDER BY total DESC"
)

_CHANNEL_MONTH_DAILY_SQL = (
    "SELECT business_date, channel, SUM(sales_amount) AS total "
    "FROM fact_channel_daily_sales "
    "WHERE business_date BETWEEN %s AND %s "
    "AND business_date <= CURDATE() "
    "GROUP BY business_date, channel"
)

_CHANNEL_MTD_COMPARISON_SQL = (
    "SELECT channel, "
    "SUM(sales_amount) AS sales, "
    "SUM(promotion_cost) AS promo, "
    "COUNT(DISTINCT store_name) AS stores "
    "FROM fact_channel_daily_sales "
    "WHERE business_date BETWEEN %s AND %s "
    "AND business_date <= CURDATE() "
    "GROUP BY channel "
    "ORDER BY sales DESC"
)


def _optional_channel_filter(channel):
    """可选 channel 过滤：返回 (SQL 片段, 追加参数元组)；空即全渠道。"""
    if channel:
        return "AND channel = %s ", (channel,)
    return "", ()


def channel_month_ranking(connection, *, first_day, last_day) -> dict:
    """指定月渠道 Σsales 降序排行（参数化版 channel_mtd_ranking）。"""
    rows = _fetch_rows(connection, _CHANNEL_MONTH_RANKING_SQL, (first_day, last_day))
    return {
        "categories": [row["channel"] or "" for row in rows],
        "values": [Decimal(row["total"] or 0) for row in rows],
    }


def channel_month_daily_series(connection, *, first_day, last_day) -> dict:
    """指定月按日 Σsales 分渠道序列（渠道名升序，零填充）。"""
    rows = _fetch_rows(connection, _CHANNEL_MONTH_DAILY_SQL, (first_day, last_day))
    return _align_series_rows(rows, "channel")


def channel_mtd_comparison(connection, *, first_day, last_day) -> list:
    """渠道对比行：Σsales、Σpromo（组内全空→NULL）、店铺数（DISTINCT）。"""
    return _fetch_rows(connection, _CHANNEL_MTD_COMPARISON_SQL, (first_day, last_day))


def store_mtd_ranking(connection, *, channel=None, first_day, last_day) -> dict:
    """店铺当月排行：Σsales 降序、服务端名次；无 channel 含渠道列。"""
    channel_sql, channel_params = _optional_channel_filter(channel)
    sql = (
        "SELECT store_name, channel, SUM(sales_amount) AS total "
        "FROM fact_channel_daily_sales "
        "WHERE business_date BETWEEN %s AND %s "
        f"{channel_sql}"
        "AND business_date <= CURDATE() "
        "GROUP BY store_name, channel "
        "ORDER BY total DESC"
    )
    rows = _fetch_rows(connection, sql, (first_day, last_day) + channel_params)
    columns = [{"key": "rank", "title": "排名"}, {"key": "store", "title": "店铺"}]
    if channel is None:
        columns.append({"key": "channel", "title": "渠道"})
    columns.append({"key": "sales", "title": "本月销售额", "format": "wan"})
    table_rows = []
    for index, row in enumerate(rows, start=1):
        entry = {"rank": index, "store": row["store_name"]}
        if channel is None:
            entry["channel"] = row["channel"]
        entry["sales"] = float(Decimal(row["total"] or 0))
        table_rows.append(entry)
    return {"columns": columns, "rows": table_rows}


_PEOPLE_REGIONS = ("杭州", "绍兴")
_PEOPLE_CACHE_TTL_SECONDS = 60.0
_PEOPLE_CACHE = {}
_PEOPLE_CACHE_LOCK = threading.Lock()


def _people_params(params):
    """run 层共用解析：region（None=杭州+绍兴两榜合并）、month（缺省当前月）。"""
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    return params.get("region"), month


def _people_as_of(month):
    """月参数 → mart_collect 锚点日：当前月=今天，历史月=月末。

    elapsed 以锚点截断（include_today=True 含当天），历史月取月末即
    全月工作日；未来月不在 month_options（数据月 ∪ 当前月），不设护栏。
    """
    first_day, last_day = month_bounds(month)
    today = datetime.now().date()
    if (first_day.year, first_day.month) == (today.year, today.month):
        return today
    return last_day


def _people_for_region(connection, region, month):
    """单区域人员行；dim_calendar 缺行（MartTaskError）降级为空列表。"""
    try:
        data = mart_collect(
            connection,
            region=region,
            business_date=_people_as_of(month),
            include_today=True,
        )
    except MartTaskError:
        return []
    return list(data.people)


def _people_snapshot(connection, region, month):
    """l2-people 四卡共享的人员快照（模块级 60s 缓存，带锁）。

    页面一次加载的四张卡（三 scalar + 一 table）只触发一轮采集查询；
    region=None 时杭州+绍兴两榜合并后按 ``(-rate(None→-1), -completed,
    -target)`` 重排（排序键与 mart_collect 逐字相同），单区域沿用
    mart_collect 自带排序。

    锁覆盖检查与采集全程（与 app._TTLGate 同款语义）：同键并发首查
    只发起一轮采集，后到者等锁后直接命中缓存。
    """
    key = (region, month)
    with _PEOPLE_CACHE_LOCK:
        cached = _PEOPLE_CACHE.get(key)
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1]
        if region is None:
            people = []
            for one_region in _PEOPLE_REGIONS:
                people.extend(
                    _people_for_region(connection, one_region, month)
                )
            people.sort(
                key=lambda person: (
                    -(person["rate"] if person["rate"] is not None else -1),
                    -person["completed"],
                    -person["target"],
                )
            )
        else:
            people = _people_for_region(connection, region, month)
        _PEOPLE_CACHE[key] = (
            time.monotonic() + _PEOPLE_CACHE_TTL_SECONDS,
            people,
        )
        return people


# ---------------------------------------------------------------------------
# Card run functions: (connection, params) -> chart-ready dict.
#
# L1 cards take no URL parameters, so *params* is accepted for the Card.run
# contract and ignored; stage-B l2 cards (region/channel/month drilldowns)
# read their parameters from it.  The whitelist check lives in the app
# layer against Card.params_schema.
# ---------------------------------------------------------------------------


def run_kpi_offline_mtd(connection, params) -> dict:
    """①线下本月累计销售: scalar KPI, 元."""
    return {
        "chart": "scalar",
        "value": float(offline_mtd_total(connection)),
        "unit": _UNIT,
    }


def run_kpi_channel_mtd(connection, params) -> dict:
    """③电商渠道本月累计销售: scalar KPI, 元."""
    return {
        "chart": "scalar",
        "value": float(channel_mtd_total(connection)),
        "unit": _UNIT,
    }


def run_kpi_annual_progress(connection, params) -> dict:
    """⑫年度目标达成进度: two-line annual actuals over the two-line target.

    分子 = 线下 + 电商两线年累计 (same 合计 exclusion and future-row
    truncation); 分母 = ``dim_target`` 的 offline + channel 两行
    ``annual_target`` 之和; ``rate = value / target`` (``None`` when the
    target is 0, computed at float precision for JSON).
    """
    value = offline_annual_total(connection) + channel_annual_total(connection)
    target = annual_target_total(connection)
    rate = None if target == 0 else float(value) / float(target)
    return {
        "chart": "scalar",
        "value": float(value),
        "target": float(target),
        "rate": rate,
        "unit": _UNIT,
    }


def run_trend_region_daily(connection, params) -> dict:
    """区域日销趋势: L1 无参=当前月全区域（静态）；带参=region/month。"""
    region = params.get("region")
    month = params.get("month")
    if region is None and month is None:
        series = region_daily_series(connection)
    else:
        month = month or datetime.now().strftime("%Y-%m")
        first_day, last_day = month_bounds(month)
        series = region_month_daily_series(
            connection, region=region, first_day=first_day, last_day=last_day
        )
    return {
        "chart": "line",
        "dates": series["dates"],
        "series": [
            {"name": entry["name"], "data": [float(value) for value in entry["data"]]}
            for entry in series["series"]
        ],
    }


def run_bar_channel_mtd(connection, params) -> dict:
    """渠道当月排行: L1 无参=当前月（静态）；l2-channel 带 month 参数化。"""
    month = params.get("month")
    if month is None:
        ranking = channel_mtd_ranking(connection)
    else:
        first_day, last_day = month_bounds(month)
        ranking = channel_month_ranking(
            connection, first_day=first_day, last_day=last_day
        )
    return {
        "chart": "bar",
        "categories": ranking["categories"],
        "values": [float(value) for value in ranking["values"]],
        "unit": _UNIT,
    }


def run_kpi_region_mtd(connection, params) -> dict:
    """区域当月累计（模块 ① 区域下钻）: region/month 参数化 scalar + 进度条。

    value=当月 Σsales（截断+排合计）；target=Σ各人员 MAX(monthly_target)
    （melt 安全）；rate=value/target（target 0→None）。region 缺省全区域，
    month 缺省当前月（应用本地时钟；容器 TZ Asia/Shanghai）。
    """
    region = params.get("region")
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    first_day, last_day = month_bounds(month)
    value = region_mtd_total(
        connection, region=region, first_day=first_day, last_day=last_day
    )
    target = region_month_target(
        connection, region=region, first_day=first_day, last_day=last_day
    )
    rate = None if target == 0 else float(value) / float(target)
    return {
        "chart": "scalar",
        "value": float(value),
        "target": float(target),
        "rate": rate,
        "unit": _UNIT,
    }


def run_bar_department_mtd(connection, params) -> dict:
    """区域部门当月排行（模块 ① 区域下钻）: region/month bar 降序。"""
    region = params.get("region")
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    first_day, last_day = month_bounds(month)
    ranking = department_mtd_ranking(
        connection, region=region, first_day=first_day, last_day=last_day
    )
    return {
        "chart": "bar",
        "categories": ranking["categories"],
        "values": [float(value) for value in ranking["values"]],
        "unit": _UNIT,
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


def run_kpi_offline_dod(connection, params):
    """⑭ 线下日环比卡（L1，无参）。"""
    return _run_dod(offline_dod(connection))


def run_kpi_channel_dod(connection, params):
    """⑭ 电商日环比卡（L1，无参）。"""
    return _run_dod(channel_dod(connection))


def run_trend_channel_daily(connection, params) -> dict:
    """电商渠道日销趋势（l2-channel）: month 缺省当前月，分渠道多系列。"""
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    first_day, last_day = month_bounds(month)
    series = channel_month_daily_series(
        connection, first_day=first_day, last_day=last_day
    )
    return {
        "chart": "line",
        "dates": series["dates"],
        "series": [
            {"name": entry["name"], "data": [float(value) for value in entry["data"]]}
            for entry in series["series"]
        ],
    }


def run_table_channel_mtd(connection, params) -> dict:
    """渠道对比表（模块 ② 渠道×店铺子集）: Σsales/Σpromo/ROI/店铺数。

    ROI=Σsales÷Σpromo 在此层计算（绝不取行级 roi 源列）；promo 全空
    → NULL → 前端「—」；promo 为 0 → ROI None（除零护栏）。
    """
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    first_day, last_day = month_bounds(month)
    rows = channel_mtd_comparison(connection, first_day=first_day, last_day=last_day)
    table_rows = []
    for row in rows:
        sales = float(row["sales"] or 0)
        promo = None if row["promo"] is None else float(row["promo"])
        roi = None if promo in (None, 0) else sales / promo
        table_rows.append(
            {
                "channel": row["channel"],
                "sales": sales,
                "promo": promo,
                "roi": roi,
                "stores": int(row["stores"] or 0),
            }
        )
    return {
        "chart": "table",
        "columns": [
            {"key": "channel", "title": "渠道"},
            {"key": "sales", "title": "本月销售额", "format": "wan"},
            {"key": "promo", "title": "推广费", "format": "wan"},
            {"key": "roi", "title": "ROI", "format": "ratio"},
            {"key": "stores", "title": "店铺数"},
        ],
        "rows": table_rows,
    }


def run_table_store_mtd(connection, params) -> dict:
    """店铺排行表（模块 ② 渠道×店铺子集）: channel/month；无 channel 含渠道列。"""
    channel = params.get("channel")
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    first_day, last_day = month_bounds(month)
    table = store_mtd_ranking(
        connection, channel=channel, first_day=first_day, last_day=last_day
    )
    return {"chart": "table", "columns": table["columns"], "rows": table["rows"]}


def run_kpi_people_count(connection, params) -> dict:
    """参与人数: 榜上人员数（unit=人，前端不做万换算）。"""
    region, month = _people_params(params)
    people = _people_snapshot(connection, region, month)
    return {"chart": "scalar", "value": float(len(people)), "unit": "人"}


def run_kpi_people_completed(connection, params) -> dict:
    """Σ完成额: 已过工作日 sales_amount 合计（元）。"""
    region, month = _people_params(params)
    people = _people_snapshot(connection, region, month)
    return {
        "chart": "scalar",
        "value": float(sum(person["completed"] for person in people)),
        "unit": _UNIT,
    }


def run_kpi_people_rate(connection, params) -> dict:
    """总达成率: Σcompleted÷Σtarget（非个人率平均；Σtarget=0 → None）。"""
    region, month = _people_params(params)
    people = _people_snapshot(connection, region, month)
    completed = sum(person["completed"] for person in people)
    target = sum(person["target"] for person in people)
    rate = None if target == 0 else completed / target
    return {
        "chart": "scalar",
        "value": float(completed),
        "target": float(target),
        "rate": rate,
        "unit": _UNIT,
    }


def run_table_people_leaderboard(connection, params) -> dict:
    """人员榜: 服务端名次；未完成缺口=未填工作日数（spec §3.4）。"""
    region, month = _people_params(params)
    people = _people_snapshot(connection, region, month)
    rows = [
        {
            "rank": index,
            "name": person["name"],
            "dept": person["dept"],
            "completed": float(person["completed"]),
            "target": float(person["target"]),
            "rate": person["rate"],
            "unfilled": person["unfilled"],
        }
        for index, person in enumerate(people, start=1)
    ]
    return {
        "chart": "table",
        "columns": [
            {"key": "rank", "title": "排名"},
            {"key": "name", "title": "姓名"},
            {"key": "dept", "title": "部门"},
            {"key": "completed", "title": "完成额", "format": "wan"},
            {"key": "target", "title": "月目标", "format": "wan"},
            {"key": "rate", "title": "达成率", "format": "percent"},
            {"key": "unfilled", "title": "未完成缺口"},
        ],
        "rows": rows,
    }
