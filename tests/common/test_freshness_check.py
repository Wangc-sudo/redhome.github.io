# -*- coding: utf-8 -*-
"""L3 数据新鲜度看门狗（A3）离线单测：T-1 存在性断言 + 工作日门 + 入队。"""

import io
import re
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from unittest import mock

from common.daily_robot import freshness_check, inspection_alert
from common.daily_robot.freshness_check import (
    CHECKS,
    FreshnessCheck,
    inspect,
    run_freshness_check,
    table_has_business_date,
)

_BDAY = date(2026, 10, 14)

_TABLE_RE = re.compile(r"FROM\s+`([A-Za-z0-9_]+)`")


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._row = None
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "dim_calendar" in sql:
            self._row = self._conn.workday_row
            return
        match = _TABLE_RE.search(sql)
        table = match.group(1) if match else ""
        region = params[-1] if "AND `region` = %s" in sql else None
        self._row = {"hit": 1} if (table, region) in self._conn.present else None

    def fetchone(self):
        return self._row

    def close(self):
        pass


class _FakeConn:
    def __init__(self, present, workday_row=None):
        self.present = set(present)
        self.workday_row = workday_row
        self._cursor = _FakeCursor(self)

    def cursor(self):
        return self._cursor


class _FakeOutbox:
    def __init__(self, dedupe_hit=False):
        self.calls = []
        self._dedupe_hit = dedupe_hit

    def enqueue(self, **kwargs):
        self.calls.append(kwargs)
        return not self._dedupe_hit


_ALL_PRESENT = {
    ("fact_channel_daily_sales", None),
    ("fact_daily_report_offline", "qudao"),
}


class InspectTests(unittest.TestCase):
    def test_all_present_yields_no_findings(self):
        conn = _FakeConn(_ALL_PRESENT)
        self.assertEqual((), inspect(conn, business_date=_BDAY))

    def test_missing_source_table_is_critical(self):
        conn = _FakeConn({("fact_daily_report_offline", "qudao")})
        findings = inspect(conn, business_date=_BDAY)
        self.assertEqual(1, len(findings))
        self.assertEqual("channel_daily_sales_missing", findings[0].code)
        self.assertEqual("critical", findings[0].severity)
        self.assertIn("T-1", findings[0].text)

    def test_missing_derived_report_is_warning(self):
        conn = _FakeConn({("fact_channel_daily_sales", None)})
        findings = inspect(conn, business_date=_BDAY)
        self.assertEqual(1, len(findings))
        self.assertEqual("channel_daily_report_missing", findings[0].code)
        self.assertEqual("warning", findings[0].severity)
        self.assertIn("region=qudao", findings[0].text)

    def test_table_has_business_date_applies_region_filter(self):
        conn = _FakeConn({("fact_daily_report_offline", "qudao")})
        self.assertTrue(table_has_business_date(
            conn, "fact_daily_report_offline", _BDAY, "qudao"))
        self.assertFalse(table_has_business_date(
            conn, "fact_daily_report_offline", _BDAY, "offline_all"))

    def test_presence_is_asserted_not_max(self):
        """即使表内存在更晚日期（换月含未来日），只断言 T-1 存在。"""
        conn = _FakeConn({("fact_channel_daily_sales", None)})
        sql = None
        table_has_business_date(conn, "fact_channel_daily_sales", _BDAY)
        sql = conn._cursor.executed[-1][0]
        self.assertIn("business_date` = %s", sql)
        self.assertNotIn("MAX", sql.upper())


class RunFreshnessCheckTests(unittest.TestCase):
    def test_healthy_day_enqueues_nothing(self):
        outbox = _FakeOutbox()
        result = run_freshness_check(
            _FakeConn(_ALL_PRESENT), outbox,
            business_date=_BDAY, now=datetime(2026, 10, 14, 11, 0))
        self.assertEqual("no_findings", result["status"])
        self.assertEqual(0, result["findings"])
        self.assertEqual([], outbox.calls)

    def test_missing_rows_enqueue_alert(self):
        outbox = _FakeOutbox()
        conn = _FakeConn(
            {("fact_daily_report_offline", "qudao")},
            workday_row={"is_workday": 1})
        result = run_freshness_check(
            conn, outbox,
            business_date=_BDAY, now=datetime(2026, 10, 14, 11, 0))
        self.assertEqual("enqueued", result["status"])
        (call,) = outbox.calls
        self.assertEqual("ops", call["region"])
        self.assertEqual("alert", call["kind"])
        self.assertEqual("freshness-check:daily", call["dedupe_suffix"])

    def test_non_workday_suppresses_enqueue(self):
        outbox = _FakeOutbox()
        conn = _FakeConn(
            {("fact_daily_report_offline", "qudao")},
            workday_row={"is_workday": 0})
        result = run_freshness_check(
            conn, outbox,
            business_date=_BDAY, now=datetime(2026, 10, 14, 11, 0))
        self.assertEqual("suppressed_non_workday", result["status"])
        self.assertEqual(1, result["findings"])   # 仍照常检查
        self.assertEqual([], outbox.calls)

    def test_no_workday_gate_forces_enqueue_on_rest_day(self):
        outbox = _FakeOutbox()
        conn = _FakeConn(
            {("fact_daily_report_offline", "qudao")},
            workday_row={"is_workday": 0})
        result = run_freshness_check(
            conn, outbox, business_date=_BDAY,
            now=datetime(2026, 10, 14, 11, 0), skip_workday_gate=True)
        self.assertEqual("enqueued", result["status"])

    def test_calendar_uncovered_treated_as_workday(self):
        outbox = _FakeOutbox()
        conn = _FakeConn({("fact_daily_report_offline", "qudao")},
                         workday_row=None)
        result = run_freshness_check(
            conn, outbox, business_date=_BDAY,
            now=datetime(2026, 10, 14, 11, 0))
        self.assertEqual("enqueued", result["status"])

    def test_dry_reports_without_enqueue(self):
        outbox = _FakeOutbox()
        conn = _FakeConn({("fact_daily_report_offline", "qudao")})
        result = run_freshness_check(
            conn, outbox, business_date=_BDAY,
            now=datetime(2026, 10, 14, 11, 0), dry=True)
        self.assertEqual("dry", result["status"])
        self.assertEqual(1, result["findings"])
        self.assertEqual([], outbox.calls)

    def test_params_threshold_can_silence_warning_only_miss(self):
        outbox = _FakeOutbox()
        conn = _FakeConn({("fact_channel_daily_sales", None)},
                         workday_row={"is_workday": 1})
        result = run_freshness_check(
            conn, outbox, business_date=_BDAY,
            now=datetime(2026, 10, 14, 11, 0),
            params={"alert_threshold": "critical"})
        self.assertEqual("below_threshold", result["status"])
        self.assertEqual([], outbox.calls)


class ChecksConfigTests(unittest.TestCase):
    def test_checks_cover_two_channel_facts(self):
        tables = {c.table for c in CHECKS}
        self.assertEqual({"fact_channel_daily_sales",
                          "fact_daily_report_offline"}, tables)

    def test_custom_checks_are_honoured(self):
        custom = (FreshnessCheck("x", "fact_channel_daily_sales", None,
                                 "critical", "渠道日销"),)
        conn = _FakeConn(set())
        findings = inspect(conn, business_date=_BDAY, checks=custom)
        self.assertEqual(("x",), tuple(f.code for f in findings))

    def test_service_id_constant(self):
        self.assertEqual("freshness-check", freshness_check.SERVICE_ID)


class FreshnessCheckCliTests(unittest.TestCase):
    """mart_cli freshness-check 子命令（编排面：T-1 默认 / 门禁 / 去重后缀）。"""

    def _run(self, argv, task_result=None):
        from common.daily_robot import mart_cli

        result = task_result or {
            "status": "ok", "business_date": "2026-10-14", "findings": 0,
        }
        output = io.StringIO()
        with mock.patch.object(mart_cli, "load_settings") as settings, \
             mock.patch.object(mart_cli, "require_business_run"), \
             mock.patch.object(mart_cli, "resolve_service_id",
                               return_value="freshness-check"), \
             mock.patch.object(mart_cli, "_pipeline_enabled", return_value=True), \
             mock.patch.object(mart_cli, "connect_mart"), \
             mock.patch.object(mart_cli, "build_outbox"), \
             mock.patch.object(mart_cli, "run_freshness_check_task",
                               return_value=result) as task, \
             mock.patch.object(
                 inspection_alert, "fetch_service_params",
                 return_value={}), \
             mock.patch.object(mart_cli, "datetime") as mock_dt:
            settings.return_value.region_seed_path = None
            mock_dt.now.return_value = datetime(2026, 10, 15, 11, 0)
            mock_dt.fromisoformat = date.fromisoformat
            with redirect_stdout(output):
                mart_cli.main(argv)
        return task, output.getvalue()

    def test_defaults_to_yesterday_and_prints_result(self):
        task, text = self._run(["freshness-check", "--confirm-local-test-write"])
        self.assertIn("kind=alert", text)
        kwargs = task.call_args.kwargs
        self.assertEqual(date(2026, 10, 14), kwargs["business_date"])  # T-1
        self.assertIsNone(kwargs["dedupe_override"])
        self.assertFalse(kwargs["dry"])
        self.assertFalse(kwargs["skip_workday_gate"])
        self.assertEqual({}, kwargs["params"])

    def test_date_override_wins(self):
        task, _ = self._run([
            "freshness-check", "--confirm-local-test-write", "--date", "2026-10-01",
        ])
        self.assertEqual(date(2026, 10, 1),
                         task.call_args.kwargs["business_date"])

    def test_dry_and_no_workday_gate_flags(self):
        task, _ = self._run([
            "freshness-check", "--confirm-local-test-write",
            "--dry", "--no-workday-gate",
        ])
        kwargs = task.call_args.kwargs
        self.assertTrue(kwargs["dry"])
        self.assertTrue(kwargs["skip_workday_gate"])

    def test_force_suffix_bypasses_dedupe(self):
        task, _ = self._run([
            "freshness-check", "--confirm-local-test-write", "--force",
        ])
        self.assertTrue(
            task.call_args.kwargs["dedupe_override"].startswith("manual-"))

    def test_requires_confirmation(self):
        from common.daily_robot import mart_cli

        with self.assertRaises(SystemExit):
            mart_cli.main(["freshness-check"])

    def test_disabled_line_skips(self):
        from common.daily_robot import mart_cli

        output = io.StringIO()
        with mock.patch.object(mart_cli, "load_settings") as settings, \
             mock.patch.object(mart_cli, "require_business_run"), \
             mock.patch.object(mart_cli, "resolve_service_id",
                               return_value="freshness-check"), \
             mock.patch.object(mart_cli, "_pipeline_enabled", return_value=False), \
             mock.patch.object(mart_cli, "run_freshness_check_task") as task:
            settings.return_value.region_seed_path = None
            with redirect_stdout(output):
                mart_cli.main(["freshness-check", "--confirm-local-test-write"])
        self.assertIn("reason=disabled", output.getvalue())
        task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
