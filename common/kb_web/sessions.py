# -*- coding: utf-8 -*-
"""Web 会话存储：Redis 主、进程内兜底（fail-open 同 bi_web 缓存约定）。

数据模型（方案 §4）：
- 索引键 ``kbqa:web:idx:{userid}`` → JSON 列表 [{sid,title,updated}]，
  按 updated 倒序即会话列表；上限 MAX_SESSIONS_PER_USER，超出淘汰最旧。
- 会话键 ``kbqa:web:sess:{userid}:{sid}`` → JSON 消息列表
  [{role,content,ts,meta?}]；上限 MAX_MESSAGES_PER_SESSION。
- 两键都 7 天滑动 TTL（读写即续期），Redis 原生过期免清理。

与群内对话层（kb_qa.context）的区别：Web 场景用户期待「回来还能接着聊」，
故生命周期 7 天而非 10 分钟——两套参数两种产品语义，不合并。
"""

import hashlib
import json
import time
import uuid

#: 会话滑动过期（秒）：7 天。
SESSION_TTL_SECONDS = 7 * 24 * 3600

#: 每用户会话上限 / 每会话消息上限（防爆）。
MAX_SESSIONS_PER_USER = 50
MAX_MESSAGES_PER_SESSION = 200

#: 会话标题=首条用户消息截断长度。
TITLE_CHARS = 18

#: 发送幂等（2026-10-26「发送失败（网络异常）」治理配套）：在飞锁 TTL
#: 须覆盖 agent 最坏时长（多轮 query_sql × 推理版模型）且大于 nginx
#: proxy_read_timeout；结果窗给「重发/双击/弱网重试」一个直接取答案的
#: 窗口，命中即不重跑 agent、不重复写会话。
IDEM_LOCK_MS = 240_000
IDEM_DONE_TTL_SECONDS = 300

_KEY_INDEX = "kbqa:web:idx"
_KEY_SESSION = "kbqa:web:sess"
_KEY_IDEM_FLY = "kbqa:web:idem:fly"
_KEY_IDEM_DONE = "kbqa:web:idem:done"
#: clarify 待选择候选键（2026-10-23 Web 侧接线）：与群内 kbqa:pending
#: 同语义——消费即作废，TTL 随会话滑动。
_KEY_PENDING = "kbqa:web:pending"


class _MemoryKV:
    """进程内 KV 兜底（Redis 未配置/不可达时），接口对齐 redis 子集。"""

    def __init__(self):
        self._data = {}

    def get(self, key):
        entry = self._data.get(key)
        if entry is None:
            return None
        payload, expires_at = entry
        if expires_at is not None and time.time() >= expires_at:
            self._data.pop(key, None)
            return None
        return payload

    def setex(self, key, ttl, value):
        self._data[key] = (value, time.time() + ttl)

    def set_nx(self, key, value, ttl_ms):
        """SET NX PX 等价：键不存在或已过期才写入，返回是否抢到。"""
        entry = self._data.get(key)
        if entry is not None:
            _, expires_at = entry
            if expires_at is None or time.time() < expires_at:
                return False
        self._data[key] = (value, time.time() + ttl_ms / 1000.0)
        return True

    def delete(self, key):
        self._data.pop(key, None)


class WebSessionStore:
    """(userid, sid) 会话存取。后端异常一律 fail-open（读写当空）。"""

    def __init__(self, url=None, *, client=None,
                 ttl_seconds=SESSION_TTL_SECONDS):
        self._url = url
        self._client = client
        self._ttl = int(ttl_seconds)
        # 排序决胜序号：同一秒内批量建会话时保证「最新在前」确定性。
        self._seq = 0

    def _next_seq(self):
        self._seq += 1
        return self._seq

    # ------------------------------------------------------------------
    # backend
    # ------------------------------------------------------------------
    def _kv(self):
        if self._client is None:
            if self._url:
                import redis
                self._client = redis.Redis.from_url(self._url)
            else:
                self._client = _MemoryKV()
        return self._client

    def _read_json(self, key, default):
        try:
            raw = self._kv().get(key)
            if not raw:
                return default
            value = json.loads(raw)
            return value if isinstance(value, type(default)) else default
        except Exception:
            return default

    def _write_json(self, key, value):
        try:
            self._kv().setex(key, self._ttl,
                             json.dumps(value, ensure_ascii=False))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # keys
    # ------------------------------------------------------------------
    @staticmethod
    def _index_key(userid):
        return f"{_KEY_INDEX}:{userid}"

    @staticmethod
    def _session_key(userid, sid):
        return f"{_KEY_SESSION}:{userid}:{sid}"

    @staticmethod
    def _pending_key(userid, sid):
        return f"{_KEY_PENDING}:{userid}:{sid}"

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def list_sessions(self, userid):
        """会话列表（updated+seq 倒序）。"""
        entries = self._read_json(self._index_key(userid), [])
        return sorted(entries,
                      key=lambda e: (e.get("updated", 0), e.get("seq", 0)),
                      reverse=True)

    def create_session(self, userid):
        sid = uuid.uuid4().hex[:12]
        now = time.time()
        entries = self._read_json(self._index_key(userid), [])
        entries.append({"sid": sid, "title": None, "updated": now,
                        "seq": self._next_seq()})
        # 超上限淘汰最旧（连带删会话键）
        entries.sort(key=lambda e: e.get("updated", 0))
        while len(entries) > MAX_SESSIONS_PER_USER:
            evicted = entries.pop(0)
            try:
                self._kv().delete(self._session_key(userid, evicted["sid"]))
            except Exception:
                pass
        self._write_json(self._index_key(userid), entries)
        self._write_json(self._session_key(userid, sid), [])
        return sid

    def get_messages(self, userid, sid):
        """消息列表；会话不存在返回 None（区别于空会话 []）。"""
        entries = self._read_json(self._index_key(userid), [])
        if not any(e.get("sid") == sid for e in entries):
            return None
        return self._read_json(self._session_key(userid, sid), [])

    def get_title(self, userid, sid):
        for entry in self._read_json(self._index_key(userid), []):
            if entry.get("sid") == sid:
                return entry.get("title")
        return None

    def append(self, userid, sid, messages, *, title_hint=None):
        """追加消息（list of dict）；首条用户消息自动生成标题。"""
        existing = self._read_json(self._session_key(userid, sid), [])
        existing.extend(messages)
        existing = existing[-MAX_MESSAGES_PER_SESSION:]
        self._write_json(self._session_key(userid, sid), existing)

        entries = self._read_json(self._index_key(userid), [])
        for entry in entries:
            if entry.get("sid") == sid:
                entry["updated"] = time.time()
                entry["seq"] = self._next_seq()
                if not entry.get("title") and title_hint:
                    entry["title"] = title_hint[:TITLE_CHARS]
                break
        self._write_json(self._index_key(userid), entries)

    def rename(self, userid, sid, title):
        entries = self._read_json(self._index_key(userid), [])
        for entry in entries:
            if entry.get("sid") == sid:
                entry["title"] = (title or "")[:TITLE_CHARS] or None
                self._write_json(self._index_key(userid), entries)
                return True
        return False

    def delete(self, userid, sid):
        entries = self._read_json(self._index_key(userid), [])
        remaining = [e for e in entries if e.get("sid") != sid]
        if len(remaining) == len(entries):
            return False
        self._write_json(self._index_key(userid), remaining)
        try:
            self._kv().delete(self._session_key(userid, sid))
            self._kv().delete(self._pending_key(userid, sid))
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------
    # clarify 待选择候选（2026-10-23）
    # ------------------------------------------------------------------
    def set_pending(self, userid, sid, candidates):
        """存 clarify 反问的候选列表（[{record_id,name,code}…]）。"""
        self._write_json(self._pending_key(userid, sid), list(candidates))

    def pop_pending(self, userid, sid):
        """取出并作废候选（消费语义）；无/异常返回 None。

        注意 default 用 [] 不用 None：_read_json 按 default 类型校验
        载荷（防类型污染），None 的 NoneType 会让任何 list 载荷判型
        失败恒返回 default。
        """
        key = self._pending_key(userid, sid)
        value = self._read_json(key, [])
        if not value:
            return None
        try:
            self._kv().delete(key)
        except Exception:
            pass
        return value

    # ------------------------------------------------------------------
    # 发送幂等（2026-10-26）：在飞锁 + 结果窗
    # ------------------------------------------------------------------
    @staticmethod
    def _idem_digest(userid, sid, content):
        digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]
        return userid, sid, digest

    @classmethod
    def _idem_fly_key(cls, userid, sid, content):
        uid, sid_, digest = cls._idem_digest(userid, sid, content)
        return f"{_KEY_IDEM_FLY}:{uid}:{sid_}:{digest}"

    @classmethod
    def _idem_done_key(cls, userid, sid, content):
        uid, sid_, digest = cls._idem_digest(userid, sid, content)
        return f"{_KEY_IDEM_DONE}:{uid}:{sid_}:{digest}"

    def acquire_idem(self, userid, sid, content):
        """抢在飞锁：抢到→True 可执行；已存在→False（同内容请求在飞）；
        后端异常→True（fail-open，幂等是增强不是门禁）。"""
        key = self._idem_fly_key(userid, sid, content)
        try:
            kv = self._kv()
            if isinstance(kv, _MemoryKV):
                return kv.set_nx(key, "1", IDEM_LOCK_MS)
            return bool(kv.set(key, "1", nx=True, px=IDEM_LOCK_MS))
        except Exception:
            return True

    def get_idem_done(self, userid, sid, content):
        """结果窗命中返回上次同内容的结果 dict，否则 None。"""
        value = self._read_json(self._idem_done_key(userid, sid, content), {})
        return value or None

    def complete_idem(self, userid, sid, content, result):
        """答完：结果写结果窗（短 TTL）后放锁。调用前路由已完成会话
        append——放锁时拉取会话即见本轮问答。"""
        try:
            self._kv().setex(self._idem_done_key(userid, sid, content),
                             IDEM_DONE_TTL_SECONDS,
                             json.dumps(result, ensure_ascii=False))
        except Exception:
            pass
        self.release_idem(userid, sid, content)

    def release_idem(self, userid, sid, content):
        """异常路径放锁（不写结果窗，重发可重新执行）。"""
        try:
            self._kv().delete(self._idem_fly_key(userid, sid, content))
        except Exception:
            pass
