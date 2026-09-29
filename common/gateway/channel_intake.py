# -*- coding: utf-8 -*-
"""渠道日销机器人填报（2026-09-29 方案 P1.1，region=qudao）。

群内一句话填报替代手工 AI 表台账：``京东 1 15867`` → 解析 →
写 ``channel_sales_robot_inbox``（append-only）→ 即时回执。extract
归并进 fact 由 :mod:`common.public_data.channel_robot_inbox` 承担。

P1.1 三键定位（2026-09-29 运维定稿，实测 19 负责人 12 人管多店）：

1. **渠道+编号**（主键，``京东 1 15867``）：编号 = 渠道内按月目标降序
   （无目标新店排尾），从月目标表自动派生，零维护；
2. **负责人**（``饶佳君`` 或 ``饶佳君 京东``）：归属来自月目标表
   ``owners_json``；单店直达，多店回执给个性化编号引导（歧义→教学）；
3. **店名模糊**（``京东 购喝 15867``）：包含互查，代填/管理精确通道。

``/店铺映射表`` 指令回复「渠道｜编号｜店名｜负责人」全表（可贴群公告）。
门禁 = 群成员即可填（19 位负责人不在本企业组织，P0 探针证实）；店名/
编号在文本里，渠道对接人代填与门店自填同一通道。默认业务日 = 昨天，
日期前缀补填最长 30 天；同业务键重发即覆盖。
"""

import contextlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from common.daily_robot.channel_missing import parse_owner_entries
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

#: 渠道词位置：开头或结尾（「京东 3」「饶佳君 京东」都认）。
_NUMBER_RE = re.compile(r"^\d{1,3}$")


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


@dataclass(frozen=True)
class Roster:
    """填报名册三视图（编号 / 负责人 / 店名）。"""

    stores: dict  # store -> channel
    numbers: dict  # (channel, number) -> store
    store_numbers: dict  # store -> (channel, number)
    owners: dict  # name -> [(channel, number, store)]
    owners_casefold: dict = field(default_factory=dict)


def build_roster(rows):
    """月目标表行 → :class:`Roster`（纯逻辑）。

    编号：渠道内按月目标降序（填报人记大店；无目标新店排尾、店名排序
    保证确定性）。负责人索引同名取 ``owners_json`` 原样，另建
    casefold 索引容错英文花名大小写。
    """
    stores = {}
    by_channel = defaultdict(list)
    for row in rows:
        store = str(row.get("store_name") or "").strip()
        if not store:
            continue
        channel = str(row.get("channel") or "").strip()
        stores[store] = channel
        by_channel[channel].append((store, row.get("monthly_target")))
    numbers = {}
    store_numbers = {}
    for channel, items in by_channel.items():
        def sort_key(item):
            store, target = item
            if target is None:
                return (1, 0.0, store)
            return (0, -float(target), store)

        for idx, (store, _target) in enumerate(sorted(items, key=sort_key), 1):
            numbers[(channel, idx)] = store
            store_numbers[store] = (channel, idx)
    owners = defaultdict(list)
    for row in rows:
        store = str(row.get("store_name") or "").strip()
        if not store or store not in store_numbers:
            continue
        channel, number = store_numbers[store]
        for entry in parse_owner_entries(row.get("owners_json")):
            owners[entry["name"]].append((channel, number, store))
    owners_casefold = {name.casefold(): name for name in owners}
    return Roster(
        stores=stores, numbers=numbers, store_numbers=store_numbers,
        owners=dict(owners), owners_casefold=owners_casefold,
    )


# ---------------------------------------------------------------------------
# 解析（纯逻辑）
# ---------------------------------------------------------------------------

def _clean_text(text):
    return _ZERO_WIDTH_RE.sub("", text or "")


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
    """店名包含互查 → ``(store, channel)`` / ``("AMBIGUOUS", candidates)`` / ``None``。"""
    scope = roster.stores
    if channel_hint:
        scoped = {s: c for s, c in roster.stores.items() if c == channel_hint}
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


def _owner_stores(name, roster, *, channel_hint=None):
    """负责人 → 名下店铺 ``[(channel, number, store)]``（精确名 + 大小写容错）。"""
    canonical = roster.owners_casefold.get(name.casefold())
    if canonical is None:
        return None
    entries = roster.owners[canonical]
    if channel_hint:
        entries = [e for e in entries if e[0] == channel_hint]
    return entries, canonical


def _owner_guide(name, entries, roster):
    """多店负责人的个性化编号引导文案。"""
    parts = [f"{channel} {number}={store}" for channel, number, store in entries]
    example_channel, example_number, _ = entries[0]
    return (
        f"「{name}」负责 {len(entries)} 家店：{'；'.join(parts)}\n"
        f"请发：{example_channel} {example_number} 金额"
        f"（渠道 编号 金额；全部编号见 /店铺映射表）"
    )


def _strip_channel_word(token_text):
    """剥开头/结尾渠道词 → ``(channel_hint, rest)``；无渠道词 → ``(None, 原文)``。"""
    for word in CHANNEL_WORDS:
        if token_text.startswith(word):
            return word, token_text[len(word):].strip(" ,，:：")
        if token_text.endswith(word):
            return word, token_text[: -len(word)].strip(" ,，:：")
    return None, token_text


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
        return FillReject(raw_line, "缺少店名/渠道词/负责人")

    # 整段店名匹配优先（「私域 0」店名=渠道词、「朴朴 100」直达）。
    whole = token_text.replace(" ", "")
    matched = _match_store(whole, roster=roster)
    if matched is not None and matched[0] != "AMBIGUOUS":
        store, channel = matched
        return FillEntry(
            business_date, channel, store, amount,
            None if whole == store else whole, raw_line,
        )

    channel_hint, token = _strip_channel_word(whole)

    # 渠道词 + 纯编号（主键）：京东 1 15867
    if channel_hint and _NUMBER_RE.match(token):
        store = roster.numbers.get((channel_hint, int(token)))
        if store is None:
            return FillReject(
                raw_line,
                f"「{channel_hint} {token}」编号不存在（发 /店铺映射表 查看）",
            )
        return FillEntry(business_date, channel_hint, store, amount,
                         None, raw_line)

    # 渠道级填报：仅渠道词（猫超式）
    if channel_hint and not token:
        if channel_hint in CHANNEL_LEVEL_OK:
            return FillEntry(business_date, channel_hint, None, amount,
                             None, raw_line)
        return FillReject(
            raw_line, f"「{channel_hint}」需带店名或编号（仅猫超支持渠道级填报）"
        )

    # 负责人（可带渠道词）：饶佳君 [京东] 15867
    hit = _owner_stores(token, roster, channel_hint=channel_hint)
    if hit is not None:
        entries, canonical = hit
        if len(entries) == 1:
            channel, _number, store = entries[0]
            return FillEntry(business_date, channel, store, amount,
                             canonical, raw_line)
        if entries:
            return FillReject(
                raw_line, _owner_guide(canonical, entries, roster)
            )

    # 店名匹配（渠道内优先）
    matched = _match_store(token, roster=roster, channel_hint=channel_hint)
    if matched is None:
        return FillReject(raw_line, f"未找到门店「{token}」（发 /店铺映射表 查看编号）")
    if matched[0] == "AMBIGUOUS":
        hint = "请加渠道词，或" if not channel_hint else "请"
        return FillReject(
            raw_line,
            f"匹配多家店：{'、'.join(matched[1])}，{hint}用「渠道 编号」"
            f"（见 /店铺映射表）",
        )
    store, channel = matched
    return FillEntry(
        business_date, channel, store, amount,
        None if token == store else token, raw_line,
    )


def parse_fill_text(text, *, today, roster):
    """填报文本 → ``(entries, rejects)``；不含任何金额数字 → ``([], [])``。

    *roster*：:class:`Roster`（:func:`build_roster` 构建）。按行解析
    （一条一行一店一金额）；剥 @提及 与零宽字符；``/`` 开头由调用方
    拦截（辅助指令），这里按普通行处理。
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
# 回执与指令文案（纯逻辑）
# ---------------------------------------------------------------------------

def build_channel_help():
    return (
        "🤖 渠道日销填报（默认报昨天）：\n"
        "  京东 1 15867（渠道+编号，推荐）\n"
        "  饶佳君 15867（负责人，单店直达）\n"
        "  京东 购喝 15867（店名也行）\n"
        "  猫超 731033（猫超可只报渠道）\n"
        "补填带日期：9.27 京东 1 82059\n"
        "当天无销售报 0；填错了重发一条同店同日即可覆盖～\n"
        "/店铺映射表 — 查看全部编号与负责人"
    )


def build_format_hint():
    return (
        "⚠️ 没看懂～格式：[日期] 渠道 编号/店名/负责人 金额\n"
        "例如：京东 1 15867（报昨天）；9.27 天猫 3 82059（补填）\n"
        "查编号：/店铺映射表"
    )


def build_mapping_table(roster):
    """``/店铺映射表`` 全表：渠道｜编号｜店名｜负责人（目标降序，新店标尾注）。"""
    by_channel = defaultdict(list)
    for store, (channel, number) in roster.store_numbers.items():
        by_channel[channel].append((number, store))
    owner_names = {}
    for name, entries in roster.owners.items():
        for _channel, _number, store in entries:
            owner_names.setdefault(store, []).append(name)
    lines = ["📋 店铺映射表（填报：渠道 编号 金额）"]
    for channel in CHANNEL_WORDS:
        items = by_channel.get(channel)
        if not items:
            continue
        lines.append(f"【{channel}】")
        for number, store in sorted(items):
            names = "、".join(owner_names.get(store) or []) or "—"
            lines.append(f"{number}={store}（{names}）")
    return "\n".join(lines)


def _fmt_wan(value):
    number = float(value)
    if abs(number) >= 10000:
        return f"{number / 10000:.1f}万"
    return _fmt_amount(number)


def build_fill_reply(*, entries, rejects, progress_lines, roster):
    """回执。entries = ``[(entry, prev_amount|None)]``；店名前回显编号。"""
    lines = [f"✅ 已记录 {len(entries)} 条渠道日销："]
    for entry, prev in entries:
        if entry.store_name is None:
            store_disp = f"{entry.channel}（渠道级）"
        else:
            _channel, number = roster.store_numbers.get(
                entry.store_name, (entry.channel, None)
            )
            store_disp = (
                f"{number}={entry.store_name}" if number else entry.store_name
            )
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
        lines.append("格式：[日期] 渠道 编号/店名/负责人 金额（查 /店铺映射表）")
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


def fetch_roster_rows(conn):
    """填报名册源行（fact_channel_store_target 全店，含目标与负责人）。"""
    return _fetch_all(
        conn,
        "SELECT `store_name`, `channel`, `monthly_target`, `owners_json` "
        "FROM `fact_channel_store_target`",
    )


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
    roster = build_roster(fetch_roster_rows(conn))
    if cleaned.startswith("/"):
        command = cleaned.lstrip("/").strip()
        if command in ("店铺映射表", "映射表", "店铺"):
            return IntakeOutcome(
                "aux", build_mapping_table(roster), region=region_cfg.region,
            )
        return IntakeOutcome(
            "aux", build_channel_help(), region=region_cfg.region,
        )

    entries, rejects = parse_fill_text(text, today=today, roster=roster)
    if not entries and not rejects:
        return IntakeOutcome("ignored", None, region=region_cfg.region)

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
    if not entries:
        return IntakeOutcome(
            "no_number",
            "\n".join(
                [f"⚠️ {len(rejects)} 条未识别："]
                + [f"「{r.raw_line}」{r.reason}" for r in rejects]
                + [build_format_hint()]
            ),
            region=region_cfg.region,
        )

    reply = build_fill_reply(
        entries=recorded, rejects=rejects, roster=roster,
        progress_lines=_progress_lines(conn, entries, diffs),
    )
    return IntakeOutcome(
        "recorded", reply, region=region_cfg.region,
        value=[e.amount for e, _ in recorded], overwritten=overwritten,
    )
