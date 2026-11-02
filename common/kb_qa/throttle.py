# -*- coding: utf-8 -*-
"""D6 去重限流（10-29 方案 §6，2026-11-02）。

- dedupe：sender_id+platform+event_id，SET NX EX 10s——钉钉重推/用户
  双击在 DB claim 之前挡掉（claim 是永久幂等，本层是 10s 短窗前移）；
- min_interval：同 sender 10s 至多一条（防刷屏）；
- flags（改配置不改代码）：KB_DEDUP_ENABLE 默认 1、
  KB_MIN_INTERVAL_ENABLE 默认 0（先观测 duplicate/limited 日志再开）；
- store：cache_core.store_from_environment（Redis 优先、内存兜底），
  存储异常全程 fail-open，绝不拦正常消息。
"""

import os

from common import cache_core

DEDUP_TTL_SECONDS = 10
MIN_INTERVAL_SECONDS = 10

_STORE = None


def shared_store():
    global _STORE
    if _STORE is None:
        _STORE = cache_core.store_from_environment()
    return _STORE


def _enabled(name, default, environ=None):
    env = os.environ if environ is None else environ
    return (env.get(name) or default) == "1"


def _set_nx(key, ttl, store):
    try:
        return store.set_nx(key, "1", ttl)
    except Exception:
        return True  # fail-open：存储异常不拦消息


def is_duplicate(sender_id, event_id, *, platform="dingtalk", store=None,
                 environ=None):
    """同 sender+event 10s 内重复 → True（应丢弃）。"""
    if not _enabled("KB_DEDUP_ENABLE", "1", environ):
        return False
    if not sender_id or not event_id:
        return False
    key = f"kbqa:dedupe:{platform}:{sender_id}:{event_id}"
    return not _set_nx(key, DEDUP_TTL_SECONDS, store or shared_store())


def below_min_interval(sender_id, *, store=None, environ=None):
    """同 sender 距上条不足 10s → True（应限流）。"""
    if not _enabled("KB_MIN_INTERVAL_ENABLE", "0", environ):
        return False
    if not sender_id:
        return False
    key = f"kbqa:interval:{sender_id}"
    return not _set_nx(key, MIN_INTERVAL_SECONDS, store or shared_store())
