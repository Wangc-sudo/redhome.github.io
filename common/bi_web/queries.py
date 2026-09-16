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

import textwrap
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal

from common.bi_web import derived
from common.calendar_utils import month_days
from common.daily_robot.mart_leaderboard import mart_collect
from common.daily_robot.mart_tasks import MartTaskError
from common.metrics.daily_report import fetch_workdays

#: Money unit for every chart payload (元; the front end formats 万).
_UNIT = "元"

_OFFLINE_MTD_SQL = textwrap.dedent(
    """
    SELECT COALESCE(SUM(sales_amount), 0)
    FROM fact_daily_report_offline
    WHERE business_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01')
    AND business_date <= CURDATE()
    AND responsible_person NOT LIKE '%合计%'
    """
).strip()

_OFFLINE_ANNUAL_SQL = textwrap.dedent(
    """
    SELECT COALESCE(SUM(sales_amount), 0)
    FROM fact_daily_report_offline
    WHERE business_date >= MAKEDATE(YEAR(CURDATE()), 1)
    AND business_date <= CURDATE()
    AND responsible_person NOT LIKE '%合计%'
    """
).strip()

_CHANNEL_MTD_SQL = textwrap.dedent(
    """
    SELECT COALESCE(SUM(sales_amount), 0)
    FROM fact_channel_daily_sales
    WHERE business_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01')
    AND business_date <= CURDATE()
    """
).strip()

_CHANNEL_ANNUAL_SQL = textwrap.dedent(
    """
    SELECT COALESCE(SUM(sales_amount), 0)
    FROM fact_channel_daily_sales
    WHERE business_date >= MAKEDATE(YEAR(CURDATE()), 1)
    AND business_date <= CURDATE()
    """
).strip()

_ANNUAL_TARGET_SQL = textwrap.dedent(
    """
    SELECT COALESCE(SUM(annual_target), 0)
    FROM dim_target
    WHERE scope = 'line'
    AND scope_key IN ('offline', 'channel')
    AND year = YEAR(CURDATE())
    """
).strip()

_REGION_DAILY_SQL = textwrap.dedent(
    """
    SELECT business_date, region, SUM(sales_amount) AS total
    FROM fact_daily_report_offline
    WHERE business_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01')
    AND business_date <= CURDATE()
    AND responsible_person NOT LIKE '%合计%'
    GROUP BY business_date, region
    """
).strip()

_CHANNEL_RANKING_SQL = textwrap.dedent(
    """
    SELECT channel, SUM(sales_amount) AS total
    FROM fact_channel_daily_sales
    WHERE business_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01')
    AND business_date <= CURDATE()
    GROUP BY channel
    ORDER BY total DESC
    """
).strip()

_SKU_MTD_SQL = textwrap.dedent(
    """
    SELECT spec_no, goods_name, SUM(paid_amount) AS total
    FROM fact_order_line
    WHERE trade_time >= DATE_FORMAT(CURDATE(), '%Y-%m-01')
    AND trade_time < DATE_ADD(CURDATE(), INTERVAL 1 DAY)
    GROUP BY spec_no, goods_name
    ORDER BY total DESC, spec_no
    """
).strip()

_REGION_OPTIONS_SQL = (
    "SELECT DISTINCT region FROM fact_daily_report_offline ORDER BY region"
)

_CHANNEL_OPTIONS_SQL = (
    "SELECT DISTINCT channel FROM fact_channel_daily_sales ORDER BY channel"
)

_MONTH_OPTIONS_SQL = textwrap.dedent(
    """
    SELECT DISTINCT DATE_FORMAT(business_date, '%Y-%m') AS month
    FROM fact_daily_report_offline
    UNION
    SELECT DISTINCT DATE_FORMAT(business_date, '%Y-%m')
    FROM fact_channel_daily_sales
    UNION
    SELECT DATE_FORMAT(CURDATE(), '%Y-%m')
    ORDER BY 1 DESC
    """
).strip()

# 商品动销（l2-product）：fact_order_line 的品牌/渠道下拉。渠道为投影时
# 归一化的店铺渠道（channel_name），与 fact_channel_daily_sales.channel
# 是两套维度，故筛选 source 分开（brands / sku_channels）。
_BRAND_OPTIONS_SQL = (
    "SELECT DISTINCT brand_name FROM fact_order_line ORDER BY brand_name"
)

_SKU_CHANNEL_OPTIONS_SQL = (
    "SELECT DISTINCT channel_name FROM fact_order_line ORDER BY channel_name"
)

# 月内最新成交日（数据截至）；未来预填行以 CURDATE()+1 截断，与既有
# 事实表查询同口径。
_SKU_LATEST_DATE_SQL = textwrap.dedent(
    """
    SELECT MAX(DATE(trade_time)) AS d
    FROM fact_order_line
    WHERE trade_time >= %s AND trade_time < %s
    AND trade_time < DATE_ADD(CURDATE(), INTERVAL 1 DAY)
    """
).strip()

# 商品动销核：一个窗口内同时给出月累计、数据日与前一日销售额。
# ``{group}`` 由调用方注入固定列串（总量/分品牌/分渠道三种分组），
# 值一律走 %s 绑定；日窗口独立于月窗口，跨月的前一日也能取到。
_SKU_ROLLUP_SQL = textwrap.dedent(
    """
    SELECT {group}
    SUM(CASE WHEN trade_time >= %s AND trade_time < %s
             THEN paid_amount ELSE 0 END) AS mtd_amount,
    SUM(CASE WHEN trade_time >= %s AND trade_time < %s
             THEN quantity ELSE 0 END) AS mtd_qty,
    SUM(CASE WHEN trade_time >= %s AND trade_time < %s
             THEN paid_amount ELSE 0 END) AS latest_amount,
    SUM(CASE WHEN trade_time >= %s AND trade_time < %s
             THEN paid_amount ELSE 0 END) AS prev_amount
    FROM fact_order_line
    WHERE trade_time >= %s AND trade_time < %s
    {filters}
    GROUP BY {group_only}
    ORDER BY mtd_amount DESC, spec_no
    """
).strip()

# 总榜也把 brand_name 放进 SELECT/GROUP BY：品牌来自商品镜像，同一
# spec_no 的品牌唯一，带上它不影响分组粒度，却能让总榜直接显示品牌。
_SKU_GROUP_TOTAL = ("brand_name, spec_no, goods_name,", "brand_name, spec_no, goods_name")
_SKU_GROUP_BRAND = _SKU_GROUP_TOTAL
_SKU_GROUP_CHANNEL = (
    "channel_name, spec_no, goods_name,",
    "channel_name, spec_no, goods_name",
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


def brand_options(connection) -> list:
    """筛选器「品牌」下拉：fact_order_line DISTINCT brand_name，升序。"""
    return [row["brand_name"] for row in _fetch_rows(connection, _BRAND_OPTIONS_SQL)
            if row["brand_name"]]


def sku_channel_options(connection) -> list:
    """筛选器「渠道」下拉（商品口径）：DISTINCT channel_name，升序。"""
    return [row["channel_name"] for row in _fetch_rows(connection, _SKU_CHANNEL_OPTIONS_SQL)
            if row["channel_name"]]


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


def sku_mtd_distribution(connection) -> list:
    """SKU 当月销售额分布（静态 SQL 单一版本），按销售额降序。

    口径：fact_order_line.trade_time 为订单成交时间（DATETIME），月内
    截断用「< 明天」而非「<= 今天」——``<= CURDATE()`` 会漏掉今天
    00:00:00 之后的行；取消/未付款/退款行已在 extract 层排除，
    paid_amount 即买家实付。按 spec_no 聚合，展示名取 goods_name，
    缺失时回退 spec_no。返回 [{"name", "value": Decimal}]。
    """
    return [
        {
            "name": (row["goods_name"] or "").strip()
            or (row["spec_no"] or "").strip(),
            "value": Decimal(row["total"] or 0),
        }
        for row in _fetch_rows(connection, _SKU_MTD_SQL)
    ]


# 商品动销榜的行数护栏：总榜取前 N 个 SKU；分品牌/分渠道榜每组取前 N 名
# （分组榜不设总量上限——品牌/渠道数本身有限，且每组截断后行数可控）。
_SKU_TOTAL_LIMIT = 50
_SKU_TOP_PER_GROUP = 5

_SKU_DAILY_WINDOW_SQL = textwrap.dedent(
    """
    SELECT DATE(trade_time) AS d, SUM(paid_amount) AS total
    FROM fact_order_line
    WHERE trade_time >= %s AND trade_time < %s
    GROUP BY DATE(trade_time)
    """
).strip()


def _sku_reference_dates(connection, first_day, exclusive_end):
    """月内最新成交日与其前一日；无数据 → ``(None, None)``。

    数据截至日以「有成交的最后一天」为准而非字面昨天：订单行是 T+1
    入仓的字面昨天常无数据，环比对到空日会被读成 -100%。
    """
    as_of = _fetch_one(connection, _SKU_LATEST_DATE_SQL, (first_day, exclusive_end))
    if as_of is None:
        return None, None
    return as_of, as_of - timedelta(days=1)


def _sku_rollup(connection, *, group, first_day, exclusive_end, as_of,
                prev_day, brand=None, channel=None) -> list:
    """商品动销聚合行：月累计、数据日、前一日（分组 + 可选品牌/渠道）。"""
    group_sql, group_only = group
    filters = ""
    extra = []
    if brand:
        filters += "AND brand_name = %s "
        extra.append(brand)
    if channel:
        filters += "AND channel_name = %s "
        extra.append(channel)
    sql = _SKU_ROLLUP_SQL.format(
        group=group_sql, filters=filters, group_only=group_only
    )
    params = (
        first_day, exclusive_end,          # 月累计销售额
        first_day, exclusive_end,          # 月累计销量
        as_of, as_of + timedelta(days=1),  # 数据日
        prev_day, prev_day + timedelta(days=1),  # 前一日
        min(first_day, prev_day), exclusive_end,  # 扫描窗口（含跨月前一日）
        *extra,
    )
    return _fetch_rows(connection, sql, params)


def _decimal_total(row, key) -> float:
    return float(Decimal(row.get(key) or 0))


def _sku_entry(row, rank, share_of=None):
    """一行榜单；环比 = (数据日 − 前一日) ÷ 前一日，前一日为 0 → None。"""
    sales = _decimal_total(row, "mtd_amount")
    latest = _decimal_total(row, "latest_amount")
    prev = _decimal_total(row, "prev_amount")
    share = None
    if share_of:
        share = float(Decimal(sales) / Decimal(share_of))
    return {
        "rank": rank,
        "goods": (row.get("goods_name") or "").strip()
        or (row.get("spec_no") or "").strip(),
        "sales": sales,
        "share": share,
        "qty": _decimal_total(row, "mtd_qty"),
        "latest": latest,
        "delta_pct": None if prev <= 0 else (latest - prev) / prev,
    }


def _sku_total_rows(rows, *, group_key=None, per_group=None) -> list:
    """按分组取每组前 ``per_group`` 名（不分组即总榜前 ``_SKU_TOTAL_LIMIT``）。"""
    entries = []
    if group_key is None:
        # 占比以全量商品为分母（而非仅前 N 名），截断只影响展示行数。
        total = sum(Decimal(row.get("mtd_amount") or 0) for row in rows)
        rows = rows[:_SKU_TOTAL_LIMIT]
        for index, row in enumerate(rows, start=1):
            entry = _sku_entry(row, index, total)
            entry["brand"] = row.get("brand_name") or "未匹配"
            entries.append(entry)
        return entries
    buckets = {}
    for row in rows:
        # rows 已按 mtd_amount 降序，组内保序即组内排名。
        buckets.setdefault(row.get(group_key) or "未分组", []).append(row)
    for name in sorted(buckets):
        bucket = buckets[name][:per_group]
        group_total = sum(Decimal(row.get("mtd_amount") or 0) for row in bucket)
        for index, row in enumerate(bucket, start=1):
            entry = _sku_entry(row, index, group_total)
            entry[group_key] = name
            entries.append(entry)
    return entries


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
    sql = textwrap.dedent(
        f"""
        SELECT COALESCE(SUM(sales_amount), 0)
        FROM fact_daily_report_offline
        WHERE business_date BETWEEN %s AND %s
        {region_sql}
        AND business_date <= CURDATE()
        AND responsible_person NOT LIKE '%%合计%%'
        """
    ).strip()
    return _fetch_scalar(connection, sql, (first_day, last_day) + region_params)


def region_month_target(connection, *, region=None, first_day, last_day) -> Decimal:
    """区域月目标 = Σ各人员 MAX(monthly_target)（melt 陷阱：绝不跨行 SUM）。

    与 ``summarize_people`` 同口径取**全月行** MAX：月目标是月级常量，
    预填未来行不影响 MAX，故不做 CURDATE 截断（与取数查询刻意不对称）。
    """
    region_sql, region_params = _optional_region_filter(region)
    sql = textwrap.dedent(
        f"""
        SELECT COALESCE(SUM(mx), 0)
        FROM (
        SELECT responsible_person, MAX(monthly_target) AS mx
        FROM fact_daily_report_offline
        WHERE business_date BETWEEN %s AND %s
        {region_sql}
        AND responsible_person NOT LIKE '%%合计%%'
        GROUP BY responsible_person
        ) t
        """
    ).strip()
    return _fetch_scalar(connection, sql, (first_day, last_day) + region_params)


def department_mtd_ranking(connection, *, region=None, first_day, last_day) -> dict:
    """选中区域（缺省全区域）某自然月按部门 Σsales 降序；空部门→未分组。"""
    region_sql, region_params = _optional_region_filter(region)
    sql = textwrap.dedent(
        f"""
        SELECT department, SUM(sales_amount) AS total
        FROM fact_daily_report_offline
        WHERE business_date BETWEEN %s AND %s
        {region_sql}
        AND business_date <= CURDATE()
        AND responsible_person NOT LIKE '%%合计%%'
        GROUP BY department
        ORDER BY total DESC
        """
    ).strip()
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
    sql = textwrap.dedent(
        f"""
        SELECT business_date, region, SUM(sales_amount) AS total
        FROM fact_daily_report_offline
        WHERE business_date BETWEEN %s AND %s
        {region_sql}
        AND business_date <= CURDATE()
        AND responsible_person NOT LIKE '%%合计%%'
        GROUP BY business_date, region
        """
    ).strip()
    rows = _fetch_rows(connection, sql, (first_day, last_day) + region_params)
    return _align_series_rows(rows, "region")


_OFFLINE_DOD_LATEST_SQL = textwrap.dedent(
    """
    SELECT MAX(business_date) AS d
    FROM fact_daily_report_offline
    WHERE business_date <= CURDATE()
    AND responsible_person NOT LIKE '%合计%'
    """
).strip()

_CHANNEL_DOD_LATEST_SQL = textwrap.dedent(
    """
    SELECT MAX(business_date) AS d
    FROM fact_channel_daily_sales
    WHERE business_date <= CURDATE()
    """
).strip()

_OFFLINE_DOD_WINDOW_SQL = textwrap.dedent(
    """
    SELECT business_date, SUM(sales_amount) AS total
    FROM fact_daily_report_offline
    WHERE business_date BETWEEN %s AND %s
    AND responsible_person NOT LIKE '%%合计%%'
    GROUP BY business_date
    """
).strip()

_CHANNEL_DOD_WINDOW_SQL = textwrap.dedent(
    """
    SELECT business_date, SUM(sales_amount) AS total
    FROM fact_channel_daily_sales
    WHERE business_date BETWEEN %s AND %s
    GROUP BY business_date
    """
).strip()


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


_CHANNEL_MONTH_RANKING_SQL = textwrap.dedent(
    """
    SELECT channel, SUM(sales_amount) AS total
    FROM fact_channel_daily_sales
    WHERE business_date BETWEEN %s AND %s
    AND business_date <= CURDATE()
    GROUP BY channel
    ORDER BY total DESC
    """
).strip()

_CHANNEL_MONTH_DAILY_SQL = textwrap.dedent(
    """
    SELECT business_date, channel, SUM(sales_amount) AS total
    FROM fact_channel_daily_sales
    WHERE business_date BETWEEN %s AND %s
    AND business_date <= CURDATE()
    GROUP BY business_date, channel
    """
).strip()

_CHANNEL_MTD_COMPARISON_SQL = textwrap.dedent(
    """
    SELECT channel,
    SUM(sales_amount) AS sales,
    SUM(promotion_cost) AS promo,
    COUNT(DISTINCT store_name) AS stores
    FROM fact_channel_daily_sales
    WHERE business_date BETWEEN %s AND %s
    AND business_date <= CURDATE()
    GROUP BY channel
    ORDER BY sales DESC
    """
).strip()


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
    sql = textwrap.dedent(
        f"""
        SELECT store_name, channel, SUM(sales_amount) AS total
        FROM fact_channel_daily_sales
        WHERE business_date BETWEEN %s AND %s
        {channel_sql}
        AND business_date <= CURDATE()
        GROUP BY store_name, channel
        ORDER BY total DESC
        """
    ).strip()
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


#: 环形图最多单列的 SKU 切片数；其余 SKU 合并为「其他」。
_PIE_SKU_TOP_LIMIT = 8


def run_pie_sku_mtd(connection, params) -> dict:
    """电商 SKU 当月销售占比: L1 无参环形图（电商本月销售的下钻补充）。

    头部 ``_PIE_SKU_TOP_LIMIT`` 个 SKU 各占一片，长尾合并「其他」——
    SKU 数随业务增长，切片过多环形图不可读；占比由前端按 items 求和
    换算（ECharts 内建 percent）。
    """
    items = sku_mtd_distribution(connection)
    top = items[:_PIE_SKU_TOP_LIMIT]
    rest = items[_PIE_SKU_TOP_LIMIT:]
    if rest:
        top.append({"name": "其他", "value": sum(i["value"] for i in rest)})
    return {
        "chart": "pie",
        "items": [{"name": i["name"], "value": float(i["value"])} for i in top],
        "unit": _UNIT,
    }


_SKU_TOTAL_COLUMNS = (
    {"key": "rank", "title": "排名"},
    {"key": "goods", "title": "商品"},
    {"key": "brand", "title": "品牌"},
    {"key": "sales", "title": "累计销售额", "format": "wan"},
    {"key": "share", "title": "销售占比", "format": "percent"},
    {"key": "qty", "title": "累计销量", "format": "number"},
    {"key": "latest", "title": "昨日销售额", "format": "wan"},
    {"key": "delta_pct", "title": "环比", "format": "delta"},
)

_SKU_GROUPED_MONEY_COLUMNS = (
    {"key": "sales", "title": "累计销售额", "format": "wan"},
    {"key": "latest", "title": "昨日销售额", "format": "wan"},
    {"key": "delta_pct", "title": "环比", "format": "delta"},
)


def _sku_window(params):
    """(month, 月首, 月末+1 天)：月窗口一律按半开区间，避免丢当天。"""
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    first_day, last_day = month_bounds(month)
    return month, first_day, last_day + timedelta(days=1)


def _sku_trend7(connection, as_of) -> list:
    """近 7 个自然日的商品销售额（缺数日为 null，前端断线）。"""
    rows = _fetch_rows(
        connection,
        _SKU_DAILY_WINDOW_SQL,
        (as_of - timedelta(days=6), as_of + timedelta(days=1)),
    )
    totals = {row["d"]: row["total"] for row in rows}
    trend = []
    for offset in range(6, -1, -1):
        day = as_of - timedelta(days=offset)
        value = totals.get(day)
        trend.append(
            {
                "date": day.isoformat(),
                "value": None if value is None else float(Decimal(value)),
            }
        )
    return trend


def _sku_rollup_or_empty(connection, params, group, brand=None, channel=None):
    """(rows, as_of)：无数据（as_of 为 None）时返回空行，卡片自行占位。"""
    _, first_day, exclusive_end = _sku_window(params)
    as_of, prev_day = _sku_reference_dates(connection, first_day, exclusive_end)
    if as_of is None:
        return [], None
    rows = _sku_rollup(
        connection,
        group=group,
        first_day=first_day,
        exclusive_end=exclusive_end,
        as_of=as_of,
        prev_day=prev_day,
        brand=brand,
        channel=channel,
    )
    return rows, as_of


def _sku_grouped_payload(rows, as_of, group_key, group_title) -> dict:
    """分品牌/分渠道榜共用载荷：分组名 + 组内排名 + 商品 + 金额 + 环比。"""
    entries = _sku_total_rows(
        rows, group_key=group_key, per_group=_SKU_TOP_PER_GROUP
    )
    return {
        "chart": "table",
        "as_of": as_of.isoformat() if as_of else None,
        "columns": [
            {"key": group_key, "title": group_title},
            {"key": "rank", "title": "组内排名"},
            {"key": "goods", "title": "商品"},
            *_SKU_GROUPED_MONEY_COLUMNS,
        ],
        "rows": entries,
    }


def run_kpi_sku_mtd(connection, params) -> dict:
    """商品动销 KPI：本月商品累计销售额 + 数据日/前一日/环比 + SKU 数。

    value 为月累计（电商口径，已排除取消/待付/全额退款单）；
    latest/prev/delta_pct 为「数据截至日 vs 前一日」的日环比（日环比
    与月累计并列，不做二次换算）；trend7 供卡内迷你趋势。
    """
    month, first_day, exclusive_end = _sku_window(params)
    payload = {
        "chart": "scalar",
        "value": 0.0,
        "date": None,
        "latest": None,
        "prev": None,
        "delta_pct": None,
        "trend7": [],
        "sku_count": 0,
        "active_sku_count": 0,
        "month": month,
        "unit": _UNIT,
    }
    as_of, prev_day = _sku_reference_dates(connection, first_day, exclusive_end)
    if as_of is None:
        return payload
    rows = _sku_rollup(
        connection,
        group=_SKU_GROUP_TOTAL,
        first_day=first_day,
        exclusive_end=exclusive_end,
        as_of=as_of,
        prev_day=prev_day,
    )
    total = sum((Decimal(row["mtd_amount"] or 0) for row in rows), Decimal(0))
    latest = sum((Decimal(row["latest_amount"] or 0) for row in rows), Decimal(0))
    prev = sum((Decimal(row["prev_amount"] or 0) for row in rows), Decimal(0))
    payload.update(
        {
            "value": float(total),
            "date": as_of.isoformat(),
            "latest": float(latest),
            "prev": float(prev),
            "delta_pct": None if prev == 0 else float((latest - prev) / prev),
            "trend7": _sku_trend7(connection, as_of),
            "sku_count": len(rows),
            "active_sku_count": sum(
                1 for row in rows if Decimal(row["mtd_amount"] or 0) > 0
            ),
        }
    )
    return payload


def run_table_sku_hot_total(connection, params) -> dict:
    """热销商品总榜：累计销售额降序，含品牌、占比、销量、昨日与环比。"""
    rows, as_of = _sku_rollup_or_empty(connection, params, _SKU_GROUP_TOTAL)
    return {
        "chart": "table",
        "as_of": as_of.isoformat() if as_of else None,
        "columns": list(_SKU_TOTAL_COLUMNS),
        "rows": _sku_total_rows(rows),
    }


def run_table_sku_hot_brand(connection, params) -> dict:
    """分品牌热销商品：每个品牌内按销售额取前 ``_SKU_TOP_PER_GROUP`` 名。"""
    brand = params.get("brand")
    rows, as_of = _sku_rollup_or_empty(
        connection, params, _SKU_GROUP_BRAND, brand=brand
    )
    return _sku_grouped_payload(rows, as_of, "brand_name", "品牌")


def run_table_sku_hot_channel(connection, params) -> dict:
    """分渠道热销商品：每个店铺渠道内取前 ``_SKU_TOP_PER_GROUP`` 名。"""
    channel = params.get("channel")
    rows, as_of = _sku_rollup_or_empty(
        connection, params, _SKU_GROUP_CHANNEL, channel=channel
    )
    return _sku_grouped_payload(rows, as_of, "channel_name", "渠道")


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


# ---------------------------------------------------------------------------
# 缺口 / 四级告警（CubeSchema §2：派生口径后端化）
#
# SQL 只取**事实**（name / target / done）；shortfall、required_daily、
# severity 全部交给 :mod:`common.bi_web.derived` 的纯函数 —— 口径可单测、
# 前端不得重算一遍。
#
# 目标取值纪律与 ``region_month_target`` 逐字相同：月目标 = Σ各人员
# MAX(monthly_target)，**绝不跨行 SUM**（melt 后每行重复携带月目标）。
# 取 MAX 的子查询刻意**不做** CURDATE 截断（月目标是月级常量，预填未来行
# 不影响 MAX）；取 done 的子查询按惯例 ``business_date <= CURDATE()``
# 截断未来预填行。
# ---------------------------------------------------------------------------

#: 事实表 grain → 卡面/下钻粒度标识（people 的前端 grain 是 person）。
_SHORTFALL_GRAINS = ("people", "region")
_SHORTFALL_FRONT_GRAIN = {"people": "person", "region": "region"}
_DEFAULT_SHORTFALL_GRAIN = "people"

#: 人员粒度：每人一行目标（MAX 子查询）+ 已完成（截断子查询）。
#:
#: ``has_fact``（§5 第 5 类缺陷）由 **LEFT JOIN 右表主键是否为空**判定，
#: 不靠 ``done == 0`` 反推：**没有事实行 ≠ 挂零**，两者的告警语义相反。
_SHORTFALL_PEOPLE_SQL = textwrap.dedent(
    """
    SELECT t.person AS name, MAX(t.department) AS dept,
    SUM(t.mx) AS target, COALESCE(SUM(d.done), 0) AS done,
    (MAX(d.person) IS NOT NULL) AS has_fact
    FROM (
    SELECT responsible_person AS person, MAX(department) AS department,
    MAX(monthly_target) AS mx
    FROM fact_daily_report_offline
    WHERE business_date BETWEEN %s AND %s
    {region_sql}
    AND responsible_person NOT LIKE '%%合计%%'
    GROUP BY responsible_person
    ) t
    LEFT JOIN (
    SELECT responsible_person AS person, SUM(sales_amount) AS done
    FROM fact_daily_report_offline
    WHERE business_date BETWEEN %s AND %s
    AND business_date <= CURDATE()
    {region_sql}
    AND responsible_person NOT LIKE '%%合计%%'
    GROUP BY responsible_person
    ) d ON d.person <=> t.person
    GROUP BY t.person
    ORDER BY target DESC, t.person
    """
).strip()

#: 区域粒度：区域目标是该区域内各人员 MAX(monthly_target) 之和，而不是
#: 区域内所有行的 SUM（同一人的月目标在 melt 后每行重复携带，只计一次）。
_SHORTFALL_REGION_SQL = textwrap.dedent(
    """
    SELECT t.region AS name, SUM(t.mx) AS target,
    COALESCE(SUM(d.done), 0) AS done,
    (MAX(d.person) IS NOT NULL) AS has_fact
    FROM (
    SELECT region, responsible_person, MAX(monthly_target) AS mx
    FROM fact_daily_report_offline
    WHERE business_date BETWEEN %s AND %s
    AND responsible_person NOT LIKE '%%合计%%'
    GROUP BY region, responsible_person
    ) t
    LEFT JOIN (
    SELECT region, responsible_person, SUM(sales_amount) AS done
    FROM fact_daily_report_offline
    WHERE business_date BETWEEN %s AND %s
    AND business_date <= CURDATE()
    AND responsible_person NOT LIKE '%%合计%%'
    GROUP BY region, responsible_person
    ) d ON d.region <=> t.region
    AND d.responsible_person <=> t.responsible_person
    GROUP BY t.region
    ORDER BY target DESC, t.region
    """
).strip()

#: AnomalyList 的服务端截断行数（也是 card 的契约：最多 N 行）。
_ANOMALY_TOP_N = 10


def shortfall_facts(connection, *, grain="people", region=None,
                    first_day, last_day) -> list:
    """只读事实：``[{name, target, done}]``（金额与 ``_unit`` 同为元）。

    **不含任何派生列** —— 派生一律走 :mod:`common.bi_web.derived`，这是
    「口径后端化」的可测边界。``grain`` 为 ``people``（人员，含部门）或
    ``region``（区域汇总）；``region`` 参数只对人员粒度生效。
    """
    if grain not in _SHORTFALL_GRAINS:
        grain = _DEFAULT_SHORTFALL_GRAIN
    if grain == "region":
        return _fetch_rows(
            connection, _SHORTFALL_REGION_SQL,
            (first_day, last_day, first_day, last_day),
        )
    region_sql, region_params = _optional_region_filter(region)
    sql = _SHORTFALL_PEOPLE_SQL.replace("{region_sql}", region_sql)
    return _fetch_rows(
        connection, sql,
        (first_day, last_day) + region_params
        + (first_day, last_day) + region_params,
    )


def _has_fact(fact) -> bool:
    """诊断标记：``has_fact`` 由 SQL 的 LEFT JOIN 右表主键是否为空给出。

    **不参与 severity 判定**：在这张卡里左表由 target 驱动（有目标才出现在
    结果集），右表为空 == 「有目标、本月截至今日无销单」== 挂零，与
    ``done == 0`` 是同一批人。把它当成「数据缺失」降级，就是把挂零粉饰成
    没数据 —— 漏报比错报严重。真正的第 5 类缺陷需要**独立的人员状态信号**
    （离职/未启用/数据源未接入），目前不存在，不能靠 JOIN 空值倒推。

    字段缺失（旧调用方/老夹具）时按 ``True`` 处理以保持既有行为；SQL 侧
    ``(MAX(d.person) IS NOT NULL)`` 返回 0/1，这里统一转成 ``bool``。
    """
    value = fact.get("has_fact")
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return True


def shortfall_rows(facts, *, total_workdays, elapsed_workdays,
                   remaining_workdays) -> list:
    """纯组装：事实行 → CubeSchema §2 行（派生列全部在此调用 derived）。

    排序键只有 ``shortfall``（降序，§2.1「默认排序键」），缺口不可算的行
    （``shortfall`` 为 ``None``：无目标）排末尾 —— 它们是数据缺陷，不是
    落后人员。

    ``has_fact`` 是**纯诊断字段**（``False`` = 本月截至今日无销单行），原值
    透传给前端做角标，**不参与任何派生计算**：无论它真假，``done`` 都按事实
    行的聚合值（缺失即 0）走 §2.3 分级，该 p0 就 p0。
    """
    rows = []
    for fact in facts:
        target = fact.get("target")
        done = fact.get("done")
        required = derived.required_daily(target, total_workdays)
        gap = derived.shortfall(target, done)
        row = {
            key: value for key, value in fact.items()
            if key not in ("target", "done", "has_fact")
        }
        row.update(
            {
                "target": None if target is None else float(target),
                "done": float(done or 0),
                "shortfall": None if gap is None else float(gap),
                "rate": derived.rate(done, target),
                "required_daily": None if required is None else float(required),
                "severity": derived.severity(
                    done, gap, required, remaining_workdays, elapsed_workdays
                ),
                # 纯诊断字段（不参与上面的任何计算）：False = 本月截至今日
                # 无销单行，与 done == 0 同义，前端可据此渲染「本月无销单」角标。
                "has_fact": _has_fact(fact),
                "remaining_workdays": remaining_workdays,
                "elapsed_workdays": elapsed_workdays,
            }
        )
        rows.append(row)
    rows.sort(
        key=lambda row: (
            row["shortfall"] is None,
            -(row["shortfall"] or 0.0),
            row.get("name") or "",
        )
    )
    return rows


def month_workdays(connection, *, first_day, last_day) -> set:
    """当月工作日集合（``dim_calendar.is_workday=1``，日报机器人同口径）。"""
    return fetch_workdays(connection, year=first_day.year, month=first_day.month)


def _shortfall_as_of(first_day, last_day):
    """锚点日：当前月=今天（含当天），历史月=月末（``_people_as_of`` 同款）。"""
    today = datetime.now().date()
    if first_day <= today <= last_day:
        return today
    return last_day


def shortfall_calendar(connection, *, first_day, last_day) -> dict:
    """缺口径的日历上下文：``total/elapsed/remaining_workdays``。

    已闭月（今天不在该月内）→ ``remaining_workdays`` 为 ``None``：历史月
    的「剩余产能」没有意义，p1 随之不可判（``derived.severity`` 退到
    p2），而不是把上个月的每个人都标成 p1。
    """
    workdays = month_workdays(connection, first_day=first_day, last_day=last_day)
    as_of = _shortfall_as_of(first_day, last_day)
    today = datetime.now().date()
    return {
        "total_workdays": derived.total_workdays(workdays),
        "elapsed_workdays": derived.elapsed_workdays(workdays, as_of),
        "remaining_workdays": (
            derived.remaining_workdays(workdays, as_of)
            if first_day <= today <= last_day else None
        ),
    }


def shortfall_columns(grain) -> list:
    """卡面列定义（Schema 驱动渲染：前端按 format 格式化，不做换算）。"""
    label = "姓名" if grain == "people" else "区域"
    columns = [{"key": "name", "title": label}]
    if grain == "people":
        columns.append({"key": "dept", "title": "部门"})
    columns.extend(
        [
            {"key": "target", "title": "月目标", "format": "wan"},
            {"key": "done", "title": "已完成", "format": "wan"},
            {"key": "shortfall", "title": "缺口", "format": "wan"},
            {"key": "rate", "title": "完成率", "format": "percent"},
            {"key": "required_daily", "title": "所需日均", "format": "wan"},
            {"key": "severity", "title": "告警", "format": "severity"},
        ]
    )
    return columns


def _shortfall_window(params):
    """run 层共用解析：month（缺省当前月）+ 月界（``month_bounds``）。"""
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    first_day, last_day = month_bounds(month)
    return month, first_day, last_day


def run_kpi_shortfall(connection, params) -> dict:
    """⑮ 目标缺口与告警表（CubeSchema §2）: target/done/shortfall/rate/
    required_daily/severity，**全部后端派生**。

    region/month 可选；内部已支持 ``grain=region`` 的区域汇总。URL 暂不放
    开 grain 参数：新增一个筛选 source 就要改 ``app._FILTER_SOURCE_QUERIES``
    与 ``config.KNOWN_FILTER_SOURCES``，而 app 层本批冻结，留给下一批增量。
    """
    grain = params.get("grain") or _DEFAULT_SHORTFALL_GRAIN
    if grain not in _SHORTFALL_GRAINS:
        grain = _DEFAULT_SHORTFALL_GRAIN
    month, first_day, last_day = _shortfall_window(params)
    facts = shortfall_facts(
        connection,
        grain=grain,
        region=params.get("region"),
        first_day=first_day,
        last_day=last_day,
    )
    calendar = shortfall_calendar(
        connection, first_day=first_day, last_day=last_day
    )
    rows = shortfall_rows(facts, **calendar)
    return {
        "chart": "table",
        "unit": _UNIT,
        "month": month,
        "grain": _SHORTFALL_FRONT_GRAIN[grain],
        "as_of": _shortfall_as_of(first_day, last_day).isoformat(),
        "severity_domain": list(derived.SEVERITY_DOMAIN),
        "columns": shortfall_columns(grain),
        "rows": rows,
    }


def run_anomaly_top(connection, params) -> dict:
    """⑯ 缺口 TOP N（AnomalyList）: 与 ⑮ 同口径，服务端按缺口降序截断。

    无目标的行（缺口不可算）**不参与**排序（§5 数据缺陷显式化）；名次由
    服务端给出（``rank`` 1..N），前端不重排。
    """
    month, first_day, last_day = _shortfall_window(params)
    facts = shortfall_facts(
        connection, grain=_DEFAULT_SHORTFALL_GRAIN,
        first_day=first_day, last_day=last_day,
    )
    calendar = shortfall_calendar(
        connection, first_day=first_day, last_day=last_day
    )
    ranked = [row for row in shortfall_rows(facts, **calendar)
              if row["shortfall"] is not None][:_ANOMALY_TOP_N]
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
    columns = [{"key": "rank", "title": "名次"}] + shortfall_columns(
        _DEFAULT_SHORTFALL_GRAIN
    )
    return {
        "chart": "table",
        "unit": _UNIT,
        "month": month,
        "grain": _SHORTFALL_FRONT_GRAIN[_DEFAULT_SHORTFALL_GRAIN],
        "as_of": _shortfall_as_of(first_day, last_day).isoformat(),
        "severity_domain": list(derived.SEVERITY_DOMAIN),
        "limit": _ANOMALY_TOP_N,
        "columns": columns,
        "rows": ranked,
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
