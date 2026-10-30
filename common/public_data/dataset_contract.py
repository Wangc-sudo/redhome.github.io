"""版本化数据集契约：新鲜度、对账和自动恢复共用的注册中心。"""

import re
from dataclasses import dataclass, field
from datetime import time
from typing import Optional


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class DatasetContract:
    dataset_key: str
    owner: str
    source: str
    schedule: str
    expected_by: time
    max_lag_minutes: int = 60
    business_date_column: Optional[str] = None
    business_key_columns: tuple = ()
    expected_scope: Optional[str] = None
    dq_rules: tuple = ()
    reconcile_rules: tuple = ()
    downstream_consumers: tuple = ()
    auto_retry: bool = True
    auto_catchup: bool = True
    auto_replay: bool = True
    kind: str = "business"
    enabled: bool = True
    version: int = 1
    target_table: Optional[str] = None
    raw_dataset: Optional[str] = None
    required_window: Optional[str] = None
    expected_day: int = 1
    extra: dict = field(default_factory=dict)


def _t(hour, minute=0):
    return time(hour=hour, minute=minute)


def _contract(key, owner, source, target, *, expected=_t(7), lag=120,
              date_column=None, keys=(), raw=None, window=None, consumers=(),
              dq=False, count=False, schedule="0 4,8,15,17 * * *", scope=None):
    return DatasetContract(
        dataset_key=key,
        owner=owner,
        source=source,
        target_table=target,
        schedule=schedule,
        expected_by=expected,
        max_lag_minutes=lag,
        business_date_column=date_column,
        business_key_columns=tuple(keys),
        raw_dataset=raw,
        required_window=window,
        expected_scope=scope or "all",
        downstream_consumers=tuple(consumers),
        dq_rules=("latest_quality_gate",) if dq else (),
        reconcile_rules=("record_count",) if count else ("run_summary",),
    )


_BUSINESS_CONTRACTS = (
    _contract(
        "daily_report_offline", "offline_ops", "dingtalk",
        "fact_daily_report_offline", date_column="business_date",
        keys=("region", "responsible_person", "business_date"),
        raw="daily_report_offline", window="t1",
        consumers=("pages-offline_all", "leaderboard-offline_all"), count=True,
        scope="regions:hangzhou,shaoxing,vanke,other",
    ),
    _contract(
        "channel_daily_sales", "channel_ops", "dingtalk",
        "fact_channel_daily_sales", expected=_t(10, 40), lag=60,
        date_column="business_date",
        keys=("channel", "store_name", "business_date"),
        raw="channel_daily_sales%", window="t1",
        consumers=("pages-qudao-t1", "channel-order-daily"),
        dq=True, count=True, schedule="35 10 * * *",
    ),
    _contract(
        "channel_monthly_target", "channel_ops", "dingtalk",
        "fact_channel_store_target", expected=_t(10, 40), lag=60,
        keys=("store_name", "channel"), raw="channel_monthly_target",
        consumers=("pages-qudao", "channel_daily_sales"),
        schedule="35 10 * * *",
    ),
    _contract(
        "fin_offline_receivables_aging", "finance", "dingtalk",
        "fact_fin_receivables_aging", date_column="updated_date",
        keys=("counterparty_name", "company_entity"),
        raw="fin_offline_receivables_aging", count=True,
    ),
    _contract(
        "fin_ecommerce_prepayment_supplier_invoice", "finance", "dingtalk",
        "fact_fin_prepayment_invoice", date_column="statement_date_raw",
        keys=("supplier_name", "company_entity"),
        raw="fin_ecommerce_prepayment_supplier_invoice", count=True,
    ),
    _contract(
        "fin_offline_deposit_other_receivables", "finance", "dingtalk",
        "fact_fin_offline_deposit", date_column="updated_at",
        keys=("company_entity", "supplier_name", "project_name"),
        raw="fin_offline_deposit_other_receivables", count=True,
    ),
    _contract(
        "fin_ecommerce_platform_deposit", "finance", "dingtalk",
        "fact_fin_platform_deposit",
        keys=("company_entity", "platform", "store_name", "project_name"),
        raw="fin_ecommerce_platform_deposit", count=True,
    ),
    _contract(
        "fin_ecommerce_store_funds_balance", "finance", "dingtalk",
        "fact_fin_store_funds", date_column="month",
        keys=("store_name", "channel", "month"),
        raw="fin_ecommerce_store_funds_balance",
    ),
    _contract(
        "fin_store_expense", "finance", "dingtalk", "fact_fin_store_expense",
        date_column="month", keys=("fin_store_name", "fin_platform", "fee_item", "month"),
        raw="fin_store_commission",
    ),
    _contract(
        "wdt_dim_product_mirror", "ecommerce_ops", "wdt", "dim_product",
        keys=("spec_no",), raw="wdt_goods_catalog%",
    ),
    _contract(
        "wdt_order_line_fact", "ecommerce_ops", "wdt", "fact_order_line",
        date_column="trade_time", keys=("trade_no", "line_no"),
        raw="wdt_trade_detail%", consumers=("channel-order-daily",), dq=True,
    ),
    _contract(
        "wdt_order_cancel_fact", "ecommerce_ops", "wdt",
        "fact_order_cancel_line", date_column="cancel_time", keys=("trade_no",),
        raw="wdt_trade_detail%",
    ),
    _contract(
        "wdt_stockout_line_fact", "ecommerce_ops", "wdt", "fact_stockout_line",
        date_column="consign_time", keys=("order_no", "line_no"),
        raw="wdt_stockout_sales%", consumers=("wdt_sales_daily",),
    ),
    _contract(
        "wdt_refund_line_fact", "ecommerce_ops", "wdt", "fact_refund_line",
        date_column="check_time", keys=("order_no", "line_no"),
        raw="wdt_stockin_refund%",
    ),
    _contract(
        "wdt_sales_daily", "ecommerce_ops", "derived", "fact_sales_daily",
        date_column="business_date", keys=("business_date", "spec_no", "warehouse_no"),
    ),
    _contract(
        "wdt_inventory_sku_daily", "ecommerce_ops", "wdt",
        "fact_inventory_sku_daily", date_column="business_date",
        keys=("business_date", "spec_no", "warehouse_scope"), raw="wdt_stock_specs%",
    ),
    _contract(
        "wdt_purchase_inbound", "ecommerce_ops", "wdt", "fact_purchase_inbound",
        date_column="stockin_time", keys=("order_no", "spec_no"),
        raw="wdt_purchase_orders%",
    ),
    _contract(
        "dim_calendar", "ops", "derived", "dim_calendar",
        date_column="business_date", keys=("business_date",),
        consumers=("all_business_lines",),
    ),
    _contract(
        "dim_robot_member", "ops", "dingtalk", "dim_robot_member",
        keys=("user_id",), consumers=("all_business_lines",),
    ),
    _contract(
        "dim_dept_leader", "ops", "dingtalk", "dim_dept_leader",
        keys=("dept_id", "user_id"), consumers=("all_business_lines",),
    ),
    _contract(
        "ecom_people", "channel_ops", "derived", "fact_daily_report_offline",
        date_column="business_date",
        keys=("region", "responsible_person", "business_date"),
        window="t1", consumers=("pages-qudao",), scope="regions:qudao",
    ),
)


_MANUAL_CONTRACTS = tuple(
    DatasetContract(
        dataset_key=key,
        owner=owner,
        source="manual",
        target_table="fact_manual_report",
        schedule="manual monthly import",
        expected_by=_t(12),
        expected_day=5,
        max_lag_minutes=1440,
        business_date_column="period_end",
        business_key_columns=keys,
        required_window="previous_month",
        downstream_consumers=("bi-web",),
        auto_retry=False,
        auto_catchup=False,
        kind="manual",
    )
    for key, owner, keys in (
        ("ecommerce_monthly", "channel_ops", ("period_start", "dim_scope", "dimension_value", "metric")),
        ("restaurant_monthly", "restaurant_ops", ("period_start", "dim_scope", "dimension_value", "metric")),
        ("platform_bill_monthly", "finance", ("period_start", "dim_scope", "dimension_value", "metric")),
    )
)


_KNOWLEDGE_CONTRACTS = (
    DatasetContract(
        dataset_key="kb_doc_bm25", owner="kb_ops", source="wiki",
        schedule="17 3 * * *", expected_by=_t(4), max_lag_minutes=180,
        kind="knowledge", downstream_consumers=("kb-gateway",),
    ),
    DatasetContract(
        dataset_key="kb_doc_vector", owner="kb_ops", source="wiki",
        schedule="17 3 * * *", expected_by=_t(4), max_lag_minutes=180,
        kind="knowledge", downstream_consumers=("kb-gateway",),
    ),
    DatasetContract(
        dataset_key="kb_product_structured", owner="kb_ops", source="aitable",
        schedule="7 * * * *", expected_by=_t(1), max_lag_minutes=120,
        kind="knowledge", downstream_consumers=("kb-gateway",),
    ),
    DatasetContract(
        dataset_key="kb_product_text_vector", owner="kb_ops", source="aitable",
        schedule="47 3 * * *", expected_by=_t(4), max_lag_minutes=180,
        kind="knowledge", downstream_consumers=("kb-gateway",),
    ),
    DatasetContract(
        dataset_key="kb_product_image_vector", owner="kb_ops", source="aitable",
        schedule="47 3 * * *", expected_by=_t(4), max_lag_minutes=180,
        kind="knowledge", downstream_consumers=("kb-gateway",),
    ),
)


ALL_CONTRACTS = _BUSINESS_CONTRACTS + _MANUAL_CONTRACTS + _KNOWLEDGE_CONTRACTS
CONTRACT_BY_KEY = {contract.dataset_key: contract for contract in ALL_CONTRACTS}


_SERVICE_DATASETS = {
    "sync-dingtalk": (
        "daily_report_offline", "channel_daily_sales", "channel_monthly_target",
        "channel_member_base", "channel_store_member_history",
        "fin_offline_receivables_aging", "fin_offline_invoice_invoice",
        "fin_store_bankcard", "fin_store_expense",
    ),
    "sync-wdt": ("wdt_order_line_fact", "dim_product"),
    "extract-mart": (
        "daily_report_offline", "wdt_order_line_fact", "dim_product", "ecom_people",
        "fin_offline_receivables_aging", "fin_offline_invoice_invoice",
        "fin_store_bankcard", "fin_store_expense",
    ),
    "extract-channel": (
        "channel_daily_sales", "channel_monthly_target", "ecom_people",
    ),
    "kb-sync-wiki": ("kb_doc_bm25",),
    "rag-index-docs": ("kb_doc_vector",),
    "kb-sync-products": ("kb_product_structured",),
    "rag-index-products": ("kb_product_text_vector", "kb_product_image_vector"),
}


def service_recovery_allowed(service_id, action):
    """服务覆盖多个数据集时，只有全部契约允许才自动执行。"""
    if action not in {"auto_retry", "auto_catchup", "auto_replay"}:
        raise ValueError(f"unknown recovery action: {action}")
    dataset_keys = _SERVICE_DATASETS.get(service_id)
    if not dataset_keys:
        return True
    return all(
        getattr(CONTRACT_BY_KEY[dataset_key], action)
        for dataset_key in dataset_keys
        if dataset_key in CONTRACT_BY_KEY
    )


def validate_contracts(contracts=ALL_CONTRACTS):
    keys = [contract.dataset_key for contract in contracts]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate dataset_key")
    for contract in contracts:
        if not _IDENTIFIER.fullmatch(contract.dataset_key):
            raise ValueError(f"invalid dataset_key: {contract.dataset_key}")
        if contract.kind not in {"business", "manual", "knowledge"}:
            raise ValueError(f"invalid kind: {contract.dataset_key}")
        if not all((contract.owner, contract.source, contract.schedule)):
            raise ValueError(f"incomplete ownership or schedule: {contract.dataset_key}")
        if contract.kind == "business" and (
                not contract.target_table or not contract.business_key_columns):
            raise ValueError(f"incomplete business target or keys: {contract.dataset_key}")
        if any(not isinstance(getattr(contract, name), bool) for name in (
                "auto_retry", "auto_catchup", "auto_replay", "enabled")):
            raise ValueError(f"invalid recovery policy: {contract.dataset_key}")
        if contract.max_lag_minutes < 0 or not 1 <= contract.expected_day <= 28:
            raise ValueError(f"invalid freshness SLA: {contract.dataset_key}")
        for identifier in (contract.target_table, contract.business_date_column):
            if identifier is not None and not _IDENTIFIER.fullmatch(identifier):
                raise ValueError(f"invalid SQL identifier: {contract.dataset_key}")
        if any(not _IDENTIFIER.fullmatch(key) for key in contract.business_key_columns):
            raise ValueError(f"invalid business key: {contract.dataset_key}")
    return contracts


validate_contracts()


def get_contract(dataset_key: str) -> DatasetContract:
    return CONTRACT_BY_KEY[dataset_key]


def list_contracts(*, kind=None, enabled_only=True):
    contracts = ALL_CONTRACTS
    if kind is not None:
        contracts = tuple(contract for contract in contracts if contract.kind == kind)
    if enabled_only:
        contracts = tuple(contract for contract in contracts if contract.enabled)
    return contracts
