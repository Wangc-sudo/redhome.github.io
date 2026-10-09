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
   容错（2026-10-09，rejected 审计驱动）：包含互查失败后查店名别名表
   ``_STORE_ALIASES``（归一化键 → 正式店名，尊重渠道作用域；只登记
   实测高频错名，不做模糊归一化匹配——短品牌词会错配，宁缺毋滥）。

``/店铺映射表`` 指令回复「渠道｜编号｜店名｜负责人」全表（可贴群公告）。

权限（2026-09-29 收口裁决）：**只能报自己负责的店，群管理员可代填**。
身份 = 群昵称 ↔ ``owners_json`` 负责人名匹配（精确 → casefold → 包含
互查唯一；回调无 unionId 字段，昵称是跨组织场景唯一可用键，群规要求
昵称=本人姓名）。无法识别 → 拒收并引导改昵称；报他人店 → 拒收并列出
本人店铺；``is_admin``（群管理员）不受限。拒收一律落 rejected 审计。

门禁场景依据：19 位负责人不在本企业组织（P0 探针证实），不查
``dim_robot_member``。默认业务日 = 昨天，日期前缀补填最长 30 天；
同业务键重发即覆盖。
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
    owners: dict  # name -> [(channel, number, store)]（权限面：负责人∪代填报人）
    owners_casefold: dict = field(default_factory=dict)
    # 展示面（v3 角色）：仅名册切源行携带；legacy 行缺省 → 全部按负责人展示。
    store_owner_names: dict = field(default_factory=dict)  # store -> [负责人名]
    store_deputies: dict = field(default_factory=dict)  # store -> [代填报人名]


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

    def sort_key(item):
        store, target = item
        if target is None:
            return (1, 0.0, store)
        return (0, -float(target), store)

    if any(row.get("store_no") is not None for row in rows):
        # 编号冻结（2026-10-07 统一管理方案 §6-A）：名册 store_no 为准——
        # 月目标变化不再洗牌；未编号的店按目标降序排在该渠道已编号之后。
        tail = defaultdict(list)
        for row in rows:
            store = str(row.get("store_name") or "").strip()
            if not store:
                continue
            channel = str(row.get("channel") or "").strip()
            store_no = row.get("store_no")
            if store_no is not None:
                numbers[(channel, int(store_no))] = store
                store_numbers[store] = (channel, int(store_no))
            else:
                tail[channel].append((store, row.get("monthly_target")))
        for channel, items in tail.items():
            used = [no for (ch, no) in numbers if ch == channel]
            for idx, (store, _target) in enumerate(
                sorted(items, key=sort_key), max(used, default=0) + 1
            ):
                numbers[(channel, idx)] = store
                store_numbers[store] = (channel, idx)
    else:
        for channel, items in by_channel.items():
            for idx, (store, _target) in enumerate(
                sorted(items, key=sort_key), 1
            ):
                numbers[(channel, idx)] = store
                store_numbers[store] = (channel, idx)
    owners = defaultdict(list)
    store_owner_names = {}
    store_deputies = {}
    for row in rows:
        store = str(row.get("store_name") or "").strip()
        if not store or store not in store_numbers:
            continue
        channel, number = store_numbers[store]
        for entry in parse_owner_entries(row.get("owners_json")):
            owners[entry["name"]].append((channel, number, store))
        if row.get("owner_names"):
            store_owner_names[store] = sorted(row["owner_names"])
        if row.get("deputy_names"):
            store_deputies[store] = sorted(row["deputy_names"])
    owners_casefold = {name.casefold(): name for name in owners}
    return Roster(
        stores=stores, numbers=numbers, store_numbers=store_numbers,
        owners=dict(owners), owners_casefold=owners_casefold,
        store_owner_names=store_owner_names, store_deputies=store_deputies,
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


#: 店名归一化（别名键用）：去渠道前缀与店型后缀，不改正式店名。
_STORE_CHANNEL_PREFIXES = ("PDD", "JD", "TM", "DY", "MC")
_STORE_SUFFIXES = ("旗舰店", "专卖店", "专营店")

#: 店名别名（归一化后键 → 正式店名）：包含互查救不回来的实测高频错名。
#: 来源 = rejected 审计（2026-10-09，17 条「未找到门店」）。新增条目必须
#: 有 rejected 审计依据，键经 :func:`_normalize_store_name` 归一。
_STORE_ALIASES = {
    "购喝": "JD购喝",  # 「购喝旗舰店」（王城 09-30 / 王蕊 10-07）
    "金沙习水村": "JD金沙专卖店",  # 「金沙习水村专卖店」（王蕊 10-07）
    "习酒习水村": "JD习水村习酒专卖店",  # 词序颠倒（王蕊 10-07）
    "习酒平澜路习水村": "PDD习酒平澜路专卖店",  # ×12（王城/李帆/杨美聪）
}


def _normalize_store_name(text):
    """店名归一：去渠道前缀与店型后缀（别名键专用）。"""
    name = str(text or "").strip()
    upper = name.upper()
    for prefix in _STORE_CHANNEL_PREFIXES:
        if upper.startswith(prefix):
            name = name[len(prefix):]
            break
    for suffix in _STORE_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.strip()


def _match_store(token, *, roster, channel_hint=None):
    """店名匹配 → ``(store, channel)`` / ``("AMBIGUOUS", candidates)`` / ``None``。

    两级：包含互查 → 店名别名（2026-10-09 容错扩展，别名命中须落在
    渠道作用域内；不做模糊归一化匹配，短品牌词错配风险宁缺毋滥）。
    """
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
    canonical = _STORE_ALIASES.get(_normalize_store_name(token))
    if canonical and canonical in scope:
        return canonical, scope[canonical]
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


def resolve_sender_name(nick, roster):
    """群昵称 → 负责人名（精确 → casefold → 包含互查唯一）；失败 ``None``。

    回调没有 unionId 字段，昵称是跨组织场景唯一可用的身份键——依赖
    群规「昵称 = 本人姓名」。包含互查防「饶佳君-习酒」式后缀昵称，
    多候选（如单姓）不信任。
    """
    name = (nick or "").strip()
    if not name:
        return None
    if name in roster.owners:
        return name
    canonical = roster.owners_casefold.get(name.casefold())
    if canonical:
        return canonical
    hits = [n for n in roster.owners if name in n or n in name]
    return hits[0] if len(hits) == 1 else None


def store_owner_names(roster):
    """``{store: {负责人名}}``（权限校验反查）。"""
    index = defaultdict(set)
    for name, entries in roster.owners.items():
        for _channel, _number, store in entries:
            index[store].add(name)
    return dict(index)


def check_entry_permission(entry, *, sender_name, is_admin, roster,
                           store_owners):
    """单条填报权限 → ``None``（放行）或拒因文案。

    群管理员全放；负责人只能报名下店（含共管）；渠道级行按该渠道全部
    负责人集合判定；未识别身份一律拒。
    """
    if is_admin:
        return None
    if sender_name is None:
        return (
            "无法识别你的身份（群昵称不在负责人名单）。"
            "请将群昵称改为本人姓名后重试，或联系群管理员代报。"
        )
    if entry.store_name is None:
        allowed = {
            name
            for store, names in store_owners.items()
            if roster.stores.get(store) == entry.channel
            for name in names
        }
    else:
        allowed = store_owners.get(entry.store_name) or set()
    if sender_name in allowed:
        return None
    mine = "、".join(
        store for _c, _n, store in roster.owners.get(sender_name, [])
    )
    return (
        f"「{entry.store_name or entry.channel}」不是你负责的店铺"
        f"（你负责：{mine or '无'}）。如需代报请联系群管理员。"
    )


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
        "/店铺映射表 — 查看全部编号与负责人\n"
        "权限：负责人/代填报人可报名下店铺（群昵称须为本人姓名）"
    )


def build_format_hint():
    return (
        "⚠️ 没看懂～格式：[日期] 渠道 编号/店名/负责人 金额\n"
        "例如：京东 1 15867（报昨天）；9.27 天猫 3 82059（补填）\n"
        "查编号：/店铺映射表"
    )


def build_mapping_table(roster):
    """``/店铺映射表`` 全表：渠道｜编号｜店名｜负责人/代填报人（编号升序）。

    展示（2026-10-07 运维裁决）：负责人与代填报人分列标识、左对齐；
    同店多个负责人时换行、续行用全角空格对齐姓名列（"　负责人："≈5
    全角宽）。legacy 行（名册未切源）owners_json 全部按负责人展示。
    """
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
            deputies = list(roster.store_deputies.get(store) or ())
            if store in roster.store_owner_names or deputies:
                owners = list(roster.store_owner_names.get(store) or ())
            else:
                owners = sorted(owner_names.get(store) or [])
            deputy_text = (
                f"｜代填报人：{'、'.join(deputies)}" if deputies else ""
            )
            if len(owners) <= 1:
                if owners:
                    lines.append(
                        f"{number}={store}（负责人：{owners[0]}{deputy_text}）"
                    )
                elif deputies:
                    lines.append(
                        f"{number}={store}（代填报人：{'、'.join(deputies)}）"
                    )
                else:
                    lines.append(f"{number}={store}（—）")
                continue
            lines.append(f"{number}={store}")
            for index, name in enumerate(owners):
                prefix = "　负责人：" if index == 0 else "　　　　　"
                lines.append(f"{prefix}{name}")
            if deputies:
                lines.append(f"　代填报人：{'、'.join(deputies)}")
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
    """填报名册源行（fact_channel_store_target 全店，含目标与负责人）。

    名册切源（2026-10-06 运维裁决）：负责人归属以 ``dim_report_roster``
    为准（ops-web 管理面），月目标仍取 ``fact_channel_store_target``；
    名册表 qudao 无启用记录时回退 legacy ``owners_json``（种子导入前的
    行为保持不变）。
    """
    import json as _json

    from common.public_data import report_roster

    rows = _fetch_all(
        conn,
        "SELECT `store_name`, `channel`, `monthly_target`, `owners_json` "
        "FROM `fact_channel_store_target`",
    )
    role_map = report_roster.fetch_store_role_map(conn, "qudao")
    if not role_map:
        return rows
    number_map = report_roster.fetch_store_numbers(conn, "qudao")
    switched = []
    for row in rows:
        store = str(row.get("store_name") or "").strip()
        buckets = role_map.get(store) or {"owners": [], "deputies": []}
        # 权限面（owners_json）：负责人 ∪ 代填报人皆可填（v3 语义不变）；
        # 展示面（owner_names/deputy_names）：映射表分列标识。
        fillable = sorted(set(buckets["owners"]) | set(buckets["deputies"]))
        switched_row = dict(row)
        switched_row["owners_json"] = (
            _json.dumps(fillable, ensure_ascii=False) if fillable else None
        )
        switched_row["owner_names"] = buckets["owners"]
        switched_row["deputy_names"] = buckets["deputies"]
        switched_row["store_no"] = number_map.get(store, (None, None))[1]
        switched.append(switched_row)
    return switched


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
                        conversation_id, now, is_admin=False):
    """处理一条渠道群填报消息。不写连接 commit（由调用方提交）。

    权限（2026-09-29 收口）：*is_admin*（群管理员）可代填任何店；其余
    按群昵称 ↔ 负责人匹配结果只能报名下店，拒收落 rejected 审计。

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

    # 权限分流：本人名下店放行，其余改判拒收（落 rejected 审计）。
    sender_owner = resolve_sender_name(sender_name, roster)
    owners_by_store = store_owner_names(roster)
    permitted = []
    for entry in entries:
        deny = check_entry_permission(
            entry, sender_name=sender_owner, is_admin=is_admin,
            roster=roster, store_owners=owners_by_store,
        )
        if deny is None:
            permitted.append(entry)
        else:
            rejects.append(FillReject(entry.raw_line, deny))
    entries = permitted

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
