#!/usr/bin/env python3
"""
每日播报调度器（渠道日报表群 · 提醒事项机器人）
=================================================
按用户确认的时刻表调度五个播报，本机常驻运行（千问办公定时任务触发入口）：
后续迁 ECS 时改用 crontab/systemd timer，业务脚本不用动。

时刻表（2026-09-03 用户确认）：
  09:00  热卖品监控（最早，Top10 酒类）
  10:00  采购入库提醒（近24h新增，去重键保证不重播）
  11:00  销售达成/渠道日报（留填写时间；周日不播）
  15:00  订单风控提醒（分析昨日订单）
  17:00  库存预警（最晚，下班前催采购）

规则：
  - 周日(weekday==6)跳过渠道日报，其余照常
  - 相邻播报间隔均 ≥1 小时
  - 每个任务独立子进程运行，失败不影响后续任务，结果写 logs/schedule_YYYYMM.log
  - 重复触发同一时段幂等（state 记录当天已执行的任务，手动补跑除外）

用法：
  python daily_scheduler.py            # 常驻模式，每小时检查一次该跑什么
  python daily_scheduler.py --once     # 只检查当前时段该跑的任务（给外部定时器调用）
  python daily_scheduler.py --run 渠道日报   # 手动补跑指定任务
  python daily_scheduler.py --list     # 列出任务表
"""
import argparse
import json
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
STATE_FILE = BASE_DIR / ".schedule_state.json"
PYTHON = sys.executable

# ---------- 任务表：name → (时刻HH:MM, 脚本, 参数, 周日跳过) ----------
TASKS = {
    "热卖品监控": ("09:00", "hot_items_monitor.py", ["--fresh"], False),
    "采购入库":   ("10:00", "purchase_alert.py", ["--hours", "24"], False),
    "渠道日报":   ("11:00", "run_today.py", [], True),   # 周日不播销售进度
    "订单风控":   ("15:00", "order_risk_alert.py", [], False),
    "库存预警":   ("17:00", "stock_alert.py", ["--fresh"], False),
}


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(exist_ok=True)
    with open(LOG_DIR / f"schedule_{datetime.now().strftime('%Y%m')}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def due_tasks(now=None):
    """当前时刻应执行且今天还没执行过的任务（错过窗口补跑：到点没跑过就算到期）"""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    state = load_state()
    due = []
    for name, (hhmm, script, args, skip_sunday) in TASKS.items():
        if skip_sunday and now.weekday() == 6:
            continue  # 周日不播销售进度
        hh, mm = map(int, hhmm.split(":"))
        task_time = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now >= task_time and state.get(f"{today}:{name}") != "ok":
            due.append(name)
    return due


def run_task(name, force=False):
    hhmm, script, args, _ = TASKS[name]
    cmd = [PYTHON, str(BASE_DIR / script)] + args
    log(f"▶ 开始 {name}: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=60 * 30, encoding="utf-8", errors="replace",
                           cwd=str(BASE_DIR))
        out = (r.stdout or "")[-1500:]
        ok = r.returncode == 0
        log(f"{'✔' if ok else '✘'} {name} 退出码={r.returncode}\n{out}")
        if (r.stderr or "").strip():
            log(f"  stderr: {r.stderr[-800:]}")
    except Exception:
        ok = False
        log(f"✘ {name} 异常:\n{traceback.format_exc()[-1000:]}")
    if ok or force:
        state = load_state()
        state[f"{datetime.now().strftime('%Y-%m-%d')}:{name}"] = "ok" if ok else "forced"
        save_state(state)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="检查一次当前到期任务并退出")
    ap.add_argument("--run", help="手动补跑指定任务名")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        print("任务表：")
        for name, (hhmm, script, args_, skip) in TASKS.items():
            sun = "（周日不播）" if skip else ""
            print(f"  {hhmm}  {name:<8} {script} {' '.join(args_)} {sun}")
        return 0

    if args.run:
        if args.run not in TASKS:
            print(f"未知任务: {args.run}，可选: {', '.join(TASKS)}")
            return 1
        return 0 if run_task(args.run, force=True) else 2

    if args.once:
        due = due_tasks()
        if not due:
            log("当前无到期任务")
            return 0
        log(f"到期任务: {due}")
        results = {n: run_task(n) for n in due}
        failed = [n for n, ok in results.items() if not ok]
        return 1 if failed else 0

    # 常驻模式：每 5 分钟检查一次
    import time
    log(f"调度器常驻启动，任务表: {[(t[0], n) for n, t in TASKS.items()]}")
    while True:
        try:
            for name in due_tasks():
                run_task(name)
        except Exception:
            log(f"调度循环异常:\n{traceback.format_exc()[-500:]}")
        time.sleep(300)


if __name__ == "__main__":
    sys.exit(main())
