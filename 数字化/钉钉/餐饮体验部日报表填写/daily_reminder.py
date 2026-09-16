# -*- coding: utf-8 -*-
"""
餐饮部门&体验部 日报表填写 - 提醒/催办（薄包装，多群分发）
- 18:30（工作日）：逐群 @ 本群未填写人
- 20:00（工作日）：逐群应用内 DING + 群 @ 知会人
- 三个填写群共用一张总表，按「项目部」过滤，互不串扰
- 非工作日自动跳过；幂等防重发（state key 带群后缀）
"""
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import today_info, fetch_status, do_remind, do_check, iter_groups

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
STATE_FILE = BASE_DIR / ".reminder_state.json"
LOG_DIR = BASE_DIR / "logs"

PLACEHOLDER = "待填"


def log(msg):
    LOG_DIR.mkdir(exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(LOG_DIR / f"reminder_{datetime.now():%Y%m}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _base_ready():
    return PLACEHOLDER not in str(CONFIG.get("base", {}).get("baseId", ""))


def do_status():
    groups = iter_groups(CONFIG)
    print(f"渠道: 餐饮部门&体验部（{len(groups)} 个填写群，共用一张总表）")
    for g in groups:
        projects = g.get("projects") or []
        members = [n for n in g.get("members", {}) if not n.startswith("_")]
        print(f"\n[{g.get('name')}] 项目部: {'、'.join(projects) or '(未配置)'}")
        print(f"  配置名单 ({len(members)}人): {'、'.join(sorted(members)) or '(待组织同步)'}")
        if not _base_ready():
            print("  （base 未配置 <待填>，跳过表查询，仅显示配置名单）")
            continue
        try:
            filled, unfilled, _, _ = fetch_status(CONFIG, projects=projects or None)
            print(f"  今日已填 ({len(filled)}人): {'、'.join(filled) or '(无)'}")
            print(f"  今日未填 ({len(unfilled)}人): {'、'.join(unfilled) or '(无)'}")
        except Exception as e:
            print(f"  表查询失败: {e}")


def main():
    args = sys.argv[1:]
    CONFIG["stateFile"] = str(STATE_FILE)
    CONFIG["logDir"] = str(LOG_DIR)
    CONFIG["baseDir"] = str(BASE_DIR)

    if "--status" in args:
        do_status()
        return

    now, day, err = today_info(CONFIG["calendar"])
    if err:
        log(f"SKIP: {err}")
        return
    if day is None:
        log(f"SKIP: 今天（{now:%Y-%m-%d}）是休息日")
        return

    state = load_state()
    if "--remind" in args:
        mode = "remind"
    elif "--check" in args:
        mode = "check"
    elif "--once" in args:
        mode = "remind" if now.hour < 19 else "check"
    else:
        print(__doc__)
        sys.exit(1)

    for g in iter_groups(CONFIG):
        try:
            if mode == "remind":
                do_remind(CONFIG, state, now, day, group=g)
            else:
                do_check(CONFIG, state, now, day, group=g)
        except Exception:
            log(f"[{g.get('name')}] {mode} 失败:\n{traceback.format_exc()}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("脚本异常退出:\n" + traceback.format_exc())
        raise
