# -*- coding: utf-8 -*-
"""qudao 榜单页的渠道播报板块（HTML 渲染 + 装配）。

五个板块对应旧渠道群日报类播报（2026-09-23 并入页面、停单独播报）：

1. 渠道日销快报（原 11:00 渠道日报 / 08:30 channel-daily 的渠道段）
2. 热卖品监控（原 09:00）
3. 库存补货提醒（原 17:00）
4. 采购入库提醒（原 10:00）
5. 订单风险防控（原 15:00，昨日口径）

数据全部经 :mod:`common.broadcast.queries` / :mod:`common.daily_robot.channel_daily`
读 mart_ops（设计稿 §6.3 读取契约）。单板块失败不拖垮整页：降级为
"暂缺"占位（设计稿 §7 降级纪律），错误详情只进日志不进页面。
"""

from __future__ import annotations

import html as html_mod
import logging
from datetime import timedelta

logger = logging.getLogger(__name__)


def _fmt_wan(v):
    v = float(v)
    if abs(v) >= 10000:
        return f"{v / 10000:.1f}万"
    return f"{v:,.0f}"


def _fmt_qty(v):
    v = float(v)
    return f"{v:,.0f}" if v == int(v) else f"{v:,.1f}"


def _esc(value):
    return html_mod.escape(str(value if value is not None else ""))


def _panel(title, body, note=None):
    note_html = (
        f'<div class="small muted" style="margin-top:8px">{note}</div>' if note else ""
    )
    return f'<div class="panel">\n  <h2>{title}</h2>\n  {body}\n{note_html}\n</div>'


def _placeholder_panel(title, reason="数据暂缺"):
    return _panel(title, f'<div class="muted small">{_esc(reason)}</div>')


def _stale_note(as_of, stale):
    if as_of is None:
        return ""
    tag = ' <span class="badge amber">延迟</span>' if stale else ""
    return f"数据截至 {_esc(as_of)}{tag}"


# ---------------------------------------------------------------------------
# 1. 渠道日销快报
# ---------------------------------------------------------------------------

def build_channel_daily_panel(connection, *, business_date, workdays, monthly_targets):
    from common.daily_robot.channel_daily import (
        collect_channel_rows,
        fetch_channel_month_facts,
        resolve_channel_business_date,
    )

    month_facts = fetch_channel_month_facts(
        connection, year=business_date.year, month=business_date.month
    )
    channel_date = resolve_channel_business_date(month_facts, business_date)
    if channel_date is None:
        return _placeholder_panel("🛒 渠道日销快报", "本月暂无非零渠道日销数据")
    # T-1 水位（2026-09-28）：锚点 = business_date-1（10:31 采集窗口的
    # 目标日）；回退到更早说明昨日全渠道无有效数据，页面显著标注。
    expected = business_date - timedelta(days=1)
    stale = channel_date < expected
    total, rows = collect_channel_rows(
        month_facts=month_facts,
        business_date=channel_date,
        workdays=workdays,
        monthly_targets=monthly_targets,
    )
    trs = []
    for row in rows:
        mom = row["mom"]
        mom_cls = "g" if (mom is not None and mom >= 0) else ("r" if mom is not None else "muted")
        trs.append(
            f'<tr><td class="strong">{_esc(row["channel"])}</td>'
            f'<td class="num">{_fmt_wan(row["sales"])}</td>'
            f'<td class="num {mom_cls}">{_esc(row["mom_txt"])}</td>'
            f'<td class="num muted">{_esc(row["target_txt"])}</td>'
            f'<td class="num">{_esc(row["rate_txt"])}</td></tr>'
        )
    stale_banner = (
        f'<div class="small" style="margin-bottom:8px;color:#b26a00">'
        f'⚠️ 昨日数据未到齐，当前显示 '
        f'{channel_date.month} 月 {channel_date.day} 日</div>'
        if stale else ""
    )
    body = (
        f'{stale_banner}'
        f'<div class="small muted" style="margin-bottom:8px">'
        f'全渠道{channel_date.month}月{channel_date.day}日销售额 '
        f'<b style="color:#1f2329">{_fmt_wan(total)} 元</b></div>'
        f'<table><thead><tr><th>渠道</th><th>销售额</th><th>环比</th>'
        f'<th>月目标</th><th>达成率</th></tr></thead>'
        f'<tbody>{"".join(trs)}</tbody></table>'
    )
    return _panel(
        "🛒 渠道日销快报", body,
        note=(
            f"数据截至 {channel_date.month}月{channel_date.day}日"
            f"（T+1 10:31 采集 · 店铺后台导出 · DB）"
        ),
    )


# ---------------------------------------------------------------------------
# 2. 热卖品监控
# ---------------------------------------------------------------------------

def build_hot_items_panel(connection, *, today):
    from common.broadcast.queries import fetch_hot_items

    data = fetch_hot_items(connection, today=today)
    if not data["items"]:
        return _placeholder_panel("🔥 热卖品监控", "暂无库存快照数据")

    def state_badge(item):
        if item["stock_state"] == "OVERSOLD":
            return '<span class="badge red">‼️断货</span>'
        if item["stock_state"] == "URGENT":
            return '<span class="badge amber">⚠️偏低</span>'
        return '<span class="badge green">正常</span>'

    trs = []
    for item in data["items"]:
        days = item["days_left"]
        if days is None:
            days_html = '<span class="muted">—</span>'
        elif days <= 7:
            days_html = f'<b>{days:.0f}天</b>'
        else:
            days_html = f'{days:.0f}天'
        trs.append(
            f'<tr><td class="strong">{_esc(item["goods_name"])}</td>'
            f'<td class="muted">{_esc(item["spec_no"])}</td>'
            f'<td class="num">{_fmt_qty(item["available_qty"])}</td>'
            f'<td class="num">{days_html}</td>'
            f'<td class="num">{_fmt_qty(item["qty_30d"])}</td>'
            f'<td class="num muted">{_fmt_qty(item["purchase_intransit_qty"])}</td>'
            f'<td>{state_badge(item)}</td></tr>'
        )
    coverage = (
        f'{data["coverage_pct"] * 100:.0f}%'
        if data["coverage_pct"] is not None else "—"
    )
    summary = (
        f'监控 {data["watch_count"]} 个SKU（Top10 + 危险品保底，覆盖近30天销量 {coverage}）'
        f' · ‼️断货{data["oversold_count"]}个 ⚠️偏低{data["danger_count"]}个'
    )
    body = (
        f'<div class="small muted" style="margin-bottom:8px">{summary}</div>'
        f'<table><thead><tr><th>货品名称</th><th>编码</th><th>库存</th>'
        f'<th>可售</th><th>30天销</th><th>在途</th><th>状态</th></tr></thead>'
        f'<tbody>{"".join(trs)}</tbody></table>'
    )
    # 两窗口并列防误读（2026-09-23 核查：习酒761 "30天销1260 但正常/可售—"）
    window = data.get("moving_window_days") or 15
    note = (
        f"{_stale_note(data['as_of'], data['stale'])}"
        f" · 可售/状态按近{window}天动销（「—」= 近{window}天无动销），30天销为近30天窗口"
    )
    return _panel("🔥 热卖品监控", body, note=note)


# ---------------------------------------------------------------------------
# 3. 库存补货提醒
# ---------------------------------------------------------------------------

def build_stock_alert_panel(connection, *, today):
    from common.broadcast.queries import fetch_stock_alerts

    data = fetch_stock_alerts(connection, today=today)
    if data["as_of"] is None:
        return _placeholder_panel("📦 库存补货提醒", "暂无库存快照数据")

    sections = []
    if data["urgent"]:
        trs = "".join(
            f'<tr><td class="strong">{_esc(r["goods_name"])}</td>'
            f'<td class="muted">{_esc(r["spec_no"])}</td>'
            f'<td class="num">{_fmt_qty(r["available_qty"])}</td>'
            f'<td class="num">{float(r["daily_avg"] or 0):.1f}</td>'
            f'<td class="num"><b>{float(r["days_left"] or 0):.0f}天</b></td></tr>'
            for r in data["urgent"]
        )
        sections.append(
            f'<div class="small" style="margin:6px 0 4px">'
            f'⚠️ 预计可售≤7天（{len(data["urgent"])}个）</div>'
            f'<table><thead><tr><th>货品名称</th><th>编码</th><th>库存</th>'
            f'<th>日销</th><th>可售天数</th></tr></thead><tbody>{trs}</tbody></table>'
        )
    if data["oversold"]:
        trs = "".join(
            f'<tr><td class="strong">{_esc(r["goods_name"])}</td>'
            f'<td class="muted">{_esc(r["spec_no"])}</td>'
            f'<td class="num warn">{_fmt_qty(max(0.0, -float(r["available_qty"] or 0)))}</td></tr>'
            for r in data["oversold"]
        )
        sections.append(
            f'<div class="small" style="margin:10px 0 4px">'
            f'‼️ 已超卖（{len(data["oversold"])}个）</div>'
            f'<table><thead><tr><th>货品名称</th><th>编码</th>'
            f'<th>超卖数量</th></tr></thead><tbody>{trs}</tbody></table>'
        )
    # 零库存在售单独成组（库存恰好为 0 但仍在卖，2026-09-23 核查拆分）
    zero_stock = data.get("zero_stock") or []
    if zero_stock:
        trs = "".join(
            f'<tr><td class="strong">{_esc(r["goods_name"])}</td>'
            f'<td class="muted">{_esc(r["spec_no"])}</td>'
            f'<td class="num">{float(r["daily_avg"] or 0):.1f}</td></tr>'
            for r in zero_stock
        )
        sections.append(
            f'<div class="small" style="margin:10px 0 4px">'
            f'🚫 零库存在售（{len(zero_stock)}个，按日销降序）</div>'
            f'<table><thead><tr><th>货品名称</th><th>编码</th>'
            f'<th>日销</th></tr></thead><tbody>{trs}</tbody></table>'
        )
    if not sections:
        sections.append('<div class="ok small">当前无紧急补货与超卖 SKU</div>')
    window = data["moving_window_days"] or 15
    note = (
        f"动销窗口 近{window}天 · 已剔除赠品/服务卡等非商品行 · "
        f"{_stale_note(data['as_of'], data['stale'])}"
    )
    return _panel("📦 库存补货提醒", "".join(sections), note=note)


# ---------------------------------------------------------------------------
# 4. 采购入库提醒
# ---------------------------------------------------------------------------

def build_purchase_inbound_panel(connection, *, now):
    from common.broadcast.queries import fetch_purchase_inbound

    data = fetch_purchase_inbound(connection, now=now)
    if not data["groups"]:
        return _panel(
            "🏭 采购入库（近24小时）",
            '<div class="muted small">近24小时无已完成入库</div>',
            note=f"数据截至 {_esc(now.strftime('%m-%d %H:%M'))}",
        )
    sections = []
    for group in data["groups"]:
        lis = "".join(
            f'<tr><td class="muted">{_esc(s["spec_no"])}</td>'
            f'<td class="strong">{_esc(s["goods_name"])}</td>'
            f'<td class="num"><b>×{_fmt_qty(s["qty"])}</b></td></tr>'
            for s in group["specs"]
        )
        sections.append(
            f'<div class="small" style="margin:6px 0 4px">'
            f'🏭 <b>{_esc(group["warehouse_name"])}</b>'
            f'（{group["order_count"]}张单）</div>'
            f'<table><thead><tr><th>编码</th><th>货品名称</th>'
            f'<th>数量</th></tr></thead><tbody>{lis}</tbody></table>'
        )
    note = (
        f'共 {data["total_lines"]} 条明细（{data["total_specs"]} 个SKU），'
        f'{data["total_orders"]} 张采购单，合计 {_fmt_qty(data["total_qty"])} 件'
        f' · 数据截至 {_esc(now.strftime("%m-%d %H:%M"))}'
    )
    return _panel("🏭 采购入库（近24小时）", "".join(sections), note=note)


# ---------------------------------------------------------------------------
# 5. 订单风险防控
# ---------------------------------------------------------------------------

def build_order_risk_panel(connection, *, business_date):
    from common.broadcast.queries import fetch_order_risk

    risk_date = business_date - timedelta(days=1)
    data = fetch_order_risk(connection, business_date=risk_date)
    head = (
        f'<div class="small muted" style="margin-bottom:8px">'
        f'同店铺+同地区同日 ≥{data["threshold"]} 单判定风险'
        + (
            f' · ⚠️ 拼多多地区字段受隐私协议限制（{data["pdd_no_area_count"]}单未计入）'
            if data["pdd_no_area_count"] else ""
        )
        + "</div>"
    )
    def _group_html(group):
        areas = group["areas"][:15]
        trs = "".join(
            f'<tr><td>{_esc(a["area"])}</td>'
            f'<td class="num warn">{a["count"]}</td></tr>'
            for a in areas
        )
        more = (
            f'<div class="small muted">… 共{len(group["areas"])}个地区</div>'
            if len(group["areas"]) > 15 else ""
        )
        # platform_of 未识别时兜底返回店名本身，避免"店名-店名"重复显示
        title = (
            group["shop_name"]
            if group["platform"] == group["shop_name"]
            else f'{group["platform"]}-{group["shop_name"]}'
        )
        return (
            f'<div class="small" style="margin:6px 0 4px">'
            f'<b>{_esc(title)}</b></div>'
            f'<table><thead><tr><th>重点地区</th><th>异常单数</th></tr></thead>'
            f'<tbody>{trs}</tbody></table>{more}'
        )

    incomplete = data.get("incomplete_groups") or []
    sections = []
    if data["groups"]:
        sections.extend(_group_html(g) for g in data["groups"])
    else:
        sections.append('<div class="ok small">昨日未发现同店同地区集中下单</div>')
    if incomplete:
        sections.append(
            '<div class="small muted" style="margin:10px 0 4px">'
            '⚠️ 以下店铺地区字段不全（区级缺失），不参与风险判定，仅供参考</div>'
        )
        sections.extend(_group_html(g) for g in incomplete)
    body = head + "".join(sections)
    return _panel(
        f"🚨 订单风险防控（{risk_date.month}月{risk_date.day}日）", body,
        note="口径：按付款时间，排除已取消订单；同地区定义为省市区三级归一",
    )


# ---------------------------------------------------------------------------
# 0. 月度核心指标看板（P0）——白酒电商分析框架的月度总表
# ---------------------------------------------------------------------------

def build_monthly_kpi_panel(connection, *, today):
    """月度核心指标看板：GMV/订单数/客单价/买家资产/取消/退款/净额/推广/ROI。

    对齐「白酒电商数据分析」P0 模块：按月一行，月份连续。同比因数据源
    限制显式标注缺口，不臆造；残月（数据起点所在月）标注环比失真。
    """
    from common.broadcast.queries import fetch_monthly_kpi

    data = fetch_monthly_kpi(connection)
    months = data["months"]
    if not months:
        return _placeholder_panel("📊 月度核心指标看板", "暂无订单数据（fact_order_line 为空）")
    data_start = data.get("data_start")
    start_ym = (
        f"{data_start.year:04d}-{data_start.month:02d}" if data_start else None
    )

    def _mom_html(v):
        if v is None:
            return '<span class="muted">NA</span>'
        cls = "g" if v >= 0 else "r"
        sign = "+" if v >= 0 else ""
        return f'<span class="{cls}">{sign}{v * 100:.1f}%</span>'

    def _pct(v):
        return f"{v * 100:.1f}%" if v is not None else '<span class="muted">—</span>'

    def _int(v):
        return f"{v:,}" if v else '<span class="muted">—</span>'

    trs = []
    for m in months:
        roi_txt = f"{m['roi']:.2f}" if m["roi"] else '<span class="muted">—</span>'
        promo_txt = _fmt_wan(m["promo"]) if m["promo"] else '<span class="muted">—</span>'
        ym_label = _esc(m["ym"])
        if data_start and m["ym"] == start_ym:
            ym_label += (
                f'<div class="muted small">残月({data_start.month}月'
                f'{data_start.day}日起)</div>'
            )
        trs.append(
            f'<tr>'
            f'<td class="strong">{ym_label}</td>'
            f'<td class="num">{_fmt_wan(m["gmv"])}</td>'
            f'<td class="num">{_mom_html(m["mom"])}</td>'
            f'<td class="num">{m["orders"]:,}</td>'
            f'<td class="num">¥{m["aov"]:,.0f}</td>'
            f'<td class="num">{_int(m["buyers"])}</td>'
            f'<td class="num">{m["new_buyers"]:,}</td>'
            f'<td class="num">{_pct(m["repeat_rate"])}</td>'
            f'<td class="num">{m["cancel_orders"]:,}</td>'
            f'<td class="num">{_pct(m["cancel_rate"])}</td>'
            f'<td class="num">{_fmt_wan(m["refund_amt"])}</td>'
            f'<td class="num">{_pct(m["refund_rate_amt"])}</td>'
            f'<td class="num">{_fmt_wan(m["net_gmv"])}</td>'
            f'<td class="num">{promo_txt}</td>'
            f'<td class="num">{roi_txt}</td>'
            f'</tr>'
        )
    notes = ['⚠️ 缺失指标：同比(无去年同期数据，WDT 为全量重放)']
    if data.get("data_start") and start_ym in {m["ym"] for m in months}:
        notes.append(
            f'残月：数据自 {data_start:%Y-%m-%d} 起，{start_ym} 环比失真'
        )
    if data.get("buyer_null_share"):
        notes.append(
            f'买家ID缺失行占比 {data["buyer_null_share"] * 100:.0f}%'
            '（拼多多按隐私协议不返回买家，未计入买家资产）'
        )
    body = (
        f'<table><thead><tr>'
        f'<th>月份</th><th>GMV(剔除取消)</th><th>环比</th>'
        f'<th>订单数</th><th>客单价</th>'
        f'<th>活跃买家</th><th>新客</th><th>复购率</th>'
        f'<th>取消单</th><th>取消率</th>'
        f'<th>退款金额</th><th>退款率</th>'
        f'<th>净额(扣退款)</th><th>推广花费</th><th>ROI</th>'
        f'</tr></thead><tbody>{"".join(trs)}</tbody></table>'
        f'<div class="small muted" style="margin-top:6px">'
        f'{" · ".join(notes)}'
        f'</div>'
    )
    note = (
        "口径：GMV=已付款未取消(trade_status∉4/5/24) Σ paid_amount 按付款时间归月 · "
        "活跃买家=buyer_id 去重、新客=首购当月、复购率=(活跃−新客)÷活跃 · "
        "取消率=取消单÷(付款订单+取消单)，按下单时间归月 · "
        "退款=actual_refund_amount 按入库审核时间归月；净额=GMV−退款 · "
        "推广=promotion_cost"
    )
    return _panel("📊 月度核心指标看板（P0）", body, note=note)


# ---------------------------------------------------------------------------
# 0.1 费用/盈利面板（P0）——渠道×月 费用与费率（材料一模式）
# ---------------------------------------------------------------------------

def build_expense_panel(connection, *, today):
    """费用/盈利面板：渠道×月 财务费用（扣点等）、推广充值与费率。

    数据 = fact_fin_store_expense（店铺挂接种子映射渠道）JOIN
    fact_channel_daily_sales（GMV），费率=费用合计÷当月 GMV。
    「未挂渠道」行与「财务未填月份」显式标注，不臆造、不静默。
    """
    from common.broadcast.queries import UNMAPPED_CHANNEL_LABEL, fetch_expense_summary

    data = fetch_expense_summary(connection, today=today)
    if not data["rows"]:
        return _placeholder_panel(
            "💰 费用/盈利面板", "暂无费用数据（fact_fin_store_expense 为空）"
        )

    def _cell(c):
        if not c["total"] and c["gmv"] is None:
            return '<span class="muted">—</span>'
        second = (
            f'{c["rate"] * 100:.1f}%' if c["rate"] is not None else "—"
        )
        if c["recharge"]:
            second += f' · 充{_fmt_wan(c["recharge"])}'
        return f'{_fmt_wan(c["total"])}<div class="muted small">{second}</div>'

    ths = "".join(f"<th>{_esc(ym)}</th>" for ym in data["months"])
    trs = []
    for row in data["rows"]:
        tds = "".join(
            f'<td class="num">{_cell(row["cells"][ym])}</td>'
            for ym in data["months"]
        )
        trs.append(
            f'<tr><td class="strong">{_esc(row["channel"])}</td>{tds}'
            f'<td class="num"><b>{_fmt_wan(row["total"])}</b></td></tr>'
        )
    body = (
        f'<table><thead><tr><th>渠道</th>{ths}<th>近{len(data["months"])}月合计</th>'
        f'</tr></thead><tbody>{"".join(trs)}</tbody></table>'
    )
    notes = []
    if data["empty_months"]:
        notes.append(
            f'⚠️ 财务未填：{"、".join(data["empty_months"])}'
        )
    if data["unmapped_stores"]:
        shown = "、".join(data["unmapped_stores"][:6])
        more = f" 等{len(data['unmapped_stores'])}店" if len(data["unmapped_stores"]) > 6 else ""
        notes.append(
            f'未挂渠道店铺（历史关店/渠道外新店/待归，不计入在营渠道费率）：'
            f'{shown}{more}（分类证据见 seeds/store_channel_mapping.json，改后重跑 extract 生效）'
        )
    notes.append(
        "口径：店铺费用=fin_store_commission（应缴/实缴待财务确认）· "
        "推广=fin_ecommerce_promotion_recharge_balance（充值/余额口径待确认）· "
        f"费率=费用合计÷当月GMV（{UNMAPPED_CHANNEL_LABEL}=挂接种子未命中的财务店铺）"
    )
    return _panel(
        "💰 费用/盈利面板", body,
        note=" · ".join(notes),
    )


# ---------------------------------------------------------------------------
# 0.2 商品结构面板——系列/价格带/集中度（材料二 P1 模块）
# ---------------------------------------------------------------------------

def build_product_structure_panel(connection, *, today):
    """商品结构面板：系列结构 / 价格带分布 / Top10 单品集中度（近6月）。

    系列与价格带各一张小表，Top10 附集中度汇总；NULL 系列归「未分类」、
    零件数行 GMV 归「单价未知」档，均显式示出不静默。
    """
    from common.broadcast.queries import SERIES_TOP_N, fetch_product_structure

    data = fetch_product_structure(connection, today=today)
    if not data["total_gmv"]:
        return _placeholder_panel(
            "🧱 商品结构分析", "暂无订单数据（窗口内 GMV=0）"
        )

    def _share(v):
        return f"{v * 100:.1f}%" if v is not None else '<span class="muted">—</span>'

    series_trs = "".join(
        f'<tr><td class="strong">{_esc(s["series"])}</td>'
        f'<td class="num">{_fmt_wan(s["gmv"])}</td>'
        f'<td class="num">{_share(s["share"])}</td></tr>'
        for s in data["series"]
    )
    band_trs = "".join(
        f'<tr><td class="strong">{_esc(b["band"])}元</td>'
        f'<td class="num">{_fmt_wan(b["gmv"])}</td>'
        f'<td class="num">{_share(b["share"])}</td></tr>'
        for b in data["price_bands"]
    )
    top_trs = "".join(
        f'<tr><td>{_esc(p["goods_name"] or p["spec_no"])}</td>'
        f'<td class="small muted">{_esc(p["series_name"])}</td>'
        f'<td class="num">{_fmt_wan(p["gmv"])}</td>'
        f'<td class="num">{_share(p["share"])}</td></tr>'
        for p in data["top_products"]
    )
    head = (
        f'<div class="small muted" style="margin:0 0 4px">'
        f'近{len(data["months"])}月 GMV 合计 {_fmt_wan(data["total_gmv"])} · '
        f'Top10 占比 {_share(data["top10_share"])} · '
        f'HHI {_share(data["hhi"])}（Σ单品占比²，越高越集中）</div>'
    )
    body = (
        head
        + f'<div class="small strong" style="margin:6px 0 2px">'
          f'系列结构（Top{SERIES_TOP_N}，其余计「其他」）</div>'
        + f'<table><thead><tr><th>系列</th><th>GMV</th><th>占比</th>'
          f'</tr></thead><tbody>{series_trs}</tbody></table>'
        + f'<div class="small strong" style="margin:10px 0 2px">'
          f'价格带分布（件均价口径）</div>'
        + f'<table><thead><tr><th>价格带</th><th>GMV</th><th>占比</th>'
          f'</tr></thead><tbody>{band_trs}</tbody></table>'
        + f'<div class="small strong" style="margin:10px 0 2px">Top10 单品</div>'
        + f'<table><thead><tr><th>商品</th><th>系列</th><th>GMV</th><th>占比</th>'
          f'</tr></thead><tbody>{top_trs}</tbody></table>'
    )
    return _panel(
        "🧱 商品结构分析", body,
        note=(
            "口径：近6月 fact_order_line 按付款时间（trade_time），系列=dim_product.series_name"
            "（NULL→未分类）；价格带按件均价=paid_amount÷quantity 实付口径分档；"
            "集中度=Top10 单品 GMV 占比 / HHI"
        ),
    )


# ---------------------------------------------------------------------------
# 0.3 退款原因面板——损耗归因（材料二行业提示）
# ---------------------------------------------------------------------------

def build_refund_reason_panel(connection, *, today):
    """退款原因面板：近6月按 reason 聚合退款金额/单数与占比。

    与月度 KPI 的退款同口径（fact_refund_line.actual_refund_amount 按
    check_time 归月）；reason 空白归「未填写」显示，不静默。
    """
    from common.broadcast.queries import fetch_refund_reason

    data = fetch_refund_reason(connection, today=today)
    if not data["reasons"]:
        return _placeholder_panel(
            "↩️ 退款原因分析", "暂无退款数据（fact_refund_line 为空）"
        )

    def _share(v):
        return f"{v * 100:.1f}%" if v is not None else '<span class="muted">—</span>'

    trs = "".join(
        f'<tr><td class="strong">{_esc(r["reason"])}</td>'
        f'<td class="num">{_fmt_wan(r["amount"])}</td>'
        f'<td class="num">{r["orders"]:,}</td>'
        f'<td class="num">{_share(r["share"])}</td></tr>'
        for r in data["reasons"]
    )
    body = (
        f'<div class="small muted" style="margin:0 0 4px">'
        f'近{len(data["months"])}月退款合计 {_fmt_wan(data["total_amount"])}'
        f'</div>'
        f'<table><thead><tr><th>原因</th><th>退款金额</th><th>退款单数</th>'
        f'<th>占比</th></tr></thead><tbody>{trs}</tbody></table>'
    )
    return _panel(
        "↩️ 退款原因分析", body,
        note=(
            "口径：fact_refund_line.actual_refund_amount 按入库审核时间(check_time)归月，"
            "与月度看板退款同口径；「未填写」为原因空白的退款单"
        ),
    )


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------

def build_qudao_panels(connection, *, business_date, now, workdays, monthly_targets):
    """生成 qudao 榜单页的全部渠道板块（顺序即页面顺序）。

    单板块异常 → "暂缺"占位（绝不拖垮整页生成），错误进日志。
    """
    builders = (
        ("月度核心指标看板", lambda: build_monthly_kpi_panel(connection, today=business_date)),
        ("费用/盈利面板", lambda: build_expense_panel(connection, today=business_date)),
        ("商品结构分析", lambda: build_product_structure_panel(connection, today=business_date)),
        ("退款原因分析", lambda: build_refund_reason_panel(connection, today=business_date)),
        ("渠道日销快报", lambda: build_channel_daily_panel(
            connection,
            business_date=business_date,
            workdays=workdays,
            monthly_targets=monthly_targets,
        )),
        ("热卖品监控", lambda: build_hot_items_panel(connection, today=business_date)),
        ("库存补货提醒", lambda: build_stock_alert_panel(connection, today=business_date)),
        ("采购入库提醒", lambda: build_purchase_inbound_panel(connection, now=now)),
        ("订单风险防控", lambda: build_order_risk_panel(connection, business_date=business_date)),
    )
    panels = []
    for name, build in builders:
        try:
            panels.append(build())
        except Exception:
            logger.warning("qudao 板块 %s 生成失败，降级为占位", name, exc_info=True)
            panels.append(_placeholder_panel(name))
    return panels


__all__ = [
    "build_channel_daily_panel",
    "build_expense_panel",
    "build_hot_items_panel",
    "build_monthly_kpi_panel",
    "build_order_risk_panel",
    "build_product_structure_panel",
    "build_purchase_inbound_panel",
    "build_qudao_panels",
    "build_refund_reason_panel",
    "build_stock_alert_panel",
]
