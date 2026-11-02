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


def get_or_compute(key, ttl, loader, *, store=None, now=None):
    """读穿缓存：命中直接返回；未命中调 loader，非空才写入。"""
    store = store if store is not None else _DEFAULT_STORE
    try:
        value = store.get(key, now=now)
    except Exception as exc:
        log.warning("cache get failed (%s): %s", key, type(exc).__name__)
        value = MISSING
    if value is not MISSING:
        return value
    value = loader()
    if _non_empty(value):
        try:
            store.set(key, value, ttl, now=now)
        except Exception as exc:
            log.warning("cache set failed (%s): %s", key, type(exc).__name__)
    return value
