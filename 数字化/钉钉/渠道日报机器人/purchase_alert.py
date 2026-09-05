#!/usr/bin/env python3
"""
采购入库提醒（渠道日报表群 · 提醒事项机器人）
=============================================
复刻旧 sync_purchase_hourly.py 的群消息格式：
  **【采购入库提醒】** HH:MM
  🏭 仓库
  | 货品名称 | 编码 / 数量 |
  > 共 N 条明细，M 张采购单，合计 X 件

数据源: 旺店通旗舰版 wms.stockin.Purchase.queryWithDetail（已验证连通）
去重: 入库单号|商家编码（状态文件 .purchase_state.json）
推送: 「提醒事项」机器人 webhook（config.json）

用法:
  python purchase_alert.py              # 拉最近1小时（定时任务用）
  python purchase_alert.py --hours 8    # 拉最近8小时
  python purchase_alert.py --dry        # 只看不推
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.dingtalk import DingTalkClient, send_markdown
from common.test_group import resolve_target
from wdt_client import WdtClient

STATE_FILE = BASE_DIR / ".purchase_state.json"
STATE_KEEP = 5000  # 状态文件最多保留条数

WAREHOUSE_MAP = {"01": "习水村", "12": "杭易"}
STATUS_DONE = 80  # 已完成入库


def load_config():
    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
    return cfg


def load_state():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_state(keys):
    recent = sorted(keys)[-STATE_KEEP:]
    STATE_FILE.write_text(json.dumps(recent, ensure_ascii=False), encoding="utf-8")


def pull_purchase(client, hours):
    end = datetime.now()
    start = end - timedelta(hours=hours)
    rows = []
    # 时间跨度大时分片拉（单次窗口≤30天，这里几小时无压力；分页在 client 内处理）
    orders = client.call_paged(
        "wms.stockin.Purchase.queryWithDetail",
        {"start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
         "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
         "status": str(STATUS_DONE)},
        page_size=40)
    for o in orders:
        wh = WAREHOUSE_MAP.get(str(o.get("warehouse_no")), str(o.get("warehouse_no")))
        for det in o.get("details_list", []):
            rows.append({
                "key": f"{o.get('order_no')}|{det.get('spec_no')}",
                "warehouse": wh,
                "goods_name": det.get("spec_name") or det.get("goods_name") or "",
                "spec_no": det.get("spec_no") or "",
                "num": float(det.get("num") or 0),
                "order_no": o.get("order_no"),
            })
    return rows


def build_markdown(new_rows):
    """列表式排版：同SKU合并数量，按数量降序（钉钉表格不支持列宽控制，列表更清晰）"""
    if not new_rows:
        return None
    lines = [f"**【采购入库提醒】**  {datetime.now().strftime('%H:%M')}", ""]

    by_wh = {}
    for r in new_rows:
        g = by_wh.setdefault(r["warehouse"], {})
        item = g.setdefault(r["spec_no"], {"name": r["goods_name"], "num": 0, "orders": set()})
        item["num"] += r["num"]
        item["orders"].add(r["order_no"])

    total_num = sum(r["num"] for r in new_rows)
    total_orders = len({r["order_no"] for r in new_rows})
    sku_count = sum(len(g) for g in by_wh.values())

    for wh, goods in by_wh.items():
        wh_orders = set()
        for it in goods.values():
            wh_orders |= it["orders"]
        lines.append(f"**🏭 {wh}**（{len(wh_orders)}张单）")
        lines.append("")
        # 数量大的排前面，扫一眼先看重点
        for spec_no, it in sorted(goods.items(), key=lambda kv: -kv[1]["num"]):
            lines.append(f"- {spec_no}　**×{int(it['num'])}**　{it['name']}")
        lines.append("")

    lines.append(f"> 共 {len(new_rows)} 条明细（{sku_count} 个SKU），"
                 f"{total_orders} 张采购单，合计 {int(total_num)} 件")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=1, help="回溯小时数，默认1")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    wdt_cfg = json.loads((BASE_DIR / "wdt_credentials.json").read_text(encoding="utf-8"))
    dt_client = WdtClient(wdt_cfg["sid"], wdt_cfg["appkey"], wdt_cfg["appsecret"])
    client = DingTalkClient.from_config(cfg["dingtalk"])
    push = resolve_target(cfg["push"])

    rows = pull_purchase(dt_client, args.hours)
    state = load_state()
    new_rows = [r for r in rows if r["key"] not in state]
    print(f"拉取 {len(rows)} 条明细，新提醒 {len(new_rows)} 条")

    md = build_markdown(new_rows)
    if md is None:
        print("无新增采购入库，跳过推送")
        return 0

    if args.dry:
        print("\n" + md + "\n")
        return 0

    send_markdown(client, push, "采购入库提醒", md)
    state.update(r["key"] for r in new_rows)
    save_state(state)
    print(f"已推送 {len(new_rows)} 条新增采购入库提醒"+
          f" -> {push.get('groupName', push.get('webhook', 'unknown'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
