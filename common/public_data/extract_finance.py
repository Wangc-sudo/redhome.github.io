"""Finance snapshots and monthly store-fund projections from local raw data."""

import contextlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

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


# ---------------------------------------------------------------------------
# 店铺费用窄表（2026-10-12 渠道BI费用面板，签认预填版）
# ---------------------------------------------------------------------------

#: 费用窄表业务列（replace_table 白名单来源，extract_mart 侧引用）。
_STORE_EXPENSE_COLUMNS = (
    "month", "source", "fin_store_name", "fin_platform", "fee_category",
    "fee_item", "mart_store_name", "mart_channel", "expense_amount",
)

#: 两张费用宽表的 12 个月金额列 → "YYYY-MM"（与 fact_fin_store_funds 同口径）。
_EXPENSE_MONTH_PAIRS = tuple(
    (f"amount_2026_{m:02d}", f"2026-{m:02d}") for m in range(1, 13)
)

#: 推广费充值宽表（双源投影的第二张表）。read_table 白名单按单源投影圈定，
#: 本表在投影器内显式点名直读 raw 连接（常量表名，无注入面；与
#: extract_ecom_people 直取连接同款先例）。
_RECHARGE_SOURCE_TABLE = "fin_ecommerce_promotion_recharge_balance"

#: 版本受控种子：店铺→mart 渠道挂接（预填版 2026-10-12 签认推进；
#: 三类口径问题后置，provisional=true 行待财务/运营二次确认，直接改 JSON）。
_STORE_CHANNEL_SEED = (
    Path(__file__).resolve().parent / "seeds" / "store_channel_mapping.json"
)

_PSEUDO_JSON_NAME_RE = re.compile(r"'name':\s*'([^']*)'")


def load_store_channel_mapping(path=None):
    """解析店铺挂接种子，返回 ``{(fin_store, fin_platform): (mart_store, mart_channel)}``。

    每次投影重新读盘、不缓存：scheduler 是长驻进程，改种子重跑 extract
    即生效。``mart_store`` 为 null 表示暂不挂渠道——费用仍入表，
    面板归「未挂渠道」桶。
    """
    seed = Path(path) if path is not None else _STORE_CHANNEL_SEED
    raw = json.loads(seed.read_text(encoding="utf-8"))
    mapping = {}
    for item in raw.get("mappings") or []:
        mapping[(item.get("fin_store"), item.get("fin_platform"))] = (
            item.get("mart_store"), item.get("mart_channel"),
        )
    return mapping


def _parse_name(value):
    """伪 JSON 单引号串 → name 纯文本（``{'name': '天猫', 'id': …}`` → ``天猫``）。

    raw 表 platform/fee_category/fee_item 三列同坑：MySQL ``JSON_EXTRACT``
    直接报 3141（映射文档 §三），只能字符串解析；普通字符串原样透传，
    空串/None 归 None。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _PSEUDO_JSON_NAME_RE.search(text)
    if match:
        return match.group(1) or None
    return text


def _read_recharge_rows(repository):
    with contextlib.closing(repository.raw_connection.cursor()) as cursor:
        cursor.execute(f"SELECT * FROM `{_RECHARGE_SOURCE_TABLE}`")
        return [dict(row) for row in cursor.fetchall()]


def project_store_expense_melt(repository, dataset, run_id, synced_at):
    """两张费用宽表 → ``fact_fin_store_expense`` 窄表（店铺×平台×费用项×月）。

    * 源 1 ``fin_store_commission``（店铺佣金/扣点等，source=store_commission）
    * 源 2 ``fin_ecommerce_promotion_recharge_balance``（推广费充值，
      source=promotion_recharge；fee_category 源表无此列，落 NULL）

    宽表 amount_2026_01..12 转行：空月跳过、0 与负数保留。同业务键重复行
    （多法人主体重复提报）金额求和；店铺→渠道挂接走种子映射，未命中
    mart_store_name/mart_channel 落 NULL（面板归「未挂渠道」）。
    """
    mapping = load_store_channel_mapping()
    agg = {}
    identities = []
    skipped = 0
    unmapped = set()
    source_count = 0

    def melt(sources, source_name):
        nonlocal skipped, source_count
        for row in sources:
            source_count += 1
            fin_store = row.get("store_name")
            fin_platform = _parse_name(row.get("platform"))
            mart_store, mart_channel = mapping.get(
                (fin_store, fin_platform), (None, None)
            )
            if mart_store is None and (fin_store or fin_platform):
                unmapped.add((fin_store, fin_platform))
            record_id = row.get("dingtalk_record_id")
            for column, month in _EXPENSE_MONTH_PAIRS:
                amount = row.get(column)
                if amount is None or str(amount).strip() == "":
                    skipped += 1
                    continue
                key = (
                    source_name, fin_store or "", fin_platform or "",
                    str(_parse_name(row.get("fee_category")) or ""),
                    str(_parse_name(row.get("fee_item")) or ""), month,
                )
                slot = agg.setdefault(key, [None, Decimal("0")])
                slot[0] = (mart_store, mart_channel)
                slot[1] = slot[1] + Decimal(str(amount))
                identities.append(f"{record_id}:{month}")

    melt(repository.read_table(dataset.source_table), "store_commission")
    melt(_read_recharge_rows(repository), "promotion_recharge")

    rows = []
    for key, (mart, amount) in agg.items():
        source_name, fin_store, fin_platform, fee_category, fee_item, month = key
        rows.append({
            "month": month,
            "source": source_name,
            "fin_store_name": fin_store or None,
            "fin_platform": fin_platform or None,
            "fee_category": fee_category or None,
            "fee_item": fee_item or None,
            "mart_store_name": mart[0],
            "mart_channel": mart[1],
            "expense_amount": amount,
            "synced_at": synced_at,
            "sync_run_id": run_id,
        })
    written = repository.replace_table(
        dataset.target_table,
        list(_STORE_EXPENSE_COLUMNS) + ["synced_at", "sync_run_id"],
        rows,
    )
    return {
        "records_read": source_count, "records_new": written,
        "records_updated": 0, "records_skipped": skipped,
        "unmapped_fin_stores": sorted(
            f"{store}（{platform}）" for store, platform in unmapped
        ),
        "_record_ids": identities,
    }
