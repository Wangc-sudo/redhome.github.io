#!/usr/bin/env python3
"""
订单风险防控提醒（渠道日报表群 · 提醒事项机器人）
=================================================
复刻旧 order_risk_detect.py 判定逻辑：同店铺+同地区+同日 ≥3 单 → 风险告警。
数据源从桌面 Excel 改为 WDT 旗舰版 API 直查（sales.TradeQuery.queryWithDetail）。

⚠️ 平台覆盖限制（2026-09-03 实测）：
  ✅ 抖音/京东/快手/视频号 —— receiver_area 正常返回
  ❌ 拼多多 —— WDT API 按隐私协议不返回地区字段（旧流程靠桌面 Excel 导出才有）
  如需恢复 PDD 风控，需：WDT 客户端机器保留 Excel 导出流程，或接入拼多多开放平台。

状态口径（旗舰版）：在途单 30待客审/27待分配预订单/55已审核/16延时审核 + 95已发货 + 110已完成
（旧版仅查在途；API 场景下在途单很少且多为已完成态，故放宽到 95/110 以保证检出量，取消单5排除）

用法:
  python order_risk_alert.py            # 分析昨天（按付款时间）
  python order_risk_alert.py --today    # 分析今天
  python order_risk_alert.py --dry      # 只看不推
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from wdt_client import WdtClient

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / ".order_risk_state.json"

RISK_THRESHOLD = 3          # 同店铺同地区同日最低订单数
MAX_MSG_LEN = 4000
TARGET_STATUSES = "95,110,30,27,55,16"  # 排除已取消5

# 平台映射（店铺名前缀 → 展示平台，沿用旧口径）
def platform_of(shop_name):
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


def pull_orders(client, day):
    """按付款时间拉一天订单（60分钟切片），返回 order 列表"""
    orders = []
    for hour in range(24):
        start = f"{day} {hour:02d}:00:00"
        end = f"{day} {hour:02d}:59:59"
        page = 0
        while True:
            d = client.call("sales.TradeQuery.queryWithDetail",
                            {"start_time": start, "end_time": end,
                             "time_type": "2", "status": TARGET_STATUSES},
                            page_size=100, page_no=page, calc_total=0)
            batch = d.get("data", {}).get("order", [])
            orders.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        time.sleep(0.3)
    return orders


def normalize_area(raw):
    """'广东省 深圳市 龙华区 民治街道' → '广东省深圳市龙华区'（省市区三级；去街道与连续重复）"""
    if not raw:
        return None
    parts = [p for p in re.split(r"\s+", str(raw).strip()) if p]
    # 去连续重复（东莞市东莞市 / 北京北京市）
    dedup = [parts[0]] if parts else []
    for p in parts[1:]:
        if p != dedup[-1]:
            dedup.append(p)
    if len(dedup) < 2:
        return None  # 省市区不全不算有效地区
    return "".join(dedup[:3])


def detect_groups(orders, day):
    """店铺+地区 聚合 ≥阈值 → {店铺: [(地区, 数量), ...]}"""
    counter = {}
    for o in orders:
        area = normalize_area(o.get("receiver_area"))
        if not area:
            continue  # 无地区的（PDD）跳过
        shop = o.get("shop_name") or "?"
        counter[(shop, area)] = counter.get((shop, area), 0) + 1
    groups = {}
    for (shop, area), n in counter.items():
        if n >= RISK_THRESHOLD:
            groups.setdefault(shop, []).append((area, n))
    for shop in groups:
        groups[shop].sort(key=lambda x: -x[1])
    return groups


def build_messages(groups, day, skipped_pdd):
    lines = [f"# 订单风险防控提醒（{day}）", ""]
    if skipped_pdd:
        lines.append(f"> ⚠️ 拼多多订单地区字段受WDT隐私协议限制，本次未纳入检测"
                     f"（{skipped_pdd}单）")
        lines.append("")
    for shop, areas in sorted(groups.items(), key=lambda kv: -sum(n for _, n in kv[1])):
        plat = platform_of(shop)
        shop_short = shop.split("-", 1)[-1] if "-" in shop else shop
        lines.append(f"**{plat}-{shop_short}**")
        lines.append("")
        lines.append("| 重点地区 | 异常单数 |")
        lines.append("|---------|---------|")
        for area, n in areas[:15]:
            lines.append(f"| {area} | {n}单 |")
        if len(areas) > 15:
            lines.append(f"| ... | 共{len(areas)}个地区 |")
        lines.append("")
    if not groups:
        return []
    lines.append(f"> 同店铺同地区同日 ≥{RISK_THRESHOLD}单判定为风险 · 数据源：旺店通订单（抖音/京东/快手/视频号）")
    text = "\n".join(lines)
    if len(text) <= MAX_MSG_LEN:
        return [text]
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


def send_webhook(webhook, title, text):
    import urllib.request
    body = json.dumps({"msgtype": "markdown",
                       "markdown": {"title": title, "text": text}}).encode()
    req = urllib.request.Request(webhook, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        r = json.loads(resp.read().decode())
    if r.get("errcode") != 0:
        raise RuntimeError(f"webhook 失败: {r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--days-ago", type=int, default=1)
    args = ap.parse_args()

    if args.today:
        day = datetime.now().strftime("%Y-%m-%d")
        end_hour = datetime.now().hour  # 今天只拉到当前小时
    else:
        day = (datetime.now() - timedelta(days=args.days_ago)).strftime("%Y-%m-%d")
        end_hour = 24

    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    wdt = json.loads((BASE_DIR / "wdt_credentials.json").read_text(encoding="utf-8"))
    client = WdtClient(wdt["sid"], wdt["appkey"], wdt["appsecret"])

    print(f"拉取 {day} 订单（0-{end_hour}点，60分钟切片）...")
    orders = []
    skipped_pdd = 0
    for hour in range(end_hour):
        start = f"{day} {hour:02d}:00:00"
        end = f"{day} {hour:02d}:59:59"
        page = 0
        while True:
            d = client.call("sales.TradeQuery.queryWithDetail",
                            {"start_time": start, "end_time": end,
                             "time_type": "2", "status": TARGET_STATUSES},
                            page_size=100, page_no=page, calc_total=0)
            batch = d.get("data", {}).get("order", [])
            orders.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        time.sleep(0.3)
        if hour % 6 == 5:
            print(f"  ... 已拉 {hour + 1} 小时，累计 {len(orders)} 单")

    skipped_pdd = sum(1 for o in orders if "拼多多" in (o.get("shop_name") or ""))
    print(f"共 {len(orders)} 单（其中拼多多 {skipped_pdd} 单无地区，跳过）")

    groups = detect_groups(orders, day)
    total_risk = sum(len(v) for v in groups.values())
    print(f"检测到 {total_risk} 个高风险组合，涉及 {len(groups)} 个店铺")

    if not groups:
        print("无风险，跳过推送")
        return 0

    msgs = build_messages(groups, day, skipped_pdd)
    if args.dry:
        for m in msgs:
            print("\n" + m + "\n" + "=" * 50)
        return 0

    for m in msgs:
        send_webhook(cfg["push"]["webhook"], f"订单风险防控提醒 {day}", m)
        time.sleep(1)
    print(f"已推送订单风控提醒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
