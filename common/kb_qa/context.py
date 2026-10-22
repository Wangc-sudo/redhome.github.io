# -*- coding: utf-8 -*-
"""对话上下文（多轮）：进程内会话存储。

定位（2026-10-21 对话层升级）：客服群问答从「每条消息独立」升级为
「同人同群最近 N 轮有记忆」——指代（它/那款/价格呢）与追问靠历史
由 LLM 在二次解析与渲染时消化（见 llm.extract_query/render_*_answer
的 history 参数）。

设计取舍：
- 双后端（2026-10-22 Redis 化）：配置了 Redis URL → Redis 后端
  （原生 TTL 自动过期，根治进程内版「过期键只在下次访问时删除、
  永不活跃的键常驻内存」的残留问题，且重启不丢上下文）；未配置
  → 进程内 dict + deque 兜底（语义等价，重启丢上下文可接受——
  对话本来就是短期的，不建表）。后端选择见 build_conversation_store，
  与 bi_web 卡片缓存同一 fail-open 模式：Redis 不可达只表现为
  当轮无历史，绝不影响问答主链路。
- 键 = (conversation_id, sender_id)：同群多人各自独立，互不串话。
- TTL 过期：超时后历史作废（昨天的「它」不该指代到今天）。
"""

import json
import os
import time
from collections import deque

#: 默认保留轮数与过期时间（秒）。4 轮足够覆盖「问→追问→再追问」；
#: 10 分钟是客服群一个话题的自然寿命。
DEFAULT_MAX_TURNS = 4
DEFAULT_TTL_SECONDS = 600

#: 历史进 LLM prompt 时每轮问答的截断长度（控 token）。
SNIPPET_CHARS = 120


def _turn_payload(question, answer, category, record_id, subject):
    """单轮存储载荷（进程内与 Redis 两后端同构）。

    subject（2026-10-22）：本轮命中商品名（如有），供 format_history
    透出「上文涉及商品」——指代补全不再只靠被截断的答案文本猜。
    """
    return {
        "q": (question or "")[:SNIPPET_CHARS],
        "a": (answer or "")[:SNIPPET_CHARS],
        "category": category,
        "record_id": record_id,
        "subject": subject,
    }


class ConversationStore:
    """(conversation_id, sender_id) → 最近 N 轮对话（进程内后端）。"""

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

    def record(self, key, *, question, answer, category=None, record_id=None,
               subject=None):
        """记一轮（回复为空的静默轮不记）。"""
        if not answer:
            return
        turns, _ = self._turns.get(key, (deque(maxlen=self._max_turns), 0))
        turns.append(_turn_payload(question, answer, category, record_id,
                                   subject))
        self._turns[key] = (turns, self._now())


#: Redis 键前缀（与 biweb:card 同风格，杜绝跨面混淆）。
_KEY_PREFIX = "kbqa:ctx"

#: 后端选择环境变量：KB_REDIS_URL 优先，其次复用 bi-web 的
#: PUBLIC_DATA_REDIS_URL（同一 Redis 实例，kb-gateway 经 app.env 已注入）。
_REDIS_URL_ENVS = ("KB_REDIS_URL", "PUBLIC_DATA_REDIS_URL")


class RedisConversationStore:
    """Redis 后端：原生 TTL 自动过期，键不残留；重启不丢上下文。

    存储形态：每键一个 Redis list（最新在右），record = RPUSH + LTRIM
    保留最近 max_turns 轮 + EXPIRE 滑动续期；history = LRANGE 全取。
    fail-open：任何 Redis 异常 → history 当 []、record 静默丢弃，
    问答主链路无感（与 bi_web.RedisCardCache 同一约定）。

    连接惰性：构造不连，首次读写才 from_url；client 可注入（测试）。
    """

    def __init__(self, url=None, *, client=None, max_turns=DEFAULT_MAX_TURNS,
                 ttl_seconds=DEFAULT_TTL_SECONDS):
        self._url = url
        self._client = client
        self._max_turns = max_turns
        self._ttl = ttl_seconds

    @staticmethod
    def key(conversation_id, sender_id):
        return f"{_KEY_PREFIX}:{conversation_id or ''}:{sender_id or ''}"

    def _redis(self):
        if self._client is None:
            import redis
            self._client = redis.Redis.from_url(self._url)
        return self._client

    def history(self, key):
        """未过期历史（按时间正序）；无或后端异常均 []。"""
        try:
            raw = self._redis().lrange(key, 0, -1)
        except Exception:
            return []
        turns = []
        for item in raw or []:
            try:
                turns.append(json.loads(item))
            except (TypeError, ValueError):
                continue  # 坏片跳过，不拖累整段历史
        return turns

    def record(self, key, *, question, answer, category=None, record_id=None,
               subject=None):
        """记一轮（回复为空的静默轮不记）；后端异常静默。"""
        if not answer:
            return
        payload = json.dumps(
            _turn_payload(question, answer, category, record_id, subject),
            ensure_ascii=False, default=str)
        try:
            client = self._redis()
            client.rpush(key, payload)
            client.ltrim(key, -self._max_turns, -1)
            client.expire(key, self._ttl)
        except Exception:
            pass


def build_conversation_store(environ=None, *, max_turns=DEFAULT_MAX_TURNS,
                             ttl_seconds=DEFAULT_TTL_SECONDS):
    """按环境选后端：KB_REDIS_URL / PUBLIC_DATA_REDIS_URL 非空 → Redis，
    否则进程内兜底（语义等价，重启丢上下文）。"""
    env = os.environ if environ is None else environ
    for name in _REDIS_URL_ENVS:
        url = (env.get(name) or "").strip()
        if url:
            return RedisConversationStore(
                url, max_turns=max_turns, ttl_seconds=ttl_seconds)
    return ConversationStore(max_turns=max_turns, ttl_seconds=ttl_seconds)


def format_history(history):
    """历史 → prompt 文本段（最近在前）。空调用方不拼接该段。

    末尾透出最近一次命中商品（「上文涉及商品」）：指代追问（它/那款/
    价格呢）的锚点，不再只靠被截断的答案文本猜（2026-10-22）。
    旧条目无 subject 字段时自动跳过。
    """
    if not history:
        return ""
    lines = ["# 对话历史（最近的在前）"]
    subject = None
    for turn in reversed(history):
        if subject is None:
            subject = turn.get("subject")
        lines.append(f"客服：{turn['q']}")
        lines.append(f"助手：{turn['a']}")
    if subject:
        lines.append("")
        lines.append(f"上文涉及商品：{subject}")
    return "\n".join(lines)
