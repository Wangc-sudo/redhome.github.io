"""Discover DingTalk AI table field schemas for manifest construction.

Reads credentials from existing robot configs and outputs field names/types
for each finance table. No secrets are printed.

Usage:
    python scripts/discover_dingtalk_fields.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.public_data.dingtalk_read import DingTalkReadGateway

SHEETS_TO_DISCOVER = [
    {
        "dataset": "fin_store_commission",
        "target_table": "fin_store_commission",
        "base_id": "KGZLxjv9VGjmo2zLuYonDNjOW6EDybno",
        "sheet_id": "5y8vtogysp4dwysq5spc8",
    },
    {
        "dataset": "fin_tax_declaration_2026",
        "target_table": "fin_tax_declaration_2026",
        "base_id": "dpYLaezmVNZ3MxPwiK2GA4ewVrMqPxX6",
        "sheet_id": "q1wnmyq6r4r6xfdsqlvyw",
    },
    {
        "dataset": "fin_ecommerce_promotion_recharge_balance",
        "target_table": "fin_ecommerce_promotion_recharge_balance",
        "base_id": "r1R7q3QmWe4D5BqNTXxnB9k68xkXOEP2",
        "sheet_id": "Y2yOyXt",
    },
    {
        "dataset": "fin_ecommerce_platform_deposit",
        "target_table": "fin_ecommerce_platform_deposit",
        "base_id": "ZX6GRezwJlNpeB4asQ5v1XBOWdqbropQ",
        "sheet_id": "k4z3335fh3s7y9k6hu8fd",
    },
    {
        "dataset": "fin_ecommerce_store_funds_balance",
        "target_table": "fin_ecommerce_store_funds_balance",
        "base_id": "R1zknDm0WRlPXQGwu2kqRRA5JBQEx5rG",
        "sheet_id": "e2q1741g5aekd54ecyudv",
    },
    {
        "dataset": "fin_billion_subsidy",
        "target_table": "fin_billion_subsidy_pdd",
        "base_id": "jb9Y4gmKWrZn9yzvienZODMaWGXn6lpz",
        "sheet_id": "hERWDMS",
    },
    {
        "dataset": "fin_daily_funds",
        "target_table": "fin_daily_funds",
        "base_id": "r1R7q3QmWe4D5BqNTZmpnY198xkXOEP2",
        "sheet_id": "ljudw0h1p9licd4vchp08",
    },
    {
        "dataset": "fin_offline_receivables_aging",
        "target_table": "fin_offline_receivables_aging",
        "base_id": "vy20BglGWOayj65ltG0Oq2leJA7depqY",
        "sheet_id": "bzjkk0ayura1iaxp1csm1",
    },
    {
        "dataset": "fin_ecommerce_prepayment_supplier_invoice",
        "target_table": "fin_ecommerce_prepayment_supplier_invoice",
        "base_id": "jb9Y4gmKWrZn9yzviQ9XOoQ9WGXn6lpz",
        "sheet_id": "63eg8lu2zpqikfl5uh0ls",
    },
    {
        "dataset": "fin_offline_deposit_other_receivables",
        "target_table": "fin_offline_deposit_other_receivables",
        "base_id": "G1DKw2zgV2wdnYZGHBB4xgzxJB5r9YAn",
        "sheet_id": "k4z3335fh3s7y9k6hu8fd",
    },
]


def load_credentials():
    config_path = Path(__file__).resolve().parent.parent / "数字化" / "钉钉" / "杭州日报机器人" / "config.json"
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    dt = cfg["dingtalk"]
    return dt["appKey"], dt["appSecret"], dt["operatorId"]


def main():
    app_key, app_secret, operator_id = load_credentials()
    gateway = DingTalkReadGateway(app_key, app_secret, operator_id)

    results = {}
    output_lines = []

    def out(line=""):
        output_lines.append(line)
        print(".", end="", flush=True)

    for sheet_info in SHEETS_TO_DISCOVER:
        base_id = sheet_info["base_id"]
        sheet_id = sheet_info["sheet_id"]
        dataset = sheet_info["dataset"]
        label = f"{dataset} ({base_id}/{sheet_id or '??'})"

        out(f"\n{'='*60}")
        out(f"Discovering: {label}")
        out(f"{'='*60}")

        try:
            if sheet_id is None:
                sheets = gateway.list_sheets(base_id)
                out(f"  Sheets in base {base_id}:")
                for s in sheets:
                    out(f"    - id={s.get('id')}  name={s.get('name')}")
                results[dataset] = {"base_id": base_id, "sheets_listed": True, "fields": []}
                continue

            fields = gateway.list_fields(base_id, sheet_id)
            out(f"  Fields ({len(fields)}):")
            field_list = []
            for field in fields:
                name = field.get("name", "?")
                ftype = field.get("type", "?")
                field_list.append({"name": name, "type": ftype})
                out(f"    - {name}  (type={ftype})")
            results[dataset] = {"base_id": base_id, "sheet_id": sheet_id, "fields": field_list}

        except Exception as exc:
            out(f"  ERROR: {exc}")
            results[dataset] = {"base_id": base_id, "sheet_id": sheet_id, "error": str(exc)}

    out(f"\n\n{'='*60}")
    out("SUMMARY JSON")
    out(f"{'='*60}")
    out(json.dumps(results, ensure_ascii=False, indent=2))

    output_path = Path(__file__).resolve().parent.parent / "scripts" / "discovery_output.txt"
    output_path.write_text("\n".join(output_lines), encoding="utf-8")
    print(f"\nDone. Results written to {output_path}")


if __name__ == "__main__":
    main()
