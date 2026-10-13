# -*- coding: utf-8 -*-
"""手册切片检索：kb_doc_chunks 全量读出 → BM25 top-k（进程内 TTL 缓存）。

几百个 chunk 的量级，全量加载 + 内存 BM25 每次 <50ms；缓存 5 分钟，
kb-sync-docs 重导后最迟 5 分钟生效（也可重启进程立即生效）。
"""

import time

from common.kb_qa.bm25 import Bm25Index

_CACHE_TTL_SECONDS = 300
_cache = {"loaded_at": 0.0, "chunks": [], "index": None}


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


def _index_for(conn, *, now=None):
    now = time.time() if now is None else now
    if now - _cache["loaded_at"] > _CACHE_TTL_SECONDS:
        chunks = _load(conn)
        _cache["chunks"] = chunks
        _cache["index"] = (
            Bm25Index([chunk["content"] for chunk in chunks]) if chunks else None
        )
        _cache["loaded_at"] = now
    return _cache["chunks"], _cache["index"]


def search_docs(conn, query, *, k=3, now=None):
    """BM25 召回 top-k 切片（dict 列表）；无切片/零命中返回 []。"""
    chunks, index = _index_for(conn, now=now)
    if not chunks or index is None:
        return []
    return [chunks[i] for i, _ in index.topk(query, k=k)]


def reset_cache():
    """测试/重导后强制失效。"""
    _cache["loaded_at"] = 0.0
    _cache["chunks"] = []
    _cache["index"] = None
