# -*- coding: utf-8 -*-
"""渠道日销机器人填报（2026-09-29 方案，region=qudao）。

群内一句话填报替代手工 AI 表台账：``京东 习水村 15867`` → 解析 →
写 ``channel_sales_robot_inbox``（append-only）→ 即时回执。extract
归并进 fact 由 :mod:`common.public_data.channel_robot_inbox` 承担。

与线下报数 :mod:`common.gateway.report_intake` 的核心差异：

* 门禁 = **群成员即可填**，不查 ``dim_robot_member``（P0 探针证实 19
  位渠道负责人不在本企业钉钉组织）；sender 仅作审计记录；
* 店名在文本里（名册 = ``fact_channel_store_target`` 全店），填报人与
  门店无归属绑定——渠道对接人代填与门店自填同一通道；
* 默认业务日 = **昨天**（T+1 采集口径），日期前缀补填最长 30 天；
* 同业务键重发即覆盖（inbox 追加，extract 取最新），无编辑概念。

已定格式裁决（2026-09-29 运维）：一条（行）一店一金额；多店分行发。
"""

import contextlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from common.gateway.report_intake import IntakeOutcome, _fmt_amount
from common.metrics.daily_report import fetch_channel_dept_rollup
from common.public_data import channel_robot_inbox as inbox_mod

#: 渠道词（= fact_channel_store_target.channel 取值域）。
CHANNEL_WORDS = ("即时零售", "拼多多", "京东", "天猫", "直播", "私域", "猫超")

#: 允许「渠道级填报」（无店名、store_name=NULL）的渠道（猫超式整渠道留空）。
CHANNEL_LEVEL_OK = frozenset({"猫超"})

#: 补填可追溯天数上限（与 /补签 一致）。
MAX_BACKFILL_DAYS = 30

_WEEKDAYS = "一二三四五六日"

#: 日期前缀：9.27 / 9-27 / 9/27 / 9月27(日)，段间须分隔符（防店名数字误切）。
_DATE_PREFIX_RE = re.compile(
    r"^(?P<m>\d{1,2})\s*[.\-/月]\s*(?P<d>\d{1,2})日?[\s,，:：]+"
)

#: 行尾金额：必须与店名部分有空白/逗号/冒号分隔（防「DY1988旗舰店」的
#: 1988 被误当金额——粘连数字不认）。
_AMOUNT_TAIL_RE = re.compile(r"(?:^|[\s,，:：])(?P<amt>-?\d[\d,]*(?:\.\d+)?)\s*$")

_ZERO_WIDTH_RE = re.compile("[​‌‍⁠﻿]")
_LEADING_MENTION_RE = re.compile(r"^@[^\s/]+\s*")


@dataclass(frozen=True)
class FillEntry:
    """一条解析成功的填报。"""

    business_date: date
    channel: str
    store_name: str | None  # None = 渠道级（猫超式）
    amount: object
    recognized_from: str | None  # 模糊识别原词（回执回显）
    raw_line: str


@dataclass(frozen=True)
class FillReject:
    """一条解析失败的行（落 inbox status=rejected，供运营观察）。"""

    raw_line: str
    reason: str


# ---------------------------------------------------------------------------
# 解析（纯逻辑）
# ---------------------------------------------------------------------------

def _clean_text(text):
    text = _ZERO_WIDTH_RE.sub("", text or "")
    return text


def _resolve_date(month, day, *, today):
    """补填日期：年份取不晚于今天的最近一次；未来/超 30 天 → ``None``。"""
    for year in (today.year, today.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate <= today:
            break
    else:
        return None
    if (today - candidate).days > MAX_BACKFILL_DAYS:
        return None
    return candidate


def _match_store(token, *, roster, channel_hint=None):
    """店名包含互查 → ``(store, channel)`` / ``("AMBIGUOUS", candidates)`` / ``None``。

    *channel_hint* 非空时先按渠道过滤名册（``京东 习水村`` 只在京东店内
    选，降歧义）；候选唯一才认。
    """
    scope = roster
    if channel_hint:
        scoped = {s: c for s, c in roster.items() if c == channel_hint}
        if scoped:
            scope = scoped
    candidates = [
        s for s in scope if token and (token in s or s in token)
    ]
    if len(candidates) == 1:
        return candidates[0], scope[candidates[0]]
    if candidates:
        return "AMBIGUOUS", sorted(candidates)
    return None


def _parse_line(line, *, today, roster):
    """单行 → ``FillEntry | FillReject | None``（None = 无金额数字，非填报行）。"""
    raw_line = line
    default_date = today - timedelta(days=1)

    date_match = _DATE_PREFIX_RE.match(line)
    if date_match:
        business_date = _resolve_date(
            int(date_match.group("m")), int(date_match.group("d")), today=today
        )
        if business_date is None:
            return FillReject(
                raw_line,
                f"日期超出范围（仅支持近 {MAX_BACKFILL_DAYS} 天内）",
            )
        line = line[date_match.end():]
    else:
        business_date = default_date

    amount_match = _AMOUNT_TAIL_RE.search(line)
    if not amount_match:
        if re.search(r"\d", line):
            return FillReject(raw_line, "金额未识别（金额须与店名以空格分隔）")
        return None
    num_str = amount_match.group("amt").replace(",", "")
    try:
        value = float(num_str)
    except ValueError:
        return None
    amount = int(value) if value == int(value) else value
    token_text = line[: amount_match.start()].strip(" ,，:：")

    if not token_text:
        return FillReject(raw_line, "缺少店名/渠道词")

    # 整段先按店名匹配（覆盖「私域 0」这类店名=渠道词的写法）。
    whole = token_text.replace(" ", "")
    hit = _match_store(whole, roster=roster)
    if hit is not None and hit[0] != "AMBIGUOUS":
        store, channel = hit
        return FillEntry(
            business_date, channel, store, amount,
            None if whole == store else whole, raw_line,
        )

    # 剥渠道词前缀，渠道内匹配剩余店名。
    channel_hint = next(
        (w for w in CHANNEL_WORDS if token_text.startswith(w)), None
    )
    if channel_hint:
        token = token_text[len(channel_hint):].strip(" ,，:：")
        if not token:
            if channel_hint in CHANNEL_LEVEL_OK:
                return FillEntry(
                    business_date, channel_hint, None, amount, None, raw_line
                )
            return FillReject(
                raw_line, f"「{channel_hint}」需带店名（仅猫超支持渠道级填报）"
            )
        hit = _match_store(token, roster=roster, channel_hint=channel_hint)
    else:
        if hit is not None:  # 整段歧义（无渠道词可剥）
            return FillReject(
                raw_line,
                "「{}」匹配多家店：{}，请加渠道词或写全店名".format(
                    whole, "、".join(hit[1])
                ),
            )
        hit = _match_store(token_text, roster=roster)

    if hit is None:
        return FillReject(raw_line, f"未找到门店「{token_text}」")
    if hit[0] == "AMBIGUOUS":
        return FillReject(
            raw_line,
            "匹配多家店：{}，请写全店名".format("、".join(hit[1])),
        )
    store, channel = hit
    token = token_text[len(channel_hint):].strip(" ,，:：") if channel_hint else token_text
    return FillEntry(
        business_date, channel, store, amount,
        None if token == store else token, raw_line,
    )


def parse_fill_text(text, *, today, roster):
    """填报文本 → ``(entries, rejects)``；不含任何金额数字 → ``([], [])``。

    按行解析（一条一行一店一金额，2026-09-29 运维裁决）；剥 @提及 与
    零宽字符；``/`` 开头由调用方拦截（辅助指令），这里按普通行处理。
    """
    entries, rejects = [], []
    for raw in _clean_text(text).splitlines():
        line = _LEADING_MENTION_RE.sub("", raw.strip())
        if not line:
            continue
        result = _parse_line(line, today=today, roster=roster)
        if isinstance(result, FillEntry):
            entries.append(result)
        elif isinstance(result, FillReject):
            rejects.append(result)
    return entries, rejects


# ---------------------------------------------------------------------------
# 回执文案（纯逻辑）
# ---------------------------------------------------------------------------

def build_channel_help():
    return (
        "🤖 渠道日销填报：\n"
        "直接发：店名 金额（默认报昨天）\n"
        "  京东 习水村 15867\n"
        "  猫超 731033（猫超可只报渠道）\n"
        "补填带日期：9.27 天猫 酒旗 82059\n"
        "当天无销售报 0；填错了重发一条同店同日即可覆盖～"
    )


def build_format_hint():
    return (
        "⚠️ 没看懂～格式：[日期] 渠道 店名 金额\n"
        "例如：京东 习水村 15867（报昨天）；9.27 天猫 酒旗 82059（补填）"
    )


def _fmt_wan(value):
    number = float(value)
    if abs(number) >= 10000:
        return f"{number / 10000:.1f}万"
    return _fmt_amount(number)


def build_fill_reply(*, entries, overwritten, rejects, progress_lines):
    """回执。entries = ``[(entry, prev_amount|None)]``。"""
    lines = [f"✅ 已记录 {len(entries)} 条渠道日销："]
    for entry, prev in entries:
        store_disp = entry.store_name or f"{entry.channel}（渠道级）"
        line = (
            f"{entry.business_date.month}/{entry.business_date.day} "
            f"{entry.channel}·{store_disp}：{_fmt_amount(entry.amount)}"
        )
        if entry.recognized_from:
            line += f"（识别：{entry.recognized_from}）"
        if prev is not None and float(prev) != float(entry.amount):
            line += f"（🔁 覆盖旧值 {_fmt_amount(prev)}）"
        lines.append(line)
    lines.extend(progress_lines)
    if rejects:
        lines.append(f"⚠️ {len(rejects)} 条未识别：")
        for reject in rejects:
            lines.append(f"「{reject.raw_line}」{reject.reason}")
        lines.append("格式：[日期] 渠道 店名 金额，如：京东 习水村 15867")
    else:
        lines.append("填错了重发一条即可覆盖～")
    return "\n".join(lines)


def build_error_reply():
    return "⚠️ 处理填报时出错，请稍后再试，或在表格中直接填写。"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _fetch_all(conn, sql, params=()):
    with contextlib.closing(conn.cursor()) as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def fetch_roster(conn):
    """填报名册：``{store_name: channel}``（fact_channel_store_target 全店）。"""
    rows = _fetch_all(
        conn,
        "SELECT `store_name`, `channel` FROM `fact_channel_store_target`",
    )
    return {
        str(row["store_name"]).strip(): str(row.get("channel") or "").strip()
        for row in rows
        if str(row.get("store_name") or "").strip()
    }


def _fetch_prev_inbox(conn, entry):
    """同业务键最新 parsed 行（覆盖检测 + 进度 diff 基准）。"""
    rows = _fetch_all(
        conn,
        f"SELECT `sales_amount` FROM `{inbox_mod.INBOX_TABLE}` "
        "WHERE `status` = 'parsed' AND `business_date` = %s "
        "AND `channel` = %s AND `store_name` <=> %s "
        "ORDER BY `id` DESC LIMIT 1",
        (entry.business_date, entry.channel, entry.store_name),
    )
    return rows[0]["sales_amount"] if rows else None


def _progress_lines(conn, entries, diffs):
    """每个涉及渠道一行 MTD 进度（含本次填报 diff；自然日口径同 qudao.html）。

    读表失败（进度只是参考信息）→ 静默略过，不影响填报主流程。
    """
    by_date = {}
    for entry in entries:
        by_date.setdefault(
            (entry.business_date.year, entry.business_date.month),
            entry.business_date,
        )
    lines = []
    try:
        for (year, month), through in sorted(by_date.items()):
            rollup = fetch_channel_dept_rollup(
                conn, year=year, month=month, through=through
            )
            channels = sorted({
                e.channel for e in entries
                if (e.business_date.year, e.business_date.month) == (year, month)
            })
            for channel in channels:
                stats = rollup.get(channel)
                if not stats:
                    continue
                completed = float(stats.get("completed") or 0) + diffs.get(
                    (year, month, channel), 0.0
                )
                target = stats.get("target")
                if target:
                    rate = completed / float(target) * 100
                    lines.append(
                        f"📊 {channel} 本月累计 {_fmt_wan(completed)} / "
                        f"目标 {_fmt_wan(target)}，完成 {rate:.1f}%"
                    )
                else:
                    lines.append(f"📊 {channel} 本月累计 {_fmt_wan(completed)}")
    except Exception:
        return []
    return lines


def handle_channel_fill(conn, *, region_cfg, text, sender_uid, sender_name,
                        conversation_id, now):
    """处理一条渠道群填报消息。不写连接 commit（由调用方提交）。

    返回 :class:`IntakeOutcome`；``reply=None`` 表示非填报消息（无金额
    数字的闲聊），调用方静默不回执。
    """
    today = now.date() if isinstance(now, datetime) else now
    cleaned = _clean_text(text).strip()
    if cleaned.startswith("/"):
        return IntakeOutcome(
            "aux", build_channel_help(), region=region_cfg.region,
        )

    roster = fetch_roster(conn)
    entries, rejects = parse_fill_text(text, today=today, roster=roster)
    if not entries and not rejects:
        return IntakeOutcome("ignored", None, region=region_cfg.region)
    if not entries:
        for reject in rejects:
            inbox_mod.insert_inbox(
                conn, conversation_id=conversation_id,
                sender_userid=sender_uid, sender_name=sender_name,
                raw_text=reject.raw_line, channel=None, store_name=None,
                business_date=None, sales_amount=None, status="rejected",
                reject_reason=reject.reason, now=now,
            )
        return IntakeOutcome(
            "no_number",
            "\n".join(
                [f"⚠️ {len(rejects)} 条未识别："]
                + [f"「{r.raw_line}」{r.reason}" for r in rejects]
                + [build_format_hint()]
            ),
            region=region_cfg.region,
        )

    recorded = []
    diffs = {}
    overwritten = False
    for entry in entries:
        prev = _fetch_prev_inbox(conn, entry)
        inbox_mod.insert_inbox(
            conn, conversation_id=conversation_id,
            sender_userid=sender_uid, sender_name=sender_name,
            raw_text=entry.raw_line, channel=entry.channel,
            store_name=entry.store_name, business_date=entry.business_date,
            sales_amount=entry.amount, status="parsed", reject_reason=None,
            now=now,
        )
        recorded.append((entry, prev))
        overwritten = overwritten or (
            prev is not None and float(prev) != float(entry.amount)
        )
        key = (entry.business_date.year, entry.business_date.month,
               entry.channel)
        diffs[key] = diffs.get(key, 0.0) + (
            float(entry.amount) - (float(prev) if prev is not None else 0.0)
        )
    for reject in rejects:
        inbox_mod.insert_inbox(
            conn, conversation_id=conversation_id,
            sender_userid=sender_uid, sender_name=sender_name,
            raw_text=reject.raw_line, channel=None, store_name=None,
            business_date=None, sales_amount=None, status="rejected",
            reject_reason=reject.reason, now=now,
        )

    reply = build_fill_reply(
        entries=recorded, overwritten=overwritten, rejects=rejects,
        progress_lines=_progress_lines(conn, entries, diffs),
    )
    return IntakeOutcome(
        "recorded", reply, region=region_cfg.region,
        value=[e.amount for e, _ in recorded], overwritten=overwritten,
    )
