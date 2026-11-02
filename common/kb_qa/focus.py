# -*- coding: utf-8 -*-
"""问句理解层：期间 / 同比 / 粒度解析 + 数据边界直答（2026-11-02）。

方案：docs/小易答复相关性过滤与SKU维度查询方案-2026-10-29.md §3（A1/A2）
与 §4 B1（工具结果聚焦压缩）。

设计原则：
- **纯规则、零 LLM、零网络**：解析不出来就留空，各消费方退化现状行为
  （fail-open），绝不因为解析失败改变原有答复；
- 只解析「字面上确定性可读」的结构（期间、同比基期、粒度、点名渠道），
  不做实体消歧——店铺名 / SKU 名的真源是库表，不靠猜；
- 数据边界（A2）：同比基期早于数据起点时**不跑工具直接一句话说明**，
  从根上消灭「同比：0 条」这类把缺数渲染成 0 的答复（2026-10-29 截图
  badcase 二）。

消费方：
- ``parse_focus`` → L2 聚焦压缩、L4 实体匹配过滤、工具默认期间；
- ``boundary_reply`` → 问答主链路在调 agent 前的短路直答；
- ``compress_rows`` → 只把与问句相关的行喂给 LLM（导出仍走全量）。
"""

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

#: 粒度取值。
GRANULARITY_SKU = "sku"
GRANULARITY_STORE = "store"
GRANULARITY_CHANNEL = "channel"

_SKU_WORDS = (
    "sku", "单品", "每个品", "各款", "哪款", "哪个品", "哪几个品",
    "按品", "各单品", "规格编码", "哪个商品", "哪些品",
)
_STORE_WORDS = ("各店", "每家店", "各门店", "按店", "分店", "店铺", "门店")
_CHANNEL_WORDS = ("各渠道", "按渠道", "分渠道", "渠道")

#: 渠道名（只用于「问句点名了谁」的判定，不用于渠道归集——归集真源
#: 是 dim_channel_map）。
_CHANNEL_NAMES = (
    "京东", "天猫", "猫超", "拼多多", "抖音", "直播", "快手", "视频号",
    "即时零售", "私域", "丰享", "得物", "微盟", "其它",
)

_TODAY_WORDS = ("今天", "今日", "当天")
_YESTERDAY_WORDS = ("昨天", "昨日")
_THIS_WEEK_WORDS = ("本周", "这周", "这个周")
_LAST_WEEK_WORDS = ("上周", "上个周")
_THIS_MONTH_WORDS = ("本月", "这个月", "当月")
_LAST_MONTH_WORDS = ("上月", "上个月")
_THIS_YEAR_WORDS = ("今年",)
_YOY_WORDS = ("同比", "去年同期")

_CN_MONTHS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
}

_RANGE_RE = re.compile(
    r"(\d{4})-(\d{1,2})-(\d{1,2})\s*(?:到|至|~|—|-)\s*"
    r"(\d{4})-(\d{1,2})-(\d{1,2})")
_YEAR_MONTH_RE = re.compile(r"(\d{4})\s*[年\-/]\s*(\d{1,2})\s*月?")
_CN_YEAR_MONTH_RE = re.compile(
    r"(\d{4})\s*年\s*([一二三四五六七八九十]+|\d{1,2})\s*月")
_MONTH_RE = re.compile(r"(?<!\d)(\d{1,2}|[一二三四五六七八九十]+)\s*月")
_RECENT_DAYS_RE = re.compile(r"(?:近|最近|过去)\s*(\d{1,3})\s*[天日]")

#: 数据起点探针表（表 → 日期列）。值为空则不做边界判定（fail-open）。
DATA_START_TABLES = {"fact_order_line": "trade_time"}

#: 边界直答开关（方案 §8 boundary.enabled；默认开，置 0 可单层回滚）。
BOUNDARY_ENV = "KB_BOUNDARY_ENABLE"


def boundary_enabled(environ=None):
    """边界直答是否启用；未配置时启用。"""
    raw = ((environ or os.environ).get(BOUNDARY_ENV) or "").strip().lower()
    return raw not in ("0", "false", "off", "no")


@dataclass(frozen=True)
class Period:
    """一段闭区间业务期间。"""

    start: date
    end: date
    label: str = ""

    @property
    def iso(self):
        return f"{self.start.isoformat()}~{self.end.isoformat()}"

    def shifted(self, years=1):
        """同比基期（2 月 29 日对齐平年 2 月 28 日）。"""
        return Period(_shift_year(self.start, years),
                      _shift_year(self.end, years),
                      f"{self.label}同期" if self.label else "")


@dataclass(frozen=True)
class Focus:
    """问句焦点：期间 / 同比 / 粒度 / 点名渠道。"""

    period: Period = None
    yoy: bool = False
    yoy_period: Period = None
    granularity: str = None
    channels: tuple = field(default_factory=tuple)


def _shift_year(day, years=1):
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 2 月 29 日 → 平年 2 月 28 日
        return day.replace(year=day.year - years, day=28)


def _month_start(year, month):
    return date(year, month, 1)


def _month_end(year, month):
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _to_month(token):
    if token in _CN_MONTHS:
        return _CN_MONTHS[token]
    try:
        value = int(token)
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 12 else None


def parse_period(text, now=None):
    """从问句解析业务期间；读不出来返回 None（调用方按缺省期间处理）。"""
    if not text:
        return None
    today = now or date.today()
    match = _RANGE_RE.search(text)
    if match:
        try:
            start = date(int(match.group(1)), int(match.group(2)),
                         int(match.group(3)))
            end = date(int(match.group(4)), int(match.group(5)),
                       int(match.group(6)))
        except ValueError:
            start = end = None
        if start and end and start <= end:
            return Period(start, end, f"{start.isoformat()}~{end.isoformat()}")

    match = _CN_YEAR_MONTH_RE.search(text) or _YEAR_MONTH_RE.search(text)
    if match:
        month = _to_month(match.group(2))
        if month:
            year = int(match.group(1))
            return Period(_month_start(year, month), _month_end(year, month),
                          f"{year}-{month:02d}")

    if any(word in text for word in _TODAY_WORDS):
        return Period(today, today, "今天")
    if any(word in text for word in _YESTERDAY_WORDS):
        day = today - timedelta(days=1)
        return Period(day, day, "昨天")
    if any(word in text for word in _THIS_MONTH_WORDS):
        return Period(_month_start(today.year, today.month), today, "本月")
    if any(word in text for word in _LAST_MONTH_WORDS):
        first = _month_start(today.year, today.month) - timedelta(days=1)
        return Period(_month_start(first.year, first.month), first, "上月")
    if any(word in text for word in _THIS_WEEK_WORDS):
        monday = today - timedelta(days=today.weekday())
        return Period(monday, today, "本周")
    if any(word in text for word in _LAST_WEEK_WORDS):
        monday = today - timedelta(days=today.weekday())
        return Period(monday - timedelta(days=7), monday - timedelta(days=1),
                      "上周")
    if any(word in text for word in _THIS_YEAR_WORDS):
        return Period(date(today.year, 1, 1), today, "今年")

    match = _RECENT_DAYS_RE.search(text)
    if match:
        days = int(match.group(1))
        if 0 < days <= 366:
            return Period(today - timedelta(days=days - 1), today,
                          f"近{days}天")

    match = _MONTH_RE.search(text)
    if match:
        month = _to_month(match.group(1))
        if month:
            year = today.year
            start = _month_start(year, month)
            if start > today:  # 问的是去年的这个月
                year -= 1
                start = _month_start(year, month)
            end = _month_end(year, month)
            return Period(start, min(end, today) if end > today else end,
                          f"{year}-{month:02d}")
    return None


def parse_focus(question, now=None):
    """问句 → Focus；任何一步读不出来都是 None/False，绝不抛异常。"""
    text = question or ""
    period = parse_period(text, now)
    yoy = any(word in text for word in _YOY_WORDS)
    granularity = None
    lowered = text.lower()
    if any(word.lower() in lowered for word in _SKU_WORDS):
        granularity = GRANULARITY_SKU
    elif any(word in text for word in _STORE_WORDS):
        granularity = GRANULARITY_STORE
    elif any(word in text for word in _CHANNEL_WORDS):
        granularity = GRANULARITY_CHANNEL
    channels = tuple(name for name in _CHANNEL_NAMES
                     if name in text and name != "其它")
    return Focus(
        period=period,
        yoy=yoy,
        yoy_period=period.shifted() if (period and yoy) else None,
        granularity=granularity,
        channels=channels,
    )


# ---------------------------------------------------------------------------
# A2：数据边界直答
# ---------------------------------------------------------------------------

def fetch_data_start(conn, table="fact_order_line", column=None):
    """表的数据起点（最早业务日期）；查不到 / 异常返回 None（fail-open）。"""
    column = column or DATA_START_TABLES.get(table)
    if not column:
        return None
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT MIN(`{column}`) AS `min_value` FROM `{table}`")
        row = cursor.fetchone() or {}
    except Exception:
        return None
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
    value = row.get("min_value")
    if value is None:
        return None
    # datetime 是 date 的子类，必须先判（否则边界比较拿到的不是纯日期）。
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return getattr(value, "date", lambda: None)()


def boundary_reply(focus, data_start):
    """同比基期早于数据起点 → 一句话直答；否则 None（正常走工具）。"""
    if focus is None or not focus.yoy or data_start is None:
        return None
    base = focus.yoy_period or (focus.period.shifted() if focus.period else None)
    if base is None or base.end >= data_start:
        return None
    label = base.label or f"{base.start.isoformat()}~{base.end.isoformat()}"
    return (f"同比基期 {label} 早于订单数据起点 {data_start.isoformat()}，"
            "没有去年数据可对比。去掉「同比」我可以查本期数据。")


def resolve_boundary_reply(conn, question, focus=None, now=None):
    """主链路入口：解析 → 需要时探针数据起点 → 直答文本或 None。

    只在问句带同比/去年时才打探针（一次 MIN，走索引），其余问句零额外
    查询。探针失败返回 None（按现状跑工具）。
    """
    focus = focus if focus is not None else parse_focus(question, now=now)
    if focus is None or not focus.yoy:
        return None
    return boundary_reply(focus, fetch_data_start(conn))


# ---------------------------------------------------------------------------
# B1：工具结果聚焦压缩
# ---------------------------------------------------------------------------

#: 行内可能承载「问句点名实体」的字段。
_ENTITY_KEYS = ("channel_key", "channel", "shop_name", "store_name", "scope")
#: 排序用金额字段（取第一个非空者）。
_AMOUNT_KEYS = ("current_sales_amount", "sales_amount", "paid_amount", "amount")


def _row_amount(row):
    for key in _AMOUNT_KEYS:
        value = row.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _row_matches(row, question, channels):
    for key in _ENTITY_KEYS:
        value = row.get(key)
        if value and str(value) in question:
            return True
    for channel in channels or ():
        if any(str(row.get(key) or "") == channel for key in _ENTITY_KEYS):
            return True
    return False


#: 未点名对象时仍原样全喂的行数上限（「各渠道对比」这类问句要看到全部
#: 渠道，渠道数约 9 个；店铺级动辄 50 行才是「其它店铺供参考」的土壤）。
KEEP_ALL_LIMIT = 12


def _entity_length(row):
    """行内实体名的最大长度（用于「点名店15」不被「店1」抢先）。"""
    return max((len(str(row.get(key))) for key in _ENTITY_KEYS
                if row.get(key)), default=0)


def compress_rows(rows, question="", focus=None, *, top_n=3,
                  keep_all_limit=KEEP_ALL_LIMIT):
    """问句点名的行 + 金额 TOP N，其余压成一条汇总。

    返回 ``(kept_rows, summary_or_None)``：
    - 问句点名了具体对象（店铺/渠道名出现在问句里）→ 保留命中行 + 未命中
      里的金额 TOP ``top_n``；
    - 没点名对象且行数不超过 ``keep_all_limit`` → **原样全喂**（各渠道对比
      这类问句要看全量，压掉就是丢数据，2026-11-02 评审修正）；
    - 没点名对象且行数超上限 → TOP ``top_n`` + 汇总；
    - 汇总为 ``{"omitted": n, "total_amount": x}``，总量信息不丢，只是
      不逐行列出；
    - **导出/快照仍用调用方手里的全量 rows**（本函数不修改入参）。
    """
    if not rows or len(rows) <= top_n:
        return list(rows), None
    channels = getattr(focus, "channels", ()) or ()
    matched = [row for row in rows if _row_matches(row, question, channels)]
    if not matched and len(rows) <= keep_all_limit:
        return list(rows), None
    # 长实体优先：「店15」也是「店1」的子串命中，按长度降序让更具体的在前。
    matched.sort(key=_entity_length, reverse=True)
    matched_ids = {id(row) for row in matched}
    ranked = sorted((row for row in rows if id(row) not in matched_ids),
                    key=_row_amount, reverse=True)
    kept = list(matched) + ranked[:top_n]
    kept_ids = {id(row) for row in kept}
    omitted = [row for row in rows if id(row) not in kept_ids]
    if not omitted:
        return kept, None
    summary = {
        "omitted": len(omitted),
        "total_amount": round(sum(_row_amount(row) for row in omitted), 2),
    }
    return kept, summary
