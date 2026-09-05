# -*- coding: utf-8 -*-
"""
杭州销售完成率榜单 - 数据推送脚本（定时任务用，跑在有钉钉凭据的机器上）
==========================================================================
从钉钉 AI 表格拉取日报数据 → 组装榜单快照 JSON → POST 到榜单服务后端。

后端收到后入库，前端页面自动展示最新数据——页面无需重新发布。

用法:
  python push_leaderboard.py [--url http://SERVER:8300] [--include-today]

环境变量:
  LEADERBOARD_URL    后端地址（--url 优先）
  LEADERBOARD_TOKEN  推送令牌（与后端 LEADERBOARD_TOKEN 一致）

退出码:
  0 成功（含"无新数据跳过"）  2 网络/接口失败  3 口径/月份错误
"""
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
_REPO_ROOT = BASE_DIR.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common.dingtalk import DingTalkClient
from common.calendar_utils import CalendarError

sys.path.insert(0, str(BASE_DIR))
from leaderboard_report import (CAL, MONTH, REST, WORKDAYS, TARGET_COL, DEPT_ORDER,
                                DEPT_LABEL, BC_EXCLUDE, parse_num, collect)

URL = os.environ.get("LEADERBOARD_URL", "")
TOKEN = os.environ.get("LEADERBOARD_TOKEN", "")


def build_payload(include_today=False):
    """复用 leaderboard_report.collect() 拉取数据，组装为榜单服务契约 JSON"""
    now, elapsed, people = collect(include_today)
    n_elapsed, n_total = len(elapsed), len(WORKDAYS)
    progress = n_elapsed / n_total
    total_c = sum(p["completed"] for p in people)
    total_t = sum(p["target"] for p in people)

    # 部门聚合（与播报口径一致：排除 BC_EXCLUDE，仅三项目部）
    bc_people = [p for p in people if p["name"] not in BC_EXCLUDE]
    depts = {}
    for p in bc_people:
        depts.setdefault(p["dept"], []).append(p)
    dept_stats = []
    for dname in DEPT_ORDER + [d for d in depts if d not in DEPT_ORDER]:
        members = depts.get(dname)
        if not members:
            continue
        dc = sum(m["completed"] for m in members)
        dt = sum(m["target"] for m in members)
        dept_stats.append({
            "name": dname, "label": DEPT_LABEL.get(dname, dname), "count": len(members),
            "completed": dc, "target": dt, "rate": dc / dt if dt else 0,
            "unfilled_total": sum(m["unfilled"] for m in members),
        })
    dept_stats.sort(key=lambda x: -x["rate"])

    return {
        "year": now.year,
        "month": MONTH,
        "stat_date": f"{MONTH}月{elapsed[-1]}日" if elapsed else "—",
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "stat_through": f"{MONTH}月{elapsed[-1]}日" if elapsed else "—",
        "progress": {"elapsed": n_elapsed, "total": n_total, "pct": progress},
        "overall": {"completed": total_c, "target": total_t,
                    "rate": total_c / total_t if total_t else 0},
        "people": [{
            "name": p["name"], "dept": p["dept"],
            "dept_label": DEPT_LABEL.get(p["dept"], p["dept"]),
            "target": p["target"], "completed": p["completed"],
            "rate": p["rate"], "unfilled": p["unfilled"],
        } for p in people],
        "depts": dept_stats,
    }


def push(url, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + "/api/update", data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def main():
    include_today = "--include-today" in sys.argv
    if "--url" in sys.argv:
        global URL
        URL = sys.argv[sys.argv.index("--url") + 1]

    if not URL:
        print("ERROR: 未配置后端地址（--url 或环境变量 LEADERBOARD_URL）")
        sys.exit(2)
    if not TOKEN:
        print("ERROR: 未配置推送令牌（环境变量 LEADERBOARD_TOKEN）")
        sys.exit(2)

    try:
        payload = build_payload(include_today)
    except CalendarError as e:
        print(f"SKIP: {e}")
        sys.exit(3)

    try:
        r = push(URL, payload)
    except Exception as e:
        print(f"ERROR: 推送失败 {URL}: {e}")
        sys.exit(2)

    ok = r.get("ok")
    print(f"{'OK' if ok else 'WARN'}: 已推送 {payload['stat_date']} 快照 "
          f"({len(payload['people'])}人, {payload['progress']['elapsed']}/"
          f"{payload['progress']['total']} 工作日) → {URL}")
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
