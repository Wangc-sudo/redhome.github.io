# -*- coding: utf-8 -*-
"""巡检告警通用通道（C5）离线单测：阈值筛选 / 冷却后缀 / outbox 入队。"""

import unittest
from datetime import date, datetime

from common.daily_robot import inspection_alert
from common.daily_robot.inspection_alert import (
    ALERT_KIND,
    ALERT_REGION,
    Finding,
    build_message,
    cooldown_suffix,
    dedupe_suffix,
    emit,
    normalize_cooldown_hours,
    normalize_threshold,
    resolve_params,
    select,
    severity_rank,
)


class _FakeOutbox:
    """记录入队调用的假 outbox（dedupe_hit=True 模拟唯一键命中）。"""

    def __init__(self, dedupe_hit=False):
        self.calls = []
        self._dedupe_hit = dedupe_hit

    def enqueue(self, **kwargs):
        self.calls.append(kwargs)
        return not self._dedupe_hit


_BDAY = date(2026, 10, 14)


class SeverityTests(unittest.TestCase):
    def test_rank_monotonic(self):
        self.assertLess(severity_rank("info"), severity_rank("warning"))
        self.assertLess(severity_rank("warning"), severity_rank("critical"))

    def test_rank_unknown_is_minus_one(self):
        self.assertEqual(-1, severity_rank("fatal"))
        self.assertEqual(-1, severity_rank(None))
        self.assertEqual(-1, severity_rank(3))

    def test_rank_is_case_insensitive(self):
        self.assertEqual(severity_rank("WARNING"), severity_rank("warning"))


class NormalizeTests(unittest.TestCase):
    def test_threshold_falls_back_on_invalid(self):
        self.assertEqual("warning", normalize_threshold("bogus"))
        self.assertEqual("critical", normalize_threshold(" CRITICAL "))
        self.assertEqual("warning", normalize_threshold(None))

    def test_cooldown_rejects_bool_and_non_positive(self):
        self.assertEqual(24, normalize_cooldown_hours(True))
        self.assertEqual(24, normalize_cooldown_hours(0))
        self.assertEqual(24, normalize_cooldown_hours(-3))
        self.assertEqual(24, normalize_cooldown_hours("nope"))
        self.assertEqual(24, normalize_cooldown_hours(None))

    def test_cooldown_accepts_numbers_and_numeric_strings(self):
        self.assertEqual(6, normalize_cooldown_hours(6))
        self.assertEqual(6, normalize_cooldown_hours("6"))
        self.assertEqual(12.5, normalize_cooldown_hours(12.5))

    def test_resolve_params_uses_defaults_when_missing(self):
        self.assertEqual(("warning", 24), resolve_params({}))
        self.assertEqual(("warning", 24), resolve_params(None))
        self.assertEqual(("critical", 6),
                         resolve_params({"alert_threshold": "critical",
                                         "alert_cooldown_hours": 6}))


class SelectTests(unittest.TestCase):
    def test_threshold_filters_lower_severities(self):
        findings = (
            Finding("a", "info", "x"),
            Finding("b", "warning", "y"),
            Finding("c", "critical", "z"),
        )
        picked = select(findings, threshold="warning")
        self.assertEqual(("b", "c"), tuple(f.code for f in picked))

    def test_select_dedupes_by_code_preserving_order(self):
        findings = (
            Finding("a", "critical", "first"),
            Finding("a", "critical", "second"),
            Finding("b", "critical", "third"),
        )
        picked = select(findings, threshold="info")
        self.assertEqual(("a", "b"), tuple(f.code for f in picked))
        self.assertEqual("first", picked[0].text)

    def test_select_ignores_non_finding_and_unknown_severity(self):
        picked = select(("not-a-finding", Finding("x", "fatal", "y")),
                        threshold="info")
        self.assertEqual((), tuple(picked))


class CooldownSuffixTests(unittest.TestCase):
    def test_ge_24_or_missing_yields_none(self):
        self.assertIsNone(cooldown_suffix(datetime(2026, 10, 14, 9, 30), 24))
        self.assertIsNone(cooldown_suffix(datetime(2026, 10, 14, 9, 30), 48))
        self.assertIsNone(cooldown_suffix(datetime(2026, 10, 14, 9, 30), None))

    def test_sub_day_windows_bucket_by_clock(self):
        early = cooldown_suffix(datetime(2026, 10, 14, 9, 30), 6)
        late = cooldown_suffix(datetime(2026, 10, 14, 15, 30), 6)
        self.assertEqual("c6h1", early)   # 570 // 360 = 1
        self.assertEqual("c6h2", late)    # 930 // 360 = 2
        self.assertNotEqual(early, late)

    def test_same_window_is_stable(self):
        a = cooldown_suffix(datetime(2026, 10, 14, 9, 0), 6)
        b = cooldown_suffix(datetime(2026, 10, 14, 9, 59), 6)
        self.assertEqual(a, b)


class DedupeSuffixTests(unittest.TestCase):
    def test_stamps_service_id_to_avoid_cross_line_collision(self):
        now = datetime(2026, 10, 14, 9, 30)
        ch = dedupe_suffix("channel-missing-check", now, 24)
        dash = dedupe_suffix("calendar-watch", now, 24)
        self.assertEqual("channel-missing-check:daily", ch)
        self.assertEqual("calendar-watch:daily", dash)
        self.assertNotEqual(ch, dash)

    def test_override_takes_priority(self):
        now = datetime(2026, 10, 14, 9, 30)
        got = dedupe_suffix("freshness-check", now, 24, override="manual-1-2")
        self.assertEqual("freshness-check:manual-1-2", got)

    def test_sub_day_window_is_embeded(self):
        now = datetime(2026, 10, 14, 9, 30)
        got = dedupe_suffix("freshness-check", now, 6)
        self.assertEqual("freshness-check:c6h1", got)


class BuildMessageTests(unittest.TestCase):
    def test_lists_severity_and_code(self):
        title, body = build_message(
            (Finding("ch", "critical", "渠道日销缺行"),),
            service_id="freshness-check",
            business_date=_BDAY,
            threshold="warning",
            cooldown_hours=24,
        )
        self.assertEqual("巡检告警 · freshness-check", title)
        self.assertIn("**CRITICAL** [ch] 渠道日销缺行", body)
        self.assertIn(_BDAY.isoformat(), body)


class EmitTests(unittest.TestCase):
    def test_no_findings_is_silent(self):
        outbox = _FakeOutbox()
        self.assertEqual("no_findings", emit(
            outbox, service_id="freshness-check", findings=(),
            business_date=_BDAY, now=datetime(2026, 10, 14, 11, 0)))
        self.assertEqual([], outbox.calls)

    def test_below_threshold_is_silent(self):
        outbox = _FakeOutbox()
        self.assertEqual("below_threshold", emit(
            outbox, service_id="freshness-check",
            findings=(Finding("r", "warning", "x"),),
            business_date=_BDAY, now=datetime(2026, 10, 14, 11, 0),
            params={"alert_threshold": "critical"}))
        self.assertEqual([], outbox.calls)

    def test_enqueues_to_ops_region_with_alert_kind(self):
        outbox = _FakeOutbox()
        status = emit(
            outbox, service_id="freshness-check",
            findings=(Finding("r", "critical", "x"),),
            business_date=_BDAY, now=datetime(2026, 10, 14, 11, 0))
        self.assertEqual("enqueued", status)
        (call,) = outbox.calls
        self.assertEqual(ALERT_REGION, call["region"])
        self.assertEqual("ops", call["region"])
        self.assertEqual(ALERT_KIND, call["kind"])
        self.assertEqual(_BDAY, call["business_date"])
        self.assertEqual("freshness-check:daily", call["dedupe_suffix"])

    def test_duplicate_window_reports_already_sent(self):
        outbox = _FakeOutbox(dedupe_hit=True)
        status = emit(
            outbox, service_id="freshness-check",
            findings=(Finding("r", "critical", "x"),),
            business_date=_BDAY, now=datetime(2026, 10, 14, 11, 0))
        self.assertEqual("already_sent", status)

    def test_override_bypasses_cooldown_suffix(self):
        outbox = _FakeOutbox()
        emit(
            outbox, service_id="freshness-check",
            findings=(Finding("r", "critical", "x"),),
            business_date=_BDAY, now=datetime(2026, 10, 14, 11, 0),
            override_suffix="manual-deadbeef")
        (call,) = outbox.calls
        self.assertEqual("freshness-check:manual-deadbeef",
                         call["dedupe_suffix"])


class ConstantsTests(unittest.TestCase):
    def test_alert_services_registry(self):
        self.assertIn("freshness-check", inspection_alert.ALERT_SERVICES)
        self.assertIn("channel-missing-check", inspection_alert.ALERT_SERVICES)


if __name__ == "__main__":
    unittest.main()
