"""渠道日销·订单口径聚合（渠道管线迁 WDT 第 3 步）。"""
import unittest
from datetime import date, datetime

from common.public_data.channel_order_daily import (
    FACT_CHANNEL_ORDER_DAILY,
    RECORD_PREFIX,
    UNMAPPED_KEY,
    aggregate_channel_daily,
    build_fact_rows,
    channel_order_daily_ddl_statements,
    main,
    run_channel_order_daily,
)


def _rule_tuple(channel_key, match_value, match_type="buyer_nick",
                mode="exact", priority=10, label=None, panel="channel",
                business_line="online"):
    """按 fetch_channel_map 的列序（_MAP_KEYS）造一行。"""
    return (
        1, channel_key, label or channel_key, panel, business_line,
        match_type, mode, match_value, priority, None, 1,
    )


class _FakeCursor:
    def __init__(self, rules, orders):
        self._rules = rules
        self._orders = orders
        self.writes = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        if "dim_channel_map" in sql:
            self._pending = list(self._rules)
        elif "fact_order_line" in sql:
            self._pending = list(self._orders)
        else:
            self.writes += 1
            self._pending = []
        return 1

    def fetchall(self):
        return list(getattr(self, "_pending", []))


class _FakeConnection:
    def __init__(self, rules, orders):
        self._cursor = _FakeCursor(rules, orders)
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


class AggregateTests(unittest.TestCase):
    def test_groups_by_date_and_channel(self):
        rules = [
            {"channel_key": "拼多多", "match_type": "shop",
             "match_mode": "contains", "match_value": "拼多多", "priority": 72,
             "channel_label": "拼多多", "panel": "channel",
             "business_line": "online"},
        ]
        rows = [
            {"trade_time": datetime(2026, 10, 22, 9, 0), "paid_amount": 100,
             "shop_name": "习水村-拼多多习酒旗舰店", "buyer_nick": ""},
            {"trade_time": datetime(2026, 10, 22, 23, 30), "paid_amount": 50,
             "shop_name": "习水村-拼多多习酒旗舰店", "buyer_nick": ""},
            {"trade_time": datetime(2026, 10, 23, 9, 0), "paid_amount": 7,
             "shop_name": "习水村-拼多多习酒旗舰店", "buyer_nick": ""},
        ]
        agg = aggregate_channel_daily(rows, rules)
        self.assertEqual(agg[date(2026, 10, 22), "拼多多"]["order_count"], 2)
        self.assertEqual(
            agg[date(2026, 10, 22), "拼多多"]["paid_amount"], 150.0)
        self.assertEqual(agg[date(2026, 10, 23), "拼多多"]["order_count"], 1)

    def test_unmapped_orders_are_kept_visible(self):
        """映射落后于新开店铺时，未命中订单必须落到「未映射」而不是被丢掉。"""
        agg = aggregate_channel_daily(
            [{"trade_time": datetime(2026, 10, 22, 9, 0), "paid_amount": 3,
              "shop_name": "习水村-新平台某某店", "buyer_nick": ""}],
            [{"channel_key": "拼多多", "match_type": "shop",
              "match_mode": "contains", "match_value": "拼多多",
              "priority": 72}],
        )
        self.assertEqual(len(agg), 1)
        (_date, key), bucket = next(iter(agg.items()))
        self.assertEqual(key, UNMAPPED_KEY)
        self.assertEqual(bucket["paid_amount"], 3.0)

    def test_empty_rules_falls_back_to_unmapped(self):
        agg = aggregate_channel_daily(
            [{"trade_time": datetime(2026, 10, 22, 9, 0), "paid_amount": 1}],
            [],
        )
        self.assertEqual(
            list(agg.values())[0]["order_count"], 1)
        self.assertEqual(list(agg)[0][1], UNMAPPED_KEY)


class BuildRowsTests(unittest.TestCase):
    def test_record_id_and_column_count(self):
        rules = [
            {"channel_key": "猫超", "channel_label": "猫超", "panel": "channel",
             "business_line": "online", "match_type": "buyer_nick",
             "match_mode": "exact", "match_value": "天猫", "priority": 10},
        ]
        agg = {("2026-10-22", "猫超"): {"order_count": 2, "paid_amount": 10.5}}
        rows = build_fact_rows(agg, rules, sync_run_id="run-1")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(len(row), 10)
        self.assertEqual(
            row[0], f"{RECORD_PREFIX}:2026-10-22:猫超")
        self.assertEqual(row[4], "channel")
        self.assertEqual(row[5], "online")
        self.assertEqual(row[6], 2)
        self.assertEqual(row[7], 10.5)


class RunTests(unittest.TestCase):
    def test_dry_run_counts_without_writing(self):
        conn = _FakeConnection(
            [_rule_tuple("拼多多", "拼多多", match_type="shop", mode="contains")],
            [{"trade_time": datetime(2026, 10, 22, 9, 0), "paid_amount": 9,
              "shop_name": "习水村-拼多多习酒旗舰店", "buyer_nick": ""}],
        )
        stats = run_channel_order_daily(
            conn, start=datetime(2026, 10, 22), end=datetime(2026, 10, 23),
            apply_write=False,
        )
        self.assertEqual(stats["orders"], 1)
        self.assertEqual(stats["rows"], 1)
        self.assertEqual(stats["written"], 0)
        self.assertFalse(conn.committed)

    def test_apply_writes_and_commits(self):
        conn = _FakeConnection(
            [_rule_tuple("拼多多", "拼多多", match_type="shop", mode="contains")],
            [{"trade_time": datetime(2026, 10, 22, 9, 0), "paid_amount": 9,
              "shop_name": "习水村-拼多多习酒旗舰店", "buyer_nick": ""}],
        )
        stats = run_channel_order_daily(
            conn, start=datetime(2026, 10, 22), end=datetime(2026, 10, 23),
            apply_write=True,
        )
        self.assertEqual(stats["written"], 1)
        self.assertTrue(conn.committed)


class DdlTests(unittest.TestCase):
    def test_ddl_is_replayable_and_keyed(self):
        statements = channel_order_daily_ddl_statements()
        self.assertEqual(len(statements), 1)
        sql = statements[0]
        self.assertIn("CREATE TABLE IF NOT EXISTS", sql)
        self.assertIn(FACT_CHANNEL_ORDER_DAILY, sql)
        # 业务键唯一约束：订单口径敢幂等重放全靠它（旧表没有）
        self.assertIn("UNIQUE KEY `uk_channel_order_daily`", sql)


class CliTests(unittest.TestCase):
    def test_refuses_without_live_read(self):
        """门禁参数缺失时退出码 2，且绝不连库。"""
        self.assertEqual(main(["--confirm-local-test-write"]), 2)


if __name__ == "__main__":
    unittest.main()
