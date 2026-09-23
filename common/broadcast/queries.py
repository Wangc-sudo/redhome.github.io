# -*- coding: utf-8 -*-
"""渠道播报板块与 BI 共用的 mart 查询层（设计稿 §6.3 读取契约）。

每个函数返回 ``{"as_of": ..., "stale": bool, ...}``：页面/播报文案要显示
"数据截至"，跨天延迟不被误读为当日数据。所有阈值判定 push down 到 SQL
或在唯一的 Python 口径函数里，页面与 BI 不允许各算一遍。
"""

from __future__ import annotations

import contextlib
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# 口径常量（沿用旧脚本，页面/BI/未来触发式告警共用）
# ---------------------------------------------------------------------------

#: 热卖品名单大小与危险品保底门槛（hot_items_monitor.py TOP_N /
#: DANGER_MONTH_SALES）。
HOT_TOP_N = 10
DANGER_MONTH_SALES = 300

#: 只监控酒类（hot_items_monitor.py WINE/EXCLUDE_KEYWORDS 同口径）。
WINE_KEYWORDS = (
    "酒", "茅台", "习酒", "古越龙山", "女儿红", "金沙", "赖茅", "郎酒",
    "汾酒", "泸州", "洋河", "剑南春", "五粮液", "水井坊", "舍得", "口子窖",
)
EXCLUDE_KEYWORDS = ("赠品", "服务", "卡", "杯", "伞", "包材", "开瓶器", "酒具")

#: 订单风控：同店铺+同地区+同日 ≥ N 单（order_risk_alert.RISK_THRESHOLD），
#: 状态集合与旧播报一致（排除已取消；投影层已剔 4/5/24，这里按旧口径再收）。
RISK_THRESHOLD = 3
RISK_TRADE_STATUSES = ("95", "110", "30", "27", "55", "16")

#: 采购入库提醒：只统计已完成入库（purchase_alert.STATUS_DONE）。
PURCHASE_STATUS_DONE = "80"
PURCHASE_LOOKBACK_HOURS = 24

#: 热卖品排名/保底的销量窗口（旧脚本用 WDT num_month=近30天，DB 化后
#: 从 fact_sales_daily 自算，窗口保持一致）。
HOT_SALES_DAYS = 30


def is_wine(goods_name) -> bool:
    """酒类判定（赠品/服务/卡/包材等剔除）——与旧脚本逐字同口径。"""
    name = goods_name or ""
    if any(k in name for k in EXCLUDE_KEYWORDS):
        return False
    return any(k in name for k in WINE_KEYWORDS)


def platform_of(shop_name) -> str:
    """店铺名 → 展示平台（order_risk_alert.platform_of 同口径）。"""
    s = shop_name or ""
    if "抖音" in s:
        return "抖音"
    if "快手" in s:
        return "快手"
    if "视频号" in s:
        return "视频号"
    if "京东" in s:
        return "京东"
    if "天猫" in s or "淘宝" in s:
        return "天猫"
    if "拼多多" in s or "PDD" in s.upper():
        return "拼多多"
    return s.split("-")[0] if "-" in s else s


def _query(connection, sql, params=None):
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def _latest_inventory_date(connection, as_of):
    sql = "SELECT MAX(`business_date`) AS `d` FROM `fact_inventory_sku_daily`"
    params = None
    if as_of is not None:
        sql += " WHERE `business_date` <= %s"
        params = (as_of,)
    rows = _query(connection, sql, params)
    return rows[0]["d"] if rows and rows[0]["d"] else None


def fetch_inventory_snapshot(connection, *, as_of=None, today=None):
    """最新库存快照（fact_inventory_sku_daily 全量行）。

    *as_of*：取不超过该日的最新快照日；*today* 用于 stale 判定
    （快照日 < today 即标记延迟）。返回行含 stock_state / days_left /
    daily_avg / available_qty 等投影期算好的口径列。
    """
    snap_date = _latest_inventory_date(connection, as_of)
    if snap_date is None:
        return {"as_of": None, "stale": True, "rows": []}
    rows = _query(
        connection,
        "SELECT `spec_no`, `goods_name`, `available_qty`, `stock_qty`, "
        "`qty_7days`, `qty_month`, `purchase_intransit_qty`, `daily_avg`, "
        "`days_left`, `moving_window_days`, `is_moving`, `stock_state` "
        "FROM `fact_inventory_sku_daily` WHERE `business_date` = %s",
        (snap_date,),
    )
    reference = today or date.today()
    return {
        "as_of": snap_date,
        "stale": snap_date < reference,
        "rows": rows,
    }


def fetch_hot_items(connection, *, as_of=None, today=None,
                    top_n=HOT_TOP_N, danger_month_sales=DANGER_MONTH_SALES):
    """热卖品监控名单：30 天销量 TopN ∪ 危险品保底（断货/偏低且销量达标）。

    30 天销量从 ``fact_sales_daily`` 自算（窗口与旧 WDT num_month 一致），
    库存/可售天数/状态取最新快照；只监控酒类（:func:`is_wine`）。
    排序：状态（断货>偏低>正常）→ 30天销量↓ → 在途↓ → 库存↑（旧口径）。
    """
    snapshot = fetch_inventory_snapshot(connection, as_of=as_of, today=today)
    if snapshot["as_of"] is None:
        return {
            **snapshot, "items": [], "watch_count": 0, "coverage_pct": None,
            "moving_window_days": None,
        }

    snap_date = snapshot["as_of"]
    since = snap_date - timedelta(days=HOT_SALES_DAYS)
    sales_rows = _query(
        connection,
        "SELECT `spec_no` AS `spec_no`, SUM(`qty_sold`) AS `qty` "
        "FROM `fact_sales_daily` "
        "WHERE `business_date` > %s AND `business_date` <= %s "
        "GROUP BY `spec_no`",
        (since, snap_date),
    )
    sales_30d = {r["spec_no"]: float(r["qty"] or 0) for r in sales_rows}

    state_rank = {"OVERSOLD": 0, "URGENT": 1, "HEALTHY": 2, "DEAD": 3}
    items = []
    wine_total = 0.0
    for row in snapshot["rows"]:
        if not is_wine(row.get("goods_name")):
            continue
        spec = row["spec_no"]
        qty = sales_30d.get(spec, 0.0)
        wine_total += qty
        items.append({
            "spec_no": spec,
            "goods_name": row.get("goods_name"),
            "available_qty": float(row["available_qty"] or 0),
            "days_left": float(row["days_left"]) if row["days_left"] is not None else None,
            "purchase_intransit_qty": float(row["purchase_intransit_qty"] or 0),
            "stock_state": row["stock_state"],
            "qty_30d": qty,
        })

    top = sorted(items, key=lambda x: -x["qty_30d"])[:top_n]
    top_specs = {x["spec_no"] for x in top}
    watch = list(top)
    for item in items:
        if item["spec_no"] in top_specs:
            continue
        if item["stock_state"] in ("OVERSOLD", "URGENT") and item["qty_30d"] >= danger_month_sales:
            watch.append(item)
    watch.sort(key=lambda x: (
        state_rank.get(x["stock_state"], 9),
        -x["qty_30d"],
        -x["purchase_intransit_qty"],
        x["available_qty"],
    ))
    watched_qty = sum(x["qty_30d"] for x in watch)
    return {
        "as_of": snap_date,
        "stale": snapshot["stale"],
        "items": watch,
        "watch_count": len(watch),
        "danger_count": sum(1 for x in watch if x["stock_state"] == "URGENT"),
        "oversold_count": sum(1 for x in watch if x["stock_state"] == "OVERSOLD"),
        "coverage_pct": (watched_qty / wine_total) if wine_total else None,
        "moving_window_days": (
            snapshot["rows"][0]["moving_window_days"] if snapshot["rows"] else None
        ),
    }


def fetch_stock_alerts(connection, *, as_of=None, today=None):
    """库存预警：紧急补货（URGENT，可售天数升序）+ 超卖/零库存（OVERSOLD）。

    OVERSOLD 投影口径 = 可发 ≤ 0 且 30 天内仍在卖，本层细分两档：
    * ``oversold``：真超卖（available < 0），缺口大在前；
    * ``zero_stock``：库存恰好为 0 但仍在卖——单独成组，避免满屏
      "超卖 0"噪音（2026-09-23 核查：26 行里 22 行超卖数量为 0）。

    剔除非商品行（赠品/服务卡/包材等，EXCLUDE_KEYWORDS 排除法）——
    不用 :func:`is_wine` 白名单：古越金三年/舍之道/鉴湖等真酒品名不含
    关键词，白名单会误伤（2026-09-23 实测），而噪音来源只是赠品
    "服务升级卡"这类非商品行。

    去重状态机（旧 .stock_alert_state.json）不进本层——静态页按日快照
    展示全量当前状态；若未来恢复触发式播报，去重走 broadcast_dedup 表。
    """
    snapshot = fetch_inventory_snapshot(connection, as_of=as_of, today=today)
    wine_rows = [
        r for r in snapshot["rows"]
        if not any(k in (r.get("goods_name") or "") for k in EXCLUDE_KEYWORDS)
    ]
    urgent = sorted(
        (r for r in wine_rows if r["stock_state"] == "URGENT"),
        key=lambda r: (float(r["days_left"] or 0), r["spec_no"]),
    )
    oversold_all = [r for r in wine_rows if r["stock_state"] == "OVERSOLD"]
    oversold = sorted(
        (r for r in oversold_all if float(r["available_qty"] or 0) < 0),
        key=lambda r: (float(r["available_qty"] or 0), r["spec_no"]),
    )
    zero_stock = sorted(
        (r for r in oversold_all if float(r["available_qty"] or 0) == 0),
        key=lambda r: (-(float(r["daily_avg"] or 0)), r["spec_no"]),
    )
    return {
        "as_of": snapshot["as_of"],
        "stale": snapshot["stale"],
        "urgent": urgent,
        "oversold": oversold,
        "zero_stock": zero_stock,
        "moving_window_days": (
            snapshot["rows"][0]["moving_window_days"] if snapshot["rows"] else None
        ),
    }


def fetch_purchase_inbound(connection, *, now, hours=PURCHASE_LOOKBACK_HOURS):
    """采购入库提醒：最近 *hours* 小时内已完成入库（status=80）的明细。

    按仓库分组、同 SKU 跨单合并数量、数量降序（旧 build_markdown 口径）。
    群播报时代的"今日已报去重"不适用于静态页——页面就是当前事实。
    """
    since = now - timedelta(hours=hours)
    rows = _query(
        connection,
        "SELECT `order_no`, `spec_no`, `goods_name`, `warehouse_no`, "
        "`warehouse_name`, `qty`, `stockin_time` "
        "FROM `fact_purchase_inbound` "
        "WHERE `status` = %s AND `stockin_time` >= %s AND `stockin_time` <= %s",
        (PURCHASE_STATUS_DONE, since, now),
    )
    warehouses: dict[str, dict] = {}
    for row in rows:
        name = row["warehouse_name"] or row["warehouse_no"] or "未知仓"
        slot = warehouses.setdefault(name, {"specs": {}, "orders": set(), "lines": 0})
        slot["lines"] += 1
        slot["orders"].add(row["order_no"])
        spec_slot = slot["specs"].setdefault(row["spec_no"], {
            "spec_no": row["spec_no"],
            "goods_name": row.get("goods_name"),
            "qty": 0.0,
        })
        spec_slot["qty"] += float(row["qty"] or 0)
    groups = []
    total_qty = 0.0
    for name, slot in sorted(warehouses.items()):
        specs = sorted(slot["specs"].values(), key=lambda x: -x["qty"])
        group_qty = sum(x["qty"] for x in specs)
        total_qty += group_qty
        groups.append({
            "warehouse_name": name,
            "order_count": len(slot["orders"]),
            "line_count": slot["lines"],
            "specs": specs,
            "total_qty": group_qty,
        })
    return {
        "as_of": now,
        "stale": False,
        "groups": groups,
        "total_lines": len(rows),
        "total_specs": sum(len(g["specs"]) for g in groups),
        "total_orders": sum(g["order_count"] for g in groups),
        "total_qty": total_qty,
    }


def _area_incomplete(area):
    """地区归一后区级缺失（"浙江省杭州市-"）：隐私/掩码导致，非真实集中。"""
    return str(area or "").endswith("-")


def fetch_order_risk(connection, *, business_date, threshold=RISK_THRESHOLD):
    """订单风控：昨日同店铺+同地区 ≥ threshold 单的分组（count distinct 订单）。

    ``receiver_area_norm`` 为 NULL 的订单不参与分组（旧口径：无地区跳过）；
    拼多多无地区单数单列返回 ``pdd_no_area_count``（设计稿 §4.3：
    源头限制必须显式计数，不得静默少单）。

    地区区级缺失的分组拆到 ``incomplete_groups``（2026-09-23 核查：店铺
    "杭州习水村酒业有限公司"全部订单归一为"浙江省杭州市-"，802 单/日恒触发
    ≥3 规则，属于地区字段不全的系统性误报，不参与风险判定、仅供参考）。
    """
    placeholders = ", ".join(["%s"] * len(RISK_TRADE_STATUSES))
    rows = _query(
        connection,
        "SELECT `shop_name` AS `shop_name`, `receiver_area_norm` AS `area`, "
        "COUNT(DISTINCT `trade_no`) AS `n` "
        "FROM `fact_order_line` "
        "WHERE `trade_time` >= %s AND `trade_time` < %s "
        f"AND `trade_status` IN ({placeholders}) "
        "AND `receiver_area_norm` IS NOT NULL "
        "GROUP BY `shop_name`, `receiver_area_norm` "
        "HAVING COUNT(DISTINCT `trade_no`) >= %s",
        (
            business_date,
            business_date + timedelta(days=1),
            *RISK_TRADE_STATUSES,
            threshold,
        ),
    )
    shops: dict[str, list] = {}
    for row in rows:
        shops.setdefault(row["shop_name"] or "?", []).append(
            {"area": row["area"], "count": int(row["n"])}
        )
    groups = [
        {
            "shop_name": shop,
            "platform": platform_of(shop),
            "areas": sorted(areas, key=lambda x: -x["count"]),
            "total": sum(a["count"] for a in areas),
        }
        for shop, areas in shops.items()
    ]
    groups.sort(key=lambda g: (-len(g["areas"]), -g["total"], g["shop_name"]))
    # 地区区级缺失（"…-"）的分组拆出：不参与风险判定，仅供参考
    complete_groups = []
    incomplete_groups = []
    for g in groups:
        if g["areas"] and all(_area_incomplete(a["area"]) for a in g["areas"]):
            incomplete_groups.append(g)
        else:
            complete_groups.append(g)
    groups = complete_groups

    pdd = _query(
        connection,
        "SELECT COUNT(DISTINCT `trade_no`) AS `n` FROM `fact_order_line` "
        "WHERE `trade_time` >= %s AND `trade_time` < %s "
        f"AND `trade_status` IN ({placeholders}) "
        "AND `receiver_area_norm` IS NULL AND `channel_name` = '拼多多'",
        (business_date, business_date + timedelta(days=1), *RISK_TRADE_STATUSES),
    )
    return {
        "as_of": business_date,
        "stale": business_date < (date.today() - timedelta(days=1)),
        "groups": groups,
        "incomplete_groups": incomplete_groups,
        "pdd_no_area_count": int(pdd[0]["n"]) if pdd else 0,
        "threshold": threshold,
    }


__all__ = [
    "DANGER_MONTH_SALES",
    "EXCLUDE_KEYWORDS",
    "HOT_SALES_DAYS",
    "HOT_TOP_N",
    "PURCHASE_LOOKBACK_HOURS",
    "PURCHASE_STATUS_DONE",
    "RISK_THRESHOLD",
    "RISK_TRADE_STATUSES",
    "WINE_KEYWORDS",
    "fetch_hot_items",
    "fetch_inventory_snapshot",
    "fetch_order_risk",
    "fetch_purchase_inbound",
    "fetch_stock_alerts",
    "is_wine",
    "platform_of",
]
