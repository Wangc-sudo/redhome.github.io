"""Stock-flow mart projections（仓库运作，2026-09-21）.

Two projectors, both WDT-backed and full-replay（与 fact_order_line 同范式）:

- ``project_stockout_lines`` reads ``raw_wdt.wdt_records`` for
  ``wms.stockout.Sales.queryWithDetail`` and expands each outbound order's
  ``details_list`` into ``fact_stockout_line`` rows.
- ``project_refund_lines`` reads ``raw_wdt.wdt_records`` for
  ``wms.stockin.Refund.queryWithDetail`` and expands each return-inbound
  order's ``details_list`` into ``fact_refund_line`` rows.

口径说明（已用 2026-09-21 真实 payload 校准）：

- 出库单头 ``order_no`` / ``consign_time``（发货时间，"YYYY-MM-DD HH:MM:SS"
  北京时间）/ ``status`` / ``warehouse_no`` / ``shop_name`` / ``trade_no``
  / ``logistics_no``；明细 ``spec_no`` / ``goods_name`` / ``brand_name`` /
  ``num`` / ``sell_price`` / ``paid``。
- 退货单头 ``order_no``（入库单号）/ ``refund_no``（退货单号）/
  ``check_time``（审核=入库时间，epoch 毫秒）/ ``process_status`` /
  ``reason``；明细 ``num`` / ``stockin_num`` / ``refund_amount`` /
  ``actual_refund_amount``。
- 品牌直接取明细 payload 的 ``brand_name``（WDT 侧已带，不经 dim_product
  反查）；为空回填 ``未匹配``，与 fact_order_line 口径一致。
- ``channel_name`` 由 ``shop_name`` 关键词归一（复用订单行同一函数），
  未命中归 ``其他``。
- 状态过滤刻意不做：采集窗口已按发货/入库时间圈定，已取消单不会出现在
  结果集；状态码原样落列，口径变化由消费侧决定。
"""

from __future__ import annotations

import json

from common.public_data.extract_order_line import (
    _UNMATCHED_BRAND,
    _clean,
    _dump_json,
    _normalize_channel,
    _parse_datetime,
    _to_decimal,
)

_STOCKOUT_METHOD = "wms.stockout.Sales.queryWithDetail"
_REFUND_METHOD = "wms.stockin.Refund.queryWithDetail"

# 与 mart fact_stockout_line DDL 一一对应。
_STOCKOUT_LINE_COLUMNS = (
    "order_no",
    "line_no",
    "consign_time",
    "status",
    "warehouse_no",
    "warehouse_name",
    "shop_name",
    "channel_name",
    "trade_no",
    "logistics_no",
    "logistics_name",
    "spec_no",
    "goods_name",
    "brand_name",
    "quantity",
    "sell_price",
    "paid_amount",
    "raw_json",
)

# 与 mart fact_refund_line DDL 一一对应。
_REFUND_LINE_COLUMNS = (
    "order_no",
    "line_no",
    "refund_no",
    "check_time",
    "process_status",
    "warehouse_no",
    "shop_name",
    "channel_name",
    "reason",
    "logistics_no",
    "spec_no",
    "goods_name",
    "brand_name",
    "quantity",
    "stockin_quantity",
    "refund_amount",
    "actual_refund_amount",
    "raw_json",
)


def _load_payload(row: dict) -> dict | None:
    payload = row.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    return payload if isinstance(payload, dict) else None


def _expand(repository, dataset, run_id, synced_at, *, method, build_row) -> dict:
    """Shared expand-and-replace flow for both stock-flow projectors.

    *build_row(header, detail, line_no)* returns one fact row dict, or
    ``None`` to skip the detail.
    """

    orders = repository.read_wdt_trades(method)
    expanded: list[dict] = []
    record_ids: list[str] = []
    for row in orders:
        payload = _load_payload(row)
        if payload is None:
            continue
        order_no = _clean(payload.get("order_no"))
        if not order_no:
            continue
        line_no = 0
        for detail in payload.get("details_list") or []:
            if not isinstance(detail, dict):
                continue
            if not _clean(detail.get("spec_no")):
                continue
            line_no += 1
            record = build_row(payload, detail, order_no, line_no)
            if record is None:
                continue
            record["synced_at"] = synced_at
            record["sync_run_id"] = run_id
            expanded.append(record)
            record_ids.append(f"{order_no}:{line_no}")
    written = repository.replace_table(
        dataset.target_table,
        list(dataset_columns(dataset)) + ["synced_at", "sync_run_id"],
        expanded,
    )
    return {
        "records_read": len(orders),
        "records_new": written,
        "records_updated": 0,
        "records_skipped": 0,
        "_record_ids": record_ids,
    }


def dataset_columns(dataset) -> tuple:
    """Column tuple for *dataset*'s expand kind (empty-table fallback)."""

    if dataset.kind == "stockout_line_expand":
        return _STOCKOUT_LINE_COLUMNS
    if dataset.kind == "refund_line_expand":
        return _REFUND_LINE_COLUMNS
    raise ValueError(f"unknown stock-flow kind: {dataset.kind!r}")


def project_stockout_lines(repository, dataset, run_id, synced_at) -> dict:
    """Expand raw WDT sales-outbound payloads into fact_stockout_line rows."""

    def build_row(header, detail, order_no, line_no):
        shop_name = _clean(header.get("shop_name"))
        brand = _clean(detail.get("brand_name"))
        return {
            "order_no": order_no,
            "line_no": line_no,
            "consign_time": _parse_datetime(header.get("consign_time")),
            "status": _clean(header.get("status")) or None,
            "warehouse_no": _clean(header.get("warehouse_no")) or None,
            "warehouse_name": _clean(header.get("warehouse_name")) or None,
            "shop_name": shop_name or None,
            "channel_name": _normalize_channel(shop_name),
            "trade_no": _clean(header.get("trade_no")) or None,
            "logistics_no": _clean(header.get("logistics_no")) or None,
            "logistics_name": _clean(header.get("logistics_name")) or None,
            "spec_no": _clean(detail.get("spec_no")),
            "goods_name": _clean(detail.get("goods_name")) or None,
            "brand_name": brand or _UNMATCHED_BRAND,
            "quantity": _to_decimal(detail.get("num")),
            "sell_price": _to_decimal(detail.get("sell_price")),
            "paid_amount": _to_decimal(detail.get("paid")),
            "raw_json": _dump_json(detail),
        }

    return _expand(
        repository, dataset, run_id, synced_at,
        method=_STOCKOUT_METHOD, build_row=build_row,
    )


def project_refund_lines(repository, dataset, run_id, synced_at) -> dict:
    """Expand raw WDT refund-inbound payloads into fact_refund_line rows."""

    def build_row(header, detail, order_no, line_no):
        shop_name = _clean(header.get("shop_name"))
        brand = _clean(detail.get("brand_name"))
        return {
            "order_no": order_no,
            "line_no": line_no,
            "refund_no": _clean(header.get("refund_no")) or None,
            "check_time": _parse_datetime(
                header.get("check_time") or header.get("created_time")
            ),
            "process_status": _clean(header.get("process_status")) or None,
            "warehouse_no": _clean(header.get("warehouse_no")) or None,
            "shop_name": shop_name or None,
            "channel_name": _normalize_channel(shop_name),
            "reason": _clean(header.get("reason")) or None,
            "logistics_no": _clean(header.get("logistics_no")) or None,
            "spec_no": _clean(detail.get("spec_no")),
            "goods_name": _clean(detail.get("goods_name")) or None,
            "brand_name": brand or _UNMATCHED_BRAND,
            "quantity": _to_decimal(detail.get("num")),
            "stockin_quantity": _to_decimal(detail.get("stockin_num")),
            "refund_amount": _to_decimal(detail.get("refund_amount")),
            "actual_refund_amount": _to_decimal(
                detail.get("actual_refund_amount")
            ),
            "raw_json": _dump_json(detail),
        }

    return _expand(
        repository, dataset, run_id, synced_at,
        method=_REFUND_METHOD, build_row=build_row,
    )


__all__ = [
    "_REFUND_LINE_COLUMNS",
    "_REFUND_METHOD",
    "_STOCKOUT_LINE_COLUMNS",
    "_STOCKOUT_METHOD",
    "project_refund_lines",
    "project_stockout_lines",
]
