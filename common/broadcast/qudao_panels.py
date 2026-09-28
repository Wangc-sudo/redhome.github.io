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
# 装配
# ---------------------------------------------------------------------------

def build_qudao_panels(connection, *, business_date, now, workdays, monthly_targets):
    """生成 qudao 榜单页的全部渠道板块（顺序即页面顺序）。

    单板块异常 → "暂缺"占位（绝不拖垮整页生成），错误进日志。
    """
    builders = (
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
    "build_hot_items_panel",
    "build_order_risk_panel",
    "build_purchase_inbound_panel",
    "build_qudao_panels",
    "build_stock_alert_panel",
]
