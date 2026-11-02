# -*- coding: utf-8 -*-
"""统一缓存核心（10-29 方案 D1：此前各模块自带 TTL 缓存、语义各异）。

统一语义（后续 Redis store / pub-sub 失效（D4）也走同一接口）：
- 未命中 = 调 loader（KeyError 不外抛给调用方）；
- **非空结果才写缓存**（None/空容器不缓存，空值占位会放大故障窗口）；
- store 读写异常 fail-open：只记日志退化为直算，不阻断主链路；
- close() 幂等释放资源（MemoryStore 仅清空，Redis store 同接口）。
"""

import logging
import threading
import time

log = logging.getLogger(__name__)

#: 未命中哨兵（区别于缓存了 None 的场景——本核心不缓存 None）。
MISSING = object()


class MemoryStore:
    """进程内 TTL 存储（线程安全；整块值替换，读者不见中间态）。"""

    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def get(self, key, *, now=None):
        now = time.time() if now is None else now
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return MISSING
            expires, value = item
            if now >= expires:
                self._data.pop(key, None)
                return MISSING
            return value

    def set(self, key, value, ttl, *, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._data[key] = (now + ttl, value)

    def delete(self, key):
        with self._lock:
            self._data.pop(key, None)

    def clear(self):
        with self._lock:
            self._data.clear()

    def close(self):
        self.clear()


_DEFAULT_STORE = MemoryStore()


def default_store():
    return _DEFAULT_STORE


def _non_empty(value):
    if value is None:
        return False
    if isinstance(value, (dict, list, tuple, set, frozenset)) and not value:
        return False
    return True


def get_or_compute(key, ttl, loader, *, store=None, now=None,
                   should_cache=None):
    """读穿缓存：命中直接返回；未命中调 loader，非空才写入。

    should_cache：可选谓词（默认非空即写）。业务侧用于「错误结果不缓存」
    等约束，例如 query_sql 的 {"error": ...} 响应。
    """
    store = store if store is not None else _DEFAULT_STORE
    try:
        value = store.get(key, now=now)
    except Exception as exc:
        log.warning("cache get failed (%s): %s", key, type(exc).__name__)
        value = MISSING
    if value is not MISSING:
        return value
    value = loader()
    cacheable = should_cache(value) if should_cache else _non_empty(value)
    if cacheable:
        try:
            store.set(key, value, ttl, now=now)
        except Exception as exc:
            log.warning("cache set failed (%s): %s", key, type(exc).__name__)
    return value


def store_from_environment(environ=None):
    """按环境选后端：KB_REDIS_URL / PUBLIC_DATA_REDIS_URL → RedisStore；
    未配置或初始化失败 → 进程内共享 MemoryStore（fail-open）。"""
    import os

    env = os.environ if environ is None else environ
    for name in ("KB_REDIS_URL", "PUBLIC_DATA_REDIS_URL"):
        url = (env.get(name) or "").strip()
        if url:
            try:
                from common.cache_core.redis_store import RedisStore

                return RedisStore(url)
            except Exception as exc:
                log.warning("redis store init failed: %s", type(exc).__name__)
                break
    return default_store()
