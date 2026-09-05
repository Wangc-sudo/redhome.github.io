# -*- coding: utf-8 -*-
import html as html_mod
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.dingtalk import DingTalkClient


def _parse_num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _fmt_wan(v):
    v = float(v)
    if v >= 100000000:
        return f"{v/100000000:.2f}亿"
    if v >= 10000:
        return f"{v/10000:.1f}万"
    return f"{v:,.0f}"


def _fmt_pct(v, digits=1):
    return f"{v*100:.{digits}f}%"


def collect(config, include_today=False):
    now = datetime.now()
    calendar = config["calendar"]
    month = calendar["month"]
    if now.month != month:
        raise SystemExit(f"当前月份{now.month}与配置月份{month}不符")
    workdays = [d for d in range(1, 31) if d not in calendar.get("restDays", [])]
    today = now.day
    elapsed = [d for d in workdays if d < today or (include_today and d == today)]

    client = DingTalkClient.from_config(config["dingtalk"])
    base = config["base"]
    records = client.list_records(base["baseId"], base["tableId"])
    target_col = f"{month}月销量目标（万）"

    people = []
    for rec in records:
        f = rec.get("fields") or {}
        name = str(f.get("责任人") or "").strip()
        if not name or "合计" in name:
            continue
        dept = str(f.get("项目部") or "").strip() or "未分组"
        target = _parse_num(f.get(target_col)) or 0
        completed = 0.0
        unfilled = 0
        for d in elapsed:
            v = f.get(f"{d}日")
            if v is None or str(v).strip() == "":
                unfilled += 1
            else:
                parsed = _parse_num(v)
                if parsed is None:
                    unfilled += 1
                else:
                    completed += parsed
        people.append({
            "name": name, "dept": dept, "target": target, "completed": completed,
            "unfilled": unfilled,
            "rate": completed / target if target > 0 else None,
        })

    people.sort(key=lambda p: (
        -(p["rate"] if p["rate"] is not None else -1),
        -p["completed"],
        -p["target"],
    ))
    return now, elapsed, people


def build_bc_markdown(config, url=None):
    region = config["region"]
    calendar = config["calendar"]
    now, elapsed, people = collect(config, include_today=False)
    n_elapsed = len(elapsed)
    workdays = [d for d in range(1, 31) if d not in calendar.get("restDays", [])]
    n_total = len(workdays)
    progress = n_elapsed / n_total if n_total else 0

    bc_people = [p for p in people if p["name"] not in region.get("broadcastExclude", [])]
    depts = {}
    for p in bc_people:
        depts.setdefault(p["dept"], []).append(p)
    stats = []
    for dname in region.get("deptOrder", []) + [d for d in depts if d not in region.get("deptOrder", [])]:
        members = depts.get(dname)
        if not members:
            continue
        dc = sum(m["completed"] for m in members)
        dt = sum(m["target"] for m in members)
        stats.append({
            "name": dname, "rate": dc / dt if dt else 0,
            "completed": dc, "target": dt, "count": len(members),
            "unfilled": sum(m["unfilled"] for m in members),
        })
    stats.sort(key=lambda x: -x["rate"])

    total_c = sum(p["completed"] for p in bc_people)
    total_t = sum(p["target"] for p in bc_people)
    overall = total_c / total_t if total_t else 0
    weekday = "一二三四五六日"[now.weekday()]
    stat_thru = f"{now.month}月{elapsed[-1]}日" if elapsed else "—"
    display = region.get("displayName", region["name"])

    lines = [f"### 📊 {display}销售完成率榜（{stat_thru} 周{weekday}）", ""]
    lines.append(f"时间进度 **{_fmt_pct(progress)}**（{n_elapsed}/{n_total} 工作日） · "
                 f"整体完成率 **{_fmt_pct(overall)}**（{_fmt_pct(overall - progress, 1)} vs 进度）")
    lines.append("")
    lines.append("| 排名 | 部门 | 完成金额 / 目标 | 完成率 | 进度差 / 预计月末 |")
    lines.append("|:--:|:--|:--|--:|:--|")
    for i, st in enumerate(stats):
        lb = region.get("deptLabel", {}).get(st["name"], st["name"])
        diff = st["rate"] - progress
        diff_txt = f"+{_fmt_pct(diff)}" if diff >= 0 else _fmt_pct(diff)
        proj = st["rate"] / progress if progress > 0 else 0
        proj_txt = f"{proj*100:.0f}%" if proj <= 9.99 else "999%+"
        lines.append(f"| {i+1} | {lb} | {_fmt_wan(st['completed'])} / {_fmt_wan(st['target'])} | "
                     f"**{_fmt_pct(st['rate'])}** | {diff_txt} / {proj_txt} |")
    if url:
        lines.append("")
        lines.append(f"📊 [点击查看完整榜单（个人明细）]({url})")
    return "\n".join(lines)


def build_html(config, now, elapsed, people):
    calendar = config["calendar"]
    region = config["region"]
    month = calendar["month"]
    workdays = [d for d in range(1, 31) if d not in calendar.get("restDays", [])]
    dept_order = region.get("deptOrder", [])
    dept_label = region.get("deptLabel", {})
    bc_exclude = region.get("broadcastExclude", [])
    display = region.get("displayName", region["name"])

    n_elapsed = len(elapsed)
    n_total = len(workdays)
    progress = n_elapsed / n_total if n_total else 0
    total_completed = sum(p["completed"] for p in people)
    total_target = sum(p["target"] for p in people)
    overall_rate = total_completed / total_target if total_target else 0

    def diff_badge(rate):
        if rate is None:
            return '<span class="badge gray">无目标</span>'
        d = rate - progress
        if d >= 0:
            return f'<span class="badge green">领先 {_fmt_pct(d)}</span>'
        if d >= -0.05:
            return f'<span class="badge amber">落后 {_fmt_pct(-d)}</span>'
        return f'<span class="badge red">落后 {_fmt_pct(-d)}</span>'

    def bar(rate):
        if rate is None:
            return '<span class="muted">—</span>'
        pct = rate * 100
        w = min(pct, 100)
        cls = "g" if rate >= progress else ("y" if rate >= progress * 0.8 else "r")
        marker = min(progress * 100, 100)
        return (f'<div class="bar"><div class="fill {cls}" style="width:{w:.1f}%"></div>'
                f'<div class="marker" style="left:{marker:.1f}%"></div>'
                f'<span class="barlabel">{_fmt_pct(rate)}</span></div>')

    # ---- 个人行 ----
    def person_row(i, p):
        rate = p["rate"]
        proj = (rate / progress * 100) if (rate is not None and progress > 0) else None
        proj_txt = f"{proj:.0f}%" if proj is not None else "—"
        if proj is not None and proj > 999:
            proj_txt = "999%+"
        daily = p["completed"] / n_elapsed if n_elapsed else 0
        unf = (f'<span class="warn">{p["unfilled"]}天</span>' if p["unfilled"]
               else '<span class="ok">0</span>')
        rank_cls = "top" if i < 5 else ("bottom" if i >= len(people) - 5 else "")
        dept_lb = dept_label.get(p["dept"], p["dept"])
        return f"""<tr class="{rank_cls}">
<td class="rank">{i+1 if i>=5 or i<len(people)-5 else ''}</td>
<td class="strong">{html_mod.escape(p['name'])}</td><td>{html_mod.escape(dept_lb)}</td>
<td class="num">{_fmt_wan(p['completed'])}</td><td class="num muted">{_fmt_wan(p['target'])}</td>
<td>{bar(rate)}</td><td>{diff_badge(rate)}</td>
<td class="num">{_fmt_wan(daily)}</td><td class="num">{proj_txt}</td><td class="num">{unf}</td></tr>"""

    person_rows = "\n".join(person_row(i, p) for i, p in enumerate(people))

    # ---- 部门聚合 ----
    depts = {}
    for p in people:
        depts.setdefault(p["dept"], []).append(p)
    dept_cards = []
    dept_stats = []
    for dname in dept_order + [d for d in depts if d not in dept_order]:
        members = depts.get(dname)
        if not members:
            continue
        dc = sum(m["completed"] for m in members)
        dt = sum(m["target"] for m in members)
        dr = dc / dt if dt else 0
        dept_stats.append({"name": dname, "rate": dr, "completed": dc, "target": dt,
                           "count": len(members)})
    dept_stats.sort(key=lambda x: -x["rate"])

    for st in dept_stats:
        dname = st["name"]
        members = sorted(depts[dname], key=lambda p: -(p["rate"] if p["rate"] is not None else -1))
        lb = dept_label.get(dname, dname)
        diff = st["rate"] - progress
        diff_html = (f'<span class="badge green">领先 {_fmt_pct(diff)}</span>' if diff >= 0
                     else f'<span class="badge red">落后 {_fmt_pct(-diff)}</span>')
        mrows = "\n".join(
            f'<tr><td class="rank">{j+1}</td><td class="strong">{html_mod.escape(m["name"])}</td>'
            f'<td class="num">{_fmt_wan(m["completed"])}</td>'
            f'<td class="num muted">{_fmt_wan(m["target"])}</td>'
            f'<td style="min-width:120px">{bar(m["rate"])}</td><td>{diff_badge(m["rate"])}</td></tr>'
            for j, m in enumerate(members))
        cls = "g" if st["rate"] >= progress else ("y" if st["rate"] >= progress * 0.8 else "r")
        dept_cards.append(f"""
<div class="dept-card">
  <div class="dept-head">
    <div><span class="dept-name">{html_mod.escape(lb)}</span>
      <span class="muted small">{st['count']}人 · 目标 {_fmt_wan(st['target'])}</span></div>
    <div class="dept-rate {cls}">{_fmt_pct(st['rate'])} {diff_html}</div>
  </div>
  <div class="dept-sub">累计 <b>{_fmt_wan(st['completed'])}</b> · 人均 {_fmt_wan(st['completed']/st['count'])} · 日均 {_fmt_wan(st['completed']/(n_elapsed or 1))}</div>
  <table class="mini"><thead><tr><th>#</th><th>姓名</th><th>完成</th><th>目标</th><th>完成率</th><th>进度差</th></tr></thead>
  <tbody>{mrows}</tbody></table>
</div>""")
    dept_html = "\n".join(dept_cards)

    # ---- 播报块：部门维度（不播个人，避免公开点名；排除名单人员不参与） ----
    bc_people = [p for p in people if p["name"] not in bc_exclude]
    bc_depts = {}
    for p in bc_people:
        bc_depts.setdefault(p["dept"], []).append(p)
    bc_dept_stats = []
    for dname in dept_order + [d for d in bc_depts if d not in dept_order]:
        members = bc_depts.get(dname)
        if not members:
            continue
        dc = sum(m["completed"] for m in members)
        dt = sum(m["target"] for m in members)
        bc_dept_stats.append({"name": dname, "rate": dc / dt if dt else 0,
                              "completed": dc, "target": dt, "count": len(members),
                              "members": members})
    bc_dept_stats.sort(key=lambda x: -x["rate"])

    dept_bc_rows = []
    for i, st in enumerate(bc_dept_stats):
        dname = st["name"]
        members = st["members"]
        diff = st["rate"] - progress
        diff_html = (f'<span class="badge green">领先 {_fmt_pct(diff)}</span>' if diff >= 0
                     else f'<span class="badge red">落后 {_fmt_pct(-diff)}</span>')
        cls = "g" if st["rate"] >= progress else ("y" if st["rate"] >= progress * 0.8 else "r")
        unf_total = sum(m["unfilled"] for m in members)
        unf_html = (f'<span class="warn">{unf_total}天</span>' if unf_total
                    else '<span class="ok">0</span>')
        proj = st["rate"] / progress if progress > 0 else None
        proj_txt = f"{proj*100:.0f}%" if proj is not None else "—"
        if proj is not None and proj > 9.99:
            proj_txt = "999%+"
        dept_bc_rows.append(
            f'<tr><td class="rank">{i+1}</td>'
            f'<td class="strong">{html_mod.escape(dept_label.get(dname, dname))}</td>'
            f'<td class="num">{st["count"]}</td>'
            f'<td class="num">{_fmt_wan(st["completed"])}</td>'
            f'<td class="num muted">{_fmt_wan(st["target"])}</td>'
            f'<td class="num {cls}" style="font-weight:700">{_fmt_pct(st["rate"])}</td>'
            f'<td>{diff_html}</td>'
            f'<td class="num">{_fmt_wan(st["completed"]/(n_elapsed or 1))}</td>'
            f'<td class="num">{proj_txt}</td>'
            f'<td class="num">{unf_html}</td></tr>')
    dept_bc_html = "\n".join(dept_bc_rows)

    weekday = "一二三四五六日"[now.weekday()]
    gen_time = now.strftime("%Y-%m-%d %H:%M")
    stat_thru = f"{month}月{elapsed[-1]}日" if elapsed else "—"

    exclude_note = "、".join(bc_exclude)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_mod.escape(display)}销售完成率榜单 · {html_mod.escape(month)}月</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f6f8;color:#1f2329;padding:16px;max-width:1080px;margin:0 auto}}
h1{{font-size:20px;margin-bottom:4px}}
.sub{{color:#8a919f;font-size:13px;margin-bottom:16px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}}
.card{{background:#fff;border-radius:10px;padding:12px 14px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.card .k{{font-size:12px;color:#8a919f;margin-bottom:4px}}
.card .v{{font-size:20px;font-weight:700}}
.card .s{{font-size:11px;color:#8a919f;margin-top:2px}}
.g{{color:#0ab561}}.r{{color:#e54545}}.y{{color:#e8830c}}.gray{{color:#8a919f}}
.panel{{background:#fff;border-radius:10px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.06);margin-bottom:16px}}
.panel h2{{font-size:15px;margin-bottom:10px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;color:#8a919f;font-weight:500;padding:6px 8px;border-bottom:1px solid #eef0f3;white-space:nowrap}}
td{{padding:7px 8px;border-bottom:1px solid #f3f4f6;white-space:nowrap}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}
td.strong{{font-weight:600}}
tr.top td{{background:#f0fbf5}}tr.bottom td{{background:#fdf2f2}}
.rank{{color:#8a919f;text-align:center;width:34px}}
.badge{{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px}}
.badge.green{{background:#e8f8f0;color:#0ab561}}.badge.red{{background:#fdeeee;color:#e54545}}
.badge.amber{{background:#fdf3e6;color:#e8830c}}.badge.gray{{background:#f0f1f3;color:#8a919f}}
.bar{{position:relative;background:#eef0f3;border-radius:4px;height:18px;min-width:110px}}
.fill{{height:100%;border-radius:4px;position:absolute;top:0;left:0}}
.fill.g{{background:linear-gradient(90deg,#12b76a,#0ab561)}}
.fill.y{{background:linear-gradient(90deg,#f79009,#e8830c)}}
.fill.r{{background:linear-gradient(90deg,#f04438,#e54545)}}
.marker{{position:absolute;top:-2px;bottom:-2px;width:2px;background:#1f2329;opacity:.45}}
.barlabel{{position:absolute;right:6px;top:0;line-height:18px;font-size:11px;color:#1f2329}}
.tabs{{display:flex;gap:8px;margin-bottom:12px}}
.tab{{padding:6px 16px;border-radius:8px;background:#eef0f3;cursor:pointer;font-size:13px}}
.tab.on{{background:#1f2329;color:#fff}}
.dept-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px}}
.dept-card{{border:1px solid #eef0f3;border-radius:10px;padding:14px}}
.dept-head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}}
.dept-name{{font-weight:700;font-size:15px;margin-right:8px}}
.dept-rate{{font-weight:700;font-size:16px}}
.dept-rate .badge{{margin-left:6px}}
.dept-sub{{font-size:12px;color:#8a919f;margin-bottom:8px}}
table.mini td,table.mini th{{padding:4px 6px}}
.warn{{color:#e54545;font-weight:600}}.ok{{color:#0ab561}}
.muted{{color:#8a919f}}.small{{font-size:12px}}
.note{{font-size:12px;color:#8a919f;line-height:1.8}}
.note b{{color:#4e5761}}
.split{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
@media(max-width:760px){{.split{{grid-template-columns:1fr}}.bar{{min-width:90px}}}}
.pill{{display:inline-block;background:#eef0f3;border-radius:6px;padding:2px 10px;font-size:12px;color:#4e5761;margin-right:6px}}
</style></head><body>
<h1>{html_mod.escape(display)}销售日报 · 完成率榜单</h1>
<div class="sub">{now.year}年{month}月 · {now.month}月{elapsed[-1] if elapsed else now.day}日（周{weekday}） · 数据截至 {stat_thru} · 生成于 {gen_time}</div>

<div class="cards">
  <div class="card"><div class="k">时间进度</div><div class="v">{_fmt_pct(progress)}</div><div class="s">{n_elapsed} / {n_total} 个工作日</div></div>
  <div class="card"><div class="k">{display}整体完成率</div><div class="v {'g' if overall_rate>=progress else 'r'}">{_fmt_pct(overall_rate)}</div><div class="s">{_fmt_wan(total_completed)} / {_fmt_wan(total_target)}</div></div>
  <div class="card"><div class="k">整体进度差</div><div class="v {'g' if overall_rate>=progress else 'r'}">{_fmt_pct(overall_rate-progress)}</div><div class="s">相对时间进度</div></div>
  <div class="card"><div class="k">参与人数</div><div class="v">{len(people)}</div><div class="s">未填 0 天：{sum(1 for p in people if p['unfilled']==0)} 人</div></div>
</div>

<div class="panel">
  <h2>📣 每日播报（部门维度）</h2>
  <table><thead><tr><th>排名</th><th>部门</th><th>人数</th><th>完成金额</th><th>目标</th><th>完成率</th><th>进度差</th><th>日均完成</th><th>预计月末</th><th>未填</th></tr></thead>
  <tbody>{dept_bc_html}</tbody></table>
  <div class="small muted" style="margin-top:8px">个人明细不进群播报，请点击查看完整榜单（个人总榜 / 部门内排行）</div>
</div>

<div class="panel">
  <div class="tabs">
    <div class="tab on" data-tab="person" onclick="sw(this)">个人总榜</div>
    <div class="tab" data-tab="dept" onclick="sw(this)">部门维度</div>
  </div>
  <div id="tab-person">
  <table><thead><tr><th>排名</th><th>姓名</th><th>部门</th><th>完成金额</th><th>目标</th><th>完成率</th><th>进度差</th><th>日均完成</th><th>预计月末</th><th>未填</th></tr></thead>
  <tbody>{person_rows}</tbody></table>
  </div>
  <div id="tab-dept" style="display:none">
    <div class="dept-grid">{dept_html}</div>
  </div>
</div>

<div class="panel note">
<b>口径说明</b><br>
· 完成率 = 累计完成金额 ÷ {month}月销量目标（万）；统计范围为已过工作日（{stat_thru}）内的日列<br>
· 时间进度 = 已过工作日 {n_elapsed} ÷ 全月工作日 {n_total} = {_fmt_pct(progress)}（黑竖线为达标基准）<br>
· 进度差 = 完成率 − 时间进度，正值领先、负值落后；预计月末 = 完成率 ÷ 时间进度（线性外推，仅供参考）<br>
· 未填 = 已过工作日中空白的单元格数（填 0 不算未填）<br>
· 部门维度含 {display} 各项目部及运营总监<br>
· 群播报口径：仅部门维度，不含个人明细{f"与 {exclude_note}" if exclude_note else ""}
</div>

<script>
function sw(el){{
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
  el.classList.add('on');
  document.getElementById('tab-person').style.display = el.dataset.tab==='person'?'':'none';
  document.getElementById('tab-dept').style.display = el.dataset.tab==='dept'?'':'none';
}}
</script>
</body></html>"""
    return html
