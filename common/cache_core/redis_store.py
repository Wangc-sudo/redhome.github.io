# -*- coding: utf-8 -*-
"""Redis 后端（D4a）：与 MemoryStore 同接口，供 get_or_compute 读穿。

只按 key 操作（get/set/delete），禁止 FLUSHDB 类全局动作；连接关闭走
close()。redis-py 为既有依赖（kb_qa 会话/卡片缓存在用），惰性导入。
"""

import json

from common.cache_core.core import MISSING


class RedisStore:
    def __init__(self, url):
        import redis

        self._client = redis.Redis.from_url(url, decode_responses=True)

    def get(self, key, *, now=None):
        value = self._client.get(key)
        if value is None:
            return MISSING
        return json.loads(value)

    def set(self, key, value, ttl, *, now=None):
        self._client.set(
            key, json.dumps(value, ensure_ascii=False, default=str),
            ex=int(ttl))

    def delete(self, key):
        self._client.delete(key)

    def close(self):
        self._client.close()
