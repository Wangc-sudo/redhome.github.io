#!/usr/bin/env python3
"""
热卖品监控（渠道日报表群 · 提醒事项机器人）
=============================================
每天点名 Top 热卖品的库存健康度，与 stock_alert 的异常驱动互补：
- stock_alert：出事才报（异常驱动）
- 本脚本：   每天都在视野里（清单驱动），热卖品不存在"消失"问题

名单规则（结合防断货静默）：
  Top 15 = 近30天销量前15
  ∪ 危险品保底 = 可售天数≤7 或 库存<0 且近30天有动销的 SKU（即使排名掉了也保留）
  ∓ 陈年烂账 = 近30天无销量的负库存 SKU 不进名单

展示字段：库存 / 可售天数 / 近30天销量 / 采购在途 / 状态（正常/偏低/断货中）

用法:
  python hot_items_monitor.py          # 拉数据推送
  python hot_items_monitor.py --dry    # 只看不推
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.dingtalk import DingTalkClient, send_markdown
from common.test_group import resolve_target
from wdt_client import WdtClient

STOCK_CACHE = BASE_DIR / ".stock_cache.json"  # 与 stock_alert 共用缓存
HTML_OUT_DIR = BASE_DIR / "hot-items-page"    # 全文HTML输出目录（供托管发布）

TOP_N = 10            # 热卖品名单大小
MAX_SHOW = 10         # 展示上限：只展示最危险的N个（断货>偏低>正常），其余折叠进统计
URGENT_DAYS = 7       # 可售天数阈值（危险品保底线）
DEAD_STOCK_DAILY = 0.3
KEEP_DAYS = 3         # 名单滞回：连续N天不满足条件才剔除
DANGER_MONTH_SALES = 300  # 危险品保底门槛：近30天销量≥此值才保底（防边缘滞销品膨胀名单）

# 只监控酒类：品名含酒类关键词才进名单（服务卡/赠品/包材/周边剔除）
WINE_KEYWORDS = ["酒", "茅台", "习酒", "古越龙山", "女儿红", "金沙", "赖茅", "郎酒",
                 "汾酒", "泸州", "洋河", "剑南春", "五粮液", "水井坊", "舍得", "口子窖"]
EXCLUDE_KEYWORDS = ["赠品", "服务", "卡", "杯", "伞", "包材", "开瓶器", "酒具"]


def is_wine(goods_name):
    name = goods_name or ""
    if any(k in name for k in EXCLUDE_KEYWORDS):
        return False
    return any(k in name for k in WINE_KEYWORDS)


# ---------- 同款归并：不同商家编码但为同一款酒（标红提示） ----------
# 归并键：去掉规格/容量/包装词后的"主体品名"（品牌+系列）
_SPEC_WORDS = ["ml", "毫升", "L", "升", "瓶", "装", "整箱", "单瓶", "礼盒",
               "套装", "箱装", "g", "克", "×", "x", "*", "ml)", ")"]
_CAPACITY_RE = None  # compiled lazily


def _normalize_name(name):
    """提取品名主体：去容量/规格/包装词，用于同款判定"""
    import re
    global _CAPACITY_RE
    if _CAPACITY_RE is None:
        # 容量数字+单位（500ml、2.5L、6瓶装、x2 等），含全角数字
        _CAPACITY_RE = re.compile(
            r"[0-9０-９]+\s*(?:ml|mL|ML|毫升|l|L|升|g|克|瓶|支|罐|杯|盒|箱|套|件|包|袋)"
            r"|[0-9０-９]+\s*[×xX*]\s*[0-9０-９]+"
            r"|[（(][^（）()]*[）)]")
    s = (name or "").strip()
    s = _CAPACITY_RE.sub("", s)
    for w in _SPEC_WORDS:
        s = s.replace(w, "")
    s = "".join(s.split())  # 去所有空白
    return s


def mark_duplicate_skus(watch):
    """在监控名单内标记同款不同码的 SKU，返回 {spec_no: 同款兄弟编码列表}"""
    from collections import defaultdict
    groups = defaultdict(list)
    for x in watch:
        key = _normalize_name(x["goods_name"])
        if key:  # 归并键为空的不参与
            groups[key].append(x["spec_no"])
    dup_map = {}
    for sn_list in groups.values():
        if len(sn_list) > 1:
            for sn in sn_list:
                dup_map[sn] = sorted(set(sn_list) - {sn})
    return dup_map


# ---------- 拉库存（与 stock_alert 相同的数据源，读共享缓存） ----------
def fetch_stock(client):
    now = datetime.now()
    by_sku = {}
    for wh_no in ("01", "12"):
        start = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        end = now.strftime("%Y-%m-%d %H:%M:%S")
        page = 0
        while True:
            d = client.call("wms.StockSpec.search2",
                            {"warehouse_no": wh_no, "mask": "1",
                             "start_time": start, "end_time": end},
                            page_size=100, page_no=page, calc_total=0)
            items = d.get("data", {}).get("detail_list") or []
            for it in items:
                if it.get("defect") not in (0, False, "0", "false"):
                    continue
                sn = it.get("spec_no", "")
                if not sn:
                    continue
                info = by_sku.setdefault(sn, {
                    "spec_no": sn, "goods_name": it.get("goods_name", ""),
                    "available": 0.0, "num_7days": 0.0, "num_month": 0.0,
                    "purchase_num": 0.0,
                })
                info["available"] += float(it.get("available_send_stock") or 0)
                info["num_7days"] += float(it.get("num_7days") or 0)
                info["num_month"] += float(it.get("num_month") or 0)
                info["purchase_num"] += float(it.get("purchase_num") or 0)
            if len(items) < 100:
                break
            page += 1
            time.sleep(0.3)
    return by_sku


def get_stock(client):
    if STOCK_CACHE.exists():
        try:
            import os
            if time.time() - STOCK_CACHE.stat().st_mtime < 1800:
                return json.loads(STOCK_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    by_sku = fetch_stock(client)
    STOCK_CACHE.write_text(json.dumps(by_sku, ensure_ascii=False), encoding="utf-8")
    return by_sku


# ---------- 名单计算（Top15 ∪ 危险品保底） ----------
def calc_watchlist(stock_map):
    items = []
    for info in stock_map.values():
        daily_30d = info["num_month"] / 30.0
        daily_7d = info["num_7days"] / 7.0
        avail = info["available"]
        was_active = info["num_month"] > 0
        base_daily = daily_7d if daily_7d >= DEAD_STOCK_DAILY else daily_30d
        days_left = (avail / base_daily) if base_daily >= DEAD_STOCK_DAILY else None
        # 状态判定
        if avail < 0:
            status = "断货中"
        elif days_left is not None and days_left <= URGENT_DAYS:
            status = "偏低"
        else:
            status = "正常"
        items.append({
            **info,
            "daily_30d": daily_30d,
            "days_left": days_left,
            "status": status,
            # 危险标记：进保底名单的条件
            "is_danger": (avail < 0 or (days_left is not None and days_left <= URGENT_DAYS)) and was_active,
        })
    # 只统计酒类（覆盖率的分母也只算酒类）
    items_all = [x for x in items]
    items = [x for x in items if is_wine(x["goods_name"])]
    # Top N 按近30天销量
    by_sales = sorted(items, key=lambda x: -x["num_month"])
    top_set = {x["spec_no"] for x in by_sales[:TOP_N]}
    # 保底：有足够销量基础的危险品（断货/偏低）即使排名掉出Top也保留；
    # 门槛=近30天销量≥DANGER_MONTH_SALES，防止边缘滞销品膨胀名单
    for x in items:
        if (x["is_danger"] and x["num_month"] >= DANGER_MONTH_SALES
                and x["spec_no"] not in top_set):
            top_set.add(x["spec_no"])
    watch = [x for x in items if x["spec_no"] in top_set]
    # 展示排序：状态优先（断货>偏低>正常）；状态内按 30天销量↓ → 在途↓ → 库存↑
    order = {"断货中": 0, "偏低": 1, "正常": 2}
    watch.sort(key=lambda x: (order[x["status"]], -x["num_month"],
                              -x["purchase_num"], x["available"]))
    return watch, by_sales


# ---------- 消息构建（两列表格 + 状态标记） ----------
def _q(v):
    return f"{v:.0f}" if v == int(v) else f"{v:.1f}"


def build_message(watch, by_sales):
    now_str = datetime.now().strftime("%m/%d %H:%M")
    total_month = sum(x["num_month"] for x in by_sales)
    watch_month = sum(x["num_month"] for x in watch)
    cover = watch_month / total_month * 100 if total_month else 0

    # 同款不同码标记（标红 + 说明清单）：只对展示行计算，避免隐藏行贡献噪音
    dup_map = mark_duplicate_skus(watch[:MAX_SHOW])

    lines = [f"### 【热卖品监控】{now_str}", ""]
    lines.append(f"**监控 {len(watch)} 个SKU**（Top{TOP_N} + 危险品保底，"
                 f"覆盖近30天销量 {cover:.0f}%）")
    lines.append("")
    lines.append("| 货品名称 | 编码/库存/可售 | 30天销 | 在途 |")
    lines.append("|---|---|---|---|")

    # 只展示最危险的 MAX_SHOW 个（watch 已按 断货>偏低>正常 排序），其余折叠进尾部统计
    shown = watch[:MAX_SHOW]
    hidden = watch[MAX_SHOW:]
    for x in shown:
        # 可售天数：只有 ≤7 天才显示（紧急信号），健康的不标天数避免噪音
        if x["status"] == "断货中":
            stock_str = f"{x['spec_no']} / **{x['available']:.0f}** / **0天**"
        elif x["days_left"] is not None and x["days_left"] <= URGENT_DAYS:
            stock_str = f"{x['spec_no']} / {_q(x['available'])} / **{x['days_left']:.0f}天**"
        else:
            stock_str = f"{x['spec_no']} / {_q(x['available'])} / "
        # 状态标记进名称列
        flag = {"断货中": " ‼️", "偏低": " ⚠️", "正常": ""}[x["status"]]
        # 同款不同码：品名标红（钉钉markdown用font标签，兼容性回退加粗）
        name_str = x["goods_name"]
        if x["spec_no"] in dup_map:
            name_str = f'<font color="warning">{name_str}</font>'
        # 无占位空白：在途/销量一律显示数值，0 就写 0
        onway = f"{x['purchase_num']:.0f}"
        lines.append(f"| {name_str}{flag} | {stock_str} | {_q(x['num_month'])} | {onway} |")

    lines.append("")
    n_out = sum(1 for x in watch if x["status"] == "断货中")
    n_low = sum(1 for x in watch if x["status"] == "偏低")
    hidden_note = f"（另{len(hidden)}个未列出）" if hidden else ""
    # 全文链接：托管页配置后展示"点击查看全部"，否则退回纯文字
    page_url = cfg_page_url()
    if page_url and hidden:
        lines.append(f"> ‼️断货{n_out}个 ⚠️偏低{n_low}个{hidden_note} · "
                     f"[点击查看全部 {len(watch)} 个SKU]({page_url})")
    else:
        lines.append(f"> ‼️断货{n_out}个 ⚠️偏低{n_low}个{hidden_note} · "
                     f"可售天数=库存/动销（近7天优先，断货期回溯30天口径）")

    # 同款说明清单：列出同款不同码的组合，库存/在途可合并看待
    if dup_map:
        lines.append("")
        lines.append("**🔴 同款不同条码**（库存/在途可合并看待，注意别重复采购）:")
        seen_pairs = set()
        for x in watch:
            sn = x["spec_no"]
            if sn not in dup_map:
                continue
            key = tuple(sorted(dup_map[sn] + [sn]))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            lines.append(f"> 🔴 `{sn}` ↔ {'、'.join('`%s`' % s for s in dup_map[sn])}"
                         f"（{x['goods_name']}）")
    return "\n".join(lines)


def cfg_page_url():
    """从 config.json 读取全文托管页地址（hot_items_page_url），未配置返回 None"""
    try:
        cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
        return cfg.get("hot_items_page_url") or None
    except Exception:
        return None


# ---------- 全文HTML（消息只放最危险10个，全文进托管页） ----------
def _html_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_full_html(watch, by_sales, dup_map):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    total_month = sum(x["num_month"] for x in by_sales)
    watch_month = sum(x["num_month"] for x in watch)
    cover = watch_month / total_month * 100 if total_month else 0

    rows = []
    for x in watch:
        if x["status"] == "断货中":
            stock_str = f"<b>{x['available']:.0f} / 0天</b>"
        elif x["days_left"] is not None and x["days_left"] <= URGENT_DAYS:
            stock_str = f"<b>{_q(x['available'])} / {x['days_left']:.0f}天</b>"
        else:
            stock_str = f"{_q(x['available'])} / —"
        name = _html_escape(x["goods_name"])
        if x["spec_no"] in dup_map:
            name = f'<span class="dup">{name}</span>'
        status_cls = {"断货中": "st-out", "偏低": "st-low", "正常": "st-ok"}[x["status"]]
        rows.append(
            f"<tr><td class='{status_cls}'>{x['status']}</td>"
            f"<td>{name}</td><td class='num'>{_html_escape(x['spec_no'])}</td>"
            f"<td class='num'>{stock_str}</td>"
            f"<td class='num'>{_q(x['num_month'])}</td>"
            f"<td class='num'>{x['purchase_num']:.0f}</td></tr>")

    dup_html = ""
    if dup_map:
        items = []
        seen = set()
        for x in watch:
            sn = x["spec_no"]
            if sn not in dup_map:
                continue
            key = tuple(sorted(dup_map[sn] + [sn]))
            if key in seen:
                continue
            seen.add(key)
            items.append(f"<li><code>{_html_escape(sn)}</code> ↔ "
                         + "、".join(f"<code>{_html_escape(s)}</code>" for s in dup_map[sn])
                         + f"（{_html_escape(x['goods_name'])}）</li>")
        dup_html = ("<h2>🔴 同款不同条码</h2><p class='muted'>库存/在途可合并看待，"
                    "注意别重复采购</p><ul class='dup-list'>" + "".join(items) + "</ul>")

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>热卖品监控 · 全部SKU</title><style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#f5f7fa;color:#333;padding:20px}}
.header{{background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;padding:18px 22px;border-radius:8px;margin-bottom:16px}}
.header h1{{font-size:18px;font-weight:600}}.header .sub{{font-size:12px;opacity:.7;margin-top:4px}}
.card{{background:#fff;border-radius:8px;padding:16px 18px;margin-bottom:14px;box-shadow:0 1px 2px rgba(0,0,0,.05)}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #f3f4f6}}
th{{background:#f9fafb;font-weight:600;font-size:12px;color:#374151}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.st-out{{color:#dc2626;font-weight:600}}.st-low{{color:#d97706;font-weight:600}}.st-ok{{color:#059669}}
.dup{{color:#dc2626;font-weight:600}}
code{{font-family:monospace;font-size:12px;background:#f3f4f6;padding:1px 5px;border-radius:4px}}
.dup-list li{{margin:6px 0;font-size:13px}}
h2{{font-size:14px;margin-bottom:8px}}
.muted{{font-size:12px;color:#6b7280;margin-bottom:8px}}
.footer{{text-align:center;color:#9ca3af;font-size:11px;padding:12px}}
</style></head><body>
<div class="header"><h1>热卖品监控 · 全部SKU</h1>
<div class="sub">{now_str} · 监控 {len(watch)} 个SKU（Top{TOP_N} + 危险品保底）· 覆盖近30天酒类销量 {cover:.0f}%</div></div>
<div class="card"><table>
<tr><th>状态</th><th>货品名称</th><th>编码</th><th class="num">库存/可售</th><th class="num">30天销</th><th class="num">在途</th></tr>
{''.join(rows)}</table></div>
{dup_html}
<div class="footer">每日9:00自动更新 · 可售天数=库存/动销（近7天优先，断货期回溯30天口径）</div>
</body></html>"""


def write_full_html(watch, by_sales, dup_map):
    """写全文HTML到 hot-items-page/index.html（原子写），返回路径"""
    HTML_OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HTML_OUT_DIR / "index.html.tmp"
    tmp.write_text(build_full_html(watch, by_sales, dup_map), encoding="utf-8")
    final = HTML_OUT_DIR / "index.html"
    tmp.replace(final)
    return final


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="忽略缓存")
    args = ap.parse_args()

    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    wdt = json.loads((BASE_DIR / "wdt_credentials.json").read_text(encoding="utf-8"))
    dt_client = WdtClient(wdt["sid"], wdt["appkey"], wdt["appsecret"])
    client = DingTalkClient.from_config(cfg["dingtalk"])
    push = resolve_target(cfg["push"])

    if args.fresh and STOCK_CACHE.exists():
        STOCK_CACHE.unlink()

    stock_map = get_stock(dt_client)
    print(f"共 {len(stock_map)} SKU")
    watch, by_sales = calc_watchlist(stock_map)
    n_out = sum(1 for x in watch if x["status"] == "断货中")
    n_low = sum(1 for x in watch if x["status"] == "偏低")
    print(f"监控名单 {len(watch)} 个（断货{n_out} 偏低{n_low} 正常{len(watch)-n_out-n_low}）")

    # 全文HTML：覆盖全部监控名单（含未展示行），同款标记用全量dup_map
    html_path = write_full_html(watch, by_sales, mark_duplicate_skus(watch))
    print(f"全文HTML: {html_path}")

    msg = build_message(watch, by_sales)
    if args.dry:
        print("\n" + msg + "\n")
        return 0

    send_markdown(client, push, "热卖品监控", msg)
    print("已推送热卖品监控"+
          f" -> {push.get('groupName', push.get('webhook', 'unknown'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
