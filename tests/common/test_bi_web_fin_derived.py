# -*- coding: utf-8 -*-
"""资金安全页派生口径测试（fund-safety-draft §3）——零 DB 依赖。

覆盖：``aging_over_60`` 差额推导与三分量缺失各分支（不静默补 0）、
``days_outstanding`` 的 None 护栏（不猜日期）、超期阈值 60/61 边界
（``OVERDUE_DAYS_THRESHOLD = 60``，行业默认值待财务确认）、severity
三档映射（p1 超期命中 / p2 不可算 / ok 正常；p0 保留给挂零语义，
fin 卡不用）。

末尾的 :class:`GoldenFixtureTests` 跑 ``tests/fixtures/fin_derived_golden.json``
——期望值手写（形态照 ``derived_golden.json``），不由实现回填。
"""

import json
import unittest
from datetime import date
from pathlib import Path

from common.bi_web import fin_derived
from common.bi_web.fin_derived import (
    OVERDUE_DAYS_THRESHOLD,
    aging_over_60,
    aging_severity,
    days_outstanding,
    deposit_severity,
    uninvoiced_severity,
)

#: 双端对拍夹具（与 derived_golden.json 同目录、同形态）。
_GOLDEN_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests" / "fixtures" / "fin_derived_golden.json"
)


class AgingOver60Tests(unittest.TestCase):
    """差额推导：ending − 0_30 − 31_60；任一分量缺失 → None。"""

    def test_difference_of_three_components(self):
        self.assertEqual(500.0, aging_over_60(1000.0, 300.0, 200.0))

    def test_zero_difference_is_a_value_not_none(self):
        """差额 0 是可算结果（不算超期），不是数据缺陷。"""
        self.assertEqual(0.0, aging_over_60(500.0, 300.0, 200.0))

    def test_any_missing_component_yields_none(self):
        """三分量缺失各分支 → None，绝不静默补 0。"""
        self.assertIsNone(aging_over_60(None, 300.0, 200.0))
        self.assertIsNone(aging_over_60(1000.0, None, 200.0))
        self.assertIsNone(aging_over_60(1000.0, 300.0, None))


class DaysOutstandingTests(unittest.TestCase):
    """挂账天数：对账日不可解析 → None（不猜日期）。"""

    def test_natural_day_difference(self):
        self.assertEqual(
            60, days_outstanding(date(2026, 7, 18), date(2026, 9, 16))
        )

    def test_missing_statement_date_yields_none(self):
        self.assertIsNone(days_outstanding(None, date(2026, 9, 16)))
        self.assertIsNone(days_outstanding(date(2026, 7, 18), None))


class SeverityTests(unittest.TestCase):
    """severity 三档：p1 超期命中 / p2 不可算 / ok 正常（fin 卡不用 p0）。"""

    def test_overdue_threshold_is_the_industry_default_60(self):
        """行业默认值，待财务确认；确认后只改常量，不动函数。"""
        self.assertEqual(60, OVERDUE_DAYS_THRESHOLD)

    def test_aging_severity(self):
        self.assertEqual("p1", aging_severity(500.0, 0.0))
        self.assertEqual("p1", aging_severity(0.0, 50.0))
        self.assertEqual("p2", aging_severity(None, 50.0))
        self.assertEqual("ok", aging_severity(0.0, 0.0))
        self.assertEqual("ok", aging_severity(0.0, None))

    def test_uninvoiced_threshold_boundary_60_61(self):
        """取严格大于：恰好 60 天不超期，61 天命中。"""
        self.assertEqual(
            "ok", uninvoiced_severity(100.0, OVERDUE_DAYS_THRESHOLD)
        )
        self.assertEqual(
            "p1", uninvoiced_severity(100.0, OVERDUE_DAYS_THRESHOLD + 1)
        )

    def test_uninvoiced_uncomputable_is_p2(self):
        self.assertIsNone(days_outstanding(None, date(2026, 9, 16)))
        self.assertEqual("p2", uninvoiced_severity(None, 61))
        self.assertEqual("p2", uninvoiced_severity(100.0, None))
        self.assertEqual("ok", uninvoiced_severity(0.0, 90))

    def test_deposit_severity(self):
        self.assertEqual("ok", deposit_severity("正常合作"))
        self.assertEqual("ok", deposit_severity("正常运营"))
        self.assertEqual("p2", deposit_severity("已终止"))
        self.assertEqual("p2", deposit_severity(None))


class GoldenFixtureTests(unittest.TestCase):
    """用 ``fin_derived_golden.json`` 逐位对拍（期望值手写）。"""

    @classmethod
    def setUpClass(cls):
        with _GOLDEN_PATH.open(encoding="utf-8") as handle:
            cls.fixture = json.load(handle)

    def _assert_scalar(self, label, actual, expected):
        if expected is None:
            self.assertIsNone(actual, f"{label}: 期望 None，实际 {actual!r}")
        elif isinstance(expected, float):
            self.assertAlmostEqual(
                expected, actual, delta=1e-9,
                msg=f"{label}: 期望 {expected}，实际 {actual}",
            )
        else:
            self.assertEqual(expected, actual, label)

    def test_aging_cases_match_the_frozen_golden_values(self):
        for case in self.fixture["aging_cases"]:
            with self.subTest(case=case["name"]):
                self._assert_scalar(
                    "aging_over_60",
                    fin_derived.aging_over_60(
                        case["inputs"]["ending_balance"],
                        case["inputs"]["aging_0_30"],
                        case["inputs"]["aging_31_60"],
                    ),
                    case["expect"]["over_60"],
                )

    def test_days_cases_match_the_frozen_golden_values(self):
        for case in self.fixture["days_cases"]:
            with self.subTest(case=case["name"]):
                raw = case["inputs"]["statement_date"]
                statement_date = (
                    None if raw is None else date.fromisoformat(raw)
                )
                today = date.fromisoformat(case["inputs"]["today"])
                self._assert_scalar(
                    "days_outstanding",
                    fin_derived.days_outstanding(statement_date, today),
                    case["expect"]["days"],
                )

    def test_severity_cases_match_the_frozen_golden_values(self):
        for case in self.fixture["severity_cases"]:
            with self.subTest(case=case["name"]):
                function = getattr(fin_derived, case["function"])
                self._assert_scalar(
                    case["function"],
                    function(**case["inputs"]),
                    case["expect"]["severity"],
                )

    def test_every_case_has_a_unique_name_and_a_draft_source(self):
        cases = (
            self.fixture["aging_cases"]
            + self.fixture["days_cases"]
            + self.fixture["severity_cases"]
        )
        names = [case["name"] for case in cases]

        self.assertEqual(len(names), len(set(names)), "用例名必须唯一")
        for case in cases:
            with self.subTest(case=case["name"]):
                self.assertTrue(
                    str(case.get("source", "")).startswith("draft §"),
                    "source 必须标注 fund-safety-draft 条款，便于口径变更时定位",
                )


if __name__ == "__main__":
    unittest.main()
