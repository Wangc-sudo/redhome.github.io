"""运营闭环看板（ops-health）四卡的行为测试（2026-10-24 用户裁决）。

钉住：填报完成率的应填/已填口径与合计行、播报触达的状态与类型翻译、
数据新鲜度的距今小时计算、异常出口的三类行。
"""

import unittest
from datetime import datetime

from common.bi_web import queries


class _QueueCursor:
    """按 execute 调用序弹出脚本结果（fetchall/fetchone 同结果）。"""

    def __init__(self, owner):
        self._owner = owner
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self._owner.executed.append((" ".join(str(sql).split()), params))
        self._result = (
            self._owner.script.pop(0) if self._owner.script else [])

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def close(self):
        return None


class _QueueConnection:
    def __init__(self, script):
        self.script = list(script)
        self.executed = []

    def cursor(self):
        return _QueueCursor(self)


class OpsIntakeTodayTests(unittest.TestCase):
    def test_completion_rate_by_region_with_total(self):
        conn = _QueueConnection([
            [{"region": "hangzhou", "c": 10}, {"region": "shaoxing", "c": 6}],
            [{"region": "hangzhou", "c": 7}, {"region": "shaoxing", "c": 3}],
        ])
        result = queries.run_table_ops_intake_today(conn, {})
        rows = result["rows"]
        hz = rows[0]
        self.assertEqual("杭州", hz["region"])
        self.assertEqual(10, hz["expected"])
        self.assertEqual(7, hz["filled"])
        self.assertAlmostEqual(0.7, hz["rate"])
        total = rows[-1]
        self.assertEqual("合计", total["region"])
        self.assertEqual(16, total["expected"])
        self.assertEqual(10, total["filled"])
        # 日期参数化（不依赖 DB 会话时区）
        sql, params = conn.executed[1]
        self.assertIn("business_date`=%s", sql)
        self.assertIsNotNone(params[0])

    def test_unrostered_regions_absent_and_zero_safe(self):
        conn = _QueueConnection([[], []])
        result = queries.run_table_ops_intake_today(conn, {})
        total = result["rows"][-1]
        self.assertEqual(0, total["expected"])
        self.assertIsNone(total["rate"])  # 除零安全


class OpsBroadcastTodayTests(unittest.TestCase):
    def test_status_and_kind_translated(self):
        conn = _QueueConnection([
            [{"region": "hangzhou", "kind": "offline_daily",
              "status": "delivered", "c": 1},
             {"region": "vanke", "kind": "ding", "status": "failed",
              "c": 2}],
        ])
        result = queries.run_table_ops_broadcast_today(conn, {})
        rows = result["rows"]
        self.assertEqual("晨报", rows[0]["kind"])
        self.assertEqual("已投递", rows[0]["status"])
        self.assertEqual("DING", rows[1]["kind"])
        self.assertEqual("失败", rows[1]["status"])
        self.assertEqual("万科", rows[1]["region"])

    def test_empty_before_morning_broadcast(self):
        # 晨报 08:30 前空表是正常态
        conn = _QueueConnection([[]])
        result = queries.run_table_ops_broadcast_today(conn, {})
        self.assertEqual([], result["rows"])


class OpsFreshnessTests(unittest.TestCase):
    def test_age_hours_computed_from_synced_at(self):
        conn = _QueueConnection([
            [{"d": "2026-10-23", "s": datetime(2026, 10, 24, 8, 0)}],
            [{"d": "2026-10-23", "s": datetime(2026, 10, 24, 9, 30)}],
            [{"d": datetime(2026, 10, 23, 15, 0),
              "s": datetime(2026, 10, 24, 10, 0)}],
            [{"d": None, "s": None}],
        ])
        result = queries.run_table_ops_freshness(conn, {})
        rows = result["rows"]
        self.assertEqual("线下日报", rows[0]["dataset"])
        self.assertIsNotNone(rows[0]["age_hours"])
        self.assertIsNone(rows[3]["age_hours"])  # 从未入库 → 空而非 0


class OpsExceptionsTests(unittest.TestCase):
    def test_three_channels(self):
        conn = _QueueConnection([
            [{"c": 2}],                                  # 未投递存量
            [{"c": 1}],                                  # 问答失败 7d
            [{"service_id": "rag-index-docs", "status": "failed",
              "finished_at": "2026-10-24 03:17"}],       # 管线失败
        ])
        result = queries.run_table_ops_exceptions(conn, {})
        items = [row["item"] for row in result["rows"]]
        self.assertEqual(3, len(items))
        self.assertIn("播报未投递", items[0])
        self.assertIn("小易问答失败", items[1])
        self.assertIn("rag-index-docs", items[2])


if __name__ == "__main__":
    unittest.main()
