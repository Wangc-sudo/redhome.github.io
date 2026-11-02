# -*- coding: utf-8 -*-
"""手册切片检索：kb_doc_chunks 全量读出 → BM25 top-k（cache_core TTL 缓存）。

缓存形态（D4b，2026-11-02）：
- 进程内 store（无 Redis）：缓存 (chunks, Bm25Index)，300s TTL，同旧行为；
- Redis store：只缓存 chunks（索引对象不可 JSON 序列化，进程内现建，
  几百 chunk <50ms），键含版本戳 `kb_docs:bm25_index:{version}`；
- 失效挂钩：sync_docs / sync_wiki 重导后 bump_version() 前移版本戳，
  各进程缓存键即失效（等效 pub/sub 的轻量实现，无需订阅通道）。
"""

import time

from common import cache_core
from common.kb_qa.bm25 import Bm25Index

_CACHE_TTL_SECONDS = 300
_CACHE_KEY = "kb_docs:bm25_index"
_VERSION_KEY = "kb_docs:version"
_STORE = None


def _store():
    global _STORE
    if _STORE is None:
        _STORE = cache_core.store_from_environment()
    return _STORE


def _redis_client():
    return getattr(_store(), "_client", None)


def _version():
    client = _redis_client()
    if client is None:
        return "0"  # 进程内 store：无跨进程失效，沿用 TTL
    try:
        return client.get(_VERSION_KEY) or "0"
    except Exception:
        return "0"


def bump_version():
    """手册重导后调用：版本戳前移 → 各进程文档缓存键即失效。"""
    client = _redis_client()
    if client is None:
        return
    try:
        client.set(_VERSION_KEY, str(time.time()))
    except Exception:
        pass  # 失效失败只退化为 TTL 过期，不影响写路径


def _load(conn):
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT `doc_id`, `doc_title`, `chunk_index`, `content`"
            " FROM `kb_doc_chunks` ORDER BY `doc_id`, `chunk_index`"
        )
        return list(cursor.fetchall())
    finally:
        cursor.close()


def _load_index(conn):
    chunks = _load(conn)
    if not chunks:
        return None  # 空库不写缓存（cache_core 非空语义，下轮查询重试）
    return chunks, Bm25Index([chunk["content"] for chunk in chunks])


def _index_for(conn, *, now=None):
    store = _store()
    key = f"{_CACHE_KEY}:{_version()}"
    if _redis_client() is not None:
        chunks = cache_core.get_or_compute(
            key, _CACHE_TTL_SECONDS, lambda: _load(conn), store=store,
            now=now) or []
        index = (Bm25Index([chunk["content"] for chunk in chunks])
                 if chunks else None)
        return chunks, index
    result = cache_core.get_or_compute(
        key, _CACHE_TTL_SECONDS, lambda: _load_index(conn), store=store,
        now=now)
    return result if result is not None else ([], None)


def search_docs(conn, query, *, k=3, now=None):
    """BM25 召回 top-k 切片（dict 列表）；无切片/零命中返回 []。"""
    chunks, index = _index_for(conn, now=now)
    if not chunks or index is None:
        return []
    return [chunks[i] for i, _ in index.topk(query, k=k)]


def prewarm(conn, *, now=None):
    """启动时加载文档切片并建立 BM25 索引，返回预热切片数。"""
    chunks, _ = _index_for(conn, now=now)
    return len(chunks)


def reset_cache():
    """测试/重导后强制失效（当前版本键）。"""
    _store().delete(f"{_CACHE_KEY}:{_version()}")
