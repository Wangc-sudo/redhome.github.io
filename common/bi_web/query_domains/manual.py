"""BI manual 查询域；SQL 与返回契约自原 queries.py 等价迁移。"""

from common.bi_web.query_domains.common import *


_MANUAL_REPORT_ROWS_SQL = (
    "SELECT dim_scope, dimension_value, metric, value, unit "
    "FROM fact_manual_report "
    "WHERE dataset = %s AND period_start = %s"
)


_MANUAL_MONTHLY_DATASETS = {
    "ecommerce_monthly": {"dim_scope": "channel", "dim_label": "渠道"},
    "restaurant_monthly": {"dim_scope": "store", "dim_label": "门店"},
    # ⑪ 体验馆月报（克隆 ⑤ 模式）：门店粒度，维度标题「体验馆」。
    "showroom_monthly": {"dim_scope": "store", "dim_label": "体验馆"},
}


_MANUAL_METRIC_LABELS = {
    "revenue": "收入",
    "gross_profit": "毛利",
    "expense": "费用",
}


def manual_report_rows(connection, *, dataset, period_start) -> list:
    """只读事实：某 dataset 某期间的窄行列表（契约 §3.2）。

    ``period_start`` 由调用方按 period_type 推导（month → 月首，复用
    ``month_bounds`` 的日历口径）。返回
    ``[{dim_scope, dimension_value, metric, value(Decimal|None), unit}]``，
    **不含任何派生列**（同 ``shortfall_facts`` 的可测边界）。
    """
    return _fetch_rows(
        connection, _MANUAL_REPORT_ROWS_SQL, (dataset, period_start)
    )


def pivot_manual_rows(rows, *, dim_scope) -> list:
    """窄行 → 宽行：``[{name, <metric>: Decimal|None, ...}]``（契约 §3.3）。

    * 只保留 ``dim_scope`` 匹配的行（防御多维模板混入）；
    * 同一 ``(dimension_value, metric)`` 在表内唯一（唯一键保证），直接
      取值，**绝不 SUM**（无 melt 陷阱）；
    * 缺失指标 → **键不存在**（消费方按 None 处理，显示「—」）；``value``
      为 NULL 的行保留 ``None``，不补 0；
    * 保持 ``Decimal``，``float`` 转换只在 ``run_*`` 边界做。
    """
    grouped = {}
    order = []
    for row in rows:
        if row.get("dim_scope") != dim_scope:
            continue
        name = row.get("dimension_value")
        metric = row.get("metric")
        if name is None or metric is None:
            continue
        if name not in grouped:
            grouped[name] = {"name": name}
            order.append(name)
        grouped[name][metric] = row.get("value")
    return [grouped[name] for name in order]


def _manual_monthly_payload(connection, *, dataset, month) -> dict:
    """④⑤ 月度经营对比表载荷：维度 × 指标（动态展开）+ 毛利率派生列。

    指标列由当月实际出现的 metric 集合驱动（模板加列 → 卡片自动多一列）；
    毛利率列只在 revenue 与 gross_profit 同时存在时出现，派生走
    :func:`derived.margin`（分母 0/None → None，不静默补 0）。
    """
    meta = _MANUAL_MONTHLY_DATASETS[dataset]
    first_day, _last_day = month_bounds(month)
    rows = manual_report_rows(connection, dataset=dataset, period_start=first_day)
    wide = pivot_manual_rows(rows, dim_scope=meta["dim_scope"])
    metrics = []
    for row in wide:
        for key in row:
            if key != "name" and key not in metrics:
                metrics.append(key)
    columns = [{"key": "name", "title": meta["dim_label"]}]
    columns.extend(
        {
            "key": metric,
            "title": _MANUAL_METRIC_LABELS.get(metric, metric),
            "format": "wan",
        }
        for metric in metrics
    )
    with_margin = {"revenue", "gross_profit"} <= set(metrics)
    if with_margin:
        columns.append({"key": "margin", "title": "毛利率", "format": "percent"})
    table_rows = []
    for row in wide:
        out = {"name": row["name"]}
        for metric in metrics:
            value = row.get(metric)
            out[metric] = None if value is None else float(value)
        if with_margin:
            out["margin"] = derived.margin(
                row.get("gross_profit"), row.get("revenue")
            )
        table_rows.append(out)
    return {
        "chart": "table",
        "unit": _UNIT,
        "month": month,
        "dataset": dataset,
        "columns": columns,
        "rows": table_rows,
    }


def run_table_manual_ecommerce_monthly(connection, params) -> dict:
    """④ 电商月度经营表: dataset 固化 ecommerce_monthly，渠道×指标+毛利率。

    params_schema 为空（month 缺省当前月）：月报整月一次导入，历史月
    不做 CURDATE 截断；month 参数待月份值域重叠确认后追加（见
    ``_MANUAL_MONTHLY_DATASETS`` 注释）。
    """
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    return _manual_monthly_payload(
        connection, dataset="ecommerce_monthly", month=month
    )


def run_table_manual_restaurant_monthly(connection, params) -> dict:
    """⑤ 餐饮月度经营表: dataset 固化 restaurant_monthly，门店×指标+毛利率。

    餐饮板块无系统采集源（人工模板是唯一进库通道），其余口径与
    :func:`run_table_manual_ecommerce_monthly` 相同。
    """
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    return _manual_monthly_payload(
        connection, dataset="restaurant_monthly", month=month
    )


def run_table_manual_showroom_monthly(connection, params) -> dict:
    """⑪ 体验馆月度经营表: dataset 固化 showroom_monthly（克隆 ⑤ 模式）。

    维度粒度与餐饮月报同为门店（dim_scope=store），列标题「体验馆」；
    未首填则自然挂零（空行 + 仅维度列）。params_schema 留空，与 ④⑤
    同裁决（month 参数待月份值域重叠确认后追加）。
    """
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    return _manual_monthly_payload(
        connection, dataset="showroom_monthly", month=month
    )

__all__ = ('_MANUAL_REPORT_ROWS_SQL', '_MANUAL_MONTHLY_DATASETS', '_MANUAL_METRIC_LABELS', 'manual_report_rows', 'pivot_manual_rows', '_manual_monthly_payload', 'run_table_manual_ecommerce_monthly', 'run_table_manual_restaurant_monthly', 'run_table_manual_showroom_monthly')
