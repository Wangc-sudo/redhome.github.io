# -*- coding: utf-8 -*-
"""绍兴 - 组织与人员同步（薄包装）"""
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import org_sync

CONFIG_FILE = BASE_DIR / "config.json"
INPUTS = {"shaoxing": str(BASE_DIR / "org_input_shaoxing.json")}


def log(msg):
    LOG_DIR = BASE_DIR / "logs"
    LOG_DIR.mkdir(exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [org] {msg}"
    print(line)
    with open(LOG_DIR / f"reminder_{datetime.now():%Y%m}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def do_sync():
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    config["logDir"] = str(BASE_DIR / "logs")
    changed, changes = org_sync(config, INPUTS, "shaoxing")

    if changed:
        CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        log("档案已更新: " + "；".join(changes))

        try:
            from common.daily_robot import send_group
            lines = ["### 👥 人员变动同步（绍兴）", ""]
            for c in changes:
                lines.append(f"- {c.replace('[shaoxing] ', '')}")
            lines.append("")
            lines.append("催报名单已自动更新。新入职同事需在日报表建行并设定目标。")
            send_group(config, "人员变动同步", "\n".join(lines))
        except Exception as e:
            log(f"群通知发送失败: {e}")

    print("SYNCED:" if changes else "NO_CHANGES")
    for c in changes:
        print(" -", c)


def do_status():
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    org = config["org"]
    print(f"上次同步: {org.get('lastSync')}")
    arch = org["archives"].get("shaoxing", {})
    print(f"\n绍兴 ({len(arch)}人):")
    print("  " + "、".join(sorted(arch.keys())))
    print("\n催报名单 members (%d人):" % len(config["members"]))
    print("  " + "、".join(sorted(config["members"].keys())))


if __name__ == "__main__":
    if "--sync" in sys.argv:
        do_sync()
    elif "--status" in sys.argv:
        do_status()
    else:
        print(__doc__)
        sys.exit(1)
