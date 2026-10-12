# -*- coding: utf-8 -*-
"""jina-clip-v2 embedding 客户端：文本与图片同一向量空间。

API 事实（2026-10-12 多源交叉验证，最终以探针实测为准）：
- 端点 https://api.jina.ai/v1/embeddings，OpenAI 风格鉴权 Bearer key；
- input 元素为 {"text": "..."} 或 {"image": "<http(s) url 或 data:image/...;base64,...>"}；
- 返回按 index 排序的 float 向量列表；默认维度 1024，MRL 截断可传 dimensions；
- 单次调用混合文本与图片同空间——跨模态（文字搜图）即得益于此。

仅依赖标准库（与 kb_qa.media 同款风格）：生产镜像零新增依赖。
"""

import base64
import json
import time
import urllib.request

API_URL = "https://api.jina.ai/v1/embeddings"
DEFAULT_MODEL = "jina-clip-v2"
DEFAULT_DIM = 1024
_TIMEOUT = 30


class EmbedError(RuntimeError):
    """API 返回异常或形状不符。调用方 fail-soft（跳过/重试），不静默吞。"""


def _post(payload, api_key):
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        # 非泄露约定：只带状态码，不回传 body（可能含输入回显）。
        raise EmbedError(f"jina embed HTTP {exc.code}") from None
    except Exception as exc:
        raise EmbedError(f"jina embed 网络异常: {type(exc).__name__}") from None
    data = result.get("data")
    if not isinstance(data, list) or not data:
        raise EmbedError(f"jina embed 返回异常: {sorted(result.keys())}")
    return sorted(data, key=lambda x: x.get("index", 0))


def embed_items(items, api_key, *, model=DEFAULT_MODEL, dimensions=None):
    """混合批量嵌入。items=[{"text":..}|{"image":..}]，返回 [vector,...]。

    单次调用内文本与图片顺序任意，返回按 index 对齐 items 顺序。
    """
    if not items:
        return []
    payload = {"model": model, "input": list(items)}
    if dimensions:
        payload["dimensions"] = dimensions
    t0 = time.perf_counter()
    data = _post(payload, api_key)
    _ = time.perf_counter() - t0  # 延迟由调用方计时统计
    vectors = []
    for entry in data:
        vec = entry.get("embedding")
        if not isinstance(vec, list) or not vec:
            raise EmbedError("jina embed 返回空向量")
        vectors.append([float(v) for v in vec])
    if len(vectors) != len(items):
        raise EmbedError(f"jina embed 数量不符: 入{len(items)} 出{len(vectors)}")
    return vectors


def embed_text(text, api_key, *, model=DEFAULT_MODEL, dimensions=None):
    """单条文本 → 向量。"""
    return embed_items([{"text": text}], api_key, model=model, dimensions=dimensions)[0]


def embed_image_url(url, api_key, *, model=DEFAULT_MODEL, dimensions=None):
    """单张图片（http(s) URL）→ 向量。"""
    return embed_items([{"image": url}], api_key, model=model, dimensions=dimensions)[0]


def to_data_uri(content, mime=None):
    """图片字节流 → base64 data URI（jina image 输入免外部 URL）。"""
    return f"data:{mime or 'image/png'};base64,{base64.b64encode(content).decode()}"


def embed_image_bytes(content, api_key, *, mime="image/png", model=DEFAULT_MODEL,
                      dimensions=None):
    """单张图片（字节流）→ base64 data URI → 向量。

    kb_qa 附件下载后是字节流（media.download_bytes），转 data URI 免外部 URL。
    """
    return embed_items([{"image": to_data_uri(content, mime)}], api_key,
                       model=model, dimensions=dimensions)[0]


def cosine(a, b):
    """余弦相似度（自验与测试用）。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
