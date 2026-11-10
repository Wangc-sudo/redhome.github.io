"""BI sku 查询域；SQL 与返回契约自原 queries.py 等价迁移。"""

from common.bi_web.query_domains.common import *


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


_BRAND_OPTIONS_SQL = (
    "SELECT DISTINCT brand_name FROM fact_order_line ORDER BY brand_name"
)


_SKU_CHANNEL_OPTIONS_SQL = (
    "SELECT DISTINCT channel_name FROM fact_order_line ORDER BY channel_name"
)


_SKU_LATEST_DATE_SQL = textwrap.dedent(
    """
    SELECT MAX(DATE(trade_time)) AS d
    FROM fact_order_line
    WHERE trade_time >= %s AND trade_time < %s
    AND trade_time < DATE_ADD(CURDATE(), INTERVAL 1 DAY)
    """
).strip()


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


_SKU_GROUP_TOTAL = ("brand_name, spec_no, goods_name,", "brand_name, spec_no, goods_name")


_SKU_GROUP_BRAND = _SKU_GROUP_TOTAL


_SKU_GROUP_CHANNEL = (
    "channel_name, spec_no, goods_name,",
    "channel_name, spec_no, goods_name",
)


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

__all__ = ('_SKU_MTD_SQL', '_BRAND_OPTIONS_SQL', '_SKU_CHANNEL_OPTIONS_SQL', '_SKU_LATEST_DATE_SQL', '_SKU_ROLLUP_SQL', '_SKU_GROUP_TOTAL', '_SKU_GROUP_BRAND', '_SKU_GROUP_CHANNEL', 'sku_mtd_distribution', '_SKU_TOTAL_LIMIT', '_SKU_TOP_PER_GROUP', '_SKU_DAILY_WINDOW_SQL', '_sku_reference_dates', '_sku_rollup', '_decimal_total', '_sku_entry', '_sku_total_rows', '_PIE_SKU_TOP_LIMIT', 'run_pie_sku_mtd', '_SKU_TOTAL_COLUMNS', '_SKU_GROUPED_MONEY_COLUMNS', '_sku_window', '_sku_trend7', '_sku_rollup_or_empty', '_sku_grouped_payload', 'run_kpi_sku_mtd', 'run_table_sku_hot_total', 'run_table_sku_hot_brand', 'run_table_sku_hot_channel')
