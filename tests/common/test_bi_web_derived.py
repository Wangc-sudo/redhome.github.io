"""派生指标纯口径测试（CubeSchema §2）——后端算，前端只渲染。

覆盖：§2.1 缺口（绝对口径，不乘时间进度）、§2.2 完成率、§2.3 四级告警的
四条边界（含 p0 的「≥2 工作日」与 p1 的 0.5 系数）、§2.4 环比的方向与
缺失护栏，以及数据缺陷（无目标 / 无日历）一律 ``None``（前端渲染「—」）。

全部不依赖 DB：工作日历用 ``date`` 列表（或 ``dim_calendar`` 行形态）直接
喂进来 —— 这正是「派生口径后端化」要的可测边界。

末尾的 :class:`GoldenFixtureTests` 跑 ``tests/fixtures/derived_golden.json``
—— 那份夹具是给前端 TS（临时 derive.ts）逐位对拍用的，期望值手写而非实现
回填，Python 与 TS 两边都过才算口径统一。
"""

import json
import unittest
from datetime import date
from pathlib import Path

from common.bi_web import derived
from common.bi_web.derived import (
    P0_MIN_ELAPSED_WORKDAYS,
    P1_CAPACITY_FACTOR,
    SEVERITY_DOMAIN,
    elapsed_workdays,
    mom,
    p1_threshold,
    rate,
    remaining_workdays,
    required_daily,
    severity,
    shortfall,
    total_workdays,
    workdays,
)

#: 某月的工作日（周末已剔除），22 天。
_WORKDAYS = [
    date(2026, 9, day)
    for day in (1, 2, 3, 4, 7, 8, 9, 10, 11, 14, 15, 16, 17, 18, 21, 22, 23,
                24, 25, 28, 29, 30)
]

#: dim_calendar 的行形态（dict），顺带验证 ``is_workday`` 过滤。
_CALENDAR_ROWS = [
    {"business_date": day, "is_workday": 1} for day in _WORKDAYS
] + [
    {"business_date": date(2026, 9, 5), "is_workday": 0},
    {"business_date": date(2026, 9, 6), "is_workday": 0},
]


class CalendarTests(unittest.TestCase):
    """工作日集合（两种输入形态）与已过/剩余计数。"""

    def test_workdays_accepts_plain_dates_and_calendar_rows(self):
        self.assertEqual(frozenset(_WORKDAYS), workdays(_WORKDAYS))
        self.assertEqual(frozenset(_WORKDAYS), workdays(_CALENDAR_ROWS))

    def test_workdays_ignores_unusable_calendar_rows(self):
        """日历缺行不可判：未知日不算工作日，也不会抛错。"""
        rows = [
            {"business_date": None, "is_workday": 1},
            {"business_date": date(2026, 9, 1), "is_workday": None},
        ]

        self.assertEqual(frozenset(), workdays(rows))
        self.assertEqual(frozenset(), workdays(None))
        self.assertEqual(frozenset(), workdays([]))

    def test_total_elapsed_remaining_split_the_month(self):
        as_of = date(2026, 9, 15)  # 月中（含当天）

        self.assertEqual(22, total_workdays(_CALENDAR_ROWS))
        self.assertEqual(11, elapsed_workdays(_CALENDAR_ROWS, as_of))
        self.assertEqual(11, remaining_workdays(_CALENDAR_ROWS, as_of))

    def test_elapsed_includes_the_anchor_day(self):
        """否则第 1 个工作日整天看不到告警（与 MTD 含当天同口径）。"""
        self.assertEqual(1, elapsed_workdays(_CALENDAR_ROWS, date(2026, 9, 1)))
        self.assertEqual(21, remaining_workdays(_CALENDAR_ROWS, date(2026, 9, 1)))


class ShortfallTests(unittest.TestCase):
    """§2.1 缺口 = 目标总额 − 已完成。"""

    def test_shortfall_is_absolute_not_time_scaled(self):
        self.assertEqual(70.0, shortfall(100.0, 30.0))

    def test_shortfall_of_an_overachiever_is_negative(self):
        self.assertEqual(-10.0, shortfall(100.0, 110.0))

    def test_missing_value_edges(self):
        self.assertIsNone(shortfall(None, 30.0))  # 无目标 → 前端「—」
        self.assertEqual(100.0, shortfall(100.0, None))  # 无事实行 = 未开单


class RequiredDailyTests(unittest.TestCase):
    """§2.3 所需日均 = 月目标 ÷ 当月总工作日。"""

    def test_required_daily_divides_by_total_workdays(self):
        self.assertEqual(10.0, required_daily(220.0, 22))

    def test_required_daily_needs_a_truthful_denominator(self):
        self.assertIsNone(required_daily(None, 22))
        self.assertIsNone(required_daily(220.0, 0))
        self.assertIsNone(required_daily(220.0, None))


class RateTests(unittest.TestCase):
    """§2.2 完成率：目标缺失或 ≤ 0 → None（不按 0% 计）。"""

    def test_rate_is_done_over_target(self):
        self.assertEqual(0.25, rate(25.0, 100.0))

    def test_missing_target_yields_none(self):
        self.assertIsNone(rate(25.0, None))
        self.assertIsNone(rate(25.0, 0))

    def test_no_sales_is_zero_not_none(self):
        self.assertEqual(0.0, rate(0, 100.0))
        self.assertEqual(0.0, rate(None, 100.0))


class MomTests(unittest.TestCase):
    """§2.4 环比：涨 up / 跌 down / 持平 flat，不可算 → None。"""

    def test_up_down_and_flat(self):
        self.assertEqual({"value": 0.25, "direction": "up"}, mom(125.0, 100.0))
        self.assertEqual({"value": -0.25, "direction": "down"}, mom(75.0, 100.0))
        self.assertEqual({"value": 0.0, "direction": "flat"}, mom(100.0, 100.0))

    def test_missing_or_zero_prev_yields_none(self):
        self.assertIsNone(mom(125.0, None))
        self.assertIsNone(mom(125.0, 0))
        self.assertIsNone(mom(None, 100.0))

    def test_denominator_is_the_absolute_prev(self):
        self.assertEqual({"value": 3.0, "direction": "up"}, mom(100.0, -50.0))


class SeverityTests(unittest.TestCase):
    """§2.3 四级告警（p0 最高，取最先匹配者）。"""

    #: 22 个工作日、目标 220 → 所需日均 10。
    _REQUIRED = 10.0

    def test_p0_needs_zero_done_and_at_least_two_elapsed_workdays(self):
        self.assertEqual(
            "p0",
            severity(0, 220.0, self._REQUIRED, remaining_workdays=20,
                     elapsed_workdays=2),
        )

    def test_p0_boundary_is_two_workdays(self):
        """只过 1 个工作日时，零产出也由产能规则兜底，不直接判 p0。

        旧口径（``levelOf`` 只判 ``done === 0``）第 1 天就全员标红；契约
        §2.3 补的「≥2 工作日」正是这个护栏。
        """
        self.assertEqual(2, P0_MIN_ELAPSED_WORKDAYS)
        self.assertNotEqual(
            "p0",
            severity(0, 220.0, self._REQUIRED, remaining_workdays=21,
                     elapsed_workdays=1),
        )
        self.assertEqual(
            "p0",
            severity(0, 220.0, self._REQUIRED, remaining_workdays=20,
                     elapsed_workdays=2),
        )

    def test_p0_does_not_fire_when_something_was_sold(self):
        """有进度的人按缺口分级，不因「今日未开单」直接跳 p0。"""
        self.assertEqual(
            "p1",
            severity(0.5, 219.5, self._REQUIRED, remaining_workdays=20,
                     elapsed_workdays=2),
        )

    def test_p1_threshold_uses_the_half_capacity_factor(self):
        self.assertEqual(0.5, P1_CAPACITY_FACTOR)
        self.assertEqual(50.0, p1_threshold(self._REQUIRED, 10))
        self.assertEqual(
            "p1",
            severity(0.5, 50.0, self._REQUIRED, remaining_workdays=10,
                     elapsed_workdays=3),
        )  # 缺口 == 阈值也算 p1

    def test_p2_is_one_below_the_p1_threshold(self):
        self.assertEqual(
            "p2",
            severity(50.0, 49.9, self._REQUIRED, remaining_workdays=10,
                     elapsed_workdays=3),
        )

    def test_ok_is_shortfall_at_or_below_zero(self):
        self.assertEqual(
            "ok",
            severity(220.0, 0.0, self._REQUIRED, remaining_workdays=10,
                     elapsed_workdays=12),
        )
        self.assertEqual(
            "ok",
            severity(230.0, -10.0, self._REQUIRED, remaining_workdays=10,
                     elapsed_workdays=12),
        )

    def test_severity_domain_matches_the_contract(self):
        self.assertEqual(("p0", "p1", "p2", "ok"), SEVERITY_DOMAIN)

    def test_missing_target_is_not_an_alert(self):
        """§5：缺口不可算 → None（前端「—」），绝不静默当 ok。"""
        self.assertIsNone(severity(0, None, None, 10, 3))

    def test_missing_calendar_does_not_escalate_to_p1(self):
        """日历缺失时 p1 不可判，退到 p2 —— 不能因数据缺陷把人标橙。"""
        self.assertIsNone(p1_threshold(None, 10))
        self.assertIsNone(p1_threshold(self._REQUIRED, None))
        self.assertEqual("p2", severity(30.0, 190.0, None, None, None))


class MonthStabilityRegressionTests(unittest.TestCase):
    """回归：同一组数据在月初/月中得到同一个 severity。

    旧口径 ``gap = rate − 时间进度`` 会随月内日期机械升级；锚绝对缺口后
    「今天该跟进谁」在月内保持稳定，不会到第 20 天自动全员升级。
    """

    TARGET = 220.0  # 22 个工作日 → 所需日均 10

    def _level(self, done, elapsed, remaining):
        return severity(
            done=done,
            shortfall=shortfall(self.TARGET, done),
            required_daily=required_daily(
                self.TARGET, total_workdays(_CALENDAR_ROWS)
            ),
            remaining_workdays=remaining,
            elapsed_workdays=elapsed,
        )

    def test_p1_person_stays_p1_from_month_start_to_mid_month(self):
        """缺口 120 > 月初阈值 100、月中阈值 55 → 全程 p1。"""
        early = self._level(done=100.0, elapsed=2, remaining=20)
        mid = self._level(done=100.0, elapsed=11, remaining=11)

        self.assertEqual("p1", early)
        self.assertEqual(early, mid)

    def test_p2_person_stays_p2_from_month_start_to_mid_month(self):
        """缺口 50 < 月中阈值 55 → 全程 p2，不会因日期推移自动升级。"""
        early = self._level(done=170.0, elapsed=2, remaining=20)
        mid = self._level(done=170.0, elapsed=11, remaining=11)

        self.assertEqual("p2", early)
        self.assertEqual(early, mid)

    def test_ok_person_never_degrades_over_the_month(self):
        self.assertEqual(
            "ok", self._level(done=230.0, elapsed=2, remaining=20)
        )
        self.assertEqual(
            "ok", self._level(done=230.0, elapsed=20, remaining=2)
        )


#: 双端共用的 golden 夹具（Python 与前端 TS 同一份）。
_GOLDEN_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests" / "fixtures" / "derived_golden.json"
)


def _calendar_rows(entries):
    """夹具日历 → derived 期望的 ``dim_calendar`` 行形态。

    夹具里用中性键名（``date`` / ``is_workday``）与纯日期字符串，两端各自
    适配自己的形状；这里转成 Python 侧的行形态。
    """
    rows = []
    for entry in entries:
        if isinstance(entry, dict):
            raw = entry.get("date")
            rows.append(
                {
                    "business_date": None if raw is None else date.fromisoformat(raw),
                    "is_workday": entry.get("is_workday"),
                }
            )
        else:
            rows.append(date.fromisoformat(entry))
    return rows


class GoldenFixtureTests(unittest.TestCase):
    """用同一份 JSON 夹具跑 Python 实现。

    期望值手写（见夹具 meta），不由实现生成 —— 否则两端一起错还会互相印证。
    改口径时先看每条的 ``source`` 字段定位 CubeSchema.md 的对应条款。
    """

    @classmethod
    def setUpClass(cls):
        with _GOLDEN_PATH.open(encoding="utf-8") as handle:
            cls.fixture = json.load(handle)

    # -- helpers ------------------------------------------------------------
    def _assert_scalar(self, label, actual, expected):
        if expected is None:
            self.assertIsNone(actual, f"{label}: 期望 None，实际 {actual!r}")
        elif isinstance(expected, (int, float)):
            self.assertAlmostEqual(
                expected, actual, delta=1e-9,
                msg=f"{label}: 期望 {expected}，实际 {actual}",
            )
        else:
            self.assertEqual(expected, actual, f"{label}")

    def _assert_mom(self, actual, expected):
        if expected is None:
            self.assertIsNone(actual, f"mom: 期望 None，实际 {actual!r}")
            return
        self.assertEqual(expected["direction"], actual["direction"], "mom.direction")
        self.assertAlmostEqual(
            expected["value"], actual["value"], delta=1e-9, msg="mom.value"
        )

    # -- cases --------------------------------------------------------------
    def test_derived_cases_match_the_frozen_golden_values(self):
        for case in self.fixture["cases"]:
            with self.subTest(case=case["name"]):
                inputs, expect = case["inputs"], case["expect"]
                target = inputs["target"]
                done = inputs["done"]
                required = derived.required_daily(
                    target, inputs["total_workdays"]
                )
                gap = derived.shortfall(target, done)

                self._assert_scalar("shortfall", gap, expect["shortfall"])
                self._assert_scalar(
                    "rate", derived.rate(done, target), expect["rate"]
                )
                self._assert_scalar(
                    "required_daily", required, expect["required_daily"]
                )
                if "p1_threshold" in expect:
                    self._assert_scalar(
                        "p1_threshold",
                        derived.p1_threshold(
                            required, inputs["remaining_workdays"]
                        ),
                        expect["p1_threshold"],
                    )
                self.assertEqual(
                    expect["severity"],
                    derived.severity(
                        done, gap, required,
                        inputs["remaining_workdays"],
                        inputs["elapsed_workdays"],
                    ),
                    "severity",
                )
                if "mom" in expect:
                    self._assert_mom(
                        derived.mom(inputs["current"], inputs["prev"]),
                        expect["mom"],
                    )

    def test_calendar_cases_match_the_frozen_golden_values(self):
        for case in self.fixture["calendar_cases"]:
            with self.subTest(case=case["name"]):
                rows = _calendar_rows(case["inputs"]["calendar"])
                as_of = date.fromisoformat(case["inputs"]["as_of"])

                self.assertEqual(
                    case["expect"]["total_workdays"],
                    derived.total_workdays(rows), "total_workdays",
                )
                self.assertEqual(
                    case["expect"]["elapsed_workdays"],
                    derived.elapsed_workdays(rows, as_of), "elapsed_workdays",
                )
                self.assertEqual(
                    case["expect"]["remaining_workdays"],
                    derived.remaining_workdays(rows, as_of), "remaining_workdays",
                )

    def test_row_cases_match_the_frozen_golden_values(self):
        """行级组装用例：钉「无事实行 ≠ 挂零」这道护栏。

        ``cases`` 钉的是纯函数，这里钉的是组装层（``queries.shortfall_rows``）
        的处理方式：``has_fact=false`` 必须整行置 ``None`` 且不进 p0，
        ``has_fact=true`` 的真挂零照旧告警。
        """
        from common.bi_web import queries  # 延迟导入：保持本模块 import 期零 DB 依赖

        for case in self.fixture["row_cases"]:
            with self.subTest(case=case["name"]):
                row = queries.shortfall_rows(
                    [case["inputs"]["fact"]], **case["inputs"]["calendar"]
                )[0]
                for key, expected in case["expect"].items():
                    actual = row[key]
                    if expected is None:
                        self.assertIsNone(actual, f"{key}: 期望 None，实际 {actual!r}")
                    elif isinstance(expected, float):
                        self.assertAlmostEqual(
                            expected, actual, delta=1e-9,
                            msg=f"{key}: 期望 {expected}，实际 {actual!r}",
                        )
                    else:
                        self.assertEqual(expected, actual, key)

    def test_every_case_has_a_unique_name_and_a_contract_source(self):
        cases = (
            self.fixture["cases"]
            + self.fixture["calendar_cases"]
            + self.fixture["row_cases"]
        )
        names = [case["name"] for case in cases]

        self.assertEqual(len(names), len(set(names)), "用例名必须唯一")
        for case in cases:
            with self.subTest(case=case["name"]):
                self.assertTrue(
                    str(case.get("source", "")).startswith("§"),
                    "source 必须标注 CubeSchema.md 条款，便于口径变更时定位",
                )


if __name__ == "__main__":
    unittest.main()
