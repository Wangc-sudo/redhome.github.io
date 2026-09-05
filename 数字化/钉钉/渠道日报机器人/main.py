#!/usr/bin/env python3
"""
渠道日报表群机器人 — 主入口
============================
流程：读配置 → 读AI表格 → 聚合当日渠道数据 → 生成 markdown → 推送到群 → 记日志

用法:
  python main.py                 # 生成并推送昨日（dateOffset=-1）日报
  python main.py --date 2026-09-02
  python main.py --dry           # 只生成不推送，打印到控制台
"""
import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from dingtalk_client import DingTalkClient, DingTalkError
import channel_report as cr

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"


def load_config():
    cfg_path = BASE_DIR / "config.json"
    if not cfg_path.exists():
        print("未找到 config.json，请先复制 config.example.json 为 config.json 并填写")
        sys.exit(1)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    # 环境变量覆盖（ECS 上推荐，避免配置文件落盘明文）
    cfg["dingtalk"]["appSecret"] = os.environ.get(
        "DINGTALK_APP_SECRET", cfg["dingtalk"].get("appSecret", ""))
    return cfg


def check_config(cfg):
    problems = []
    dt = cfg["dingtalk"]
    for k in ("appKey", "appSecret", "operatorId"):
        v = str(dt.get(k, ""))
        if not v or v.startswith("<"):
            problems.append(f"dingtalk.{k} 未填写")
    b = cfg["base"]
    for k in ("baseId", "dailySalesTable"):
        v = str(b.get(k, ""))
        if not v or v.startswith("<"):
            problems.append(f"base.{k} 未填写")
    p = cfg["push"]
    if p.get("mode") == "webhook" and (not p.get("webhook") or str(p.get("webhook")).startswith("<")):
        problems.append("push.webhook 未填写")
    if p.get("mode") == "groupSend":
        for k in ("robotCode", "openConversationId"):
            v = str(p.get(k, ""))
            if not v or v.startswith("<"):
                problems.append(f"push.{k} 未填写")
    return problems


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def write_run_log(target_date, ok, detail):
    LOG_DIR.mkdir(exist_ok=True)
    entry = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "date": target_date,
        "ok": ok,
        "detail": detail[:2000],
    }
    log_file = LOG_DIR / f"run_{datetime.now().strftime('%Y%m')}.jsonl"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def resolve_sheet_id(client, base_id, table_name):
    """按表名解析 sheetId；找不到时给出全部表名提示"""
    sheets = client.list_sheets(base_id)
    for s in sheets:
        if (s.get("name") or s.get("title")) == table_name:
            return s.get("id") or s.get("sheetId") or table_name
    names = [s.get("name") or "?" for s in sheets]
    raise DingTalkError(
        f"未找到表「{table_name}」。Base 现有表：{', '.join(names[:30])}...")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="指定日期 YYYY-MM-DD，默认按 dateOffset")
    ap.add_argument("--dry", action="store_true", help="只生成不推送")
    args = ap.parse_args()

    cfg = load_config()
    problems = check_config(cfg)
    if problems:
        print("配置未完成：")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)

    offset = int(cfg["report"].get("dateOffset", -1))
    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target_date = datetime.now().date() + timedelta(days=offset)
    prev_date = target_date - timedelta(days=1)
    target_str, prev_str = str(target_date), str(prev_date)

    log(f"=== 渠道日报 {target_str} {'[DRY] ' if args.dry else ''}===")

    dt_cfg = cfg["dingtalk"]
    client = DingTalkClient(dt_cfg["appKey"], dt_cfg["appSecret"], dt_cfg["operatorId"])
    base_id = cfg["base"]["baseId"]
    table_name = cfg["base"]["dailySalesTable"]

    try:
        sheet_id = resolve_sheet_id(client, base_id, table_name)
        records = client.list_records(base_id, sheet_id)
        log(f"读取 {len(records)} 条记录（表: {table_name}）")

        field_map = cr.detect_field_map(records, cfg.get("fields", {}))
        log(f"字段映射: {field_map}")
        if not field_map["sales"]:
            raise DingTalkError("未能识别销售额字段，请在 config.json 的 fields.sales 显式指定")

        agg = cr.aggregate(records, field_map, target_str)
        prev_agg = cr.aggregate(records, field_map, prev_str)

        if not agg:
            log("当日无数据")
            if not cfg["report"].get("emptyDataAlsoPush"):
                write_run_log(target_str, True, "无数据，未推送")
                return 0
            markdown = cr.build_empty_markdown(target_str, cfg["report"])
        else:
            markdown, total = cr.build_markdown(target_str, agg, cfg["report"], prev_agg)
            log(f"聚合 {len(agg)} 个渠道，总销售额 {total:,.0f}")

        if args.dry:
            print("\n" + markdown + "\n")
            write_run_log(target_str, True, "DRY RUN")
            return 0

        push = cfg["push"]
        if push["mode"] == "webhook":
            client.send_webhook_markdown(
                push["webhook"],
                title=f"渠道日报 {target_str}",
                markdown_text=markdown,
                secret=push.get("secret", ""),
            )
            log("webhook 推送成功")
        else:
            client.send_group_message(
                push["robotCode"], push["openConversationId"],
                "sampleMarkdown", {"title": f"渠道日报 {target_str}", "text": markdown},
            )
            log("群消息推送成功")

        write_run_log(target_str, True, "ok")
        log("=== 完成 ===")
        return 0

    except DingTalkError as e:
        log(f"失败: {e}")
        write_run_log(target_str, False, str(e))
        return 2
    except Exception:
        detail = traceback.format_exc()
        log(f"异常:\n{detail}")
        write_run_log(target_str, False, detail)
        return 3


if __name__ == "__main__":
    sys.exit(main())
