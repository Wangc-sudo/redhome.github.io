"""BI channel 查询域；SQL 与返回契约自原 queries.py 等价迁移。"""

from common.bi_web.query_domains.common import *


_CHANNEL_MTD_SQL = textwrap.dedent(
    """
    SELECT COALESCE(SUM(sales_amount), 0)
    FROM fact_channel_daily_sales
    WHERE business_date >= DATE_FORMAT(CURDATE(), '%%Y-%%m-01')
    AND business_date <= %s
    """
).strip()


_CHANNEL_LATEST_SQL = textwrap.dedent(
    """
    SELECT MAX(business_date) AS d
    FROM fact_channel_daily_sales
    WHERE business_date <= CURDATE()
    AND sales_amount IS NOT NULL
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


def channel_latest_date(connection):
    """渠道数据真实水位：最新有数日（预填 NULL 行不算；空表返回 None）。"""
    return _fetch_one(connection, _CHANNEL_LATEST_SQL)


def _channel_mtd_sum(connection, latest) -> Decimal:
    """当月 1 号 → 水位日的 Σsales；水位为空（空表/全 NULL）时直接 0。"""
    if latest is None:
        return Decimal(0)
    return _fetch_scalar(connection, _CHANNEL_MTD_SQL, (latest,))


def channel_mtd_total(connection) -> Decimal:
    """电商渠道本月累计销售 (③)：窗口上界 = 真实水位（最新有数日）。

    与线下 B2 同款：预填 NULL 空行不算入仓，「本月累计」截至最新
    有数日；水位滞后整月时区间为空、和为 0。
    """
    return _channel_mtd_sum(connection, channel_latest_date(connection))


def channel_annual_total(connection) -> Decimal:
    """电商渠道年累计: year-to-date channel sales."""
    return _fetch_scalar(connection, _CHANNEL_ANNUAL_SQL)


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


_CHANNEL_DOD_LATEST_SQL = textwrap.dedent(
    """
    SELECT MAX(business_date) AS d
    FROM fact_channel_daily_sales
    WHERE business_date <= CURDATE()
    AND sales_amount IS NOT NULL
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
    """指定月按日 Σsales 分渠道序列（渠道名升序，三态轴）。

    与区域日序列同一核心：轴 = 月首→min(月末, 锚点) 完整日窗口，缺失
    null 断线、营业日真实 0 标 zero。锚点沿用 ``_shortfall_as_of``
    （当前月=今天）——B2 的真实水位只切线下侧，渠道口径本批不动。
    """
    rows = _fetch_rows(connection, _CHANNEL_MONTH_DAILY_SQL, (first_day, last_day))
    return _daily_three_state(
        connection, rows, "channel", first_day, last_day,
        anchor=_shortfall_as_of(first_day, last_day),
    )


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


def run_kpi_channel_mtd(connection, params) -> dict:
    """③电商渠道本月累计销售: scalar KPI, 元（窗口截至真实水位，as_of 下发）。

    水位定锚只跑一次（``_channel_mtd_sum`` 直接吃定锚结果）；空表时
    value=0、as_of=None——与线下卡同款契约。
    """
    latest = channel_latest_date(connection)
    return {
        "chart": "scalar",
        "value": float(_channel_mtd_sum(connection, latest)),
        "as_of": latest.isoformat() if latest is not None else None,
        "unit": _UNIT,
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
            {"name": entry["name"],
             "data": [None if value is None else float(value)
                      for value in entry["data"]]}
            for entry in series["series"]
        ],
        "status": series["status"],
        "as_of": series["as_of"],
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

__all__ = ('_CHANNEL_MTD_SQL', '_CHANNEL_LATEST_SQL', '_CHANNEL_ANNUAL_SQL', '_CHANNEL_RANKING_SQL', 'channel_latest_date', '_channel_mtd_sum', 'channel_mtd_total', 'channel_annual_total', 'channel_mtd_ranking', '_CHANNEL_DOD_LATEST_SQL', '_CHANNEL_DOD_WINDOW_SQL', 'channel_dod', '_CHANNEL_MONTH_RANKING_SQL', '_CHANNEL_MONTH_DAILY_SQL', '_CHANNEL_MTD_COMPARISON_SQL', '_optional_channel_filter', 'channel_month_ranking', 'channel_month_daily_series', 'channel_mtd_comparison', 'store_mtd_ranking', 'run_kpi_channel_mtd', 'run_bar_channel_mtd', 'run_kpi_channel_dod', 'run_trend_channel_daily', 'run_table_channel_mtd', 'run_table_store_mtd')
