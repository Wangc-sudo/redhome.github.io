"""Finance snapshots and monthly store-fund projections from local raw data."""

from datetime import date, datetime

from common.public_data.mart_extract_schema import FACT_FIN_PREPAYMENT_INVOICE


BALANCE_MONTHS = tuple(f"2026{month:02d}" for month in range(1, 9))


def parse_statement_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            pass
    return None


def project_snapshot(repository, dataset, run_id, synced_at):
    source_rows = repository.read_dataset(dataset)
    rows = []
    columns = list(dataset.target_columns)
    is_prepayment = dataset.target_table == FACT_FIN_PREPAYMENT_INVOICE
    if is_prepayment:
        columns.append("statement_date")
    unparsed = 0
    for source in source_rows:
        row = {name: source.get(name) for name in dataset.target_columns}
        if is_prepayment:
            raw_date = row.get("statement_date_raw")
            row["statement_date"] = parse_statement_date(raw_date)
            if raw_date is not None and str(raw_date).strip() and row["statement_date"] is None:
                unparsed += 1
        rows.append({**row, "synced_at": synced_at, "sync_run_id": run_id})
    written = repository.replace_table(
        dataset.target_table, columns + ["synced_at", "sync_run_id"], rows,
    )
    return {
        "records_read": len(source_rows), "records_new": written,
        "records_updated": 0, "records_skipped": 0,
        "statement_date_unparsed": unparsed if is_prepayment else None,
        "_record_ids": [row["source_record_id"] for row in source_rows],
    }


def project_store_funds_melt(repository, dataset, run_id, synced_at):
    source_rows = repository.read_table(dataset.source_table)
    rows, identities = [], []
    skipped = 0
    for source in source_rows:
        base = {name: source.get(name) for name in ("store_name", "channel", "company_entity")}
        for ym in BALANCE_MONTHS:
            balance = source.get(f"balance_{ym}")
            if balance is None or str(balance).strip() == "":
                skipped += 1
                continue
            rows.append({
                **base, "month": f"{ym[:4]}-{ym[4:]}", "balance": balance,
                "synced_at": synced_at, "sync_run_id": run_id,
            })
            identities.append(f"{source['dingtalk_record_id']}:{ym}")
    written = repository.replace_table(
        dataset.target_table,
        ["store_name", "channel", "company_entity", "month", "balance", "synced_at", "sync_run_id"],
        rows,
    )
    return {
        "records_read": len(source_rows), "records_new": written,
        "records_updated": 0, "records_skipped": skipped,
        "balance_months": len(BALANCE_MONTHS), "_record_ids": identities,
    }
