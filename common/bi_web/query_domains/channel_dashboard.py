"""BI channel_dashboard 查询域；SQL 与返回契约自原 queries.py 等价迁移。"""

from common.bi_web.query_domains.common import *
from common.bi_web.query_domains.channel import *
from common.bi_web.query_domains.offline_dashboards import run_kpi_hz_progress, _hz_anchor


_CHANNEL_MONTH_SUM_SQL = (
    "SELECT COALESCE(SUM(sales_amount), 0) AS `s` "
    "FROM `fact_channel_daily_sales` "
    "WHERE `business_date` BETWEEN %s AND %s "
    "AND `business_date` <= CURDATE()"
)


_CHANNEL_DAY_SQL = (
    "SELECT COALESCE(SUM(sales_amount), 0) AS `s` "
    "FROM `fact_channel_daily_sales` "
    "WHERE `business_date` = %s"
)


def run_kpi_qd_mtd(connection, params) -> dict:
    """渠道本月累计销售（scalar）：月窗口 = 锚点日所在月（截断未来日）。"""
    anchor = _hz_anchor(params)
    first_day, last_day = month_bounds(anchor.strftime("%Y-%m"))
    rows = _fetch_rows(connection, _CHANNEL_MONTH_SUM_SQL, (first_day, last_day))
    value = rows[0]["s"] if rows else 0
    return {
        "chart": "scalar",
        "value": float(value),
        "unit": _UNIT,
        "date": anchor.isoformat(),
    }


def run_kpi_qd_today(connection, params) -> dict:
    """渠道当日销售（scalar）：锚点日 Σ sales_amount（渠道表无合计行）。"""
    anchor = _hz_anchor(params)
    rows = _fetch_rows(connection, _CHANNEL_DAY_SQL, (anchor,))
    value = rows[0]["s"] if rows else 0
    return {
        "chart": "scalar",
        "value": float(value),
        "unit": _UNIT,
        "date": anchor.isoformat(),
    }


def run_kpi_qd_progress(connection, params) -> dict:
    """渠道时间进度（scalar）：与杭州卡同函数——工作日日历公司统一。"""
    return run_kpi_hz_progress(connection, params)


def run_trend_qd_daily(connection, params) -> dict:
    """渠道日销趋势（line）：分渠道多系列（月 = 锚点日所在月）。"""
    anchor = _hz_anchor(params)
    return run_trend_channel_daily(
        connection, {"month": anchor.strftime("%Y-%m")}
    )


def run_bar_qd_channel(connection, params) -> dict:
    """渠道本月排行（bar）：channel_month_ranking 锚点月版。"""
    anchor = _hz_anchor(params)
    return run_bar_channel_mtd(
        connection, {"month": anchor.strftime("%Y-%m")}
    )


def run_table_qd_store_mtd(connection, params) -> dict:
    """店铺本月排行（table）：全渠道含渠道列（月 = 锚点日所在月）。"""
    anchor = _hz_anchor(params)
    return run_table_store_mtd(
        connection, {"month": anchor.strftime("%Y-%m")}
    )


def _qd_unfilled_payload(connection, anchor):
    """渠道锚点日未填报门店（table）：与渠道催办同真源。

    复用 :mod:`common.daily_robot.channel_missing` 的纯逻辑与只读查询：
    点名册 = 月目标表全店 ∪ 近 30 天活跃店（不减 storeExclude，单纯报）；
    判定 = 当日无行或 ``sales_amount IS NULL`` → 缺，店铺名留空行按渠道级
    状态兜底。与 10:31 T+1 催办/群播报逐字同口径，绝不另造一套。
    """
    from common.daily_robot import channel_missing as cm

    target_rows, recent_rows = cm.fetch_roster_rows(connection, anchor)
    roster = cm.expected_stores(target_rows, recent_rows)
    daily_rows = cm.fetch_daily_rows(connection, anchor)
    states = cm.store_states(daily_rows)
    cstates = cm.channel_states(daily_rows)
    missing = []
    for store, channel in roster.items():
        state = states.get(store) or cstates.get(channel) or cm.MISSING
        if state == cm.MISSING:
            missing.append((store, channel))
    missing.sort()
    return {
        "chart": "table",
        "columns": [
            {"key": "rank", "title": "#"},
            {"key": "store", "title": "门店"},
            {"key": "channel", "title": "渠道"},
        ],
        "rows": [
            {"rank": index, "store": store, "channel": channel}
            for index, (store, channel) in enumerate(missing, start=1)
        ],
        "date": anchor.isoformat(),
    }


def run_table_qd_unfilled_today(connection, params) -> dict:
    """渠道锚点日未填报门店（table）：``_qd_unfilled_payload`` 锚定版。"""
    return _qd_unfilled_payload(connection, _hz_anchor(params))


def _qd_table(columns, rows, anchor) -> dict:
    """qd 迁移卡共用的 table 载荷：列定义 + 行 + 日期（页面页脚显示）。"""
    return {
        "chart": "table",
        "columns": columns,
        "rows": [
            {key: _qd_jsonable(value) for key, value in row.items()}
            for row in rows
        ],
        "date": anchor.isoformat(),
    }


def run_table_qd_monthly_kpi(connection, params) -> dict:
    """面板1 月度核心指标（table）：``fetch_monthly_kpi`` 输出直映射（近6月）。"""
    from common.broadcast.queries import fetch_monthly_kpi

    anchor = _hz_anchor(params)
    data = fetch_monthly_kpi(connection)
    keys = (
        "ym", "gmv", "mom", "orders", "aov", "buyers", "new_buyers",
        "repeat_rate", "cancel_orders", "cancel_rate", "refund_amt",
        "refund_rate_amt", "net_gmv", "promo", "roi",
    )
    rows = [{key: month.get(key) for key in keys} for month in data["months"]]
    columns = [
        {"key": "ym", "title": "月份"},
        {"key": "gmv", "title": "GMV", "format": "wan"},
        {"key": "mom", "title": "环比", "format": "percent"},
        {"key": "orders", "title": "订单数", "format": "number"},
        {"key": "aov", "title": "客单价", "format": "number"},
        {"key": "buyers", "title": "活跃买家", "format": "number"},
        {"key": "new_buyers", "title": "新客", "format": "number"},
        {"key": "repeat_rate", "title": "复购率", "format": "percent"},
        {"key": "cancel_orders", "title": "取消单", "format": "number"},
        {"key": "cancel_rate", "title": "取消率", "format": "percent"},
        {"key": "refund_amt", "title": "退款金额", "format": "wan"},
        {"key": "refund_rate_amt", "title": "退款率", "format": "percent"},
        {"key": "net_gmv", "title": "净额", "format": "wan"},
        {"key": "promo", "title": "推广花费", "format": "wan"},
        {"key": "roi", "title": "ROI", "format": "ratio"},
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_expense(connection, params) -> dict:
    """面板2a 费用（table）：``fetch_expense_summary`` 渠道×月费用金额。"""
    from common.broadcast.queries import fetch_expense_summary

    anchor = _hz_anchor(params)
    data = fetch_expense_summary(connection, today=anchor)
    columns = [{"key": "channel", "title": "渠道"}]
    columns += [
        {"key": ym, "title": f"费用({ym})", "format": "wan"}
        for ym in data["months"]
    ]
    columns.append({"key": "total", "title": "合计费用", "format": "wan"})
    rows = [
        {
            "channel": row["channel"],
            **{ym: row["cells"][ym]["total"] for ym in data["months"]},
            "total": row["total"],
        }
        for row in data["rows"]
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_expense_rate(connection, params) -> dict:
    """面板2b 费率（table）：``fetch_expense_summary`` 渠道×月费率。"""
    from common.broadcast.queries import fetch_expense_summary

    anchor = _hz_anchor(params)
    data = fetch_expense_summary(connection, today=anchor)
    columns = [{"key": "channel", "title": "渠道"}]
    columns += [
        {"key": ym, "title": f"费率({ym})", "format": "percent"}
        for ym in data["months"]
    ]
    rows = [
        {
            "channel": row["channel"],
            **{ym: row["cells"][ym]["rate"] for ym in data["months"]},
        }
        for row in data["rows"]
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_bill_category(connection, params) -> dict:
    """面板3a 账单费用类别（table）：``fetch_bill_breakdown`` by_category。"""
    from common.broadcast.queries import fetch_bill_breakdown

    anchor = _hz_anchor(params)
    data = fetch_bill_breakdown(connection, today=anchor)
    grand = data["grand_total"]
    rows = [
        {
            "category": row["category"],
            "total": row["total"],
            "share": (row["total"] / grand) if grand else None,
        }
        for row in data["by_category"]
    ]
    columns = [
        {"key": "category", "title": "费用类别"},
        {"key": "total", "title": "金额", "format": "wan"},
        {"key": "share", "title": "占比", "format": "percent"},
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_bill_platform(connection, params) -> dict:
    """面板3b 账单平台（table）：``fetch_bill_breakdown`` by_platform。"""
    from common.broadcast.queries import fetch_bill_breakdown

    anchor = _hz_anchor(params)
    data = fetch_bill_breakdown(connection, today=anchor)
    rows = [
        {
            "platform": row["platform"],
            "total": row["total"],
            "share": row["share"],
        }
        for row in data["by_platform"]
    ]
    columns = [
        {"key": "platform", "title": "平台"},
        {"key": "total", "title": "金额", "format": "wan"},
        {"key": "share", "title": "占比", "format": "percent"},
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_bill_items(connection, params) -> dict:
    """面板3c 账单费用项目（table）：``fetch_bill_breakdown`` top_items + 其他项。"""
    from common.broadcast.queries import fetch_bill_breakdown

    anchor = _hz_anchor(params)
    data = fetch_bill_breakdown(connection, today=anchor)
    grand = data["grand_total"]
    rows = [
        {"item": item["item"], "total": item["total"], "share": item["share"]}
        for item in data["top_items"]
    ]
    other = data["other_items"]
    if other["count"] > 0:
        rows.append({
            "item": f'其他 {other["count"]} 项',
            "total": other["total"],
            "share": (other["total"] / grand) if grand else None,
        })
    columns = [
        {"key": "item", "title": "费用项目"},
        {"key": "total", "title": "金额", "format": "wan"},
        {"key": "share", "title": "占比", "format": "percent"},
    ]
    return _qd_table(columns, rows, anchor)


BILL_MANUAL_DATASET = "platform_bill_monthly"


BILL_MANUAL_METRIC = "amount"


def _bill_manual_period(connection, anchor):
    """最近一个**已导入**月份的首日（不晚于锚点月）；从未导入返回 None。

    账单是月报、导入常滞后于月份，锚定当月会经常性空白；故退回最近有数月，
    月份号显示在列标题上，不静默冒充当月。
    """
    return _fetch_one(
        connection,
        "SELECT MAX(`period_start`) AS `period_start` "
        "FROM `fact_manual_report` WHERE `dataset` = %s AND `period_start` <= %s",
        (BILL_MANUAL_DATASET, anchor.replace(day=1)),
    )


def _bill_manual_rows(connection, period_start):
    """某月平台账单明细：窄表行 → ``{维度字段: 值, 'amount': Decimal}``。"""
    if period_start is None:
        return []
    rows = _fetch_rows(
        connection,
        "SELECT `dim_scope`, `dimension_value`, `value` "
        "FROM `fact_manual_report` WHERE `dataset` = %s "
        "AND `metric` = %s AND `period_start` = %s",
        (BILL_MANUAL_DATASET, BILL_MANUAL_METRIC, period_start),
    )
    items = []
    for row in rows:
        item = dict(zip(row["dim_scope"].split("+"), row["dimension_value"].split("|")))
        item["amount"] = row["value"]
        items.append(item)
    return items


def run_table_qd_bill_manual(connection, params) -> dict:
    """面板3d 平台账单·科目汇总（table）：按费用科目汇总 + 占比。"""
    anchor = _hz_anchor(params)
    period_start = _bill_manual_period(connection, anchor)
    ym = period_start.strftime("%Y-%m") if period_start else None
    items = _bill_manual_rows(connection, period_start)

    totals = {}
    for item in items:
        key = item.get("fee_item") or "未填科目"
        totals[key] = totals.get(key, Decimal(0)) + (item["amount"] or Decimal(0))
    grand = sum(totals.values(), Decimal(0))
    rows = [
        {
            "fee_item": name,
            "total": total,
            "share": (total / grand) if grand else None,
        }
        for name, total in sorted(
            totals.items(), key=lambda kv: abs(kv[1]), reverse=True
        )
    ]
    columns = [
        {"key": "fee_item", "title": "费用科目"},
        {"key": "total", "title": f"金额({ym})" if ym else "金额", "format": "wan"},
        {"key": "share", "title": "占比", "format": "percent"},
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_bill_manual_detail(connection, params) -> dict:
    """面板3e 平台账单·明细（table）：平台 × 店铺 × 费用科目 逐行，金额降序。"""
    anchor = _hz_anchor(params)
    period_start = _bill_manual_period(connection, anchor)
    ym = period_start.strftime("%Y-%m") if period_start else None
    items = _bill_manual_rows(connection, period_start)
    items.sort(key=lambda item: abs(item["amount"] or Decimal(0)), reverse=True)

    rows = [
        {
            "platform": item.get("platform") or "未填平台",
            "store": item.get("store") or "",
            "fee_item": item.get("fee_item") or "未填科目",
            "amount": item["amount"],
        }
        for item in items
    ]
    columns = [
        {"key": "platform", "title": "平台"},
        {"key": "store", "title": "店铺"},
        {"key": "fee_item", "title": "费用科目"},
        {"key": "amount", "title": f"金额({ym})" if ym else "金额", "format": "wan"},
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_product_series(connection, params) -> dict:
    """面板4a 商品系列结构（table）：``fetch_product_structure`` series。"""
    from common.broadcast.queries import fetch_product_structure

    anchor = _hz_anchor(params)
    data = fetch_product_structure(connection, today=anchor)
    rows = [
        {"series": row["series"], "gmv": row["gmv"], "share": row["share"]}
        for row in data["series"]
    ]
    columns = [
        {"key": "series", "title": "系列"},
        {"key": "gmv", "title": "GMV", "format": "wan"},
        {"key": "share", "title": "占比", "format": "percent"},
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_product_bands(connection, params) -> dict:
    """面板4b 商品价格带（table）：``fetch_product_structure`` price_bands。"""
    from common.broadcast.queries import fetch_product_structure

    anchor = _hz_anchor(params)
    data = fetch_product_structure(connection, today=anchor)
    rows = [
        {"band": row["band"], "gmv": row["gmv"], "share": row["share"]}
        for row in data["price_bands"]
    ]
    columns = [
        {"key": "band", "title": "价格带"},
        {"key": "gmv", "title": "GMV", "format": "wan"},
        {"key": "share", "title": "占比", "format": "percent"},
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_product_top(connection, params) -> dict:
    """面板4c Top10 单品（table）：``fetch_product_structure`` top_products。"""
    from common.broadcast.queries import fetch_product_structure

    anchor = _hz_anchor(params)
    data = fetch_product_structure(connection, today=anchor)
    rows = [
        {
            "goods_name": row["goods_name"] or row["spec_no"],
            "series_name": row["series_name"],
            "gmv": row["gmv"],
            "share": row["share"],
        }
        for row in data["top_products"]
    ]
    columns = [
        {"key": "goods_name", "title": "商品"},
        {"key": "series_name", "title": "系列"},
        {"key": "gmv", "title": "GMV", "format": "wan"},
        {"key": "share", "title": "占比", "format": "percent"},
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_product_summary(connection, params) -> dict:
    """面板4d 商品窗口汇总（table）：``fetch_product_structure`` 单行集中度。"""
    from common.broadcast.queries import fetch_product_structure

    anchor = _hz_anchor(params)
    data = fetch_product_structure(connection, today=anchor)
    rows = [{
        "total_gmv": data["total_gmv"],
        "top10_share": data["top10_share"],
        "hhi": data["hhi"],
    }]
    columns = [
        {"key": "total_gmv", "title": "窗口GMV", "format": "wan"},
        {"key": "top10_share", "title": "Top10占比", "format": "percent"},
        {"key": "hhi", "title": "HHI", "format": "percent"},
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_refund_reason(connection, params) -> dict:
    """面板5 退款原因（table）：``fetch_refund_reason`` reasons。"""
    from common.broadcast.queries import fetch_refund_reason

    anchor = _hz_anchor(params)
    data = fetch_refund_reason(connection, today=anchor)
    rows = [
        {
            "reason": row["reason"],
            "amount": row["amount"],
            "orders": row["orders"],
            "share": row["share"],
        }
        for row in data["reasons"]
    ]
    columns = [
        {"key": "reason", "title": "原因"},
        {"key": "amount", "title": "退款金额", "format": "wan"},
        {"key": "orders", "title": "退款单数", "format": "number"},
        {"key": "share", "title": "占比", "format": "percent"},
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_gmv_factors(connection, params) -> dict:
    """面板6 GMV 三因子拆解（table）：``fetch_gmv_factors`` 输出直映射。"""
    from common.broadcast.queries import fetch_gmv_factors

    anchor = _hz_anchor(params)
    data = fetch_gmv_factors(connection)
    rows = []
    for month in data["months"]:
        contrib = month["contrib"] or {}
        rows.append({
            "ym": month["ym"],
            "gmv": month["gmv"],
            "buyers": month["buyers"],
            "qty": month["qty"],
            "qty_per_buyer": month["qty_per_buyer"],
            "unit_price": month["unit_price"],
            "c_buyers": contrib.get("buyers"),
            "c_qty_per_buyer": contrib.get("qty_per_buyer"),
            "c_unit_price": contrib.get("unit_price"),
        })
    columns = [
        {"key": "ym", "title": "月份"},
        {"key": "gmv", "title": "GMV", "format": "wan"},
        {"key": "buyers", "title": "买家数", "format": "number"},
        {"key": "qty", "title": "件数", "format": "number"},
        {"key": "qty_per_buyer", "title": "人均件数", "format": "ratio"},
        {"key": "unit_price", "title": "件均价", "format": "number"},
        {"key": "c_buyers", "title": "买家数贡献", "format": "wan"},
        {"key": "c_qty_per_buyer", "title": "人均件数贡献", "format": "wan"},
        {"key": "c_unit_price", "title": "件均价贡献", "format": "wan"},
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_cohort(connection, params) -> dict:
    """面板7 Cohort 留存矩阵（table）：``fetch_cohort_retention`` + 加权平均行。"""
    from common.broadcast.queries import fetch_cohort_retention

    anchor = _hz_anchor(params)
    data = fetch_cohort_retention(connection, today=anchor)
    periods = data["periods"]
    columns = [
        {"key": "cohort", "title": "首购月"},
        {"key": "size", "title": "队列规模", "format": "number"},
    ]
    columns += [
        {"key": f"m{k}", "title": f"M{k}", "format": "percent"}
        for k in periods
    ]
    rows = [
        {
            "cohort": row["cohort"],
            "size": row["size"],
            **{f"m{k}": row["retention"].get(k) for k in periods},
        }
        for row in data["cohorts"]
    ]
    rows.append({
        "cohort": "加权平均",
        "size": None,
        **{f"m{k}": data["avg_retention"].get(k) for k in periods},
    })
    return _qd_table(columns, rows, anchor)


def run_table_qd_channel_daily(connection, params) -> dict:
    """面板9 渠道日销快报（table）：channel_daily 三函数 + 店铺粒度月目标。

    ``channel_date`` = resolve_channel_business_date(month_facts, anchor)（锚点
    之前最近一个有数日）；无有效日 → 空表。月累计按自然日（≤ channel_date，
    含周末，对齐 AI 表仪表盘口径），环比按上一工作日——与旧页 collect_channel_rows
    同源，此处只把文本列还原为数值，不重新推导 sales/mom。
    """
    from common.daily_robot.channel_daily import (
        collect_channel_rows,
        fetch_channel_month_facts,
        resolve_channel_business_date,
    )
    from common.metrics.daily_report import fetch_channel_monthly_targets

    anchor = _hz_anchor(params)
    month_facts = fetch_channel_month_facts(
        connection, year=anchor.year, month=anchor.month
    )
    columns = [
        {"key": "channel", "title": "渠道"},
        {"key": "sales", "title": "销售额", "format": "wan"},
        {"key": "mom", "title": "环比", "format": "percent"},
        {"key": "target", "title": "月目标", "format": "wan"},
        {"key": "rate", "title": "达成率", "format": "percent"},
    ]
    channel_date = resolve_channel_business_date(month_facts, anchor)
    if channel_date is None:
        return _qd_table(columns, [], anchor)
    first_day, last_day = month_bounds(anchor.strftime("%Y-%m"))
    workdays = _window_workdays(connection, first_day, last_day)
    monthly_targets = fetch_channel_monthly_targets(connection) or {}
    _total, rows = collect_channel_rows(
        month_facts=month_facts,
        business_date=channel_date,
        workdays=workdays,
        monthly_targets=monthly_targets,
    )
    # 全渠道合计不进表体：前端对 wan 列自动加合计 footer，再叠一行合计会翻倍；
    # 合计改在「口径说明」卡里以文字给出（与 collect_channel_rows 同源 total）。
    out = []
    for row in rows:
        target = monthly_targets.get(row["channel"])
        mtd = sum(
            value
            for day, value in month_facts.get(row["channel"], {}).items()
            if day <= channel_date
        )
        out.append({
            "channel": row["channel"],
            "sales": row["sales"],
            "mom": row["mom"],
            "target": target,
            "rate": (mtd / target) if target else None,
        })
    return _qd_table(columns, out, anchor)


def run_table_qd_hot_items(connection, params) -> dict:
    """面板10 热卖品监控（table）：``fetch_hot_items`` items（状态转中文）。"""
    from common.broadcast.queries import fetch_hot_items

    anchor = _hz_anchor(params)
    data = fetch_hot_items(connection, today=anchor)
    state_map = {"OVERSOLD": "断货", "URGENT": "偏低"}
    rows = [
        {
            "goods_name": item["goods_name"],
            "spec_no": item["spec_no"],
            "available_qty": item["available_qty"],
            "days_left": item["days_left"],
            "qty_30d": item["qty_30d"],
            "purchase_intransit_qty": item["purchase_intransit_qty"],
            "state": state_map.get(item["stock_state"], "正常"),
        }
        for item in data["items"]
    ]
    columns = [
        {"key": "goods_name", "title": "货品名称"},
        {"key": "spec_no", "title": "编码"},
        {"key": "available_qty", "title": "可用库存", "format": "number"},
        {"key": "days_left", "title": "可售天数", "format": "number"},
        {"key": "qty_30d", "title": "30天销量", "format": "number"},
        {"key": "purchase_intransit_qty", "title": "在途", "format": "number"},
        {"key": "state", "title": "状态"},
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_stock_urgent(connection, params) -> dict:
    """面板11a 库存紧急补货（table）：``fetch_stock_alerts`` urgent。"""
    from common.broadcast.queries import fetch_stock_alerts

    anchor = _hz_anchor(params)
    data = fetch_stock_alerts(connection, today=anchor)
    rows = [
        {
            "goods_name": row["goods_name"],
            "spec_no": row["spec_no"],
            "available_qty": row["available_qty"],
            "daily_avg": row["daily_avg"],
            "days_left": row["days_left"],
        }
        for row in data["urgent"]
    ]
    columns = [
        {"key": "goods_name", "title": "货品名称"},
        {"key": "spec_no", "title": "编码"},
        {"key": "available_qty", "title": "可用库存", "format": "number"},
        {"key": "daily_avg", "title": "日销", "format": "number"},
        {"key": "days_left", "title": "可售天数", "format": "number"},
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_stock_oversold(connection, params) -> dict:
    """面板11b 超卖/零库存在售（table）：``fetch_stock_alerts`` oversold+zero_stock。"""
    from common.broadcast.queries import fetch_stock_alerts

    anchor = _hz_anchor(params)
    data = fetch_stock_alerts(connection, today=anchor)
    rows = [
        {
            "goods_name": row["goods_name"],
            "spec_no": row["spec_no"],
            "qty": max(0.0, -float(row["available_qty"] or 0)),
            "daily_avg": row["daily_avg"],
            "state": "超卖",
        }
        for row in data["oversold"]
    ]
    rows += [
        {
            "goods_name": row["goods_name"],
            "spec_no": row["spec_no"],
            "qty": 0,
            "daily_avg": row["daily_avg"],
            "state": "零库存在售",
        }
        for row in data["zero_stock"]
    ]
    columns = [
        {"key": "goods_name", "title": "货品名称"},
        {"key": "spec_no", "title": "编码"},
        {"key": "qty", "title": "数量", "format": "number"},
        {"key": "daily_avg", "title": "日销", "format": "number"},
        {"key": "state", "title": "状态"},
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_purchase_inbound(connection, params) -> dict:
    """面板12 采购入库（table）：``fetch_purchase_inbound(now=真实时钟「近24小时」)``。

    取数用 datetime.now()（旧页口径），忽略「日期」筛选；该口径在 notes 卡明示。
    """
    from common.broadcast.queries import fetch_purchase_inbound

    anchor = _hz_anchor(params)
    data = fetch_purchase_inbound(connection, now=datetime.now())
    rows = [
        {
            "warehouse": group["warehouse_name"],
            "spec_no": spec["spec_no"],
            "goods_name": spec["goods_name"],
            "qty": spec["qty"],
        }
        for group in data["groups"]
        for spec in group["specs"]
    ]
    columns = [
        {"key": "warehouse", "title": "仓库"},
        {"key": "spec_no", "title": "编码"},
        {"key": "goods_name", "title": "货品名称"},
        {"key": "qty", "title": "数量", "format": "number"},
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_order_risk(connection, params) -> dict:
    """面板13 订单风险防控（table）：``fetch_order_risk`` 锚点前一日分组展开。"""
    from common.broadcast.queries import fetch_order_risk

    anchor = _hz_anchor(params)
    data = fetch_order_risk(
        connection, business_date=anchor - timedelta(days=1)
    )
    def _title(group):
        # 与旧面板一致：platform_of 未识别时兜底返回店名本身，
        # 避免「店名-店名」重复显示（qudao_panels.build_order_risk_panel）。
        if group["platform"] == group["shop_name"]:
            return group["shop_name"]
        return f'{group["platform"]}-{group["shop_name"]}'

    rows = []
    for group in data["groups"]:
        for area in group["areas"]:
            rows.append({
                "shop": _title(group),
                "area": area["area"],
                "count": area["count"],
                "mark": "",
            })
    for group in data["incomplete_groups"]:
        for area in group["areas"]:
            rows.append({
                "shop": _title(group),
                "area": area["area"],
                "count": area["count"],
                "mark": "地区字段不全（仅供参考）",
            })
    columns = [
        {"key": "shop", "title": "店铺 / 平台"},
        {"key": "area", "title": "重点地区"},
        {"key": "count", "title": "异常单数", "format": "number"},
        {"key": "mark", "title": "标记"},
    ]
    return _qd_table(columns, rows, anchor)


def run_table_qd_notes(connection, params) -> dict:
    """面板说明卡（table）：13 个旧面板口径/数据缺口说明（逐字迁移）+ 补充。

    说明文本与 `common/broadcast/qudao_panels.py` 的 ``note=`` 披露逐字一致
    （动态片段用既有 fetch_* 结果回填）；另补：面板2 未挂渠道/财务未填/充值口径、
    面板9 T-1 采集、面板12 真实时钟忽略日期筛选、面板13 阈值与拼多多无地区单数。
    """
    from common.broadcast import queries as broadcast_queries
    from common.daily_robot.channel_daily import (
        fetch_channel_month_facts,
        resolve_channel_business_date,
    )

    anchor = _hz_anchor(params)
    notes = []

    notes.append(("月度核心指标", (
        "口径：GMV=已付款未取消(trade_status∉4/5/24) Σ paid_amount 按付款时间归月 · "
        "活跃买家=buyer_id 去重、新客=首购当月、复购率=(活跃−新客)÷活跃 · "
        "取消率=取消单÷(付款订单+取消单)，按下单时间归月 · "
        "退款=actual_refund_amount 按入库审核时间归月；净额=GMV−退款 · "
        "推广=promotion_cost"
    )))

    expense = broadcast_queries.fetch_expense_summary(connection, today=anchor)
    notes.append(("费用/盈利", (
        "口径：店铺费用=fin_store_commission（应缴/实缴待财务确认）· "
        "推广=fin_ecommerce_promotion_recharge_balance（充值/余额口径待确认）· "
        "费率=费用合计÷当月GMV（未挂渠道=挂接种子未命中的财务店铺）"
    )))
    expense_extra = ["推广充值费已计入费用合计（充值/余额口径待财务确认）"]
    if expense["empty_months"]:
        expense_extra.append("⚠️ 财务未填：" + "、".join(expense["empty_months"]))
    if expense["unmapped_stores"]:
        shown = "、".join(expense["unmapped_stores"][:6])
        more = (
            f" 等{len(expense['unmapped_stores'])}店"
            if len(expense["unmapped_stores"]) > 6 else ""
        )
        expense_extra.append(
            "未挂渠道店铺（历史关店/渠道外新店/待归，不计入在营渠道费率）："
            f"{shown}{more}（分类证据见 seeds/store_channel_mapping.json）"
        )
    notes.append(("费用/盈利·补充", " · ".join(expense_extra)))

    bill = broadcast_queries.fetch_bill_breakdown(connection, today=anchor)
    bill_notes = [
        "数据源：平台账单（钉钉表「电商—店铺扣点费用管理」→ fin_store_commission"
        " → fact_fin_store_expense，source=store_commission）",
        "费用类别（店铺扣点/开票填写负数）对应应缴/实缴口径，待财务确认",
    ]
    if bill["empty_months"]:
        bill_notes.append("⚠️ 财务未填：" + "、".join(bill["empty_months"]))
    bill_notes.append(
        "数据缺失：推广类型明细（关键词推广/人群推广/货品全站推）不在账单粒度内，"
        "需接平台推广后台 API，本期不臆造"
    )
    notes.append(("账单费用拆解", " · ".join(bill_notes)))

    notes.append(("商品结构", (
        "口径：近6月 fact_order_line 按付款时间（trade_time），"
        "系列=dim_product.series_name（NULL→未分类）；"
        "价格带按件均价=paid_amount÷quantity 实付口径分档；"
        "集中度=Top10 单品 GMV 占比 / HHI"
    )))

    notes.append(("退款原因", (
        "口径：fact_refund_line.actual_refund_amount 按入库审核时间(check_time)归月，"
        "与月度看板退款同口径；「未填写」为原因空白的退款单"
    )))

    notes.append(("GMV 三因子", (
        "口径：GMV=Σpaid_amount（剔除取消单，付款时间归月）· "
        "买家数=COUNT(DISTINCT buyer_id) · 件数=Σquantity · "
        "人均件数=件数÷买家数 · 件均价=GMV÷件数 · "
        "恒等式 GMV=买家数×人均件数×件均价 · "
        "环比贡献=连环替代法（买家数贡献=Δ买家数×上期人均件数×上期件均价；"
        "人均件数贡献=本期买家数×Δ人均件数×上期件均价；"
        "件均价贡献=本期买家数×本期人均件数×Δ件均价；三项和=ΔGMV）· "
        "买家ID缺失行（拼多多按隐私协议不返回）不计入买家数，"
        "会高估人均件数/件均价"
    )))

    cohort = broadcast_queries.fetch_cohort_retention(connection, today=anchor)
    notes.append(("Cohort 留存", (
        "口径：队列=买家首购月（MIN(trade_time) 所在月）· "
        "留存率(Mk)=首购后第 k 月仍有成交的队列买家数÷队列规模（M0 恒 100%）· "
        f"列覆盖至数据最新月（{cohort['last_ym'] or '—'}）· "
        "加权平均=Σ已满 k 月队列当月活跃买家÷Σ已满 k 月队列规模"
        "（未满 k 月的队列不计入）· "
        "无 buyer_id 行（拼多多按隐私协议）不入队列，不臆造"
    )))

    month_facts = fetch_channel_month_facts(
        connection, year=anchor.year, month=anchor.month
    )
    channel_date = resolve_channel_business_date(month_facts, anchor)
    if channel_date is None:
        notes.append(("渠道日销快报", "本月暂无非零渠道日销数据"))
    else:
        # 全渠道合计（= collect_channel_rows 的 total：当日各渠道非空值之和），
        # 不进表体、以文字给出，避免前端 wan 列合计 footer 重复计一次。
        channel_total = 0.0
        for facts in month_facts.values():
            value = facts.get(channel_date)
            if value is not None:
                channel_total += value
        amount_txt = (
            f"{channel_total / 10000:.1f}万"
            if abs(channel_total) >= 10000 else f"{channel_total:,.0f}"
        )
        notes.append(("渠道日销快报", (
            f"全渠道 {channel_date.month}月{channel_date.day}日销售额"
            f" {amount_txt} 元 · 数据截至 {channel_date.month}月"
            f"{channel_date.day}日（T+1 10:31 采集 · 店铺后台导出 · DB）"
        )))
    notes.append(("渠道日销快报·采集", (
        "采集窗口 T-1 10:31（锚点业务日之前最近一个有数日）；"
        "昨日未到齐则回退更早业务日并标注延迟"
    )))

    hot = broadcast_queries.fetch_hot_items(connection, today=anchor)
    hot_window = hot.get("moving_window_days") or 15
    notes.append(("热卖品监控", (
        f"可售/状态按近{hot_window}天动销"
        f"（「—」= 近{hot_window}天无动销），30天销为近30天窗口；"
        "只监控酒类（赠品/服务卡/包材等非商品行剔除）"
    )))

    stock = broadcast_queries.fetch_stock_alerts(connection, today=anchor)
    stock_window = stock.get("moving_window_days") or 15
    notes.append(("库存补货提醒", (
        f"动销窗口 近{stock_window}天 · 已剔除赠品/服务卡等非商品行 · "
        "OVERSOLD 拆为真超卖（可售<0）与零库存在售两档"
    )))

    now = datetime.now()
    purchase = broadcast_queries.fetch_purchase_inbound(connection, now=now)
    total_qty = purchase["total_qty"]
    qty_txt = (
        f"{total_qty:,.0f}" if total_qty == int(total_qty)
        else f"{total_qty:,.1f}"
    )
    notes.append(("采购入库（近24小时）", (
        f"共 {purchase['total_lines']} 条明细"
        f"（{purchase['total_specs']} 个SKU），"
        f"{purchase['total_orders']} 张采购单，合计 {qty_txt} 件"
        f" · 数据截至 {now.strftime('%m-%d %H:%M')}"
    )))
    notes.append(("采购入库·时钟", (
        "本卡取数用真实时钟的「近24小时」口径，忽略页面「日期」筛选"
    )))

    risk = broadcast_queries.fetch_order_risk(
        connection, business_date=anchor - timedelta(days=1)
    )
    notes.append(("订单风险防控", (
        "口径：按付款时间，排除已取消订单；同地区定义为省市区三级归一"
    )))
    risk_extra = f'同店铺+同地区同日 ≥{risk["threshold"]} 单判定风险'
    if risk["pdd_no_area_count"]:
        risk_extra += (
            " · ⚠️ 拼多多地区字段受隐私协议限制"
            f"（{risk['pdd_no_area_count']}单未计入）"
        )
    notes.append(("订单风险·阈值", risk_extra))

    columns = [
        {"key": "section", "title": "板块"},
        {"key": "note", "title": "口径与数据缺口说明"},
    ]
    rows = [{"section": section, "note": note} for section, note in notes]
    return _qd_table(columns, rows, anchor)

__all__ = ('_CHANNEL_MONTH_SUM_SQL', '_CHANNEL_DAY_SQL', 'run_kpi_qd_mtd', 'run_kpi_qd_today', 'run_kpi_qd_progress', 'run_trend_qd_daily', 'run_bar_qd_channel', 'run_table_qd_store_mtd', '_qd_unfilled_payload', 'run_table_qd_unfilled_today', '_qd_table', 'run_table_qd_monthly_kpi', 'run_table_qd_expense', 'run_table_qd_expense_rate', 'run_table_qd_bill_category', 'run_table_qd_bill_platform', 'run_table_qd_bill_items', 'BILL_MANUAL_DATASET', 'BILL_MANUAL_METRIC', '_bill_manual_period', '_bill_manual_rows', 'run_table_qd_bill_manual', 'run_table_qd_bill_manual_detail', 'run_table_qd_product_series', 'run_table_qd_product_bands', 'run_table_qd_product_top', 'run_table_qd_product_summary', 'run_table_qd_refund_reason', 'run_table_qd_gmv_factors', 'run_table_qd_cohort', 'run_table_qd_channel_daily', 'run_table_qd_hot_items', 'run_table_qd_stock_urgent', 'run_table_qd_stock_oversold', 'run_table_qd_purchase_inbound', 'run_table_qd_order_risk', 'run_table_qd_notes')
