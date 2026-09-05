#!/usr/bin/env python3
"""
渠道日报生成
============
从 AI表格「各渠道每日总销售」类数据表读取记录，按渠道聚合生成 markdown 日报。

字段自动识别 + 配置覆盖：
- 日期字段：含「日期/时间/日期时间」
- 渠道字段：含「渠道/平台」
- 销售额字段：含「销售额/销售/金额/实付」
- 目标字段：含「目标」
- 达成率字段：含「达成率/达成」
"""
import json
import re
from datetime import datetime

DATE_FIELD_KEYS = ["日期", "时间"]
CHANNEL_FIELD_KEYS = ["渠道", "平台"]
SALES_FIELD_KEYS = ["销售额", "销售", "金额", "实付"]
TARGET_FIELD_KEYS = ["目标"]
RATE_FIELD_KEYS = ["达成率", "达成"]


def flat_value(v, depth=0):
    if depth > 3 or v is None:
        return v
    if isinstance(v, dict):
        for k in ("text", "value", "number", "date", "name", "title"):
            if k in v:
                return flat_value(v[k], depth + 1)
        return json.dumps(v, ensure_ascii=False)[:40] if v else None
    if isinstance(v, list):
        return ", ".join(str(flat_value(x, depth + 1)) for x in v)
    return v


def _norm_date(v):
    """把各种日期形态统一成 YYYY-MM-DD，无法解析返回 None"""
    if v is None:
        return None
    s = str(v).strip()
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    try:  # ISO / 时间戳毫秒
        if s.isdigit() and len(s) >= 12:
            return datetime.fromtimestamp(int(s) / 1000).strftime("%Y-%m-%d")
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return None


def _to_num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").replace("¥", "").replace("%", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def record_fields(record):
    """notable 记录 → {字段名: 展示值} 的扁平 dict"""
    fields_map = record.get("fields") or record.get("values") or {}
    if isinstance(fields_map, dict):
        return {k: flat_value(v) for k, v in fields_map.items()}
    return {}


def find_field(names, keys, configured=""):
    if configured:
        for n in names:
            if n == configured:
                return n
        return None
    for key in keys:
        for n in names:
            if key in n:
                return n
    return None


def detect_field_map(sample_records, cfg_fields):
    """从样例记录自动识别字段映射；config.fields 显式指定的优先。"""
    names = []
    for r in sample_records[:50]:
        for k in record_fields(r):
            if k not in names:
                names.append(k)
    mapping = {
        "date": find_field(names, DATE_FIELD_KEYS, cfg_fields.get("date", "")),
        "channel": find_field(names, CHANNEL_FIELD_KEYS, cfg_fields.get("channel", "")),
        "sales": find_field(names, SALES_FIELD_KEYS, cfg_fields.get("sales", "")),
        "target": find_field(names, TARGET_FIELD_KEYS, cfg_fields.get("target", "")),
        "rate": find_field(names, RATE_FIELD_KEYS, cfg_fields.get("achievementRate", "")),
    }
    return mapping


def aggregate(records, field_map, target_date_str):
    """筛选目标日期记录并按渠道聚合。返回 {渠道: {sales, target, rate}}"""
    result = {}
    for r in records:
        f = record_fields(r)
        d = _norm_date(f.get(field_map["date"])) if field_map["date"] else None
        if d != target_date_str:
            continue
        channel = str(f.get(field_map["channel"], "") or "未分类").strip() if field_map["channel"] else "未分类"
        sales = _to_num(f.get(field_map["sales"])) if field_map["sales"] else None
        row = result.setdefault(channel, {"sales": 0.0, "target": None, "rate": None, "count": 0})
        if sales is not None:
            row["sales"] += sales
        if field_map["target"]:
            t = _to_num(f.get(field_map["target"]))
            if t is not None:
                row["target"] = (row["target"] or 0.0) + t
        if field_map["rate"]:
            rate = _to_num(f.get(field_map["rate"]))
            if rate is not None:
                # 多行时用销售额加权近似；单行直接取
                row["rate"] = rate if row["count"] == 0 else max(row["rate"] or 0, rate)
        row["count"] += 1
    return result


def fmt_wan(v):
    """金额格式化：≥1万显示 X.X万，否则原值"""
    if v is None:
        return "--"
    if abs(v) >= 10000:
        return f"{v / 10000:.1f}万"
    return f"{v:,.0f}"


def build_markdown(target_date_str, agg, cfg_report, prev_agg=None):
    """渲染 markdown 日报（钉钉群 markdown）"""
    title = cfg_report.get("title", "渠道日报")
    lines = [f"### {title} {target_date_str}", ""]

    order = cfg_report.get("channelsOrder") or []
    channels = sorted(agg.keys(), key=lambda c: order.index(c) if c in order else len(order))
    total = sum(x["sales"] for x in agg.values())

    lines.append(f"**今日全渠道销售额：{fmt_wan(total)} 元**")
    lines.append("")
    lines.append("| 渠道 | 销售额 | 环比 | 目标 | 达成率 |")
    lines.append("|---|---|---|---|---|")

    for c in channels:
        row = agg[c]
        mom = "--"
        if prev_agg and c in prev_agg and prev_agg[c]["sales"]:
            r = (row["sales"] - prev_agg[c]["sales"]) / abs(prev_agg[c]["sales"])
            mom = f"{'+' if r >= 0 else ''}{r * 100:.1f}%"
        target = fmt_wan(row["target"]) if cfg_report.get("includeAchievement") else "--"
        rate = "--"
        if cfg_report.get("includeAchievement"):
            if row["rate"] is not None:
                rate = f"{row['rate']:.1f}%" if row["rate"] <= 1 else f"{row['rate']:.0f}%"
            elif row["target"]:
                rate = f"{row['sales'] / row['target'] * 100:.1f}%"
        lines.append(f"| {c} | {fmt_wan(row['sales'])} | {mom} | {target} | {rate} |")

    lines.append("")
    lines.append(f"> 数据来源：钉钉AI表格 · 生成时间 {datetime.now().strftime('%H:%M')}")
    return "\n".join(lines), total


def build_empty_markdown(target_date_str, cfg_report):
    title = cfg_report.get("title", "渠道日报")
    return (f"### {title} {target_date_str}\n\n"
            f"⚠️ 当日无数据（表内未找到 {target_date_str} 的记录），请检查数据登记是否完成。")
