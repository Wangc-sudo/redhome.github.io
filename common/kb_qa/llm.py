# -*- coding: utf-8 -*-
"""天翼云 DeepSeek 客户端（OpenAI 兼容）+ 三个业务调用点。

接入（2026-10-10 运维提供）：https://ai.ctaigw.cn/v1/chat/completions，
模型 deepseek-v4.1-flash-1017（2026-10-20 授权生效实测可用；旧名
deepseek-v4.1-flash 未授权 403）。全部配置走 env：
    KB_LLM_BASE_URL / KB_LLM_API_KEY / KB_LLM_MODEL / KB_LLM_TIMEOUT(可选,默认20s)

**fail-open 铁律**：LLM 不可达/未开通/超时/返回畸形时，调用方一律降级
到 P1 模板行为，绝不把异常甩给客服。from_environment() 配置缺失返回
None，handler 按「无 LLM」运行。

三个调用点的提示词来自方案《提示词设计》（2026-10-10 定稿版，按运维
「全数据非敏感」裁决移除 BLOCKED 类与敏感拦截条款）。
"""

import json
import os
import urllib.request

from common.kb_qa.context import format_history


class LlmError(RuntimeError):
    pass


class LlmClient:
    def __init__(self, base_url, api_key, model, *, timeout=20,
                 request=None):
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._request = request or self._http_post

    def _http_post(self, payload):
        request = urllib.request.Request(
            self._base_url,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode())

    def chat(self, messages, *, max_tokens=600, temperature=0.3):
        """单轮 chat，返回 content 文本；失败抛 LlmError（调用方降级）。"""
        payload = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        try:
            result = self._request(payload)
            return result["choices"][0]["message"]["content"].strip()
        except LlmError:
            raise
        except Exception as exc:
            raise LlmError(str(exc)[:120]) from None

    def chat_with_tools(self, messages, *, tools, max_tokens=2048,
                        temperature=0.3):
        """function calling（2026-10-21 实测 deepseek-v4.1-flash-1017 支持）。

        返回 {"content": str|None, "tool_calls": [...]}；失败抛 LlmError。
        tool_calls 原样透传（id/type/function{name,arguments}）。

        max_tokens 默认 2048：该模型是推理版，reasoning_tokens 与答案共享
        max_tokens 预算——600 曾被 reasoning 吃光导致 content 空、agent
        静默无答（2026-10-21「各区域目标完成排名」实测）。
        """
        payload = {
            "model": self._model,
            "messages": messages,
            "tools": list(tools),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        try:
            result = self._request(payload)
            message = result["choices"][0]["message"]
        except LlmError:
            raise
        except Exception as exc:
            raise LlmError(str(exc)[:120]) from None
        if not isinstance(message, dict):
            raise LlmError("tools chat: bad message shape")
        content = message.get("content")
        return {
            "content": content.strip() if isinstance(content, str) else None,
            "tool_calls": message.get("tool_calls") or [],
        }


def from_environment(environ=None):
    """env 齐备返回 LlmClient，缺失返回 None（无 LLM 降级运行）。"""
    values = os.environ if environ is None else environ
    base_url = (values.get("KB_LLM_BASE_URL") or "").strip()
    api_key = (values.get("KB_LLM_API_KEY") or "").strip()
    model = (values.get("KB_LLM_MODEL") or "").strip()
    if not (base_url and api_key and model):
        return None
    timeout = int(values.get("KB_LLM_TIMEOUT", "20"))
    return LlmClient(base_url, api_key, model, timeout=timeout)


# ---------------------------------------------------------------------------
# 调用点 1：查询理解（规则未命中时的二次解析，治「不准确」）
# ---------------------------------------------------------------------------

_QUERY_PROMPT = """你是商品知识库机器人的查询理解器。

# 任务
从客服的原始提问中提取查询意图和商品关键词。

# 可选类别
- PRODUCT_PARAM   查商品参数（箱规/尺寸/重量/材质/69码/品牌等）
- PRODUCT_FILTER  按条件筛选商品列表
- PRODUCT_ASSET   索取图片或文件（细节图/素材/质检报告）
- DOC_QA          产品卖点/品牌故事/工艺/对客话术
- CHITCHAT        问候闲聊与知识库无关

# 输出（严格 JSON，不要任何其他内容）
{"category": "<类别>", "keyword": "<商品名或检索关键词，没有就给空串>"}

# 规则
- keyword 只留有助于检索商品的词：去掉问句、语气词、「验收/测试」等无关词
- 问题里有具体商品名 → keyword 必须是商品名（可保留度数/规格等区分词）
- 问题里有「它/那/这款/价格呢」这类指代追问 → 按对话历史补全成完整
  商品名或检索词（如「价格呢」+ 上文习酒 → keyword=「习酒」）
- 历史末尾若有「上文涉及商品：X」，指代追问优先指向 X
{{history}}

# 提问
{{question}}"""


def extract_query(client, question, history=None):
    """LLM 二次解析提问，返回 (category, keyword)；失败抛 LlmError。

    history（对话层，2026-10-21）：最近几轮问答（见 context.format_history），
    供指代追问补全；None 与旧行为一致。
    """
    history_text = format_history(history) if history else ""
    raw = client.chat(
        [{"role": "user",
          "content": (_QUERY_PROMPT
                      .replace("{{history}}", history_text)
                      .replace("{{question}}", question))}],
        max_tokens=512, temperature=0.1,
    )
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise LlmError("query extract: non-JSON output")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        raise LlmError("query extract: bad JSON") from None
    category = data.get("category")
    keyword = data.get("keyword")
    if not isinstance(category, str) or not isinstance(keyword, str):
        raise LlmError("query extract: missing fields")
    return category.strip(), keyword.strip()


# ---------------------------------------------------------------------------
# 调用点 2：商品问答渲染（SQL 行 → 口语化答案，治「慢」——少一轮反问）
# ---------------------------------------------------------------------------

_PRODUCT_PROMPT = """你是酒类商品资料助手，服务对象是公司客服人员。

# 你的任务
根据下面的商品数据，回答客服的问题。

# 商品数据
{{rows_json}}

# 回答要求
1. 直接给结论，不要复述问题
2. 口语化表达，像同事之间说话；回答要能被客服直接复制转发给客户
3. 关键参数后标注来源，格式：【商品资料】
4. 数据里没有的字段，明确说"这条资料里没有"，禁止推测
5. 涉及多个商品时用列表，一个商品一行
6. 不要出现"根据以上数据""检索结果显示"等系统语
7. 回答控制在 200 字以内，除非用户明确要求详细
8. 问题里的「它/那/这款」等指代按对话历史理解，回答自然衔接不重复
{{history}}

# 客服的问题
{{question}}"""

#: 注入 LLM 的行字段（与 PARAM_COLUMNS 对齐，附件/审计字段不进上下文）。
_ROW_FIELDS = (
    "name", "code", "brand", "category", "box_qty", "case_qty",
    "size_mm", "weight_kg", "material", "gift_bag_spec",
    "barcode_single", "barcode_case",
)


def render_product_answer(client, question, rows, history=None):
    """SQL 行 → 口语化答案；失败抛 LlmError（调用方降模板渲染）。

    history（对话层）：最近几轮问答，指代与追问按历史理解；None 与旧行为一致。
    """
    slim = [
        {key: row.get(key) for key in _ROW_FIELDS if row.get(key) is not None}
        for row in rows
    ]
    prompt = (_PRODUCT_PROMPT
              .replace("{{rows_json}}",
                       json.dumps(slim, ensure_ascii=False, default=str))
              .replace("{{history}}",
                       format_history(history) if history else "")
              .replace("{{question}}", question))
    # max_tokens=2048：推理版模型 reasoning_tokens 与答案共享预算，
    # 500 会被推理吃光 → content 空 → 用户看到静默不回（2026-10-22
    # 实测「53°500ml*6习酒窖藏1988新包装2025款」audit reply_kind=none）。
    return client.chat([{"role": "user", "content": prompt}],
                       max_tokens=2048, temperature=0.3)


# ---------------------------------------------------------------------------
# 调用点 3：手册问答（BM25 召回切片 → 带出处答案）
# ---------------------------------------------------------------------------

_DOC_PROMPT = """你是品牌与产品知识助手，服务对象是公司客服人员。

# 你的任务
根据下面的产品手册内容，回答客服的问题。

# 参考内容
{{chunks}}

# 回答要求
1. 只使用参考内容中的信息，不得引入任何外部知识
2. 参考内容不足以回答时，直接说"手册里没有这部分内容"，不要编
3. 口语化、可用于对客沟通；若为卖点类问题，整理成可直接使用的销售话术
4. 末尾标注来源，格式：【引自《文档标题》】
5. 不要出现"根据检索""上下文提到"等系统语
6. 问题里的「它/那/这款」等指代按对话历史理解，回答自然衔接不重复
{{history}}

# 客服的问题
{{question}}"""


def render_doc_answer(client, question, chunks, history=None):
    """手册切片 → 带出处答案；失败抛 LlmError。

    history（对话层）：最近几轮问答；None 与旧行为一致。
    """
    blocks = "\n\n".join(
        f"【{chunk['doc_title']}】{chunk['content']}" for chunk in chunks
    )
    prompt = (_DOC_PROMPT
              .replace("{{chunks}}", blocks)
              .replace("{{history}}",
                       format_history(history) if history else "")
              .replace("{{question}}", question))
    # max_tokens=2048：同 render_product_answer 的推理版预算坑。
    return client.chat([{"role": "user", "content": prompt}],
                       max_tokens=2048, temperature=0.3)
