"""Card payload cache for bi-web (2026-09-14 cache spec, stage 2).

Redis 数据缓存 + 冷热分层，一律 fail-open：

* **可选增强，不是硬依赖**（设计 D2）：``PUBLIC_DATA_REDIS_URL`` 缺省为空
  → 进程内缓存兜底（语义等价单容器部署）；配置了才连，且连接是惰性的，
  Redis 不可达只表现为缓存 miss，绝不把 5xx 带给卡片路由。
* **冷热分层 = 缓存分层，不是存储分层**（设计 D1）：窗口含当天（无
  ``month`` 或 ``month`` == 当前月）= 热，短 TTL；``month`` 非当前月
  （已封月或未来月，结果不变）= 冷，长 TTL，键随月翻转。
* **键规范**（设计 D5）：``biweb:card:{card_id}:{规范化参数}:{周期类别}``；
  参数按名排序拼接（顺序无关），杜绝键爆炸。
* **防惊群**：同一键并发 miss 只算一次（per-key 锁，双检查）；``compute``
  抛错时异常传播且不缓存坏值。
* 缓存只存在于服务端：HTTP 层的 ``Cache-Control: no-store`` 响应头由
  路由保留，与本模块无关。

输出纪律与 :mod:`common.bi_web.app` 一致：后端故障静默吞掉（不记日志、
不带异常文本），因为 miss 的代价只是一次 mart 直查。
"""

import json
import os
import threading
import time
from datetime import datetime

#: 热 TTL：对齐页面 ``refresh_seconds`` 缺省（设计阶段 2）。
HOT_TTL_SECONDS = 300.0

#: 冷 TTL：封月数据理论上不变，一天兜底（设计阶段 2）。
COLD_TTL_SECONDS = 86400.0

#: 键前缀（设计 D5）。
_KEY_PREFIX = "biweb:card"

#: 环境开关：非空 → Redis 后端，否则进程内后端。
_REDIS_URL_ENV = "PUBLIC_DATA_REDIS_URL"


# ---------------------------------------------------------------------------
# Key construction and the hot/cold split
# ---------------------------------------------------------------------------

def normalize_params(params):
    """参数规范化：按名排序拼接，同一参数集与顺序无关（杜绝键爆炸）。"""
    return "&".join(f"{name}={params[name]}" for name in sorted(params))


def period_class(params, *, now=None,
                 hot_ttl=HOT_TTL_SECONDS, cold_ttl=COLD_TTL_SECONDS):
    """周期类别 + TTL：``("cur", 热 TTL)`` 或 ``(month, 冷 TTL)``。

    无 ``month`` 或 ``month`` == 当前月 → 热（窗口含当天，随 extract
    同步在变）。任何非当前月 → 冷、键带月：封月结果不变；未来月结果
    为空且不变，与冷数据同性质，且跨入该月后类别翻转为 ``cur``，键
    自然轮换，绝不永久陈旧。
    """
    current = (datetime.now() if now is None else now).strftime("%Y-%m")
    month = params.get("month") or None
    if month is None or month == current:
        return "cur", hot_ttl
    return month, cold_ttl


def card_cache_key(card_id, params, *, now=None,
                   hot_ttl=HOT_TTL_SECONDS, cold_ttl=COLD_TTL_SECONDS):
    """缓存键 + TTL：``biweb:card:{card_id}:{规范化参数}:{周期类别}``。"""
    period, ttl = period_class(params, now=now, hot_ttl=hot_ttl, cold_ttl=cold_ttl)
    return f"{_KEY_PREFIX}:{card_id}:{normalize_params(params)}:{period}", ttl


# ---------------------------------------------------------------------------
# The cache contract and its backends
# ---------------------------------------------------------------------------

class CardCache:
    """Contract: ``get`` / ``set`` / ``close``，外加共享的读穿防惊群。

    后端只承诺两件事：``get`` miss 返回 ``None``；``set`` 失败不向调用
    方传播（fail-open）。``get_or_compute`` 在基类用 per-key 锁实现，
    两个后端自动获得「同一键并发 miss 只算一次」的语义。
    """

    def __init__(self):
        self._key_locks = {}
        self._key_locks_guard = threading.Lock()

    def get(self, key):  # pragma: no cover - interface
        """命中返回载荷，miss（含后端故障）返回 ``None``。"""
        raise NotImplementedError

    def set(self, key, payload, ttl):  # pragma: no cover - interface
        """写入载荷；失败静默（fail-open，绝不拖垮卡片路由）。"""
        raise NotImplementedError

    def close(self):
        """释放后端资源（缺省无资源）。"""

    def _key_lock(self, key):
        with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def get_or_compute(self, key, ttl, compute):
        """读穿：命中直返；miss 时在 per-key 锁内双检查后只算一次。

        ``compute`` 抛错 → 异常原样传播且不缓存坏值，下一次调用重试。
        """
        payload = self.get(key)
        if payload is not None:
            return payload
        with self._key_lock(key):
            payload = self.get(key)
            if payload is not None:
                return payload
            payload = compute()
            self.set(key, payload, ttl)
            return payload


class InProcessCardCache(CardCache):
    """缺省后端：进程内 TTL 字典（线程安全）。

    语义等价单容器部署的现状：没有 Redis 时卡片仍在同一进程内去重。
    """

    def __init__(self, *, monotonic=None):
        super().__init__()
        self._monotonic = time.monotonic if monotonic is None else monotonic
        self._entries = {}
        self._guard = threading.Lock()

    def get(self, key):
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, payload = entry
            if self._monotonic() >= expires_at:
                self._entries.pop(key, None)
                return None
            return payload

    def set(self, key, payload, ttl):
        with self._guard:
            self._entries[key] = (self._monotonic() + ttl, payload)

    def close(self):
        with self._guard:
            self._entries.clear()


class RedisCardCache(CardCache):
    """Redis 后端（fail-open）：任何异常 → 视为 miss；set 失败静默。

    连接是惰性的：构造不连，首次读写才经 ``redis.Redis.from_url``
    建立，所以 Redis 不可达从启动到运行都只是 miss，绝不 503。
    载荷以 JSON 存储（``run_*`` 已转 float，与 HTTP 响应同一编码面）。
    """

    def __init__(self, url, *, client=None):
        super().__init__()
        self._url = url
        self._client = client  # injectable for tests

    def _redis(self):
        if self._client is None:
            import redis
            self._client = redis.Redis.from_url(self._url)
        return self._client

    def get(self, key):
        try:
            raw = self._redis().get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception:
            return None

    def set(self, key, payload, ttl):
        try:
            self._redis().set(key, json.dumps(payload), ex=max(1, int(ttl)))
        except Exception:
            pass

    def close(self):
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def build_card_cache(environ=None):
    """按环境选后端：``PUBLIC_DATA_REDIS_URL`` 非空 → Redis，否则进程内。"""
    env = os.environ if environ is None else environ
    url = (env.get(_REDIS_URL_ENV) or "").strip()
    if url:
        return RedisCardCache(url)
    return InProcessCardCache()
