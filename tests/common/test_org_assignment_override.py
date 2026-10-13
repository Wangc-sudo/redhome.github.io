# -*- coding: utf-8 -*-
"""组织归属覆盖层（dim_org_assignment_override，2026-10-13 P6）单测。

覆盖面：DDL（表 + 审计表）/ 纯函数合并 apply_region_overrides（剔除 / 补入 /
换部门 / region=None 移出 / 按姓名排序）/ 消费侧唯一入口 merge_region_members
（fail-soft 回退 + 移入在职校验）/ 写面 CRUD 与审计同事务、参数校验。
"""

import unittest

from common.public_data import org_assignment_override as oa


_ROW_KEYS = oa._OVERRIDE_KEYS


def _row(**kwargs):
    """按 :data:`_OVERRIDE_KEYS` 造一行（默认启用、region=hangzhou）。"""
    base = {
        "id": 1, "user_id": "u1", "name": "张三", "region": "hangzhou",
        "dept_id": "d1", "dept_name": "门店一部", "enabled": 1, "note": None,
        "updated_by": "ops", "updated_at": None,
    }
    base.update(kwargs)
    return tuple(base[k] for k in _ROW_KEYS)


def _override(user_id, region, dept_id=None, dept_name=None, name="", enabled=1):
    """纯函数入参形态的覆盖 dict（:func:`fetch_overrides` 的产出形）。"""
    return {
        "user_id": user_id, "name": name, "region": region,
        "dept_id": dept_id, "dept_name": dept_name, "enabled": enabled,
    }


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
    """按 SQL 关键字路由假库；boom=True 模拟读表故障。"""

    def __init__(self, rows_by_keyword=None, *, affected=1, boom=False):
        self._rows_by_keyword = rows_by_keyword or {}
        self.affected = affected
        self.boom = boom
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

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
        self.rollbacks += 1


def _audit_params(conn, marker):
    return next(p for s, p in conn.executed if marker in s)


class DdlTests(unittest.TestCase):

    def test_ddl_creates_both_tables(self):
        statements = oa.org_assignment_override_ddl_statements()
        self.assertEqual(2, len(statements))
        self.assertIn("dim_org_assignment_override`", statements[0])
        self.assertIn("uk_org_assignment_user", statements[0])
        self.assertIn("dim_org_assignment_override_audit`", statements[1])
        for sql in statements:
            self.assertIn("CREATE TABLE IF NOT EXISTS", sql)


class FetchOverridesTests(unittest.TestCase):

    def test_only_enabled_rows_keyed_by_user_id(self):
        conn = _Conn({"`enabled` = 1": [
            _row(user_id="u9", name="钱七"),
            _row(user_id="u1", name="张三"),
        ]})
        overrides = oa.fetch_overrides(conn)
        self.assertEqual({"u9", "u1"}, set(overrides))
        self.assertEqual("钱七", overrides["u9"]["name"])
        sql, _ = conn.executed[0]
        self.assertIn("`enabled` = 1", sql)
        self.assertIn("ORDER BY `user_id`", sql)

    def test_all_overrides_can_include_disabled(self):
        conn = _Conn({"FROM `dim_org_assignment_override`": [
            _row(user_id="u1", enabled=0),
        ]})
        rows = oa.fetch_all_overrides(conn)
        self.assertEqual(1, len(rows))
        self.assertEqual(0, rows[0]["enabled"])


class ApplyRegionOverridesTests(unittest.TestCase):

    _BASE = [
        {"user_id": "u1", "name": "张三", "region": "hangzhou",
         "dept_id": "d1", "dept_name": "门店一部"},
        {"user_id": "u2", "name": "李四", "region": "hangzhou",
         "dept_id": "d2", "dept_name": "门店二部"},
    ]

    def test_no_overrides_keeps_base(self):
        out = oa.apply_region_overrides(self._BASE, {}, region="hangzhou")
        self.assertEqual(["u1", "u2"], [r["user_id"] for r in out])
        self.assertEqual("d1", out[0]["dept_id"])

    def test_override_changes_dept(self):
        overrides = {"u1": _override("u1", "hangzhou", "d9", "总经办")}
        out = oa.apply_region_overrides(self._BASE, overrides, region="hangzhou")
        row = next(r for r in out if r["user_id"] == "u1")
        self.assertEqual("d9", row["dept_id"])
        self.assertEqual("总经办", row["dept_name"])

    def test_override_other_region_removes(self):
        overrides = {"u1": _override("u1", "shaoxing", "d9", "绍兴店")}
        out = oa.apply_region_overrides(self._BASE, overrides, region="hangzhou")
        self.assertEqual(["u2"], [r["user_id"] for r in out])

    def test_override_region_none_moves_out(self):
        overrides = {"u1": _override("u1", None)}
        out = oa.apply_region_overrides(self._BASE, overrides, region="hangzhou")
        self.assertEqual(["u2"], [r["user_id"] for r in out])

    def test_override_appends_inbound(self):
        overrides = {"u9": _override("u9", "hangzhou", "d1", "门店一部",
                                     name="钱七")}
        out = oa.apply_region_overrides(self._BASE, overrides, region="hangzhou")
        # 按姓名排序：张三(u1) < 李四(u2) < 钱七(u9)
        self.assertEqual(["u1", "u2", "u9"], [r["user_id"] for r in out])
        self.assertEqual("钱七", out[2]["name"])

    def test_inbound_for_other_region_not_appended(self):
        overrides = {"u9": _override("u9", "shaoxing", name="钱七")}
        out = oa.apply_region_overrides(self._BASE, overrides, region="hangzhou")
        self.assertEqual(["u1", "u2"], [r["user_id"] for r in out])

    def test_result_sorted_by_name(self):
        base = [
            {"user_id": "u2", "name": "李四", "region": "hangzhou",
             "dept_id": "d", "dept_name": "x"},
            {"user_id": "u1", "name": "张三", "region": "hangzhou",
             "dept_id": "d", "dept_name": "x"},
        ]
        out = oa.apply_region_overrides(base, {}, region="hangzhou")
        self.assertEqual(["u1", "u2"], [r["user_id"] for r in out])


class MergeRegionMembersTests(unittest.TestCase):

    def test_merge_applies_overrides(self):
        conn = _Conn({
            "`enabled` = 1": [_row(user_id="u9", name="钱七",
                                   region="hangzhou", dept_id="d1",
                                   dept_name="门店一部")],
            "`is_active` = 1": [("u9",)],
        })
        base = [{"user_id": "u1", "name": "张三", "region": "hangzhou",
                 "dept_id": "d2", "dept_name": "门店二部"}]
        out = oa.merge_region_members(conn, "hangzhou", base)
        self.assertEqual(["u1", "u9"], [r["user_id"] for r in out])

    def test_merge_fail_soft_on_read_error(self):
        conn = _Conn(boom=True)
        base = [{"user_id": "u1", "name": "张三", "region": "hangzhou",
                 "dept_id": "d2", "dept_name": "门店二部"}]
        out = oa.merge_region_members(conn, "hangzhou", base)
        self.assertEqual(["u1"], [r["user_id"] for r in out])

    def test_merge_drops_inactive_inbound(self):
        conn = _Conn({
            "`enabled` = 1": [
                _row(user_id="u9", name="钱七", region="hangzhou"),
                _row(user_id="u8", name="离职者", region="hangzhou"),
            ],
            "`is_active` = 1": [("u9",)],       # u8 不在职
        })
        out = oa.merge_region_members(conn, "hangzhou", [])
        self.assertEqual(["u9"], [r["user_id"] for r in out])

    def test_merge_no_override_skips_active_probe(self):
        conn = _Conn({})
        base = [{"user_id": "u1", "name": "张三", "region": "hangzhou",
                 "dept_id": "d2", "dept_name": "门店二部"}]
        out = oa.merge_region_members(conn, "hangzhou", base)
        self.assertEqual(["u1"], [r["user_id"] for r in out])
        self.assertFalse(any("`is_active` = 1" in s for s, _ in conn.executed))


class WriteTests(unittest.TestCase):

    def test_upsert_writes_audit_in_same_transaction(self):
        conn = _Conn()
        result = oa.upsert_assignment(
            conn, "u9", "hangzhou", dept_id="d1", dept_name="门店一部",
            name="钱七", actor="ops")
        self.assertEqual(("u9", "hangzhou", "d1", "门店一部"), result)
        sqls = [s for s, _ in conn.executed]
        self.assertTrue(any("INSERT INTO `dim_org_assignment_override`" in s
                            and "ON DUPLICATE KEY UPDATE" in s for s in sqls))
        audit = _audit_params(conn, "dim_org_assignment_override_audit")
        self.assertEqual("ops", audit[0])
        self.assertEqual("assign", audit[1])
        self.assertEqual("u9", audit[2])
        self.assertEqual(1, conn.commits)

    def test_upsert_null_region_notes_moved_out(self):
        conn = _Conn()
        oa.upsert_assignment(conn, "u9", None, actor="ops")
        audit = _audit_params(conn, "dim_org_assignment_override_audit")
        self.assertIsNone(audit[4])
        self.assertEqual("移出全部区域", audit[7])

    def test_upsert_rejects_empty_user_id(self):
        with self.assertRaises(oa.OrgAssignmentOverrideError):
            oa.upsert_assignment(_Conn(), "  ", "hangzhou", actor="ops")

    def test_upsert_rejects_bad_region_key(self):
        with self.assertRaises(oa.OrgAssignmentOverrideError):
            oa.upsert_assignment(_Conn(), "u9", "Hang Zhou", actor="ops")

    def test_upsert_rejects_overlong_note(self):
        with self.assertRaises(oa.OrgAssignmentOverrideError):
            oa.upsert_assignment(_Conn(), "u9", "hangzhou", actor="ops",
                                 note="x" * 256)

    def test_set_enabled_writes_audit(self):
        conn = _Conn({"WHERE `user_id` = %s": [_row(user_id="u9", enabled=0)]},
                     affected=1)
        self.assertTrue(
            oa.set_assignment_enabled(conn, "u9", False, actor="ops"))
        audit = _audit_params(conn, "dim_org_assignment_override_audit")
        self.assertEqual("disable", audit[1])
        self.assertEqual(1, conn.commits)

    def test_set_enabled_miss_returns_false_without_audit(self):
        conn = _Conn({}, affected=0)
        self.assertFalse(
            oa.set_assignment_enabled(conn, "u9", True, actor="ops"))
        self.assertFalse(any("audit" in s for s, _ in conn.executed))

    def test_delete_removes_and_audits(self):
        conn = _Conn({"WHERE `user_id` = %s": [_row(user_id="u9")]}, affected=1)
        self.assertTrue(oa.delete_assignment(conn, "u9", actor="ops"))
        sqls = [s for s, _ in conn.executed]
        self.assertTrue(any(s.startswith("DELETE FROM `dim_org_assignment_override`")
                            for s in sqls))
        audit = _audit_params(conn, "dim_org_assignment_override_audit")
        self.assertEqual("delete", audit[1])

    def test_delete_miss_returns_false(self):
        conn = _Conn({}, affected=1)
        self.assertFalse(oa.delete_assignment(conn, "u9", actor="ops"))
        self.assertFalse(any("audit" in s for s, _ in conn.executed))

    def test_reset_clears_all_and_audits(self):
        conn = _Conn(affected=4)
        deleted = oa.reset_assignments(conn, actor="ops")
        self.assertEqual(4, deleted)
        sqls = [s for s, _ in conn.executed]
        self.assertTrue(any(s.startswith("DELETE FROM `dim_org_assignment_override`")
                            and "WHERE" not in s for s in sqls))
        audit = _audit_params(conn, "dim_org_assignment_override_audit")
        self.assertEqual("reset", audit[1])
        self.assertEqual("*", audit[2])


if __name__ == "__main__":
    unittest.main()