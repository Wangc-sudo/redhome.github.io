"""Generate manifest.json for the 10-table trial sync."""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Sheet name lookup from API discovery
SHEET_NAMES = {
    "5y8vtogysp4dwysq5spc8": "店铺扣点费用管理",
    "q1wnmyq6r4r6xfdsqlvyw": "纳税申报核算表-2026",
    "8jz4u8jzpaqsiliw5b5z3": "推广费余额明细表",
    "k4z3335fh3s7y9k6hu8fd": "平台保证金管理",
    "e2q1741g5aekd54ecyudv": "店铺资金余额核对表",
    "hERWDMS": "百亿补贴管理表-拼多多",
    "ljudw0h1p9licd4vchp08": "每日资金明细",
    "bzjkk0ayura1iaxp1csm1": "线下—应收账款账龄分析表",
    "63eg8lu2zpqikfl5uh0ls": "电商预付及供应商发票管理表",
}

# For fin_offline_deposit_other_receivables, the sheet_id is also k4z3335fh3s7y9k6hu8fd
# but in a different base. The sheet_name from API is "线下保证金及其他应收管理-202602"
SHEET_NAMES_OVERRIDE = {
    "G1DKw2zgV2wdnYZGHBB4xgzxJB5r9YAn/k4z3335fh3s7y9k6hu8fd": "线下保证金及其他应收管理-202602",
}


def fm(source_name, column, source_type):
    return {"column": column, "source_type": source_type}


def build_store_commission():
    mapping = {
        "公司主体": fm("公司主体", "company_entity", "text"),
        "费用项目": fm("费用项目", "fee_item", "singleSelect"),
        "店铺名称": fm("店铺名称", "store_name", "text"),
        "填写人": fm("填写人", "submitted_by", "user"),
        "备注": fm("备注", "remark_values", "multipleSelect"),
        "平台": fm("平台", "platform", "singleSelect"),
        "费用类别": fm("费用类别", "fee_category", "singleSelect"),
    }
    for m in range(1, 13):
        mapping[f"2026-{m:02d}"] = fm(f"2026-{m:02d}", f"amount_2026_{m:02d}", "number")
    return {
        "base_id": "KGZLxjv9VGjmo2zLuYonDNjOW6EDybno",
        "sheets": [{
            "sheet_id": "5y8vtogysp4dwysq5spc8",
            "sheet_name": SHEET_NAMES["5y8vtogysp4dwysq5spc8"],
            "dataset": "fin_store_commission",
            "target_table": "fin_store_commission",
            "max_pages": 100,
            "field_mapping": mapping,
        }],
    }


def build_tax_declaration():
    mapping = {
        "日期": fm("日期", "tax_date", "date"),
        "申报日期": fm("申报日期", "filing_date", "date"),
        "实际申报的增值税": fm("实际申报的增值税", "actual_vat_amount", "number"),
        "实际申报的收入": fm("实际申报的收入", "actual_declared_revenue", "number"),
        "校验2": fm("校验2", "validation_note", "text"),
        "工会经费": fm("工会经费", "union_fund_amount", "number"),
        "社保": fm("社保", "social_insurance_amount", "number"),
        "申报的工资总额": fm("申报的工资总额", "declared_payroll_amount", "number"),
        "个人所得税": fm("个人所得税", "individual_income_tax_amount", "number"),
        "城镇土地使用税": fm("城镇土地使用税", "urban_land_use_tax_amount", "number"),
        "房产税": fm("房产税", "property_tax_amount", "number"),
        "企业所得税": fm("企业所得税", "corporate_income_tax_amount", "number"),
        "进项税额转出": fm("进项税额转出", "input_tax_transfer_note", "text"),
        "上期留抵税额": fm("上期留抵税额", "prior_period_input_tax_credit", "number"),
        "申报人": fm("申报人", "filer", "user"),
        "公司主体": fm("公司主体", "company_entity", "text"),
    }
    return {
        "base_id": "dpYLaezmVNZ3MxPwiK2GA4ewVrMqPxX6",
        "sheets": [{
            "sheet_id": "q1wnmyq6r4r6xfdsqlvyw",
            "sheet_name": SHEET_NAMES["q1wnmyq6r4r6xfdsqlvyw"],
            "dataset": "fin_tax_declaration_2026",
            "target_table": "fin_tax_declaration_2026",
            "max_pages": 100,
            "field_mapping": mapping,
        }],
    }


def build_promotion_recharge_balance():
    mapping = {
        "公司主体": fm("公司主体", "company_entity", "text"),
        "店铺名称": fm("店铺名称", "store_name", "text"),
        "平台": fm("平台", "platform", "singleSelect"),
        "费用项目": fm("费用项目", "fee_item", "singleSelect"),
        "期初余额": fm("期初余额", "opening_balance", "number"),
        "填写人": fm("填写人", "submitted_by", "singleSelect"),
    }
    for m in range(1, 13):
        mapping[f"2026-{m:02d}"] = fm(f"2026-{m:02d}", f"amount_2026_{m:02d}", "number")
    return {
        "base_id": "r1R7q3QmWe4D5BqNTXxnB9k68xkXOEP2",
        "sheets": [{
            "sheet_id": "8jz4u8jzpaqsiliw5b5z3",
            "sheet_name": SHEET_NAMES["8jz4u8jzpaqsiliw5b5z3"],
            "dataset": "fin_ecommerce_promotion_recharge_balance",
            "target_table": "fin_ecommerce_promotion_recharge_balance",
            "max_pages": 100,
            "field_mapping": mapping,
        }],
    }


def build_platform_deposit():
    mapping = {
        "保证金缴纳时间": fm("保证金缴纳时间", "deposit_paid_at_raw", "text"),
        "店铺是否正常运营": fm("店铺是否正常运营", "store_operating_status", "singleSelect"),
        "平台": fm("平台", "platform", "singleSelect"),
        "填写人": fm("填写人", "submitted_by", "user"),
        "公司主体": fm("公司主体", "company_entity", "text"),
        "保证金余额": fm("保证金余额", "deposit_balance", "number"),
        "店铺名称": fm("店铺名称", "store_name", "text"),
        "项目": fm("项目", "project_name", "text"),
        "是否复核": fm("是否复核", "review_status", "singleSelect"),
        "复核人": fm("复核人", "reviewer", "user"),
    }
    return {
        "base_id": "ZX6GRezwJlNpeB4asQ5v1XBOWdqbropQ",
        "sheets": [{
            "sheet_id": "k4z3335fh3s7y9k6hu8fd",
            "sheet_name": SHEET_NAMES["k4z3335fh3s7y9k6hu8fd"],
            "dataset": "fin_ecommerce_platform_deposit",
            "target_table": "fin_ecommerce_platform_deposit",
            "max_pages": 100,
            "field_mapping": mapping,
        }],
    }


def build_store_funds_balance():
    mapping = {
        "店铺名称": fm("店铺名称", "store_name", "text"),
        "渠道": fm("渠道", "channel", "singleSelect"),
        "公司主体": fm("公司主体", "company_entity", "text"),
        "是否复核": fm("是否复核", "review_status", "singleSelect"),
        "复核人": fm("复核人", "reviewer", "user"),
        "填写人": fm("填写人", "submitted_by", "user"),
        "父记录": fm("父记录", "parent_record_refs", "unidirectionalLink"),
    }
    for m in range(1, 9):
        dt_name = f"20260{m}" if m < 10 else f"2026{m}"
        col_name = f"balance_20260{m}" if m < 10 else f"balance_2026{m}"
        mapping[dt_name] = fm(dt_name, col_name, "currency")
    return {
        "base_id": "R1zknDm0WRlPXQGwu2kqRRA5JBQEx5rG",
        "sheets": [{
            "sheet_id": "e2q1741g5aekd54ecyudv",
            "sheet_name": SHEET_NAMES["e2q1741g5aekd54ecyudv"],
            "dataset": "fin_ecommerce_store_funds_balance",
            "target_table": "fin_ecommerce_store_funds_balance",
            "max_pages": 100,
            "field_mapping": mapping,
        }],
    }


def build_billion_subsidy():
    mapping = {
        "日期": fm("日期", "business_date", "date"),
        "销售商品品名": fm("销售商品品名", "product_name", "text"),
        "百亿补贴比率": fm("百亿补贴比率", "billion_subsidy_rate", "number"),
        "费用返还比率": fm("费用返还比率", "expense_rebate_rate", "number"),
        "销售金额": fm("销售金额", "sales_amount", "number"),
        "销售店铺名称": fm("销售店铺名称", "store_name", "text"),
        "申请开票日期": fm("申请开票日期", "invoice_application_date", "date"),
        "补贴到账日期": fm("补贴到账日期", "subsidy_received_date", "date"),
        "补贴到账金额": fm("补贴到账金额", "subsidy_received_amount", "number"),
        "填写人": fm("填写人", "submitted_by", "user"),
        "财务确认人": fm("财务确认人", "finance_confirmer", "user"),
        "开票金额": fm("开票金额", "invoice_amount", "number"),
    }
    return {
        "base_id": "jb9Y4gmKWrZn9yzvienZODMaWGXn6lpz",
        "sheets": [{
            "sheet_id": "hERWDMS",
            "sheet_name": SHEET_NAMES["hERWDMS"],
            "dataset": "fin_billion_subsidy_pdd",
            "target_table": "fin_billion_subsidy_pdd",
            "max_pages": 200,
            "field_mapping": mapping,
        }],
    }


def build_daily_funds():
    mapping = {
        "公司": fm("公司", "company_entity", "text"),
        "银行": fm("银行", "bank_name", "text"),
        "期初余额": fm("期初余额", "opening_balance", "currency"),
        "填表人": fm("填表人", "submitted_by", "user"),
        "父记录": fm("父记录", "parent_record_refs", "unidirectionalLink"),
    }
    patterns = [
        ("day_{nn:02d}_goods_service_income", "（货款/服务收入）"),
        ("day_{nn:02d}_goods_service_expense", "（货款/服务支出）"),
        ("day_{nn:02d}_payroll_social_insurance_expense", "（工薪社保支出）"),
        ("day_{nn:02d}_other_income", "（其他收入）"),
        ("day_{nn:02d}_other_expense", "其他支出"),
    ]
    for day in range(1, 32):
        for col_pattern, name_suffix in patterns:
            col_name = col_pattern.format(nn=day)
            if name_suffix == "其他支出":
                dt_name = f"{day}日{name_suffix}"
            else:
                dt_name = f"{day}日{name_suffix}"
            mapping[dt_name] = fm(dt_name, col_name, "currency")
    return {
        "base_id": "r1R7q3QmWe4D5BqNTZmpnY198xkXOEP2",
        "sheets": [{
            "sheet_id": "ljudw0h1p9licd4vchp08",
            "sheet_name": SHEET_NAMES["ljudw0h1p9licd4vchp08"],
            "dataset": "fin_daily_funds",
            "target_table": "fin_daily_funds",
            "max_pages": 100,
            "field_mapping": mapping,
        }],
    }


def build_offline_receivables_aging():
    mapping = {
        "往来单位": fm("往来单位", "counterparty_name", "text"),
        "账龄（31-60天）": fm("账龄（31-60天）", "aging_31_60_days_amount", "number"),
        "类别": fm("类别", "receivable_category", "singleSelect"),
        "账龄（0-30天）": fm("账龄（0-30天）", "aging_0_30_days_amount", "number"),
        "公司主体": fm("公司主体", "company_entity", "singleSelect"),
        "说明": fm("说明", "description_note", "text"),
        "更新日期": fm("更新日期", "updated_date", "date"),
        "应收账款期末余额": fm("应收账款期末余额", "accounts_receivable_ending_balance", "number"),
        "逾期金额": fm("逾期金额", "overdue_amount", "number"),
        "编制人": fm("编制人", "prepared_by", "user"),
        "复核人": fm("复核人", "reviewer", "user"),
        "复核时间": fm("复核时间", "reviewed_at", "date"),
        "业务部门确认": fm("业务部门确认", "business_department_confirmation", "text"),
        "确认人": fm("确认人", "confirmer", "user"),
        "负责人": fm("负责人", "owner", "user"),
    }
    return {
        "base_id": "vy20BglGWOayj65ltG0Oq2leJA7depqY",
        "sheets": [{
            "sheet_id": "bzjkk0ayura1iaxp1csm1",
            "sheet_name": SHEET_NAMES["bzjkk0ayura1iaxp1csm1"],
            "dataset": "fin_offline_receivables_aging",
            "target_table": "fin_offline_receivables_aging",
            "max_pages": 100,
            "field_mapping": mapping,
        }],
    }


def build_prepayment_supplier_invoice():
    mapping = {
        "说明要求": fm("说明要求", "requirement_note", "text"),
        "应付-暂估账面": fm("应付-暂估账面", "accounts_payable_estimated_ledger_amount", "number"),
        "公司主体": fm("公司主体", "company_entity", "text"),
        "账账相符核对": fm("账账相符核对", "ledger_reconciliation_status", "singleSelect"),
        "未到货未到票情况说明": fm("未到货未到票情况说明", "undelivered_uninvoiced_note", "text"),
        "核对人": fm("核对人", "reviewer", "user"),
        "未到票金额": fm("未到票金额", "uninvoiced_amount", "number"),
        "日期": fm("日期", "statement_date_raw", "text"),
        "账面预付账款": fm("账面预付账款", "prepayment_ledger_amount", "number"),
        "供应商名称": fm("供应商名称", "supplier_name", "text"),
        "填写人": fm("填写人", "submitted_by", "user"),
    }
    return {
        "base_id": "jb9Y4gmKWrZn9yzviQ9XOoQ9WGXn6lpz",
        "sheets": [{
            "sheet_id": "63eg8lu2zpqikfl5uh0ls",
            "sheet_name": SHEET_NAMES["63eg8lu2zpqikfl5uh0ls"],
            "dataset": "fin_ecommerce_prepayment_supplier_invoice",
            "target_table": "fin_ecommerce_prepayment_supplier_invoice",
            "max_pages": 100,
            "field_mapping": mapping,
        }],
    }


def build_offline_deposit_other_receivables():
    mapping = {
        "保证金缴纳时间": fm("保证金缴纳时间", "deposit_paid_at_raw", "date"),
        "是否正常合作": fm("是否正常合作", "cooperation_status", "singleSelect"),
        "填写人": fm("填写人", "submitted_by", "user"),
        "公司主体": fm("公司主体", "company_entity", "singleSelect"),
        "保证金余额": fm("保证金余额", "deposit_balance", "number"),
        "往来单位（供应商）": fm("往来单位（供应商）", "supplier_name", "text"),
        "项目": fm("项目", "project_name", "singleSelect"),
        "更新时间": fm("更新时间", "updated_at", "date"),
        "复核人": fm("复核人", "reviewer", "user"),
        "说明": fm("说明", "description_note", "text"),
    }
    return {
        "base_id": "G1DKw2zgV2wdnYZGHBB4xgzxJB5r9YAn",
        "sheets": [{
            "sheet_id": "k4z3335fh3s7y9k6hu8fd",
            "sheet_name": SHEET_NAMES_OVERRIDE["G1DKw2zgV2wdnYZGHBB4xgzxJB5r9YAn/k4z3335fh3s7y9k6hu8fd"],
            "dataset": "fin_offline_deposit_other_receivables",
            "target_table": "fin_offline_deposit_other_receivables",
            "max_pages": 100,
            "field_mapping": mapping,
        }],
    }


_WDT_CONFIG_PATH = Path(__file__).resolve().parent / "wdt_datasets.json"

#: Default number of past days each WDT window sweep covers.
_DEFAULT_LOOKBACK_DAYS = 1
#: Default slice length (minutes) for time-split WDT datasets.
_DEFAULT_WINDOW_MINUTES = 50


def load_wdt_config(config_path=None):
    """Read the externalized WDT dataset definitions.

    The configuration lives in ``scripts/wdt_datasets.json`` so the four
    allowlisted WDT methods (and their pagination / id-path / params) can be
    tuned without editing Python.  Each entry is one logical dataset;
    ``build_wdt_datasets`` expands time-split entries into concrete windows.
    """
    path = Path(config_path) if config_path else _WDT_CONFIG_PATH
    with open(path, encoding="utf-8") as fh:
        config = json.load(fh)
    if not isinstance(config, dict) or not isinstance(config.get("datasets"), list):
        raise ValueError(f"WDT config must be an object with a 'datasets' list: {path}")
    return config


def build_wdt_datasets(lookback_days=None, config_path=None):
    """Expand the externalized WDT definitions into concrete windowed datasets.

    Time-split entries are divided into ``window_minutes`` slices covering the
    most recent *lookback_days*; ``single`` entries (e.g. ``goods_query``, a
    full catalog pull) get one minimal window.  Re-run ``build_manifest.py``
    to shift the windows forward.
    """
    config = load_wdt_config(config_path)
    if lookback_days is None:
        lookback = int(config.get("lookback_days", _DEFAULT_LOOKBACK_DAYS))
    else:
        lookback = lookback_days
    window_minutes = int(config.get("window_minutes", _DEFAULT_WINDOW_MINUTES))

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    step = timedelta(minutes=window_minutes)

    datasets = []
    for entry in config["datasets"]:
        base = {
            "method": entry["method"],
            "target_table": "wdt_records",
            "record_id_path": entry["record_id_path"],
            "page_size": entry["page_size"],
            "max_pages": entry["max_pages"],
            "max_window_minutes": window_minutes,
            "params": entry.get("params", {}),
        }
        # Only emitted when False; the manifest reader defaults it to True.
        if not entry.get("time_boxed", True):
            base["time_boxed"] = False

        if entry.get("window", "split") == "single":
            datasets.append({
                **base,
                "dataset": entry["dataset"],
                "window_start": start.strftime(fmt),
                "window_end": (start + timedelta(minutes=1)).strftime(fmt),
            })
            continue

        idx = 0
        w_start = start
        while w_start < now:
            w_end = min(w_start + step, now)
            datasets.append({
                **base,
                "dataset": f"{entry['dataset']}_{idx:04d}",
                "window_start": w_start.strftime(fmt),
                "window_end": w_end.strftime(fmt),
            })
            w_start = w_end
            idx += 1

    return datasets


def _bootstrap_dingtalk_bases():
    """The 10-table trial DingTalk bases (used only for a fresh manifest)."""
    return [
        build_store_commission(),
        build_tax_declaration(),
        build_promotion_recharge_balance(),
        build_platform_deposit(),
        build_store_funds_balance(),
        build_billion_subsidy(),
        build_daily_funds(),
        build_offline_receivables_aging(),
        build_prepayment_supplier_invoice(),
        build_offline_deposit_other_receivables(),
    ]


def build_org():
    """Contact-directory declaration: region mapping lives in the org seed,
    not here -- the manifest only pins the sync contract itself."""
    return {
        "dataset": "org_directory",
        "target_table": "dingtalk_org_member",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="build_manifest")
    parser.add_argument(
        "--lookback-days", type=int, default=None,
        help="override the WDT lookback window in days (default: config value)",
    )
    parser.add_argument(
        "--output", default=None,
        help="manifest output path (default: docker/integration/source-manifest.json)",
    )
    parser.add_argument(
        "--wdt-only", action="store_true",
        help="emit a manifest with only WDT datasets (empty dingtalk.bases)",
    )
    args = parser.parse_args(argv)

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = Path(__file__).resolve().parent.parent / "docker" / "integration" / "source-manifest.json"

    if args.wdt_only:
        manifest = {"version": 1, "dingtalk": {"bases": []}, "wdt": {"datasets": []}}
    elif output_path.exists():
        # Non-destructive refresh: keep every existing DingTalk sheet (this
        # builder only knows the 10-table trial subset) and rewrite only the
        # WDT window block.  Re-running is the supported way to shift windows.
        manifest = json.loads(output_path.read_text(encoding="utf-8"))
        preserved = sum(
            len(base.get("sheets", []))
            for base in manifest.get("dingtalk", {}).get("bases", [])
        )
        print(f"Preserved {preserved} existing DingTalk sheets")
    else:
        manifest = {
            "version": 1,
            "dingtalk": {
                "bases": _bootstrap_dingtalk_bases(),
                "org": build_org(),
            },
            "wdt": {"datasets": []},
        }

    manifest["wdt"] = {"datasets": build_wdt_datasets(lookback_days=args.lookback_days)}

    content = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    output_path.write_text(content, encoding="utf-8")
    print(f"Manifest written to {output_path}")

    # Validate
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from common.public_data.manifest import load_manifest
    loaded = load_manifest(output_path)
    print(f"Validation passed: {len(loaded.dingtalk_sheets)} sheets, {len(loaded.wdt_datasets)} wdt datasets")
    if loaded.dingtalk_org is not None:
        print(f"  {loaded.dingtalk_org.dataset}: contact directory → {loaded.dingtalk_org.target_table}")
    for sheet in loaded.dingtalk_sheets:
        print(f"  {sheet.dataset}: {len(sheet.fields)} fields → {sheet.target_table}")
    for ds in loaded.wdt_datasets:
        print(f"  {ds.dataset}: {ds.method} (id={ds.record_id_path}) → {ds.target_table}")


if __name__ == "__main__":
    main()
