# -*- coding: utf-8 -*-
"""jina-clip-v2 embedding 客户端：文本与图片同一向量空间。

API 事实（2026-10-12 多源交叉验证，最终以探针实测为准）：
- 端点 https://api.jina.ai/v1/embeddings，OpenAI 风格鉴权 Bearer key；
- input 元素为 {"text": "..."} 或 {"image": "<http(s) url 或 data:image/...;base64,...>"}；
- 返回按 index 排序的 float 向量列表；默认维度 1024，MRL 截断可传 dimensions；
- 单次调用混合文本与图片同空间——跨模态（文字搜图）即得益于此。

provider 抽象（2026-10-19）：除 jina 外支持 ``openai``——OpenAI 兼容
``{base_url}/embeddings``（input 为字符串数组，响应 data[].embedding），
各家网关/自部署端点同构。文档线是纯文本，不必被图片跨模态绑死在单一厂商；
商品线（图文混排）仍走 jina。

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

#: provider 选择：``jina``（图文同空间，跨模态）/ ``openai``（OpenAI 兼容
#: /v1/embeddings——天翼 ai.ctaigw.cn 等网关与各家自部署端点都长这样）。
#: 2026-10-19 加 openai 分支的原因：jina key 一直没到位，而客户侧网关只给
#: 文本 embedding；文档线（纯文本）不必被图片跨模态绑死在单一厂商上。
PROVIDER_JINA = "jina"
PROVIDER_OPENAI = "openai"
#: 天翼 AI 网关 multimodal-embedding（2026-10-20 实测契约，AppKey 授权后）：
#: 请求 ``input.contents`` 混合 text/image/video，``enable_fusion=false``
#: 时每条独立向量（true 会把整批融合成一条）；响应
#: ``output.embeddings[]``（{embedding, index, type:"vl"}），dim=2560，
#: 图片与文本同空间同维——商品图文线不必再等 jina。
PROVIDER_CTAIGW = "ctaigw"
DEFAULT_PROVIDER = PROVIDER_JINA
OPENAI_EMBEDDINGS_PATH = "/embeddings"
CTAIGW_DEFAULT_URL = ("https://ai.ctaigw.cn/v1/services/embeddings/"
                      "multimodal-embedding/multimodal-embedding")
CTAIGW_DEFAULT_MODEL = "qwen3-vl-embedding"
CTAIGW_DEFAULT_DIM = 2560


class EmbedError(RuntimeError):
    """API 返回异常或形状不符。调用方 fail-soft（跳过/重试），不静默吞。"""


def _post(payload, api_key, *, url=API_URL):
    req = urllib.request.Request(
        url,
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


def default_model(provider):
    """各 provider 的默认模型名；openai 无默认（jina-clip-v2 在别的网关上
    不存在，静默兜底只会换来一串 403），返回 None 由调用方强制显式配置。"""
    if provider == PROVIDER_CTAIGW:
        return CTAIGW_DEFAULT_MODEL
    if provider == PROVIDER_JINA:
        return DEFAULT_MODEL
    return None


def max_batch(provider):
    """单请求 contents 数量上限（2026-10-20 梯度实测：ctaigw 20 OK、22 →
    HTTP 400；取 16 留长文本 token 余量，与 index_docs.TEXT_BATCH 同值）。
    jina/openai 沿用 64。"""
    if provider == PROVIDER_CTAIGW:
        return 16
    return 64


def embed_items(items, api_key, *, model=DEFAULT_MODEL, dimensions=None,
                provider=DEFAULT_PROVIDER, base_url=None):
    """混合批量嵌入。items=[{"text":..}|{"image":..}]，返回 [vector,...]。

    单次调用内文本与图片顺序任意，返回按 index 对齐 items 顺序。

    provider：
    - ``jina``：图文同空间，input 元素即 {"text"/"image"}；
    - ``openai``：OpenAI 兼容端点（``base_url`` + ``/embeddings``），只吃
      文本——传图片项直接报错（文本模型出不了图向量，静默退化更糟）；
    - ``ctaigw``：天翼网关 multimodal-embedding，图文同空间 2560 维。
    """
    if not items:
        return []
    if provider == PROVIDER_OPENAI:
        return _embed_openai(items, api_key, model=model, base_url=base_url,
                             dimensions=dimensions)
    if provider == PROVIDER_CTAIGW:
        return _embed_ctaigw(items, api_key, model=model, base_url=base_url)
    if provider != PROVIDER_JINA:
        raise EmbedError(f"未知 embedding provider: {provider}")
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


def _embed_openai(items, api_key, *, model, base_url, dimensions=None):
    """OpenAI 兼容 /v1/embeddings：input 为字符串数组，响应 data[].embedding。

    各家网关（天翼 ai.ctaigw.cn 等）与 OpenAI 同构；部分实现把结果放在
    ``output.embeddings[]``（阿里系），这里一并兼容，两者都没有就 fail-fast。
    """
    if not base_url:
        raise EmbedError("provider=openai 需要 base_url（如 https://host/v1）")
    texts = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise EmbedError("provider=openai 只支持文本项（图片需 jina 跨模态）")
        texts.append(item["text"])
    payload = {"model": model, "input": texts}
    if dimensions:
        payload["dimensions"] = dimensions
    url = base_url.rstrip("/") + OPENAI_EMBEDDINGS_PATH
    result = _post_payload(payload, api_key, url)
    entries = result.get("data")
    if not isinstance(entries, list) or not entries:
        output = result.get("output")
        entries = (output or {}).get("embeddings") if isinstance(output, dict) else None
    if not isinstance(entries, list) or not entries:
        raise EmbedError(f"openai embed 返回异常: {sorted(result.keys())}")
    if hasattr(entries[0], "get") is False:
        raise EmbedError("openai embed 返回结构不符")
    vectors = []
    for entry in entries:
        vec = entry.get("embedding") if isinstance(entry, dict) else None
        if not isinstance(vec, list) or not vec:
            raise EmbedError("openai embed 返回空向量")
        vectors.append([float(v) for v in vec])
    if len(vectors) != len(items):
        raise EmbedError(f"openai embed 数量不符: 入{len(items)} 出{len(vectors)}")
    return vectors


def _embed_ctaigw(items, api_key, *, model, base_url=None):
    """天翼网关 multimodal-embedding：图文同空间，dim=2560（实测契约）。

    请求 ``input.contents`` 混合 {"text"}/{"image"}，``enable_fusion=false``
    （true 会把整批融合成一条向量，灌库必须 false）；响应
    ``output.embeddings[]``（{embedding, index, type:"vl"}），按 index 对齐
    输入顺序。图片项给 http(s) URL；字节流先经 ``to_data_uri``（网关支持
    data URI 与否未经实测，调用方若走字节流需先小流量验证）。
    """
    contents = []
    for item in items:
        if not isinstance(item, dict):
            raise EmbedError("ctaigw 输入项必须是 dict")
        if isinstance(item.get("text"), str):
            contents.append({"text": item["text"]})
        elif isinstance(item.get("image"), str):
            contents.append({"image": item["image"]})
        else:
            raise EmbedError("ctaigw 只支持 text/image 项")
    payload = {
        "model": model,
        "input": {"contents": contents},
        "parameters": {"enable_fusion": False},
    }
    result = _post_payload(payload, api_key, base_url or CTAIGW_DEFAULT_URL)
    output = result.get("output")
    entries = (output or {}).get("embeddings") if isinstance(output, dict) else None
    if not isinstance(entries, list) or not entries:
        raise EmbedError(f"ctaigw embed 返回异常: {sorted(result.keys())}")
    vectors = []
    for entry in sorted(entries, key=lambda e: e.get("index", 0)):
        vec = entry.get("embedding") if isinstance(entry, dict) else None
        if not isinstance(vec, list) or not vec:
            raise EmbedError("ctaigw embed 返回空向量")
        vectors.append([float(v) for v in vec])
    if len(vectors) != len(items):
        raise EmbedError(
            f"ctaigw embed 数量不符: 入{len(items)} 出{len(vectors)}")
    return vectors


def _post_payload(payload, api_key, url):
    """发请求并做最小形状校验（只取 JSON，向量提取交给调用方）。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise EmbedError(f"embed HTTP {exc.code}") from None
    except Exception as exc:
        raise EmbedError(f"embed 网络异常: {type(exc).__name__}") from None


def embed_text(text, api_key, *, model=DEFAULT_MODEL, dimensions=None,
               provider=DEFAULT_PROVIDER, base_url=None):
    """单条文本 → 向量。"""
    return embed_items([{"text": text}], api_key, model=model,
                       dimensions=dimensions, provider=provider,
                       base_url=base_url)[0]


def embed_image_url(url, api_key, *, model=DEFAULT_MODEL, dimensions=None,
                    provider=DEFAULT_PROVIDER, base_url=None):
    """单张图片（http(s) URL）→ 向量。"""
    return embed_items([{"image": url}], api_key, model=model,
                       dimensions=dimensions, provider=provider,
                       base_url=base_url)[0]


def to_data_uri(content, mime=None):
    """图片字节流 → base64 data URI（jina image 输入免外部 URL）。"""
    return f"data:{mime or 'image/png'};base64,{base64.b64encode(content).decode()}"


def embed_image_bytes(content, api_key, *, mime="image/png", model=DEFAULT_MODEL,
                      dimensions=None, provider=DEFAULT_PROVIDER, base_url=None):
    """单张图片（字节流）→ base64 data URI → 向量。

    kb_qa 附件下载后是字节流（media.download_bytes），转 data URI 免外部 URL。
    """
    return embed_items([{"image": to_data_uri(content, mime)}], api_key,
                       model=model, dimensions=dimensions, provider=provider,
                       base_url=base_url)[0]


def cosine(a, b):
    """余弦相似度（自验与测试用）。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
