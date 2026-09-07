# 绍兴日报机器人 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 复用杭州日报机器人能力，通过新建 `common/daily_robot/` 公共核心包，为「线下绍兴日报群」建设独立配置、独立表格、独立人员的日报机器人，并同步完成杭州脚本的薄包装改造。

**Architecture:** 把杭州脚本里硬编码的区域常量（部门顺序、排除名单、合计行前缀等）抽到 `config.json` 的 `region` 段；将业务逻辑下沉到 `common/daily_robot/` 的三个模块（core/leaderboard/listener）；杭州/绍兴目录只保留读配置 + 调用公共模块的薄包装脚本。

**Tech Stack:** Python 3.12、钉钉 OpenAPI、dingtalk-stream SDK、unittest/pytest、common.dingtalk / common.test_group。

---

## File Structure

| 路径 | 职责 |
|------|------|
| `common/daily_robot/__init__.py` | 包入口，导出公共函数 |
| `common/daily_robot/core.py` | 提醒、催办、组织同步、合计重算、数据体检、群发送 |
| `common/daily_robot/leaderboard.py` | 榜单数据汇总与 Markdown/HTML 生成 |
| `common/daily_robot/listener.py` | Stream 报数监听 Handler |
| `tests/common/test_daily_robot.py` | 公共核心模块单元测试 |
| `数字化/钉钉/杭州日报机器人/hangzhou_reminder.py` | 改造为薄包装 |
| `数字化/钉钉/杭州日报机器人/hangzhou_listener.py` | 改造为薄包装 |
| `数字化/钉钉/杭州日报机器人/org_sync.py` | 改造为薄包装 |
| `数字化/钉钉/杭州日报机器人/recalc_totals.py` | 改造为薄包装 |
| `数字化/钉钉/杭州日报机器人/check_data.py` | 改造为薄包装 |
| `数字化/钉钉/杭州日报机器人/leaderboard_report.py` | 改造为薄包装 |
| `数字化/钉钉/杭州日报机器人/config.example.json` | 增加 `region` 段 |
| `数字化/钉钉/shaoxing_daily_robot/` | 新建目录 |
| `数字化/钉钉/shaoxing_daily_robot/shaoxing_reminder.py` | 薄包装 |
| `数字化/钉钉/shaoxing_daily_robot/shaoxing_listener.py` | 薄包装 |
| `数字化/钉钉/shaoxing_daily_robot/org_sync.py` | 薄包装 |
| `数字化/钉钉/shaoxing_daily_robot/recalc_totals.py` | 薄包装 |
| `数字化/钉钉/shaoxing_daily_robot/check_data.py` | 薄包装 |
| `数字化/钉钉/shaoxing_daily_robot/leaderboard_report.py` | 薄包装 |
| `数字化/钉钉/shaoxing_daily_robot/config.example.json` | 绍兴配置模板 |
| `数字化/钉钉/shaoxing_daily_robot/README.md` | 绍兴机器人说明 |
| `数字化/钉钉/shaoxing_daily_robot/register_autostart.bat` | 开机自启脚本 |
| `数字化/钉钉/shaoxing_daily_robot/org_input_shaoxing.json` | 绍兴通讯录快照 |
| `docs/命令速查表.md` | 补充绍兴命令 |

---

## Task 1: Bootstrap `common/daily_robot` package

**Files:**
- Create: `common/daily_robot/__init__.py`
- Create: `common/daily_robot/core.py`
- Create: `common/daily_robot/leaderboard.py`
- Create: `common/daily_robot/listener.py`

- [ ] **Step 1: Create package `__init__.py`**

Create `common/daily_robot/__init__.py`:

```python
# -*- coding: utf-8 -*-
from .core import (
    today_info,
    fetch_status,
    send_group,
    do_remind,
    do_check,
    org_sync,
    recalc_totals,
    check_data,
)
from .leaderboard import collect, build_bc_markdown, build_html
from .listener import ReportHandler

__all__ = [
    "today_info", "fetch_status", "send_group", "do_remind", "do_check",
    "org_sync", "recalc_totals", "check_data",
    "collect", "build_bc_markdown", "build_html",
    "ReportHandler",
]
```

- [ ] **Step 2: Commit bootstrap**

```bash
git add common/daily_robot/__init__.py
git commit -m "chore: bootstrap common/daily_robot package"
```

---

## Task 2: Extract `common/daily_robot/core.py` - basic helpers

**Files:**
- Create: `common/daily_robot/core.py`
- Modify: `common/daily_robot/__init__.py` (already exports)

- [ ] **Step 1: Write `core.py` basic helpers**

Create `common/daily_robot/core.py` with the first half (imports, log, today_info, fetch_status, send_group):

```python
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
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(log_dir / f"reminder_{datetime.now():%Y%m}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state(state_file):
    state_file = Path(state_file)
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state_file, state):
    Path(state_file).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def today_info(calendar):
    now = datetime.now()
    if now.month != calendar["month"]:
        return now, None, f"当前月份 {now.month} 与配置月份 {calendar['month']} 不符"
    day = now.day
    if day in calendar.get("restDays", []):
        return now, None, None
    return now, day, None


def fetch_status(config):
    base = config["base"]
    calendar = config["calendar"]
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
```

- [ ] **Step 2: Run compileall on new file**

```bash
python -m compileall common/daily_robot/core.py
```

Expected: Compiled OK.

- [ ] **Step 3: Commit**

```bash
git add common/daily_robot/core.py
python -m compileall common/daily_robot/core.py
git commit -m "feat(daily_robot): extract basic helpers (today_info, fetch_status, send_group)"
```

---

## Task 3: Extract `do_remind` / `do_check`

**Files:**
- Modify: `common/daily_robot/core.py`

- [ ] **Step 1: Append reminder/check functions**

Append to `common/daily_robot/core.py`:

```python

def do_remind(config, state, now, day):
    key = f"remind_{now:%Y%m%d}"
    if state.get(key):
        log(Path(config.get("logDir", Path(__file__).parent / "logs")),
            "今日提醒已发过，跳过")
        return

    filled, unfilled, total, skipped = fetch_status(config)
    log(Path(config.get("logDir", Path(__file__).parent / "logs")),
        f"填写状态: 已填{len(filled)} 未填{len(unfilled)}")
    if not unfilled:
        log(Path(config.get("logDir", Path(__file__).parent / "logs")), "全员已填写，不发提醒")
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
    key = f"check_{now:%Y%m%d}"
    if state.get(key):
        log(Path(config.get("logDir", Path(__file__).parent / "logs")),
            "今日检查已发过，跳过")
        print("NO_ACTION: 今日检查已发过")
        return

    filled, unfilled, total, skipped = fetch_status(config)
    log(Path(config.get("logDir", Path(__file__).parent / "logs")),
        f"填写状态: 已填{len(filled)} 未填{len(unfilled)}")
    if not unfilled:
        log(Path(config.get("logDir", Path(__file__).parent / "logs")), "全员已填写，不发催办")
        state[key] = datetime.now().isoformat()
        save_state(config["stateFile"], state)
        print("NO_ACTION: 全员已填写")
        return

    members = config["members"]
    cc = config["ccUsers"]
    ding_ids = [members[n] for n in unfilled if members.get(n)]
    missing = [n for n in unfilled if not members.get(n)]
    weekday = "一二三四五六日"[now.weekday()]
    url = config["base"]["tableUrl"]
    names_text = "、".join(unfilled)
    at_ids = list(ding_ids) + [cc["沈聪"]]
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
        log(Path(config.get("logDir", Path(__file__).parent / "logs")),
            f"待执行DING: {names_text}")
```

- [ ] **Step 2: Run compileall**

```bash
python -m compileall common/daily_robot/core.py
```

Expected: Compiled OK.

- [ ] **Step 3: Commit**

```bash
git add common/daily_robot/core.py
git commit -m "feat(daily_robot): add do_remind and do_check"
```

---

## Task 4: Extract `org_sync`

**Files:**
- Modify: `common/daily_robot/core.py`

- [ ] **Step 1: Append org_sync helpers**

Append to `common/daily_robot/core.py`:

```python

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
```

- [ ] **Step 2: Run compileall**

```bash
python -m compileall common/daily_robot/core.py
```

Expected: Compiled OK.

- [ ] **Step 3: Commit**

```bash
git add common/daily_robot/core.py
git commit -m "feat(daily_robot): add org_sync"
```

---

## Task 5: Extract `recalc_totals`

**Files:**
- Modify: `common/daily_robot/core.py`

- [ ] **Step 1: Append recalc_totals**

Append to `common/daily_robot/core.py`:

```python

def _parse_num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def recalc_totals(config):
    client = DingTalkClient.from_config(config["dingtalk"])
    base = config["base"]
    calendar = config["calendar"]
    region = config["region"]
    month = calendar["month"]
    workdays = [d for d in range(1, 31) if d not in calendar.get("restDays", [])]
    day_cols = [f"{d}日" for d in workdays]
    target_col = f"{month}月销量目标（万）"
    total_prefixes = region.get("totalPrefixes", [])
    all_dept_names = region.get("allDeptNames", total_prefixes)

    records = client.list_records(base["baseId"], base["tableId"])
    updates = []
    n_fixed = 0

    members_by_dept = {}
    for rec in records:
        f = rec.get("fields") or {}
        name = str(f.get("责任人") or "")
        if not name or "合计" in name:
            continue
        dept = str(f.get("项目部") or "?")
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
        if "合计" not in name or not name:
            continue
        dept = str(f.get("项目部") or "")
        for prefix in total_prefixes:
            if prefix in dept or prefix in name:
                total_rows[prefix] = (rec["id"], f)
                break
        else:
            all_name = region.get("allDisplayName", region.get("displayName", "总"))
            if all_name in name or all_name in dept:
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
```

- [ ] **Step 2: Run compileall and commit**

```bash
python -m compileall common/daily_robot/core.py
git add common/daily_robot/core.py
git commit -m "feat(daily_robot): add recalc_totals"
```

---

## Task 6: Extract `check_data`

**Files:**
- Modify: `common/daily_robot/core.py`

- [ ] **Step 1: Append check_data**

Append to `common/daily_robot/core.py`:

```python

def check_data(config):
    client = DingTalkClient.from_config(config["dingtalk"])
    base = config["base"]
    calendar = config["calendar"]
    month = calendar["month"]
    workdays = [d for d in range(1, 31) if d not in calendar.get("restDays", [])]
    target_col = f"{month}月销量目标（万）"
    records = client.list_records(base["baseId"], base["tableId"])
    problems = []

    for rec in records:
        f = rec.get("fields") or {}
        name = f.get("责任人")
        if not name:
            problems.append(f"[空责任人行] id={rec.get('id')} keys={list(f.keys())[:5]}")
            continue
        if "合计" in str(name):
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

        today = datetime.now().day
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
```

- [ ] **Step 2: Run compileall and commit**

```bash
python -m compileall common/daily_robot/core.py
git add common/daily_robot/core.py
git commit -m "feat(daily_robot): add check_data"
```

---

## Task 7: Extract `common/daily_robot/leaderboard.py`

**Files:**
- Create: `common/daily_robot/leaderboard.py`

- [ ] **Step 1: Write leaderboard.py**

Create `common/daily_robot/leaderboard.py` (adapting from Hangzhou's leaderboard_report.py, using config["region"] constants):

```python
# -*- coding: utf-8 -*-
import html as html_mod
import json
import re
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.dingtalk import DingTalkClient


def _parse_num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _fmt_wan(v):
    v = float(v)
    if v >= 100000000:
        return f"{v/100000000:.2f}亿"
    if v >= 10000:
        return f"{v/10000:.1f}万"
    return f"{v:,.0f}"


def _fmt_pct(v, digits=1):
    return f"{v*100:.{digits}f}%"


def collect(config, include_today=False):
    now = datetime.now()
    calendar = config["calendar"]
    month = calendar["month"]
    if now.month != month:
        raise SystemExit(f"当前月份{now.month}与配置月份{month}不符")
    workdays = [d for d in range(1, 31) if d not in calendar.get("restDays", [])]
    today = now.day
    elapsed = [d for d in workdays if d < today or (include_today and d == today)]

    client = DingTalkClient.from_config(config["dingtalk"])
    base = config["base"]
    records = client.list_records(base["baseId"], base["tableId"])
    target_col = f"{month}月销量目标（万）"

    people = []
    for rec in records:
        f = rec.get("fields") or {}
        name = str(f.get("责任人") or "").strip()
        if not name or "合计" in name:
            continue
        dept = str(f.get("项目部") or "").strip() or "未分组"
        target = _parse_num(f.get(target_col)) or 0
        completed = 0.0
        unfilled = 0
        for d in elapsed:
            v = f.get(f"{d}日")
            if v is None or str(v).strip() == "":
                unfilled += 1
            else:
                completed += float(str(v).replace(",", "") or 0)
        people.append({
            "name": name, "dept": dept, "target": target, "completed": completed,
            "unfilled": unfilled,
            "rate": completed / target if target > 0 else None,
        })

    people.sort(key=lambda p: (
        -(p["rate"] if p["rate"] is not None else -1),
        -p["completed"],
        -p["target"],
    ))
    return now, elapsed, people


def build_bc_markdown(config, url=None):
    region = config["region"]
    calendar = config["calendar"]
    now, elapsed, people = collect(config, include_today=False)
    n_elapsed = len(elapsed)
    workdays = [d for d in range(1, 31) if d not in calendar.get("restDays", [])]
    n_total = len(workdays)
    progress = n_elapsed / n_total if n_total else 0

    bc_people = [p for p in people if p["name"] not in region.get("broadcastExclude", [])]
    depts = {}
    for p in bc_people:
        depts.setdefault(p["dept"], []).append(p)
    stats = []
    for dname in region.get("deptOrder", []) + [d for d in depts if d not in region.get("deptOrder", [])]:
        members = depts.get(dname)
        if not members:
            continue
        dc = sum(m["completed"] for m in members)
        dt = sum(m["target"] for m in members)
        stats.append({
            "name": dname, "rate": dc / dt if dt else 0,
            "completed": dc, "target": dt, "count": len(members),
            "unfilled": sum(m["unfilled"] for m in members),
        })
    stats.sort(key=lambda x: -x["rate"])

    total_c = sum(p["completed"] for p in bc_people)
    total_t = sum(p["target"] for p in bc_people)
    overall = total_c / total_t if total_t else 0
    weekday = "一二三四五六日"[now.weekday()]
    stat_thru = f"{now.month}月{elapsed[-1]}日" if elapsed else "—"
    display = region.get("displayName", region["name"])

    lines = [f"### 📊 {display}销售完成率榜（{stat_thru} 周{weekday}）", ""]
    lines.append(f"时间进度 **{_fmt_pct(progress)}**（{n_elapsed}/{n_total} 工作日） · "
                 f"整体完成率 **{_fmt_pct(overall)}**（{_fmt_pct(overall - progress, 1)} vs 进度）")
    lines.append("")
    lines.append("| 排名 | 部门 | 完成金额 / 目标 | 完成率 | 进度差 / 预计月末 |")
    lines.append("|:--:|:--|:--|--:|:--|")
    for i, st in enumerate(stats):
        lb = region.get("deptLabel", {}).get(st["name"], st["name"])
        diff = st["rate"] - progress
        diff_txt = f"+{_fmt_pct(diff)}" if diff >= 0 else _fmt_pct(diff)
        proj = st["rate"] / progress if progress > 0 else 0
        proj_txt = f"{proj*100:.0f}%" if proj <= 9.99 else "999%+"
        lines.append(f"| {i+1} | {lb} | {_fmt_wan(st['completed'])} / {_fmt_wan(st['target'])} | "
                     f"**{_fmt_pct(st['rate'])}** | {diff_txt} / {proj_txt} |")
    if url:
        lines.append("")
        lines.append(f"📊 [点击查看完整榜单（个人明细）]({url})")
    return "\n".join(lines)
```

- [ ] **Step 2: Run compileall and commit**

```bash
python -m compileall common/daily_robot/leaderboard.py
git add common/daily_robot/leaderboard.py
git commit -m "feat(daily_robot): add leaderboard broadcast markdown generator"
```

---

## Task 8: Extract `common/daily_robot/listener.py`

**Files:**
- Create: `common/daily_robot/listener.py`

- [ ] **Step 1: Write listener.py**

Create `common/daily_robot/listener.py`:

```python
# -*- coding: utf-8 -*-
import json
import re
import sys
import time as _t
from datetime import datetime
from pathlib import Path

import dingtalk_stream
from dingtalk_stream import AckMessage

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.dingtalk import DingTalkClient


class ReportHandler(dingtalk_stream.ChatbotHandler):
    def __init__(self, config, log_fn=None):
        super().__init__()
        self.config = config
        self.log = log_fn or print
        self._name2rid = None
        self._uid2name = {}
        self._uid2name_ts = 0.0
        self._uid2name_ttl = 300
        self.base_dir = Path(config.get("baseDir", "."))

    def _day_column(self):
        now = datetime.now()
        cal = self.config["calendar"]
        if now.month != cal["month"]:
            return None, f"当前月份 {now.month} 与配置月份 {cal['month']} 不符"
        day = now.day
        if day in cal.get("restDays", []):
            return None, None
        return f"{day}日", day

    def _refresh_uid2name(self):
        now = _t.time()
        if now - self._uid2name_ts > self._uid2name_ttl or not self._uid2name:
            try:
                cfg = json.loads((self.base_dir / "config.json").read_text(encoding="utf-8"))
                self._uid2name = {uid_: name for name, uid_ in cfg["members"].items()
                                  if not name.startswith("_")}
                self._uid2name_ts = now
                self.log(f"白名单已重载: {len(self._uid2name)} 人")
            except Exception as e:
                self.log(f"白名单重载失败（用旧名单）: {e}")

    def _refresh_map(self, force=False):
        if self._name2rid is None or force:
            client = DingTalkClient.from_config(self.config["dingtalk"])
            records = client.list_records(self.config["base"]["baseId"], self.config["base"]["tableId"])
            m = {}
            for rec in records:
                fvals = rec.get("fields") or {}
                name = fvals.get("责任人")
                if name and "合计" not in str(name):
                    m[str(name).strip()] = rec["id"]
            self._name2rid = m
            self.log(f"记录映射已加载: {len(m)} 人")

    def _reply(self, incoming, text):
        self.reply_text(text, incoming)

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        try:
            incoming = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
        except Exception:
            return AckMessage.STATUS_OK, "OK"

        text = (incoming.text.content or "").strip() if incoming.text else ""
        sender_uid = incoming.sender_staff_id or ""
        self._refresh_uid2name()
        sender_name = self._uid2name.get(sender_uid)

        if not sender_name:
            self.log(f"门禁拦截: 非责任人(uid={sender_uid}) 尝试使用: {text[:30]}")
            try:
                self._reply(incoming,
                            "⛔ 报数功能仅限销售日报责任人使用。\n"
                            "如需填写日报请联系管理员，或在表格中直接填写。")
            except Exception:
                pass
            return AckMessage.STATUS_OK, "OK"

        try:
            self._handle_report(incoming, text, sender_uid, sender_name)
        except Exception as e:
            self.log(f"处理异常: {e}")
            try:
                self._reply(incoming, f"⚠️ 处理报数时出错：{e}")
            except Exception:
                pass
        return AckMessage.STATUS_OK, "OK"

    def _handle_report(self, incoming, text, sender_uid, sender_name):
        col, day = self._day_column()
        if col is None:
            self._reply(incoming, "今天不是销售日报工作日，无需报数～")
            return

        m = re.search(r"(-?\d[\d,]*(?:\.\d+)?)", text.replace("，", ","))
        if not m:
            self._reply(incoming,
                        f"{sender_name} 你好～报数格式：@提醒事项 数字\n"
                        f"例如：@提醒事项 12800（当天无销量报 0）")
            return
        num_str = m.group(1).replace(",", "")
        try:
            value = float(num_str)
            if value == int(value):
                value = int(value)
        except ValueError:
            self._reply(incoming, f"无法识别数字：{num_str}")
            return

        rid_map = self._refresh_map()
        rid = rid_map.get(sender_name)
        if not rid:
            self._reply(incoming, f"{sender_name}：表格中未找到你的行，请联系管理员添加")
            return

        client = DingTalkClient.from_config(self.config["dingtalk"])
        old_value = None
        try:
            records = client.list_records(self.config["base"]["baseId"], self.config["base"]["tableId"])
            rec = next((r for r in records if r.get("id") == rid), None)
            if rec:
                old_raw = (rec.get("fields") or {}).get(col)
                if old_raw is not None and str(old_raw).strip() != "":
                    old_value = str(old_raw).replace(",", "")
        except Exception as e:
            self.log(f"读取旧值失败(忽略,继续覆盖写入): {e}")

        client.update_records(self.config["base"]["baseId"], self.config["base"]["tableId"],
                              [{"id": rid, "fields": {col: str(value)}}])
        self.log(f"报数入表: {sender_name} {col}={value} (rid={rid}, 旧值={old_value})")

        month_total, target, ratio_str = self._calc_progress(client, rid)
        weekday = "一二三四五六日"[datetime.now().weekday()]
        lines = [f"✅ 已记录 {datetime.now().month}月{day}日（周{weekday}）销量：{value}"]
        if old_value is not None and old_value != str(value):
            lines.append(f"🔁 已覆盖你之前填报的 {old_value}")
        if ratio_str:
            lines.append(f"📊 本月累计 {month_total} / 目标 {target}，完成 {ratio_str}")
        lines.append("祝您下班愉快 🎉")
        self._reply(incoming, "\n".join(lines))

    def _calc_progress(self, client, rid):
        try:
            records = client.list_records(self.config["base"]["baseId"], self.config["base"]["tableId"])
            rec = next((r for r in records if r.get("id") == rid), None)
            if not rec:
                return None, None, None
            fvals = rec.get("fields") or {}
            day_pat = re.compile(r"^(\d+)日$")
            total = 0.0
            for k, v in fvals.items():
                if day_pat.match(str(k)):
                    try:
                        total += float(str(v).replace(",", ""))
                    except (ValueError, TypeError):
                        pass
            total = int(total) if total == int(total) else round(total, 2)
            target = fvals.get("9月销量目标（万）") or fvals.get(f"{self.config['calendar']['month']}月销量目标（万）")
            try:
                target = float(str(target).replace(",", ""))
            except (ValueError, TypeError):
                return f"{total}", None, None
            if target <= 0:
                return f"{total}", f"{int(target) if target == int(target) else target}", None
            ratio = total / target * 100
            ratio_str = f"{ratio:.1f}%"
            tgt_disp = int(target) if target == int(target) else target
            return f"{total}", f"{tgt_disp}", ratio_str
        except Exception as e:
            self.log(f"计算完成比例失败: {e}")
            return None, None, None
```

- [ ] **Step 2: Run compileall and commit**

```bash
python -m compileall common/daily_robot/listener.py
git add common/daily_robot/listener.py
git commit -m "feat(daily_robot): add Stream listener handler"
```

---

## Task 9: Refactor Hangzhou scripts to thin wrappers

**Files:**
- Modify: `数字化/钉钉/杭州日报机器人/hangzhou_reminder.py`
- Modify: `数字化/钉钉/杭州日报机器人/hangzhou_listener.py`
- Modify: `数字化/钉钉/杭州日报机器人/org_sync.py`
- Modify: `数字化/钉钉/杭州日报机器人/recalc_totals.py`
- Modify: `数字化/钉钉/杭州日报机器人/check_data.py`
- Modify: `数字化/钉钉/杭州日报机器人/leaderboard_report.py`

- [ ] **Step 1: Refactor `hangzhou_reminder.py`**

Replace `数字化/钉钉/杭州日报机器人/hangzhou_reminder.py` with:

```python
# -*- coding: utf-8 -*-
"""
杭州销售日报提醒机器人 v3（薄包装）
- 18:30（工作日）：@ 未填写人
- 20:00（工作日）：应用内 DING + 群 @ 沈聪
- 支持责任人 @提醒事项 报数（由 hangzhou_listener.py 常驻监听）
- 非工作日自动跳过；幂等防重发
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
from common.dingtalk import DingTalkClient

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
STATE_FILE = BASE_DIR / ".reminder_state.json"
LOG_DIR = BASE_DIR / "logs"


def DingTalk(cfg):
    return DingTalkClient.from_config(cfg)


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


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def send_group(title, text, at_ids=None):
    from common.daily_robot import send_group as _send_group
    from common.test_group import resolve_target
    return _send_group(CONFIG, title, text, at_ids=at_ids)


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
```

- [ ] **Step 2: Refactor `hangzhou_listener.py`**

Replace `数字化/钉钉/杭州日报机器人/hangzhou_listener.py` with:

```python
# -*- coding: utf-8 -*-
"""
杭州销售日报 - 报数监听机器人（常驻）
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import dingtalk_stream

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import ReportHandler

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
CONFIG["baseDir"] = str(BASE_DIR)
DING = CONFIG["dingtalk"]


def log(msg):
    LOG_DIR = BASE_DIR / "logs"
    LOG_DIR.mkdir(exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(LOG_DIR / f"reminder_{datetime.now():%Y%m}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    credential = dingtalk_stream.Credential(DING["appKey"], DING["appSecret"])
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
                                     ReportHandler(CONFIG, log_fn=log))
    log("监听启动：等待 @提醒事项 报数 ...")
    client.start_forever()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Refactor other Hangzhou scripts**

Replace `数字化/钉钉/杭州日报机器人/org_sync.py` with:

```python
# -*- coding: utf-8 -*-
"""
杭州 - 组织与人员同步（薄包装）
"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import org_sync

CONFIG_FILE = BASE_DIR / "config.json"
INPUTS = {
    "hangzhou": BASE_DIR / "org_input_hangzhou.json",
    "shaoxing": BASE_DIR / "org_input_shaoxing.json",
    "other": BASE_DIR / "org_input_other.json",
}


def log(msg):
    from datetime import datetime
    LOG_DIR = BASE_DIR / "logs"
    LOG_DIR.mkdir(exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [org] {msg}"
    print(line)
    with open(LOG_DIR / f"reminder_{datetime.now():%Y%m}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def do_sync():
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    config["logDir"] = str(BASE_DIR / "logs")
    changed, changes = org_sync(config, {k: str(v) for k, v in INPUTS.items()}, "hangzhou")

    if changed:
        CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        log("档案已更新: " + "；".join(changes))

    hz_changes = [c for c in changes if c.startswith("[hangzhou]")]
    if hz_changes:
        try:
            sys.path.insert(0, str(BASE_DIR))
            from hangzhou_reminder import send_group
            lines = ["### 👥 人员变动同步（杭州）", ""]
            for c in hz_changes:
                lines.append(f"- {c.replace('[hangzhou] ', '')}")
            lines.append("")
            lines.append("催报名单已自动更新。新入职同事需在日报表建行并设定目标。")
            send_group("人员变动同步", "\n".join(lines))
        except Exception as e:
            log(f"群通知发送失败: {e}")

    print("SYNCED:" if changes else "NO_CHANGES")
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


if __name__ == "__main__":
    if "--sync" in sys.argv:
        do_sync()
    elif "--status" in sys.argv:
        do_status()
    else:
        print(__doc__)
        sys.exit(1)
```

Replace `数字化/钉钉/杭州日报机器人/recalc_totals.py` with:

```python
# -*- coding: utf-8 -*-
"""杭州 - 合计自动维护（薄包装）"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import recalc_totals
import json

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

if __name__ == "__main__":
    recalc_totals(CONFIG)
```

Replace `数字化/钉钉/杭州日报机器人/check_data.py` with:

```python
# -*- coding: utf-8 -*-
"""杭州 - 表格数据体检（薄包装）"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import check_data
import json

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

if __name__ == "__main__":
    check_data(CONFIG)
```

Replace `数字化/钉钉/杭州日报机器人/leaderboard_report.py` with:

```python
# -*- coding: utf-8 -*-
"""杭州 - 销售完成率榜单（薄包装）"""
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import build_bc_markdown, collect, build_html

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))


def main():
    include_today = "--include-today" in sys.argv
    out = BASE_DIR / "leaderboard.html"
    if "--output" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--output") + 1])

    if "--bc" in sys.argv:
        url = None
        args = sys.argv[sys.argv.index("--bc") + 1:]
        if args and args[0].startswith("http"):
            url = args[0]
        print(build_bc_markdown(CONFIG, url=url))
        return

    if "--send" in sys.argv:
        from common.daily_robot import send_group
        url = CONFIG["base"]["tableUrl"]
        md = build_bc_markdown(CONFIG, url=url)
        send_group(CONFIG, "销售完成率榜", md)
        return

    now, elapsed, people = collect(CONFIG, include_today)
    html = build_html(CONFIG, now, elapsed, people)
    out.write_text(html, encoding="utf-8")
    print(f"OK 已生成: {out}")
    print(f"参与 {len(people)} 人, 已过工作日 {len(elapsed)}/{len(CONFIG['calendar']['restDays'])} 工作日")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run compileall on Hangzhou scripts**

```bash
python -m compileall "数字化/钉钉/杭州日报机器人/"hangzhou_*.py "数字化/钉钉/杭州日报机器人/"org_sync.py "数字化/钉钉/杭州日报机器人/"recalc_totals.py "数字化/钉钉/杭州日报机器人/"check_data.py "数字化/钉钉/杭州日报机器人/"leaderboard_report.py
```

Expected: All compiled OK.

- [ ] **Step 5: Commit**

```bash
git add 数字化/钉钉/杭州日报机器人/
git commit -m "refactor(hangzhou): wrap around common.daily_robot core"
```

---

## Task 10: Update Hangzhou `config.example.json`

**Files:**
- Modify: `数字化/钉钉/杭州日报机器人/config.example.json`

- [ ] **Step 1: Add region section and robot.mode**

Edit `数字化/钉钉/杭州日报机器人/config.example.json`:

```json
{
  "region": {
    "name": "hangzhou",
    "displayName": "杭州",
    "activeOrgRegion": "hangzhou",
    "deptOrder": ["杭中", "滨萧", "余杭"],
    "deptLabel": {"杭州运营总监": "运营总监"},
    "broadcastExclude": ["余发兴"],
    "totalPrefixes": ["杭中", "滨萧", "余杭"],
    "allDeptNames": ["杭中", "滨萧", "余杭", "杭州运营总监"]
  },
  "dingtalk": {...},
  "base": {...},
  "robot": {
    "mode": "groupSend",
    "robotCode": "<机器人robotCode>",
    "openConversationId": "<群openConversationId>",
    "groupName": "线下事业部杭州日报群"
  },
  ...
}
```

- [ ] **Step 2: Commit**

```bash
git add 数字化/钉钉/杭州日报机器人/config.example.json
git commit -m "config(hangzhou): add region section and robot.mode"
```

---

## Task 11: Create `shaoxing_daily_robot` directory and config

**Files:**
- Create: `数字化/钉钉/shaoxing_daily_robot/config.example.json`

- [ ] **Step 1: Create config.example.json**

Create `数字化/钉钉/shaoxing_daily_robot/config.example.json`:

```json
{
  "_说明": "绍兴销售日报提醒机器人配置模板。复制为 config.json 并填写真实值。凭据同杭州机器人（企业应用「提醒事项」）。换月建新表后需更新 base/tableId/calendar。",
  "region": {
    "name": "shaoxing",
    "displayName": "绍兴",
    "activeOrgRegion": "shaoxing",
    "deptOrder": ["绍兴", "诸暨", "绍兴运营总监"],
    "deptLabel": {"绍兴运营总监": "运营总监"},
    "broadcastExclude": [],
    "totalPrefixes": ["绍兴", "诸暨"],
    "allDeptNames": ["绍兴", "诸暨", "绍兴运营总监"]
  },
  "dingtalk": {
    "appKey": "<企业应用AppKey>",
    "appSecret": "<企业应用AppSecret>",
    "operatorId": "<操作人unionId>"
  },
  "base": {
    "baseId": "N7dx2rn0Jbl4O5yguNdqpvg1WMGjLRb3",
    "tableId": "mgqo9inoj99gtv5cfii9r",
    "tableUrl": "https://alidocs.dingtalk.com/i/nodes/N7dx2rn0Jbl4O5yguNdqpvg1WMGjLRb3?entrance=data&sheetId=mgqo9inoj99gtv5cfii9r"
  },
  "calendar": {
    "month": 9,
    "restDays": [6, 13, 19, 25, 26, 27],
    "_说明": "当月休息日列表，依据钉钉考勤核验。每月换表时更新。"
  },
  "org": {
    "rootDeptId": 1050135497,
    "aliases": {},
    "_说明": "组织同步档案（org_sync.py 维护）。",
    "depts": {
      "shaoxing": [1050416052, 1049663672, 1050408252]
    },
    "archives": {
      "shaoxing": {}
    },
    "lastSync": ""
  },
  "robot": {
    "mode": "groupSend",
    "robotCode": "<机器人robotCode>",
    "openConversationId": "cidO7oMaE/xLi8uyyHj4t2tgw==",
    "groupName": "线下绍兴日报群"
  },
  "members": {},
  "ccUsers": {
    "_说明": "20:00 催办时固定@的人",
    "沈聪": "<userId>"
  },
  "schedule": {
    "remindHour": 18,
    "checkHour": 20
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add 数字化/钉钉/shaoxing_daily_robot/config.example.json
git commit -m "config(shaoxing): add config template"
```

---

## Task 12: Create Shaoxing wrapper scripts

**Files:**
- Create: `数字化/钉钉/shaoxing_daily_robot/shaoxing_reminder.py`
- Create: `数字化/钉钉/shaoxing_daily_robot/shaoxing_listener.py`
- Create: `数字化/钉钉/shaoxing_daily_robot/org_sync.py`
- Create: `数字化/钉钉/shaoxing_daily_robot/recalc_totals.py`
- Create: `数字化/钉钉/shaoxing_daily_robot/check_data.py`
- Create: `数字化/钉钉/shaoxing_daily_robot/leaderboard_report.py`

- [ ] **Step 1: Write `shaoxing_reminder.py`**

```python
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
```

- [ ] **Step 2: Write `shaoxing_listener.py`**

```python
# -*- coding: utf-8 -*-
"""绍兴销售日报 - 报数监听机器人（常驻）"""
import json
from datetime import datetime
from pathlib import Path

import dingtalk_stream

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import ReportHandler

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
CONFIG["baseDir"] = str(BASE_DIR)
DING = CONFIG["dingtalk"]


def log(msg):
    LOG_DIR = BASE_DIR / "logs"
    LOG_DIR.mkdir(exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(LOG_DIR / f"reminder_{datetime.now():%Y%m}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    credential = dingtalk_stream.Credential(DING["appKey"], DING["appSecret"])
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
                                     ReportHandler(CONFIG, log_fn=log))
    log("监听启动：等待 @提醒事项 报数 ...")
    client.start_forever()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Write `org_sync.py`, `recalc_totals.py`, `check_data.py`, `leaderboard_report.py`**

`org_sync.py`:

```python
# -*- coding: utf-8 -*-
"""绍兴 - 组织与人员同步（薄包装）"""
import json
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
    import sys
    if "--sync" in sys.argv:
        do_sync()
    elif "--status" in sys.argv:
        do_status()
    else:
        print(__doc__)
        sys.exit(1)
```

`recalc_totals.py`:

```python
# -*- coding: utf-8 -*-
"""绍兴 - 合计自动维护（薄包装）"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import recalc_totals

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

if __name__ == "__main__":
    recalc_totals(CONFIG)
```

`check_data.py`:

```python
# -*- coding: utf-8 -*-
"""绍兴 - 表格数据体检（薄包装）"""
import json
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import check_data

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

if __name__ == "__main__":
    check_data(CONFIG)
```

`leaderboard_report.py`:

```python
# -*- coding: utf-8 -*-
"""绍兴 - 销售完成率榜单（薄包装）"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import build_bc_markdown, collect, build_html, send_group

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))


def main():
    include_today = "--include-today" in sys.argv
    out = BASE_DIR / "leaderboard.html"
    if "--output" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--output") + 1])

    if "--bc" in sys.argv:
        url = None
        args = sys.argv[sys.argv.index("--bc") + 1:]
        if args and args[0].startswith("http"):
            url = args[0]
        print(build_bc_markdown(CONFIG, url=url))
        return

    if "--send" in sys.argv:
        url = CONFIG["base"]["tableUrl"]
        md = build_bc_markdown(CONFIG, url=url)
        send_group(CONFIG, "销售完成率榜", md)
        return

    now, elapsed, people = collect(CONFIG, include_today)
    html = build_html(CONFIG, now, elapsed, people)
    out.write_text(html, encoding="utf-8")
    print(f"OK 已生成: {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run compileall on Shaoxing scripts**

```bash
python -m compileall "数字化/钉钉/shaoxing_daily_robot/"*.py
```

Expected: Compiled OK.

- [ ] **Step 5: Commit**

```bash
git add 数字化/钉钉/shaoxing_daily_robot/
git commit -m "feat(shaoxing): add daily robot wrapper scripts"
```

---

## Task 13: Copy/move org snapshot and add README/autostart

**Files:**
- Create: `数字化/钉钉/shaoxing_daily_robot/org_input_shaoxing.json`
- Create: `数字化/钉钉/shaoxing_daily_robot/README.md`
- Create: `数字化/钉钉/shaoxing_daily_robot/register_autostart.bat`

- [ ] **Step 1: Copy snapshot**

```bash
cp "数字化/钉钉/杭州日报机器人/org_input_shaoxing.json" "数字化/钉钉/shaoxing_daily_robot/org_input_shaoxing.json"
```

- [ ] **Step 2: Write README.md**

Create `数字化/钉钉/shaoxing_daily_robot/README.md`:

```markdown
# 绍兴销售日报提醒机器人

## 机制

1. **@机器人报数，自动入表**：责任人在「线下绍兴日报群」发 `@提醒事项 12800`（报 0 也行），机器人自动写入其当日列并回复确认。
2. **18:30 仅提醒未填人**：检查表格后只 @ 当日未填写的人。
3. **20:00 催办**：对未填人发应用内 DING + 群里 @沈聪 知会。
4. **每晚 00:05 组织同步**：拉取绍兴区域通讯录快照，更新催报名单并发群通知。
5. **8:30 榜单播报**：发送部门维度完成率榜单。

## 手动命令

```
python shaoxing_reminder.py --status    # 查看填写状态
python shaoxing_reminder.py --remind    # 手动跑 18:30 提醒
python shaoxing_reminder.py --check     # 手动跑 20:00 催办
python org_sync.py --status             # 查看组织档案
python org_sync.py --sync               # 手动同步组织
python recalc_totals.py                 # 重算合计
python check_data.py                    # 数据体检
python leaderboard_report.py --send     # 发送榜单
```
```

- [ ] **Step 3: Write register_autostart.bat**

Create `数字化/钉钉/shaoxing_daily_robot/register_autostart.bat`:

```batch
@echo off
setlocal
set "VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\shaoxing_listener.vbs"
set "PY=%USERPROFILE%\miniconda3\python.exe"
set "SCRIPT=%~dp0shaoxing_listener.py"
(
  echo Set WshShell = CreateObject("WScript.Shell"^)
  echo WshShell.Run """%PY%"" """%SCRIPT%""", 0, False
) > "%VBS%"
echo 已注册开机自启: %VBS%
start /b "" "%PY%" "%SCRIPT%"
echo 已后台启动 shaoxing_listener.py
pause
```

- [ ] **Step 4: Commit**

```bash
git add 数字化/钉钉/shaoxing_daily_robot/
git commit -m "docs(shaoxing): add snapshot, README and autostart script"
```

---

## Task 14: Update command cheat sheet

**Files:**
- Modify: `docs/命令速查表.md`

- [ ] **Step 1: Add Shaoxing section**

Append to `docs/命令速查表.md`:

```markdown
## 绍兴日报机器人（shaoxing_daily_robot）

```bat
rem 查看状态
python 数字化/钉钉/shaoxing_daily_robot/shaoxing_reminder.py --status

rem 18:30 提醒（生产）
python 数字化/钉钉/shaoxing_daily_robot/shaoxing_reminder.py --remind

rem 20:00 催办（生产）
python 数字化/钉钉/shaoxing_daily_robot/shaoxing_reminder.py --check

rem 18:30/20:00 按当前小时自动分流（供 cron 用）
python 数字化/钉钉/shaoxing_daily_robot/shaoxing_reminder.py --once

rem 测试群推送
set TEST_MODE=1 && python 数字化/钉钉/shaoxing_daily_robot/shaoxing_reminder.py --remind
set TEST_MODE=1 && python 数字化/钉钉/shaoxing_daily_robot/shaoxing_reminder.py --check

rem 组织同步
python 数字化/钉钉/shaoxing_daily_robot/org_sync.py --status
python 数字化/钉钉/shaoxing_daily_robot/org_sync.py --sync

rem 合计重算 / 数据体检 / 榜单播报
python 数字化/钉钉/shaoxing_daily_robot/recalc_totals.py
python 数字化/钉钉/shaoxing_daily_robot/check_data.py
python 数字化/钉钉/shaoxing_daily_robot/leaderboard_report.py --send
```
```

- [ ] **Step 2: Commit**

```bash
git add docs/命令速查表.md
git commit -m "docs: add shaoxing daily robot commands"
```

---

## Task 15: Write unit tests for `common/daily_robot`

**Files:**
- Create: `tests/common/test_daily_robot.py`

- [ ] **Step 1: Write tests**

Create `tests/common/test_daily_robot.py`:

```python
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
import sys
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot import today_info, fetch_status


class TestTodayInfo(unittest.TestCase):
    def test_workday(self):
        import datetime
        # 2026-09-01 is Tuesday, assume not in restDays
        cal = {"month": 9, "restDays": [6, 13, 19, 25, 26, 27]}
        now = datetime.datetime(2026, 9, 1)
        _, day, err = today_info(cal, now=now)
        self.assertEqual(day, 1)
        self.assertIsNone(err)

    def test_rest_day(self):
        import datetime
        cal = {"month": 9, "restDays": [6, 13, 19, 25, 26, 27]}
        now = datetime.datetime(2026, 9, 6)
        _, day, err = today_info(cal, now=now)
        self.assertIsNone(day)
        self.assertIsNone(err)

    def test_month_mismatch(self):
        import datetime
        cal = {"month": 9, "restDays": []}
        now = datetime.datetime(2026, 10, 1)
        _, day, err = today_info(cal, now=now)
        self.assertIsNone(day)
        self.assertIn("月份", err)
```

- [ ] **Step 2: Run tests**

```bash
python -m pytest tests/common/test_daily_robot.py -v
```

Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/common/test_daily_robot.py
git commit -m "test(daily_robot): add unit tests for today_info"
```

---

## Task 16: Final compileall and smoke tests

**Files:**
- All files above

- [ ] **Step 1: Run compileall**

```bash
python -m compileall common/daily_robot/ tests/common/ "数字化/钉钉/杭州日报机器人/" "数字化/钉钉/shaoxing_daily_robot/"
```

Expected: All compiled OK.

- [ ] **Step 2: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: All tests pass.

- [ ] **Step 3: Dry-run Hangzhou --status**

```bash
python 数字化/钉钉/杭州日报机器人/hangzhou_reminder.py --status
```

Expected: Prints status without sending messages.

- [ ] **Step 4: Dry-run Shaoxing --status (after config.json filled)**

```bash
python 数字化/钉钉/shaoxing_daily_robot/shaoxing_reminder.py --status
```

Expected: Prints status without sending messages.

- [ ] **Step 5: Commit final state**

```bash
git add .
git commit -m "feat(shaoxing): complete shaoxing daily robot with shared core"
```

---

## Self-Review

### Spec coverage

| Spec 要求 | 对应 Task |
|-----------|-----------|
| 新建 `common/daily_robot/` 公共包 | Task 1, 2, 3, 4, 5, 6, 7, 8 |
| 杭州脚本改为薄包装 | Task 9 |
| 杭州 config 增加 `region` 段 | Task 10 |
| 新建 `shaoxing_daily_robot/` 目录 | Task 11, 12, 13 |
| 绍兴 config 模板 | Task 11 |
| 绍兴 18:30/20:00 提醒催办 | Task 12 |
| 绍兴 @机器人报数 listener | Task 12 |
| 绍兴组织同步/合计重算/数据体检/榜单 | Task 12, 13 |
| 命令速查表更新 | Task 14 |
| 单元测试与验证 | Task 15, 16 |

### Placeholder scan

- 无 TBD/TODO。
- 配置模板中 `<...>` 为待填写项，符合现有 config.example.json 风格。
- 每个 Task 包含完整代码或命令。

### Type consistency

- `today_info` 签名统一为 `(calendar, now=None)`；测试用例已使用 `now=` 关键字。
- `org_sync` 返回 `(changed: bool, changes: list)`，包装脚本一致使用。
- `send_group(config, title, text, at_ids=None)` 签名一致。

**Plan complete.**
