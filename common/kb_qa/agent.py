# -*- coding: utf-8 -*-
"""工具调用 agent：LLM 自主决定查商品 / 查文档 / 查业务数据（2026-10-21）。

定位：规则路由的补充而非替代——规则高置信命中（商品参数/筛选/手册词）
仍走原有路径（成本时延不变）；**规则 miss 的消息**（含业务数据问句
「今天杭州出了多少货」这类规则接不住的）进入 agent loop。

协议：OpenAI 原生 function calling（2026-10-21 实测 deepseek-v4.1-flash-1017
返回 tool_calls ✓）。loop 上限 2 轮工具调用防循环，超限时让 LLM 用已
有结果直接作答。

安全铁律（不可协商）：
- LLM 只能「选工具 + 给参数」，**永不执行 LLM 生成的 SQL**；
- 工具集是预定义只读查询，scope/channel 白名单校验、date 格式校验，
  参数全部走 SQL 占位符——注入面为零；
- 业务工具只读 mart_ops 聚合/事实表，不写。

fail-open：LLM 异常/返回畸形/工具执行异常 → 返回 None，调用方降级
（HELP_TEXT 或原有兜底），绝不把异常甩给客服。
"""

import json
import re

from common.kb_qa.context import format_history
from common.kb_qa.llm import LlmError

#: 工具调用轮数上限（第一轮决策+调用，第二轮基于结果再决策或直接作答）。
MAX_TOOL_ROUNDS = 2

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SYSTEM_PROMPT = """你是公司客服群的业务助手，可以调用工具获取数据后回答。

# 工具选择
1. 商品参数（箱规/尺寸/重量/材质/条码/礼袋）或按条件筛选商品 → search_products
2. 产品手册/品牌工艺/卖点话术/售后流程/销售总结规划 → search_docs
3. 某区域销售额/出货量/业绩/完成率/环比 → daily_sales
4. 各渠道销售额汇总对比 → channel_sales
5. 不需要工具的闲聊 → 直接简短回答，不要调用工具

# 回答要求
- 工具结果里没有的信息如实说"查不到"，禁止编造数字与参数
- 口语化、客服可直接转发；数据类回答带上日期与单位
{{history}}"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "查询商品资料：箱规/尺寸/重量/材质/条码/礼袋规格"
                           "等参数，或按条件筛选商品",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string",
                                "description": "商品名或筛选关键词"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "检索产品手册与业务文档（品牌工艺/卖点话术/售后"
                           "流程/销售总结与规划）",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索问题"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "daily_sales",
            "description": "查询某区域某日销售：销售额、日环比、月目标完成率",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string",
                              "description": "区域标识（如 hangzhou）"},
                    "date": {"type": "string",
                             "description": "YYYY-MM-DD，缺省为最近一天"},
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "channel_sales",
            "description": "按渠道汇总某日销售额（可指定单一渠道）",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string",
                                "description": "渠道名，留空为全部渠道"},
                    "date": {"type": "string",
                             "description": "YYYY-MM-DD，缺省为最近一天"},
                },
            },
        },
    },
]

#: 工具结果给 LLM 的截断长度（控 token，防单行数据撑爆上下文）。
_RESULT_LIMIT = 2000


class ToolExecutor:
    """白名单工具执行器。所有 SQL 为预定义形态，参数走占位符。"""

    def __init__(self, conn, *, doc_search=None, vector_search=None,
                 now=None):
        self._conn = conn
        self._doc_search = doc_search
        self._vector_search = vector_search
        self._now = now
        self._scopes = None
        self._channels = None

    # ------------------------------------------------------------------
    def execute(self, name, arguments):
        """执行一个工具调用，返回给 LLM 的结果文本（JSON 字符串）。"""
        args = arguments if isinstance(arguments, dict) else {}
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return json.dumps({"error": f"unknown tool: {name}"},
                              ensure_ascii=False)
        return json.dumps(handler(args), ensure_ascii=False, default=str)[
            :_RESULT_LIMIT]

    # ------------------------------------------------------------------
    def _tool_search_products(self, args):
        from common.kb_qa.products import find_products

        keyword = str(args.get("keyword") or "").strip()
        if not keyword:
            return {"error": "keyword required"}
        rows = find_products(self._conn, keyword)
        return {
            "count": len(rows),
            "products": [
                {key: row.get(key) for key in (
                    "name", "code", "brand", "category", "box_qty",
                    "case_qty", "size_mm", "weight_kg", "material",
                    "gift_bag_spec", "barcode_single", "barcode_case",
                    "record_id")
                 if row.get(key) is not None}
                for row in rows[:5]
            ],
        }

    def _tool_search_docs(self, args):
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "query required"}
        chunks = []
        if self._doc_search is not None:
            chunks.extend(self._doc_search(self._conn, query, k=2))
        if self._vector_search is not None:
            try:
                chunks.extend(self._vector_search(query)[:2])
            except Exception:
                pass
        return {
            "count": len(chunks),
            "chunks": [
                {"doc_title": c.get("doc_title"),
                 "content": (c.get("content") or "")[:400]}
                for c in chunks[:4]
            ],
        }

    def _tool_daily_sales(self, args):
        scope = str(args.get("scope") or "").strip()
        if not scope:
            return {"error": "scope required"}
        scopes = self._valid_scopes()
        if scope not in scopes:
            return {"error": f"unknown scope: {scope}",
                    "valid_scopes": sorted(scopes)[:20]}
        date = str(args.get("date") or "").strip()
        if date and not _DATE_RE.match(date):
            return {"error": "date must be YYYY-MM-DD"}
        cursor = self._conn.cursor()
        try:
            if date:
                cursor.execute(
                    "SELECT `stat_date`, `scope`, `sales_amount`, `dod_rate`,"
                    " `wow_rate`, `month_target`, `month_rate`"
                    " FROM `agg_offline_daily`"
                    " WHERE `scope`=%s AND `stat_date`<=%s"
                    " ORDER BY `stat_date` DESC LIMIT 1",
                    (scope, date))
            else:
                cursor.execute(
                    "SELECT `stat_date`, `scope`, `sales_amount`, `dod_rate`,"
                    " `wow_rate`, `month_target`, `month_rate`"
                    " FROM `agg_offline_daily` WHERE `scope`=%s"
                    " ORDER BY `stat_date` DESC LIMIT 1",
                    (scope,))
            row = cursor.fetchone()
        finally:
            cursor.close()
        if not row:
            return {"found": False, "scope": scope, "date": date or "latest"}
        row["found"] = True
        return row

    def _tool_channel_sales(self, args):
        date = str(args.get("date") or "").strip()
        if date and not _DATE_RE.match(date):
            return {"error": "date must be YYYY-MM-DD"}
        channel = str(args.get("channel") or "").strip()
        if channel:
            channels = self._valid_channels()
            if channel not in channels:
                return {"error": f"unknown channel: {channel}",
                        "valid_channels": sorted(channels)[:20]}
        cursor = self._conn.cursor()
        try:
            sql = ("SELECT `channel`, `business_date`,"
                   " SUM(`sales_amount`) AS `sales_amount`"
                   " FROM `fact_channel_daily_sales`")
            conditions, params = [], []
            if date:
                conditions.append("`business_date`=%s")
                params.append(date)
            if channel:
                conditions.append("`channel`=%s")
                params.append(channel)
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += (" GROUP BY `channel`, `business_date`"
                    " ORDER BY `business_date` DESC, `sales_amount` DESC"
                    " LIMIT 10")
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        finally:
            cursor.close()
        return {"count": len(rows), "rows": rows}

    # ------------------------------------------------------------------
    def _valid_scopes(self):
        if self._scopes is None:
            cursor = self._conn.cursor()
            try:
                cursor.execute("SELECT DISTINCT `scope` FROM `agg_offline_daily`")
                self._scopes = {r["scope"] for r in cursor.fetchall()
                                if r.get("scope")}
            finally:
                cursor.close()
        return self._scopes

    def _valid_channels(self):
        if self._channels is None:
            cursor = self._conn.cursor()
            try:
                cursor.execute(
                    "SELECT DISTINCT `channel` FROM `fact_channel_daily_sales`")
                self._channels = {r["channel"] for r in cursor.fetchall()
                                  if r.get("channel")}
            finally:
                cursor.close()
        return self._channels


def run_agent(llm, question, *, executor, history=None,
              max_rounds=MAX_TOOL_ROUNDS, chat_with_tools=None):
    """agent loop：问题 → 工具调用（≤max_rounds 轮）→ 答案。

    返回答案文本；LLM 异常/畸形/超限无答案 → None（调用方降级）。
    chat_with_tools 注入点（测试替身），缺省调 llm.chat_with_tools。
    """
    call = chat_with_tools or llm.chat_with_tools
    history_text = format_history(history) if history else ""
    messages = [
        {"role": "system",
         "content": _SYSTEM_PROMPT.replace("{{history}}", history_text)},
        {"role": "user", "content": question},
    ]
    for _ in range(max_rounds):
        try:
            response = call(messages, tools=TOOLS)
        except LlmError:
            return None
        except Exception:
            return None
        tool_calls = response.get("tool_calls") or []
        if not tool_calls:
            content = (response.get("content") or "").strip()
            return content or None
        messages.append({"role": "assistant",
                         "content": response.get("content") or "",
                         "tool_calls": tool_calls})
        for call_info in tool_calls[:3]:
            function = (call_info or {}).get("function") or {}
            name = function.get("name")
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except (TypeError, json.JSONDecodeError):
                arguments = {}
            try:
                result = executor.execute(name, arguments)
            except Exception:
                result = json.dumps({"error": "tool execution failed"},
                                    ensure_ascii=False)
            messages.append({
                "role": "tool",
                "tool_call_id": call_info.get("id") or "",
                "name": name or "",
                "content": result,
            })
    # 轮数用尽：让 LLM 基于已有工具结果直接作答（不再给工具）。
    try:
        final = llm.chat(messages, max_tokens=500, temperature=0.3)
    except LlmError:
        return None
    return final or None
