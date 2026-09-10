from dataclasses import dataclass


@dataclass(frozen=True)
class ColumnDefinition:
    name: str
    mysql_type: str
    source_type: str
    nullable: bool = True


@dataclass(frozen=True)
class IndexDefinition:
    name: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class TableDefinition:
    name: str
    business_columns: tuple[ColumnDefinition, ...]
    technical_columns: tuple[ColumnDefinition, ...]
    indexes: tuple[IndexDefinition, ...]

    @property
    def columns(self) -> tuple[ColumnDefinition, ...]:
        return self.business_columns + self.technical_columns


_TECHNICAL_COLUMNS = (
    ColumnDefinition("dingtalk_record_id", "VARCHAR(255)", "text", nullable=False),
    ColumnDefinition("synced_at", "DATETIME(6)", "", nullable=False),
    ColumnDefinition("sync_run_id", "CHAR(36)", "", nullable=False),
)


def _t(name, business, indexes):
    return TableDefinition(
        name=name,
        business_columns=tuple(business),
        technical_columns=_TECHNICAL_COLUMNS,
        indexes=tuple(indexes),
    )


def _c(name, mysql_type, source_type):
    return ColumnDefinition(name, mysql_type, source_type)


def _idx(name, *columns):
    return IndexDefinition(name, columns)


_FIN_STORE_COMMISSION = _t("fin_store_commission", [
    _c("company_entity", "VARCHAR(255)", "text"),
    _c("fee_item", "VARCHAR(255)", "text"),
    _c("store_name", "VARCHAR(255)", "text"),
    _c("submitted_by", "JSON", "user"),
    _c("remark_values", "JSON", "user"),
    _c("platform", "VARCHAR(255)", "text"),
    _c("fee_category", "VARCHAR(255)", "text"),
    *[_c(f"amount_2026_{m:02d}", "DECIMAL(20,4)", "currency") for m in range(1, 13)],
], [_idx("idx_company_store", "company_entity", "store_name")])

_FIN_TAX_DECLARATION_2026 = _t("fin_tax_declaration_2026", [
    _c("tax_date", "DATE", "date"),
    _c("filing_date", "DATE", "date"),
    _c("actual_vat_amount", "DECIMAL(20,4)", "currency"),
    _c("actual_declared_revenue", "DECIMAL(20,4)", "currency"),
    _c("validation_note", "TEXT", "text"),
    _c("union_fund_amount", "DECIMAL(20,4)", "currency"),
    _c("social_insurance_amount", "DECIMAL(20,4)", "currency"),
    _c("declared_payroll_amount", "DECIMAL(20,4)", "currency"),
    _c("individual_income_tax_amount", "DECIMAL(20,4)", "currency"),
    _c("urban_land_use_tax_amount", "DECIMAL(20,4)", "currency"),
    _c("property_tax_amount", "DECIMAL(20,4)", "currency"),
    _c("corporate_income_tax_amount", "DECIMAL(20,4)", "currency"),
    _c("input_tax_transfer_note", "TEXT", "text"),
    _c("prior_period_input_tax_credit", "DECIMAL(20,4)", "currency"),
    _c("filer", "JSON", "user"),
    _c("company_entity", "VARCHAR(255)", "text"),
], [_idx("idx_company_tax_date", "company_entity", "tax_date")])

_FIN_TAX_SALES_RECONCILIATION_2026 = _t("fin_tax_sales_reconciliation_2026", [
    _c("company_entity", "VARCHAR(255)", "text"),
    _c("sales_order_amount_13pct", "DECIMAL(20,4)", "currency"),
    _c("sales_delivery_amount_6pct", "DECIMAL(20,4)", "currency"),
    _c("general_ledger_sales_revenue", "DECIMAL(20,4)", "currency"),
    _c("period_date", "DATE", "date"),
    _c("submitted_by", "JSON", "user"),
    _c("sales_delivery_amount_other_rate", "DECIMAL(20,4)", "currency"),
    _c("output_tax_amount_2", "DECIMAL(20,4)", "currency"),
], [_idx("idx_company_period", "company_entity", "period_date")])

_FIN_TAX_STAMP_DUTY = _t("fin_tax_stamp_duty", [
    _c("taxable_document_name", "VARCHAR(255)", "text"),
    _c("taxable_amount", "DECIMAL(20,4)", "currency"),
    _c("applicable_tax_rate", "VARCHAR(255)", "text"),
    _c("handler", "JSON", "user"),
    _c("company_entity", "VARCHAR(255)", "text"),
    _c("tax_filing_period", "DATE", "date"),
], [_idx("idx_company_filing_period", "company_entity", "tax_filing_period")])

_FIN_TAX_UNINVOICED_SALES_SUMMARY = _t("fin_tax_uninvoiced_sales_summary", [
    _c("sales_company_name", "VARCHAR(255)", "text"),
    _c("uninvoiced_sales_tax_rate", "VARCHAR(255)", "text"),
    _c("period_date", "DATE", "date"),
    _c("prior_period_invoice_tax_rate", "VARCHAR(255)", "text"),
    _c("pre_invoice_tax_rate", "VARCHAR(255)", "text"),
    _c("uninvoiced_filing_tax_rate", "VARCHAR(255)", "text"),
    _c("handler", "JSON", "user"),
], [_idx("idx_sales_company_period", "sales_company_name", "period_date")])

_FIN_TAX_UNINVOICED_SALES = _t("fin_tax_uninvoiced_sales", [
    _c("company_entity", "VARCHAR(255)", "text"),
    _c("handler", "JSON", "user"),
    _c("uninvoiced_sales_tax_rate", "VARCHAR(255)", "text"),
    _c("uninvoiced_sales_excl_tax_amount", "DECIMAL(20,4)", "currency"),
    _c("period_date", "DATE", "date"),
], [_idx("idx_company_period", "company_entity", "period_date")])

_FIN_TAX_PRIOR_PERIOD_INVOICE = _t("fin_tax_prior_period_invoice", [
    _c("company_entity", "VARCHAR(255)", "text"),
    _c("handler", "JSON", "user"),
    _c("prior_period_invoice_tax_rate", "VARCHAR(255)", "text"),
    _c("prior_period_invoice_excl_tax_amount_raw", "VARCHAR(255)", "text"),
    _c("period_date", "DATE", "date"),
    _c("parent_record_refs", "JSON", "unidirectionalLink"),
], [_idx("idx_company_period", "company_entity", "period_date")])

_FIN_TAX_PRE_INVOICE = _t("fin_tax_pre_invoice", [
    _c("company_entity", "VARCHAR(255)", "text"),
    _c("handler", "JSON", "user"),
    _c("pre_invoice_tax_rate", "VARCHAR(255)", "text"),
    _c("pre_invoice_excl_tax_amount", "DECIMAL(20,4)", "currency"),
    _c("period_date", "DATE", "date"),
], [_idx("idx_company_period", "company_entity", "period_date")])

_FIN_TAX_INPUT_INVOICE = _t("fin_tax_input_invoice", [
    _c("company_entity", "VARCHAR(255)", "text"),
    _c("invoice_date", "DATE", "date"),
    _c("seller_name", "VARCHAR(255)", "text"),
    _c("excl_tax_amount", "DECIMAL(20,4)", "currency"),
    _c("tax_rate", "VARCHAR(255)", "text"),
    _c("certification_month", "DATE", "date"),
    _c("accounting_month", "DATE", "date"),
    _c("handler", "JSON", "user"),
    _c("invoice_number", "VARCHAR(255)", "text"),
], [
    _idx("idx_company_invoice_date", "company_entity", "invoice_date"),
    _idx("idx_invoice_number", "invoice_number"),
])

_FIN_ECOMMERCE_PREPAYMENT_SUPPLIER_INVOICE = _t("fin_ecommerce_prepayment_supplier_invoice", [
    _c("requirement_note", "TEXT", "text"),
    _c("accounts_payable_estimated_ledger_amount", "DECIMAL(20,4)", "currency"),
    _c("company_entity", "VARCHAR(255)", "text"),
    _c("ledger_reconciliation_status", "VARCHAR(255)", "text"),
    _c("undelivered_uninvoiced_note", "TEXT", "text"),
    _c("reviewer", "JSON", "user"),
    _c("uninvoiced_amount", "DECIMAL(20,4)", "currency"),
    _c("statement_date_raw", "VARCHAR(255)", "text"),
    _c("prepayment_ledger_amount", "DECIMAL(20,4)", "currency"),
    _c("supplier_name", "VARCHAR(255)", "text"),
    _c("submitted_by", "JSON", "user"),
], [_idx("idx_company_supplier", "company_entity", "supplier_name")])

_FIN_ECOMMERCE_PROMOTION_RECHARGE_BALANCE = _t("fin_ecommerce_promotion_recharge_balance", [
    _c("company_entity", "VARCHAR(255)", "text"),
    _c("store_name", "VARCHAR(255)", "text"),
    _c("platform", "VARCHAR(255)", "text"),
    _c("fee_item", "VARCHAR(255)", "text"),
    _c("opening_balance", "DECIMAL(20,4)", "currency"),
    _c("submitted_by", "VARCHAR(255)", "text"),
    *[_c(f"amount_2026_{m:02d}", "DECIMAL(20,4)", "currency") for m in range(1, 13)],
], [_idx("idx_company_store_fee", "company_entity", "store_name", "fee_item")])

_FIN_ECOMMERCE_PLATFORM_DEPOSIT = _t("fin_ecommerce_platform_deposit", [
    _c("deposit_paid_at_raw", "VARCHAR(255)", "text"),
    _c("store_operating_status", "VARCHAR(255)", "text"),
    _c("platform", "VARCHAR(255)", "text"),
    _c("submitted_by", "JSON", "user"),
    _c("company_entity", "VARCHAR(255)", "text"),
    _c("deposit_balance", "DECIMAL(20,4)", "currency"),
    _c("store_name", "VARCHAR(255)", "text"),
    _c("project_name", "VARCHAR(255)", "text"),
    _c("review_status", "VARCHAR(255)", "text"),
    _c("reviewer", "JSON", "user"),
], [_idx("idx_company_store", "company_entity", "store_name")])

_FIN_ECOMMERCE_STORE_FUNDS_BALANCE = _t("fin_ecommerce_store_funds_balance", [
    _c("store_name", "VARCHAR(255)", "text"),
    _c("channel", "VARCHAR(255)", "text"),
    _c("company_entity", "VARCHAR(255)", "text"),
    _c("review_status", "VARCHAR(255)", "text"),
    _c("reviewer", "JSON", "user"),
    _c("submitted_by", "JSON", "user"),
    _c("parent_record_refs", "JSON", "unidirectionalLink"),
    *[_c(f"balance_20260{m}" if m < 10 else f"balance_2026{m}", "DECIMAL(20,4)", "currency") for m in range(1, 9)],
], [_idx("idx_company_store", "company_entity", "store_name")])

_FIN_BILLION_SUBSIDY_PDD = _t("fin_billion_subsidy_pdd", [
    _c("business_date", "DATE", "date"),
    _c("product_name", "VARCHAR(255)", "text"),
    _c("billion_subsidy_rate", "DECIMAL(20,4)", "currency"),
    _c("expense_rebate_rate", "DECIMAL(20,4)", "currency"),
    _c("sales_amount", "DECIMAL(20,4)", "currency"),
    _c("store_name", "VARCHAR(255)", "text"),
    _c("invoice_application_date", "DATE", "date"),
    _c("subsidy_received_date", "DATE", "date"),
    _c("subsidy_received_amount", "DECIMAL(20,4)", "currency"),
    _c("submitted_by", "JSON", "user"),
    _c("finance_confirmer", "JSON", "user"),
    _c("invoice_amount", "DECIMAL(20,4)", "currency"),
], [_idx("idx_store_business_date", "store_name", "business_date")])

_FIN_BILLION_SUBSIDY_DOUYIN = _t("fin_billion_subsidy_douyin", [
    _c("business_date", "DATE", "date"),
    _c("store_name", "VARCHAR(255)", "text"),
    _c("billion_subsidy_rate", "DECIMAL(20,4)", "currency"),
    _c("expense_rebate_rate", "DECIMAL(20,4)", "currency"),
    _c("sales_amount", "DECIMAL(20,4)", "currency"),
    _c("finance_confirmer", "JSON", "user"),
    _c("subsidy_received_amount", "DECIMAL(20,4)", "currency"),
    _c("subsidy_received_date", "DATE", "date"),
    _c("invoice_application_date", "DATE", "date"),
    _c("submitted_by", "JSON", "user"),
    _c("product_name", "VARCHAR(255)", "text"),
], [_idx("idx_store_business_date", "store_name", "business_date")])

_DAILY_FUNDS_PATTERNS = (
    ("day_{nn:02d}_goods_service_income", "currency"),
    ("day_{nn:02d}_goods_service_expense", "currency"),
    ("day_{nn:02d}_payroll_social_insurance_expense", "currency"),
    ("day_{nn:02d}_other_income", "currency"),
    ("day_{nn:02d}_other_expense", "currency"),
)

_FIN_DAILY_FUNDS = _t("fin_daily_funds", [
    _c("company_entity", "VARCHAR(255)", "text"),
    _c("bank_name", "VARCHAR(255)", "text"),
    _c("opening_balance", "DECIMAL(20,4)", "currency"),
    _c("submitted_by", "JSON", "user"),
    _c("parent_record_refs", "JSON", "unidirectionalLink"),
    *[
        _c(pattern.format(nn=nn), "DECIMAL(20,4)", source_type)
        for pattern, source_type in _DAILY_FUNDS_PATTERNS
        for nn in range(1, 32)
    ],
], [_idx("idx_company_bank", "company_entity", "bank_name")])

_FIN_OFFLINE_RECEIVABLES_AGING = _t("fin_offline_receivables_aging", [
    _c("counterparty_name", "VARCHAR(255)", "text"),
    _c("aging_31_60_days_amount", "DECIMAL(20,4)", "currency"),
    _c("receivable_category", "VARCHAR(255)", "text"),
    _c("aging_0_30_days_amount", "DECIMAL(20,4)", "currency"),
    _c("company_entity", "VARCHAR(255)", "text"),
    _c("description_note", "TEXT", "text"),
    _c("updated_date", "DATE", "date"),
    _c("accounts_receivable_ending_balance", "DECIMAL(20,4)", "currency"),
    _c("overdue_amount", "DECIMAL(20,4)", "currency"),
    _c("prepared_by", "JSON", "user"),
    _c("reviewer", "JSON", "user"),
    _c("reviewed_at", "DATE", "date"),
    _c("business_department_confirmation", "VARCHAR(255)", "text"),
    _c("confirmer", "JSON", "user"),
    _c("owner", "JSON", "user"),
], [
    _idx("idx_company_counterparty", "company_entity", "counterparty_name"),
    _idx("idx_updated_date", "updated_date"),
])

_FIN_OFFLINE_DEPOSIT_OTHER_RECEIVABLES = _t("fin_offline_deposit_other_receivables", [
    _c("deposit_paid_at_raw", "VARCHAR(255)", "text"),
    _c("cooperation_status", "VARCHAR(255)", "text"),
    _c("submitted_by", "JSON", "user"),
    _c("company_entity", "VARCHAR(255)", "text"),
    _c("deposit_balance", "DECIMAL(20,4)", "currency"),
    _c("supplier_name", "VARCHAR(255)", "text"),
    _c("project_name", "VARCHAR(255)", "text"),
    _c("updated_at", "DATE", "date"),
    _c("reviewer", "JSON", "user"),
    _c("description_note", "TEXT", "text"),
], [
    _idx("idx_company_supplier", "company_entity", "supplier_name"),
    _idx("idx_updated_at", "updated_at"),
])


_TABLES = {
    "fin_store_commission": _FIN_STORE_COMMISSION,
    "fin_tax_declaration_2026": _FIN_TAX_DECLARATION_2026,
    "fin_tax_sales_reconciliation_2026": _FIN_TAX_SALES_RECONCILIATION_2026,
    "fin_tax_stamp_duty": _FIN_TAX_STAMP_DUTY,
    "fin_tax_uninvoiced_sales_summary": _FIN_TAX_UNINVOICED_SALES_SUMMARY,
    "fin_tax_uninvoiced_sales": _FIN_TAX_UNINVOICED_SALES,
    "fin_tax_prior_period_invoice": _FIN_TAX_PRIOR_PERIOD_INVOICE,
    "fin_tax_pre_invoice": _FIN_TAX_PRE_INVOICE,
    "fin_tax_input_invoice": _FIN_TAX_INPUT_INVOICE,
    "fin_ecommerce_prepayment_supplier_invoice": _FIN_ECOMMERCE_PREPAYMENT_SUPPLIER_INVOICE,
    "fin_ecommerce_promotion_recharge_balance": _FIN_ECOMMERCE_PROMOTION_RECHARGE_BALANCE,
    "fin_ecommerce_platform_deposit": _FIN_ECOMMERCE_PLATFORM_DEPOSIT,
    "fin_ecommerce_store_funds_balance": _FIN_ECOMMERCE_STORE_FUNDS_BALANCE,
    "fin_billion_subsidy_pdd": _FIN_BILLION_SUBSIDY_PDD,
    "fin_billion_subsidy_douyin": _FIN_BILLION_SUBSIDY_DOUYIN,
    "fin_daily_funds": _FIN_DAILY_FUNDS,
    "fin_offline_receivables_aging": _FIN_OFFLINE_RECEIVABLES_AGING,
    "fin_offline_deposit_other_receivables": _FIN_OFFLINE_DEPOSIT_OTHER_RECEIVABLES,
}


def table_definition(name: str) -> TableDefinition:
    try:
        return _TABLES[name]
    except KeyError as exc:
        raise KeyError(f"unregistered DingTalk target table: {name}") from exc


def all_table_definitions() -> tuple[TableDefinition, ...]:
    return tuple(_TABLES[name] for name in sorted(_TABLES))
