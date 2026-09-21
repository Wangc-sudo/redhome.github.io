import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.calendar_utils import (  # noqa: E402
    SOURCE_LOCAL,
    SOURCE_YONYOU_TP,
    Calendar,
    CalendarError,
    CalendarSourceUnavailable,
    LocalRestDays,
    YonyouTplusSchedule,
    calendar_source,
    check_rule_matches_rest_days,
    generate_rest_days,
    load_calendar_seed,
    month_days,
)

EXAMPLE_CFG = REPO_ROOT / "数字化" / "钉钉" / "杭州日报机器人" / "config.example.json"
SEED_PATH = REPO_ROOT / "docker" / "integration" / "calendar.seed.json"


class TestGenerateRestDays(unittest.TestCase):
    """「大小休 + 法定节假日 + 调休」规则推导。"""

    def test_2026_09_reproduces_current_config(self):
        """自证：与现行 config 的 restDays 逐位一致。"""
        self.assertEqual(
            generate_rest_days(
                2026, 9,
                big_rest_saturdays=[19],
                holidays=[25, 26, 27],
                makeup_workdays=[20],
            ),
            [6, 13, 19, 25, 26, 27],
        )

    def test_baseline_is_every_sunday(self):
        """无大休周/节假日/调休时，基准即每周日休（小休周）。"""
        self.assertEqual(generate_rest_days(2026, 9), [6, 13, 20, 27])

    def test_big_rest_week_adds_saturday(self):
        self.assertEqual(
            generate_rest_days(2026, 9, big_rest_saturdays=[26]),
            [6, 13, 20, 26, 27],
        )

    def test_holiday_outside_baseline_is_added(self):
        self.assertEqual(
            generate_rest_days(2026, 9, holidays=[25]),
            [6, 13, 20, 25, 27],
        )

    def test_makeup_workday_removes_rest(self):
        self.assertEqual(
            generate_rest_days(2026, 9, makeup_workdays=[20]),
            [6, 13, 27],
        )

    def test_handles_31_day_month(self):
        """10 月有 31 天，不得按 1~30 截断。"""
        self.assertEqual(generate_rest_days(2026, 10), [4, 11, 18, 25])
        self.assertEqual(
            generate_rest_days(2026, 10, holidays=[31]),
            [4, 11, 18, 25, 31],
        )

    def test_result_is_sorted_and_deduplicated(self):
        # 基准 {6,13,20,27} + 节假日 {6,25} - 调休 {13} => {6,20,25,27}
        self.assertEqual(
            generate_rest_days(2026, 9, holidays=[6, 6, 25], makeup_workdays=[13, 13]),
            [6, 20, 25, 27],
        )

    def test_big_rest_saturday_must_be_saturday(self):
        with self.assertRaises(CalendarError) as ctx:
            generate_rest_days(2026, 9, big_rest_saturdays=[20])
        self.assertIn("不是周六", str(ctx.exception))

    def test_out_of_range_day_raises(self):
        with self.assertRaises(CalendarError) as ctx:
            generate_rest_days(2026, 9, holidays=[31])
        self.assertIn("超出", str(ctx.exception))

    def test_accepts_string_days(self):
        """config JSON 里数字即 int，但容忍字符串以免上游传参类型不一。"""
        self.assertEqual(generate_rest_days(2026, 9, holidays=["25"]), [6, 13, 20, 25, 27])


class TestCheckRuleMatches(unittest.TestCase):
    def _cfg(self, rest_days, rule=None):
        cfg = {"month": 9, "restDays": rest_days}
        if rule is not None:
            cfg["rule"] = rule
        return cfg

    def test_no_rule_returns_empty(self):
        self.assertEqual(check_rule_matches_rest_days(self._cfg([6, 13])), [])

    def test_matching_rule_returns_empty(self):
        cfg = self._cfg(
            [6, 13, 19, 25, 26, 27],
            {"year": 2026, "bigRestSaturdays": [19], "holidays": [25, 26, 27],
             "makeupWorkdays": [20]},
        )
        self.assertEqual(check_rule_matches_rest_days(cfg), [])

    def test_drift_reports_both_sides(self):
        cfg = self._cfg(
            [6, 13, 20, 27],
            {"year": 2026, "bigRestSaturdays": [19], "holidays": [25, 26, 27],
             "makeupWorkdays": [20]},
        )
        issues = check_rule_matches_rest_days(cfg)
        self.assertEqual(len(issues), 1)
        self.assertIn("6, 13, 19, 25, 26, 27", issues[0])
        self.assertIn("6, 13, 20, 27", issues[0])


class TestCalendarSource(unittest.TestCase):
    def _cfg(self, **kw):
        cfg = {"month": 9, "restDays": [6, 13, 19, 25, 26, 27]}
        cfg.update(kw)
        return cfg

    def test_default_source_is_local(self):
        src = calendar_source(self._cfg())
        self.assertIsInstance(src, LocalRestDays)
        self.assertEqual(src.name, SOURCE_LOCAL)
        self.assertEqual(src.load(2026, 9), [6, 13, 19, 25, 26, 27])

    def test_explicit_local_source(self):
        self.assertIsInstance(calendar_source(self._cfg(source=SOURCE_LOCAL)), LocalRestDays)

    def test_yonyou_tplus_is_placeholder_and_fails_loudly(self):
        src = calendar_source(self._cfg(source=SOURCE_YONYOU_TP, yonyouTplus={"baseUrl": ""}))
        self.assertIsInstance(src, YonyouTplusSchedule)
        self.assertEqual(src.name, SOURCE_YONYOU_TP)
        with self.assertRaises(CalendarSourceUnavailable) as ctx:
            src.load(2026, 9)
        self.assertIn("用友 T+", str(ctx.exception))

    def test_unknown_source_raises(self):
        with self.assertRaises(CalendarError) as ctx:
            calendar_source(self._cfg(source="dingtalk_schedule"))
        self.assertIn("未知的 calendar.source", str(ctx.exception))


class TestCalendarFromConfig(unittest.TestCase):
    def test_local_source_builds_calendar(self):
        cal = Calendar.from_config(
            {"month": 9, "source": "local", "restDays": [6, 13, 19, 25, 26, 27]},
            year=2026,
        )
        self.assertEqual(cal.total, 24)
        self.assertTrue(cal.is_rest(6))
        self.assertFalse(cal.is_rest(5))
        self.assertEqual(cal.workdays()[0], 1)

    def test_yonyou_tplus_source_fails_loudly(self):
        with self.assertRaises(CalendarSourceUnavailable):
            Calendar.from_config({"month": 9, "source": "yonyou_tplus", "restDays": []})

    def test_example_config_rule_is_consistent(self):
        """模板里的 rule 与 restDays 必须自洽，防止换月时漏改一半。"""
        cfg = json.loads(EXAMPLE_CFG.read_text(encoding="utf-8"))["calendar"]
        self.assertEqual(check_rule_matches_rest_days(cfg), [])

    def test_example_config_keeps_local_source(self):
        """用友 T+ 未接入前必须保持 local，否则机器人起不来。"""
        cfg = json.loads(EXAMPLE_CFG.read_text(encoding="utf-8"))["calendar"]
        self.assertEqual(cfg.get("source"), SOURCE_LOCAL)
        cal = Calendar.from_config(cfg, now=datetime(2026, 9, 11))
        self.assertEqual(cal.total, 24)


class TestMonthDays(unittest.TestCase):
    """`dim_calendar` 逐日落库需要真实月份长度，不能用 Calendar 的 1~30 口径。"""

    def test_covers_every_day_of_a_30_day_month(self):
        days = month_days(2026, 9)
        self.assertEqual(len(days), 30)
        self.assertEqual(days[0], date(2026, 9, 1))
        self.assertEqual(days[-1], date(2026, 9, 30))

    def test_covers_a_31_day_month(self):
        self.assertEqual(len(month_days(2026, 10)), 31)

    def test_covers_a_leap_february(self):
        self.assertEqual(len(month_days(2024, 2)), 29)

    def test_covers_a_common_february(self):
        self.assertEqual(len(month_days(2026, 2)), 28)


class _SeedFile:
    """写一份临时种子并登记清理。"""

    def __init__(self, test_case, document):
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        with handle:
            handle.write(
                document if isinstance(document, str)
                else json.dumps(document, ensure_ascii=False)
            )
        self.path = handle.name
        test_case.addCleanup(os.unlink, self.path)


class TestShippedCalendarSeed(unittest.TestCase):
    """真正发版的种子必须能复现 spec §10 的规则。"""

    def test_shipped_seed_reproduces_september_2026_rest_days(self):
        """回归锚点：扩年后 2026-09 条目不动力（24 工作日、9/20 调休上班）。"""
        months = load_calendar_seed(SEED_PATH)
        by_month = {(year, month): (rest_days, source)
                    for year, month, rest_days, source in months}
        rest_days, source = by_month[(2026, 9)]
        self.assertEqual(rest_days, [6, 13, 19, 25, 26, 27])
        self.assertEqual(source, SOURCE_LOCAL)

    def test_shipped_seed_covers_2025_01_through_2026_12(self):
        months = load_calendar_seed(SEED_PATH)
        self.assertEqual(len(months), 24)
        self.assertEqual(
            [(year, month) for year, month, _, _ in months],
            [(year, month)
             for year in (2025, 2026) for month in range(1, 13)],
        )
        for _, _, _, source in months:
            self.assertEqual(source, SOURCE_LOCAL)

    def test_shipped_seed_pins_every_month_rest_days(self):
        """逐月钉死 rest_days 推导结果（种子 diff 即审计轨迹，改动必撞此表）。

        口径备忘：2025 年各月周六一律休（main 2026-09-18 裁定，宁漏报不
        误报红，待 HR 回补），调休周六 2/8、10/11 已显式恢复；2026 年
        维持锚点同口径（每月第 3 个周六大休）。
        """
        expected = {
            (2025, 1): [1, 4, 5, 11, 12, 18, 19, 25, 28, 29, 30, 31],
            (2025, 2): [1, 2, 3, 4, 9, 15, 16, 22, 23],
            (2025, 3): [1, 2, 8, 9, 15, 16, 22, 23, 29, 30],
            (2025, 4): [4, 5, 6, 12, 13, 19, 20, 26],
            (2025, 5): [1, 2, 3, 4, 5, 10, 11, 17, 18, 24, 25, 31],
            (2025, 6): [1, 2, 7, 8, 14, 15, 21, 22, 28, 29],
            (2025, 7): [5, 6, 12, 13, 19, 20, 26, 27],
            (2025, 8): [2, 3, 9, 10, 16, 17, 23, 24, 30, 31],
            (2025, 9): [6, 7, 13, 14, 20, 21, 27],
            (2025, 10): [1, 2, 3, 4, 5, 6, 7, 8, 12, 18, 19, 25, 26],
            (2025, 11): [1, 2, 8, 9, 15, 16, 22, 23, 29, 30],
            (2025, 12): [6, 7, 13, 14, 20, 21, 27, 28],
            (2026, 1): [1, 2, 3, 11, 17, 18, 25],
            (2026, 2): [1, 8, 15, 16, 17, 18, 19, 20, 21, 22, 23],
            (2026, 3): [1, 8, 15, 21, 22, 29],
            (2026, 4): [4, 5, 6, 12, 18, 19, 26],
            (2026, 5): [1, 2, 3, 4, 5, 10, 16, 17, 24, 31],
            (2026, 6): [7, 14, 19, 20, 21, 28],
            (2026, 7): [5, 12, 18, 19, 26],
            (2026, 8): [2, 9, 15, 16, 23, 30],
            (2026, 9): [6, 13, 19, 25, 26, 27],
            (2026, 10): [1, 2, 3, 4, 5, 6, 7, 11, 17, 18, 25],
            (2026, 11): [1, 8, 15, 21, 22, 29],
            (2026, 12): [6, 13, 19, 20, 27],
        }
        months = load_calendar_seed(SEED_PATH)
        self.assertEqual(
            {(year, month): rest_days for year, month, rest_days, _ in months},
            expected,
        )

    def test_shipped_seed_never_declares_a_derived_rest_days_list(self):
        """restDays 必须推导、不得手工列举，否则会漂移。"""
        document = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        for month_document in document["months"]:
            self.assertNotIn("restDays", month_document)


class TestLoadCalendarSeed(unittest.TestCase):
    def test_missing_rule_entries_fall_back_to_sundays_only(self):
        path = _SeedFile(self, {
            "version": 1,
            "months": [{"year": 2026, "month": 9}],
        }).path
        _, _, rest_days, _ = load_calendar_seed(path)[0]
        self.assertEqual(rest_days, [6, 13, 20, 27])

    def test_source_defaults_to_local(self):
        path = _SeedFile(self, {
            "version": 1,
            "months": [{"year": 2026, "month": 9}],
        }).path
        self.assertEqual(load_calendar_seed(path)[0][3], SOURCE_LOCAL)

    def test_multiple_months_keep_seed_order(self):
        path = _SeedFile(self, {
            "version": 1,
            "months": [
                {"year": 2026, "month": 10, "holidays": [1]},
                {"year": 2026, "month": 9},
            ],
        }).path
        self.assertEqual(
            [(year, month) for year, month, _, _ in load_calendar_seed(path)],
            [(2026, 10), (2026, 9)],
        )

    def test_rejects_wrong_version(self):
        path = _SeedFile(
            self, {"version": 2, "months": [{"year": 2026, "month": 9}]}
        ).path
        with self.assertRaises(CalendarError):
            load_calendar_seed(path)

    def test_rejects_yonyou_tplus_source(self):
        """未接入的来源必须显式失败，绝不静默降级。"""
        path = _SeedFile(self, {
            "version": 1,
            "source": "yonyou_tplus",
            "months": [{"year": 2026, "month": 9}],
        }).path
        with self.assertRaises(CalendarSourceUnavailable):
            load_calendar_seed(path)

    def test_rejects_unknown_source(self):
        path = _SeedFile(self, {
            "version": 1,
            "source": "guesswork",
            "months": [{"year": 2026, "month": 9}],
        }).path
        with self.assertRaises(CalendarError):
            load_calendar_seed(path)

    def test_rejects_empty_month_list(self):
        path = _SeedFile(self, {"version": 1, "months": []}).path
        with self.assertRaises(CalendarError):
            load_calendar_seed(path)

    def test_rejects_duplicate_month(self):
        path = _SeedFile(self, {
            "version": 1,
            "months": [{"year": 2026, "month": 9}, {"year": 2026, "month": 9}],
        }).path
        with self.assertRaises(CalendarError):
            load_calendar_seed(path)

    def test_rejects_month_out_of_range(self):
        path = _SeedFile(
            self, {"version": 1, "months": [{"year": 2026, "month": 13}]}
        ).path
        with self.assertRaises(CalendarError):
            load_calendar_seed(path)

    def test_rejects_non_object_month(self):
        path = _SeedFile(self, {"version": 1, "months": ["2026-09"]}).path
        with self.assertRaises(CalendarError):
            load_calendar_seed(path)

    def test_rejects_missing_year_or_month(self):
        path = _SeedFile(self, {"version": 1, "months": [{"month": 9}]}).path
        with self.assertRaises(CalendarError):
            load_calendar_seed(path)

    def test_rejects_a_day_outside_the_month(self):
        path = _SeedFile(self, {
            "version": 1,
            "months": [{"year": 2026, "month": 9, "holidays": [31]}],
        }).path
        with self.assertRaises(CalendarError):
            load_calendar_seed(path)

    def test_rejects_a_non_saturday_big_rest_day(self):
        path = _SeedFile(self, {
            "version": 1,
            "months": [{"year": 2026, "month": 9, "bigRestSaturdays": [16]}],
        }).path
        with self.assertRaises(CalendarError):
            load_calendar_seed(path)

    def test_rejects_a_missing_file(self):
        with self.assertRaises(CalendarError):
            load_calendar_seed(REPO_ROOT / "docker" / "integration" / "nope.json")

    def test_rejects_malformed_json(self):
        path = _SeedFile(self, "{not json").path
        with self.assertRaises(CalendarError):
            load_calendar_seed(path)


if __name__ == "__main__":
    unittest.main()
