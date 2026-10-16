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
    RULE_FALLBACK_SOURCE,
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
    synthesize_fallback_months,
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

    def test_yonyou_tplus_missing_credentials_fail_loudly(self):
        src = calendar_source(self._cfg(source=SOURCE_YONYOU_TP, yonyouTplus={"baseUrl": ""}))
        self.assertIsInstance(src, YonyouTplusSchedule)
        self.assertEqual(src.name, SOURCE_YONYOU_TP)
        with self.assertRaises(CalendarSourceUnavailable) as ctx:
            src.load(2026, 9)
        self.assertIn("凭据未就绪", str(ctx.exception))

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
        """未配置 yonyouTplus 段时明确失败（缺全部凭据），不静默降级。"""
        with self.assertRaises(CalendarSourceUnavailable) as ctx:
            Calendar.from_config({"month": 9, "source": "yonyou_tplus", "restDays": []})
        self.assertIn("凭据未就绪", str(ctx.exception))

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


class TestYonyouTplusScheduleFramework(unittest.TestCase):
    """T+ 排班源框架：凭据校验 / token 复用 / 按月缓存 / 失败回退+告警。

    endpoint、签名、报文解析为待实测钉死点，这里只钉结构行为。
    """

    TP_CFG = {"baseUrl": "https://tplus.example.com", "appKey": "ak",
              "appSecret": "sk", "tenantId": "t1", "schemeId": "sch1"}

    def _transport(self, routes, calls):
        def transport(method, url, headers=None, payload=None, timeout=10):
            calls.append((method, url, payload))
            for suffix, response in routes.items():
                if url.endswith(suffix):
                    if isinstance(response, Exception):
                        raise response
                    return response
            raise AssertionError(f"未 mock 的 URL: {url}")
        return transport

    def _source(self, routes, *, fallback_cfg=None, alarms=None):
        calls = []
        src = YonyouTplusSchedule(
            self.TP_CFG,
            fallback_cfg=fallback_cfg,
            transport=self._transport(routes, calls),
            alarm=(alarms.append if alarms is not None else None),
        )
        return src, calls

    def test_load_success_and_caches_by_month(self):
        src, calls = self._source({
            "/auth/getAppToken": {"access_token": "tok-1"},
            "/tplus/api/v1/schedule/query": {"days": [
                {"date": "2026-09-06", "isRest": True},
                {"date": "2026-09-05", "isRest": False},
                {"date": "2026-10-01", "isRest": True},  # 跨月忽略
            ]},
        })
        self.assertEqual(src.load(2026, 9), [6])
        self.assertEqual(src.load(2026, 9), [6])  # 第二次走缓存
        schedule_calls = [c for c in calls if c[1].endswith("/schedule/query")]
        self.assertEqual(len(schedule_calls), 1)

    def test_token_reused_across_months(self):
        src, calls = self._source({
            "/auth/getAppToken": {"access_token": "tok-1"},
            "/tplus/api/v1/schedule/query": {"days": []},
        })
        src.load(2026, 9)
        src.load(2026, 10)
        token_calls = [c for c in calls if c[1].endswith("/auth/getAppToken")]
        self.assertEqual(len(token_calls), 1)

    def test_fetch_failure_falls_back_to_local_with_alarm(self):
        alarms = []
        src, _ = self._source(
            {"/auth/getAppToken": ConnectionError("T+ 不可达")},
            fallback_cfg={"restDays": [6, 13, 19]},
            alarms=alarms,
        )
        self.assertEqual(src.load(2026, 9), [6, 13, 19])
        self.assertEqual(len(alarms), 1)
        self.assertIn("回退 local restDays", alarms[0])

    def test_fetch_failure_without_fallback_fails_loudly(self):
        src, _ = self._source({"/auth/getAppToken": ConnectionError("T+ 不可达")})
        with self.assertRaises(CalendarSourceUnavailable) as ctx:
            src.load(2026, 9)
        self.assertIn("无 local restDays 可回退", str(ctx.exception))

    def test_partial_credentials_report_missing_keys(self):
        src = YonyouTplusSchedule({"baseUrl": "https://x", "appKey": "ak"})
        with self.assertRaises(CalendarSourceUnavailable) as ctx:
            src.load(2026, 9)
        message = str(ctx.exception)
        self.assertIn("appSecret", message)
        self.assertIn("tenantId", message)
        self.assertIn("schemeId", message)
        self.assertNotIn("baseUrl", message.split("缺", 1)[1])

    def test_token_response_missing_token_fails_loudly(self):
        src, _ = self._source({"/auth/getAppToken": {"unexpected": True}})
        with self.assertRaises(CalendarSourceUnavailable) as ctx:
            src.load(2026, 9)
        self.assertIn("access_token", str(ctx.exception))


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
    """纯播报口径（2026-10-16 裁决）：种子月基线 rest 恒为空。"""

    def test_shipped_seed_rest_days_always_empty(self):
        """每天都是播报日；种子 rule 数组仅作历史档案，不参与推导。"""
        months = load_calendar_seed(SEED_PATH)
        by_month = {(year, month): (rest_days, source)
                    for year, month, rest_days, source in months}
        rest_days, source = by_month[(2026, 9)]
        self.assertEqual(rest_days, [])
        self.assertEqual(source, SOURCE_LOCAL)

    def test_shipped_seed_covers_2025_01_through_2026_12(self):
        months = load_calendar_seed(SEED_PATH, fallback=False)
        self.assertEqual(len(months), 24)
        self.assertEqual(
            [(year, month) for year, month, _, _ in months],
            [(year, month)
             for year in (2025, 2026) for month in range(1, 13)],
        )
        for _, _, _, source in months:
            self.assertEqual(source, SOURCE_LOCAL)

    def test_shipped_seed_pins_every_month_rest_days(self):
        """逐月钉死：纯播报口径下所有种子月 rest_days 均为空列表。

        休息日只来自 DB dim_calendar_override 人工裁决（如 2026-10-31
        大休），不在种子承载。
        """
        months = load_calendar_seed(SEED_PATH, fallback=False)
        self.assertEqual(
            {(year, month): rest_days for year, month, rest_days, _ in months},
            {(year, month): []
             for year in (2025, 2026) for month in range(1, 13)},
        )

    def test_shipped_seed_never_declares_a_derived_rest_days_list(self):
        """restDays 必须推导、不得手工列举，否则会漂移。"""
        document = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        for month_document in document["months"]:
            self.assertNotIn("restDays", month_document)


class TestLoadCalendarSeed(unittest.TestCase):
    def test_missing_rule_entries_mean_all_broadcast(self):
        path = _SeedFile(self, {
            "version": 1,
            "months": [{"year": 2026, "month": 9}],
        }).path
        _, _, rest_days, _ = load_calendar_seed(path)[0]
        self.assertEqual(rest_days, [])

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
            [(year, month) for year, month, _, _
             in load_calendar_seed(path, fallback=False)],
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


class TestRuleFallbackMonths(unittest.TestCase):
    """规则兜底月（2026-10-10 三层防线 A 层）：近端月份 dim_calendar 永不空。"""

    def test_synthesize_window_skips_covered(self):
        rows = synthesize_fallback_months(
            {(2026, 10)}, today=date(2026, 10, 10))
        self.assertEqual(
            [(2026, 8), (2026, 9), (2026, 11), (2026, 12), (2027, 1)],
            [(year, month) for year, month, _, _ in rows],
        )
        self.assertTrue(
            all(source == RULE_FALLBACK_SOURCE for _, _, _, source in rows))

    def test_fallback_rest_is_all_broadcast(self):
        """纯播报口径：兜底月 rest=∅（不再按周日休+大休合成）。"""
        rows = synthesize_fallback_months(set(), today=date(2026, 10, 10))
        self.assertTrue(rows)
        for _, _, rest_days, _ in rows:
            self.assertEqual([], rest_days)

    def test_load_seed_appends_fallback_by_default(self):
        path = _SeedFile(self, {
            "version": 1,
            "months": [{"year": 2026, "month": 10}],
        }).path
        rows = load_calendar_seed(path, today=date(2026, 10, 10))
        seeds = [r for r in rows if r[3] == SOURCE_LOCAL]
        fallback = [r for r in rows if r[3] == RULE_FALLBACK_SOURCE]
        self.assertEqual([(2026, 10)], [(y, m) for y, m, _, _ in seeds])
        self.assertIn((2027, 1), [(y, m) for y, m, _, _ in fallback])
        # 种子覆盖月不重复出兜底行
        self.assertNotIn((2026, 10), [(y, m) for y, m, _, _ in fallback])

    def test_load_seed_fallback_disabled(self):
        path = _SeedFile(self, {
            "version": 1,
            "months": [{"year": 2026, "month": 9}],
        }).path
        rows = load_calendar_seed(path, fallback=False,
                                  today=date(2026, 10, 10))
        self.assertEqual([(2026, 9)], [(y, m) for y, m, _, _ in rows])


class TestLoadCalendarSeedValidation(unittest.TestCase):
    def test_rejects_missing_year_or_month(self):
        path = _SeedFile(self, {"version": 1, "months": [{"month": 9}]}).path
        with self.assertRaises(CalendarError):
            load_calendar_seed(path)

    def test_rule_arrays_are_archive_only(self):
        """纯播报口径后 rule 数组不再参与推导（历史档案），非法日号不再抛错。"""
        path = _SeedFile(self, {
            "version": 1,
            "months": [{"year": 2026, "month": 9, "holidays": [31],
                        "bigRestSaturdays": [16]}],
        }).path
        _, _, rest_days, _ = load_calendar_seed(path)[0]
        self.assertEqual([], rest_days)

    def test_rejects_a_missing_file(self):
        with self.assertRaises(CalendarError):
            load_calendar_seed(REPO_ROOT / "docker" / "integration" / "nope.json")

    def test_rejects_malformed_json(self):
        path = _SeedFile(self, "{not json").path
        with self.assertRaises(CalendarError):
            load_calendar_seed(path)


if __name__ == "__main__":
    unittest.main()
