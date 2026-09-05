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
    raise NotImplementedError


def do_check(config, state, now, day):
    raise NotImplementedError


def org_sync(config, inputs, active_region):
    raise NotImplementedError


def recalc_totals(config):
    raise NotImplementedError


def check_data(config):
    raise NotImplementedError
