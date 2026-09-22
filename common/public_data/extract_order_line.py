"""Order-line mart projection（B1-阶段二 Task 8/9）.

Two projectors, both WDT-backed and full-replay:

- ``project_dim_product_mirror`` mirrors ``raw_wdt.dim_product`` 1:1 into the
  mart ``dim_product`` table (product catalog projection).
- ``project_order_lines`` reads ``raw_wdt.wdt_records`` for
  ``sales.TradeQuery.queryWithDetail`` and expands each trade's
  ``detail_list`` into ``fact_order_line`` rows.

口径说明（§1.2 已用真实 payload 校准）：

- 交易级字段 ``trade_no`` / ``trade_time`` / ``trade_status`` /
  ``detail_list[].spec_no`` / ``goods_name`` / ``num`` / ``paid`` 均与线上
  一致；
- ``trade_status`` 为数值码，已付款计入、排除取消/关闭类（开放点 §7.3）：
  - ``4``  已取消（明细实付全 0）
  - ``24`` 待付款（明细实付全 0）
  - ``5``  全额退款关闭（全部明细行 ``refund_status=5``，实付已退回）
- 退款对账第一批次不减去部分退款金额（110 内少量 rfs=5 行保留毛额），
  补贴拆分（platform/shop）后续批次处理，本批次固定 0；
- 品牌经由 mart ``dim_product`` 反查回填，必须在同一 run 内先跑
  ``wdt_dim_product_mirror`` 再跑 ``wdt_order_line_fact``（注册顺序保证）；
  未匹配的 spec_no 回填 ``未匹配``；
- 店铺与渠道来自交易级 ``shop_name``：``shop_name`` 存原名，
  ``channel_name`` 按关键词归一化为渠道（抖音/拼多多/京东…），未命中
  归 ``其他``；两列由迁移 ``mart-extract-order-line-v2`` 建在
  fact_order_line 上。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

_TRADE_METHOD = "sales.TradeQuery.queryWithDetail"
_DIM_PRODUCT_SOURCE_TABLE = "dim_product"

_UNMATCHED_BRAND = "未匹配"

# 以 2026-09 真实数据校准（约 3.3 万行明细）：4=已取消、24=待付款（明细
# 实付均为 0）；5=全额退款关闭（明细 refund_status 全为 5）。
_EXCLUDED_TRADE_STATUSES = frozenset({"4", "5", "24"})

# 与 mart dim_product DDL 一一对应（镜像 raw_wdt.dim_product 业务列，
# 技术列 synced_at/sync_run_id 由 replace_table 统一附加）。
_DIM_PRODUCT_MIRROR_COLUMNS = (
    "spec_no",
    "barcode",
    "goods_id",
    "goods_no",
    "goods_name",
    "spec_name",
    "brand_name",
    "series_name",
    "class_name",
    "retail_price",
    "wholesale_price",
    "is_deleted",
    "raw_json",
)

# 与 mart fact_order_line DDL 一一对应（渠道两列由迁移
# mart-extract-order-line-v2 补建，见 mart_extract_schema）。
_ORDER_LINE_COLUMNS = (
    "trade_no",
    "line_no",
    "trade_time",
    "trade_status",
    "spec_no",
    "goods_name",
    "brand_name",
    "shop_name",
    "channel_name",
    "quantity",
    "paid_amount",
    "platform_subsidy",
    "shop_subsidy",
    "raw_json",
)

_UNMATCHED_CHANNEL = "其他"

# 店铺名 → 渠道：按此顺序扫描，命中最先出现的关键词即归属该渠道。
# 店铺名为源侧原名（如「习水村-抖音习酒窖藏1988酒类旗舰店」），
# 公司主体类店铺（如「杭州习水村酒业有限公司」）不含平台词，归「其他」。
_CHANNEL_KEYWORDS = (
    ("天猫超市", "猫超"),
    ("抖音", "抖音"),
    ("快手", "快手"),
    ("视频号", "视频号"),
    ("拼多多", "拼多多"),
    ("京东", "京东"),
    ("天猫", "天猫"),
    ("淘宝", "淘宝"),
    ("小红书", "小红书"),
    ("微信", "微信"),
)


def _normalize_channel(shop_name: str) -> str:
    """店铺原名 → 渠道名；空值或无关键词命中 → ``其他``。"""
    for keyword, channel in _CHANNEL_KEYWORDS:
        if keyword in shop_name:
            return channel
    return _UNMATCHED_CHANNEL


def _clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def _to_decimal(value: object) -> Decimal:
    if value in (None, "", "None"):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _to_int(value: object) -> int:
    if value in (None, "", "None"):
        return 0
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


# 交易级时间字段（trade_time/created 等）为 epoch 毫秒；``modified``
# 等为 "YYYY-MM-DD HH:MM:SS" 字符串（北京时间）。统一换算为北京时间
# naive datetime，与 raw/mart 其余表口径一致。
_CN_TZ = timezone(timedelta(hours=8))


def _from_epoch(value: float) -> datetime | None:
    # 13 位（>1e11）按毫秒处理，否则按秒处理。
    if value > 1e11:
        value = value / 1000.0
    try:
        return datetime.fromtimestamp(value, tz=_CN_TZ).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    text = _clean(value)
    if not text or text.startswith("0000-00-00"):
        return None
    if text.lstrip("-").replace(".", "", 1).isdigit():
        return _from_epoch(float(text))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _dump_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def project_dim_product_mirror(repository, dataset, run_id, synced_at) -> dict:
    """Mirror raw_wdt.dim_product into the mart dim_product table.

    镜像口径：raw 表 1:1 投影，不做过滤；空表直接清空（目录类全量镜像
    与 snapshot 语义不同——raw 侧本身就是 goods.Goods.query 全量结果，
    空表即代表源侧目录为空，mart 应如实反映）。
    """

    rows = repository.read_wdt_table(_DIM_PRODUCT_SOURCE_TABLE)
    mapped: list[dict] = []
    record_ids: list[str] = []
    for row in rows:
        spec_no = _clean(row.get("spec_no"))
        if not spec_no:
            continue
        raw = row.get("raw_json")
        record = {
            "spec_no": spec_no,
            "barcode": _clean(row.get("barcode")) or None,
            "goods_id": _clean(row.get("goods_id")) or None,
            "goods_no": _clean(row.get("goods_no")) or None,
            "goods_name": _clean(row.get("goods_name")) or None,
            "spec_name": _clean(row.get("spec_name")) or None,
            "brand_name": _clean(row.get("brand_name")) or None,
            "series_name": _clean(row.get("series_name")) or None,
            "class_name": _clean(row.get("class_name")) or None,
            "retail_price": _to_decimal(row.get("retail_price")),
            "wholesale_price": _to_decimal(row.get("wholesale_price")),
            "is_deleted": _to_int(row.get("is_deleted")),
            "raw_json": raw if isinstance(raw, str) else _dump_json(raw),
            "synced_at": synced_at,
            "sync_run_id": run_id,
        }
        mapped.append(record)
        record_ids.append(spec_no)
    written = repository.replace_table(
        dataset.target_table,
        list(_DIM_PRODUCT_MIRROR_COLUMNS) + ["synced_at", "sync_run_id"],
        mapped,
    )
    return {
        "records_read": len(rows),
        "records_new": written,
        "records_updated": 0,
        "records_skipped": 0,
        "_record_ids": record_ids,
    }


def _brand_index(repository) -> dict[str, str]:
    """spec_no → brand_name，来自同一 run 已刷新的 mart dim_product。"""

    index: dict[str, str] = {}
    # 只读反查所需两列（排查报告 2026-09-17 §2.3），避免全列载入。
    for row in repository.read_mart_table(
        "dim_product", columns=("spec_no", "brand_name")
    ):
        spec_no = _clean(row.get("spec_no"))
        brand = _clean(row.get("brand_name"))
        if spec_no and brand:
            index[spec_no] = brand
    return index


def project_order_lines(repository, dataset, run_id, synced_at) -> dict:
    """Expand raw WDT trade payloads into fact_order_line rows."""

    trades = repository.read_wdt_trades(_TRADE_METHOD)
    brand_index = _brand_index(repository)
    expanded: list[dict] = []
    record_ids: list[str] = []
    for trade in trades:
        payload = trade.get("payload_json")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        if not isinstance(payload, dict):
            continue
        trade_status = _clean(payload.get("trade_status"))
        if trade_status in _EXCLUDED_TRADE_STATUSES:
            continue
        trade_no = _clean(payload.get("trade_no"))
        if not trade_no:
            continue
        trade_time = _parse_datetime(payload.get("trade_time"))
        shop_name = _clean(payload.get("shop_name"))
        channel_name = _normalize_channel(shop_name)
        details = payload.get("detail_list") or []
        line_no = 0
        for detail in details:
            if not isinstance(detail, dict):
                continue
            spec_no = _clean(detail.get("spec_no"))
            if not spec_no:
                continue
            line_no += 1
            record = {
                "trade_no": trade_no,
                "line_no": line_no,
                "trade_time": trade_time,
                "trade_status": trade_status,
                "spec_no": spec_no,
                "goods_name": _clean(detail.get("goods_name")) or None,
                "brand_name": brand_index.get(spec_no, _UNMATCHED_BRAND),
                "shop_name": shop_name or None,
                "channel_name": channel_name,
                "quantity": _to_decimal(detail.get("num")),
                "paid_amount": _to_decimal(detail.get("paid")),
                "platform_subsidy": Decimal("0"),
                "shop_subsidy": Decimal("0"),
                "raw_json": _dump_json(detail),
                "synced_at": synced_at,
                "sync_run_id": run_id,
            }
            expanded.append(record)
            record_ids.append(f"{trade_no}:{line_no}")
    written = repository.replace_table(
        dataset.target_table,
        list(_ORDER_LINE_COLUMNS) + ["synced_at", "sync_run_id"],
        expanded,
    )
    return {
        "records_read": len(trades),
        "records_new": written,
        "records_updated": 0,
        "records_skipped": 0,
        "_record_ids": record_ids,
    }


__all__ = [
    "_CHANNEL_KEYWORDS",
    "_DIM_PRODUCT_MIRROR_COLUMNS",
    "_EXCLUDED_TRADE_STATUSES",
    "_ORDER_LINE_COLUMNS",
    "_TRADE_METHOD",
    "_UNMATCHED_BRAND",
    "_UNMATCHED_CHANNEL",
    "_normalize_channel",
    "project_dim_product_mirror",
    "project_order_lines",
]
