"""月目标结转/提醒任务（target_rollover）离线单测。

DB 访问面（fetch_target_month_summary / carry_forward_targets）在
test_report_roster.py 以 sqlite 覆盖；本文件用 monkeypatch + 假 outbox
验证任务编排（月份推算、scope 过滤、入队参数）。
"""

import unittest
from datetime import date, datetime
from unittest import mock

from common.daily_robot import target_rollover


class _FakeOutbox:
    def __init__(self, dedupe_hit=False):
        self.calls = []
        self._dedupe_hit = dedupe_hit

    def enqueue(self, **kwargs):
        self.calls.append(kwargs)
        return not self._dedupe_hit


class ShiftYearMonthTests(unittest.TestCase):
    def test_shift_within_and_across_year(self):
        self.assertEqual("2026-10",
                         target_rollover.shift_year_month("2026-11", -1))
        self.assertEqual("2026-11",
                         target_rollover.shift_year_month("2026-10", 1))
        self.assertEqual("2026-12",
                         target_rollover.shift_year_month("2027-01", -1))
        self.assertEqual("2027-01",
                         target_rollover.shift_year_month("2026-12", 1))


class RunRolloverTests(unittest.TestCase):
    def test_month_wiring_and_dry_passthrough(self):
        captured = {}

        def _carry(conn, *, from_month, to_month, actor, apply=True):
            captured.update(from_month=from_month, to_month=to_month,
                            actor=actor, apply=apply)
            return {"inserted": [("hangzhou", "余发兴")], "skipped": []}

        with mock.patch.object(target_rollover.report_roster,
                               "carry_forward_targets", _carry):
            result = target_rollover.run_rollover(
                object(), anchor_date=date(2026, 11, 1), apply=False)

        self.assertEqual(
            {"from_month": "2026-10", "to_month": "2026-11",
             "actor": "target-rollover", "apply": False},
            captured)
        self.assertEqual([("hangzhou", "余发兴")], result["inserted"])


class RunRemindTests(unittest.TestCase):
    def _run(self, summary, configs, dedupe_hit=False):
        outbox = _FakeOutbox(dedupe_hit=dedupe_hit)
        with mock.patch.object(
                target_rollover.report_roster, "fetch_target_month_summary",
                lambda conn, ym: summary):
            results = target_rollover.run_remind(
                object(), outbox, region_configs=configs,
                anchor_date=date(2026, 10, 28),
                now=datetime(2026, 10, 28, 10, 0),
            )
        return results, outbox

    def test_enqueues_per_scope_with_robot_only(self):
        results, outbox = self._run(
            summary={
                "hangzhou": {"people": 4, "total": 3165000.0},
                "dining": {"people": 1, "total": 100000.0},  # 无群机器人
            },
            configs={"hangzhou": object(), "shaoxing": object()},
        )

        self.assertEqual(
            [("dining", "no_robot"), ("hangzhou", "enqueued")], results)
        self.assertEqual(1, len(outbox.calls))
        call = outbox.calls[0]
        self.assertEqual("hangzhou", call["region"])
        self.assertEqual("target_remind", call["kind"])
        self.assertEqual(date(2026, 10, 28), call["business_date"])
        self.assertIn("2026-10", call["body_md"])
        self.assertIn("2026-11", call["body_md"])   # 结转预告指向下月
        self.assertIn("3,165,000", call["body_md"])
        self.assertIn(target_rollover.OPS_WEB_ROSTER_URL, call["body_md"])
        self.assertIsNone(call["dedupe_suffix"])

    def test_dedupe_hit_reports_already_sent(self):
        results, outbox = self._run(
            summary={"hangzhou": {"people": 4, "total": 3165000.0}},
            configs={"hangzhou": object()},
            dedupe_hit=True,
        )
        self.assertEqual([("hangzhou", "already_sent")], results)
        self.assertEqual(1, len(outbox.calls))

    def test_force_suffix_passthrough(self):
        outbox = _FakeOutbox()
        with mock.patch.object(
                target_rollover.report_roster, "fetch_target_month_summary",
                lambda conn, ym: {"hangzhou": {"people": 1, "total": 1.0}}):
            target_rollover.run_remind(
                object(), outbox, region_configs={"hangzhou": object()},
                anchor_date=date(2026, 10, 28),
                now=datetime(2026, 10, 28, 10, 0),
                dedupe_suffix="manual-20261028103000",
            )
        self.assertEqual("manual-20261028103000",
                         outbox.calls[0]["dedupe_suffix"])


if __name__ == "__main__":
    unittest.main()
