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
5. 上述工具答不了的业务数据问题（订单/出库/退款/库存/人员目标等）→
   query_sql 写一条 SELECT（只能用白名单表，大表必须带日期过滤，
   结果超 100 行用 GROUP BY 聚合）
6. 不需要工具的闲聊 → 直接简短回答，不要调用工具

# 回答要求
- 工具结果里没有的信息如实说"查不到"，禁止编造数字与参数
- 口语化、客服可直接转发；数据类回答带上日期与单位

# query_sql 可用表（含口径，别编造表与列）
{{sql_tables}}

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
    {
        "type": "function",
        "function": {
            "name": "query_sql",
            "description": "受控 SQL 查询业务库（只读 SELECT，白名单表，"
                           "自动 LIMIT，大表必须带日期过滤）。用于其他工具"
                           "答不了的订单/出库/退款/库存/人员目标类问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string",
                            "description": "单条 SELECT 语句（MySQL 方言）"},
                },
                "required": ["sql"],
            },
        },
    },
]

#: 工具结果给 LLM 的截断长度（控 token，防单行数据撑爆上下文）。
_RESULT_LIMIT = 2000

# ---------------------------------------------------------------------------
# 受控 SQL（query_sql，2026-10-21 裁决方案 B：Text-to-SQL + 多层护栏）
#
# 防线（缺一不可）：
#   1. 语句形态：只允许单条 SELECT/WITH（多语句、写操作、管理语句全拒）；
#   2. 表白名单：FROM/JOIN 的表必须在 _SQL_TABLES（带口径注释，防编造）；
#   3. 强制 LIMIT：缺省追加 100，上限 clamp 500；
#   4. EXPLAIN 预检：估算扫描行数超阈值直接拒（防全表/JOIN 放大打挂
#      生产库——mart_ops 上还有 scheduler/播报/BI）；
#   5. 语句超时：SET SESSION MAX_EXECUTION_TIME（毫秒，MySQL 5.7.8+，
#      只对只读 SELECT 生效）；
#   6. 结果截断：返回 LLM ≤50 行、单元格截 200 字。
# ---------------------------------------------------------------------------

#: 白名单表与口径注释（喂 LLM 的 schema 摘要；口径正确性比能跑更重要）。
_SQL_TABLES = {
    "agg_offline_daily":
        "线下区域日聚合：stat_date 日期, scope 区域(hangzhou/shaoxing/vanke/"
        "qudao/junpin/offline_all), sales_amount 销售额(元), dod_rate 日环比,"
        " wow_rate 周环比, month_target 月目标(元), month_rate 月完成率",
    "fact_channel_daily_sales":
        "渠道日销：channel 渠道, store_name 店铺, business_date 日期,"
        " sales_amount 销售额(元), promotion_cost 推广费(元), roi",
    "fact_order_line":
        "订单行（约10万行，必须带 trade_time 日期过滤）：trade_time 下单时间,"
        " shop_name 店铺, channel_name 渠道, goods_name 商品, brand_name 品牌,"
        " quantity 数量, paid_amount 实付(元), trade_status 状态",
    "fact_stockout_line":
        "出库行（约10万行，必须带 consign_time 日期过滤）：consign_time 发货"
        "时间, warehouse_name 仓, shop_name 店铺, channel_name 渠道,"
        " goods_name 商品, quantity 数量, sell_price 售价(元)",
    "fact_refund_line":
        "退款行：check_time 审核时间, process_status 状态, shop_name 店铺,"
        " channel_name 渠道, reason 退款原因, goods_name 商品, quantity 数量,"
        " refund_amount 退款额(元)",
    "fact_purchase_inbound":
        "采购入库：stockin_time 入库时间, warehouse_name 仓, goods_name 商品,"
        " qty 数量, status 状态",
    "fact_sales_daily":
        "每日销量（按规格编码）：business_date 日期, spec_no 规格编码,"
        " qty_sold 销量",
    "dim_report_roster":
        "人员花名册：scope 区域, person_name 姓名, aliases 别名(json数组),"
        " channel 渠道, role 角色, enabled 是否启用",
    "dim_report_target":
        "月度目标：scope 区域, person_name 姓名, year_month 月份(YYYY-MM),"
        " monthly_target 目标(元)",
    "kb_products":
        "商品资料：code 商品编码, name 名称, brand 品牌, category 系列,"
        " box_qty 箱规, case_qty 盒规, size_mm 尺寸, weight_kg 重量,"
        " gift_bag_spec 礼袋规格, barcode_single/barcode_case 69码",
}

_SQL_FORBIDDEN_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|SET|"
    r"USE|SHOW|DESCRIBE|EXPLAIN|INTO|LOAD|CALL|EXEC|HANDLER|LOCK|"
    r"UNLOCK|KILL|REPLACE)\b",
    re.IGNORECASE)
_SQL_SCHEMA_RE = re.compile(
    r"\b(information_schema|performance_schema|mysql|sys)\s*\.", re.IGNORECASE)
_SQL_TABLE_RE = re.compile(r"\b(?:FROM|JOIN)\s+`?([A-Za-z_]\w*)`?",
                           re.IGNORECASE)

#: EXPLAIN 估算扫描行数阈值（行）；订单行/出库行全表 106k，JOIN 放大防到 50 万。
_SQL_EXPLAIN_ROW_LIMIT = 500_000
#: 语句超时（毫秒，SET SESSION MAX_EXECUTION_TIME）。
_SQL_TIMEOUT_MS = 5000
#: 强制 LIMIT：缺省值与上限。
_SQL_DEFAULT_LIMIT = 100
_SQL_MAX_LIMIT = 500
#: 返回给 LLM 的最大行数与单元格长度。
_SQL_RESULT_ROWS = 50
_SQL_CELL_LIMIT = 200


class SqlRejected(ValueError):
    """SQL 未通过护栏校验（原因文本可直接回喂 LLM 修正）。"""


def validate_sql(sql):
    """受控 SQL 校验与规整，返回可执行 SQL；不通过抛 SqlRejected（原因）。"""
    text = (sql or "").strip()
    if not text:
        raise SqlRejected("empty sql")
    if ";" in text.rstrip(";"):
        raise SqlRejected("只允许单条语句")
    text = text.rstrip(";").strip()
    first = text.split(None, 1)[0].upper() if text.split(None, 1) else ""
    if first not in ("SELECT", "WITH"):
        raise SqlRejected("只允许 SELECT 查询")
    match = _SQL_FORBIDDEN_RE.search(text)
    if match:
        raise SqlRejected(f"含禁止语句: {match.group(1)}")
    if _SQL_SCHEMA_RE.search(text):
        raise SqlRejected("不允许访问系统库")
    tables = {t.lower() for t in _SQL_TABLE_RE.findall(text)}
    if not tables:
        raise SqlRejected("未找到 FROM 表")
    # CTE 别名（WITH t AS (...)）不是物理表，从校验集合剔除
    cte_names = {name.lower() for name in re.findall(
        r"\bWITH\s+`?(\w+)`?\s+AS\s*\(", text, re.IGNORECASE)}
    unknown = tables - cte_names - set(_SQL_TABLES)
    if unknown:
        raise SqlRejected(
            f"表不在白名单: {sorted(unknown)}（可用: {sorted(_SQL_TABLES)}）")
    limit_match = re.search(r"\bLIMIT\s+(\d+)", text, re.IGNORECASE)
    if not limit_match:
        text = f"{text} LIMIT {_SQL_DEFAULT_LIMIT}"
    elif int(limit_match.group(1)) > _SQL_MAX_LIMIT:
        text = (text[:limit_match.start(1)] + str(_SQL_MAX_LIMIT)
                + text[limit_match.end(1):])
    return text

# DSML 文本工具调用（2026-10-21 实测：deepseek-v4.1-flash-1017 偶发不走
# 原生 tool_calls 协议，而把调用写成 ``<｜｜DSML｜｜ calls>…`` 标记文本塞在
# content 里——用户曾收到这段原文）。分隔符全角 ``｜``，兼容半角 ``|``。
_DSML_CALLS_RE = re.compile(
    r"<[|｜]{2}DSML[|｜]{2}\s*calls>(.*?)</[|｜]{2}DSML[|｜]{2}\s*calls>",
    re.S)
_DSML_INVOKE_RE = re.compile(
    r"<[|｜]{2}DSML[|｜]{2}\s*invoke\s+name=\"(?P<name>[^\"]+)\"\s*>"
    r"(?P<body>.*?)</[|｜]{2}DSML[|｜]{2}\s*invoke>",
    re.S)
_DSML_PARAM_RE = re.compile(
    r"<[|｜]{2}DSML[|｜]{2}\s*parameter\s+name=\"(?P<name>[^\"]+)\"[^>]*>"
    r"(?P<value>.*?)</[|｜]{2}DSML[|｜]{2}\s*parameter>",
    re.S)


def parse_dsml_calls(content):
    """content 里的 DSML 工具调用 → 与原生 tool_calls 同构的列表；无则 []。

    输出项 ``{"id", "type", "function": {"name", "arguments"}}``，arguments
    为 JSON 字符串（与原生协议对齐，直接进既有执行路径）。
    """
    if not content or "DSML" not in content:
        return []
    body_match = _DSML_CALLS_RE.search(content)
    if not body_match:
        return []
    calls = []
    for index, invoke in enumerate(
            _DSML_INVOKE_RE.finditer(body_match.group(1))):
        params = {
            param.group("name"): param.group("value").strip()
            for param in _DSML_PARAM_RE.finditer(invoke.group("body"))
        }
        calls.append({
            "id": f"dsml-{index}",
            "type": "function",
            "function": {
                "name": invoke.group("name"),
                "arguments": json.dumps(params, ensure_ascii=False),
            },
        })
    return calls


def has_dsml_residue(content):
    """content 含 DSML 标记残留（调用了但解析不出来/答案里带乱码）。

    残留绝不能原样发给客服——调用方据此降级。
    """
    return bool(content) and "DSML" in content


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

    def _tool_query_sql(self, args):
        try:
            sql = validate_sql(args.get("sql"))
        except SqlRejected as exc:
            return {"error": f"SQL 未通过安全检查: {exc}"}
        cursor = self._conn.cursor()
        try:
            # 成本预检：估算扫描行数超阈值直接拒（防全表/JOIN 放大）。
            cursor.execute(f"EXPLAIN {sql}")
            plan_rows = 0
            for plan in cursor.fetchall():
                estimate = plan.get("rows") or 0
                try:
                    plan_rows += int(estimate)
                except (TypeError, ValueError):
                    continue
            if plan_rows > _SQL_EXPLAIN_ROW_LIMIT:
                return {"error": f"查询估算扫描 {plan_rows} 行，超阈值 "
                                 f"{_SQL_EXPLAIN_ROW_LIMIT}——请加日期过滤或"
                                 " GROUP BY 聚合后重试"}
            cursor.execute(f"SET SESSION MAX_EXECUTION_TIME={_SQL_TIMEOUT_MS}")
            cursor.execute(sql)
            rows = cursor.fetchall()
        finally:
            cursor.close()
        truncated = [
            {key: (str(value)[:_SQL_CELL_LIMIT] if value is not None else None)
             for key, value in row.items()}
            for row in rows[:_SQL_RESULT_ROWS]
        ]
        result = {"total_rows": len(rows), "rows": truncated}
        # 自我压缩到结果预算内——外层 execute 的 _RESULT_LIMIT 硬截会破坏
        # JSON 结构（2026-10-21 实测），行数由这里减，不给外层截的机会。
        budget = _RESULT_LIMIT - 200
        while (result["rows"]
               and len(json.dumps(result, ensure_ascii=False,
                                  default=str)) > budget):
            result["rows"].pop()
        result["returned"] = len(result["rows"])
        return result

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
    tables_text = "\n".join(f"- {name}：{desc}"
                            for name, desc in _SQL_TABLES.items())
    messages = [
        {"role": "system",
         "content": (_SYSTEM_PROMPT
                     .replace("{{sql_tables}}", tables_text)
                     .replace("{{history}}", history_text))},
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
        content = response.get("content") or ""
        if not tool_calls and content:
            # DSML 偶发形态：调用写在文本里而非协议字段——解析出来按原生
            # 调用照常执行，原文不当答案发出去（2026-10-21 用户收到乱码）。
            tool_calls = parse_dsml_calls(content)
            if tool_calls:
                content = ""
        if not tool_calls:
            content = content.strip()
            if has_dsml_residue(content):
                return None  # 残留乱码绝不发给客服，降级由调用方决定
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
    if has_dsml_residue(final):
        return None
    return final or None
