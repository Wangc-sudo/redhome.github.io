#!/usr/bin/env python3
"""
库存补货提醒（渠道日报表群 · 提醒事项机器人）
=============================================
复刻旧 stock_forecast_alert.py 的预警逻辑，数据源改为 WDT 旗舰版 API 直连：
- 库存+销量: wms.StockSpec.search2（mask=1 返回 num_7days 近7天销量）
  本地仓 01 习水村 + 12 杭易，跨仓合并（跨仓可调拨）
- 预警逻辑（沿用旧参数）:
    动销 = num_7days / 7
    预计可售天数 = 可发库存 / 动销
    ≤7天 且 近7天有动销 → 紧急补货
    可发≤0 且 近7天有动销 → 已超卖
    动销 < 0.3/天 → 滞销（不预警）
- 去重状态机（沿用旧设计）:
    当天每个 SKU 紧急提醒一次；超卖缺口扩大 >5 再提醒
- 排版: 列表式（钉钉表格不支持列宽控制）

用法:
  python stock_alert.py            # 正常运行（含推送）
  python stock_alert.py --dry      # 只看不推
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

STATE_FILE = BASE_DIR / ".stock_alert_state.json"

# ---------- 配置（沿用旧参数） ----------
WAREHOUSE_MAP = {"01": "习水村", "12": "杭易"}
URGENT_DAYS = 7          # 紧急补货阈值（预计售罄天数）
DEAD_STOCK_DAILY = 0.3   # 滞销判定（日均出库低于此值不预警）
OVERSELL_WORSEN_THRESHOLD = 5  # 超卖恶化再提醒阈值
MAX_MSG_LEN = 4000       # 钉钉单条消息上限
STOCK_CACHE = BASE_DIR / ".stock_cache.json"
STOCK_CACHE_TTL = 1800   # 库存缓存30分钟（避免高频调用）


# ---------- 数据拉取 ----------
def load_cache():
    if STOCK_CACHE.exists():
        try:
            if time.time() - STOCK_CACHE.stat().st_mtime < STOCK_CACHE_TTL:
                return json.loads(STOCK_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def fetch_stock(client):
    """拉本地两仓库存（正品），按 SKU 跨仓合并。返回 {spec_no: info}"""
    now = datetime.now()
    by_sku = {}
    for wh_no in WAREHOUSE_MAP:
        start = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        end = now.strftime("%Y-%m-%d %H:%M:%S")
        page, total = 0, None
        while True:
            d = client.call("wms.StockSpec.search2",
                            {"warehouse_no": wh_no, "mask": "1",
                             "start_time": start, "end_time": end},
                            page_size=100, page_no=page, calc_total=1 if total is None else 0)
            data = d.get("data", {})
            total = data.get("total_count", total)
            items = data.get("detail_list") or []
            for it in items:
                if it.get("defect") not in (0, False, "0", "false"):
                    continue  # 只要正品
                sn = it.get("spec_no", "")
                if not sn:
                    continue
                info = by_sku.setdefault(sn, {
                    "spec_no": sn,
                    "goods_name": it.get("goods_name", ""),
                    "available": 0.0, "stock_num": 0.0,
                    "num_7days": 0.0, "num_month": 0.0,
                    "purchase_num": 0.0, "warehouses": {},
                })
                info["available"] += float(it.get("available_send_stock") or 0)
                info["stock_num"] += float(it.get("stock_num") or 0)
                info["num_7days"] += float(it.get("num_7days") or 0)
                info["num_month"] += float(it.get("num_month") or 0)
                info["purchase_num"] += float(it.get("purchase_num") or 0)
                info["warehouses"][WAREHOUSE_MAP[wh_no]] = float(it.get("available_send_stock") or 0)
            if len(items) < 100:
                break
            page += 1
            time.sleep(0.3)
    for info in by_sku.values():
        whs = info["warehouses"]
        info["warehouse"] = "+".join(sorted(whs)) if len(whs) > 1 else (list(whs)[0] if whs else "")
    return by_sku


def get_stock(client):
    cached = load_cache()
    if cached is not None:
        print(f"  库存读缓存（{len(cached)} SKU，{datetime.fromtimestamp(STOCK_CACHE.stat().st_mtime).strftime('%H:%M')}）")
        return cached
    print("  调 WDT API 拉库存...")
    by_sku = fetch_stock(client)
    STOCK_CACHE.write_text(json.dumps(by_sku, ensure_ascii=False), encoding="utf-8")
    return by_sku


# ---------- 预警计算（补丁：超卖用 num_month 兜底，热卖品断货不再静默） ----------
def forecast(stock_map):
    urgent, oversold = [], []
    for info in stock_map.values():
        avail = info["available"]
        daily = info["num_7days"] / 7.0
        daily_30d = info.get("num_month", 0) / 30.0
        is_active = info["num_7days"] > 0          # 近7天有动销
        was_active = info.get("num_month", 0) > 0  # 近30天有动销（断货场景兜底）
        if avail <= 0:
            # 超卖判定：近7天在卖 OR 近30天有销量（热卖品断货8天后7天口径归零，
            # 但30天口径仍在 → 持续提醒，直到断货满30天才因彻底滞销静默）
            if (daily >= DEAD_STOCK_DAILY and is_active) or \
               (daily_30d >= DEAD_STOCK_DAILY and was_active):
                oversold.append(info)
            continue
        if daily < DEAD_STOCK_DAILY and daily_30d < DEAD_STOCK_DAILY:
            continue  # 短期长期都滞销才不预警
        # 动销基准：优先近7天；断货期间7天归零时用30天口径估售罄天数
        base_daily = daily if daily >= DEAD_STOCK_DAILY else daily_30d
        days_left = avail / base_daily
        if days_left <= URGENT_DAYS and (is_active or was_active):
            info = {**info, "days_left": days_left, "daily_avg": base_daily}
            urgent.append(info)
    urgent.sort(key=lambda x: x["days_left"])
    oversold.sort(key=lambda x: -x["available"])
    return urgent, oversold


# ---------- 去重状态机（沿用旧设计） ----------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def filter_alerted_today(urgent, oversold, state):
    """当天每 SKU 紧急提醒一次；超卖缺口扩大超阈值再提醒"""
    today = datetime.now().date().isoformat()
    new_urgent, new_oversold = [], []
    for r in urgent:
        s = state.get(r["spec_no"], {})
        if s.get("urgent_date") == today:
            continue
        new_urgent.append(r)
        state[r["spec_no"]] = {**s, "urgent_date": today}
    for r in oversold:
        s = state.get(r["spec_no"], {})
        if s.get("oversell_date") == today:
            last = s.get("oversell_available")
            if last is None:
                state[r["spec_no"]] = {**s, "oversell_available": r["available"]}
            elif r["available"] < last - OVERSELL_WORSEN_THRESHOLD:
                new_oversold.append(r)
                state[r["spec_no"]] = {**s, "oversell_available": r["available"]}
        else:
            new_oversold.append(r)
            state[r["spec_no"]] = {**s, "oversell_date": today, "oversell_available": r["available"]}
    return new_urgent, new_oversold


# ---------- 消息构建（沿用旧 bot 两列表格格式） ----------
def _q(v):
    return f"{v:.0f}" if v == int(v) else f"{v:.1f}"


def build_messages(urgent, oversold):
    now_str = datetime.now().strftime("%m/%d %H:%M")
    lines = [f"### 【库存补货提醒】{now_str}", ""]

    if urgent:
        lines.append(f"## ⚠️ 预计可售天数≤{URGENT_DAYS}天（{len(urgent)}个）")
        lines.append("")
        lines.append("| 货品名称 | 编码/库存/日销 |")
        lines.append("|---|---|")
        for r in urgent[:30]:
            lines.append(f"| {r['goods_name']} | "
                         f"{r['spec_no']} / **{_q(r['available'])}** / {_q(r['daily_avg'])} |")
        if len(urgent) > 30:
            lines.append(f"| ... | 共{len(urgent)}个已截断 |")
        lines.append("")

    if oversold:
        lines.append(f"**已超卖（{len(oversold)}个）**")
        lines.append("")
        lines.append("| 货品名称 | 编码/超卖数量 |")
        lines.append("|---|---|")
        for r in oversold[:30]:
            lines.append(f"| {r['goods_name']} | "
                         f"{r['spec_no']} / **{_q(max(0, -r['available']))}** |")
        if len(oversold) > 30:
            lines.append(f"| ... | 共{len(oversold)}个已截断 |")
        lines.append("")

    if not urgent and not oversold:
        return []

    lines.append("> 数据来源：旺店通实时库存+近7天动销 · 跨仓（习水村+杭易）合并计算")
    text = "\n".join(lines)
    if len(text) <= MAX_MSG_LEN:
        return [text]
    # 超长拆条
    msgs, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 2 > MAX_MSG_LEN and cur:
            msgs.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        msgs.append(cur)
    return msgs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="忽略缓存重新拉库存")
    args = ap.parse_args()

    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    wdt = json.loads((BASE_DIR / "wdt_credentials.json").read_text(encoding="utf-8"))
    dt_client = WdtClient(wdt["sid"], wdt["appkey"], wdt["appsecret"])
    client = DingTalkClient.from_config(cfg["dingtalk"])
    push = resolve_target(cfg["push"])

    if args.fresh and STOCK_CACHE.exists():
        STOCK_CACHE.unlink()

    stock_map = get_stock(dt_client)
    print(f"  共 {len(stock_map)} 个SKU")

    urgent, oversold = forecast(stock_map)
    print(f"  预警: 紧急补货 {len(urgent)} 个，已超卖 {len(oversold)} 个")

    state = load_state()
    new_urgent, new_oversold = filter_alerted_today(urgent, oversold, state)
    print(f"  去重后待推送: 紧急 {len(new_urgent)}，超卖 {len(new_oversold)}")

    if not new_urgent and not new_oversold:
        print("无新增预警，跳过推送")
        save_state(state)
        return 0

    msgs = build_messages(new_urgent, new_oversold)
    if args.dry:
        for m in msgs:
            print("\n" + m + "\n" + "=" * 50)
        return 0

    for m in msgs:
        send_markdown(client, push, "库存补货提醒", m)
        time.sleep(1)
    save_state(state)
    print(f"已推送库存预警（紧急{len(new_urgent)} 超卖{len(new_oversold)}）"+
          f" -> {push.get('groupName', push.get('webhook', 'unknown'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
