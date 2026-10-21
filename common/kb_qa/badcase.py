# -*- coding: utf-8 -*-
"""kb_qa_audit 反馈闭环报表：失败流水 + 未命中聚类（badcase 复盘入口）。

背景（2026-10-21 用户裁决「先做反馈闭环」）：audit 表扩观测列
（mart-ops-kb-qa-audit-v2）后，「答得对不对」不再靠用户口头反馈——
本模块把失败行与未命中行聚类成可行动的清单：什么问题反复答不了
（知识库缺口）、哪类意图在失败（代码 bug 信号）、agent 工具用得如何。

用法（云上容器内，只读）：
    docker exec -i -e PYTHONPATH=/opt/dops/repo -w /opt/dops/repo \
        dops-scheduler python3 -m common.kb_qa.badcase --days 7
"""

import argparse
from collections import Counter

from common.kb_qa.products import HELP_TEXT

#: 未命中分类：answer_excerpt 前缀 → 类别。顺序即优先级。
_MISS_PREFIXES = (
    ("product_miss", "知识库里暂时没有找到"),
    ("doc_fallback", "产品手册问答还在接入中"),
    ("unmatched_help", HELP_TEXT[:12]),
)

#: 类别中文标签（报表展示）。
CATEGORY_LABELS = {
    "failed": "处理失败",
    "product_miss": "商品未找到",
    "doc_fallback": "手册无召回",
    "unmatched_help": "规则未命中(回 HELP)",
}


def classify_row(row):
    """单行审计 → badcase 类别；正常作答返回 None。

    failed=1 优先；其余按 answer_excerpt 前缀判兜底话术。静默行
    （reply_kind=none，多为空文本消息）不算 badcase。
    """
    if row.get("failed"):
        return "failed"
    if row.get("reply_kind") != "text":
        return None
    excerpt = (row.get("answer_excerpt") or "").strip()
    if not excerpt:
        return None
    for category, prefix in _MISS_PREFIXES:
        if excerpt.startswith(prefix):
            return category
    return None


def _normalize_question(text):
    """聚类键：去首尾空白、压缩连续空白。不做语义归并（V1 精确聚类）。"""
    return " ".join((text or "").split())


def collect_badcases(conn, days=7):
    """聚合近 N 天审计流水 → 报表 dict（纯查询，不写库）。"""
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT `msg_id`, `question`, `intent`, `sender_name`,"
            " `created_at`, `failed`, `reply_kind`, `recall_source`,"
            " `tool_used`, `latency_ms`, `answer_excerpt`"
            " FROM `kb_qa_audit`"
            " WHERE `created_at` >= DATE_SUB(NOW(), INTERVAL %s DAY)"
            " ORDER BY `created_at` DESC",
            (int(days),),
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()

    intents = Counter()
    tools = Counter()
    recall_sources = Counter()
    latencies = []
    bad_counts = Counter()
    bad_samples = {}
    failed_rows = []
    answered = 0

    for row in rows:
        intents[row.get("intent") or "(未回填)"] += 1
        if row.get("reply_kind") == "text":
            answered += 1
        if row.get("recall_source"):
            recall_sources[row["recall_source"]] += 1
        if row.get("tool_used"):
            tools.update(name for name in row["tool_used"].split(",") if name)
        if row.get("latency_ms") is not None:
            latencies.append(row["latency_ms"])
        category = classify_row(row)
        if category is None:
            continue
        key = _normalize_question(row.get("question"))
        bad_counts[(category, key)] += 1
        # rows 已按 created_at DESC，首见即最近样本
        if (category, key) not in bad_samples:
            bad_samples[(category, key)] = row
        if category == "failed":
            failed_rows.append(row)

    latencies.sort()
    size = len(latencies)
    report = {
        "days": int(days),
        "total": len(rows),
        "answered": answered,
        "failed": len(failed_rows),
        "intents": intents,
        "tools": tools,
        "recall_sources": recall_sources,
        "latency_avg": (sum(latencies) // size) if size else None,
        "latency_p95": latencies[int(0.95 * (size - 1))] if size else None,
        "latency_max": latencies[-1] if size else None,
        "badcases": [
            {
                "category": category,
                "question": question,
                "count": count,
                "latest_at": bad_samples[(category, question)].get("created_at"),
                "sender": bad_samples[(category, question)].get("sender_name"),
            }
            for (category, question), count in bad_counts.most_common()
        ],
        "failed_rows": failed_rows[:20],
    }
    return report


def render_markdown(report):
    """报表 dict → markdown 文本（运维可直接粘群/文档）。"""
    lines = [
        f"# kb-qa 反馈闭环报表（近 {report['days']} 天）",
        "",
        f"- 消息总量 {report['total']}，已回复 {report['answered']}，"
        f"处理失败 {report['failed']}",
    ]
    if report["latency_avg"] is not None:
        lines.append(
            f"- 耗时：均值 {report['latency_avg']}ms / "
            f"P95 {report['latency_p95']}ms / 最大 {report['latency_max']}ms")
    if report["intents"]:
        dist = "、".join(f"{name} {count}"
                         for name, count in report["intents"].most_common())
        lines.append(f"- 意图分布：{dist}")
    if report["recall_sources"]:
        dist = "、".join(
            f"{name} {count}"
            for name, count in report["recall_sources"].most_common())
        lines.append(f"- 召回通路：{dist}")
    if report["tools"]:
        dist = "、".join(f"{name}×{count}"
                         for name, count in report["tools"].most_common())
        lines.append(f"- agent 工具调用：{dist}")

    lines += ["", f"## 未命中聚类（{len(report['badcases'])} 组）", ""]
    if report["badcases"]:
        lines.append("| 类别 | 次数 | 问题 | 最近时间 | 提问人 |")
        lines.append("| --- | ---: | --- | --- | --- |")
        for item in report["badcases"]:
            label = CATEGORY_LABELS.get(item["category"], item["category"])
            lines.append(
                f"| {label} | {item['count']} | {item['question']} "
                f"| {item['latest_at']} | {item['sender'] or ''} |")
    else:
        lines.append("（无）")

    lines += ["", "## 失败流水（最近 20 条）", ""]
    if report["failed_rows"]:
        lines.append("| 时间 | 提问人 | 问题 |")
        lines.append("| --- | --- | --- |")
        for row in report["failed_rows"]:
            lines.append(
                f"| {row.get('created_at')} | {row.get('sender_name') or ''} "
                f"| {(row.get('question') or '').strip()} |")
    else:
        lines.append("（无）")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kb_qa.badcase")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)

    from common.public_data import db
    from common.public_data.settings import Settings

    settings = Settings.from_environment()
    conn = db.connect(settings.mart_database)
    try:
        print(render_markdown(collect_badcases(conn, days=args.days)))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
