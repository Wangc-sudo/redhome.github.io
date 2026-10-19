# -*- coding: utf-8 -*-
"""舰队月检（common.public_data.fleet_audit）测试。

四节体检口径与 flow/governance.js 同场景夹具互钉（2026-10-17 治理 P2
设计裁决：两侧测试同场景防漂移）。
"""

import json
import unittest
from datetime import datetime, timezone

from common.public_data.fleet_audit import (
    build_report,
    collect_sla_status,
    cron_pileup,
    decorative_deps,
    disabled_audit,
    fetch_latest_snapshot,
    fleet_audit_ddl_statements,
    grace_days,
    save_snapshot,
    sla_audit,
    zombie_audit,
)
from common.public_data.pipeline_config import PipelineConfig

TODAY = "2026-10-17"


def _cfg(sid, **kw):
    return PipelineConfig(service_id=sid, **kw)


class GraceDaysTests(unittest.TestCase):
    def test_cadence_buckets(self):
        self.assertEqual(32, grace_days("0 10 25 * *"))    # 月检（日次字段）
        self.assertEqual(32, grace_days("0 6 1 * *"))
        self.assertEqual(9, grace_days("0 9 * * 1"))       # 周检（星期字段）
        self.assertEqual(7, grace_days("30 8 * * *"))      # 日检
        self.assertEqual(7, grace_days(""))                # 兜底
        self.assertEqual(7, grace_days(None))


class DisabledAuditTests(unittest.TestCase):
    def test_overdue_first(self):
        rows = disabled_audit([
            _cfg("b", enabled=False, disabled_reason="r",
                 review_after="2026-11-01"),
            _cfg("a", enabled=False, disabled_reason="r",
                 review_after="2026-10-01"),
            _cfg("c", enabled=True),
        ], TODAY)
        self.assertEqual(2, len(rows))
        self.assertEqual("a", rows[0]["service_id"])       # 过期置顶
        self.assertTrue(rows[0]["overdue"])
        self.assertFalse(rows[1]["overdue"])


class ZombieAuditTests(unittest.TestCase):
    def test_never_ran_and_stale(self):
        """启用但无记录=僵尸；超龄=僵尸（与 JS 同场景）。"""
        rows = zombie_audit([
            _cfg("norun"),
            _cfg("fresh"),
            _cfg("stale"),
            _cfg("off", enabled=False),
        ], TODAY, {
            "fresh": datetime(2026, 10, 16, 0, 30),
            "stale": datetime(2026, 10, 1, 0, 30),
        }, lambda sid: True)
        self.assertEqual(["norun", "stale"],
                         sorted(r["service_id"] for r in rows))

    def test_cadence_grace(self):
        """月检/周检不按日检误报（2026-10-17 用户裁决，与 JS 同场景）。"""
        rows = zombie_audit([
            # 月检从未跑过：首期未到，不算僵尸
            _cfg("calendar-watch", schedule="0 10 25 * *"),
            # 周检约 11 天没跑 > 宽限 9 天 → 僵尸
            _cfg("weekly-x", schedule="30 9 * * 1"),
            # 日检约 9 天没跑 > 宽限 7 天 → 僵尸
            _cfg("daily-x", schedule="30 8 * * *"),
        ], TODAY, {
            "weekly-x": datetime(2026, 10, 6, 1, 30),
            "daily-x": datetime(2026, 10, 8, 0, 30),
        }, lambda sid: True)
        self.assertEqual(["daily-x", "weekly-x"],
                         sorted(r["service_id"] for r in rows))

    def test_unschedulable_skipped(self):
        """has_command=false（daemon/手动修复工具）不归 cron 管，跳过。

        2026-10-17 僵尸复核裁决：bi-web/dingtalk-gateway/kb-gateway/
        scheduler 常驻进程 + project-mart（_UNSCHEDULABLE）永不产生
        cron 运行历史（与 JS has_command===false 豁免同场景）。
        """
        schedulable = {"real-cron", "normal"}
        rows = zombie_audit([
            _cfg("bi-web"),
            _cfg("project-mart", schedule="0 3 * * *"),
            _cfg("real-cron"),
            _cfg("normal"),
        ], TODAY, {
            "normal": datetime(2026, 10, 16, 0, 30),
        }, lambda sid: sid in schedulable)
        self.assertEqual(["real-cron"], [r["service_id"] for r in rows])


class DecorativeDepsTests(unittest.TestCase):
    def test_unprobed_upstream_flagged(self):
        rows = decorative_deps([
            _cfg("a", depends_on=("extract-mart",)),
            _cfg("b", depends_on=("robot-hangzhou",)),
            _cfg("c", depends_on=("extract-mart", "robot-x")),
        ])
        self.assertEqual(["b", "c"], [r["service_id"] for r in rows])
        self.assertEqual(["robot-x"], rows[1]["deps"])


class SlaAuditTests(unittest.TestCase):
    """SLA 迟到（2026-10-19 治理 P2 尾巴，用户裁决「不要播报」）。"""

    def test_late_only_when_past_deadline_and_no_success_today(self):
        configs = [
            _cfg("late", sla="09:15"),                 # 过点+无成功 → 迟到
            _cfg("ok", sla="09:15"),                   # 过点但当日已成功
            _cfg("early", sla="23:59"),                # deadline 未到
            _cfg("undeclared"),                        # 未声明
            _cfg("off", enabled=False, sla="09:15"),   # 停用跳过
            _cfg("daemon", sla="09:15"),               # 不可调度跳过
        ]
        rows = sla_audit(configs, {"ok"}, "10:00",
                         lambda sid: sid != "daemon")
        self.assertEqual(["late"], [r["service_id"] for r in rows])
        self.assertEqual("09:15", rows[0]["deadline"])

    def test_collect_sla_status_beijing_day_boundary(self):
        """当日口径=北京日界（UTC 前一日 16:00 起）；finished 才算成功。"""
        class _Cur:
            def execute(self, *a, **k):
                pass

            def fetchall(self):
                return (
                    # 昨日（北京）的成功：不计入当日
                    {"service_id": "old", "status": "finished",
                     "started_at": datetime(2026, 10, 18, 15, 59)},
                    # 当日（北京 10-19 00:00 = UTC 10-18 16:00）后的成功
                    {"service_id": "fresh", "status": "finished",
                     "started_at": datetime(2026, 10, 18, 16, 5)},
                    # 当日但失败：不算成功
                    {"service_id": "broken", "status": "failed",
                     "started_at": datetime(2026, 10, 19, 0, 30)},
                )

            def close(self):
                return None

        class _C:
            def cursor(self):
                return _Cur()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        configs = [
            _cfg("old", sla="09:15"),      # 昨日成功今日无 → 迟到
            _cfg("fresh", sla="09:15"),    # 当日已成功
            _cfg("broken", sla="09:15"),   # 当日只有失败 → 迟到
        ]
        source = type("S", (), {"get_pipeline": lambda self, sid:
                                next(c for c in configs
                                     if c.service_id == sid)})()
        violations, now_hm = collect_sla_status(
            lambda _db: _C(), None,
            fleet_source=lambda: ("old", "fresh", "broken"),
            config_source=source, has_command=lambda sid: True,
            now=datetime(2026, 10, 19, 2, 0, tzinfo=timezone.utc),  # 北京 10:00
        )
        self.assertEqual("10:00", now_hm)
        self.assertEqual(["broken", "old"],
                         sorted(r["service_id"] for r in violations))


class CronPileupTests(unittest.TestCase):
    def test_threshold_and_disabled_ignored(self):
        rows = cron_pileup([
            _cfg("a", schedule="30 8 * * *"),
            _cfg("b", schedule="30 8 * * *"),
            _cfg("c", schedule="30 8 * * *"),
            _cfg("off", enabled=False, schedule="30 8 * * *"),
            _cfg("d", schedule="0 18 * * *"),
        ])
        self.assertEqual(1, len(rows))
        self.assertEqual({"time": "08:30", "count": 3,
                          "services": ["a", "b", "c"]}, rows[0])


class BuildReportTests(unittest.TestCase):
    def test_assembly_counts(self):
        report = build_report([
            _cfg("a"),
            _cfg("b", enabled=False, disabled_reason="r"),
        ], TODAY, {"a": datetime(2026, 10, 17, 0, 30)}, lambda sid: True)
        self.assertEqual(2, report["total"])
        self.assertEqual(1, report["enabled"])
        self.assertEqual(1, len(report["disabled"]))
        self.assertEqual([], report["zombies"])


class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self._row = None

    def close(self):
        return None

    def execute(self, sql, params=None):
        if sql.startswith("INSERT INTO `pd_ops_fleet_audit`"):
            self._conn.saved = params
        elif sql.startswith("SELECT `audit_date`"):
            self._row = self._conn.row
        else:  # pragma: no cover - 防呆
            raise AssertionError(f"unexpected sql: {sql}")

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, row=None):
        self.row = row
        self.saved = None
        self.committed = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        return None


class SnapshotTests(unittest.TestCase):
    def test_ddl_registered_shape(self):
        (ddl,) = fleet_audit_ddl_statements()
        self.assertIn("pd_ops_fleet_audit", ddl)
        self.assertIn("CREATE TABLE IF NOT EXISTS", ddl)
        self.assertIn("uq_audit_date", ddl)

    def test_save_snapshot_idempotent_upsert(self):
        conn = _Conn()
        report = build_report([_cfg("a")], TODAY, {}, lambda sid: True)
        save_snapshot(conn, report)
        params = conn.saved
        self.assertEqual(TODAY, params[0])
        self.assertEqual(1, params[1])                     # total
        payload = json.loads(params[7])
        self.assertEqual(TODAY, payload["date"])
        self.assertEqual(1, conn.committed)

    def test_fetch_latest_snapshot_none_and_row(self):
        self.assertIsNone(fetch_latest_snapshot(_Conn(row=None)))
        row = {
            "audit_date": "2026-11-02", "total": 48, "enabled": 27,
            "disabled_count": 21, "zombie_count": 0,
            "decorative_dep_count": 0, "pileup_count": 1,
            "report": json.dumps({"date": "2026-11-02", "zombies": []}),
            "created_at": "2026-11-02 07:00:01.000000",
        }
        snap = fetch_latest_snapshot(_Conn(row=row))
        self.assertEqual("2026-11-02", snap["audit_date"])
        self.assertEqual(48, snap["total"])
        self.assertEqual({"date": "2026-11-02", "zombies": []},
                         snap["report"])


if __name__ == "__main__":
    unittest.main()
