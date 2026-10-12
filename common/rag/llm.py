# -*- coding: utf-8 -*-
"""DeepSeek chat 客户端：RAG 答题侧生成。

API 事实（2026-10-12 查证）：
- DeepSeek 兼容 OpenAI 格式：POST {base}/chat/completions，
  base 默认 https://api.deepseek.com，Bearer 鉴权；
- model 默认 deepseek-chat；messages=[{"role","content"},...]；
- 只做答题生成，不做 embedding（DeepSeek 无原生 embedding 接口）。

仅依赖标准库（与 embed.py 同款风格）：生产镜像零新增依赖。
非泄露约定：报错不带异常原文/输入回显，只带状态码与错误类型。
"""

import json
import urllib.request

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
_TIMEOUT = 60


class LLMError(RuntimeError):
    """API 返回异常。调用方 fail-soft（降级为只返回检索结果），不静默吞。"""


def _post(base, payload, api_key):
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
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
        raise LLMError(f"deepseek chat HTTP {exc.code}") from None
    except Exception as exc:
        raise LLMError(f"deepseek chat 网络异常: {type(exc).__name__}") from None
    return result


def build_context(hits, *, max_chars=2000):
    """把检索 hits 拼成答题上下文（截断防爆 token）。

    hits 为 store.search 返回的 [{kind,name,text,image_ref,score},...]。
    图片条目无正文，只带商品名与回指（供答案标注图片来源）。
    """
    parts = []
    used = 0
    for hit in hits:
        name = hit.get("name") or ""
        if hit.get("kind") == "image":
            line = f"[图片] {name}（{hit.get('image_ref') or ''}）"
        else:
            line = f"[资料] {name}：{(hit.get('text') or '').strip()}"
        if used + len(line) > max_chars:
            break
        parts.append(line)
        used += len(line)
    return "\n".join(parts)


def answer(question, hits, api_key, *, base=DEFAULT_BASE, model=DEFAULT_MODEL,
           system=None, temperature=0.2, max_tokens=1024):
    """基于检索结果答题。返回答案文本。

    hits 为空时直接返回 None（调用方决定降级话术，不硬编码）。
    """
    if not hits:
        return None
    context = build_context(hits)
    sys_prompt = system or (
        "你是企业知识库助手。仅依据下方检索到的资料回答问题，"
        "资料不足时明说不知道，不要编造；涉及图片时注明图片对应的商品名。"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"检索资料：\n{context}\n\n问题：{question}"},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    result = _post(base, payload, api_key)
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError(f"deepseek chat 返回异常: {sorted(result.keys())}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        raise LLMError("deepseek chat 返回空答案")
    return content
