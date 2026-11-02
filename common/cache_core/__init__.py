# -*- coding: utf-8 -*-
"""cache_core：统一缓存公共核心（10-29 方案 D1，2026-11-02 新建）。"""

from common.cache_core.core import (  # noqa: F401
    MemoryStore,
    default_store,
    get_or_compute,
    store_from_environment,
)
