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

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from common import cache_core
from common.kb_qa.context import format_history
from common.kb_qa.focus import compress_rows, parse_focus
from common.kb_qa.llm import LlmError
from common.kb_qa.render import render_answer
from common.kb_qa.result import from_payload
from common.kb_qa.skills import compile_tools, validate_handlers

#: 工具调用轮数上限（LLM 调用次数，含作答）。2 曾不够：「各区域完成
#: 排名」要先查基准日期再算完成率——两轮全被工具占满，作答只能走轮尽
#: chat 而该场景轮尽输出空（2026-10-21 实测）。3 给「工具→工具→作答」
#: 留出席位。
MAX_TOOL_ROUNDS = 3

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CHANNEL_YOY_RE = re.compile(r"同比|去年同期")

_SYSTEM_PROMPT = """你是公司客服群的业务助手，可以调用工具获取数据后回答。

# 工具选择
1. 商品参数（箱规/尺寸/重量/材质/条码/礼袋）或按条件筛选商品 → search_products
2. 产品手册/品牌工艺/卖点话术/售后流程/销售总结规划 → search_docs
3. 某区域销售额/出货量/业绩/完成率/环比 → daily_sales
4. 各渠道某日销售额汇总 → channel_sales（AI 表格日销口径）
5. 渠道/店铺的销售额同比/去年同期（含按店铺拆分）→ channel_yoy，
   必须使用 DB 订单行与 dim_channel_map 渠道映射，两期同源同口径；
   has_previous_data=false 时必须明示去年同期缺数，禁止改用 channel_sales
   或静默改成环比；同比对象是商品/系列（如「金质」）时用 sku_sales
6. 商品库统计（总数、按品牌/大类分布）→ product_stats
7. 公司手机号是谁在用/尾号查人/平台账号的验证码找谁要 → phone_lookup；
   结果 requires_clarification=true 时列出候选请用户选择，不得猜属主
8. 按单品/SKU/款/系列 的销量、销售额、排行、哪款卖得好 → sku_sales；
   商品/系列销售额的同比也用它——本期与去年同期各调一次再对比
9. 库存、可售天数、呆滞、缺货、在途 → sku_inventory（最新快照）
10. 具体订单/交易单/物流单 → query_sql 精确查 fact_order_line（trade_no）
   与 fact_stockout_line（trade_no/logistics_no），不要只做聚合；其他业务
   数据问题（出库/退款/库存/人员目标等）也走 query_sql（只用白名单表，
   大表必须带日期过滤，结果超 100 行用 GROUP BY 聚合）
11. 运营周报/月报/经营概览（「做个10月运营周报」「本期经营怎么样」）
   → sales_report：总量+环比+渠道分布+TOP店铺+TOP单品+库存一次拿全，
   禁止拆成多次 query_sql 自己拼
12. 不需要工具的闲聊 → 直接简短回答，不要调用工具

# 回答要求
- 工具结果里没有的信息如实说"查不到"，禁止编造数字与参数
- 用户只回"可以/好/嗯"等纯确认词时，只能执行紧邻上一轮助手结尾的
  提议；上一轮没有明确提议就问清用户要做什么，禁止从更早历史里挑任务
- 口语化、客服可直接转发；数据类回答带上日期与单位
- 查询被护栏拒绝或超时时，必须自行修正后重试：日期过滤改用范围谓词
  （不要 DATE() 包裹列）、缩小日期范围或分段查询；禁止把「请回复分段跑」
  这类补救动作推给用户，只有重试后仍拿不到才如实说明

# 答复纪律（2026-11-02，客服群反馈"不相关的内容可以不输出吗"）
- 只答被问到的对象与指标；未被索要的"其它店铺/供参考/顺便"数据一律不输出
- 格式：结论一句 → 数据（带日期与单位）→ 注释限一句放末尾
- 缺数一律表述为"无数据/—"，**禁止写成 0**；缺数说明一句话即可，不要
  展开成多行「特别说明」
- 禁止输出内部字段名与协议词（has_previous_data、工具名等），禁止
  "问题理解：""AI 回答说明："这类自解释标题
- 不要回显用户的问题；命中多个规格/店铺时列清单让用户确认，不猜不合并
- 数据表格与结论卡由系统统一渲染：正文不要手写 Markdown 表格、不要
  复述卡片中的合计与数字，只写业务解读与必要说明（2026-11-02 显示层）

# query_sql 可用表（含口径，别编造表与列）
{{sql_tables}}

{{history}}"""

TOOLS = compile_tools()

#: 工具结果给 LLM 的截断长度（控 token，防单行数据撑爆上下文）。
_RESULT_LIMIT = 2000

#: SKU 工具返回条数：缺省值与上限（2026-11-02 方案 §5）。
_SKU_TOP_N_DEFAULT = 10
_SKU_TOP_N_MAX = 30
#: 库存三态口径（与投影层 extract_channel_ops 同源，答复必须带一句）。
_STOCK_STATE_NOTE = (
    "URGENT=可售天数<7 天（需补货）；OVERSOLD=可用库存为负（超卖）；"
    "DEAD=近 30 天零出库（呆滞，与库存量无关）"
)
_STOCK_STATES = ("URGENT", "OVERSOLD", "DEAD")

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
        " sales_amount 销售额(元), promotion_cost 推广费(元), roi。"
        " **注意：上游 AI 表预置了到月底的空行（未来日期、金额全 NULL/0"
        " 每天 21 行），查询必须加 business_date<=CURDATE() 过滤，否则"
        " MAX(business_date) 锚点会落在未来的空行上（2026-10-21 实测"
        " 裸 MAX=10-31 而真实最新=10-20）**",
    "fact_order_line":
        "订单行（约84万行，必须带 trade_time 日期过滤；按单号精确查询时用"
        " trade_no=%s 并保留合理日期范围）：trade_no 交易单号, trade_time 下单"
        "时间, buyer_nick 买家, shop_name 店铺, channel_name 渠道, goods_name"
        " 商品, brand_name 品牌, quantity 数量, paid_amount 实付(元),"
        " trade_status 状态。"
        " **日期过滤必须写范围谓词 trade_time>='YYYY-MM-DD' AND"
        " trade_time<'YYYY-MM-DD'，禁止 DATE(trade_time) 包裹列——函数包裹会"
        "让 idx_order_line_time 失效变全表扫描，被 EXPLAIN 护栏拒绝"
        "（2026-11-02 实测：范围写法 21 天聚合 0.2s，DATE() 写法估算 70 万行"
        "超阈值）**",
    "fact_stockout_line":
        "出库行（大表，必须带 consign_time 日期过滤（同样禁止 DATE()"
        "包裹列，用范围谓词）；可按 trade_no 或"
        " logistics_no 精确查询）：trade_no 交易单号, logistics_no 物流单号,"
        " logistics_name 物流公司, consign_time 发货时间, warehouse_name 仓,"
        " shop_name 店铺, channel_name 渠道, goods_name 商品, quantity 数量,"
        " sell_price 售价(元)",
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
    "fact_inventory_sku_daily":
        "库存日快照（**只有最新 business_date 是当前库存，历史日是留档**，"
        "先 MAX(business_date) 再查）：spec_no 规格编码, goods_name 商品,"
        " warehouse_scope 仓范围(ALL=跨仓合并), available_qty 可用库存,"
        " qty_7days 近7天出库, qty_month 近30天出库, purchase_intransit_qty"
        " 采购在途, daily_avg 日均出库, days_left 可售天数, stock_state"
        " 状态(HEALTHY 正常/URGENT 可售<7天/OVERSOLD 可用为负/DEAD 近30天"
        "零出库——呆滞与库存量无关)",
    "dim_report_roster":
        "人员花名册：scope 区域, person_name 姓名, aliases 别名(json数组),"
        " channel 渠道, role 角色, enabled 是否启用",
    "dim_report_target":
        "月度目标：scope 区域, person_name 姓名, `year_month` 月份(YYYY-MM"
        " 字符串，**是 MySQL 保留字，SQL 里必须带反引号**),"
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

#: EXPLAIN 估算扫描行数阈值（行）。订单行 2026-10 回填后全表约 84 万——
#: 全表扫描必超阈值（正是要的效果）；范围谓词走 idx_order_line_time 的
#: 21 天聚合估算仅约 7 万（2026-11-02 云上实测），阈值保持不变。
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
                 now=None, focus=None, question="", cache_store=None):
        self._conn = conn
        self._doc_search = doc_search
        self._vector_search = vector_search
        self._now = now
        self._scopes = None
        self._channels = None
        #: R5 查询结果缓存（D4a）：None=不启用（测试与既有行为）；
        #: 生产装配传 cache_core.store_from_environment()，TTL 60s。
        self._cache_store = cache_store
        #: 问句焦点（focus.py，2026-11-02）：只用于「喂 LLM 的结果聚焦
        #: 压缩」，不影响任何 SQL 与取值；None 时行为与旧版完全一致。
        self._focus = focus
        self._question = question or ""
        #: phone_lookup 命中后记录持卡人姓名（out-param：装配层据此把群
        #: 回复 @ 到持卡人，2026-10-26 用户裁决「小易要 @ 对应的人」）。
        self.phone_holders = []
        self.phone_candidates = []
        self.phone_query = ""
        self.phone_not_found = False
        self.query_snapshots = []
        self.index_telemetry = {}
        self.export_error_summary = None

    # ------------------------------------------------------------------
    def execute(self, name, arguments):
        """执行一个工具调用，返回给 LLM 的结果文本（JSON 字符串）。"""
        args = arguments if isinstance(arguments, dict) else {}
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return json.dumps({"error": f"unknown tool: {name}"},
                              ensure_ascii=False)
        result = handler(args)
        if isinstance(result, dict) and not result.get("error"):
            self._capture_query_snapshot(name, args, result)
        return json.dumps(result, ensure_ascii=False, default=str)[
            :_RESULT_LIMIT]

    def _snapshot_freshness(self, name):
        # 新鲜度 mapping 由技能声明派生（Task 2）：snapshot=数据集/索引键，
        # snapshot_kind=dataset|knowledge，新工具不再改这里的硬编码。
        from common.kb_qa.skills import get_skill

        skill = get_skill(name)
        if skill is None or not skill.snapshot:
            return "unknown"
        if skill.snapshot_kind == "knowledge":
            target = ("pd_knowledge_index_freshness", "index_key",
                      skill.snapshot)
        else:
            target = ("pd_dataset_freshness", "dataset_key", skill.snapshot)
        table, column, key = target
        cursor = self._conn.cursor()
        try:
            fields = "`status`"
            if table == "pd_knowledge_index_freshness":
                fields += ", `embedding_model`, `dim`, `index_version`"
            cursor.execute(
                f"SELECT {fields} FROM `{table}` WHERE `{column}`=%s",
                (key,),
            )
            row = cursor.fetchone()
            status = str((row or {}).get("status") or "unknown")
            if row and table == "pd_knowledge_index_freshness":
                self.index_telemetry = {
                    "embedding_model": row.get("embedding_model"),
                    "embedding_dim": row.get("dim"),
                    "index_version": row.get("index_version"),
                }
            if status not in {"fresh", "unknown"}:
                self.export_error_summary = f"{key}:{status}"[:255]
            return status
        except Exception:
            return "unknown"
        finally:
            cursor.close()

    def _capture_query_snapshot(self, name, args, result):
        rows = result.pop("_export_rows", None)
        effective_params = result.pop("_export_params", None) or dict(args)
        if name == "query_sql":
            rows = rows if rows is not None else list(result.get("rows") or ())
        elif name in {"channel_sales", "channel_yoy", "sku_sales",
                      "sku_inventory"}:
            rows = rows if rows is not None else list(result.get("rows") or ())
        elif name == "daily_sales":
            rows = [dict(result)] if result.get("found") else []
        elif name == "product_stats":
            rows = [{"item": "商品总数", "name": "全部", "count": result.get("total", 0)}]
            rows += [
                {"item": label, "name": row.get("name"), "count": row.get("count")}
                for label, key in (("品牌", "by_brand"), ("大类", "by_category"))
                for row in result.get(key) or ()
            ]
        else:
            return
        business_dates = [
            str(row.get("business_date") or row.get("date"))
            for row in rows
            if row.get("business_date") or row.get("date")
        ]
        self.query_snapshots.append({
            "tool_name": name,
            "effective_params": effective_params,
            "rows": rows,
            "data_scope": {
                key: value for key, value in effective_params.items() if key != "sql"
            },
            "data_version": (max(business_dates) if business_dates
                             else result.get("snapshot_date")),
            "freshness_status": self._snapshot_freshness(name),
        })

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

    def _tool_product_stats(self, args):
        """商品库统计（2026-10-24）：总数 + 按品牌/大类分布。

        补「有多少种商品」这类统计问题的空白——search_products 是关键词
        检索答不了计数（用户实测 agent 连查三次只能拒答）。商品库全员
        开放，本工具不在 Web 业务白名单收窄面内。
        """
        cursor = self._conn.cursor()
        try:
            cursor.execute("SELECT COUNT(*) AS c FROM `kb_products`")
            total = (cursor.fetchone() or {}).get("c", 0)
            by_brand = self._group_counts(cursor, "brand")
            by_category = self._group_counts(cursor, "category")
        finally:
            cursor.close()
        return {"total": total, "by_brand": by_brand,
                "by_category": by_category}

    @staticmethod
    def _group_counts(cursor, column):
        cursor.execute(
            f"SELECT `{column}` AS k, COUNT(*) AS c FROM `kb_products`"
            f" GROUP BY `{column}` ORDER BY c DESC LIMIT 10")
        return [{"name": row["k"], "count": row["c"]}
                for row in cursor.fetchall() if row.get("k")]

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

    def _tool_phone_lookup(self, args):
        """号码台账检索（2026-10-26）：「尾号 5690 是谁」「XX 店铺验证码
        找谁要」。精确 SQL 查 dim_phone_number/dim_phone_binding——
        精确映射场景不走向量（错配=验证码找错人）。全员开放（同
        product_stats），不在 Web 业务收窄面内。"""
        from common.public_data.phone_registry import search_registry

        keyword = str(args.get("keyword") or "").strip()
        self.phone_query = keyword
        self.phone_candidates = []
        self.phone_holders = []
        self.phone_not_found = False
        if not keyword:
            return {"error": "keyword required"}
        result = search_registry(self._conn, keyword)
        if not result["phones"] and not result["bindings"]:
            self.phone_not_found = True
            return {"found": False, "keyword": keyword,
                    "hint": "台账无匹配。请对方到 ops-web 号码台账页核实/补登记"}
        # 优先按号码主体判断属主；绑定只用于补充账号，避免把同号下其他
        # 账号的持卡人全部 @ 出来。多个不同属主必须先 clarify，不猜也不 @。
        owner_rows = result["phones"] or result["bindings"]
        owners = {row.get("actual_user") or "" for row in owner_rows}
        ambiguous = len(owner_rows) > 1 and (len(owners) > 1 or owners == {""})
        if ambiguous:
            self.phone_candidates = [
                {
                    "kind": "phone",
                    "record_id": row.get("phone_no") or "",
                    "code": row.get("phone_no") or "",
                    "name": row.get("actual_user") or row.get("phone_no") or "",
                    "phone_no": row.get("phone_no") or "",
                    "actual_user": row.get("actual_user") or "",
                    "phone_type": row.get("phone_type") or "",
                    "physical_device": row.get("physical_device") or "",
                    "status": row.get("status") or "",
                }
                for row in owner_rows
            ]
        else:
            self.phone_holders = sorted(h for h in owners if h)
        # 结果自压缩：一人多卡（实测 5 卡 10+ 绑定）会超 execute() 的
        # 2000 字截断把 JSON 从中间切断——号码只留关键字段、绑定限量，
        # 保证整体在截断线内是合法 JSON。
        phones = [
            {key: row.get(key) for key in (
                "phone_no", "actual_user", "phone_type", "physical_device",
                "status") if row.get(key) is not None}
            for row in result["phones"]
        ]
        bindings = result["bindings"][:6]
        payload = {
            "found": True,
            "phones": phones,
            "bindings": bindings,
            "hint": "actual_user=实际持卡人（验证码找 TA 要）；"
                    "physical_device=卡在哪台手机",
        }
        if len(result["bindings"]) > len(bindings):
            payload["bindings_more"] = len(result["bindings"]) - len(bindings)
        if self.phone_candidates:
            payload["requires_clarification"] = True
            payload["hint"] = "命中多个不同持卡人，必须先让用户按序号确认，不能猜"
        return payload

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
            # 上游 AI 表预置了到月底的空行（未来日期、金额 NULL/0）——
            # 过滤未来日期，否则 DESC 排序与「最近一天」锚点全落在空行上
            # （2026-10-21 实测裸 MAX=10-31 而真实最新=10-20）。
            conditions, params = ["`business_date`<=CURDATE()"], []
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

    def _tool_channel_yoy(self, args):
        """DB 订单行同源同比；渠道归集复用 dim_channel_map。"""
        from common.public_data.channel_map import fetch_channel_map, resolve_channel

        channel = str(args.get("channel") or "").strip()
        shop_filter = str(args.get("shop_name") or "").strip()
        group_by = str(args.get("group_by") or "channel").strip()
        if group_by not in ("channel", "store"):
            return {"error": "group_by must be channel or store"}
        start_text = str(args.get("start_date") or "").strip()
        end_text = str(args.get("end_date") or "").strip()
        if bool(start_text) != bool(end_text):
            return {"error": "start_date 和 end_date 必须同时提供"}
        try:
            if start_text:
                start = datetime.strptime(start_text, "%Y-%m-%d").date()
                end = datetime.strptime(end_text, "%Y-%m-%d").date()
            else:
                end = (self._now or datetime.now)().date()
                start = end.replace(day=1)
            if start > end:
                return {"error": "start_date 不能晚于 end_date"}

            def previous_year(day):
                try:
                    return day.replace(year=day.year - 1)
                except ValueError:  # 2 月 29 日对齐到平年 2 月 28 日
                    return day.replace(year=day.year - 1, day=28)

            previous_start = previous_year(start)
            previous_end = previous_year(end)
        except ValueError:
            return {"error": "date must be YYYY-MM-DD"}

        rules = fetch_channel_map(self._conn)
        if not rules:
            return {"error": "dim_channel_map 无可用映射，无法按业务渠道计算同比"}
        valid_channels = {row.get("channel_key") for row in rules
                          if row.get("channel_key")}
        if channel and channel not in valid_channels:
            return {"error": f"unknown channel: {channel}",
                    "valid_channels": sorted(valid_channels)}

        end_exclusive = end + timedelta(days=1)
        previous_end_exclusive = previous_end + timedelta(days=1)
        # 先在 SQL 内按店铺×抬头聚合两期金额与行数，再用同一套渠道映射
        # 归集。直接过滤 channel_name 会漏掉京东 B2B 大额抬头。
        sql = (
            "SELECT `shop_name`, `buyer_nick`,"
            " SUM(CASE WHEN `trade_time`>=%s AND `trade_time`<%s"
            " THEN `paid_amount` ELSE 0 END) AS `current_sales_amount`,"
            " SUM(CASE WHEN `trade_time`>=%s AND `trade_time`<%s"
            " THEN `paid_amount` ELSE 0 END) AS `previous_sales_amount`,"
            " SUM(CASE WHEN `trade_time`>=%s AND `trade_time`<%s"
            " THEN 1 ELSE 0 END) AS `current_row_count`,"
            " SUM(CASE WHEN `trade_time`>=%s AND `trade_time`<%s"
            " THEN 1 ELSE 0 END) AS `previous_row_count`"
            " FROM `fact_order_line`"
            " WHERE ((`trade_time`>=%s AND `trade_time`<%s)"
            " OR (`trade_time`>=%s AND `trade_time`<%s))"
            " GROUP BY `shop_name`, `buyer_nick`"
        )
        params = [
            start, end_exclusive, previous_start, previous_end_exclusive,
            start, end_exclusive, previous_start, previous_end_exclusive,
            start, end_exclusive, previous_start, previous_end_exclusive,
        ]
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql, params)
            source_rows = cursor.fetchall()
        finally:
            cursor.close()

        buckets = defaultdict(lambda: {
            "current": Decimal("0"), "previous": Decimal("0"),
            "current_rows": 0, "previous_rows": 0,
        })
        known_shops = set()
        for source in source_rows:
            channel_key = resolve_channel(source, rules) or "未映射"
            if channel and channel_key != channel:
                continue
            shop = source.get("shop_name") or "未命名店铺"
            known_shops.add(shop)
            # 点名某店时只算该店（2026-11-02 方案 B1：问句只问一家店，
            # 把全部店铺行喂给 LLM 的模型会顺手列出「其它店铺供参考」）。
            if shop_filter and shop_filter not in shop:
                continue
            identity = (channel_key if group_by == "channel"
                        else (channel_key, source.get("shop_name") or "未命名店铺"))
            bucket = buckets[identity]
            bucket["current"] += Decimal(str(
                source.get("current_sales_amount") or 0))
            bucket["previous"] += Decimal(str(
                source.get("previous_sales_amount") or 0))
            bucket["current_rows"] += int(source.get("current_row_count") or 0)
            bucket["previous_rows"] += int(source.get("previous_row_count") or 0)

        rows = []
        for identity, bucket in buckets.items():
            if group_by == "channel":
                row = {"channel_key": identity}
            else:
                row = {"channel_key": identity[0], "shop_name": identity[1]}
            current = bucket["current"]
            previous = bucket["previous"]
            row.update({
                "current_sales_amount": float(round(current, 2)),
                "previous_sales_amount": float(round(previous, 2)),
                "delta_amount": float(round(current - previous, 2)),
                "yoy_rate": (round(float((current - previous) / previous), 6)
                             if previous else None),
                "current_row_count": bucket["current_rows"],
                "previous_row_count": bucket["previous_rows"],
            })
            rows.append(row)
        rows.sort(key=lambda row: row["current_sales_amount"], reverse=True)
        if shop_filter and not rows:
            return {"error": f"未找到店铺: {shop_filter}",
                    "shops": sorted(known_shops)[:20]}
        # 聚焦压缩（2026-11-02 方案 B1）：喂 LLM 的只留问句点名的行 + 金额
        # TOP 3，其余压成一条汇总。此前最多 50 行全喂，模型顺手列出
        # 「其它店铺供参考」逐店清单（客服明确反馈的不相关内容来源）。
        # 导出/快照仍用全量 rows，总量信息不丢。
        fed_rows, omitted = compress_rows(rows, self._question, self._focus)
        result = {
            "source": "DB:fact_order_line",
            "channel_mapping": "dim_channel_map",
            "metric": "paid_amount（已剔除取消/待付款/全额退款关闭，部分退款不回扣）",
            "group_by": group_by,
            "current_period": {"start": start.isoformat(),
                               "end": end.isoformat()},
            "previous_period": {"start": previous_start.isoformat(),
                                "end": previous_end.isoformat()},
            "has_previous_data": any(
                bucket["previous_rows"] for bucket in buckets.values()),
            "count": len(rows),
            "rows": fed_rows,
        }
        if omitted:
            result["others"] = omitted
            result["others_hint"] = (
                f"其余 {omitted['omitted']} 项合计 {omitted['total_amount']}"
                " 元，未被问到，不要逐行列出")
        budget = _RESULT_LIMIT - 200
        while (result["rows"]
               and len(json.dumps(result, ensure_ascii=False,
                                  default=str)) > budget):
            result["rows"].pop()
        result["returned"] = len(result["rows"])
        result["_export_rows"] = rows
        result["_export_params"] = {
            **args,
            "current_period": {"start": start.isoformat(), "end": end.isoformat()},
            "previous_period": {
                "start": previous_start.isoformat(), "end": previous_end.isoformat(),
            },
        }
        return result

    # ------------------------------------------------------------------
    # SKU 维度（2026-11-02，方案 §5 C1-C3）
    #
    # 粒度补齐：此前「某店 9 月按 SKU 卖了多少」只能按店铺口径作答
    # （客服 2026-10-29 反馈「按照 SKU 维度没开发」）。两条工具都是预定义
    # SQL + 占位符，延续「LLM 只选工具给参数」铁律。
    # ------------------------------------------------------------------

    def _resolve_sku(self, keyword):
        """关键词 → (商品名过滤词, 多规格提示 or None)。

        2026-11-02 云上实测修正：一个商品名对应多个 spec_no 是常态
        （「习酒窖藏1988」在 2026-10 有 5 个规格在售），若命中多规格就
        反问「是哪一个」，工具基本不可用。改为**按关键词 LIKE 检索、结果
        按规格逐行列出并在结果里标注命中多个规格**——既不做规格合并
        （合并出来的数字是错的），也不多绕一轮对话。
        """
        from common.kb_qa.products import find_products

        try:
            products = find_products(self._conn, keyword)
        except Exception:
            products = []
        names = [row.get("name") for row in products if row.get("name")]
        if len(names) == 1:
            return names[0], None        # 唯一命中才用商品全名精确筛
        extra = None
        if len(names) > 1:
            extra = {
                "multiple_specs": True,
                "candidates": [
                    {"code": row.get("code"), "name": row.get("name")}
                    for row in products if row.get("name")
                ],
                "hint": "该名称对应多个规格，结果按规格拆分列出，未合并计算",
            }
        return keyword, extra

    @staticmethod
    def _sku_period(args, now=None):
        """期间解析：缺省本月 1 日至今（与 channel_yoy 同缺省口径）。"""
        from datetime import datetime as _dt

        start_text = str(args.get("start_date") or "").strip()
        end_text = str(args.get("end_date") or "").strip()
        if bool(start_text) != bool(end_text):
            return None, {"error": "start_date 和 end_date 必须同时提供"}
        try:
            if start_text:
                start = _dt.strptime(start_text, "%Y-%m-%d").date()
                end = _dt.strptime(end_text, "%Y-%m-%d").date()
            else:
                end = (now or _dt.now)().date()
                start = end.replace(day=1)
            if start > end:
                return None, {"error": "start_date 不能晚于 end_date"}
        except ValueError:
            return None, {"error": "date must be YYYY-MM-DD"}
        return (start, end), None

    @staticmethod
    def _coerce_top_n(args):
        try:
            value = int(args.get("top_n") or _SKU_TOP_N_DEFAULT)
        except (TypeError, ValueError):
            value = _SKU_TOP_N_DEFAULT
        return max(1, min(value, _SKU_TOP_N_MAX))

    def _tool_sku_sales(self, args):
        """按单品/SKU 的销售：合计 + TOP N（源 fact_order_line）。

        口径与 channel_yoy 同源（paid_amount，已剔除取消/待付款/全额退款
        关闭，部分退款不回扣），强制 trade_time 日期过滤。
        """
        from common.kb_qa.products import _like_escape

        period, error = self._sku_period(args, now=self._now)
        if error:
            return error
        start, end = period
        keyword = str(args.get("keyword") or "").strip()
        channel = str(args.get("channel") or "").strip()
        shop_name = str(args.get("shop_name") or "").strip()
        top_n = self._coerce_top_n(args)

        goods_filter = None
        multi_spec = None
        if keyword:
            goods_filter, multi_spec = self._resolve_sku(keyword)

        conditions = ["`trade_time`>=%s", "`trade_time`<%s"]
        params = [start, end + timedelta(days=1)]
        if goods_filter:
            conditions.append("(`goods_name` LIKE %s ESCAPE '\\\\'"
                              " OR `spec_no`=%s)")
            params.extend([f"%{_like_escape(goods_filter)}%", goods_filter])
        if channel:
            conditions.append("`channel_name`=%s")
            params.append(channel)
        if shop_name:
            conditions.append("`shop_name` LIKE %s ESCAPE '\\\\'")
            params.append(f"%{_like_escape(shop_name)}%")
        where = " AND ".join(conditions)

        cursor = self._conn.cursor()
        try:
            cursor.execute(
                "SELECT COUNT(DISTINCT `trade_no`) AS `order_count`,"
                " COALESCE(SUM(`quantity`),0) AS `quantity`,"
                " COALESCE(SUM(`paid_amount`),0) AS `paid_amount`"
                f" FROM `fact_order_line` WHERE {where}", params)
            total = cursor.fetchone() or {}
            cursor.execute(
                "SELECT `spec_no`, `goods_name`,"
                " COALESCE(SUM(`quantity`),0) AS `quantity`,"
                " COALESCE(SUM(`paid_amount`),0) AS `paid_amount`"
                f" FROM `fact_order_line` WHERE {where}"
                " GROUP BY `spec_no`, `goods_name`"
                " ORDER BY `paid_amount` DESC LIMIT %s",
                (*params, top_n))
            raw_rows = cursor.fetchall()
        except Exception as exc:
            return {"error": f"SKU 销售查询失败: {str(exc)[:160]}"}
        finally:
            cursor.close()

        amount_total = float(total.get("paid_amount") or 0)
        rows = []
        for row in raw_rows:
            amount = float(row.get("paid_amount") or 0)
            rows.append({
                "spec_no": row.get("spec_no"),
                "goods_name": row.get("goods_name"),
                "quantity": float(row.get("quantity") or 0),
                "paid_amount": round(amount, 2),
                "share": (round(amount / amount_total, 6)
                          if amount_total else None),
            })
        result = {
            "source": "DB:fact_order_line",
            "metric": "paid_amount（已剔除取消/待付款/全额退款关闭，部分退款不回扣）",
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "keyword": keyword or None,
            "total": {
                "order_count": int(total.get("order_count") or 0),
                "quantity": float(total.get("quantity") or 0),
                "paid_amount": round(amount_total, 2),
            },
            "count": len(rows),
            "rows": list(rows),
            "snapshot_date": end.isoformat(),
        }
        if multi_spec:
            result.update(multi_spec)
        # 自我压缩到结果预算内（外层硬截会把 JSON 从中间切断）。
        budget = _RESULT_LIMIT - 200
        while (result["rows"]
               and len(json.dumps(result, ensure_ascii=False,
                                  default=str)) > budget):
            result["rows"].pop()
        result["returned"] = len(result["rows"])
        result["_export_rows"] = rows
        result["_export_params"] = {
            **args,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        return result

    def _tool_sku_inventory(self, args):
        """库存快照（fact_inventory_sku_daily 最新 business_date）。

        无关键词时给三态分布总览 + 各态 TOP 5（「哪些呆滞」直接可答）。
        """
        from common.kb_qa.products import _like_escape

        keyword = str(args.get("keyword") or "").strip()
        state = str(args.get("state") or "").strip().upper()
        if state and state not in _STOCK_STATES:
            return {"error": f"unknown state: {state}",
                    "valid_states": list(_STOCK_STATES)}
        top_n = self._coerce_top_n(args)

        cursor = self._conn.cursor()
        try:
            cursor.execute(
                "SELECT MAX(`business_date`) AS `d`"
                " FROM `fact_inventory_sku_daily`")
            snapshot = (cursor.fetchone() or {}).get("d")
            if not snapshot:
                return {"error": "库存快照无数据"}
        except Exception as exc:
            return {"error": f"库存快照查询失败: {str(exc)[:160]}"}
        finally:
            cursor.close()
        snapshot_date = getattr(snapshot, "isoformat", lambda: str(snapshot))()

        goods_filter = None
        multi_spec = None
        if keyword:
            goods_filter, multi_spec = self._resolve_sku(keyword)

        conditions = ["`business_date`=%s"]
        params = [snapshot]
        if goods_filter:
            conditions.append("(`goods_name` LIKE %s ESCAPE '\\\\'"
                              " OR `spec_no`=%s)")
            params.extend([f"%{_like_escape(goods_filter)}%", goods_filter])
        if state:
            conditions.append("`stock_state`=%s")
            params.append(state)
        where = " AND ".join(conditions)

        cursor = self._conn.cursor()
        try:
            cursor.execute(
                "SELECT `spec_no`, `goods_name`,"
                " MAX(`stock_state`) AS `stock_state`,"
                " COALESCE(SUM(`available_qty`),0) AS `available_qty`,"
                " COALESCE(SUM(`purchase_intransit_qty`),0) AS"
                " `purchase_intransit_qty`,"
                " COALESCE(SUM(`qty_7days`),0) AS `qty_7days`,"
                " COALESCE(SUM(`qty_month`),0) AS `qty_month`,"
                " MAX(`daily_avg`) AS `daily_avg`,"
                " MAX(`days_left`) AS `days_left`"
                f" FROM `fact_inventory_sku_daily` WHERE {where}"
                " GROUP BY `spec_no`, `goods_name`"
                " ORDER BY `available_qty` DESC LIMIT %s",
                (*params, top_n))
            raw_rows = cursor.fetchall()
            overview = None
            if not goods_filter:
                cursor.execute(
                    "SELECT `stock_state`, COUNT(*) AS `sku_count`,"
                    " COALESCE(SUM(`available_qty`),0) AS `available_qty`"
                    " FROM `fact_inventory_sku_daily`"
                    " WHERE `business_date`=%s GROUP BY `stock_state`",
                    (snapshot,))
                overview = [
                    {"stock_state": row.get("stock_state"),
                     "sku_count": int(row.get("sku_count") or 0),
                     "available_qty": float(row.get("available_qty") or 0)}
                    for row in cursor.fetchall()
                ]
        except Exception as exc:
            return {"error": f"库存查询失败: {str(exc)[:160]}"}
        finally:
            cursor.close()

        rows = [{
            "spec_no": row.get("spec_no"),
            "goods_name": row.get("goods_name"),
            "stock_state": row.get("stock_state"),
            "available_qty": float(row.get("available_qty") or 0),
            "purchase_intransit_qty": float(
                row.get("purchase_intransit_qty") or 0),
            "qty_7days": float(row.get("qty_7days") or 0),
            "qty_month": float(row.get("qty_month") or 0),
            "daily_avg": float(row.get("daily_avg") or 0),
            "days_left": (float(row.get("days_left"))
                          if row.get("days_left") is not None else None),
        } for row in raw_rows]
        result = {
            "source": "DB:fact_inventory_sku_daily",
            "snapshot_date": snapshot_date,
            "state_note": _STOCK_STATE_NOTE,
            "keyword": keyword or None,
            "overview": overview,
            "count": len(rows),
            "rows": list(rows),
        }
        if multi_spec:
            result.update(multi_spec)
        budget = _RESULT_LIMIT - 200
        while (result["rows"]
               and len(json.dumps(result, ensure_ascii=False,
                                  default=str)) > budget):
            result["rows"].pop()
        result["returned"] = len(result["rows"])
        result["_export_rows"] = rows
        result["_export_params"] = {**args, "snapshot_date": snapshot_date}
        return result

    # ------------------------------------------------------------------
    # 运营报表（2026-11-02，真机 badcase 驱动）
    #
    # 「帮我做一个10月份的运营周报」让 agent 连拼 7 次 query_sql、52 秒
    # 轮尽后兜底「业务数据暂时不可用」（当日 audit 实证）。报表是已知
    # 模式，不该靠模型自由拼 SQL——一次预定义查询拿全素材。
    # ------------------------------------------------------------------

    def _report_period(self, args):
        """期间解析：month（YYYY-MM）优先，其次 start/end，缺省本月至今。

        返回 ``((start, end), error)``。指定月份若就是当月，end 截到今天
        （未到的日子不算进报表，也不拿去环比）。
        """
        from datetime import datetime as _dt

        month_text = str(args.get("month") or "").strip()
        if month_text:
            try:
                first = _dt.strptime(month_text, "%Y-%m").date()
            except ValueError:
                return None, {"error": "month must be YYYY-MM"}
            if first.month == 12:
                next_first = first.replace(year=first.year + 1, month=1)
            else:
                next_first = first.replace(month=first.month + 1)
            today = (self._now or _dt.now)().date()
            end = min(next_first - timedelta(days=1), today)
            if first > end:
                return None, {"error": f"{month_text} 还没有数据（还没开始）"}
            return (first, end), None
        return self._sku_period(args, now=self._now)

    def _tool_sales_report(self, args):
        """运营周报/月报素材：总量 + 环比 + 渠道分布 + TOP 店铺/单品 + 库存。

        环比 = 紧邻的等长上一段（月报 vs 上月全月、MTD vs 上月同段）。
        口径与 channel_yoy/sku_sales 同源（paid_amount，剔除取消/待付款/
        全额退款关闭，部分退款不回扣）。
        """
        period, error = self._report_period(args)
        if error:
            return error
        start, end = period
        top_n = self._coerce_top_n(args)
        prev_end = start - timedelta(days=1)
        month_end = (start.replace(month=start.month + 1, day=1)
                     if start.month < 12
                     else start.replace(year=start.year + 1, month=1, day=1)
                     ) - timedelta(days=1)
        if start.day == 1 and end == month_end:
            # 整月报表的环比对象=上一自然月（不是错位 31 天窗口）。
            prev_start = prev_end.replace(day=1)
        else:
            span = (end - start).days + 1
            prev_start = prev_end - timedelta(days=span - 1)
        end_excl = end + timedelta(days=1)
        prev_end_excl = prev_end + timedelta(days=1)

        cursor = self._conn.cursor()
        try:
            # 1. 两期总量（单 SQL，CASE 分期；COUNT DISTINCT 忽略 NULL）
            cursor.execute(
                "SELECT"
                " COALESCE(SUM(CASE WHEN `trade_time`>=%s AND `trade_time`<%s"
                "  THEN `paid_amount` END),0) AS `cur_amount`,"
                " COALESCE(SUM(CASE WHEN `trade_time`>=%s AND `trade_time`<%s"
                "  THEN `paid_amount` END),0) AS `prev_amount`,"
                " COALESCE(SUM(CASE WHEN `trade_time`>=%s AND `trade_time`<%s"
                "  THEN `quantity` END),0) AS `cur_qty`,"
                " COALESCE(SUM(CASE WHEN `trade_time`>=%s AND `trade_time`<%s"
                "  THEN `quantity` END),0) AS `prev_qty`,"
                " COUNT(DISTINCT CASE WHEN `trade_time`>=%s AND `trade_time`<%s"
                "  THEN `trade_no` END) AS `cur_orders`,"
                " COUNT(DISTINCT CASE WHEN `trade_time`>=%s AND `trade_time`<%s"
                "  THEN `trade_no` END) AS `prev_orders`"
                " FROM `fact_order_line`"
                " WHERE (`trade_time`>=%s AND `trade_time`<%s)"
                " OR (`trade_time`>=%s AND `trade_time`<%s)",
                (start, end_excl, prev_start, prev_end_excl,
                 start, end_excl, prev_start, prev_end_excl,
                 start, end_excl, prev_start, prev_end_excl,
                 start, end_excl, prev_start, prev_end_excl))
            totals_row = cursor.fetchone() or {}
            # 2. 渠道分布（本期）：按抬头归集（与 channel_yoy 同一映射）
            cursor.execute(
                "SELECT `shop_name`, `buyer_nick`,"
                " COALESCE(SUM(`paid_amount`),0) AS `amount`,"
                " COUNT(DISTINCT `trade_no`) AS `orders`"
                " FROM `fact_order_line`"
                " WHERE `trade_time`>=%s AND `trade_time`<%s"
                " GROUP BY `shop_name`, `buyer_nick`",
                (start, end_excl))
            channel_sources = cursor.fetchall()
            # 3. TOP 店铺（本期）
            cursor.execute(
                "SELECT `shop_name`,"
                " COUNT(DISTINCT `trade_no`) AS `orders`,"
                " COALESCE(SUM(`quantity`),0) AS `quantity`,"
                " COALESCE(SUM(`paid_amount`),0) AS `amount`"
                " FROM `fact_order_line`"
                " WHERE `trade_time`>=%s AND `trade_time`<%s"
                " GROUP BY `shop_name` ORDER BY `amount` DESC LIMIT %s",
                (start, end_excl, top_n))
            store_rows = cursor.fetchall()
            # 4. TOP 单品（本期）
            cursor.execute(
                "SELECT `spec_no`, `goods_name`,"
                " COALESCE(SUM(`quantity`),0) AS `quantity`,"
                " COALESCE(SUM(`paid_amount`),0) AS `amount`"
                " FROM `fact_order_line`"
                " WHERE `trade_time`>=%s AND `trade_time`<%s"
                " GROUP BY `spec_no`, `goods_name`"
                " ORDER BY `amount` DESC LIMIT %s",
                (start, end_excl, top_n))
            sku_rows = cursor.fetchall()
        except Exception as exc:
            return {"error": f"运营报表查询失败: {str(exc)[:160]}"}
        finally:
            cursor.close()

        # 库存三态是辅信息，单独 fail-soft（缺表/无快照不拖垮整份报表）。
        inventory = None
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                "SELECT `stock_state`, COUNT(*) AS `sku_count`,"
                " COALESCE(SUM(`available_qty`),0) AS `available_qty`"
                " FROM `fact_inventory_sku_daily`"
                " WHERE `business_date`="
                "  (SELECT MAX(`business_date`) FROM `fact_inventory_sku_daily`)"
                " GROUP BY `stock_state`")
            inventory = [
                {"stock_state": row.get("stock_state"),
                 "sku_count": int(row.get("sku_count") or 0),
                 "available_qty": float(row.get("available_qty") or 0)}
                for row in cursor.fetchall()
            ]
        except Exception:
            inventory = None
        finally:
            cursor.close()

        from common.public_data.channel_map import (
            fetch_channel_map, resolve_channel)

        cur_amount = float(totals_row.get("cur_amount") or 0)
        prev_amount = float(totals_row.get("prev_amount") or 0)

        channels = defaultdict(lambda: {"amount": 0.0, "orders": 0})
        rules = fetch_channel_map(self._conn)
        for source in channel_sources:
            key = resolve_channel(source, rules) if rules else None
            bucket = channels[key or "未映射"]
            bucket["amount"] += float(source.get("amount") or 0)
            bucket["orders"] += int(source.get("orders") or 0)
        channel_rows = [
            {"channel_key": key, "order_count": bucket["orders"],
             "paid_amount": round(bucket["amount"], 2),
             "share": (round(bucket["amount"] / cur_amount, 6)
                       if cur_amount else None)}
            for key, bucket in sorted(
                channels.items(), key=lambda kv: -kv[1]["amount"])
        ]

        result = {
            "source": "DB:fact_order_line",
            "metric": "paid_amount（已剔除取消/待付款/全额退款关闭，部分退款不回扣）",
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "compare_period": {"start": prev_start.isoformat(),
                               "end": prev_end.isoformat(),
                               "note": "环比：整月报表=上一自然月，否则=紧邻等长上一段"},
            "totals": {
                "order_count": int(totals_row.get("cur_orders") or 0),
                "quantity": float(totals_row.get("cur_qty") or 0),
                "paid_amount": round(cur_amount, 2),
            },
            "compare_totals": {
                "order_count": int(totals_row.get("prev_orders") or 0),
                "quantity": float(totals_row.get("prev_qty") or 0),
                "paid_amount": round(prev_amount, 2),
            },
            "mom_rate": (round((cur_amount - prev_amount) / prev_amount, 6)
                         if prev_amount else None),
            "channels": channel_rows,
            "top_stores": [
                {"shop_name": row.get("shop_name"),
                 "order_count": int(row.get("orders") or 0),
                 "quantity": float(row.get("quantity") or 0),
                 "paid_amount": round(float(row.get("amount") or 0), 2)}
                for row in store_rows
            ],
            "top_skus": [
                {"spec_no": row.get("spec_no"),
                 "goods_name": row.get("goods_name"),
                 "quantity": float(row.get("quantity") or 0),
                 "paid_amount": round(float(row.get("amount") or 0), 2),
                 "share": (round(float(row.get("amount") or 0) / cur_amount, 6)
                           if cur_amount else None)}
                for row in sku_rows
            ],
            "inventory_states": inventory,
        }
        # 自我压缩到结果预算内（两榜轮流行尾截断，总量与渠道不丢）。
        budget = _RESULT_LIMIT - 200
        while (len(json.dumps(result, ensure_ascii=False, default=str))
               > budget):
            if len(result["top_skus"]) >= len(result["top_stores"]) > 1:
                result["top_skus"].pop()
            elif len(result["top_stores"]) > 1:
                result["top_stores"].pop()
            else:
                break
        result["snapshot_date"] = end.isoformat()
        return result

    def _tool_query_sql(self, args):
        try:
            sql = validate_sql(args.get("sql"))
        except SqlRejected as exc:
            return {"error": f"SQL 未通过安全检查: {exc}"}
        if self._cache_store is None:
            return self._query_sql_uncached(sql)
        # R5（D4a）：同 SQL 60s 读穿缓存，命中时 EXPLAIN+SELECT 全省；
        # 错误/空行结果不缓存（错误自修正与空库重试语义保留）。
        key = "kbqa:qcache:" + hashlib.sha256(
            sql.encode("utf-8")).hexdigest()[:32]
        return cache_core.get_or_compute(
            key, 60, lambda: self._query_sql_uncached(sql),
            store=self._cache_store,
            should_cache=lambda value: isinstance(value, dict)
            and not value.get("error") and bool(value.get("rows")))

    def _query_sql_uncached(self, sql):
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
                                 f"{_SQL_EXPLAIN_ROW_LIMIT}——请检查日期过滤写法："
                                 "用 时间列>='…' AND 时间列<'…' 范围谓词，禁止"
                                 " DATE(时间列) 包裹（函数包裹让索引失效变全表"
                                 "扫描）；或缩小日期范围后重试"}
            cursor.execute(f"SET SESSION MAX_EXECUTION_TIME={_SQL_TIMEOUT_MS}")
            cursor.execute(sql)
            rows = cursor.fetchall()
        except Exception as exc:
            # 执行/语法错误回喂给 LLM 自修正（如保留字 year_month 需反引号
            # ——2026-10-21 实测 1064；吞掉时 LLM 只会看到「一直报错」）。
            return {"error": f"SQL 执行失败: {str(exc)[:220]}——请修正后重试"}
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
        result["_export_rows"] = rows
        result["_export_params"] = {"sql": sql}
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


# import 时校验：每个启用的技能都有对应 handler（cards 注册表同款模式）。
validate_handlers(ToolExecutor)


def _sync_phone_meta(executor, meta):
    """工具执行后立即透出号码状态，覆盖群与 Web 两条 agent 调用链。"""
    if meta is None or not hasattr(executor, "phone_candidates"):
        return
    candidates = executor.phone_candidates or []
    holders = executor.phone_holders or []
    if candidates:
        meta["phone_candidates"] = list(candidates)
    else:
        # 同一 agent loop 可能连续查多个号码，旧候选不能污染后一次查询。
        meta.pop("phone_candidates", None)
    if holders and not candidates:
        meta["phone_holders"] = list(holders)
    else:
        meta.pop("phone_holders", None)
    query = executor.phone_query
    if query:
        meta["phone_query"] = query
    else:
        meta.pop("phone_query", None)
    if executor.phone_not_found:
        meta["phone_not_found"] = True
    else:
        meta.pop("phone_not_found", None)
    if hasattr(executor, "query_snapshots"):
        meta["export_count"] = len(executor.query_snapshots)
        if executor.query_snapshots:
            meta["query_snapshots"] = list(executor.query_snapshots)
        else:
            meta.pop("query_snapshots", None)
    for key, value in getattr(executor, "index_telemetry", {}).items():
        if value is not None:
            meta[key] = value
    if getattr(executor, "export_error_summary", None):
        meta["error_summary"] = executor.export_error_summary


#: 显示层接管的工具（2026-11-02）：业务查询结果含 rows，表格与合计改由
#: render_table 统一渲染——LLM 手写表格出过列粘连（10/016）与合计裸数字
#: 连读（155 14,907.39）两起用户误读事故。
_DISPLAY_TOOLS = {"query_sql", "channel_sales"}


def _display_block(name, result_json, question=""):
    """工具结果 JSON → ResultSet → 答案块（结论卡+表格）。

    非显示工具/异常/无行/探索锚点返回 ""；期间以理解层为准过滤行。
    """
    if name not in _DISPLAY_TOOLS:
        return ""
    try:
        payload = json.loads(result_json)
    except (TypeError, ValueError):
        return ""
    result = from_payload(name, payload)
    if result is None:
        return ""
    period = parse_focus(question).period if question else None
    return render_answer(result.rows, total_rows=result.total_rows,
                         period=period)


#: 表格引用占位符（[[表N]]）：多查询场景由 LLM 选择与答案相关的表，
#: 中间探索性查询的表不发出（2026-11-02 云上复验：渠道探索表/max_time
#: 表被全量附上，违反「只答被问到的」答复纪律）。
_PLACEHOLDER_RE = re.compile(r"\[\[表(\d+)\]\]")


def _append_display(content, blocks):
    """最终回复 = LLM 结论 + 被引用的显示层数据块。

    - 正文含 [[表N]] 占位符 → 替换为第 N 张表（1 起），未引用的一律不发；
    - 无占位符：单表自动附末尾（最常见场景），多表不附（防中间探索表
      噪音）；无数据块时行为与原先一致。
    """
    text = (content or "").strip()
    blocks = [block for block in blocks if block]
    if not blocks:
        return text or None
    used = set()

    def _sub(match):
        index = int(match.group(1)) - 1
        if 0 <= index < len(blocks):
            used.add(index)
            return "\n" + blocks[index] + "\n"
        return ""

    if text:
        replaced = _PLACEHOLDER_RE.sub(_sub, text).strip()
        if used:
            return replaced
        text = replaced  # 清掉无效占位符，不发给用户
    if len(blocks) == 1:
        return f"{text}\n\n{blocks[0]}" if text else blocks[0]
    return text or None


def run_agent(llm, question, *, executor, history=None,
              max_rounds=MAX_TOOL_ROUNDS, chat_with_tools=None, meta=None,
              tools=None):
    """agent loop：问题 → 工具调用（≤max_rounds 轮）→ 答案。

    返回答案文本；LLM 异常/畸形/超限无答案 → None（调用方降级）。
    chat_with_tools 注入点（测试替身），缺省调 llm.chat_with_tools。
    meta（反馈闭环埋点）：传入 dict 时回填 ``tools``（工具名序列）与
    ``rounds``（LLM 调用轮数），供调用方落审计；不传零副作用。
    tools（2026-10-23 Web 通道角色白名单）：可offering的工具子集，
    None=全部 TOOLS。LLM 只能调用被 offering 的工具，越权工具根本
    不出现在协议面（比事后拦截干净）。
    """
    call = chat_with_tools or llm.chat_with_tools
    tools = TOOLS if tools is None else tools
    # 同比问句的路由不能靠模型自由发挥：只 offering channel_yoy + sku_sales，
    # 从协议面杜绝又走 AI 表格 channel_sales 或 query_sql 混口径。
    # 同比的对象可能是渠道/店铺（→ channel_yoy），也可能是商品/系列
    # （「金质今年9月销售额同比去年是多少」→ sku_sales，2026-11-02 真机
    # badcase：只给 channel_yoy 时模型只能把商品名当店铺找，反问收场）。
    if _CHANNEL_YOY_RE.search(question or ""):
        yoy_tools = [t for t in tools
                     if t.get("function", {}).get("name")
                     in ("channel_yoy", "sku_sales")]
        if yoy_tools:
            tools = yoy_tools
    display_blocks = []
    if meta is not None:
        meta["tools"] = []
        meta["rounds"] = 0
        meta["export_count"] = 0
        meta.pop("query_snapshots", None)
        if hasattr(executor, "query_snapshots"):
            executor.query_snapshots = []
    history_text = format_history(history) if history else ""
    # 工具子集运行时（Web 角色白名单）同步收窄 prompt：没 offering
    # query_sql 就不列出可用表，避免模型凭空写 SQL。
    offered = {t.get("function", {}).get("name") for t in tools}
    tables_text = (
        "\n".join(f"- {name}：{desc}" for name, desc in _SQL_TABLES.items())
        if "query_sql" in offered else "（本通道未开放）"
    )
    messages = [
        {"role": "system",
         "content": (_SYSTEM_PROMPT
                     .replace("{{sql_tables}}", tables_text)
                     .replace("{{history}}", history_text))},
        {"role": "user", "content": question},
    ]
    for round_index in range(max_rounds):
        try:
            # 工具决策与最终作答共用一次调用；不传 max_tokens，避免长答案
            # 被应用层 2048 上限截断（平台/模型自身上限仍客观存在）。
            response = call(messages, tools=tools, max_tokens=None)
        except LlmError:
            return None
        except Exception:
            return None
        if meta is not None:
            meta["rounds"] = round_index + 1
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
            return _append_display(content, display_blocks)
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
            if meta is not None and name:
                meta["tools"].append(name)
            try:
                result = executor.execute(name, arguments)
            except Exception:
                result = json.dumps({"error": "tool execution failed"},
                                    ensure_ascii=False)
            _sync_phone_meta(executor, meta)
            block = _display_block(name, result, question)
            if block:
                display_blocks.append(block)
            messages.append({
                "role": "tool",
                "tool_call_id": call_info.get("id") or "",
                "name": name or "",
                "content": result,
            })
    # 轮数用尽：让 LLM 基于已有工具结果直接作答（不再给工具）。
    # 必须加明确禁令——该推理版模型对复杂问题会在 reasoning 里继续设计
    # SQL 直到吃光 max_tokens（实测 reasoning_tokens=1024 全耗、content
    # 零产出、finish_reason=length）；指令「禁止再生成 SQL」让推理短路。
    final_instruction = (
        "以上工具结果已足够。请直接基于这些结果用中文给出最终"
        "答案，不要再生成 SQL 或调用任何工具。数据表格与合计由"
        "系统统一渲染，不要手写 Markdown 表格。")
    if display_blocks:
        labels = "、".join(f"表{i + 1}" for i in range(len(display_blocks)))
        final_instruction += (
            f"系统已把各次查询结果渲染成数据表（编号 {labels}，与工具调用"
            "同序）。与答案相关的表，在正文相应位置写 [[表N]] 引用，系统会"
            "替换为表格；中间探索过程等与答案无关的表不要引用，未被引用的"
            "表不会发给用户。")
    messages.append({
        "role": "user",
        "content": final_instruction,
    })
    if meta is not None:
        meta["rounds"] += 1  # 轮尽作答也是一次 LLM 调用
    try:
        final = llm.chat(messages, max_tokens=None, temperature=0.3)
    except LlmError:
        return None
    if has_dsml_residue(final):
        return None
    return _append_display(final, display_blocks)
