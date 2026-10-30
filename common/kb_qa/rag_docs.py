# -*- coding: utf-8 -*-
"""向量召回：问题 → kb_rag_v1 切片（与 docs.search_docs 返回同构）。

fail-open 与 LLM 同款铁律：缺配/检索异常一律返回 []，调用方按纯 BM25
继续——向量召回是增强不是依赖。凭据与 provider 走 RAG_EMBED_* env
（与 rag.index_docs/index_products 同一组变量，一处配置三处生效）。
"""

import importlib
import os

from common.rag import embed

#: 每次召回条数（与 BM25 k=3 合并去重后截断，见 app._merge_doc_hits）。
VECTOR_K = 3

#: 文本向量命中下限（COSINE），低于此值的切片不进入答案上下文。
MIN_TEXT_SCORE = 0.5


def from_environment(environ=None):
    """env 齐备返回 search callable，缺失返回 None（纯 BM25 运行）。

    provider=local 预留（本地推理），当前未实现——返回 None 安全降级。
    """
    values = os.environ if environ is None else environ
    provider = values.get("RAG_EMBED_PROVIDER", embed.DEFAULT_PROVIDER)
    api_key = values.get("RAG_EMBED_API_KEY") or values.get("JINA_API_KEY")
    if not api_key:
        return None
    if provider == embed.PROVIDER_OPENAI and not values.get("RAG_EMBED_BASE_URL"):
        return None
    if provider not in (embed.PROVIDER_JINA, embed.PROVIDER_OPENAI,
                        embed.PROVIDER_CTAIGW):
        return None

    def _search(question):
        return search_vector(question, provider=provider, api_key=api_key,
                             environ=values)

    return _search


def search_vector(question, *, provider=embed.DEFAULT_PROVIDER, api_key=None,
                  environ=None, limit=VECTOR_K):
    """问题 → [{doc_title, chunk_index, content, score}...]。

    返回结构与 kb_qa.docs.search_docs 的切片 dict 同构（多一个 score），
    供 app._merge_doc_hits 与 BM25 结果合并去重。
    """
    values = os.environ if environ is None else environ
    store = importlib.import_module("common.rag.store")

    milvus = store.connect(values.get("RAG_MILVUS_PATH", store.DEFAULT_PATH))
    try:
        model = (values.get("RAG_EMBED_MODEL") or values.get("JINA_CLIP_MODEL")
                 or embed.default_model(provider))
        dim_text = values.get("RAG_EMBED_DIM") or values.get("JINA_CLIP_DIM")
        vector = embed.embed_text(
            question, api_key, model=model,
            dimensions=int(dim_text) if dim_text else None,
            provider=provider, base_url=values.get("RAG_EMBED_BASE_URL"))
        hits = store.search(milvus, vector, limit=limit, kind="text")
    finally:
        # Milvus Lite 的 LOCK 是排他 flock：kb-gateway 与 scheduler（灌库）
        # 共享 rag.db，连接必须即用即放——不 close 时靠 GC 释放，时机不
        # 可控，凌晨灌库会撞 Open local milvus failed（2026-10-21）。
        try:
            milvus.close()
        except Exception:
            pass
    return [
        {
            "doc_title": hit.get("name") or "",
            "chunk_index": _index_of(hit.get("id")),
            "content": hit.get("text") or "",
            "score": hit.get("score"),
        }
        for hit in hits
        if (hit.get("score") or 0) >= MIN_TEXT_SCORE
    ]


def search_wdt(question, *, provider=embed.DEFAULT_PROVIDER, api_key=None,
               environ=None, limit=VECTOR_K):
    """订单问题 → kind="wdt" 交易明细块；低分命中与手册文本同样过滤。"""
    values = os.environ if environ is None else environ
    store = importlib.import_module("common.rag.store")

    milvus = store.connect(values.get("RAG_MILVUS_PATH", store.DEFAULT_PATH))
    try:
        model = (values.get("RAG_EMBED_MODEL") or values.get("JINA_CLIP_MODEL")
                 or embed.default_model(provider))
        dim_text = values.get("RAG_EMBED_DIM") or values.get("JINA_CLIP_DIM")
        vector = embed.embed_text(
            question, api_key, model=model,
            dimensions=int(dim_text) if dim_text else None,
            provider=provider, base_url=values.get("RAG_EMBED_BASE_URL"))
        hits = store.search(milvus, vector, limit=limit, kind="wdt")
    finally:
        try:
            milvus.close()
        except Exception:
            pass
    return [
        {
            "id": hit.get("id") or "",
            "name": hit.get("name") or "",
            "doc_title": hit.get("name") or "交易明细",
            "content": hit.get("text") or "",
            "score": hit.get("score"),
        }
        for hit in hits
        if (hit.get("score") or 0) >= MIN_TEXT_SCORE
    ]


def wdt_search_from_environment(environ=None):
    """env 齐备返回 WDT 检索 callable，缺失返回 None（走 agent 兜底）。"""
    values = os.environ if environ is None else environ
    provider = values.get("RAG_EMBED_PROVIDER", embed.DEFAULT_PROVIDER)
    api_key = values.get("RAG_EMBED_API_KEY") or values.get("JINA_API_KEY")
    if not api_key:
        return None
    if provider == embed.PROVIDER_OPENAI and not values.get("RAG_EMBED_BASE_URL"):
        return None
    if provider not in (embed.PROVIDER_JINA, embed.PROVIDER_OPENAI,
                        embed.PROVIDER_CTAIGW):
        return None

    def _search(question):
        return search_wdt(question, provider=provider, api_key=api_key,
                          environ=values)

    return _search


#: 文字搜图命中下限（COSINE）。2026-10-21 实测「习酒 包装 礼盒」真实
#: 命中 top1=0.4975；0.45 以下基本是噪声（跨模态分数整体偏低）。
MIN_IMAGE_SCORE = 0.45

#: 一次最多发几张召回图（超出截断；>2 张由 assets 拼长图）。
MAX_RECALL_IMAGES = 4


def search_images(question, *, provider=embed.DEFAULT_PROVIDER, api_key=None,
                  environ=None, limit=MAX_RECALL_IMAGES):
    """文字搜图：kind="image" 限定模态，返回 [{score, name, image_ref}]。

    image_ref = ``record_id/字段名/序号``，回指 AI 表附件位置（调用方
    据此 get_record 取签名 URL 下载）。与 search_vector 同款 fail-open
    约定：异常由调用方兜底。
    """
    values = os.environ if environ is None else environ
    store = importlib.import_module("common.rag.store")

    milvus = store.connect(values.get("RAG_MILVUS_PATH", store.DEFAULT_PATH))
    try:
        model = (values.get("RAG_EMBED_MODEL") or values.get("JINA_CLIP_MODEL")
                 or embed.default_model(provider))
        dim_text = values.get("RAG_EMBED_DIM") or values.get("JINA_CLIP_DIM")
        vector = embed.embed_text(
            question, api_key, model=model,
            dimensions=int(dim_text) if dim_text else None,
            provider=provider, base_url=values.get("RAG_EMBED_BASE_URL"))
        hits = store.search(milvus, vector, limit=limit, kind="image")
    finally:
        # 与 search_vector 同款：排他 flock 即用即放（防与凌晨灌库冲突）。
        try:
            milvus.close()
        except Exception:
            pass
    return [
        {"score": hit.get("score"), "name": hit.get("name") or "",
         "image_ref": hit.get("image_ref") or ""}
        for hit in hits
    ]


def image_search_from_environment(environ=None):
    """env 齐备返回图片召回 callable，缺失返回 None（图召回关闭）。"""
    values = os.environ if environ is None else environ
    provider = values.get("RAG_EMBED_PROVIDER", embed.DEFAULT_PROVIDER)
    api_key = values.get("RAG_EMBED_API_KEY") or values.get("JINA_API_KEY")
    if not api_key:
        return None
    if provider == embed.PROVIDER_OPENAI and not values.get("RAG_EMBED_BASE_URL"):
        return None
    if provider not in (embed.PROVIDER_JINA, embed.PROVIDER_OPENAI,
                        embed.PROVIDER_CTAIGW):
        return None

    def _search(question):
        return search_images(question, provider=provider, api_key=api_key,
                             environ=values)

    return _search


def _index_of(doc_entry_id):
    """``doc:{doc_id}:{序号:04d}`` → int 序号；解析失败返回 -1。"""
    try:
        return int(str(doc_entry_id).rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return -1
