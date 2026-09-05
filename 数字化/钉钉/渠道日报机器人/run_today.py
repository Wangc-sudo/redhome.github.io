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
import json
import re
import sys
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.dingtalk import DingTalkClient, send_markdown
from common.test_group import resolve_target

TZ_CN = timezone(timedelta(hours=8))  # 钉钉 date 字段为 CST 午夜毫秒时间戳

# ---------- 配置 ----------
BASE_ID = "QOG9lyrgJPmxNQlwIw4QDd6q8zN67Mw4"
CHANNELS = ["天猫", "京东", "拼多多", "猫超", "即时零售", "直播", "私域"]
MONTH = 9  # 跨月时改这里（或改为按日期自动算）


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
    today, prev_agg, mtd = {}, {}, {}
    for ch in CHANNELS:
        tname = f"{ch}渠道数据{MONTH}"
        if tname not in sheets:
            print(f"WARN: 未找到表「{tname}」，跳过该渠道")
            continue
        recs = dt.records(sheets[tname])
        today[ch] = agg_day(recs, day)
        prev_agg[ch] = agg_day(recs, prev)
        mtd[ch] = agg_mtd(recs, day)
        print(f"{ch}: 当日{today[ch]['sales']:,.0f} / 环比基期{prev_agg[ch]['sales']:,.0f} / MTD{mtd[ch]:,.0f}")

    tname = f"渠道销售目标达成率{MONTH}"
    targets = agg_targets(dt.records(sheets[tname])) if tname in sheets else {}
    print(f"月目标: { {k: f'{v:,.0f}' for k, v in targets.items()} }")

    md = build_md(day, today, prev_agg, mtd, targets)
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
