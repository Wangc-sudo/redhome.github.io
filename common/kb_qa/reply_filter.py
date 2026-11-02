# -*- coding: utf-8 -*-
"""答复出口过滤：规则去无关段 + 表示规范化 + 超长 LLM 裁切（2026-11-02）。

方案：docs/小易答复相关性过滤与SKU维度查询方案-2026-10-29.md §4 B3。

背景（2026-10-29 客服群截图）：agent 自由文本原文直发，答复里混进了
「⚠️ 两点特别说明」长解释段、其它店铺「供参考」逐店清单、以及
``has_previous_data=false`` 这类内部字段名——客服要的是能直接转发的一
句话结论。

三道闸，全部 fail-open：
1. 规则过滤（零成本，先跑）：问句回显行删除、⚠️/说明类段落超两行压成
   首句、与问句无交集的「供参考」行删除；
2. 表示规范化（确定性改写）：内部字段名/协议词掩蔽成中文或删除；缺数
   语境里的「0 条 / 0 元」改写为「无数据」（缺数 ≠ 0）；
3. 长度门禁：过滤后仍超 600 字 → 一次 LLM 裁切（只删不写），失败或
   裁切后没变短就用规则过滤结果。

observe/enforce 双态：``KB_REPLY_FILTER_MODE``（默认 observe）只算 diff
落日志不替换，抽检后切 enforce——过滤是「删用户看到的内容」，先观察
再生效，避免误删不可见。
"""

import os
import re

MODE_OFF = "off"
MODE_OBSERVE = "observe"
MODE_ENFORCE = "enforce"

#: 环境变量（文件 config 未就绪前的唯一真源，与 kb_qa 既有 env 同层）。
MODE_ENV = "KB_REPLY_FILTER_MODE"

#: 触发 LLM 裁切的长度阈值（字）。
MAX_CHARS = 600

_ECHO_RE = re.compile(
    r"^\s*(问题|提问|问句|你的问题|用户问题|你问的|用户问)\s*[:：]")
_NOTE_HEAD_RE = re.compile(
    r"^\s*[-•*]?\s*(⚠️|⚠|注意|说明|提示|备注|特别说明|温馨提示|注)\s*[:：]?")
_SUB_ITEM_RE = re.compile(r"^\s*(?:[-•*]|\d+[.、)])\s*\S")
_REFERENCE_WORDS = (
    "供参考", "顺便", "另外", "其他店铺", "其它店铺", "其他渠道",
    "其它渠道", "其他商品", "其它商品", "其他区域", "其它区域",
)
_CHANNEL_NAMES = (
    "京东", "天猫", "猫超", "拼多多", "抖音", "直播", "快手", "视频号",
    "即时零售", "私域", "丰享", "得物", "微盟",
)
_QUOTED_RE = re.compile(r"[「【\"']([^」】\"']{2,30})[」】\"']")

#: 内部字段名 → 客服看得懂的说法（2026-10-29 截图实证 leaked 字段）。
_INTERNAL_MAP = {
    "has_previous_data": "去年同期数据",
    "requires_clarification": "待确认",
    "current_period": "本期",
    "previous_period": "去年同期",
    "current_sales_amount": "本期销售额",
    "previous_sales_amount": "去年同期销售额",
    "current_row_count": "本期订单数",
    "previous_row_count": "去年同期订单数",
    "delta_amount": "差额",
    "yoy_rate": "同比",
    "channel_mapping": "渠道口径",
    "source_record_id": "记录号",
    "valid_channels": "可选渠道",
    "valid_scopes": "可选区域",
}
_ASSIGN_RE = re.compile(
    r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\s*[=:]\s*[^\s，。；、]+")
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
#: 删除/替换后残留的空括号与重复逗号。
_PUNCT_CLEANUPS = (
    (re.compile(r"\(\s*[，,；;]"), "("),
    (re.compile(r"[（(]\s*[，,；;]"), "（"),
    (re.compile(r"[，,；;]\s*[）)]"), "）"),
    (re.compile(r"[，,]{2,}"), "，"),
)

_MISSING_WORDS = (
    "无数据", "没有数据", "暂无", "未提供", "未找到", "没查到", "查不到",
    "缺少", "缺失", "为空", "无记录",
)
_ZERO_COUNT_RE = re.compile(r"(?<![\d.])0\s*(条|笔|单)")
_ZERO_AMOUNT_RE = re.compile(r"(?<![\d.])0\s*元(?![\d])")
_MISSING_TEXT = "无数据"

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def env_mode(environ=None):
    """当前过滤模式；非法值回落 observe（过滤宁慢不误）。"""
    raw = ((environ or os.environ).get(MODE_ENV) or "").strip().lower()
    if raw in (MODE_OFF, MODE_ENFORCE):
        return raw
    return MODE_OBSERVE


# ---------------------------------------------------------------------------
# 1. 规则过滤
# ---------------------------------------------------------------------------

def _drop_echo(lines):
    return [line for line in lines if not _ECHO_RE.match(line)]


def _compress_notes(lines):
    """⚠️/说明/注意 段落超过两行 → 只留首句（逐条编号一并视为该段）。"""
    result = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if not _NOTE_HEAD_RE.match(line):
            result.append(line)
            index += 1
            continue
        block = [line]
        cursor = index + 1
        while cursor < total and lines[cursor].strip():
            if _NOTE_HEAD_RE.match(lines[cursor]):
                block.append(lines[cursor])
            elif _SUB_ITEM_RE.match(lines[cursor]):
                block.append(lines[cursor])
            else:
                break
            cursor += 1
        result.extend(block[:1] if len(block) > 2 else block)
        index = cursor
    return result


def _bigrams(text):
    chars = "".join(_CJK_RE.findall(text or ""))
    if len(chars) < 2:
        return {chars} if chars else set()
    return {chars[i:i + 2] for i in range(len(chars) - 1)}


def _line_related(line, question):
    """该行是否与问句谈同一个对象（引号名/渠道名/字符重合三重判定）。"""
    entities = list(_QUOTED_RE.findall(line))
    entities += [name for name in _CHANNEL_NAMES if name in line]
    for entity in entities:
        if entity and entity in question:
            return True
    return bool(_bigrams(line) & _bigrams(question))


def _drop_unrelated_reference(lines, question):
    kept = []
    for line in lines:
        if any(word in line for word in _REFERENCE_WORDS) and not _line_related(
                line, question):
            continue
        kept.append(line)
    return kept


# ---------------------------------------------------------------------------
# 2. 表示规范化
# ---------------------------------------------------------------------------

def _normalize_internals(text):
    """内部字段名/协议词 → 中文说法（未知 snake_case 直接删）。"""

    def replace_assign(match):
        return _INTERNAL_MAP.get(match.group(1), "")

    def replace_token(match):
        return _INTERNAL_MAP.get(match.group(0), "")

    text = _ASSIGN_RE.sub(replace_assign, text)
    text = _SNAKE_RE.sub(replace_token, text)
    for pattern, replacement in _PUNCT_CLEANUPS:
        text = pattern.sub(replacement, text)
    return re.sub(r"[ \t]{2,}", " ", text)


def _normalize_missing(text):
    """缺数语境里的 0 → 「无数据」（缺数 ≠ 0，2026-10-29「同比：0 条」）。

    行内已有「无数据」时直接删掉 0 短语，避免出现「无数据（无数据）」。
    """
    lines = []
    for line in text.splitlines():
        if any(word in line for word in _MISSING_WORDS):
            already = _MISSING_TEXT in line
            line = _ZERO_COUNT_RE.sub("" if already else _MISSING_TEXT, line)
            line = _ZERO_AMOUNT_RE.sub("" if already else _MISSING_TEXT, line)
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. 长度门禁（可选 LLM 裁切）
# ---------------------------------------------------------------------------

_TRIM_PROMPT = (
    "下面是给客服的答复。请只删除与问题无关的段落，保留全部数字、日期、"
    "单位与结论，不要改写措辞、不要新增内容，直接输出处理后的答复。\n\n"
    "问题：{question}\n\n答复：\n{reply}"
)


def _llm_trim(llm, question, reply):
    """一次裁切；失败/带 DSML 残留/没变短 → None（用规则过滤结果）。"""
    try:
        trimmed = llm.chat(
            [{"role": "user",
              "content": _TRIM_PROMPT.format(question=question, reply=reply)}],
            max_tokens=None, temperature=0)
    except Exception:
        return None
    if not trimmed or "DSML" in trimmed:
        return None
    trimmed = trimmed.strip()
    return trimmed if trimmed and len(trimmed) < len(reply) else None


# ---------------------------------------------------------------------------
# 出口
# ---------------------------------------------------------------------------

def filter_reply(question, reply, focus=None, *, llm=None):
    """返回过滤后的答复；无变化则与入参相同（调用方可据此判定 diff）。"""
    text = reply or ""
    if not text.strip():
        return text
    lines = _drop_echo(text.splitlines())
    lines = _compress_notes(lines)
    lines = _drop_unrelated_reference(lines, question or "")
    text = _normalize_missing(_normalize_internals("\n".join(lines)))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if llm is not None and len(text) > MAX_CHARS:
        text = _llm_trim(llm, question or "", text) or text
    return text


def apply_reply_filter(question, reply, focus=None, *, mode=None, llm=None,
                       log=None, environ=None):
    """按模式应用过滤；observe 只记 diff 不替换（fail-open）。

    mode 为 None 时读 ``KB_REPLY_FILTER_MODE``。返回最终发出的文本。
    """
    if not reply:
        return reply
    mode = mode or env_mode(environ)
    if mode == MODE_OFF:
        return reply
    try:
        filtered = filter_reply(question, reply, focus, llm=llm)
    except Exception:
        return reply
    if filtered == reply or not filtered:
        return reply
    if mode == MODE_OBSERVE:
        if log is not None:
            log(f"kb qa: reply filter diff -{len(reply) - len(filtered)} chars")
        return reply
    if log is not None:
        log(f"kb qa: reply filtered -{len(reply) - len(filtered)} chars")
    return filtered
