"""BI ops_health 查询域；SQL 与返回契约自原 queries.py 等价迁移。"""

from common.bi_web.query_domains.common import *


_OPS_REGION_LABELS = {"hangzhou": "杭州", "shaoxing": "绍兴", "vanke": "万科"}


_OPS_KIND_LABELS = {
    "offline_daily": "晨报", "remind": "提醒", "check": "催办",
    "ding": "DING", "leaderboard": "榜单", "alert": "告警",
    "channel_missing": "渠道缺数",
}


_OPS_STATUS_LABELS = {
    "delivered": "已投递", "pending": "待投递", "failed": "失败",
    "dead": "死信",
}


def _ops_table(columns, rows):
    """运营闭环卡共用 table 载荷（无锚点参数，页脚日期=容器本地今日）。"""
    return {
        "chart": "table",
        "columns": columns,
        "rows": [
            {key: _qd_jsonable(value) for key, value in row.items()}
            for row in rows
        ],
        "date": datetime.now().date().isoformat(),
    }


def run_table_ops_intake_today(connection, params) -> dict:
    """今日填报完成率（table）：报数区域 × 应填 × 已填 × 完成率。

    应填=dim_robot_member 该区域在职成员数；已填=fact_daily_report_offline
    当日 distinct 责任人。日期参数化（不依赖 DB 会话时区）。
    """
    today = datetime.now().date()
    with connection.cursor() as cur:
        cur.execute(
            "SELECT `region`, COUNT(*) AS c FROM `dim_robot_member` "
            "WHERE `is_active`=1 "
            "AND `region` IN ('hangzhou','shaoxing','vanke') "
            "GROUP BY `region`")
        expected = {row["region"]: row["c"] for row in cur.fetchall()}
        cur.execute(
            "SELECT `region`, COUNT(DISTINCT `responsible_person`) AS c "
            "FROM `fact_daily_report_offline` WHERE `business_date`=%s "
            "GROUP BY `region`", (today,))
        filled = {row["region"]: row["c"] for row in cur.fetchall()}
    rows = []
    total_exp = total_got = 0
    for region in ("hangzhou", "shaoxing", "vanke"):
        exp = expected.get(region, 0)
        got = filled.get(region, 0)
        total_exp += exp
        total_got += got
        rows.append({
            "region": _OPS_REGION_LABELS[region],
            "expected": exp,
            "filled": got,
            "rate": (got / exp) if exp else None,
        })
    rows.append({
        "region": "合计",
        "expected": total_exp,
        "filled": total_got,
        "rate": (total_got / total_exp) if total_exp else None,
    })
    columns = [
        {"key": "region", "title": "区域"},
        {"key": "expected", "title": "应填人数"},
        {"key": "filled", "title": "今日已填"},
        {"key": "rate", "title": "完成率", "format": "percent"},
    ]
    return _ops_table(columns, rows)


def run_table_ops_broadcast_today(connection, params) -> dict:
    """今日播报触达（table）：区域 × 类型 × 状态 × 条数。

    robot_outbox 按 business_date 业务日口径；晨报 08:30 前为空是正常态
    （空表即「还没到时候」）。
    """
    today = datetime.now().date()
    with connection.cursor() as cur:
        cur.execute(
            "SELECT `region`, `kind`, `status`, COUNT(*) AS c "
            "FROM `robot_outbox` WHERE `business_date`=%s "
            "GROUP BY `region`, `kind`, `status` "
            "ORDER BY `region`, `kind`", (today,))
        items = cur.fetchall()
    rows = [{
        "region": _OPS_REGION_LABELS.get(row["region"], row["region"] or "-"),
        "kind": _OPS_KIND_LABELS.get(row["kind"], row["kind"]),
        "status": _OPS_STATUS_LABELS.get(row["status"], row["status"]),
        "count": row["c"],
    } for row in items]
    columns = [
        {"key": "region", "title": "区域"},
        {"key": "kind", "title": "类型"},
        {"key": "status", "title": "状态"},
        {"key": "count", "title": "条数"},
    ]
    return _ops_table(columns, rows)


def run_table_ops_freshness(connection, params) -> dict:
    """数据新鲜度（table）：关键数据面最新业务日期 + 最近入库时间 + 距今。"""
    now = datetime.now()
    rows = []
    with connection.cursor() as cur:
        for table, date_col, label in (
            ("fact_daily_report_offline", "business_date", "线下日报"),
            ("fact_channel_daily_sales", "business_date", "渠道日销"),
            ("fact_order_line", "trade_time", "交易订单"),
            ("kb_products", "synced_at", "商品资料"),
        ):
            cur.execute(
                f"SELECT MAX(`{date_col}`) AS d, MAX(`synced_at`) AS s "
                f"FROM `{table}`")
            row = cur.fetchone() or {}
            synced = row.get("s")
            age_h = round((now - synced).total_seconds() / 3600, 1) \
                if synced else None
            rows.append({
                "dataset": label,
                "latest_data": row.get("d"),
                "last_synced": synced,
                "age_hours": age_h,
            })
    columns = [
        {"key": "dataset", "title": "数据面"},
        {"key": "latest_data", "title": "最新数据日期"},
        {"key": "last_synced", "title": "最近入库"},
        {"key": "age_hours", "title": "距今（小时）"},
    ]
    return _ops_table(columns, rows)


def run_table_ops_exceptions(connection, params) -> dict:
    """异常出口（table）：播报未投递 + 问答失败 + 管线失败（近 2 天）。"""
    rows = []
    with connection.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM `robot_outbox` "
            "WHERE `status`<>'delivered'")
        rows.append({
            "item": "播报未投递（全周期存量）",
            "count": cur.fetchone()["c"],
            "note": "robot_outbox 非 delivered",
        })
        cur.execute(
            "SELECT COUNT(*) AS c FROM `kb_qa_audit` "
            "WHERE `failed`=1 AND `created_at`>=NOW()-INTERVAL 7 DAY")
        rows.append({
            "item": "小易问答失败（近 7 天）",
            "count": cur.fetchone()["c"],
            "note": "kb_qa_audit.failed",
        })
        cur.execute(
            "SELECT `service_id`, `status`, `finished_at` "
            "FROM `pd_ops_run_history` "
            "WHERE `status`<>'finished' "
            "AND `finished_at`>=NOW()-INTERVAL 2 DAY "
            "ORDER BY `finished_at` DESC LIMIT 5")
        for row in cur.fetchall():
            rows.append({
                "item": f"管线未成功：{row['service_id']}",
                "count": 1,
                "note": f"{row['status']} @ {row['finished_at']}",
            })
    columns = [
        {"key": "item", "title": "异常项"},
        {"key": "count", "title": "数量"},
        {"key": "note", "title": "说明"},
    ]
    return _ops_table(columns, rows)

__all__ = ('_OPS_REGION_LABELS', '_OPS_KIND_LABELS', '_OPS_STATUS_LABELS', '_ops_table', 'run_table_ops_intake_today', 'run_table_ops_broadcast_today', 'run_table_ops_freshness', 'run_table_ops_exceptions')
