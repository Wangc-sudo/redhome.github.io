# -*- coding: utf-8 -*-
"""
绍兴销售日报提醒机器人
- 18:30（工作日）：@ 未填写人
- 20:00（工作日）：应用内 DING + 群 @ 沈聪
- 支持责任人 @提醒事项 报数（由 shaoxing_listener.py 常驻监听）
"""
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import today_info, fetch_status, do_remind, do_check

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
STATE_FILE = BASE_DIR / ".reminder_state.json"
LOG_DIR = BASE_DIR / "logs"


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


def main():
    args = sys.argv[1:]
    now, day, err = today_info(CONFIG["calendar"])
    if err:
        log(f"SKIP: {err}")
        return
    if day is None:
        log(f"SKIP: 今天（{now:%Y-%m-%d}）是休息日")
        return

    state = load_state()
    CONFIG["stateFile"] = str(STATE_FILE)
    CONFIG["logDir"] = str(LOG_DIR)
    CONFIG["baseDir"] = str(BASE_DIR)

    if "--status" in args:
        filled, unfilled, total, skipped = fetch_status(CONFIG)
        print(f"{now.month}月{day}日 填写状态: 已填 {len(filled)} 人 / 未填 {len(unfilled)} 人")
        print("已填:", "、".join(filled) or "(无)")
        print("未填:", "、".join(unfilled) or "(无)")
        return

    if "--remind" in args:
        do_remind(CONFIG, state, now, day)
    elif "--check" in args:
        do_check(CONFIG, state, now, day)
    elif "--once" in args:
        if now.hour < 19:
            do_remind(CONFIG, state, now, day)
        else:
            do_check(CONFIG, state, now, day)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
