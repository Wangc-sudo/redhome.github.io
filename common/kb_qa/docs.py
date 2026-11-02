# -*- coding: utf-8 -*-
"""手册切片检索：kb_doc_chunks 全量读出 → BM25 top-k（cache_core TTL 缓存）。

几百个 chunk 的量级，全量加载 + 内存 BM25 每次 <50ms；缓存 5 分钟，
kb-sync-docs 重导后最迟 5 分钟生效（也可重启进程立即生效）。
2026-11-02 起缓存走 cache_core 统一核心（D1），TTL 与接口行为不变；
差异仅空库不再缓存占位（统一「非空才写」语义，空库时每查询重试加载）。
"""

from common import cache_core
from common.kb_qa.bm25 import Bm25Index

_CACHE_TTL_SECONDS = 300
_CACHE_KEY = "kb_docs:bm25_index"


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
    result = cache_core.get_or_compute(
        _CACHE_KEY, _CACHE_TTL_SECONDS, lambda: _load_index(conn), now=now)
    return result if result is not None else ([], None)


def search_docs(conn, query, *, k=3, now=None):
    """BM25 召回 top-k 切片（dict 列表）；无切片/零命中返回 []。"""
    chunks, index = _index_for(conn, now=now)
    if not chunks or index is None:
        return []
    return [chunks[i] for i, _ in index.topk(query, k=k)]


def reset_cache():
    """测试/重导后强制失效。"""
    cache_core.default_store().delete(_CACHE_KEY)
