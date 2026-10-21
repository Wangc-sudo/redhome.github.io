# -*- coding: utf-8 -*-
"""对话上下文（多轮）：进程内会话存储。

定位（2026-10-21 对话层升级）：客服群问答从「每条消息独立」升级为
「同人同群最近 N 轮有记忆」——指代（它/那款/价格呢）与追问靠历史
由 LLM 在二次解析与渲染时消化（见 llm.extract_query/render_*_answer
的 history 参数）。

设计取舍：
- 进程内 dict + deque：kb-gateway 单进程长驻，重启丢上下文可接受
  （对话本来就是短期的）；不建表——避免为 10 分钟生命周期的数据落库。
- 键 = (conversation_id, sender_id)：同群多人各自独立，互不串话。
- TTL 过期：超时后历史作废（昨天的「它」不该指代到今天）。
"""

import time
from collections import deque

#: 默认保留轮数与过期时间（秒）。4 轮足够覆盖「问→追问→再追问」；
#: 10 分钟是客服群一个话题的自然寿命。
DEFAULT_MAX_TURNS = 4
DEFAULT_TTL_SECONDS = 600

#: 历史进 LLM prompt 时每轮问答的截断长度（控 token）。
SNIPPET_CHARS = 120


class ConversationStore:
    """(conversation_id, sender_id) → 最近 N 轮对话。"""

    def __init__(self, *, max_turns=DEFAULT_MAX_TURNS,
                 ttl_seconds=DEFAULT_TTL_SECONDS, now=time.monotonic):
        self._max_turns = max_turns
        self._ttl = ttl_seconds
        self._now = now
        self._turns = {}

    @staticmethod
    def key(conversation_id, sender_id):
        return (conversation_id or "", sender_id or "")

    def history(self, key):
        """未过期历史（按时间正序）；无则 []。"""
        entry = self._turns.get(key)
        if not entry:
            return []
        turns, ts = entry
        if self._now() - ts > self._ttl:
            del self._turns[key]
            return []
        return list(turns)

    def record(self, key, *, question, answer, category=None, record_id=None):
        """记一轮（回复为空的静默轮不记）。"""
        if not answer:
            return
        turns, _ = self._turns.get(key, (deque(maxlen=self._max_turns), 0))
        turns.append({
            "q": (question or "")[:SNIPPET_CHARS],
            "a": (answer or "")[:SNIPPET_CHARS],
            "category": category,
            "record_id": record_id,
        })
        self._turns[key] = (turns, self._now())


def format_history(history):
    """历史 → prompt 文本段（最近在前）。空调用方不拼接该段。"""
    if not history:
        return ""
    lines = ["# 对话历史（最近的在前）"]
    for turn in reversed(history):
        lines.append(f"客服：{turn['q']}")
        lines.append(f"助手：{turn['a']}")
    return "\n".join(lines)
