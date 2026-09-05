# -*- coding: utf-8 -*-
"""
组织与人员同步（线下运营中心）
- 数据源：钉钉通讯录（由 cron agent 用 dws 拉取快照写入 org_input_*.json）
- 每晚 00:05 对比档案：
  - 杭州区（催报区）入职 → 自动加入 members 催报名单 + 群通知（提示建表行/定目标）
  - 杭州区离职/调出 → 移出催报名单 + 群通知（表行保留历史）
  - 绍兴区/其他区变动 → 记档案与日志（不发杭州群；绍兴群建立后可路由通知）
- 别名处理：通讯录实名 vs 表内用名（如 陆芳琳→陆芳林）

用法:
  python org_sync.py --sync        # 对比 org_input_*.json 快照并同步（cron 用）
  python org_sync.py --status      # 查看当前档案
"""
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"
INPUTS = {
    "hangzhou": BASE_DIR / "org_input_hangzhou.json",
    "shaoxing": BASE_DIR / "org_input_shaoxing.json",
    "other": BASE_DIR / "org_input_other.json",
}


def log(msg):
    LOG_DIR.mkdir(exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [org] {msg}"
    print(line)
    with open(LOG_DIR / f"reminder_{datetime.now():%Y%m}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_input(path):
    """读取 dws list-members 的输出快照 → {name: userId}"""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    # dws 输出: {"deptUserList":[{"userInfo":{"name","userId"}}]}
    items = data.get("deptUserList") or []
    result = {}
    for it in items:
        info = it.get("userInfo") or {}
        if info.get("name") and info.get("userId"):
            result[info["name"]] = info["userId"]
    return result


def apply_alias(name, aliases):
    return aliases.get(name, name)


def do_sync():
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    org = config["org"]
    aliases = org.get("aliases", {})
    archives = org["archives"]

    snapshots = {}
    for region, path in INPUTS.items():
        snap = load_input(path)
        if snap is None:
            log(f"缺少快照文件 {path.name}，跳过该区域")
            continue
        snapshots[region] = snap

    if not snapshots:
        log("无任何快照输入，退出")
        return

    changes = []          # 摘要行
    hangzhou_member_updates = []  # 需同步到 members 的变动

    for region, snap in snapshots.items():
        arch = archives.get(region, {})
        # 别名归一（档案键 = 通讯录实名；members 键 = 表内用名）
        snap_norm = {}
        for name, uid in snap.items():
            snap_norm[name] = uid

        added = {n: u for n, u in snap_norm.items() if n not in arch}
        removed = {n: u for n, u in arch.items() if n not in snap_norm}
        # 同名换 userId（极少见，如重新入职）
        changed_uid = {n: (arch[n], snap_norm[n]) for n in snap_norm
                       if n in arch and arch[n] != snap_norm[n]}

        if not (added or removed or changed_uid):
            log(f"[{region}] 无变化 ({len(arch)}人)")
            continue

        for n, u in added.items():
            arch[n] = u
            changes.append(f"[{region}] 入职/新增: {n} ({u})")
        for n in removed:
            del arch[n]
            changes.append(f"[{region}] 离职/调出: {n}")
        for n, (old, new) in changed_uid.items():
            arch[n] = new
            changes.append(f"[{region}] ID变更: {n} {old}->{new}")

        if region == "hangzhou":
            for n in added:
                hangzhou_member_updates.append(("add", n))
            for n in removed:
                hangzhou_member_updates.append(("remove", n))
            for n in changed_uid:
                hangzhou_member_updates.append(("update", n))

    if not changes:
        log("全部区域无变化")
        org["lastSync"] = datetime.now().isoformat(timespec="seconds")
        config["org"] = org
        CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        print("NO_CHANGES")
        return

    # 同步杭州催报名单 members（键 = 表内用名）
    members = config["members"]
    for action, real_name in hangzhou_member_updates:
        table_name = apply_alias(real_name, aliases)
        uid = archives["hangzhou"].get(real_name)
        if action in ("add", "update"):
            if table_name in members and members[table_name] == uid:
                continue
            members[table_name] = uid
            log(f"催报名单更新: +{table_name} ({uid})")
        elif action == "remove":
            if table_name in members:
                del members[table_name]
                log(f"催报名单更新: -{table_name}")

    org["lastSync"] = datetime.now().isoformat(timespec="seconds")
    config["org"] = org
    config["members"] = members
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    log("档案已更新: " + "；".join(changes))

    # 杭州变动 → 群通知（其他区域只记日志）
    hz_changes = [c for c in changes if c.startswith("[hangzhou]")]
    if hz_changes:
        try:
            sys.path.insert(0, str(BASE_DIR))
            from hangzhou_reminder import send_group
            lines = ["### 👥 人员变动同步（杭州）", ""]
            for c in hz_changes:
                # 展示名去掉区域前缀和ID
                text = c.replace("[hangzhou] ", "")
                lines.append(f"- {text}")
            lines.append("")
            lines.append("催报名单已自动更新。新入职同事需在日报表建行并设定目标（确认后可由管理员添加）。")
            send_group("人员变动同步", "\n".join(lines))
        except Exception as e:
            log(f"群通知发送失败: {e}")

    print("SYNCED:")
    for c in changes:
        print(" -", c)


def do_status():
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    org = config["org"]
    region_names = {"hangzhou": "杭州（催报区）", "shaoxing": "绍兴", "other": "省外/浙中南/中心直属"}
    print(f"上次同步: {org.get('lastSync')}")
    for region, name in region_names.items():
        arch = org["archives"].get(region, {})
        print(f"\n{name} ({len(arch)}人):")
        print("  " + "、".join(sorted(arch.keys())))
    print("\n催报名单 members (%d人):" % len(config["members"]))
    print("  " + "、".join(sorted(config["members"].keys())))


def main():
    if "--sync" in sys.argv:
        do_sync()
    elif "--status" in sys.argv:
        do_status()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
