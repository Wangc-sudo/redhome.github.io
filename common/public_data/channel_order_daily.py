"""渠道日销·订单口径聚合（渠道管线迁 WDT 第 3 步，2026-10-23）。

前情：渠道日销唯一真源长期是 AI 表人工台账（每渠道一个 sheet，靠人填），
第 2 步（``channel_map.py``）已把订单行解析成渠道键，本模块负责把订单
**聚合成渠道日销**——这是「渠道日销不再靠人工填报」的真正落点。

**为什么不写回 ``fact_channel_daily_sales``**：那张表是人工/机器人填报口径，
只按 ``source_record_id`` 唯一、没有任何口径列，两种口径混在一行里会互相
覆盖且事后无法区分。故新开 ``fact_channel_order_daily``（订单口径）双轨，
对账清楚后再谈退役人工口径（第 4 步，需运维裁决）。

口径（与既有订单侧一致，不另立标准）：
- 源：``fact_order_line``（extract 阶段已剔除 trade_status ∈ {4 已取消,
  5 全额退款关闭, 24 待付款}）。
- 金额：``paid_amount`` 毛额（部分退款不回扣，与 BI 月度口径一致）。
- 日期：``DATE(trade_time)``。
- 渠道：``channel_map.resolve_channel``（抬头/店铺名 → 渠道键）。
- 业务键 ``(business_date, channel_key)``，落 UNIQUE 约束（旧表没有，这是
  新表敢幂等重放的前提）。
- 未命中映射的订单不丢弃，落到 ``channel_key='未映射'``——映射表落后于新开
  店铺时这里是唯一可见的告警面。
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

from common.public_data.channel_map import fetch_channel_map, resolve_channel
from common.public_data.db import connect
from common.public_data.settings import Settings

logger = logging.getLogger(__name__)

FACT_CHANNEL_ORDER_DAILY = "fact_channel_order_daily"

#: 未命中映射的订单落这个键（运维看板可据此补映射规则）。
UNMAPPED_KEY = "未映射"

#: source_record_id 前缀：与人工口径（AI recordId / robot:{id}）显式区分。
RECORD_PREFIX = "wdt"

_FACT_CHANNEL_ORDER_DAILY_DDL = (
    "CREATE TABLE IF NOT EXISTS `fact_channel_order_daily` (\n"
    "  `source_record_id` VARCHAR(255) NOT NULL,\n"
    "  `business_date` DATE NOT NULL,\n"
    "  `channel_key` VARCHAR(32) NOT NULL,\n"
    "  `channel_label` VARCHAR(64) DEFAULT NULL,\n"
    "  `panel` VARCHAR(32) DEFAULT NULL,\n"
    "  `business_line` VARCHAR(32) DEFAULT NULL,\n"
    "  `order_count` INT NOT NULL DEFAULT 0,\n"
    "  `paid_amount` DECIMAL(20,4) NOT NULL DEFAULT 0,\n"
    "  `synced_at` DATETIME(6) DEFAULT NULL,\n"
    "  `sync_run_id` VARCHAR(64) DEFAULT NULL,\n"
    "  PRIMARY KEY (`source_record_id`),\n"
    "  UNIQUE KEY `uk_channel_order_daily` (`business_date`, `channel_key`),\n"
    "  KEY `idx_business_date` (`business_date`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_UPSERT_SQL = (
    "INSERT INTO `fact_channel_order_daily` (`source_record_id`, "
    "`business_date`, `channel_key`, `channel_label`, `panel`, "
    "`business_line`, `order_count`, `paid_amount`, `synced_at`, "
    "`sync_run_id`) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON DUPLICATE KEY UPDATE `channel_label` = VALUES(`channel_label`), "
    "`panel` = VALUES(`panel`), `business_line` = VALUES(`business_line`), "
    "`order_count` = VALUES(`order_count`), "
    "`paid_amount` = VALUES(`paid_amount`), `synced_at` = VALUES(`synced_at`), "
    "`sync_run_id` = VALUES(`sync_run_id`)"
)

_SELECT_ORDERS_SQL = (
    "SELECT `trade_time`, `paid_amount`, `shop_name`, `buyer_nick` "
    "FROM `fact_order_line` WHERE `trade_time` >= %s AND `trade_time` < %s"
)

_COLUMNS = (
    "source_record_id", "business_date", "channel_key", "channel_label",
    "panel", "business_line", "order_count", "paid_amount", "synced_at",
    "sync_run_id",
)


def channel_order_daily_ddl_statements() -> tuple:
    """迁移语句：订单口径渠道日销事实表。"""
    return (_FACT_CHANNEL_ORDER_DAILY_DDL,)


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------

def _channel_meta(rules) -> dict:
    """渠道键 → (label, panel, business_line)，取该键的首条规则（同键同元数据）。"""
    meta = {}
    for rule in rules:
        key = rule.get("channel_key")
        if key and key not in meta:
            meta[key] = (
                rule.get("channel_label") or key,
                rule.get("panel"),
                rule.get("business_line"),
            )
    return meta


def aggregate_channel_daily(order_rows, rules):
    """订单行 → ``{(business_date, channel_key): {"order_count", "paid_amount"}}``。

    纯函数（便于测试）：``order_rows`` 是含 ``trade_time``/``paid_amount``/
    ``shop_name``/``buyer_nick`` 的映射序列。未命中映射的订单落
    :data:`UNMAPPED_KEY`，不丢弃。
    """
    agg = defaultdict(lambda: {"order_count": 0, "paid_amount": 0.0})
    for row in order_rows:
        trade_time = row.get("trade_time")
        if trade_time is None:
            continue
        business_date = (
            trade_time.date() if isinstance(trade_time, datetime)
            else trade_time
        )
        channel_key = resolve_channel(row, rules) or UNMAPPED_KEY
        bucket = agg[(business_date, channel_key)]
        bucket["order_count"] += 1
        bucket["paid_amount"] += float(row.get("paid_amount") or 0)
    return dict(agg)


def build_fact_rows(agg, rules, synced_at=None, sync_run_id=None):
    """聚合结果 → 待 upsert 的行（元组序列，列序见 :data:`_COLUMNS`）。"""
    meta = _channel_meta(rules)
    stamp = synced_at or datetime.now()
    rows = []
    for (business_date, channel_key) in sorted(agg):
        label, panel, business_line = meta.get(
            channel_key, (channel_key, None, None))
        bucket = agg[(business_date, channel_key)]
        rows.append((
            f"{RECORD_PREFIX}:{business_date}:{channel_key}",
            business_date,
            channel_key,
            label,
            panel,
            business_line,
            bucket["order_count"],
            round(bucket["paid_amount"], 4),
            stamp,
            sync_run_id,
        ))
    return rows


def run_channel_order_daily(
    connection,
    *,
    start,
    end,
    sync_run_id=None,
    apply_write=False,
):
    """读订单 → 聚合 →（可选）写库；返回统计字典。

    ``start`` 含、``end`` 不含（半开区间，避免 DATETIME 边界漏单）。
    ``apply_write=False`` 时只算不写（演练/测试）。
    """
    rules = fetch_channel_map(connection)
    if not rules:
        logger.warning("dim_channel_map 为空，订单将全部落到「未映射」")
    with connection.cursor() as cur:
        cur.execute(_SELECT_ORDERS_SQL, (start, end))
        order_rows = cur.fetchall()

    agg = aggregate_channel_daily(order_rows, rules)
    rows = build_fact_rows(agg, rules, sync_run_id=sync_run_id)

    written = 0
    if apply_write and rows:
        with connection.cursor() as cur:
            for row in rows:
                written += cur.execute(_UPSERT_SQL, row)
        connection.commit()

    unmapped = sum(
        1 for (_date, key) in agg if key == UNMAPPED_KEY
    )
    stats = {
        "orders": len(order_rows),
        "rows": len(rows),
        "written": written,
        "rules": len(rules),
        "unmapped_keys": unmapped,
        "start": str(start),
        "end": str(end),
    }
    for (business_date, channel_key), bucket in sorted(agg.items()):
        stats.setdefault("by_channel", defaultdict(float))
        stats["by_channel"][channel_key] += bucket["paid_amount"]
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="channel-order-daily",
        description="渠道日销·订单口径聚合（fact_order_line → "
                    "fact_channel_order_daily）",
    )
    parser.add_argument("--start", help="起始日期 YYYY-MM-DD（含）")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD（不含）")
    parser.add_argument(
        "--days", type=int, default=2,
        help="未给 --start/--end 时按「最近 N 天」取窗口（默认 2：覆盖 T-1）",
    )
    parser.add_argument(
        "--live-read", action="store_true",
        help="显式声明读取线上库（与本仓其他管道同款门禁参数）",
    )
    parser.add_argument(
        "--confirm-local-test-write", action="store_true",
        help="确认写入；不给则只算不写（演练）",
    )
    return parser.parse_args(argv)


def _window(args):
    if args.start and args.end:
        return (
            datetime.strptime(args.start, "%Y-%m-%d"),
            datetime.strptime(args.end, "%Y-%m-%d"),
        )
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=args.days)
    return datetime.combine(start, datetime.min.time()), \
        datetime.combine(end, datetime.min.time())


def main(argv=None) -> int:
    args = _parse_args(argv)
    if not args.live_read:
        print("refusing to run without --live-read", file=sys.stderr)
        return 2
    settings = Settings.from_environment()
    start, end = _window(args)
    apply_write = bool(args.confirm_local_test_write)
    with connect(settings.mart_database) as conn:
        stats = run_channel_order_daily(
            conn, start=start, end=end, apply_write=apply_write,
        )
    print(
        "service=channel-order-daily status=completed "
        f"window={stats['start']}..{stats['end']} orders={stats['orders']} "
        f"rows={stats['rows']} written={stats['written']} "
        f"rules={stats['rules']} unmapped_keys={stats['unmapped_keys']} "
        f"mode={'APPLY' if apply_write else 'DRY-RUN'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
