# -*- coding: utf-8 -*-
"""检索编排：问题 → jina-clip-v2 embed → Milvus 检索 → DeepSeek 答题。

跨模态关键：问题文本与图片同一向量空间，文字可直接搜出图片条目
（hit.kind="image"，image_ref 回指商品附件位置，供调用方取图展示）。

fail-soft：LLM 异常时降级返回 None + hits（调用方可只回检索结果），
embed/检索异常直接上抛（无检索结果则无答题基础）。
"""

from common.rag import embed, llm


def retrieve(question, *, milvus, store, jina_key, limit=8, kind=None,
             model=embed.DEFAULT_MODEL, dimensions=None):
    """问题 → 向量检索 hits。kind="text"/"image" 限定模态，None 混排。"""
    vector = embed.embed_text(question, jina_key, model=model,
                              dimensions=dimensions)
    return store.search(milvus, vector, limit=limit, kind=kind)


def answer_question(question, *, milvus, store, jina_key, deepseek_key,
                    limit=8, kind=None, model=embed.DEFAULT_MODEL,
                    dimensions=None, llm_model=llm.DEFAULT_MODEL):
    """检索 + 答题。返回 {"answer": str|None, "hits": [...]}。

    hits 为空或 LLM 失败时 answer=None（降级由调用方决定话术）。
    """
    hits = retrieve(question, milvus=milvus, store=store, jina_key=jina_key,
                    limit=limit, kind=kind, model=model, dimensions=dimensions)
    if not hits:
        return {"answer": None, "hits": []}
    try:
        answer = llm.answer(question, hits, deepseek_key, model=llm_model)
    except llm.LLMError:
        answer = None
    return {"answer": answer, "hits": hits}
