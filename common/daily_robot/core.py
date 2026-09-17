# -*- coding: utf-8 -*-
"""
日报机器人公共核心：提醒、催办、组织同步、合计重算、数据体检、群发送。
 region 相关常量全部来自 CONFIG["region"]。
"""
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.dingtalk import DingTalkClient, send_markdown
from common.dingtalk.test_group import resolve_target

SKIP_PATTERNS = ("合计",)

_LOG_FMT = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_LOGGER = logging.getLogger("daily_robot")
if not _LOGGER.handlers:
    _sh = logging.StreamHandler()
    _sh.setFormatter(_LOG_FMT)
    _LOGGER.addHandler(_sh)
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False


def _is_skip_name(name):
    return any(p in str(name) for p in SKIP_PATTERNS)


def log(log_dir, msg):
    """统一走 logging；文件 handler 即用即关，避免 Windows 下句柄占用。"""
    log_dir = Path(log_dir)
    log_dir.mkdir(exist_ok=True)
    fh = logging.FileHandler(log_dir / f"reminder_{datetime.now():%Y%m}.log", encoding="utf-8")
    fh.setFormatter(_LOG_FMT)
    _LOGGER.addHandler(fh)
    try:
        _LOGGER.info(msg)
    finally:
        _LOGGER.removeHandler(fh)
        fh.close()


def _resolve_group(config, group) -> dict[str, Any]:
    """group 为 None 时把顶层单群字段包装成隐式 group（杭州/绍兴零回归）。"""
    if group is not None:
        return group
    region = config.get("region", {})
    return {
        "key": region.get("name", "default"),
        "name": region.get("displayName", region.get("name", "")),
        "projects": None,
        "robot": config.get("robot", {}),
        "members": config.get("members", {}),
        "ccUsers": config.get("ccUsers", {}),
        "broadcastExclude": region.get("broadcastExclude", []),
        "totalPrefixes": region.get("totalPrefixes", []),
    }


def iter_groups(config):
    """统一产出 group 列表：有 groups 走多群，否则包装顶层单群字段。"""
    groups = config.get("groups")
    if groups:
        return list(groups)
    return [_resolve_group(config, None)]


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


def fetch_status(config, projects=None):
    """读取当日填写状态。projects 给定时仅统计「项目部 ∈ projects」的责任人行。"""
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
        if not name or _is_skip_name(name):
            skipped.append(name or "(空行)")
            continue
        if projects is not None and str(fvals.get("项目部") or "").strip() not in projects:
            continue
        name = str(name).strip()
        v = fvals.get(col_name)
        if v is None or (isinstance(v, str) and v.strip() == ""):
            unfilled.append(name)
        else:
            filled.append(name)
    return filled, unfilled, len(records), skipped


def send_group(config, robot_target=None, title=None, text=None, at_ids=None):
    """发送群消息。robot_target 为具体群的 robot 配置（group["robot"]）。

    兼容旧调用 send_group(config, title, text, at_ids=...)（杭州/绍兴）。
    TEST_MODE=1 且 config 含 verify 段时，一律重定向 verify 群；
    否则沿用 resolve_target（test_groups.json）机制。
    """
    if text is None and title is not None and isinstance(robot_target, str):
        robot_target, title, text = None, robot_target, title
    robot_cfg = dict(robot_target or config.get("robot") or {})
    if os.environ.get("TEST_MODE") == "1" and config.get("verify"):
        verify = config["verify"]
        robot_cfg["mode"] = "groupSend"
        robot_cfg["openConversationId"] = verify["openConversationId"]
        robot_cfg["groupName"] = verify.get("groupName", "功能验证群")
        robot_cfg.pop("webhook", None)
        robot_cfg.pop("secret", None)
        robot = robot_cfg
    else:
        robot = resolve_target(robot_cfg)
    client = DingTalkClient.from_config(config["dingtalk"])
    r = send_markdown(client, robot, title, text, at_user_ids=at_ids)
    log(Path(config.get("logDir", Path(__file__).parent / "logs")),
        f"已发群消息: {title} (at={at_ids}) -> {robot.get('groupName', 'unknown')}")
    return r


def do_remind(config, state, now, day, group=None):
    group = _resolve_group(config, group)
    log_dir = Path(config.get("logDir", Path(__file__).parent / "logs"))
    key = f"remind_{now:%Y%m%d}"
    if config.get("groups"):
        key = f"{key}_{group.get('key') or group.get('name')}"
    if state.get(key):
        log(log_dir, "今日提醒已发过，跳过")
        return

    filled, unfilled, _, _ = fetch_status(config, projects=group.get("projects"))
    log(log_dir, f"[{group.get('name')}] 填写状态: 已填{len(filled)} 未填{len(unfilled)}")
    if not unfilled:
        log(log_dir, "全员已填写，不发提醒")
        state[key] = datetime.now().isoformat()
        save_state(config["stateFile"], state)
        return

    members = group.get("members", {})
    at_ids, missing = [], []
    for name in unfilled:
        uid = members.get(name)
        if uid:
            at_ids.append(uid)
        else:
            missing.append(name)

    weekday = "一二三四五六日"[now.weekday()]
    url = config["base"]["tableUrl"]
    display = group.get("name") or config.get("region", {}).get("displayName", "")
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
    send_group(config, group.get("robot"), "销售日报填写提醒", "\n".join(lines), at_ids=at_ids)
    state[key] = datetime.now().isoformat()
    save_state(config["stateFile"], state)


def do_check(config, state, now, day, group=None):
    group = _resolve_group(config, group)
    log_dir = Path(config.get("logDir", Path(__file__).parent / "logs"))
    key = f"check_{now:%Y%m%d}"
    if config.get("groups"):
        key = f"{key}_{group.get('key') or group.get('name')}"
    if state.get(key):
        log(log_dir, "今日检查已发过，跳过")
        print("NO_ACTION: 今日检查已发过")
        return

    filled, unfilled, _, _ = fetch_status(config, projects=group.get("projects"))
    log(log_dir, f"[{group.get('name')}] 填写状态: 已填{len(filled)} 未填{len(unfilled)}")
    if not unfilled:
        log(log_dir, "全员已填写，不发催办")
        state[key] = datetime.now().isoformat()
        save_state(config["stateFile"], state)
        print("NO_ACTION: 全员已填写")
        return

    members = group.get("members", {})
    cc = group.get("ccUsers", {})
    ding_ids = [members[n] for n in unfilled if members.get(n)]
    missing = [n for n in unfilled if not members.get(n)]
    weekday = "一二三四五六日"[now.weekday()]
    url = config["base"]["tableUrl"]
    names_text = "、".join(unfilled)
    at_ids = list(ding_ids)
    cc_shen = cc.get("沈聪")
    if cc_shen:
        at_ids.append(cc_shen)
    display = group.get("name") or config.get("region", {}).get("displayName", "")

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
    send_group(config, group.get("robot"), "销售日报未填写", "\n".join(lines), at_ids=at_ids)
    state[key] = datetime.now().isoformat()
    save_state(config["stateFile"], state)

    if ding_ids:
        content = (f"【销售日报催办】{display} {now.month}月{day}日（周{weekday}）：你还未填写今日销售日报，"
                   f"请在群里 @提醒事项 报数或填写表格 {url}")
        cmd = (f'dws ding message send --robot-code {group["robot"]["robotCode"]} '
               f'--users {",".join(ding_ids)} --content "{content}" --type app --format json')
        print("DING_CMD_START")
        print(cmd)
        print("DING_CMD_END")
        log(log_dir, f"待执行DING: {names_text}")


def _load_input(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
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

    if active_region not in snapshots:
        log(log_dir, f"警告：未找到 active_region={active_region} 的快照，成员列表不会更新")

    changes = []
    active_updates = []

    for region, snap in snapshots.items():
        archives.setdefault(region, {})
        arch = archives[region]
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

    if config.get("groups"):
        _route_members_to_groups(config, active_updates, active_region, aliases, log_dir)
    else:
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
        config["members"] = members

    org["lastSync"] = datetime.now().isoformat(timespec="seconds")
    config["org"] = org
    return True, changes


def _fetch_name_projects(config):
    """读总表，返回 {责任人: 项目部} 映射（用于多群路由）。"""
    client = DingTalkClient.from_config(config["dingtalk"])
    base = config["base"]
    records = client.list_records(base["baseId"], base["tableId"])
    name2dept = {}
    for rec in records:
        fvals = rec.get("fields") or {}
        name = fvals.get("责任人")
        if not name or _is_skip_name(name):
            continue
        name2dept[str(name).strip()] = str(fvals.get("项目部") or "").strip()
    return name2dept


def _route_members_to_groups(config, active_updates, active_region, aliases, log_dir):
    """多群模式：按表内「项目部」把人员路由到 projects 匹配的群并更新各群 members。"""
    archives = config["org"]["archives"]
    groups = config["groups"]
    try:
        name2dept = _fetch_name_projects(config)
    except Exception as e:
        log(log_dir, f"读取总表项目部失败，跳过 members 路由（档案已更新）: {e}")
        return

    for action, real_name in active_updates:
        table_name = _apply_alias(real_name, aliases)
        uid = archives[active_region].get(real_name)
        if action in ("add", "update"):
            dept = name2dept.get(table_name)
            target = next((g for g in groups if dept and dept in g.get("projects", [])), None)
            if target is None:
                log(log_dir, f"路由失败: {table_name} 项目部={dept or '(表中无此行)'} 无匹配群，跳过")
                continue
            members = target.setdefault("members", {})
            if members.get(table_name) == uid:
                continue
            members[table_name] = uid
            log(log_dir, f"催报名单更新[{target.get('name')}]: +{table_name} ({uid})")
        elif action == "remove":
            for g in groups:
                members = g.get("members", {})
                if table_name in members:
                    del members[table_name]
                    log(log_dir, f"催报名单更新[{g.get('name')}]: -{table_name}")


def _parse_num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def recalc_totals(config, group=None):
    """重算达成率与合计行。group 给定时仅处理 group.projects 行与 group.totalPrefixes 合计行。"""
    client = DingTalkClient.from_config(config["dingtalk"])
    base = config["base"]
    calendar = config["calendar"]
    region = config.get("region", {})
    month = calendar["month"]
    workdays = [d for d in range(1, 31) if d not in calendar.get("restDays", [])]
    day_cols = [f"{d}日" for d in workdays]
    target_col = f"{month}月销量目标（万）"
    if group is not None:
        total_prefixes = group.get("totalPrefixes") or group.get("projects", [])
        all_dept_names = group.get("projects", total_prefixes)
        projects = set(all_dept_names)
        all_display = group.get("name", "总")
    else:
        total_prefixes = region.get("totalPrefixes", [])
        all_dept_names = region.get("allDeptNames", total_prefixes)
        projects = None
        all_display = region.get("allDisplayName", region.get("displayName", "总"))

    records = client.list_records(base["baseId"], base["tableId"])
    updates = []
    n_fixed = 0

    members_by_dept = {}
    for rec in records:
        f = rec.get("fields") or {}
        name = str(f.get("责任人") or "")
        if not name or _is_skip_name(name):
            continue
        dept = str(f.get("项目部") or "?")
        if projects is not None and dept not in projects:
            continue
        members_by_dept.setdefault(dept, []).append(f)

        day_sum = sum(_parse_num(f.get(c)) or 0 for c in day_cols)
        target = _parse_num(f.get(target_col))
        new_fields = {}
        if target:
            new_rate = day_sum / target
            old_rate = _parse_num(f.get("达成率"))
            if old_rate is None or abs(old_rate - new_rate) > 0.0005:
                new_fields["达成率"] = repr(new_rate)
        if new_fields:
            updates.append({"id": rec["id"], "fields": new_fields})
            n_fixed += 1

    total_rows = {}
    for rec in records:
        f = rec.get("fields") or {}
        name = str(f.get("责任人") or "")
        if not name or not _is_skip_name(name):
            continue
        dept = str(f.get("项目部") or "")
        for prefix in total_prefixes:
            if prefix in dept or prefix in name:
                total_rows[prefix] = (rec["id"], f)
                break
        else:
            if group is None and (all_display in name or all_display in dept):
                total_rows["ALL"] = (rec["id"], f)

    def fix_total_row(rid, tf, members):
        nonlocal n_fixed
        new_fields = {}
        for c in day_cols:
            s = sum(_parse_num(mf.get(c)) or 0 for mf in members)
            old = _parse_num(tf.get(c))
            if old is None or abs(old - s) > 0.5:
                new_fields[c] = str(int(s)) if s == int(s) else str(s)
        total_all = sum(_parse_num(mf.get(c)) or 0 for mf in members for c in day_cols)
        target_sum = sum(_parse_num(mf.get(target_col)) or 0 for mf in members)
        if target_sum:
            new_rate = total_all / target_sum
            old_r = _parse_num(tf.get("达成率"))
            if old_r is None or abs(old_r - new_rate) > 0.0005:
                new_fields["达成率"] = repr(new_rate)
        if new_fields:
            updates.append({"id": rid, "fields": new_fields})
            n_fixed += 1

    for prefix in total_prefixes:
        if prefix in total_rows and members_by_dept.get(prefix):
            rid, tf = total_rows[prefix]
            fix_total_row(rid, tf, members_by_dept[prefix])

    if "ALL" in total_rows:
        all_members = []
        for d in all_dept_names:
            all_members.extend(members_by_dept.get(d, []))
        rid, tf = total_rows["ALL"]
        fix_total_row(rid, tf, all_members)

    if updates:
        for i in range(0, len(updates), 100):
            client.update_records(base["baseId"], base["tableId"], updates[i:i + 100])
    print(f"RECALC_DONE: 修正 {n_fixed} 行" if n_fixed else "RECALC_DONE: 全部一致无需修正")
    return n_fixed


def check_data(config, projects=None):
    """数据体检。projects 给定时仅检查「项目部 ∈ projects」的责任人行。"""
    client = DingTalkClient.from_config(config["dingtalk"])
    base = config["base"]
    calendar = config["calendar"]
    month = calendar["month"]
    workdays = [d for d in range(1, 31) if d not in calendar.get("restDays", [])]
    target_col = f"{month}月销量目标（万）"
    records = client.list_records(base["baseId"], base["tableId"])
    problems = []
    today = datetime.now().day

    for rec in records:
        f = rec.get("fields") or {}
        name = f.get("责任人")
        if not name:
            problems.append(f"[空责任人行] id={rec.get('id')} keys={list(f.keys())[:5]}")
            continue
        if _is_skip_name(name):
            continue
        if projects is not None and str(f.get("项目部") or "").strip() not in projects:
            continue

        for rest in calendar.get("restDays", []):
            if f"{rest}日" in f:
                problems.append(f"[{name}] 残留休息日列 {rest}日 = {f[f'{rest}日']}")

        vals = {}
        for d in workdays:
            v = f.get(f"{d}日")
            if v is not None and str(v).strip() != "":
                try:
                    vals[d] = float(str(v).replace(",", ""))
                except ValueError:
                    problems.append(f"[{name}] {d}日 非数值: {v!r}")

        suspicious_future = {d: v for d, v in vals.items() if d > today and v > 0}
        if suspicious_future:
            problems.append(f"[{name}] 未来日期已有大额数据: {suspicious_future}")

        day_sum = sum(vals.values())
        try:
            rate = float(str(f.get("达成率")))
            target = float(str(f.get(target_col)).replace(",", ""))
            if target > 0 and abs(rate - day_sum / target) > 0.005:
                problems.append(f"[{name}] 达成率({rate:.4f}) 与累计/目标({day_sum/target:.4f}) 不符")
        except (TypeError, ValueError):
            pass

        print(f"{name}: 已填{len(vals)}列 累计={int(day_sum)}")

    print()
    print("=" * 60)
    if problems:
        print(f"发现 {len(problems)} 个问题:")
        for p in problems:
            print(" -", p)
    else:
        print("数据体检通过，无问题")
    return problems
