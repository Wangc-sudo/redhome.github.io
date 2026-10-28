# -*- coding: utf-8 -*-
"""检索编排：问题 → jina-clip-v2 embed → Milvus 检索 → DeepSeek 答题。

跨模态关键：问题文本与图片同一向量空间，文字可直接搜出图片条目
（hit.kind="image"，image_ref 回指商品附件位置，供调用方取图展示）。

fail-soft：LLM 异常时降级返回 None + hits（调用方可只回检索结果），
embed/检索异常直接上抛（无检索结果则无答题基础）。
"""

from collections import OrderedDict
import math
import threading
import time
import unicodedata

from common.rag import embed, llm

_QUERY_CACHE_LIMIT = 128
_QUERY_VECTOR_CACHE = OrderedDict()
_QUERY_CACHE_LOCK = threading.Lock()


def normalize_question(question):
    """缓存键使用的规范化问题：NFKC + 折叠所有连续空白。"""
    return " ".join(unicodedata.normalize("NFKC", question or "").split())


def clear_query_vector_cache():
    with _QUERY_CACHE_LOCK:
        _QUERY_VECTOR_CACHE.clear()


def _query_vector(question, api_key, *, model, dimensions, provider, base_url):
    normalized = normalize_question(question)
    # provider/base_url 额外隔离同模型名但不同网关；规范化问题+model+dim 必在键中。
    key = (normalized, model, dimensions, provider, base_url)
    with _QUERY_CACHE_LOCK:
        if key in _QUERY_VECTOR_CACHE:
            vector = _QUERY_VECTOR_CACHE.pop(key)
            _QUERY_VECTOR_CACHE[key] = vector
            return vector, True
    vector = embed.embed_text(
        normalized, api_key, model=model, dimensions=dimensions,
        provider=provider, base_url=base_url)
    with _QUERY_CACHE_LOCK:
        _QUERY_VECTOR_CACHE[key] = vector
        while len(_QUERY_VECTOR_CACHE) > _QUERY_CACHE_LIMIT:
            _QUERY_VECTOR_CACHE.popitem(last=False)
    return vector, False


def _score(hit):
    try:
        value = float(hit.get("score"))
    except (TypeError, ValueError):
        return float("-inf")
    return value if math.isfinite(value) else float("-inf")


def _product_record_id(hit):
    item_id = hit.get("id") or ""
    if item_id.startswith("product-img:"):
        parts = item_id.split(":", 2)
        return parts[1] if len(parts) == 3 and parts[1] else None
    if item_id.startswith("product:"):
        record_id = item_id[len("product:"):]
        return record_id or None
    return None


def dedupe_product_hits(hits, *, max_images_per_product=1):
    """每商品保留最佳文本及最多 N 张最佳图片，非商品命中不合并。"""
    if type(max_images_per_product) is not int or max_images_per_product < 0:
        raise ValueError("max_images_per_product must be a non-negative integer")
    groups = {}
    selected = []
    for position, hit in enumerate(hits):
        record_id = _product_record_id(hit)
        if record_id is None:
            selected.append((position, hit))
            continue
        group = groups.setdefault(record_id, {"text": None, "images": []})
        if hit.get("kind") == "image":
            group["images"].append((position, hit))
        elif group["text"] is None or _score(hit) > _score(group["text"][1]):
            group["text"] = (position, hit)
    for group in groups.values():
        if group["text"] is not None:
            selected.append(group["text"])
        images = sorted(group["images"], key=lambda pair: (-_score(pair[1]), pair[0]))
        selected.extend(images[:max_images_per_product])
    selected.sort(key=lambda pair: (-_score(pair[1]), pair[0]))
    return [hit for _, hit in selected]


def _margin(hits):
    """最佳命中与下一商品/文档命中的分差；无竞争者时返回 None。"""
    scores = []
    seen = set()
    for position, hit in enumerate(hits):
        record_id = _product_record_id(hit)
        key = (("product", record_id) if record_id is not None
               else ("item", hit.get("id") or position))
        if key in seen:
            continue
        seen.add(key)
        score = _score(hit)
        if score == float("-inf"):
            continue
        scores.append(score)
    return scores[0] - scores[1] if len(scores) > 1 else None


def _elapsed_ms(started):
    return round((time.perf_counter() - started) * 1000, 3)


def _retrieve(question, *, milvus, store, jina_key, limit, kind, model,
              dimensions, provider, base_url, min_score, min_margin,
              max_images_per_product):
    metrics = {
        "embed": 0.0, "search": 0.0, "rerank": 0.0, "llm": 0.0,
        "top_score": None, "no_hit": True, "fallback": True,
        "cache_hit": False, "margin": None, "gate_passed": False,
    }
    started = time.perf_counter()
    vector, cache_hit = _query_vector(
        question, jina_key, model=model, dimensions=dimensions,
        provider=provider, base_url=base_url)
    metrics["embed"] = _elapsed_ms(started)
    metrics["cache_hit"] = cache_hit

    started = time.perf_counter()
    raw_hits = store.search(milvus, vector, limit=limit, kind=kind)
    metrics["search"] = _elapsed_ms(started)

    started = time.perf_counter()
    hits = dedupe_product_hits(
        raw_hits, max_images_per_product=max_images_per_product)[:limit]
    if hits and _score(hits[0]) != float("-inf"):
        metrics["top_score"] = _score(hits[0])
        metrics["margin"] = _margin(hits)
    gate_passed = bool(hits)
    if min_score is not None:
        gate_passed = (
            gate_passed and metrics["top_score"] is not None
            and metrics["top_score"] >= min_score)
    if min_margin is not None and metrics["margin"] is not None:
        gate_passed = gate_passed and metrics["margin"] >= min_margin
    metrics["rerank"] = _elapsed_ms(started)
    metrics["gate_passed"] = gate_passed
    if not gate_passed:
        return [], metrics
    metrics["no_hit"] = False
    metrics["fallback"] = False
    return hits, metrics


def retrieve(question, *, milvus, store, jina_key, limit=8, kind=None,
             model=embed.DEFAULT_MODEL, dimensions=None,
             provider=embed.DEFAULT_PROVIDER, base_url=None,
             min_score=None, min_margin=None, max_images_per_product=1):
    """检索、商品去重与可选门禁；默认不设置分数阈值，兼容既有调用。"""
    hits, _ = _retrieve(
        question, milvus=milvus, store=store, jina_key=jina_key, limit=limit,
        kind=kind, model=model, dimensions=dimensions, provider=provider,
        base_url=base_url, min_score=min_score, min_margin=min_margin,
        max_images_per_product=max_images_per_product)
    return hits


def answer_question(question, *, milvus, store, jina_key, deepseek_key,
                    limit=8, kind=None, model=embed.DEFAULT_MODEL,
                    dimensions=None, llm_model=llm.DEFAULT_MODEL,
                    provider=embed.DEFAULT_PROVIDER, base_url=None,
                    min_score=None, min_margin=None, max_images_per_product=1):
    """检索门禁通过后才调用 LLM；失败仍返回 hits，并附各阶段 metrics。"""
    hits, metrics = _retrieve(
        question, milvus=milvus, store=store, jina_key=jina_key, limit=limit,
        kind=kind, model=model, dimensions=dimensions, provider=provider,
        base_url=base_url, min_score=min_score, min_margin=min_margin,
        max_images_per_product=max_images_per_product)
    answer = None
    if hits:
        started = time.perf_counter()
        try:
            answer = llm.answer(question, hits, deepseek_key, model=llm_model)
        except llm.LLMError:
            answer = None
        metrics["llm"] = _elapsed_ms(started)
    metrics["fallback"] = answer is None
    return {"answer": answer, "hits": hits, "metrics": metrics}
