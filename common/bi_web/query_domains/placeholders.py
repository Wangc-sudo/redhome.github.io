"""BI placeholders 查询域；SQL 与返回契约自原 queries.py 等价迁移。"""

from common.bi_web.query_domains.common import *


def _placeholder_payload(columns) -> dict:
    """0 占位：has_fact=false，应接入未接入，不建临时表。"""
    return {
        "chart": "table",
        "columns": columns,
        "rows": [],
        "has_fact": False,
        "unit": _UNIT,
    }


def run_table_inventory_aging(connection, params) -> dict:
    """③⑦ 库存/库龄表（0 占位：has_fact=false，应接入未接入，不建临时表）。

    列结构参照需求表库存页（库龄分桶 0-90/91-180/181-365/>365，
    **非效期口径**）。
    """
    return _placeholder_payload(
        [
            {"key": "sku", "title": "SKU"},
            {"key": "warehouse", "title": "仓库"},
            {"key": "qty", "title": "库存量", "format": "number"},
            {"key": "amount", "title": "库存金额", "format": "wan"},
            {"key": "aging_0_90", "title": "库龄0-90天", "format": "wan"},
            {"key": "aging_91_180", "title": "库龄91-180天", "format": "wan"},
            {"key": "aging_181_365", "title": "库龄181-365天", "format": "wan"},
            {"key": "aging_over_365", "title": "库龄>365天", "format": "wan"},
            {"key": "severity", "title": "状态", "format": "severity"},
        ]
    )


def run_table_warehouse_ops(connection, params) -> dict:
    """⑧ 仓储运作表（0 占位：has_fact=false，应接入未接入，不建临时表）。

    列结构含超时六类原因（缺货待补/拣货积压/待揽收/打包瓶颈/系统故障/
    人力不足，原文透传）+ 48h/24h 分段 SLA。
    """
    return _placeholder_payload(
        [
            {"key": "order_no", "title": "订单号"},
            {"key": "channel", "title": "渠道"},
            {"key": "item", "title": "品项"},
            {"key": "reason", "title": "超时原因"},
            {"key": "hours", "title": "超时时长", "format": "number"},
            {"key": "sla_band", "title": "SLA分段"},
            {"key": "severity", "title": "状态", "format": "severity"},
        ]
    )


def run_table_quarter_budget_actual(connection, params) -> dict:
    """⑥ 季度预实表（0 占位：has_fact=false，应接入未接入，不建临时表）。

    三板块（线下/电商/餐饮）收入/毛利/费用的预算 vs 实际结构一次到位。
    """
    return _placeholder_payload(
        [
            {"key": "board", "title": "板块"},
            {"key": "revenue_budget", "title": "预算收入", "format": "wan"},
            {"key": "revenue_actual", "title": "实际收入", "format": "wan"},
            {"key": "revenue_rate", "title": "收入达成", "format": "percent"},
            {"key": "profit_budget", "title": "预算毛利", "format": "wan"},
            {"key": "profit_actual", "title": "实际毛利", "format": "wan"},
            {"key": "profit_rate", "title": "毛利达成", "format": "percent"},
            {"key": "expense_budget", "title": "预算费用", "format": "wan"},
            {"key": "expense_actual", "title": "实际费用", "format": "wan"},
            {"key": "expense_rate", "title": "费用执行", "format": "percent"},
        ]
    )


def run_table_yoy_monthly(connection, params) -> dict:
    """⑬ 月度同比表（0 占位：has_fact=false，应接入未接入，不建临时表）。

    同比率列 format 'pct'；无基数/除零 → null 显示「—」（口径见
    :func:`derived.yoy_rate`），绝不 0%/-100%。
    """
    return _placeholder_payload(
        [
            {"key": "board", "title": "板块"},
            {"key": "brand", "title": "品牌"},
            {"key": "current", "title": "当月", "format": "wan"},
            {"key": "previous", "title": "去年同月", "format": "wan"},
            {"key": "diff", "title": "增减额", "format": "wan"},
            {"key": "yoy", "title": "同比", "format": "pct"},
            {"key": "cum_yoy", "title": "1-当月累计同比", "format": "pct"},
            {"key": "severity", "title": "预警", "format": "severity"},
        ]
    )


def run_table_contract_writeoff(connection, params) -> dict:
    """⑨ 合同核销表（0 占位：has_fact=false，应接入未接入，不建临时表）。

    品牌 × 渠道核销进度表结构（合同/已核销/待核销/超期/进度/预警）。
    """
    return _placeholder_payload(
        [
            {"key": "brand", "title": "品牌"},
            {"key": "channel", "title": "渠道"},
            {"key": "total", "title": "合同金额", "format": "wan"},
            {"key": "done", "title": "已核销", "format": "wan"},
            {"key": "pending", "title": "待核销", "format": "wan"},
            {"key": "overdue", "title": "超期金额", "format": "wan"},
            {"key": "rate", "title": "核销进度", "format": "percent"},
            {"key": "severity", "title": "预警", "format": "severity"},
        ]
    )

__all__ = ('_placeholder_payload', 'run_table_inventory_aging', 'run_table_warehouse_ops', 'run_table_quarter_budget_actual', 'run_table_yoy_monthly', 'run_table_contract_writeoff')
