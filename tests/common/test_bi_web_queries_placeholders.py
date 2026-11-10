"""test_bi_web_queries 的 placeholders 域测试；断言自原文件等价迁移。"""

from tests.common.bi_web_query_test_support import *


class PlaceholderCardTests(unittest.TestCase):
    """五张 0 占位结构卡（待接入页）：不查库、rows 空、has_fact=false。

    契约逐字遵守：``{"chart": "table", "columns": [...], "rows": [],
    "has_fact": false, "unit": "元"}``；run 函数直接返回静态结构，
    ``connection`` 绝不被触碰（``executed`` 必须为空）。
    """

    #: card run 函数 → 钉死的列结构（key, format|None 有序对）。
    _PLACEHOLDER_CARDS = (
        (run_table_inventory_aging, (
            ("sku", None), ("warehouse", None), ("qty", "number"),
            ("amount", "wan"), ("aging_0_90", "wan"), ("aging_91_180", "wan"),
            ("aging_181_365", "wan"), ("aging_over_365", "wan"),
            ("severity", "severity"),
        )),
        (run_table_warehouse_ops, (
            ("order_no", None), ("channel", None), ("item", None),
            ("reason", None), ("hours", "number"), ("sla_band", None),
            ("severity", "severity"),
        )),
        (run_table_quarter_budget_actual, (
            ("board", None), ("revenue_budget", "wan"),
            ("revenue_actual", "wan"), ("revenue_rate", "percent"),
            ("profit_budget", "wan"), ("profit_actual", "wan"),
            ("profit_rate", "percent"), ("expense_budget", "wan"),
            ("expense_actual", "wan"), ("expense_rate", "percent"),
        )),
        (run_table_yoy_monthly, (
            ("board", None), ("brand", None), ("current", "wan"),
            ("previous", "wan"), ("diff", "wan"), ("yoy", "pct"),
            ("cum_yoy", "pct"), ("severity", "severity"),
        )),
        (run_table_contract_writeoff, (
            ("brand", None), ("channel", None), ("total", "wan"),
            ("done", "wan"), ("pending", "wan"), ("overdue", "wan"),
            ("rate", "percent"), ("severity", "severity"),
        )),
    )

    def test_payload_contract_and_no_sql(self):
        for run, expected_columns in self._PLACEHOLDER_CARDS:
            with self.subTest(card=run.__name__):
                connection = FakeConnection()

                payload = run(connection, {})

                # 0 占位卡不得执行任何 SQL。
                self.assertEqual([], connection.executed)
                self.assertEqual("table", payload["chart"])
                self.assertEqual([], payload["rows"])
                self.assertIs(False, payload["has_fact"])  # 挂零语义，非 falsy 巧合
                self.assertEqual("元", payload["unit"])
                self.assertEqual(
                    list(expected_columns),
                    [(column["key"], column.get("format"))
                     for column in payload["columns"]],
                )
