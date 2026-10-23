# -*- coding: utf-8 -*-
"""Web 通道业务数据角色白名单（2026-10-23 裁决）。

语义：小易 Web 前端用户默认只答商品/手册；kb_web_acl 在册且
allow_business=1 的用户才开放经营数据工具（daily_sales/channel_sales/
query_sql）。独立于 bi_authz（看板授权），语义不混用。

纪律：默认拒绝 + 异常拒绝（fail-closed）——查表失败时宁可误拒，
不放行经营数据。60s 进程内缓存（ACL 变更分钟级生效可接受）。
"""

import time

DEFAULT_CACHE_SECONDS = 60


class BusinessAcl:
    """kb_web_acl 查询器（带缓存）。connection_factory 复用 mart 连接。"""

    def __init__(self, connection_factory, *, cache_seconds=DEFAULT_CACHE_SECONDS,
                 now=time.monotonic):
        self._connection_factory = connection_factory
        self._cache_seconds = cache_seconds
        self._now = now
        self._cache = {}  # userid -> (allowed: bool, ts)

    def is_allowed(self, userid):
        if not userid:
            return False
        cached = self._cache.get(userid)
        if cached and self._now() - cached[1] < self._cache_seconds:
            return cached[0]
        allowed = self._query(userid)
        self._cache[userid] = (allowed, self._now())
        return allowed

    def _query(self, userid):
        try:
            conn = self._connection_factory()
            try:
                cursor = conn.cursor()
                try:
                    cursor.execute(
                        "SELECT `allow_business` FROM `kb_web_acl`"
                        " WHERE `userid`=%s",
                        (userid,),
                    )
                    row = cursor.fetchone()
                    return bool(row and row.get("allow_business"))
                finally:
                    cursor.close()
            finally:
                conn.close()
        except Exception:
            return False  # fail-closed
