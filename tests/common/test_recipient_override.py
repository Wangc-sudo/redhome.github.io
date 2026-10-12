# -*- coding: utf-8 -*-
"""任务级收件人覆盖层（dim_recipient_override，2026-10-12 方案 B）单测。

纯函数合并（include 追加 / exclude 移除 / 保序去重 / fail-soft）是本模块的
核心；写路径与审计同事务、读面只取启用行。
"""

import unittest

from common.public_data import recipient_override as ro


_ROW_KEYS = ro._OVERRIDE_KEYS


def _row(**kwargs):
    """按 :data:`_OVERRIDE_KEYS` 造一行（默认启用）。"""
    base = {
        "id": 1, "service_id": "robot-check-hangzhou", "mode": "include",
        "target_type": "user_id", "target_value": "u9",
        "resolved_user_id": "u9", "enabled": 1, "note": None,
        "updated_by": "ops", "updated_at": None,
    }
    base.update(kwargs)
    return tuple(base[k] for k in _ROW_KEYS)


class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0
        self._rows = []

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        upper = sql.lstrip().upper()
        if upper.startswith("UPDATE") or upper.startswith("DELETE"):
            self.rowcount = self._conn.affected
            self._rows = []
        else:
            self._rows = self._conn.router(sql, params)
            self.rowcount = len(self._rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _Conn:
    def __init__(self, rows_by_keyword=None, *, affected=1, boom=False):
        self._rows_by_keyword = rows_by_keyword or {}
        self.affected = affected
        self.boom = boom
        self.executed = []
        self.commits = 0

    def router(self, sql, params):
        if self.boom:
            raise RuntimeError("db down")
        for keyword, rows in self._rows_by_keyword.items():
            if keyword in sql:
                return list(rows)
        return []

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class DdlTests(unittest.TestCase):

    def test_ddl_creates_both_tables(self):
        statements = ro.recipient_override_ddl_statements()
        self.assertEqual(2, len(statements))
        self.assertIn("dim_recipient_override`", statements[0])
        self.assertIn("uk_recipient_override", statements[0])
        self.assertIn("dim_recipient_override_audit`", statements[1])
        for sql in statements:
            self.assertIn("CREATE TABLE IF NOT EXISTS", sql)


class FetchOverridesTests(unittest.TestCase):

    def test_only_enabled_rows_returned(self):
        conn = _Conn({"`enabled` = 1": [
            {"id": 1, "service_id": "s", "mode": "include",
             "target_type": "user_id", "target_value": "u1",
             "resolved_user_id": "u1", "enabled": 1, "note": None,
             "updated_by": "ops", "updated_at": None},
        ]})
        rows = ro.fetch_overrides(conn, "s")
        self.assertEqual(1, len(rows))
        self.assertEqual("include", rows[0]["mode"])
        self.assertEqual(1, rows[0]["enabled"])
        sql, params = conn.executed[0]
        self.assertIn("`enabled` = 1", sql)
        self.assertEqual(("s",), params)


class ApplyOverridesTests(unittest.TestCase):

    _BASE = [("u1", "张三"), ("u2", "李四"), ("u3", "王五")]

    def test_no_overrides_returns_base_order(self):
        self.assertEqual(
            ["u1", "u2", "u3"], ro.apply_recipient_overrides(self._BASE, []))

    def test_include_appends_deduped(self):
        overrides = [
            {"mode": "include", "target_type": "user_id",
             "target_value": "u2", "resolved_user_id": "u2"},   # 已在 base
            {"mode": "include", "target_type": "user_id",
             "target_value": "u9", "resolved_user_id": "u9"},   # 新增
        ]
        self.assertEqual(
            ["u1", "u2", "u3", "u9"],
            ro.apply_recipient_overrides(self._BASE, overrides))

    def test_include_by_name_uses_resolved_snapshot(self):
        overrides = [{"mode": "include", "target_type": "name",
                      "target_value": "赵六", "resolved_user_id": "u8"}]
        self.assertEqual(
            ["u1", "u2", "u3", "u8"],
            ro.apply_recipient_overrides(self._BASE, overrides))

    def test_include_by_name_without_snapshot_is_skipped(self):
        overrides = [{"mode": "include", "target_type": "name",
                      "target_value": "歧义人", "resolved_user_id": None}]
        self.assertEqual(
            ["u1", "u2", "u3"],
            ro.apply_recipient_overrides(self._BASE, overrides))

    def test_exclude_by_user_id_removes(self):
        overrides = [{"mode": "exclude", "target_type": "user_id",
                      "target_value": "u2", "resolved_user_id": None}]
        self.assertEqual(["u1", "u3"],
                         ro.apply_recipient_overrides(self._BASE, overrides))

    def test_exclude_by_name_removes_matching_base_row(self):
        overrides = [{"mode": "exclude", "target_type": "name",
                      "target_value": "李四", "resolved_user_id": None}]
        self.assertEqual(["u1", "u3"],
                         ro.apply_recipient_overrides(self._BASE, overrides))

    def test_include_then_exclude_same_id_exclude_wins(self):
        overrides = [
            {"mode": "include", "target_type": "user_id",
             "target_value": "u9", "resolved_user_id": "u9"},
            {"mode": "exclude", "target_type": "user_id",
             "target_value": "u9", "resolved_user_id": None},
        ]
        self.assertEqual(["u1", "u2", "u3"],
                         ro.apply_recipient_overrides(self._BASE, overrides))

    def test_accepts_plain_ids_and_dicts(self):
        self.assertEqual(["u1", "u2"],
                         ro.apply_recipient_overrides(["u1", "u2"], []))
        self.assertEqual(
            ["u1"], ro.apply_recipient_overrides([{"user_id": "u1"}], []))


class MergeTests(unittest.TestCase):

    def test_merge_applies_enabled_overrides(self):
        conn = _Conn({"`enabled` = 1": [
            {"mode": "exclude", "target_type": "name",
             "target_value": "李四", "resolved_user_id": None,
             "enabled": 1, "id": 1},
        ]})
        merged = ro.merge_recipient_overrides(
            conn, "robot-check-hangzhou", [("u1", "张三"), ("u2", "李四")])
        self.assertEqual(["u1"], merged)

    def test_merge_fail_soft_on_read_error(self):
        conn = _Conn(boom=True)
        merged = ro.merge_recipient_overrides(
            conn, "robot-check-hangzhou", [("u1", "张三"), ("u2", "李四")])
        self.assertEqual(["u1", "u2"], merged)


class ResolveTargetsTests(unittest.TestCase):

    def _conn(self, members):
        return _Conn({"dim_robot_member": members})

    def test_user_id_type_snapshots_itself(self):
        rows, unresolved = ro.resolve_targets(
            self._conn([]),
            [{"target_type": "user_id", "target_value": "u7"}])
        self.assertEqual("u7", rows[0]["resolved_user_id"])
        self.assertEqual([], unresolved)

    def test_name_type_resolves_unique_hit(self):
        rows, unresolved = ro.resolve_targets(
            self._conn([{"name": "张三", "user_id": "u1"}]),
            [{"target_type": "name", "target_value": "张三"}])
        self.assertEqual("u1", rows[0]["resolved_user_id"])
        self.assertEqual([], unresolved)

    def test_name_type_ambiguous_is_unresolved(self):
        rows, unresolved = ro.resolve_targets(
            self._conn([{"name": "张三", "user_id": "u1"},
                        {"name": "张三", "user_id": "u2"}]),
            [{"target_type": "name", "target_value": "张三"}])
        self.assertIsNone(rows[0]["resolved_user_id"])
        self.assertEqual(["张三"], unresolved)


class WriteTests(unittest.TestCase):

    def test_upsert_writes_audit_on_duplicate_key(self):
        conn = _Conn()
        ro.upsert_override(
            conn, "robot-check-hangzhou", "include", "user_id", "u9",
            actor="ops", note="补 @", resolved_user_id="u9")
        sqls = [sql for sql, _ in conn.executed]
        self.assertTrue(any("INSERT INTO `dim_recipient_override`" in s
                            and "ON DUPLICATE KEY UPDATE" in s for s in sqls))
        self.assertTrue(any("dim_recipient_override_audit" in s for s in sqls))
        audit = next(p for s, p in conn.executed
                     if "dim_recipient_override_audit" in s)
        self.assertEqual("ops", audit[0])
        self.assertEqual("add", audit[1])
        self.assertEqual("robot-check-hangzhou", audit[2])
        self.assertEqual(1, conn.commits)

    def test_upsert_rejects_bad_mode(self):
        with self.assertRaises(ro.RecipientOverrideError):
            ro.upsert_override(_Conn(), "s", "push", "user_id", "u9",
                               actor="ops")

    def test_upsert_rejects_bad_target_type(self):
        with self.assertRaises(ro.RecipientOverrideError):
            ro.upsert_override(_Conn(), "s", "include", "phone", "u9",
                               actor="ops")

    def test_upsert_rejects_empty_value(self):
        with self.assertRaises(ro.RecipientOverrideError):
            ro.upsert_override(_Conn(), "s", "include", "user_id", "  ",
                               actor="ops")

    def test_upsert_rejects_bad_service_id(self):
        with self.assertRaises(ro.RecipientOverrideError):
            ro.upsert_override(_Conn(), "bad id!", "include", "user_id", "u9",
                               actor="ops")

    def test_set_enabled_writes_audit(self):
        row = _row(id=5, enabled=0)
        conn = _Conn({"WHERE `id` = %s": [row]}, affected=1)
        self.assertTrue(ro.set_override_enabled(conn, 5, False, actor="ops"))
        sqls = [sql for sql, _ in conn.executed]
        self.assertTrue(any(s.startswith("UPDATE `dim_recipient_override`")
                            for s in sqls))
        audit = next(p for s, p in conn.executed
                     if "dim_recipient_override_audit" in s)
        self.assertEqual("disable", audit[1])

    def test_set_enabled_miss_returns_false_without_audit(self):
        conn = _Conn({"WHERE `id` = %s": [_row()]}, affected=0)
        self.assertFalse(ro.set_override_enabled(conn, 5, True, actor="ops"))
        self.assertFalse(any("dim_recipient_override_audit" in s
                             for s, _ in conn.executed))

    def test_delete_removes_and_audits(self):
        conn = _Conn({"WHERE `id` = %s": [_row(id=7)]}, affected=1)
        self.assertTrue(ro.delete_override(conn, 7, actor="ops"))
        sqls = [sql for sql, _ in conn.executed]
        self.assertTrue(any(s.startswith("DELETE FROM `dim_recipient_override`")
                            for s in sqls))
        audit = next(p for s, p in conn.executed
                     if "dim_recipient_override_audit" in s)
        self.assertEqual("delete", audit[1])

    def test_delete_miss_returns_false(self):
        conn = _Conn({}, affected=1)     # _load_row 无命中
        self.assertFalse(ro.delete_override(conn, 7, actor="ops"))

    def test_reset_service_clears_rows(self):
        conn = _Conn(affected=3)
        deleted = ro.reset_service_overrides(conn, "robot-hangzhou", actor="ops")
        self.assertEqual(3, deleted)
        sqls = [sql for sql, _ in conn.executed]
        self.assertTrue(any(s.startswith("DELETE FROM `dim_recipient_override`")
                            and "`service_id`" in s for s in sqls))
        audit = next(p for s, p in conn.executed
                     if "dim_recipient_override_audit" in s)
        self.assertEqual("reset", audit[1])

    def test_reset_rejects_bad_service_id(self):
        with self.assertRaises(ro.RecipientOverrideError):
            ro.reset_service_overrides(_Conn(), "bad id!", actor="ops")


if __name__ == "__main__":
    unittest.main()