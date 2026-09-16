# -*- coding: utf-8 -*-
"""
餐饮部门&体验部 - 组织与人员同步（薄包装）
拉「餐饮部门&体验部」一张部门快照 → org_input_canyin.json
→ 按总表「项目部」把人员路由到各填写群的 members。
"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import org_sync, iter_groups

CONFIG_FILE = BASE_DIR / "config.json"
DEPARTMENT_KEY = "canyin"
INPUTS = {
    DEPARTMENT_KEY: BASE_DIR / "org_input_canyin.json",
}


def log(msg):
    from datetime import datetime
    LOG_DIR = BASE_DIR / "logs"
    LOG_DIR.mkdir(exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [org] {msg}"
    print(line)
    with open(LOG_DIR / f"reminder_{datetime.now():%Y%m}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _change_name(change):
    """从 '[canyin] 入职/新增: 张三 (u1)' 提取姓名。"""
    return change.rsplit(": ", 1)[-1].split(" ")[0]


def _notify_groups(config, changes):
    """把新增/变更按路由结果通知对应群（离职无法反查群，仅记日志）。"""
    from common.daily_robot import send_group
    for g in iter_groups(config):
        members = g.get("members", {})
        lines = []
        for c in changes:
            body = c.split("] ", 1)[-1]
            name = _change_name(c)
            if "离职" in c or "调出" in c:
                continue
            if name in members:
                lines.append(f"- {body}")
        if not lines:
            continue
        md = [f"### 👥 人员变动同步（{g.get('name')}）", ""] + lines + [
            "", "催报名单已自动更新。新入职同事需在日报表建行并设定目标。"
        ]
        try:
            send_group(config, g.get("robot"), "人员变动同步", "\n".join(md))
        except Exception as e:
            log(f"群通知发送失败[{g.get('name')}]: {e}")


def do_sync():
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    config["logDir"] = str(BASE_DIR / "logs")
    changed, changes = org_sync(config, {k: str(v) for k, v in INPUTS.items()}, DEPARTMENT_KEY)

    if changed:
        CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        log("档案已更新: " + "；".join(changes))
        _notify_groups(config, changes)

    print("SYNCED:" if changes else "NO_CHANGES")
    for c in changes:
        print(" -", c)


def do_status():
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    org = config["org"]
    print(f"上次同步: {org.get('lastSync')}")
    arch = org["archives"].get(DEPARTMENT_KEY, {})
    print(f"\n餐饮部门&体验部 档案 ({len(arch)}人):")
    print("  " + "、".join(sorted(arch.keys())))
    for g in iter_groups(config):
        members = [n for n in g.get("members", {}) if not n.startswith("_")]
        print(f"\n[{g.get('name')}] 催报名单 ({len(members)}人):")
        print("  " + "、".join(sorted(members)))


if __name__ == "__main__":
    if "--sync" in sys.argv:
        do_sync()
    elif "--status" in sys.argv:
        do_status()
    else:
        print(__doc__)
        sys.exit(1)
