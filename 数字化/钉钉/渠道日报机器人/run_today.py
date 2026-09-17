#!/usr/bin/env python3
"""
渠道日报表群机器人 — 真实表结构版（独立脚本，不影响 main.py 原字段方案）
======================================================================
已探测确认的表结构（2026-09-03，Base「9月电商渠道日报表」）：
- 渠道明细表 x7：「天猫渠道数据9」「京东渠道数据9」「拼多多渠道数据9」
  「猫超渠道数据9」「即时零售渠道数据9」「直播渠道数据9」「私域渠道数据9」
  字段：店铺(text) / 负责人(user[]) / 销售额(number) / 推广费(number) / ROI(number) / 日期(date毫秒)
- 月目标表：「渠道销售目标达成率9」
  字段：店铺(text) / 负责人(user[]) / 月目标(number) / 渠道(text)

用法：
  python run_today.py --dry             # 只生成不推送
  python run_today.py --date 2026-09-02
"""
import argparse
import calendar
import json
import re
import sys
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows 控制台默认 GBK，打印 emoji 图表字符会 UnicodeEncodeError；
# Linux（ECS）下 stdout 已是 UTF-8，此调用无副作用。reconfigure 后失败的
# 字符走 replace，避免调试输出反过来影响播报主流程。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# 同目录模块（bi_chart）：daily_scheduler 的子进程 cwd 可能是仓库根，
# 仅靠 sys.path[0]（脚本目录）在 -m 调用下并不可靠，显式补齐。
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from common.dingtalk import DingTalkClient, send_markdown, resolve_target

from bi_chart import (
    bar as chart_bar,
    gauge as chart_gauge,
    pct as fmt_pct,
    pct_change as fmt_change,
    sparkline as chart_spark,
    wan as fmt_wan,
)

TZ_CN = timezone(timedelta(hours=8))  # 钉钉 date 字段为 CST 午夜毫秒时间戳

# ---------- 配置 ----------
BASE_ID = "QOG9lyrgJPmxNQlwIw4QDd6q8zN67Mw4"
CHANNELS = ["天猫", "京东", "拼多多", "猫超", "即时零售", "直播", "私域"]
MONTH = 9  # 跨月时改这里（或改为按日期自动算）
TREND_DAYS = 7  # 日报里的走势迷你图取最近几天
BI_PAGE_DIR = BASE_DIR / "channel-bi-page"  # 静态 BI 看板输出目录


def http_json(url, method="GET", body=None, headers=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json"}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


class DT:
    def __init__(self, app_key, app_secret, operator_id):
        self.app_key = app_key
        self.app_secret = app_secret
        self.operator_id = operator_id
        self._token = None

    def token(self):
        if not self._token:
            r = http_json("https://api.dingtalk.com/v1.0/oauth2/accessToken",
                          method="POST",
                          body={"appKey": self.app_key, "appSecret": self.app_secret})
            self._token = r["accessToken"]
        return self._token

    def _h(self):
        return {"x-acs-dingtalk-access-token": self.token(),
                "Content-Type": "application/json"}

    def sheets(self):
        url = f"https://api.dingtalk.com/v1.0/notable/bases/{BASE_ID}/sheets?operatorId={self.operator_id}"
        return http_json(url, headers=self._h()).get("value", [])

    def records(self, sheet_id):
        out, next_token = [], ""
        while True:
            url = (f"https://api.dingtalk.com/v1.0/notable/bases/{BASE_ID}"
                   f"/sheets/{sheet_id}/records?operatorId={self.operator_id}&pageSize=100")
            if next_token:
                url += f"&nextToken={next_token}"
            data = http_json(url, headers=self._h())
            out.extend(data.get("records", []))
            if not data.get("hasMore") or not data.get("nextToken"):
                break
            next_token = data["nextToken"]
        return out

    def send_webhook_markdown(self, webhook, title, text):
        r = http_json(webhook, method="POST",
                      body={"msgtype": "markdown", "markdown": {"title": title, "text": text}})
        if r.get("errcode") != 0:
            raise RuntimeError(f"webhook 发送失败: {r}")
        return r


# ---------- 值解析 ----------
def flat(v, depth=0):
    if depth > 3 or v is None:
        return None
    if isinstance(v, dict):
        for k in ("text", "value", "number", "date", "name"):
            if k in v:
                return flat(v[k], depth + 1)
        return None
    if isinstance(v, list):
        return ", ".join(str(x) for x in (flat(i, depth + 1) for i in v) if x)
    return v


def ms_to_day(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, tz=TZ_CN).strftime("%Y-%m-%d")
    s = str(v).strip()
    if s.isdigit() and len(s) >= 12:
        return datetime.fromtimestamp(int(s) / 1000, tz=TZ_CN).strftime("%Y-%m-%d")
    return None


def to_num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").replace("¥", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def agg_day(records, day_str):
    """渠道明细表当日聚合（销售额/推广费/店铺数）"""
    sales, promo, shops = 0.0, 0.0, set()
    for r in records:
        f = r.get("fields") or {}
        if ms_to_day(f.get("日期")) != day_str:
            continue
        s = to_num(flat(f.get("销售额")))
        if s is None:
            continue
        sales += s
        promo += to_num(flat(f.get("推广费"))) or 0.0
        sh = flat(f.get("店铺"))
        if sh:
            shops.add(str(sh).strip())
    return {"sales": sales, "promo": promo, "shops": len(shops)}


def agg_mtd(records, day_str):
    """月初~目标日累计销售额"""
    prefix = day_str[:7]
    total = 0.0
    for r in records:
        f = r.get("fields") or {}
        d = ms_to_day(f.get("日期"))
        if not d or not d.startswith(prefix) or d > day_str:
            continue
        s = to_num(flat(f.get("销售额")))
        if s is not None:
            total += s
    return total


def agg_daily_series(records, day_str, days=7):
    """近 *days* 天（含 *day_str*）的每日销售额，返回升序 [(日期, 销售额)]。

    缺数的日子补 0 而非跳过 —— sparkline 按位置映射高度，跳过一天会把趋势
    横向压缩失真（周中停摆会被画成连续下滑）。AI 明细表一行就是一个店铺
    一天，故按日期分桶求和即可。
    """
    end = datetime.strptime(day_str, "%Y-%m-%d").date()
    start = end - timedelta(days=days - 1)
    buckets = {(start + timedelta(days=i)).isoformat(): 0.0 for i in range(days)}
    for r in records:
        f = r.get("fields") or {}
        d = ms_to_day(f.get("日期"))
        if d in buckets:
            s = to_num(flat(f.get("销售额")))
            if s is not None:
                buckets[d] += s
    return sorted(buckets.items())


def merge_series(series_list, days=7):
    """把多个渠道的近 N 日序列逐日相加 → [(日期, 全渠道销售额)]。"""
    totals = {}
    for series in series_list:
        for d, v in series:
            totals[d] = totals.get(d, 0.0) + v
    return sorted(totals.items())[-days:]


def agg_targets(records):
    """月目标表 → {渠道: 月目标}"""
    out = {}
    for r in records:
        f = r.get("fields") or {}
        ch = flat(f.get("渠道"))
        tgt = to_num(flat(f.get("月目标")))
        if ch and tgt is not None:
            out[str(ch).strip()] = out.get(str(ch).strip(), 0.0) + tgt
    return out


def wan(v):
    if v is None:
        return "--"
    return f"{v / 10000:.1f}万" if abs(v) >= 10000 else f"{v:,.0f}"


def build_md(day_str, today, prev, mtd, targets):
    total_sales = sum(v["sales"] for v in today.values())
    total_promo = sum(v["promo"] for v in today.values())
    mtd_total = sum(mtd.values())
    target_total = sum(targets.values())

    head = f"**全渠道销售额：{wan(total_sales)} 元**（推广费 {wan(total_promo)}，月累计 {wan(mtd_total)}"
    if target_total:
        head += f"，月目标达成 {mtd_total / target_total * 100:.1f}%"
    head += "）"

    lines = [f"### 渠道日报 {day_str}", "", head, "",
             "| 渠道 | 销售额 | 推广费 | ROI | 环比 | 月累计/目标 | 达成率 |",
             "|---|---|---|---|---|---|---|"]
    for ch in CHANNELS:
        row = today.get(ch)
        if not row or row["sales"] == 0:
            lines.append(f"| {ch} | -- | -- | -- | -- | -- | -- |")
            continue
        sales, promo = row["sales"], row["promo"]
        roi = f"{sales / promo:.2f}" if promo > 0 else "--"
        mom = "--"
        if ch in prev and prev[ch]["sales"]:
            r = (sales - prev[ch]["sales"]) / abs(prev[ch]["sales"])
            mom = f"{'↑' if r >= 0 else '↓'}{abs(r) * 100:.1f}%"
        m, t = mtd.get(ch, 0.0), targets.get(ch)
        tail = f"{wan(m)}/{wan(t)} | {m / t * 100:.1f}%" if t else f"{wan(m)}/-- | --"
        lines.append(f"| {ch} | {wan(sales)} | {wan(promo)} | {roi} | {mom} | {tail} |")

    lines.append("")
    lines.append(f"> 数据来源：钉钉AI表格「电商渠道日报表」 · 生成时间 "
                 f"{datetime.now(TZ_CN).strftime('%H:%M')}")
    return "\n".join(lines)


def month_context(day_str):
    """当月时间进度 + 剩余天数，用于判断目标达成是超前还是落后。"""
    d = datetime.strptime(day_str, "%Y-%m-%d")
    days_in_month = calendar.monthrange(d.year, d.month)[1]
    elapsed = d.day
    return elapsed / days_in_month, days_in_month - elapsed


def build_bi_md(day_str, today, prev, mtd, targets, series):
    """BI 化日报正文：KPI 概览 + 结构化图（条形图/趋势迷你图/进度条）+ 预警。

    与 :func:`build_md` 的区别在于"先给结论、再给分布"：读的人第一屏拿到
    全渠道走势和目标进度，再去表里找自己负责的渠道。所有图形都由
    :mod:`bi_chart` 的字符函数产出，不依赖任何第三方绘图库。
    """
    L = []
    total_sales = sum(v["sales"] for v in today.values())
    total_promo = sum(v["promo"] for v in today.values())
    total_prev = sum(v["sales"] for v in prev.values())
    mtd_total = sum(mtd.values())
    target_total = sum(targets.values())
    roi = (total_sales / total_promo) if total_promo > 0 else None

    L.append(f"### \U0001f4ca 渠道日报 {day_str}")
    L.append("")

    # ---- 第一屏：全渠道 KPI + 近 7 日走势 ----
    trend = chart_spark([v for _, v in series])
    L.append(f"**全渠道 {fmt_wan(total_sales)}** {fmt_change(total_sales, total_prev)} "
             f"· 近7日 `{trend}`")
    L.append(f"> 推广费 {fmt_wan(total_promo)} · ROI {roi:.1f}" if roi
             else f"> 推广费 {fmt_wan(total_promo)} · ROI --")
    L.append("")

    time_rate, days_left = month_context(day_str)
    gap_note = ""
    if target_total:
        done = mtd_total / target_total
        delta = (done - time_rate) * 100
        judge = "超前" if delta >= 0 else "落后"
        gap_note = (f"（时间进度 {fmt_pct(time_rate)} · 进度{judge} {abs(delta):.1f}pt · "
                    f"剩 {days_left} 天缺口 {fmt_wan(max(target_total - mtd_total, 0))}）")
        L.append(f"**月目标 {fmt_wan(target_total)}** 达成 "
                 f"{fmt_pct(done)} {chart_gauge(done, 10)}")
        L.append(f"> 月累计 {fmt_wan(mtd_total)} {gap_note}")
    else:
        L.append(f"**月累计 {fmt_wan(mtd_total)}**")
    L.append("")

    # ---- 第二屏：当日格局（按销售额降序，条形图表征份额）----
    ranked = sorted(
        ((ch, v) for ch, v in today.items() if v["sales"] > 0),
        key=lambda kv: kv[1]["sales"], reverse=True,
    )
    if ranked:
        peak = ranked[0][1]["sales"]
        L.append("**\U0001f4c8 当日格局**（销售额降序 · 条形为占榜首比例）")
        L.append("")
        L.append("| 渠道 | 销售额 | 环比 | ROI | 占比 |")
        L.append("|---|---|---|---|---|")
        for ch, v in ranked:
            share = v["sales"] / total_sales if total_sales else 0
            base = prev.get(ch, {}).get("sales") if ch in prev else None
            ch_roi = (v["sales"] / v["promo"]) if v["promo"] > 0 else None
            roi_txt = f"{ch_roi:.1f}" if ch_roi is not None else "--"
            L.append(f"| {ch} | {fmt_wan(v['sales'])} {chart_bar(v['sales'], peak, 10)} "
                     f"| {fmt_change(v['sales'], base)} "
                     f"| {roi_txt} | {fmt_pct(share, 0)} |")
        L.append("")

    # ---- 第三屏：月目标进度（进度条表征达成率）----
    tracked = sorted(
        ((ch, mtd.get(ch, 0.0), targets.get(ch)) for ch in targets),
        key=lambda x: (x[2] and x[1] / x[2] or 0), reverse=True,
    )
    if tracked:
        L.append("**\U0001f3af 月目标达成**（达成率降序）")
        L.append("")
        L.append("| 渠道 | 达成率 | 月累计 / 目标 |")
        L.append("|---|---|---|")
        for ch, done, tgt in tracked:
            if not tgt:
                L.append(f"| {ch} | -- | {fmt_wan(done)} / -- |")
                continue
            rate = done / tgt
            L.append(f"| {ch} | {fmt_pct(rate)} {chart_gauge(rate, 10)} "
                     f"| {fmt_wan(done)} / {fmt_wan(tgt)} |")
        L.append("")

    # ---- 收尾：异常预警（只播需要人动作的，避免噪音）----
    alerts = []
    for ch, v in today.items():
        base = prev.get(ch, {}).get("sales") if ch in prev else None
        if v["sales"] == 0:
            alerts.append(f"{ch} 当日无数据")
            continue
        if base and base > 0:
            drop = (v["sales"] - base) / base
            if drop <= -0.3:
                alerts.append(f"{ch} {fmt_change(v['sales'], base)}"
                              f"（{fmt_wan(base)}→{fmt_wan(v['sales'])}）")
    if alerts:
        L.append(f"\u26a0\ufe0f **需关注**：{' · '.join(alerts)}")
        L.append("")

    L.append(f"> 数据来源：钉钉AI表格「电商渠道日报表」 · 生成时间 "
             f"{datetime.now(TZ_CN).strftime('%H:%M')}")
    return "\n".join(L)


def _html_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def write_bi_page(day_str, today, prev, mtd, targets, series):
    """输出一份自包含的静态 BI 看板页（channel-bi-page/index.html）。

    群里那张 markdown 是"一眼结论"，这份页面是"可交互细节"：ECharts 画
    近 7 日走势、当日渠道分布、达成率对比。图表库走 CDN，加载失败时页面
    里的表格与 KPI 数字仍然完整可读 —— 群播报的价值不依赖 CDN 可用性。
    同时按日期归档一份同名 snapshot，便于回看历史某天的播报。
    """
    total_sales = sum(v["sales"] for v in today.values())
    total_promo = sum(v["promo"] for v in today.values())
    total_prev = sum(v["sales"] for v in prev.values())
    mtd_total = sum(mtd.values())
    target_total = sum(targets.values())
    roi = (total_sales / total_promo) if total_promo > 0 else None

    rows = sorted(
        ((ch, v["sales"], v["promo"], prev.get(ch, {}).get("sales"),
          mtd.get(ch, 0.0), targets.get(ch))
         for ch, v in today.items()),
        key=lambda x: x[1], reverse=True,
    )
    table = []
    for ch, sales, promo, base, done, tgt in rows:
        rate = (done / tgt) if tgt else None
        table.append({
            "channel": ch, "sales": sales, "promo": promo, "base": base or 0,
            "roi": (sales / promo) if promo > 0 else None,
            "mtd": done, "target": tgt, "rate": rate,
        })
    payload = {
        "day": day_str,
        "total": total_sales, "prev": total_prev, "promo": total_promo,
        "roi": roi, "mtd": mtd_total, "target": target_total,
        "trendDays": [d[5:] for d, _ in series],
        "trendValues": [round(v, 2) for _, v in series],
        "rows": table,
        "generated": datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M"),
    }

    def _roi_cell(v):
        return f"{v:.2f}" if v is not None else "--"

    rows_html = "\n".join(
        f"<tr><td>{_html_escape(r['channel'])}</td>"
        f"<td class='num'>{fmt_wan(r['sales'])}</td>"
        f"<td class='num'>{fmt_wan(r['promo'])}</td>"
        f"<td class='num'>{_roi_cell(r['roi'])}</td>"
        f"<td class='num'>{fmt_change(r['sales'], r['base'])}</td>"
        f"<td class='num'>{fmt_wan(r['mtd'])}</td>"
        f"<td class='num'>{fmt_wan(r['target'])}</td>"
        f"<td class='num'>{fmt_pct(r['rate'])}</td></tr>"
        for r in table
    )
    html = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>渠道日报 BI 看板 · {day}</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#f5f7fa;color:#1f2937;padding:20px}}
.header{{background:linear-gradient(135deg,#1e3a8a,#2563eb);color:#fff;padding:20px 24px;border-radius:10px;margin-bottom:16px}}
.header h1{{font-size:19px;font-weight:600}}
.header .sub{{font-size:12px;opacity:.8;margin-top:6px}}
.kpis{{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:16px}}
.kpi{{flex:1;min-width:150px;background:#fff;border-radius:10px;padding:14px 16px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.kpi .label{{font-size:12px;color:#6b7280}}
.kpi .value{{font-size:22px;font-weight:600;margin-top:6px;font-variant-numeric:tabular-nums}}
.kpi .delta{{font-size:12px;margin-top:4px}}
.up{{color:#059669}}.down{{color:#dc2626}}
.card{{background:#fff;border-radius:10px;padding:16px 18px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.card h2{{font-size:14px;margin-bottom:10px}}
.chart{{width:100%;height:260px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #f3f4f6}}
th{{background:#f9fafb;font-weight:600;font-size:12px;color:#374151}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.footer{{text-align:center;color:#9ca3af;font-size:11px;padding:12px}}
</style></head><body>
<div class="header"><h1>渠道日报 BI 看板</h1>
<div class="sub">{day} · 数据源 钉钉AI表格「电商渠道日报表」· 生成于 {generated}</div></div>
<div class="kpis">
<div class="kpi"><div class="label">全渠道销售额</div><div class="value">{total_wan}</div><div class="delta {trend_cls}">环比 {delta}</div></div>
<div class="kpi"><div class="label">推广费</div><div class="value">{promo_wan}</div><div class="delta">ROI {roi_txt}</div></div>
<div class="kpi"><div class="label">月累计</div><div class="value">{mtd_wan}</div><div class="delta">目标 {target_wan}</div></div>
<div class="kpi"><div class="label">月目标达成</div><div class="value">{rate_txt}</div><div class="delta">时间进度 {time_txt}</div></div>
</div>
<div class="card"><h2>近 7 日全渠道走势</h2><div id="trend" class="chart"></div></div>
<div class="card"><h2>当日渠道分布（销售额 / ROI）</h2><div id="mix" class="chart"></div></div>
<div class="card"><h2>月目标达成率</h2><div id="goal" class="chart"></div></div>
<div class="card"><h2>渠道明细</h2><table>
<tr><th>渠道</th><th class="num">销售额</th><th class="num">推广费</th><th class="num">ROI</th><th class="num">环比</th><th class="num">月累计</th><th class="num">月目标</th><th class="num">达成率</th></tr>
{rows_html}
</table></div>
<div class="footer">每日 11:00 自动更新 · 达成率 = 月累计 ÷ 月目标</div>
<script>
var DATA = {payload};
if (window.echarts) {{
  var names = DATA.rows.map(function (r) {{ return r.channel; }});
  var sales = DATA.rows.map(function (r) {{ return +(r.sales / 10000).toFixed(2); }});
  var roi = DATA.rows.map(function (r) {{ return r.roi ? +r.roi.toFixed(2) : 0; }});
  var rate = DATA.rows.map(function (r) {{ return r.rate ? +(r.rate * 100).toFixed(1) : 0; }});
  echarts.init(document.getElementById('trend')).setOption({{
    grid: {{left: 8, right: 12, top: 24, bottom: 8, containLabel: true}},
    tooltip: {{trigger: 'axis'}},
    xAxis: {{type: 'category', data: DATA.trendDays, boundaryGap: false}},
    yAxis: {{type: 'value', axisLabel: {{formatter: '{{value}} 万'}}}},
    series: [{{type: 'line', smooth: true, areaStyle: {{}}, data: DATA.trendValues.map(function (v) {{ return +(v / 10000).toFixed(2); }}), itemStyle: {{color: '#2563eb'}}}}]
  }});
  echarts.init(document.getElementById('mix')).setOption({{
    grid: {{left: 8, right: 12, top: 40, bottom: 8, containLabel: true}},
    tooltip: {{trigger: 'axis', axisPointer: {{type: 'shadow'}}}},
    legend: {{data: ['销售额(万)', 'ROI']}},
    xAxis: {{type: 'category', data: names}},
    yAxis: [{{type: 'value', name: '万'}}, {{type: 'value', name: 'ROI'}}],
    series: [
      {{name: '销售额(万)', type: 'bar', barWidth: '45%', data: sales, itemStyle: {{color: '#3b82f6', borderRadius: [4, 4, 0, 0]}}}},
      {{name: 'ROI', type: 'line', yAxisIndex: 1, data: roi, itemStyle: {{color: '#f59e0b'}}}}
    ]
  }});
  echarts.init(document.getElementById('goal')).setOption({{
    grid: {{left: 8, right: 24, top: 24, bottom: 8, containLabel: true}},
    tooltip: {{trigger: 'axis', formatter: '{{b}}: {{c}}%'}},
    xAxis: {{type: 'value', max: function (v) {{ return Math.max(100, Math.ceil(v.max / 20) * 20); }}}},
    yAxis: {{type: 'category', data: names.slice().reverse(), inverse: false}},
    series: [
      {{type: 'bar', data: rate.slice().reverse(), barWidth: '55%',
        label: {{show: true, position: 'right', formatter: '{{c}}%'}},
        itemStyle: {{borderRadius: [0, 4, 4, 0], color: function (p) {{ return p.value >= 100 ? '#059669' : (p.value >= 50 ? '#2563eb' : '#f59e0b'); }}}}}},
      {{type: 'line', data: names.map(function () {{ return 100; }}), symbol: 'none',
        lineStyle: {{type: 'dashed', color: '#dc2626'}}}}
    ]
  }});
}} else {{
  document.querySelectorAll('.chart').forEach(function (el) {{
    el.innerHTML = '<p style="color:#9ca3af;font-size:12px;padding:16px 0">图表库未能加载（离线环境），下方明细数据不受影响。</p>';
  }});
}}
</script>
</body></html>""".format(
        day=day_str,
        generated=payload["generated"],
        total_wan=fmt_wan(total_sales),
        promo_wan=fmt_wan(total_promo),
        mtd_wan=fmt_wan(mtd_total),
        target_wan=fmt_wan(target_total) if target_total else "--",
        roi_txt=f"{roi:.2f}" if roi else "--",
        rate_txt=fmt_pct(mtd_total / target_total) if target_total else "--",
        time_txt=fmt_pct(month_context(day_str)[0]),
        delta=fmt_change(total_sales, total_prev),
        trend_cls="down" if total_sales < total_prev else "up",
        rows_html=rows_html,
        payload=json.dumps(payload, ensure_ascii=False),
    )

    BI_PAGE_DIR.mkdir(parents=True, exist_ok=True)
    (BI_PAGE_DIR / f"{day_str}.html").write_text(html, encoding="utf-8")
    index = BI_PAGE_DIR / "index.html"
    index.write_text(html, encoding="utf-8")
    return index


def load_config():
    """读取 config.json（与原 main.py 共用），读不到则报错"""
    cfg_path = BASE_DIR / "config.json"
    if cfg_path.exists():
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    print("未找到 config.json")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD，默认昨日")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--style", choices=("bi", "table"), default="bi",
                    help="bi=图表版（默认）；table=纯表格版")
    ap.add_argument("--html", action="store_true",
                    help="同时输出静态 BI 看板 HTML（供托管/外链）")
    args = ap.parse_args()

    day = args.date or (datetime.now(TZ_CN).date() - timedelta(days=1)).isoformat()
    prev = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

    cfg = load_config()
    dt = DT(cfg["dingtalk"]["appKey"], cfg["dingtalk"]["appSecret"],
            cfg["dingtalk"]["operatorId"])
    client = DingTalkClient.from_config(cfg["dingtalk"])
    push = resolve_target(cfg["push"])

    # 表名 → sheetId
    sheets = {s["name"]: s["id"] for s in dt.sheets()}
    today, prev_agg, mtd, series_pool = {}, {}, {}, []
    for ch in CHANNELS:
        tname = f"{ch}渠道数据{MONTH}"
        if tname not in sheets:
            print(f"WARN: 未找到表「{tname}」，跳过该渠道")
            continue
        recs = dt.records(sheets[tname])
        today[ch] = agg_day(recs, day)
        prev_agg[ch] = agg_day(recs, prev)
        mtd[ch] = agg_mtd(recs, day)
        series_pool.append(agg_daily_series(recs, day, TREND_DAYS))
        print(f"{ch}: 当日{today[ch]['sales']:,.0f} / 环比基期{prev_agg[ch]['sales']:,.0f} / MTD{mtd[ch]:,.0f}")

    series = merge_series(series_pool, TREND_DAYS) if series_pool else []

    tname = f"渠道销售目标达成率{MONTH}"
    targets = agg_targets(dt.records(sheets[tname])) if tname in sheets else {}
    print(f"月目标: { {k: f'{v:,.0f}' for k, v in targets.items()} }")

    if args.style == "bi":
        md = build_bi_md(day, today, prev_agg, mtd, targets, series)
    else:
        md = build_md(day, today, prev_agg, mtd, targets)

    if args.html:
        path = write_bi_page(day, today, prev_agg, mtd, targets, series)
        print(f"BI 看板页已生成: {path}")

    if args.dry:
        print("\n" + md + "\n")
        return

    send_markdown(client, push, f"渠道日报 {day}", md)
    print(f"推送成功 -> {push.get('groupName', push.get('webhook', 'unknown'))}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
