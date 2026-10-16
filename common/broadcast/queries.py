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


#: 月度看板展示的月份数（最近 N 个月，含本月）。
MONTHLY_KPI_MONTHS = 6


def fetch_monthly_kpi(connection, *, months_back=MONTHLY_KPI_MONTHS):
    """月度核心指标看板（P0）：GMV/订单数/客单价/买家/退款/净额/推广/ROI/取消。

    数据源与口径：

    * ``fact_order_line`` —— GMV = Σ paid_amount，已付款且未取消（投影层已
      过滤 trade_status∈{4,5,24}，即剔除已取消/待付款/全额退款关闭）；
      订单数 = COUNT DISTINCT trade_no；按 trade_time（付款时间）归月。
    * ``fact_order_line`` 买家资产 —— 活跃买家 = COUNT(DISTINCT buyer_id)
      （WDT 交易级 customer_id；拼多多按隐私协议不返回 → NULL 不计入，
      buyer_null_share 显式给出缺失行占比）；新客 = 首购月落在当月的买家数；
      复购率 = (活跃买家 − 新客) ÷ 活跃买家。
    * ``fact_order_cancel_line`` —— 取消单 = COUNT DISTINCT trade_no
      （status=4，主投影剔除单），按 cancel_time（下单时间）归月；
      取消率 = 取消单 ÷ (付款订单数 + 取消单数)。
    * ``fact_refund_line`` —— 退款金额 = Σ actual_refund_amount，退款单数 =
      COUNT DISTINCT refund_no；按 check_time（入库审核时间）归月。
    * ``fact_channel_daily_sales`` —— 推广花费 = Σ promotion_cost；按
      business_date 归月。
    * ``data_start`` = MIN(trade_time)，供残月标注（WDT 全量重放自
      2026-08-22 起，8 月为残月、环比失真，面板须显式标注）。

    缺失指标（面板显式标注）：同比 YoY —— fact_order_line 为 WDT 全量
    重放，无去年同期数据。

    返回 ``{"months": [...], "as_of": date, "data_start": ..., "buyer_null_share": float|None}``，
    months 按月升序、取最近 *months_back* 个月。每行含 ym/gmv/mom/orders/
    aov/buyers/new_buyers/repeat_rate/cancel_orders/cancel_rate/refund_amt/
    refund_rate_amt/refund_orders/refund_rate_cnt/net_gmv/promo/roi。
    """
    # GMV + 订单数 + 买家数（按月，付款时间口径）。用 YEAR()/MONTH() 而非
    # DATE_FORMAT 以避开 MySQL 驱动对 %Y-%m 的占位符歧义。
    gmv_rows = _query(
        connection,
        "SELECT YEAR(`trade_time`) AS `y`, MONTH(`trade_time`) AS `m`, "
        "SUM(`paid_amount`) AS `gmv`, "
        "COUNT(DISTINCT `trade_no`) AS `orders`, "
        "COUNT(DISTINCT `buyer_id`) AS `buyers` "
        "FROM `fact_order_line` "
        "WHERE `trade_time` IS NOT NULL "
        "GROUP BY `y`, `m` ORDER BY `y`, `m`",
    )
    # 买家首购月（全表一遍，买家数远小于行数）：新客 = 首购月=当月。
    first_rows = _query(
        connection,
        "SELECT `buyer_id` AS `buyer`, "
        "MIN(YEAR(`trade_time`) * 100 + MONTH(`trade_time`)) AS `first_ym` "
        "FROM `fact_order_line` "
        "WHERE `buyer_id` IS NOT NULL AND `trade_time` IS NOT NULL "
        "GROUP BY `buyer_id`",
    )
    new_map: dict[str, int] = {}
    for r in first_rows:
        first_ym = int(r["first_ym"])
        key = f"{first_ym // 100:04d}-{first_ym % 100:02d}"
        new_map[key] = new_map.get(key, 0) + 1
    # 取消单（按下单时间归月）
    cancel_rows = _query(
        connection,
        "SELECT YEAR(`cancel_time`) AS `y`, MONTH(`cancel_time`) AS `m`, "
        "COUNT(DISTINCT `trade_no`) AS `cancels` "
        "FROM `fact_order_cancel_line` "
        "WHERE `cancel_time` IS NOT NULL "
        "GROUP BY `y`, `m`",
    )
    cancel_map = {
        f"{r['y']:04d}-{r['m']:02d}": int(r["cancels"] or 0) for r in cancel_rows
    }
    # 数据起点与买家缺失占比（残月标注 / 拼多多隐私说明）
    meta_rows = _query(
        connection,
        "SELECT MIN(`trade_time`) AS `data_start`, COUNT(*) AS `n`, "
        "SUM(`buyer_id` IS NULL) AS `buyer_null` FROM `fact_order_line`",
    )
    meta = meta_rows[0] if meta_rows else {}
    total_lines = int(meta.get("n") or 0)
    buyer_null = int(meta.get("buyer_null") or 0)
    # 退款（按月，入库审核时间口径）
    refund_rows = _query(
        connection,
        "SELECT YEAR(`check_time`) AS `y`, MONTH(`check_time`) AS `m`, "
        "SUM(`actual_refund_amount`) AS `refund_amt`, "
        "COUNT(DISTINCT `refund_no`) AS `refund_orders` "
        "FROM `fact_refund_line` "
        "WHERE `check_time` IS NOT NULL "
        "GROUP BY `y`, `m`",
    )
    refund_map = {
        f"{r['y']:04d}-{r['m']:02d}": r for r in refund_rows
    }
    # 推广花费（按月，账单日期口径）
    promo_rows = _query(
        connection,
        "SELECT YEAR(`business_date`) AS `y`, MONTH(`business_date`) AS `m`, "
        "SUM(`promotion_cost`) AS `promo` "
        "FROM `fact_channel_daily_sales` "
        "WHERE `business_date` IS NOT NULL AND `promotion_cost` IS NOT NULL "
        "GROUP BY `y`, `m`",
    )
    promo_map = {
        f"{r['y']:04d}-{r['m']:02d}": float(r["promo"] or 0) for r in promo_rows
    }

    months = []
    prev_gmv = None
    for r in sorted(gmv_rows, key=lambda x: (x["y"], x["m"])):
        ym = f"{r['y']:04d}-{r['m']:02d}"
        gmv = float(r["gmv"] or 0)
        orders = int(r["orders"] or 0)
        aov = gmv / orders if orders else 0.0
        mom = (gmv - prev_gmv) / abs(prev_gmv) if prev_gmv else None
        buyers = int(r["buyers"] or 0)
        new_buyers = new_map.get(ym, 0)
        cancels = cancel_map.get(ym, 0)
        ref = refund_map.get(ym, {})
        refund_amt = float(ref.get("refund_amt") or 0)
        refund_orders = int(ref.get("refund_orders") or 0)
        net_gmv = gmv - refund_amt
        promo = promo_map.get(ym, 0.0)
        months.append({
            "ym": ym,
            "gmv": gmv,
            "mom": mom,
            "orders": orders,
            "aov": aov,
            "buyers": buyers,
            "new_buyers": new_buyers,
            "repeat_rate": ((buyers - new_buyers) / buyers) if buyers else None,
            "cancel_orders": cancels,
            "cancel_rate": (
                (cancels / (orders + cancels)) if (orders + cancels) else None
            ),
            "refund_amt": refund_amt,
            "refund_rate_amt": (refund_amt / gmv) if gmv else None,
            "refund_orders": refund_orders,
            "refund_rate_cnt": (refund_orders / orders) if orders else None,
            "net_gmv": net_gmv,
            "promo": promo,
            "roi": (gmv / promo) if promo else None,
        })
        prev_gmv = gmv
    return {
        "months": months[-months_back:],
        "as_of": date.today(),
        "data_start": meta.get("data_start"),
        "buyer_null_share": (buyer_null / total_lines) if total_lines else None,
    }


#: 费用面板展示的月份数（最近 N 个月，含本月，同 MONTHLY_KPI_MONTHS）。
EXPENSE_PANEL_MONTHS = 6

#: fact_fin_store_expense 里未挂渠道行的展示名（mart_store_name IS NULL）。
UNMAPPED_CHANNEL_LABEL = "未挂渠道"

#: 账单费用拆解面板展示的费用项目数，其余合并为「其他」。
BILL_TOP_ITEMS = 10


def _recent_months(today, months_back):
    """返回最近 *months_back* 个月（含当月）的 "YYYY-MM" 列表，升序。"""
    months = []
    year, month = today.year, today.month
    for _ in range(months_back):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return sorted(months)


def fetch_expense_summary(connection, *, months_back=EXPENSE_PANEL_MONTHS,
                          today=None):
    """费用/盈利面板（P0）：渠道×月 财务费用 / 推广充值 / GMV / 费率。

    数据源与口径：

    * ``fact_fin_store_expense`` —— 费用按 (month, mart_channel, source)
      汇总；source=store_commission 是店铺费用（扣点等）、
      promotion_recharge 是推广费充值（充值 vs 余额口径待财务确认，
      暂按当月费用语义展示）；``mart_channel`` IS NULL 的行归
      「未挂渠道」桶（店铺挂接种子未命中；渠道已挂但店铺未细分的行
      ——mart_store_name IS NULL 而 mart_channel 非空——按其渠道正常
      展示，不进该桶）。
    * ``fact_channel_daily_sales`` —— GMV 按 (月, channel) 汇总；
      费率 = 费用合计 ÷ 当月 GMV（无 GMV 的渠道费率为 None，不臆造）。

    返回 ``{"months": [...], "rows": [...], "unmapped_stores": [...],
    "empty_months": [...], "as_of": date}``：months 为最近 *months_back*
    个月升序；rows 按费用总额降序、「未挂渠道」恒排最后；empty_months
    是窗口内财务尚未填报的月份（raw 费用表 2026-09 起未填，显式标注
    不静默）。窗口外月份（财务已填但超出展示窗）不参与 empty 判定。
    """
    today = today or date.today()
    months = _recent_months(today, months_back)
    month_set = set(months)

    expense_rows = _query(
        connection,
        "SELECT `month` AS `month`, `mart_channel` AS `channel`, "
        "`source` AS `source`, SUM(`expense_amount`) AS `amt` "
        "FROM `fact_fin_store_expense` "
        "GROUP BY `month`, `mart_channel`, `source`",
    )
    unmapped_rows = _query(
        connection,
        "SELECT DISTINCT `fin_store_name` AS `store`, `fin_platform` AS `platform` "
        "FROM `fact_fin_store_expense` WHERE `mart_channel` IS NULL",
    )
    gmv_rows = _query(
        connection,
        "SELECT YEAR(`business_date`) AS `y`, MONTH(`business_date`) AS `m`, "
        "`channel` AS `channel`, SUM(`sales_amount`) AS `gmv` "
        "FROM `fact_channel_daily_sales` "
        "WHERE `business_date` IS NOT NULL "
        "GROUP BY `y`, `m`, `channel`",
    )
    gmv_map = {
        (f"{r['y']:04d}-{r['m']:02d}", r["channel"]): float(r["gmv"] or 0)
        for r in gmv_rows
    }

    cells: dict[str, dict] = {}
    filled_months = set()
    for row in expense_rows:
        ym = row["month"]
        if ym not in month_set:
            continue
        filled_months.add(ym)
        channel = row["channel"] or UNMAPPED_CHANNEL_LABEL
        slot = cells.setdefault(channel, {}).setdefault(
            ym, {"commission": 0.0, "recharge": 0.0}
        )
        amount = float(row["amt"] or 0)
        if row["source"] == "promotion_recharge":
            slot["recharge"] += amount
        else:
            slot["commission"] += amount

    rows = []
    for channel, by_month in cells.items():
        enriched = {}
        for ym in months:
            cell = by_month.get(ym, {"commission": 0.0, "recharge": 0.0})
            total = cell["commission"] + cell["recharge"]
            gmv = gmv_map.get((ym, channel))
            enriched[ym] = {
                "commission": cell["commission"],
                "recharge": cell["recharge"],
                "total": total,
                "gmv": gmv,
                "rate": (total / gmv) if gmv else None,
            }
        rows.append({
            "channel": channel,
            "total": sum(c["total"] for c in enriched.values()),
            "cells": enriched,
        })
    rows.sort(key=lambda x: (x["channel"] == UNMAPPED_CHANNEL_LABEL, -x["total"]))

    return {
        "months": months,
        "rows": rows,
        "unmapped_stores": sorted(
            f"{r['store']}（{r['platform']}）"
            for r in unmapped_rows if r["store"]
        ),
        "empty_months": [ym for ym in months if ym not in filled_months],
        "as_of": today,
    }


def fetch_bill_breakdown(connection, *, months_back=EXPENSE_PANEL_MONTHS,
                         today=None):
    """账单费用拆解面板：平台账单（店铺扣点）按类别/平台/费用项目拆解。

    数据源固定 ``fact_fin_store_expense WHERE source = 'store_commission'``
    —— 即钉钉表「电商—店铺扣点费用管理」→ fin_store_commission 投影；
    ``promotion_recharge``（推广费充值）口径已由费用面板承载，**不进入
    本函数**（本函数是账单拆解）。

    依次发起 4 次查询：费用类别合计 / 平台合计 / 费用项目合计 /
    有数据的月份（DISTINCT month）。口径：

    * ``by_category`` / ``by_platform`` / ``top_items`` 一律 **按金额
      绝对值降序**（含冲销负数；绝对值相同按标签升序）；标签为 ``None``
      （fee_category / fin_platform / fee_item 为空）**恒排最后，不静默
      丢弃**。
    * ``share = total / grand_total``；``grand_total`` 为所有费用类别合计
      （= 平台合计，作占比分母），``grand_total == 0`` 时 ``share = None``
      （不臆造、不除零）。``share`` 保留 float 原值，渲染层再格式化。
    * ``top_items`` 取绝对值最大的 ``BILL_TOP_ITEMS`` 项，其余合并为
      ``other_items = {"count": 剩余项数, "total": 剩余金额之和}``；
      无剩余则 count=0、total=0.0。
    * ``empty_months`` 判定与 :func:`fetch_expense_summary` 一致：仅统计
      窗口内月份（第 4 次查询命中的月份为 filled，其余为 empty）；窗口外
      月份不参与。

    返回 ``{"months", "filled_months", "empty_months", "grand_total",
    "by_category", "by_platform", "top_items", "other_items", "as_of"}``。
    """
    today = today or date.today()
    months = _recent_months(today, months_back)
    month_set = set(months)

    category_rows = _query(
        connection,
        "SELECT `fee_category` AS `k`, SUM(`expense_amount`) AS `amt` "
        "FROM `fact_fin_store_expense` WHERE `source` = 'store_commission' "
        "GROUP BY `fee_category`",
    )
    platform_rows = _query(
        connection,
        "SELECT `fin_platform` AS `k`, SUM(`expense_amount`) AS `amt` "
        "FROM `fact_fin_store_expense` WHERE `source` = 'store_commission' "
        "GROUP BY `fin_platform`",
    )
    item_rows = _query(
        connection,
        "SELECT `fee_item` AS `k`, SUM(`expense_amount`) AS `amt` "
        "FROM `fact_fin_store_expense` WHERE `source` = 'store_commission' "
        "GROUP BY `fee_item`",
    )
    month_rows = _query(
        connection,
        "SELECT DISTINCT `month` FROM `fact_fin_store_expense` "
        "WHERE `source` = 'store_commission'",
    )

    def _sorted_pairs(rows):
        # 金额绝对值降序 → 标签升序 → None 标签恒排最后
        pairs = [(r["k"], float(r["amt"] or 0)) for r in rows]
        pairs.sort(
            key=lambda p: (
                p[0] is None,
                -abs(p[1]),
                "" if p[0] is None else str(p[0]),
            )
        )
        return pairs

    category_pairs = _sorted_pairs(category_rows)
    platform_pairs = _sorted_pairs(platform_rows)
    item_pairs = _sorted_pairs(item_rows)

    grand_total = sum(total for _, total in category_pairs)

    def _share(total):
        return (total / grand_total) if grand_total else None

    top_items = [
        {"item": item, "total": total, "share": _share(total)}
        for item, total in item_pairs[:BILL_TOP_ITEMS]
    ]
    rest = item_pairs[BILL_TOP_ITEMS:]
    other_items = {
        "count": len(rest),
        "total": sum(total for _, total in rest),
    }

    filled_months = sorted(
        r["month"] for r in month_rows if r["month"] in month_set
    )
    filled_set = set(filled_months)
    return {
        "months": months,
        "filled_months": filled_months,
        "empty_months": [ym for ym in months if ym not in filled_set],
        "grand_total": grand_total,
        "by_category": [
            {"category": k, "total": total} for k, total in category_pairs
        ],
        "by_platform": [
            {"platform": k, "total": total, "share": _share(total)}
            for k, total in platform_pairs
        ],
        "top_items": top_items,
        "other_items": other_items,
        "as_of": today,
    }


#: 商品结构面板展示的月份数（近 N 个月窗口，同 MONTHLY_KPI_MONTHS）。
PRODUCT_STRUCTURE_MONTHS = MONTHLY_KPI_MONTHS

#: 系列结构展示 Top N，其余合并为「其他」。
SERIES_TOP_N = 8

#: 价格带分档（元，按件均价 = paid_amount/quantity 落档，左闭右开）。
PRICE_BANDS = (
    ("0-199", 0.0, 200.0),
    ("200-399", 200.0, 400.0),
    ("400-799", 400.0, 800.0),
    ("800-1499", 800.0, 1500.0),
    ("1500+", 1500.0, None),
)

_UNCLASSIFIED_SERIES = "未分类"
_UNPRICED_BAND = "单价未知"


def _window_start(months):
    """``_recent_months`` 结果的首月 1 号（窗口下界）。"""
    year, month = int(months[0][:4]), int(months[0][5:7])
    return date(year, month, 1)


def fetch_product_structure(connection, *, months_back=PRODUCT_STRUCTURE_MONTHS,
                            today=None):
    """商品结构面板：系列结构 / 价格带分布 / Top10 单品集中度。

    数据源与口径：

    * ``fact_order_line`` —— 近 *months_back* 个月（含当月，trade_time 口径）
      按 spec_no 聚合 GMV 与件数；件均价 = paid_amount/quantity（实付口径，
      零件数行无单价、其 GMV 归「单价未知」档显式示出）。
    * ``dim_product`` —— spec_no 反查 series_name，NULL 归「未分类」
      （不静默丢弃）；系列取 Top *SERIES_TOP_N*、其余并入「其他」。
    * 集中度 —— Top10 单品 GMV 占比与 HHI（Σ(单品GMV占比²)，0-1，
      越高越集中）。

    返回 ``{"months": [...], "total_gmv": float, "series": [...],
    "price_bands": [...], "top_products": [...], "top10_share": float|None,
    "hhi": float|None, "as_of": date}``。
    """
    today = today or date.today()
    months = _recent_months(today, months_back)
    window = _window_start(months)

    spec_rows = _query(
        connection,
        "SELECT `spec_no` AS `spec_no`, MAX(`goods_name`) AS `goods_name`, "
        "SUM(`paid_amount`) AS `gmv`, SUM(`quantity`) AS `qty` "
        "FROM `fact_order_line` "
        "WHERE `trade_time` >= %s "
        "GROUP BY `spec_no`",
        (window,),
    )
    series_rows = _query(
        connection,
        "SELECT `spec_no` AS `spec_no`, `series_name` AS `series_name` "
        "FROM `dim_product`",
    )
    series_map = {r["spec_no"]: r["series_name"] for r in series_rows}

    specs = []
    for r in spec_rows:
        gmv = float(r["gmv"] or 0)
        qty = float(r["qty"] or 0)
        specs.append({
            "spec_no": r["spec_no"],
            "goods_name": r["goods_name"],
            "series_name": series_map.get(r["spec_no"]) or _UNCLASSIFIED_SERIES,
            "gmv": gmv,
            "unit_price": (gmv / qty) if qty else None,
        })
    total_gmv = sum(s["gmv"] for s in specs)

    # 系列结构：TopN + 其他
    by_series: dict[str, float] = {}
    for s in specs:
        by_series[s["series_name"]] = (
            by_series.get(s["series_name"], 0.0) + s["gmv"]
        )
    series_items = sorted(by_series.items(), key=lambda kv: -kv[1])
    if len(series_items) > SERIES_TOP_N:
        other = sum(v for _, v in series_items[SERIES_TOP_N:])
        series_items = series_items[:SERIES_TOP_N] + [("其他", other)]
    series_out = [
        {"series": name, "gmv": gmv,
         "share": (gmv / total_gmv) if total_gmv else None}
        for name, gmv in series_items
    ]

    # 价格带：件均价落档（零件数行无单价 → 显式档，不静默）
    bands = []
    for label, lo, hi in PRICE_BANDS:
        gmv_band = sum(
            s["gmv"] for s in specs
            if s["unit_price"] is not None and s["unit_price"] >= lo
            and (hi is None or s["unit_price"] < hi)
        )
        bands.append({"band": label, "gmv": gmv_band,
                      "share": (gmv_band / total_gmv) if total_gmv else None})
    unpriced = sum(s["gmv"] for s in specs if s["unit_price"] is None)
    if unpriced:
        bands.append({"band": _UNPRICED_BAND, "gmv": unpriced,
                      "share": (unpriced / total_gmv) if total_gmv else None})

    # 集中度：Top10 占比 + HHI
    ranked = sorted(specs, key=lambda s: -s["gmv"])
    top10_share = (
        sum(s["gmv"] for s in ranked[:10]) / total_gmv if total_gmv else None
    )
    hhi = (
        sum((s["gmv"] / total_gmv) ** 2 for s in specs) if total_gmv else None
    )
    return {
        "months": months,
        "total_gmv": total_gmv,
        "series": series_out,
        "price_bands": bands,
        "top_products": [
            {"spec_no": s["spec_no"], "goods_name": s["goods_name"],
             "series_name": s["series_name"], "gmv": s["gmv"],
             "share": (s["gmv"] / total_gmv) if total_gmv else None}
            for s in ranked[:10]
        ],
        "top10_share": top10_share,
        "hhi": hhi,
        "as_of": today,
    }


def fetch_refund_reason(connection, *, months_back=MONTHLY_KPI_MONTHS,
                        today=None):
    """退款原因面板：窗口内按 reason 聚合退款金额/单数与占比。

    数据源：``fact_refund_line``，按 check_time（入库审核时间）归月——
    与月度 KPI 的退款口径一致。reason 为 NULL/空白归「未填写」显示
    （不静默），按金额降序。
    """
    today = today or date.today()
    months = _recent_months(today, months_back)
    window = _window_start(months)
    rows = _query(
        connection,
        "SELECT COALESCE(NULLIF(TRIM(`reason`), ''), '未填写') AS `reason`, "
        "SUM(`actual_refund_amount`) AS `amt`, "
        "COUNT(DISTINCT `refund_no`) AS `orders` "
        "FROM `fact_refund_line` "
        "WHERE `check_time` >= %s "
        "GROUP BY `reason` ORDER BY `amt` DESC",
        (window,),
    )
    total_amt = sum(float(r["amt"] or 0) for r in rows)
    reasons = [
        {"reason": r["reason"], "amount": float(r["amt"] or 0),
         "orders": int(r["orders"] or 0),
         "share": (float(r["amt"] or 0) / total_amt) if total_amt else None}
        for r in rows
    ]
    return {
        "months": months,
        "total_amount": total_amt,
        "reasons": reasons,
        "as_of": today,
    }


#: GMV 三因子拆解展示窗口（近 N 个月，同 MONTHLY_KPI_MONTHS）。
FACTOR_MONTHS = MONTHLY_KPI_MONTHS


def fetch_gmv_factors(connection, *, months_back=FACTOR_MONTHS):
    """GMV 三因子拆解：买家数 × 人均件数 × 件均价（近 *months_back* 月）。

    口径（``fact_order_line``，trade_time 付款时间归月，剔除取消单）：

    * 买家数 = COUNT(DISTINCT buyer_id)（WDT 交易级 customer_id；拼多多按
      隐私协议不返回 buyer_id → NULL 不计入，缺失会使人均件数/件均价被
      高估，面板附缺失说明，不臆造补数）；
    * 件数 = Σ quantity；GMV = Σ paid_amount；
    * 人均件数 = 件数 ÷ 买家数；件均价 = GMV ÷ 件数；
    * 恒等式 GMV = 买家数 × 人均件数 × 件均价（代数恒等）。
    * 环比归因（连环替代法，三项加总恰等于 ΔGMV）：
      买家数贡献 = Δ买家数 × 上期人均件数 × 上期件均价；
      人均件数贡献 = 本期买家数 × Δ人均件数 × 上期件均价；
      件均价贡献 = 本期买家数 × 本期人均件数 × Δ件均价。

    返回 ``{"months": [...], "as_of": date}``，每行含 ym/gmv/buyers/qty/
    unit_price/qty_per_buyer/contrib（contrib 含 buyers/qty_per_buyer/
    unit_price/delta，首月为 None）。
    """
    rows = _query(
        connection,
        "SELECT YEAR(`trade_time`) AS `y`, MONTH(`trade_time`) AS `m`, "
        "SUM(`paid_amount`) AS `gmv`, SUM(`quantity`) AS `qty`, "
        "COUNT(DISTINCT `buyer_id`) AS `buyers` "
        "FROM `fact_order_line` "
        "WHERE `trade_time` IS NOT NULL "
        "GROUP BY `y`, `m` ORDER BY `y`, `m`",
    )
    months = []
    prev = None
    for r in sorted(rows, key=lambda x: (x["y"], x["m"])):
        ym = f"{r['y']:04d}-{r['m']:02d}"
        gmv = float(r["gmv"] or 0)
        qty = float(r["qty"] or 0)
        buyers = int(r["buyers"] or 0)
        unit_price = (gmv / qty) if qty else None
        qty_per_buyer = (qty / buyers) if buyers else None
        contrib = None
        if (
            prev is not None
            and prev["unit_price"] is not None and unit_price is not None
            and prev["qty_per_buyer"] is not None and qty_per_buyer is not None
        ):
            contrib = {
                "buyers": (buyers - prev["buyers"])
                * prev["qty_per_buyer"] * prev["unit_price"],
                "qty_per_buyer": buyers
                * (qty_per_buyer - prev["qty_per_buyer"]) * prev["unit_price"],
                "unit_price": buyers * qty_per_buyer
                * (unit_price - prev["unit_price"]),
                "delta": gmv - prev["gmv"],
            }
        months.append({
            "ym": ym,
            "gmv": gmv,
            "buyers": buyers,
            "qty": qty,
            "unit_price": unit_price,
            "qty_per_buyer": qty_per_buyer,
            "contrib": contrib,
        })
        prev = months[-1]
    return {"months": months[-months_back:], "as_of": date.today()}


#: Cohort 留存矩阵展示窗口（近 N 个月，同 MONTHLY_KPI_MONTHS）。
COHORT_MONTHS = MONTHLY_KPI_MONTHS


def _month_offset(later_ym, earlier_ym):
    """``later - earlier`` 的月差（同口径 "YYYY-MM" 字符串）。"""
    return (
        (int(later_ym[:4]) - int(earlier_ym[:4])) * 12
        + (int(later_ym[5:7]) - int(earlier_ym[5:7]))
    )


def fetch_cohort_retention(connection, *, months_back=COHORT_MONTHS, today=None):
    """Cohort 留存矩阵：按首购月分队列，追踪其后每月复购留存率。

    口径（``fact_order_line``，buyer_id 非空，trade_time 付款时间归月）：

    * 队列 = 买家首购月（该 buyer 的 MIN(trade_time) 所在月）；
    * 留存率(Mk) = 首购后第 k 月仍有成交的队列买家数 ÷ 队列规模，
      M0（首购月）恒为 100%；
    * 展示窗口取最近 *months_back* 个月内的首购月；列 M0..Mk 覆盖到
      数据最新月；avg_retention(Mk)=Σ(已满 k 月的各队列当月活跃买家)÷
      Σ(已满 k 月的队列规模)（未满 k 月的队列不计入该列均值，避免失真）。
    拼多多买家按隐私协议无 buyer_id（NULL 不计入队列，不臆造）。

    返回 ``{"cohorts": [...], "periods": [...], "avg_retention": {...},
    "last_ym": str, "as_of": date}``，cohorts 每行含 cohort/size/retention
    （retention 为 {offset: ratio}）。
    """
    today = today or date.today()
    months = _recent_months(today, months_back)
    month_set = set(months)
    rows = _query(
        connection,
        "SELECT `buyer_id` AS `buyer`, "
        "YEAR(`trade_time`) AS `y`, MONTH(`trade_time`) AS `m` "
        "FROM `fact_order_line` "
        "WHERE `buyer_id` IS NOT NULL AND `trade_time` IS NOT NULL "
        "GROUP BY `buyer_id`, `y`, `m`",
    )
    active: dict[str, set] = {}
    for r in rows:
        ym = f"{r['y']:04d}-{r['m']:02d}"
        active.setdefault(r["buyer"], set()).add(ym)

    cohorts: dict[str, list] = {}
    for yms in active.values():
        cohort = min(yms)
        if cohort in month_set:
            cohorts.setdefault(cohort, []).append(yms)
    last_ym = max((max(yms) for yms in active.values() if yms), default=None)

    cohorts_out = []
    max_offset = 0
    for cohort in sorted(cohorts):
        buyers = cohorts[cohort]
        size = len(buyers)
        counts: dict[str, int] = {}
        for yms in buyers:
            for ym in yms:
                offset = _month_offset(ym, cohort)
                if offset < 0:
                    continue
                counts[offset] = counts.get(offset, 0) + 1
                max_offset = max(max_offset, offset)
        cohorts_out.append({
            "cohort": cohort,
            "size": size,
            "retention": {off: cnt / size for off, cnt in counts.items()},
        })
    periods = list(range(0, max_offset + 1))

    avg_retention = {}
    if last_ym:
        for k in periods:
            denom = 0
            numer = 0
            for row in cohorts_out:
                if _month_offset(last_ym, row["cohort"]) < k:
                    continue  # 该队列尚未满 k 月，不计入
                denom += row["size"]
                numer += row["retention"].get(k, 0) * row["size"]
            avg_retention[k] = (numer / denom) if denom else None
    return {
        "cohorts": cohorts_out,
        "periods": periods,
        "avg_retention": avg_retention,
        "last_ym": last_ym,
        "as_of": today,
    }


__all__ = [
    "BILL_TOP_ITEMS",
    "COHORT_MONTHS",
    "DANGER_MONTH_SALES",
    "EXPENSE_PANEL_MONTHS",
    "EXCLUDE_KEYWORDS",
    "FACTOR_MONTHS",
    "HOT_SALES_DAYS",
    "HOT_TOP_N",
    "MONTHLY_KPI_MONTHS",
    "NODE_MONTHS",
    "PRICE_BANDS",
    "PRODUCT_STRUCTURE_MONTHS",
    "PURCHASE_LOOKBACK_HOURS",
    "PURCHASE_STATUS_DONE",
    "RISK_THRESHOLD",
    "RISK_TRADE_STATUSES",
    "SERIES_TOP_N",
    "UNMAPPED_CHANNEL_LABEL",
    "WINE_KEYWORDS",
    "fetch_bill_breakdown",
    "fetch_cohort_retention",
    "fetch_expense_summary",
    "fetch_gmv_factors",
    "fetch_hot_items",
    "fetch_inventory_snapshot",
    "fetch_monthly_kpi",
    "fetch_order_risk",
    "fetch_product_structure",
    "fetch_purchase_inbound",
    "fetch_refund_reason",
    "fetch_stock_alerts",
    "is_wine",
    "platform_of",
]
