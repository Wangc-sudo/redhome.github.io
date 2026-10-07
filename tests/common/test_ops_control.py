"""Offline tests for ops-web 管理面的数据访问与写面校验。

ops_control 的 SQL 用 MySQL 占位符 ``%s``，这里用一层游标把 ``%s`` 翻译成
``?`` 直接喂给 sqlite3；其余语法（反引号、COALESCE）sqlite 原生支持。行用
``sqlite3.Row``（不是 dict），ops_control 的 ``row["x"] if isinstance(row, dict)
else row[0]`` 退化分支正好命中。无需真实 MySQL / Nacos。
"""

import sqlite3
import unittest

from common.public_data import ops_control
from common.public_data.ops_control import (
    DbRunHistoryGateway,
    DbRunRequestGateway,
    OpsControlError,
    RunRequest,
    validate_pipeline_fields,
)


# -- sqlite 适配（%s -> ?） -------------------------------------------------

class _PctCursor:
    def __init__(self, sqlite_cursor):
        self._c = sqlite_cursor

    def execute(self, sql, params=None):
        self._c.execute(sql.replace("%s", "?"), params or ())

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    @property
    def lastrowid(self):
        return self._c.lastrowid

    @property
    def rowcount(self):
        return self._c.rowcount

    def close(self):
        self._c.close()


class _SqliteConn:
    """sqlite3 连接适配成 ops_control 期望的接口（含事务提交/回滚）。"""

    def __init__(self, sqlite_conn):
        self._conn = sqlite_conn
        self._conn.row_factory = sqlite3.Row

    def cursor(self):
        return _PctCursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        # 测试里共享同一连接，close 不真正断开。
        pass


def _make_db():
    conn = sqlite3.connect(":memory:")
    wrapper = _SqliteConn(conn)
    # sqlite 兼容建表（去掉 ENUM / AUTO_INCREMENT / ENGINE）。
    wrapper.cursor().execute(
        "CREATE TABLE pd_ops_run_request ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " service_id VARCHAR(64) NOT NULL,"
        " requested_by VARCHAR(64) NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " note VARCHAR(255) DEFAULT NULL,"
        " exit_code INT DEFAULT NULL,"
        " created_at VARCHAR(32) NOT NULL,"
        " claimed_at VARCHAR(32) DEFAULT NULL,"
        " finished_at VARCHAR(32) DEFAULT NULL)"
    )
    wrapper.cursor().execute(
        "CREATE TABLE pd_ops_pipeline_audit ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " actor VARCHAR(64) NOT NULL,"
        " action VARCHAR(16) NOT NULL,"
        " service_id VARCHAR(64) NOT NULL,"
        " detail VARCHAR(255) DEFAULT NULL,"
        " created_at VARCHAR(32) NOT NULL)"
    )
    wrapper.cursor().execute(
        "CREATE TABLE pd_ops_run_history ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " service_id VARCHAR(64) NOT NULL,"
        " trigger_type TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'running',"
        " exit_code INT DEFAULT NULL,"
        " note VARCHAR(255) DEFAULT NULL,"
        " started_at VARCHAR(32) NOT NULL,"
        " finished_at VARCHAR(32) DEFAULT NULL)"
    )
    conn.commit()
    return wrapper


# -- 写面校验（validate_pipeline_fields） -----------------------------------

class WriteValidationTests(unittest.TestCase):
    def test_valid_fields_return_normalized_config(self):
        cfg = validate_pipeline_fields(
            "  robot-x  ", "apps", "0 2 * * *", enabled=True,
            description="  demo  ", depends_on=("a", "a", "b"),
        )
        self.assertEqual(cfg.service_id, "robot-x")  # 去空白
        self.assertEqual(cfg.kind, "apps")
        self.assertEqual(cfg.schedule, "0 2 * * *")
        self.assertEqual(cfg.enabled, True)
        self.assertEqual(cfg.description, "  demo  ")  # description 不裁剪
        self.assertEqual(cfg.depends_on, ("a", "b"))  # 去重

    def test_invalid_service_id_rejected(self):
        with self.assertRaises(OpsControlError):
            validate_pipeline_fields("BAD ID", "apps", "0 2 * * *")

    def test_unsupported_kind_rejected(self):
        with self.assertRaises(OpsControlError):
            validate_pipeline_fields("robot-x", "nope", "0 2 * * *")

    def test_missing_schedule_rejected(self):
        with self.assertRaises(OpsControlError):
            validate_pipeline_fields("robot-x", "apps", "   ")

    def test_invalid_cron_rejected(self):
        with self.assertRaises(OpsControlError):
            validate_pipeline_fields("robot-x", "apps", "not a cron")

    def test_self_dependency_rejected(self):
        with self.assertRaises(OpsControlError):
            validate_pipeline_fields(
                "robot-x", "apps", "0 2 * * *", depends_on=("robot-x",)
            )

    def test_invalid_dependency_rejected(self):
        with self.assertRaises(OpsControlError):
            validate_pipeline_fields(
                "robot-x", "apps", "0 2 * * *", depends_on=("BAD DEP",)
            )

    def test_too_many_dependencies_rejected(self):
        with self.assertRaises(OpsControlError):
            validate_pipeline_fields(
                "robot-x", "apps", "0 2 * * *",
                depends_on=tuple(f"d{i}" for i in range(9)),
            )

    def test_non_bool_enabled_rejected(self):
        with self.assertRaises(OpsControlError):
            validate_pipeline_fields("robot-x", "apps", "0 2 * * *", enabled="yes")


# -- 「立即运行一次」请求生命周期 -------------------------------------------

class RunRequestLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()

    def test_insert_and_fetch_pending(self):
        rid = ops_control.insert_run_request(self.db, "robot-hangzhou", "alice")
        self.assertTrue(rid)
        self.assertTrue(ops_control.has_pending_request(self.db, "robot-hangzhou"))
        pending = ops_control.fetch_pending_requests(self.db)
        self.assertEqual(
            pending, (RunRequest(rid, "robot-hangzhou", "alice"),)
        )

    def test_claim_moves_to_launched(self):
        rid = ops_control.insert_run_request(self.db, "robot-x", "alice")
        self.assertTrue(ops_control.claim_run_request(self.db, rid))
        # pending 已清空；has_pending 只看 pending 态。
        self.assertFalse(ops_control.has_pending_request(self.db, "robot-x"))
        self.assertEqual(ops_control.fetch_pending_requests(self.db), ())

    def test_claim_is_idempotent_on_already_launched(self):
        rid = ops_control.insert_run_request(self.db, "robot-x", "alice")
        ops_control.claim_run_request(self.db, rid)
        # 再次认领（并发副本）返回 False，不重复认领。
        self.assertFalse(ops_control.claim_run_request(self.db, rid))

    def test_finish_zero_is_finished(self):
        rid = ops_control.insert_run_request(self.db, "robot-x", "alice")
        ops_control.claim_run_request(self.db, rid)
        ops_control.finish_run_request(self.db, rid, 0)
        rows = ops_control.fetch_recent_requests(self.db)
        self.assertEqual(rows[0]["status"], "finished")
        self.assertEqual(rows[0]["exit_code"], 0)

    def test_finish_nonzero_is_failed(self):
        rid = ops_control.insert_run_request(self.db, "robot-x", "alice")
        ops_control.claim_run_request(self.db, rid)
        ops_control.finish_run_request(self.db, rid, 3)
        rows = ops_control.fetch_recent_requests(self.db)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(rows[0]["exit_code"], 3)

    def test_finish_none_is_failed_with_note(self):
        rid = ops_control.insert_run_request(self.db, "robot-x", "alice")
        ops_control.claim_run_request(self.db, rid)
        ops_control.finish_run_request(self.db, rid, None)
        rows = ops_control.fetch_recent_requests(self.db)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIsNotNone(rows[0]["note"])

    def test_reject_pending(self):
        rid = ops_control.insert_run_request(self.db, "bi-web", "bob")
        ops_control.reject_run_request(self.db, rid, "无调度命令映射")
        rows = ops_control.fetch_recent_requests(self.db)
        self.assertEqual(rows[0]["status"], "rejected")
        self.assertEqual(rows[0]["note"], "无调度命令映射")

    def test_fetch_recent_requests_limit_validation(self):
        with self.assertRaises(OpsControlError):
            ops_control.fetch_recent_requests(self.db, 0)
        with self.assertRaises(OpsControlError):
            ops_control.fetch_recent_requests(self.db, 501)
        with self.assertRaises(OpsControlError):
            ops_control.fetch_recent_requests(self.db, True)


# -- 注册表变更审计 ---------------------------------------------------------

class PipelineAuditTests(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()

    def test_insert_and_fetch_audit(self):
        ops_control.insert_pipeline_audit(
            self.db, "admin", "add", "robot-x", "enabled=true"
        )
        rows = ops_control.fetch_pipeline_audit(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor"], "admin")
        self.assertEqual(rows[0]["action"], "add")
        self.assertEqual(rows[0]["service_id"], "robot-x")
        self.assertEqual(rows[0]["detail"], "enabled=true")

    def test_audit_detail_truncated_to_200(self):
        ops_control.insert_pipeline_audit(
            self.db, "admin", "add", "robot-x", "x" * 500
        )
        rows = ops_control.fetch_pipeline_audit(self.db)
        self.assertEqual(len(rows[0]["detail"]), 200)

    def test_fetch_audit_limit_validation(self):
        with self.assertRaises(OpsControlError):
            ops_control.fetch_pipeline_audit(self.db, 0)
        with self.assertRaises(OpsControlError):
            ops_control.fetch_pipeline_audit(self.db, 1001)


# -- 定时任务执行流水生命周期 -------------------------------------------------

class RunHistoryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()

    def test_insert_and_fetch_running_row(self):
        hid = ops_control.insert_run_history(self.db, "robot-hangzhou", "cron")
        self.assertTrue(hid)
        rows = ops_control.fetch_run_history(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["service_id"], "robot-hangzhou")
        self.assertEqual(rows[0]["trigger_type"], "cron")
        self.assertEqual(rows[0]["status"], "running")
        self.assertIsNone(rows[0]["finished_at"])

    def test_invalid_trigger_type_rejected(self):
        with self.assertRaises(OpsControlError):
            ops_control.insert_run_history(self.db, "robot-x", "manual")

    def test_finish_zero_is_finished(self):
        hid = ops_control.insert_run_history(self.db, "sync-wdt", "cron")
        ops_control.finish_run_history(self.db, hid, 0)
        rows = ops_control.fetch_run_history(self.db)
        self.assertEqual(rows[0]["status"], "finished")
        self.assertEqual(rows[0]["exit_code"], 0)
        self.assertIsNotNone(rows[0]["finished_at"])

    def test_finish_nonzero_is_failed(self):
        hid = ops_control.insert_run_history(self.db, "sync-wdt", "cron")
        ops_control.finish_run_history(self.db, hid, 3)
        rows = ops_control.fetch_run_history(self.db)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(rows[0]["exit_code"], 3)

    def test_finish_none_is_failed_with_note(self):
        # 锁被占用/运行异常（无退出码）：failed + 说明。
        hid = ops_control.insert_run_history(self.db, "sync-wdt", "cron")
        ops_control.finish_run_history(self.db, hid, None)
        rows = ops_control.fetch_run_history(self.db)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertIsNone(rows[0]["exit_code"])
        self.assertIsNotNone(rows[0]["note"])

    def test_finish_is_idempotent_on_already_finished(self):
        hid = ops_control.insert_run_history(self.db, "sync-wdt", "cron")
        ops_control.finish_run_history(self.db, hid, 0)
        ops_control.finish_run_history(self.db, hid, 1)  # 迟到回写不覆盖
        rows = ops_control.fetch_run_history(self.db)
        self.assertEqual(rows[0]["status"], "finished")
        self.assertEqual(rows[0]["exit_code"], 0)

    def test_fetch_filter_by_service_id(self):
        ops_control.insert_run_history(self.db, "sync-wdt", "cron")
        ops_control.insert_run_history(self.db, "robot-hangzhou", "run_once")
        rows = ops_control.fetch_run_history(self.db, service_id="sync-wdt")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["service_id"], "sync-wdt")

    def test_fetch_rejects_bad_service_id_filter(self):
        with self.assertRaises(OpsControlError):
            ops_control.fetch_run_history(self.db, service_id="BAD ID' OR 1=1")

    def test_fetch_limit_validation(self):
        with self.assertRaises(OpsControlError):
            ops_control.fetch_run_history(self.db, 0)
        with self.assertRaises(OpsControlError):
            ops_control.fetch_run_history(self.db, 1001)
        with self.assertRaises(OpsControlError):
            ops_control.fetch_run_history(self.db, True)


# -- scheduler 侧网关（DbRunRequestGateway） --------------------------------

class DbRunRequestGatewayTests(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        self.connect = lambda settings: self.db
        self.gateway = DbRunRequestGateway(self.connect, None)

    def test_gateway_roundtrip(self):
        rid = ops_control.insert_run_request(self.db, "robot-hangzhou", "alice")
        self.assertEqual(
            self.gateway.fetch_pending(),
            (RunRequest(rid, "robot-hangzhou", "alice"),),
        )
        self.assertTrue(self.gateway.claim(rid))
        self.gateway.mark_finished(rid, 0)
        # 完成后不再出现在 pending。
        self.assertEqual(self.gateway.fetch_pending(), ())

    def test_gateway_reject(self):
        rid = ops_control.insert_run_request(self.db, "bi-web", "bob")
        self.gateway.mark_rejected(rid, "无调度命令映射")
        rows = ops_control.fetch_recent_requests(self.db)
        self.assertEqual(rows[0]["status"], "rejected")


# -- scheduler 侧网关（DbRunHistoryGateway） ---------------------------------

class DbRunHistoryGatewayTests(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()
        self.connect = lambda settings: self.db
        self.gateway = DbRunHistoryGateway(self.connect, None)

    def test_gateway_roundtrip(self):
        hid = self.gateway.record_start("pages-qudao", "cron")
        self.assertTrue(hid)
        self.gateway.record_finish(hid, 0)
        rows = ops_control.fetch_run_history(self.db)
        self.assertEqual(rows[0]["service_id"], "pages-qudao")
        self.assertEqual(rows[0]["trigger_type"], "cron")
        self.assertEqual(rows[0]["status"], "finished")


if __name__ == "__main__":
    unittest.main()
