# -*- coding: utf-8 -*-
"""
日报机器人公共核心：提醒、催办、组织同步、合计重算、数据体检、群发送。
 region 相关常量全部来自 CONFIG["region"]。
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.dingtalk import DingTalkClient, DingTalkError, send_markdown
from common.test_group import resolve_target


def log(log_dir, msg):
    log_dir = Path(log_dir)
    log_dir.mkdir(exist_ok=True)
    now = datetime.now()
    line = f"{now.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(log_dir / f"reminder_{now:%Y%m}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state(state_file):
    state_file = Path(state_file)
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"状态文件解析失败: {state_file}: {e}") from e
    return {}


def save_state(state_file, state):
    Path(state_file).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def today_info(calendar, now=None):
    if now is None:
        now = datetime.now()
    if now.month != calendar["month"]:
        return now, None, f"当前月份 {now.month} 与配置月份 {calendar['month']} 不符"
    day = now.day
    if day in calendar.get("restDays", []):
        return now, None, None
    return now, day, None


def fetch_status(config):
    base = config["base"]
    now = datetime.now()
    day = now.day
    col_name = f"{day}日"
    client = DingTalkClient.from_config(config["dingtalk"])

    fields = client.list_fields(base["baseId"], base["tableId"])
    if not any(f.get("name") == col_name for f in fields):
        raise RuntimeError(f"表中找不到字段「{col_name}」")

    records = client.list_records(base["baseId"], base["tableId"])
    filled, unfilled, skipped = [], [], []
    for rec in records:
        fvals = rec.get("fields") or {}
        name = fvals.get("责任人")
        if not name or "合计" in str(name):
            skipped.append(name or "(空行)")
            continue
        name = str(name).strip()
        v = fvals.get(col_name)
        if v is None or (isinstance(v, str) and v.strip() == ""):
            unfilled.append(name)
        else:
            filled.append(name)
    return filled, unfilled, len(records), skipped


def send_group(config, title, text, at_ids=None):
    robot = resolve_target(config["robot"])
    client = DingTalkClient.from_config(config["dingtalk"])
    r = send_markdown(client, robot, title, text, at_user_ids=at_ids)
    log(Path(config.get("logDir", Path(__file__).parent / "logs")),
        f"已发群消息: {title} (at={at_ids}) -> {robot.get('groupName', 'unknown')}")
    return r


def do_remind(config, state, now, day):
    log_dir = Path(config.get("logDir", Path(__file__).parent / "logs"))
    key = f"remind_{now:%Y%m%d}"
    if state.get(key):
        log(log_dir, "今日提醒已发过，跳过")
        return

    filled, unfilled, _, _ = fetch_status(config)
    log(log_dir, f"填写状态: 已填{len(filled)} 未填{len(unfilled)}")
    if not unfilled:
        log(log_dir, "全员已填写，不发提醒")
        state[key] = datetime.now().isoformat()
        save_state(config["stateFile"], state)
        return

    members = config["members"]
    at_ids, missing = [], []
    for name in unfilled:
        uid = members.get(name)
        if uid:
            at_ids.append(uid)
        else:
            missing.append(name)

    weekday = "一二三四五六日"[now.weekday()]
    url = config["base"]["tableUrl"]
    display = config["region"].get("displayName", config["region"]["name"])
    lines = [f"### 📋 销售日报填写提醒（{display} {now.month}月{day}日 周{weekday}）", ""]
    lines.append(f"以下 **{len(unfilled)}** 位同事还未填写今日销售日报，请尽快填写：")
    lines.append("")
    lines.append(f"**{'、'.join(unfilled)}**")
    lines.append("")
    lines.append("也可直接在群里 **@提醒事项 + 数字** 报数（如 `@提醒事项 12800`，报 0 也行）")
    lines.append("")
    lines.append(f"[点此填写]({url})")
    if missing:
        lines.append("")
        lines.append(f"（{'、'.join(missing)} 未在通讯录映射中，无法@，请手动提醒）")
    send_group(config, "销售日报填写提醒", "\n".join(lines), at_ids=at_ids)
    state[key] = datetime.now().isoformat()
    save_state(config["stateFile"], state)


def do_check(config, state, now, day):
    log_dir = Path(config.get("logDir", Path(__file__).parent / "logs"))
    key = f"check_{now:%Y%m%d}"
    if state.get(key):
        log(log_dir, "今日检查已发过，跳过")
        print("NO_ACTION: 今日检查已发过")
        return

    filled, unfilled, _, _ = fetch_status(config)
    log(log_dir, f"填写状态: 已填{len(filled)} 未填{len(unfilled)}")
    if not unfilled:
        log(log_dir, "全员已填写，不发催办")
        state[key] = datetime.now().isoformat()
        save_state(config["stateFile"], state)
        print("NO_ACTION: 全员已填写")
        return

    members = config["members"]
    cc = config.get("ccUsers", {})
    ding_ids = [members[n] for n in unfilled if members.get(n)]
    missing = [n for n in unfilled if not members.get(n)]
    weekday = "一二三四五六日"[now.weekday()]
    url = config["base"]["tableUrl"]
    names_text = "、".join(unfilled)
    at_ids = list(ding_ids)
    cc_shen = cc.get("沈聪")
    if cc_shen:
        at_ids.append(cc_shen)
    display = config["region"].get("displayName", config["region"]["name"])

    lines = [f"### ⏰ 销售日报未填写（{display} {now.month}月{day}日）", ""]
    lines.append(f"截至 20:00，以下 **{len(unfilled)}** 位同事仍未填写：")
    lines.append("")
    lines.append(f"**{names_text}**")
    lines.append("")
    lines.append("已同步 DING 提醒以上人员，请在群里 @提醒事项 报数或直接填写。")
    lines.append(f"[点此填写]({url})")
    if missing:
        lines.append("")
        lines.append(f"（{'、'.join(missing)} 未在通讯录映射中，无法@，请手动提醒）")
    send_group(config, "销售日报未填写", "\n".join(lines), at_ids=at_ids)
    state[key] = datetime.now().isoformat()
    save_state(config["stateFile"], state)

    if ding_ids:
        content = (f"【销售日报催办】{display} {now.month}月{day}日（周{weekday}）：你还未填写今日销售日报，"
                   f"请在群里 @提醒事项 报数或填写表格 {url}")
        cmd = (f'dws ding message send --robot-code {config["robot"]["robotCode"]} '
               f'--users {",".join(ding_ids)} --content "{content}" --type app --format json')
        print("DING_CMD_START")
        print(cmd)
        print("DING_CMD_END")
        log(log_dir, f"待执行DING: {names_text}")


def _load_input(path):
    path = Path(path)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("deptUserList") or []
    result = {}
    for it in items:
        info = it.get("userInfo") or {}
        if info.get("name") and info.get("userId"):
            result[info["name"]] = info["userId"]
    return result


def _apply_alias(name, aliases):
    return aliases.get(name, name)


def org_sync(config, inputs, active_region):
    log_dir = Path(config.get("logDir", Path(__file__).parent / "logs"))
    org = config["org"]
    aliases = org.get("aliases", {})
    archives = org["archives"]

    snapshots = {}
    for region, path in inputs.items():
        snap = _load_input(path)
        if snap is None:
            log(log_dir, f"缺少快照文件 {Path(path).name}，跳过该区域")
            continue
        snapshots[region] = snap

    if not snapshots:
        log(log_dir, "无任何快照输入，退出")
        return False, []

    changes = []
    active_updates = []

    for region, snap in snapshots.items():
        arch = archives.get(region, {})
        added = {n: u for n, u in snap.items() if n not in arch}
        removed = {n: u for n, u in arch.items() if n not in snap}
        changed_uid = {n: (arch[n], snap[n]) for n in snap if n in arch and arch[n] != snap[n]}

        if not (added or removed or changed_uid):
            log(log_dir, f"[{region}] 无变化 ({len(arch)}人)")
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

        if region == active_region:
            for n in added:
                active_updates.append(("add", n))
            for n in removed:
                active_updates.append(("remove", n))
            for n in changed_uid:
                active_updates.append(("update", n))

    if not changes:
        log(log_dir, "全部区域无变化")
        org["lastSync"] = datetime.now().isoformat(timespec="seconds")
        config["org"] = org
        return False, []

    members = config["members"]
    for action, real_name in active_updates:
        table_name = _apply_alias(real_name, aliases)
        uid = archives[active_region].get(real_name)
        if action in ("add", "update"):
            if members.get(table_name) == uid:
                continue
            members[table_name] = uid
            log(log_dir, f"催报名单更新: +{table_name} ({uid})")
        elif action == "remove":
            if table_name in members:
                del members[table_name]
                log(log_dir, f"催报名单更新: -{table_name}")

    org["lastSync"] = datetime.now().isoformat(timespec="seconds")
    config["org"] = org
    config["members"] = members
    return True, changes


def recalc_totals(config):
    raise NotImplementedError


def check_data(config):
    raise NotImplementedError
