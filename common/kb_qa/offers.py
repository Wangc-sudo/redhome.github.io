# -*- coding: utf-8 -*-
"""助手提议（offer）状态化：纯函数，零 IO。

背景（2026-10-30 上下文串台事故）：助手答完常带一句提议（「需要我把
绍兴、万科、渠道等其他区域一起拉出来做个排名吗？」），用户回「可以」
这种零语义确认词时，正确含义 100% 来自紧邻上一轮的提议；但此前确认词
被原样丢进 agent，由 LLM 在 6 轮历史里自由猜——历史旧话题（商品/质检
报告）词频压过最近一轮，模型抓错锚点答非所问。

架构定位：对话状态分三层——
1. 未决状态（本模块的 offer、clarify 的 pending_candidates）：一等公民，
   确认词只消费它，不进自由发挥；
2. 近期轮次 history：带卫生规则（TTL/噪声过滤）供 LLM 参考；
3. LLM 自由层：前两层都 miss 时才工作。

本模块负责 1 的纯函数部分：识别助手回复末尾的提议（detect_offer）、
识别用户纯确认/拒绝（parse_ack）、把确认改写成显式任务文本
（build_confirmed_text）。存取由 context.py（群内）与 kb_web.sessions
（Web）实现，消费语义与 clarify pending 一致：任一条新消息都消费，
说了别的自然作废不污染后续。
"""

import re

#: 纯确认词（剥掉空白与标点后的完整匹配）。只收零语义词——带任何实质
#: 内容的回复（「可以，顺便看下绍兴」）不属于本层，走正常链路。
_CONFIRM_WORDS = frozenset((
    "可以", "可以啊", "可以呀", "好", "好的", "好呀", "好啊", "好嘞",
    "行", "嗯", "嗯嗯", "嗯哪", "是", "是的", "对", "对的", "要", "要得",
    "ok", "okay", "来吧", "继续",
))

#: 纯拒绝词：消费掉 offer（自然作废），不做短路改写。
_REJECT_WORDS = frozenset((
    "不用", "不用了", "不需要", "不了", "不要", "先不用", "暂时不用",
    "先不要", "否",
))

_STRIP_RE = re.compile(r"[\s，。！？!?,．.、~～]+")

#: 提议句形态（匹配回复最后一个非空行）：「需要我…吗？」「要我…吗？」
#: 「要不要(我)…吗？」「我可以…，需要吗？」。action 为去掉提议框架后的
#: 动作本体，可直接作为后续任务指令。
_OFFER_RE = re.compile(
    r"(?:需要我帮你|要我帮你|需要我|要我|我可以帮你|我可以|要不要我帮你"
    r"|要不要我|要不要)"
    r"(?P<action>[^？?]{2,120}?)(?:吗|呢)？?$")


def parse_ack(text):
    """纯确认/拒绝识别：返回 "confirm" / "reject" / None。

    完整匹配（剥标点空白后）：「可以」「好的」「嗯」→ confirm；
    「不用」「先不要」→ reject；其他（含带实质内容的句子）→ None。
    """
    normalized = _STRIP_RE.sub("", text or "").lower()
    if not normalized:
        return None
    if normalized in _CONFIRM_WORDS:
        return "confirm"
    if normalized in _REJECT_WORDS:
        return "reject"
    return None


def detect_offer(reply):
    """识别助手回复末尾的提议，返回 {"question": 原句, "action": 动作}。

    只看最后一个非空行（提议几乎总在结尾）；非提议返回 None。
    """
    if not reply:
        return None
    last_line = ""
    for line in reply.strip().splitlines():
        if line.strip():
            last_line = line.strip()
    if not last_line:
        return None
    match = _OFFER_RE.search(last_line)
    if not match:
        return None
    action = match.group("action").strip("，,。 　")
    if len(action) < 2:
        return None
    return {"question": last_line, "action": action}


def build_confirmed_text(offer, last_question=""):
    """确认 → 显式任务文本（替代原确认词交给下游问答链路）。

    例：offer.action=「把绍兴、万科、渠道等其他区域一起拉出来做个排名」、
    last_question=「杭州昨天销售额」→
    「把绍兴、万科、渠道等其他区域一起拉出来做个排名
     （这是上一轮「杭州昨天销售额」的后续操作，用户已确认）」
    """
    action = (offer or {}).get("action") or ""
    if last_question:
        return (f"{action}（这是上一轮「{last_question}」的后续操作，"
                f"用户已确认）")
    return action
