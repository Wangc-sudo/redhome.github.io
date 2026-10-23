"""渠道看板「平台账单费用」两卡的行为测试（阶段4 B 方案，2026-10-23）。

账单走人工导入通道落到 ``mart_ops.fact_manual_report`` 窄表：多维度被压成
``dim_scope``（字段名 + 连接）与 ``dimension_value``（取值 | 连接）。卡片必须
按 dim_scope 反解，且**只认最近一个已导入月份**（月报导入常滞后，锚定当月会
经常性空白）。这里钉住这三条语义。
"""

import unittest
from datetime import date
from decimal import Decimal

from common.bi_web import queries


class _ScriptedCursor:
    """按调用序返回脚本结果的游标：第一次 fetchone（期间），第二次 fetchall（明细）。"""

    def __init__(self, owner):
        self._owner = owner

    def execute(self, sql, params=None):
        self._owner.executed.append(" ".join(str(sql).split()))

    def fetchone(self):
        return self._owner.script[0]

    def fetchall(self):
        return self._owner.script[1]

    def close(self):
        return None


class _ScriptedConnection:
    def __init__(self, period_row, fact_rows):
        self.script = (period_row, fact_rows)
        self.executed = []

    def cursor(self):
        return _ScriptedCursor(self)


def _fact_row(scope, value, amount):
    return {
        "dim_scope": scope,
        "dimension_value": value,
        "value": Decimal(amount),
    }


_BILL_ROWS = [
    _fact_row("platform+store+fee_item", "拼多多|PDD习酒旗舰店|推广-全站", "12345.60"),
    _fact_row("platform+store+fee_item", "抖音|DY习酒酒类旗舰店|推广-关键词", "8800.00"),
    _fact_row("platform+store+fee_item", "天猫||平台扣点", "5200.50"),
]


class BillManualCardTests(unittest.TestCase):
    def test_rollup_groups_by_fee_item_with_share(self):
        """科目汇总：按费用科目聚合，占比以合计为分母。"""
        conn = _ScriptedConnection(
            {"period_start": date(2025, 1, 1)}, list(_BILL_ROWS)
        )
        payload = queries.run_table_qd_bill_manual(conn, {"date": "2025-01-15"})
        self.assertEqual(payload["chart"], "table")
        rows = payload["rows"]
        self.assertEqual(len(rows), 3)
        by_item = {row["fee_item"]: row for row in rows}
        self.assertAlmostEqual(by_item["推广-全站"]["total"], 12345.60, places=2)
        grand = 12345.60 + 8800.00 + 5200.50
        self.assertAlmostEqual(
            by_item["平台扣点"]["share"], 5200.50 / grand, places=6
        )
        # 合计占比归一
        self.assertAlmostEqual(sum(row["share"] for row in rows), 1.0, places=6)

    def test_rollup_title_carries_the_imported_month(self):
        """列标题带实际月份——账单导入滞后时不冒充当月。"""
        conn = _ScriptedConnection(
            {"period_start": date(2025, 1, 1)}, list(_BILL_ROWS)
        )
        payload = queries.run_table_qd_bill_manual(conn, {"date": "2025-03-20"})
        title = [col for col in payload["columns"] if col["key"] == "total"][0]
        self.assertEqual(title["title"], "金额(2025-01)")

    def test_detail_splits_dimensions_and_keeps_blank_store(self):
        """明细按 dim_scope 反解三段；店铺为空留空串，不臆造店名。"""
        conn = _ScriptedConnection(
            {"period_start": date(2025, 1, 1)}, list(_BILL_ROWS)
        )
        payload = queries.run_table_qd_bill_manual_detail(
            conn, {"date": "2025-01-15"}
        )
        rows = payload["rows"]
        self.assertEqual(
            [row["platform"] for row in rows], ["拼多多", "抖音", "天猫"]
        )
        self.assertEqual(rows[0]["store"], "PDD习酒旗舰店")
        self.assertEqual(rows[0]["fee_item"], "推广-全站")
        self.assertEqual(rows[2]["store"], "")
        self.assertAlmostEqual(rows[2]["amount"], 5200.50, places=2)

    def test_no_import_yields_empty_rows(self):
        """从未导入：空表，且列标题不带月份（不编造期间）。"""
        conn = _ScriptedConnection({"period_start": None}, [])
        payload = queries.run_table_qd_bill_manual(conn, {"date": "2026-10-23"})
        self.assertEqual(payload["rows"], [])
        title = [col for col in payload["columns"] if col["key"] == "total"][0]
        self.assertEqual(title["title"], "金额")

    def test_period_query_is_capped_by_anchor(self):
        """只取不晚于锚点月的已导入月份（未来月不提前展示）。"""
        conn = _ScriptedConnection({"period_start": None}, [])
        queries.run_table_qd_bill_manual(conn, {"date": "2026-10-23"})
        period_sql = conn.executed[0]
        self.assertIn("MAX(`period_start`)", period_sql)
        self.assertIn("period_start` <= %s", period_sql)

    def test_dimension_order_follows_dim_scope(self):
        """模板加维度（dim_scope 变化）时按字段名对齐，不会错位。"""
        rows = [
            _fact_row(
                "platform+fee_item", "京东|推广-人群", "1000",
            )
        ]
        conn = _ScriptedConnection({"period_start": date(2026, 9, 1)}, rows)
        payload = queries.run_table_qd_bill_manual_detail(
            conn, {"date": "2026-09-10"}
        )
        row = payload["rows"][0]
        self.assertEqual(row["platform"], "京东")
        self.assertEqual(row["fee_item"], "推广-人群")
        self.assertEqual(row["store"], "")


if __name__ == "__main__":
    unittest.main()
