# -*- coding: utf-8 -*-
"""calendar_watch（工作日历覆盖看门，三层防线 B 层）离线单测。"""

import io
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from unittest import mock

from common.daily_robot import calendar_watch
from common.daily_robot.calendar_watch import (
    RULE_FALLBACK_SOURCE,
    build_watch_markdown,
    classify_issues,
    fetch_calendar_coverage,
    run_watch,
)


class _FakeCursor:
    def __init__(self, coverage):
        self._coverage = coverage
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._row = self._coverage.get(params[1], {"n": 0, "fb": 0})

    def fetchone(self):
        return self._row

    def close(self):
        pass


class _FakeConn:
    def __init__(self, coverage):
        self._cursor = _FakeCursor(coverage)

    def cursor(self):
        return self._cursor


class _FakeOutbox:
    def __init__(self, dedupe_hit=False):
        self.calls = []
        self._dedupe_hit = dedupe_hit

    def enqueue(self, **kwargs):
        self.calls.append(kwargs)
        return not self._dedupe_hit


_OK = {"n": 31, "fb": 0}                       # DB 行形（_FakeConn 用）
_OK_SNAPSHOT = {"rows": 31, "fallback_rows": 0}  # 覆盖快照形（classify 用）


class FetchCoverageTests(unittest.TestCase):
    def test_counts_rows_and_fallback_per_month(self):
        conn = _FakeConn({
            "2026-10": {"n": 31, "fb": 0},
            "2027-01": {"n": 31, "fb": 31},
        })
        coverage = fetch_calendar_coverage(conn, ("2026-10", "2027-01"))
        self.assertEqual({"rows": 31, "fallback_rows": 0}, coverage["2026-10"])
        self.assertEqual({"rows": 31, "fallback_rows": 31}, coverage["2027-01"])
        sql, params = conn._cursor.executed[-1]
        self.assertIn(RULE_FALLBACK_SOURCE, params)
        self.assertIn("2027-01", params)
        self.assertIn("dim_calendar", sql)


class ClassifyIssuesTests(unittest.TestCase):
    def test_ok_when_both_months_fully_seeded(self):
        coverage = {"2026-10": _OK_SNAPSHOT,
                    "2026-11": {"rows": 30, "fallback_rows": 0}}
        self.assertEqual([], classify_issues(
            coverage, current_month="2026-10", next_month="2026-11"))

    def test_p0_current_month_zero_rows(self):
        coverage = {"2026-11": _OK_SNAPSHOT}
        issues = classify_issues(
            coverage, current_month="2026-10", next_month="2026-11")
        self.assertEqual("P0", issues[0][0])
        self.assertIn("2026-10", issues[0][1])

    def test_p1_current_month_on_rule_fallback(self):
        coverage = {"2026-10": {"rows": 31, "fallback_rows": 31},
                    "2026-11": _OK_SNAPSHOT}
        issues = classify_issues(
            coverage, current_month="2026-10", next_month="2026-11")
        self.assertEqual("P1", issues[0][0])
        self.assertIn("规则兜底", issues[0][1])

    def test_p1_next_month_zero_and_p2_next_month_fallback(self):
        p1 = classify_issues(
            {"2026-10": _OK_SNAPSHOT},
            current_month="2026-10", next_month="2026-11")
        self.assertEqual(["P1"], [level for level, _ in p1])
        p2 = classify_issues(
            {"2026-10": _OK_SNAPSHOT,
             "2026-11": {"rows": 30, "fallback_rows": 30}},
            current_month="2026-10", next_month="2026-11")
        self.assertEqual(["P2"], [level for level, _ in p2])


class RunWatchTests(unittest.TestCase):
    def test_ok_months_enqueue_nothing(self):
        outbox = _FakeOutbox()
        result = run_watch(
            _FakeConn({"2026-10": _OK, "2026-11": _OK}), outbox,
            anchor_date=date(2026, 10, 25), now=datetime(2026, 10, 25, 10, 0))
        self.assertEqual(("ok", 0), result)
        self.assertEqual([], outbox.calls)

    def test_alert_enqueues_to_offline_all_group(self):
        outbox = _FakeOutbox()
        conn = _FakeConn({
            "2026-10": {"n": 31, "fb": 0},
            "2026-11": {"n": 30, "fb": 30},
        })
        result = run_watch(
            conn, outbox,
            anchor_date=date(2026, 10, 25), now=datetime(2026, 10, 25, 10, 0))
        self.assertEqual(("alerted", 1, "enqueued"), result)
        (call,) = outbox.calls
        self.assertEqual("offline_all", call["region"])
        self.assertEqual("calendar_watch", call["kind"])
        self.assertEqual(date(2026, 10, 25), call["business_date"])
        self.assertIsNone(call["dedupe_suffix"])
        self.assertIn("P2", call["body_md"])
        self.assertIn("calendar-import", call["body_md"])

    def test_dedupe_hit_reports_already_sent(self):
        outbox = _FakeOutbox(dedupe_hit=True)
        result = run_watch(
            _FakeConn({"2026-10": {"n": 31, "fb": 0},
                        "2026-11": {"n": 30, "fb": 30}}),
            outbox,
            anchor_date=date(2026, 10, 25), now=datetime(2026, 10, 25, 10, 0))
        self.assertEqual(("alerted", 1, "already_sent"), result)

    def test_markdown_lists_levels_and_hint(self):
        body = build_watch_markdown(issues=[("P1", "当月兜底"), ("P2", "下月兜底")])
        self.assertIn("**P1** 当月兜底", body)
        self.assertIn("**P2** 下月兜底", body)
        self.assertIn("calendar-import", body)


class CalendarWatchCliTests(unittest.TestCase):
    """mart_cli calendar-watch 子命令（编排面：门禁/锚定/去重后缀）。"""

    def _run(self, argv, watch_result=("ok", 0)):
        from common.daily_robot import mart_cli

        output = io.StringIO()
        with mock.patch.object(mart_cli, "load_settings") as settings, \
             mock.patch.object(mart_cli, "require_business_run"), \
             mock.patch.object(mart_cli, "connect_mart") as conn, \
             mock.patch.object(mart_cli, "build_outbox"), \
             mock.patch.object(
                 mart_cli, "run_calendar_watch_task",
                 return_value=watch_result) as task, \
             mock.patch.object(mart_cli, "datetime") as mock_dt:
            settings.return_value.region_seed_path = None
            mock_dt.now.return_value = datetime(2026, 10, 25, 10, 0)
            mock_dt.fromisoformat = date.fromisoformat
            with redirect_stdout(output):
                mart_cli.main(argv)
        return task, output.getvalue()

    def test_enqueues_with_anchor_and_prints_result(self):
        task, text = self._run([
            "calendar-watch", "--confirm-local-test-write",
        ])
        self.assertIn("kind=calendar_watch", text)
        self.assertIn("('ok', 0)", text)
        kwargs = task.call_args.kwargs
        self.assertEqual(date(2026, 10, 25), kwargs["anchor_date"])
        self.assertIsNone(kwargs["dedupe_suffix"])

    def test_force_suffix_bypasses_dedupe(self):
        task, _ = self._run([
            "calendar-watch", "--confirm-local-test-write", "--force",
        ])
        self.assertTrue(
            task.call_args.kwargs["dedupe_suffix"].startswith("manual-"))

    def test_requires_confirmation(self):
        from common.daily_robot import mart_cli

        with self.assertRaises(SystemExit):
            mart_cli.main(["calendar-watch"])


if __name__ == "__main__":
    unittest.main()
