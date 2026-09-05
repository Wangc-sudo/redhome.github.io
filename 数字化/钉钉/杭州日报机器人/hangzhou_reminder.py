# -*- coding: utf-8 -*-
"""
杭州销售日报提醒机器人 v2
- 18:30（工作日）：检查表格，仅 @未填写的人（已填的不打扰）
- 20:00（工作日）：对未填写的人发应用内 DING（权限开通前降级为群@），群里 @沈聪 知会
- 支持责任人 @提醒事项 报数（由 hangzhou_listener.py 常驻监听写表）
- 非工作日自动跳过；幂等防重发

用法:
  python hangzhou_reminder.py --once      # 按当前小时自动分流（供 cron 用）
  python hangzhou_reminder.py --remind    # 手动跑 18:30 提醒
  python hangzhou_reminder.py --check     # 手动跑 20:00 DING 催办
  python hangzhou_reminder.py --status    # 查看当日填写状态（不发消息）
"""
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
_REPO_ROOT = BASE_DIR.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common.dingtalk import DingTalkClient

CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
STATE_FILE = BASE_DIR / ".reminder_state.json"
LOG_DIR = BASE_DIR / "logs"

# 兼容旧引用（hangzhou_listener / check_data / leaderboard_report / recalc_totals
# 均从本模块 import DingTalk 并传 config 字典）——统一实现见 common/dingtalk.py
def DingTalk(cfg):
    """旧接口兼容: DingTalk({"appKey":..,"appSecret":..,"operatorId":..}) → DingTalkClient"""
    return DingTalkClient.from_config(cfg)


# ---------- 状态/日志 ----------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def log(msg):
    LOG_DIR.mkdir(exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(LOG_DIR / f"reminder_{datetime.now():%Y%m}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------- 业务 ----------
def today_info():
    now = datetime.now()
    cal = CONFIG["calendar"]
    if now.month != cal["month"]:
        return now, None, f"当前月份 {now.month} 与配置月份 {cal['month']} 不符，请为新月建表并更新 config"
    day = now.day
    if day in cal["restDays"]:
        return now, None, None  # 休息日
    return now, day, None


def fetch_status(dt, day):
    """返回 (filled_names, unfilled_names, total, skipped)。API的fields按字段名索引"""
    base = CONFIG["base"]
    client = DingTalk(CONFIG["dingtalk"])

    col_name = f"{day}日"
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
        # 未填 = 当日列无值（键缺失/空串）；数值 0 视为已填
        v = fvals.get(col_name)
        if v is None or (isinstance(v, str) and v.strip() == ""):
            unfilled.append(name)
        else:
            filled.append(name)
    return filled, unfilled, len(records), skipped


def send_group(title, text, at_ids=None):
    robot = CONFIG["robot"]
    client = DingTalk(CONFIG["dingtalk"])
    r = client.send_group_markdown(robot["robotCode"], robot["openConversationId"],
                                   title, text, at_user_ids=at_ids)
    log(f"已发群消息: {title} (at={at_ids})")
    return r


def do_remind(state, now, day):
    """18:30 仅提醒未填写的人"""
    key = f"remind_{now:%Y%m%d}"
    if state.get(key):
        log("今日提醒已发过，跳过")
        return
    filled, unfilled, total, skipped = fetch_status(now, day)
    log(f"填写状态: 已填{len(filled)} 未填{len(unfilled)}")
    if not unfilled:
        log("全员已填写，不发提醒")
        state[key] = datetime.now().isoformat()
        save_state(state)
        return

    members = CONFIG["members"]
    at_ids, missing = [], []
    for name in unfilled:
        uid = members.get(name)
        if uid:
            at_ids.append(uid)
        else:
            missing.append(name)

    weekday = "一二三四五六日"[now.weekday()]
    url = CONFIG["base"]["tableUrl"]
    lines = [f"### 📋 销售日报填写提醒（{now.month}月{day}日 周{weekday}）", ""]
    lines.append(f"以下 **{len(unfilled)}** 位同事还未填写今日销售日报，请尽快填写：")
    lines.append("")
    lines.append(f"**{('、'.join(unfilled))}**")
    lines.append("")
    lines.append("也可直接在群里 **@提醒事项 + 数字** 报数（如 `@提醒事项 12800`，报 0 也行）")
    lines.append("")
    lines.append(f"[点此填写]({url})")
    if missing:
        lines.append("")
        lines.append(f"（{('、'.join(missing))} 未在通讯录映射中，无法@，请手动提醒）")
    send_group("销售日报填写提醒", "\n".join(lines), at_ids=at_ids)
    state[key] = datetime.now().isoformat()
    save_state(state)


def do_check(state, now, day):
    """20:00 DING 未填人 + 群@沈聪。群消息脚本自发；DING 由 cron agent 执行输出的 dws 命令"""
    key = f"check_{now:%Y%m%d}"
    if state.get(key):
        log("今日检查已发过，跳过")
        print("NO_ACTION: 今日检查已发过")
        return
    filled, unfilled, total, skipped = fetch_status(now, day)
    log(f"填写状态: 已填{len(filled)} 未填{len(unfilled)}")
    if not unfilled:
        log("全员已填写，不发催办")
        state[key] = datetime.now().isoformat()
        save_state(state)
        print("NO_ACTION: 全员已填写")
        return

    members = CONFIG["members"]
    cc = CONFIG["ccUsers"]
    ding_ids = [members[n] for n in unfilled if members.get(n)]
    missing = [n for n in unfilled if not members.get(n)]
    weekday = "一二三四五六日"[now.weekday()]
    url = CONFIG["base"]["tableUrl"]
    names_text = "、".join(unfilled)

    # 1) 群内知会：@未填人 + @沈聪（保证触达，DING为加强）
    at_ids = list(ding_ids) + [cc["沈聪"]]
    lines = [f"### ⏰ 销售日报未填写（{now.month}月{day}日）", ""]
    lines.append(f"截至 20:00，以下 **{len(unfilled)}** 位同事仍未填写：")
    lines.append("")
    lines.append(f"**{names_text}**")
    lines.append("")
    lines.append("已同步 DING 提醒以上人员，请在群里 @提醒事项 报数或直接填写。")
    lines.append(f"[点此填写]({url})")
    if missing:
        lines.append("")
        lines.append(f"（{('、'.join(missing))} 未在通讯录映射中，无法@，请手动提醒）")
    send_group("销售日报未填写", "\n".join(lines), at_ids=at_ids)
    state[key] = datetime.now().isoformat()
    save_state(state)

    # 2) 输出 DING 命令给 cron agent 执行（dws 服务端通道已授权）
    if ding_ids:
        content = (f"【销售日报催办】{now.month}月{day}日（周{weekday}）：你还未填写今日销售日报，"
                   f"请在群里 @提醒事项 报数或填写表格 {url}")
        cmd = (f'dws ding message send --robot-code {CONFIG["robot"]["robotCode"]} '
               f'--users {",".join(ding_ids)} --content "{content}" --type app --format json')
        print("DING_CMD_START")
        print(cmd)
        print("DING_CMD_END")
        log(f"待执行DING: {names_text}")


def main():
    args = sys.argv[1:]
    now, day, err = today_info()
    if err:
        log(f"SKIP: {err}")
        return
    if day is None:
        log(f"SKIP: 今天（{now:%Y-%m-%d}）是休息日")
        return

    state = load_state()
    if "--status" in args:
        filled, unfilled, total, skipped = fetch_status(now, day)
        print(f"{now.month}月{day}日 填写状态: 已填 {len(filled)} 人 / 未填 {len(unfilled)} 人")
        print("已填:", "、".join(filled) or "(无)")
        print("未填:", "、".join(unfilled) or "(无)")
        return

    if "--remind" in args:
        do_remind(state, now, day)
    elif "--check" in args:
        do_check(state, now, day)
    elif "--once" in args:
        # 18点段（含18:30补跑窗口）→ 提醒；19点及以后 → 检查
        if now.hour < 19:
            do_remind(state, now, day)
        else:
            do_check(state, now, day)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
