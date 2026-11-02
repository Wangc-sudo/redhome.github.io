# -*- coding: utf-8 -*-
"""显示层（2026-11-02 用户截图驱动）：工具结构化结果 → 标准 Markdown 数据块。

动机：表格此前由 LLM 手写，出现两类事故——
- 群通道表格列分隔丢失（「10/016」= 日期 10/01 + 订单行数 6 粘连）；
- 合计行不同量纲裸数字并排（「155 14,907.39」被用户连读成 15514907）。
改为代码确定性渲染：列分隔、千分位、单位、合计全部由本模块负责，
LLM 只写结论（agent.run_agent 负责接线）。合计由代码计算，顺带消除
LLM 口算错误面。

保守口径：
- 只对可加指标列（数量/金额/计数）出合计；比率/单价类不合计；
- 结果集被截断（returned < total_rows）时不出合计——部分行求和是误导；
- 已含「合计/总计」行的结果不重复追加；
- 单行结果不出合计（无信息量）。
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

#: 列名 → 中文标签（未识别列保留原名）。
_COLUMN_LABELS = {
    "d": "日期", "date": "日期", "business_date": "日期", "stat_date": "日期",
    "shop_name": "店铺", "store_name": "店铺", "goods_name": "商品",
    "channel": "渠道", "channel_name": "渠道", "channel_label": "渠道",
    "spec_no": "规格编码", "brand_name": "品牌", "scope": "区域",
    "person_name": "姓名", "warehouse_name": "仓库", "trade_no": "交易单号",
    "qty": "销量", "quantity": "销量", "qty_sold": "销量",
    "amt": "实付金额", "paid_amount": "实付金额", "sales_amount": "销售额",
    "refund_amount": "退款金额", "promotion_cost": "推广费",
    "orders": "订单数", "order_count": "订单数", "line_cnt": "订单行数",
    "count": "数量", "c": "数量",
}
_DATE_COLUMNS = {"d", "date", "business_date", "stat_date"}
_AMOUNT_COLUMNS = {
    "amt", "paid_amount", "sales_amount", "refund_amount", "promotion_cost",
}
_QUANTITY_COLUMNS = {"qty", "quantity", "qty_sold"}
_COUNT_COLUMNS = {"orders", "order_count", "line_cnt", "count", "c"}
#: 可加总列 = 数量/金额/计数；比率、单价、库存快照等不在内（合计无意义）。
_ADDITIVE_COLUMNS = _AMOUNT_COLUMNS | _QUANTITY_COLUMNS | _COUNT_COLUMNS

#: 表格最多渲染行数（超出给截断说明；query_sql 结果本身 ≤50 行）。
MAX_ROWS = 25

_TOTAL_WORDS = ("合计", "总计")


def _is_number(value):
    return isinstance(value, (int, float, Decimal)) and not isinstance(
        value, bool)


def _fmt_number(value, digits):
    return f"{float(value):,.{digits}f}"


def _as_number(column, value):
    """数值列归一：query_sql 给 LLM 的值已 str() 化（"804.0000"），
    显示层须把数值字符串转回数字，否则格式化与合计全部失效
    （2026-11-02 云上验收实测）。无法解析返回 None。"""
    if _is_number(value):
        return value
    if isinstance(value, str) and column in _ADDITIVE_COLUMNS:
        try:
            return Decimal(value.strip().replace(",", ""))
        except (InvalidOperation, ValueError):
            return None
    return None


def _fmt_value(column, value):
    if value is None:
        return "—"
    if isinstance(value, datetime):
        if value.hour or value.minute or value.second:
            return value.strftime("%Y-%m-%d %H:%M")
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    number = _as_number(column, value) if column in _ADDITIVE_COLUMNS else None
    if number is not None:
        if column in _AMOUNT_COLUMNS:
            return _fmt_number(number, 2)
        if float(number) == int(number):
            return _fmt_number(number, 0)
        return _fmt_number(number, 2)
    if _is_number(value):
        return _fmt_number(value, 2) if isinstance(
            value, (float, Decimal)) else f"{value:,}"
    text = str(value)
    # 日期字符串带 00:00:00 时间尾时只留日期（SQL DATE() 输出的常见形态）。
    if column in _DATE_COLUMNS and len(text) >= 10:
        return text[:10]
    return text


def _header(column):
    label = _COLUMN_LABELS.get(column, column)
    if column in _AMOUNT_COLUMNS:
        return f"{label}(元)"
    if column in _QUANTITY_COLUMNS:
        return f"{label}(件)"
    if column in _COUNT_COLUMNS:
        return f"{label}(个)" if label == "数量" else label
    return label


def _total_line(columns, rows):
    """合计行（一行文字）：仅可加列、完整结果集、多行、无既有合计行。"""
    if len(rows) < 2:
        return ""
    first_key = columns[0] if columns else ""
    for row in rows:
        first = str(row.get(first_key) or "")
        if any(word in first for word in _TOTAL_WORDS):
            return ""
    parts = []
    for column in columns:
        if column not in _ADDITIVE_COLUMNS:
            continue
        numbers = [n for n in (_as_number(column, row.get(column))
                               for row in rows) if n is not None]
        if not numbers:
            continue
        total = sum(float(n) for n in numbers)
        label = _COLUMN_LABELS.get(column, column)
        if column in _AMOUNT_COLUMNS:
            parts.append(f"{label} {_fmt_number(total, 2)} 元")
        elif column in _QUANTITY_COLUMNS:
            parts.append(f"{label} {_fmt_number(total, 0)} 件")
        else:
            parts.append(f"{label} {_fmt_number(total, 0)}")
    return "合计：" + "、".join(parts) if parts else ""


def render_table(rows, *, total_rows=None, max_rows=MAX_ROWS,
                 with_total=True):
    """行列表（dict）→ Markdown 数据块；空行/非 dict 行返回空串。

    total_rows：结果集总行数（截断时大于 len(rows)，此时不出合计）。
    with_total：render_answer 场景合计由结论卡承载时置 False（不重复）。
    """
    if not rows:
        return ""
    columns = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    if not columns:
        return ""
    lines = [
        "| " + " | ".join(_header(c) for c in columns) + " |",
        "|" + "|".join(" --- " for _ in columns) + "|",
    ]
    for row in rows[:max_rows]:
        lines.append(
            "| " + " | ".join(_fmt_value(c, row.get(c)) for c in columns)
            + " |")
    block = "\n".join(lines)
    notes = []
    if len(rows) > max_rows:
        notes.append(f"（仅显示前 {max_rows} 行，共 {len(rows)} 行）")
    complete = total_rows is None or total_rows <= len(rows)
    if with_total and complete and len(rows) <= max_rows:
        total = _total_line(columns, rows)
        if total:
            notes.append(total)
    if notes:
        block += "\n" + "\n".join(notes)
    return block


# ---------------------------------------------------------------------------
# 结论卡 + 期间过滤（2026-11-02 答复架构：数字只能来自同一份行数据）
# ---------------------------------------------------------------------------

def _row_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def filter_period(rows, period):
    """按问句期间过滤行（理解层 focus.Period）。

    有日期列才过滤；无日期列原样返回（粒度不明不过滤，fail-open）。
    解决「问昨天、表格合计却跨 4 天」的期间错位（2026-11-02 云上实测）。
    """
    if not period or not rows:
        return list(rows or [])
    columns = {key for row in rows for key in row}
    date_col = next((c for c in columns if c in _DATE_COLUMNS), None)
    if date_col is None:
        return list(rows)
    return [row for row in rows
            if (day := _row_date(row.get(date_col))) is not None
            and period.start <= day <= period.end]


def summary_card(rows, *, complete=True):
    """结论卡：区间 + 合计 + 日均 + 极值，全部代码计算、与表格同源。

    不完整结果集 / 单行不出（与合计同口径）；无任何可述内容返回空串。
    """
    if not complete or not rows or len(rows) < 2:
        return ""
    columns = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    date_col = next((c for c in columns if c in _DATE_COLUMNS), None)
    days = []
    if date_col:
        days = sorted({day for day in (_row_date(row.get(date_col))
                                       for row in rows) if day})
    range_text = ""
    if days:
        range_text = (days[0].isoformat() if len(days) == 1
                      else f"{days[0].isoformat()}~{days[-1].isoformat()}")
    total = _total_line(columns, rows)
    metric = next((c for c in columns if c in _AMOUNT_COLUMNS), None)
    if metric is None:
        metric = next((c for c in columns if c in _QUANTITY_COLUMNS), None)
    daily = ""
    if metric in _AMOUNT_COLUMNS and len(days) > 1:
        numbers = [n for n in (_as_number(metric, row.get(metric))
                               for row in rows) if n is not None]
        if numbers:
            daily = f"日均 {_fmt_number(sum(float(n) for n in numbers) / len(days), 2)} 元"
    extremes = ""
    if metric:
        valued = [(row, _as_number(metric, row.get(metric))) for row in rows]
        valued = [(row, n) for row, n in valued if n is not None]
        if len(valued) >= 2:
            high = max(valued, key=lambda item: float(item[1]))
            low = min(valued, key=lambda item: float(item[1]))
            if float(high[1]) != float(low[1]):
                unit = " 元" if metric in _AMOUNT_COLUMNS else " 件"
                first_col = columns[0]

                def _label(row):
                    if date_col:
                        day = _row_date(row.get(date_col))
                        if day:
                            return f"{day.month:02d}-{day.day:02d}"
                    return str(row.get(first_col) or "")

                extremes = (f"峰值 {_label(high[0])}"
                            f"（{_fmt_value(metric, high[1])}{unit}）、"
                            f"谷值 {_label(low[0])}"
                            f"（{_fmt_value(metric, low[1])}{unit}）")
    body = "；".join(part for part in (
        "、".join(part for part in (total, daily) if part), extremes) if part)
    if not body:
        return ""
    return f"{range_text}：{body}" if range_text else body


def render_answer(rows, *, total_rows=None, period=None, max_rows=MAX_ROWS):
    """答案块 = 结论卡 + 明细表（期间过滤后）；空行返回空串。

    契约：结论卡与表格出自同一份（过滤后的）行数据，数字永不打架；
    合计只在结论卡出现一次。
    """
    rows = filter_period(rows, period)
    if not rows:
        return ""
    complete = total_rows is None or total_rows <= len(rows)
    card = summary_card(rows, complete=complete)
    table = render_table(rows, total_rows=total_rows, max_rows=max_rows,
                         with_total=False)
    return f"{card}\n{table}" if card else table
