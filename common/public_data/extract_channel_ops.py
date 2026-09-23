# -*- coding: utf-8 -*-
"""渠道播报 DB 化的三个投影器（2026-09-23）。

设计稿：docs/superpowers/specs/2026-09-17-broadcast-alerts-db-sourcing-design.md
+ 2026-09-17-inventory-alert-db-design.md（§9 动销窗口用户裁定）。

- ``project_sales_daily``：mart ``fact_stockout_line`` 聚合成
  ``fact_sales_daily``（SKU×仓×日），全量替换（派生表，确定性重建）。
  零新增 WDT 调用（设计稿 §3.1.1 路径 B 的数据已在库）。
- ``project_inventory_snapshot``：raw ``wdt_records`` 的
  ``wms.StockSpec.search2`` payload → ``fact_inventory_sku_daily`` 当日
  快照。跨仓（01 习水村 + 12 杭易）合并为 ``warehouse_scope='ALL'``；
  ``daily_avg``/``days_left``/``stock_state`` 在投影期一次算死
  （播报与看板共用），写法语义 = 当日 DELETE + INSERT（历史日保留）。
- ``project_purchase_inbound``：raw 采购入库单 payload 展开成
  ``fact_purchase_inbound`` 行，append 语义（PK 幂等 upsert）。

注册顺序即依赖顺序：``wdt_sales_daily`` 必须先于
``wdt_inventory_sku_daily``（daily_avg 的数据源）。
"""

from __future__ import annotations

from datetime import timedelta, timezone

from common.broadcast import moving_window_days
from common.public_data.extract_order_line import (
    _clean,
    _parse_datetime,
    _to_decimal,
)
from common.public_data.extract_stock_flow import _load_payload

_STOCK_SPEC_METHOD = "wms.StockSpec.search2"
_PURCHASE_METHOD = "wms.stockin.Purchase.queryWithDetail"

#: 跨仓合并范围（沿用旧脚本的 WAREHOUSE_MAP：跨仓可调拨，合并监控）。
WAREHOUSE_MAP = {"01": "习水村", "12": "杭易"}

#: 紧急补货阈值（沿用 stock_alert.py URGENT_DAYS）。
URGENT_DAYS = 7

#: 「仍在卖」兜底判定的回看天数（沿用 stock_alert 30 天口径：断货后
#: 窗口日销归零，但 30 天内卖过就持续标记超卖/紧急，不静默）。
ACTIVE_LOOKBACK_DAYS = 30

# 与 mart DDL 一一对应。
_SALES_DAILY_COLUMNS = (
    "business_date",
    "spec_no",
    "warehouse_no",
    "qty_sold",
)

_INVENTORY_COLUMNS = (
    "business_date",
    "spec_no",
    "warehouse_scope",
    "goods_name",
    "available_qty",
    "stock_qty",
    "qty_7days",
    "qty_month",
    "purchase_intransit_qty",
    "daily_avg",
    "days_left",
    "moving_window_days",
    "is_moving",
    "stock_state",
)

_PURCHASE_INBOUND_COLUMNS = (
    "order_no",
    "spec_no",
    "purchase_no",
    "warehouse_no",
    "warehouse_name",
    "goods_name",
    "qty",
    "stockin_time",
    "status",
)


_CN_TZ = timezone(timedelta(hours=8))


def _business_date_of(synced_at):
    """``synced_at``（extract 服务固定 UTC 口径）→ 北京业务日。

    2026-09-23 实锤：直接 ``synced_at.date()`` 会让 08:00 北京
    （= 00:00 UTC）的快照落成前一日，"数据截至"整天错位；统一经
    +08:00 换算。naive 输入按 UTC 处理（生产路径均为 aware UTC）。
    """
    if not hasattr(synced_at, "date"):
        return synced_at
    ts = synced_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(_CN_TZ).date()


def dataset_columns(dataset) -> tuple:
    """Column tuple for *dataset*'s kind (column whitelist wiring)."""

    if dataset.kind == "sales_daily_aggregate":
        return _SALES_DAILY_COLUMNS
    if dataset.kind == "inventory_snapshot":
        return _INVENTORY_COLUMNS
    if dataset.kind == "purchase_inbound_expand":
        return _PURCHASE_INBOUND_COLUMNS
    raise ValueError(f"unknown channel-ops kind: {dataset.kind!r}")


def project_sales_daily(repository, dataset, run_id, synced_at) -> dict:
    """fact_stockout_line → fact_sales_daily（全量替换）。"""

    rows = repository.read_stockout_daily_sales()
    mapped = [
        {
            "business_date": row["business_date"],
            "spec_no": row["spec_no"],
            "warehouse_no": row["warehouse_no"],
            "qty_sold": row["qty_sold"],
            "synced_at": synced_at,
            "sync_run_id": run_id,
        }
        for row in rows
    ]
    written = repository.replace_sales_daily(
        dataset,
        list(_SALES_DAILY_COLUMNS) + ["synced_at", "sync_run_id"],
        mapped,
    )
    return {
        "records_read": len(rows),
        "records_new": written,
        "records_updated": 0,
        "records_skipped": 0,
        "_record_ids": [
            f"{row['business_date']}:{row['spec_no']}:{row['warehouse_no']}"
            for row in rows
        ],
    }


def _classify_stock(*, available, daily_avg, days_left, was_active):
    """投影期一次性判定 stock_state（播报/看板唯一口径）。

    * OVERSOLD：可发 ≤ 0 且 30 天内仍在卖（滞销负库存不算超卖）；
    * URGENT：可发 > 0、有窗口动销、可售天数 ≤ URGENT_DAYS；
    * DEAD：30 天无动销；
    * HEALTHY：其余。
    """
    if not was_active:
        return "DEAD"
    if available <= 0:
        return "OVERSOLD"
    if daily_avg and daily_avg > 0 and days_left is not None and days_left <= URGENT_DAYS:
        return "URGENT"
    return "HEALTHY"


def project_inventory_snapshot(repository, dataset, run_id, synced_at) -> dict:
    """wms.StockSpec.search2 payload → 当日库存快照（跨仓合并）。"""

    records = repository.read_wdt_trades(_STOCK_SPEC_METHOD)
    window_days = moving_window_days()
    business_date = _business_date_of(synced_at)

    # 跨仓合并：spec_no → 累加行（只留 01/12 两仓正品，口径同旧脚本）。
    merged: dict[str, dict] = {}
    for row in records:
        payload = _load_payload(row)
        if payload is None:
            continue
        if _clean(payload.get("warehouse_no")) not in WAREHOUSE_MAP:
            continue
        if _clean(payload.get("defect")) not in ("", "0", "false", "False"):
            continue  # 只保留正品（旧脚本 defect 过滤口径）
        spec_no = _clean(payload.get("spec_no"))
        if not spec_no:
            continue
        slot = merged.setdefault(spec_no, {
            "goods_name": _clean(payload.get("goods_name")) or None,
            "available_qty": 0.0,
            "stock_qty": 0.0,
            "qty_7days": None,
            "qty_month": None,
            "purchase_intransit_qty": 0.0,
        })
        slot["goods_name"] = slot["goods_name"] or (
            _clean(payload.get("goods_name")) or None
        )
        slot["available_qty"] += float(_to_decimal(payload.get("available_send_stock")))
        slot["stock_qty"] += float(_to_decimal(payload.get("stock_num")))
        slot["purchase_intransit_qty"] += float(_to_decimal(payload.get("purchase_num")))
        # WDT 原生桶值（mask=1 才有；存量无 mask 的行保持 None）：跨仓求和。
        for src_key, dst_key in (("num_7days", "qty_7days"), ("num_month", "qty_month")):
            raw_val = payload.get(src_key)
            if raw_val in (None, ""):
                continue
            slot[dst_key] = (slot[dst_key] or 0.0) + float(_to_decimal(raw_val))

    # 窗口/兜底销量：投影期从 fact_sales_daily 一次算好。
    sales = repository.read_sales_window_sums(
        business_date,
        window_days=window_days,
        active_days=ACTIVE_LOOKBACK_DAYS,
    )

    rows: list[dict] = []
    for spec_no, slot in sorted(merged.items()):
        window_qty, active_qty = sales.get(spec_no, (0.0, 0.0))
        was_active = active_qty > 0
        daily_avg = window_qty / window_days if window_qty > 0 else None
        days_left = (
            slot["available_qty"] / daily_avg if daily_avg else None
        )
        rows.append({
            "business_date": business_date,
            "spec_no": spec_no,
            "warehouse_scope": "ALL",
            "goods_name": slot["goods_name"],
            "available_qty": slot["available_qty"],
            "stock_qty": slot["stock_qty"],
            "qty_7days": slot["qty_7days"],
            "qty_month": slot["qty_month"],
            "purchase_intransit_qty": slot["purchase_intransit_qty"],
            "daily_avg": daily_avg,
            "days_left": days_left,
            "moving_window_days": window_days,
            "is_moving": 1 if was_active else 0,
            "stock_state": _classify_stock(
                available=slot["available_qty"],
                daily_avg=daily_avg,
                days_left=days_left,
                was_active=was_active,
            ),
            "synced_at": synced_at,
            "sync_run_id": run_id,
        })

    written = repository.replace_inventory_day(
        dataset,
        list(_INVENTORY_COLUMNS) + ["synced_at", "sync_run_id"],
        rows,
        business_date,
    )
    return {
        "records_read": len(records),
        "records_new": written,
        "records_updated": 0,
        "records_skipped": 0,
        "_record_ids": [f"{business_date}:{spec}" for spec in sorted(merged)],
    }


def project_purchase_inbound(repository, dataset, run_id, synced_at) -> dict:
    """采购入库单 payload → fact_purchase_inbound（append/upsert）。"""

    orders = repository.read_wdt_trades(_PURCHASE_METHOD)
    rows: list[dict] = []
    record_ids: list[str] = []
    for row in orders:
        payload = _load_payload(row)
        if payload is None:
            continue
        order_no = _clean(payload.get("order_no"))
        if not order_no:
            continue
        purchase_no = _clean(payload.get("purchase_no")) or None
        warehouse_no = _clean(payload.get("warehouse_no")) or None
        warehouse_name = WAREHOUSE_MAP.get(warehouse_no, warehouse_no)
        stockin_time = _parse_datetime(
            payload.get("check_time") or payload.get("created_time")
        )
        status = _clean(payload.get("status")) or None
        for detail in payload.get("details_list") or []:
            if not isinstance(detail, dict):
                continue
            spec_no = _clean(detail.get("spec_no"))
            if not spec_no:
                continue
            goods_name = (
                _clean(detail.get("spec_name"))
                or _clean(detail.get("goods_name"))
                or None
            )
            rows.append({
                "order_no": order_no,
                "spec_no": spec_no,
                "purchase_no": purchase_no,
                "warehouse_no": warehouse_no,
                "warehouse_name": warehouse_name,
                "goods_name": goods_name,
                "qty": _to_decimal(detail.get("num")),
                "stockin_time": stockin_time,
                "status": status,
                "synced_at": synced_at,
                "sync_run_id": run_id,
            })
            record_ids.append(f"{order_no}:{spec_no}")

    written = repository.upsert_purchase_inbound(
        dataset,
        list(_PURCHASE_INBOUND_COLUMNS) + ["synced_at", "sync_run_id"],
        rows,
    )
    return {
        "records_read": len(orders),
        "records_new": written,
        "records_updated": 0,
        "records_skipped": 0,
        "_record_ids": record_ids,
    }


__all__ = [
    "ACTIVE_LOOKBACK_DAYS",
    "URGENT_DAYS",
    "WAREHOUSE_MAP",
    "dataset_columns",
    "project_inventory_snapshot",
    "project_purchase_inbound",
    "project_sales_daily",
]
