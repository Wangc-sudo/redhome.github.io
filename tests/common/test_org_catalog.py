# -*- coding: utf-8 -*-
"""组织目录登记层（dim_org_region / dim_org_dept，2026-10-13 P7）单测。

覆盖面：DDL（区域 + 部门 + 审计三表）/ 读面 fetch_catalog（仅启用行、
fail-soft 回空目录）/ 写面 upsert_region / upsert_dept（同事务审计、
参数校验、重复登记去重）/ 迁移注册。
"""

import unittest

from common.public_data import org_catalog as oc


def _region_row(**kwargs):
    base = {
        "id": 1, "region_key": "hangzhou", "label": "杭州", "sort_order": 0,
        "enabled": 1, "updated_by": "ops", "updated_at": None,
    }
    base.update(kwargs)
    return tuple(base[k] for k in oc._REGION_KEYS)


def _dept_row(**kwargs):
    base = {
        "id": 1, "region_key": "hangzhou", "dept_id": None,
        "dept_name": "杭州销售部", "sort_order": 0, "enabled": 1,
        "updated_by": "ops", "updated_at": None,
    }
    base.update(kwargs)
    return tuple(base[k] for k in oc._DEPT_KEYS)


class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0
        self._rows = []

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        upper = sql.lstrip().upper()
        if upper.startswith(("UPDATE", "DELETE")):
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


def _audit_params(conn, marker="dim_org_catalog_audit"):
    return next(p for s, p in conn.executed if marker in s)


class DdlTests(unittest.TestCase):

    def test_ddl_creates_three_tables(self):
        statements = oc.org_catalog_ddl_statements()
        self.assertEqual(3, len(statements))
        self.assertIn("dim_org_region`", statements[0])
        self.assertIn("uk_org_region", statements[0])
        self.assertIn("dim_org_dept`", statements[1])
        self.assertIn("uk_org_dept", statements[1])
        self.assertIn("dim_org_catalog_audit`", statements[2])
        for sql in statements:
            self.assertIn("CREATE TABLE IF NOT EXISTS", sql)


class FetchCatalogTests(unittest.TestCase):

    def test_only_enabled_rows(self):
        conn = _Conn({
            "FROM `dim_org_region`": [_region_row()],
            "FROM `dim_org_dept`": [_dept_row()],
        })
        catalog = oc.fetch_catalog(conn)
        self.assertEqual(["hangzhou"],
                         [r["region_key"] for r in catalog["regions"]])
        self.assertEqual("杭州", catalog["regions"][0]["label"])
        self.assertEqual("杭州销售部",
                         catalog["depts"][0]["dept_name"])
        sqls = [s for s, _ in conn.executed]
        self.assertTrue(any("`enabled` = 1" in s
                            and "FROM `dim_org_region`" in s for s in sqls))
        self.assertTrue(any("`enabled` = 1" in s
                            and "FROM `dim_org_dept`" in s for s in sqls))

    def test_read_failure_is_fail_soft(self):
        conn = _Conn(boom=True)
        catalog = oc.fetch_catalog(conn)
        self.assertEqual([], catalog["regions"])
        self.assertEqual([], catalog["depts"])


class ValidationTests(unittest.TestCase):

    def test_rejects_bad_region_key(self):
        conn = _Conn()
        with self.assertRaises(oc.OrgCatalogError):
            oc.upsert_region(conn, "Bad Key", "杭州", actor="ops")
        self.assertEqual(0, conn.commits)

    def test_rejects_blank_label(self):
        conn = _Conn()
        with self.assertRaises(oc.OrgCatalogError):
            oc.upsert_region(conn, "hangzhou", "  ", actor="ops")
        self.assertEqual(0, conn.commits)

    def test_rejects_blank_dept_name(self):
        conn = _Conn()
        with self.assertRaises(oc.OrgCatalogError):
            oc.upsert_dept(conn, "hangzhou", "", actor="ops")
        self.assertEqual(0, conn.commits)


class WriteTests(unittest.TestCase):

    def test_upsert_region_writes_audit_same_transaction(self):
        conn = _Conn()
        result = oc.upsert_region(conn, "hangzhou", "杭州", actor="ops")
        self.assertEqual(("hangzhou", "杭州", 0), result)
        sqls = [s for s, _ in conn.executed]
        self.assertTrue(any("INSERT INTO `dim_org_region`" in s
                            and "ON DUPLICATE KEY UPDATE" in s for s in sqls))
        audit = _audit_params(conn)
        self.assertEqual("ops", audit[0])
        self.assertEqual("add_region", audit[1])
        self.assertEqual("region", audit[2])
        self.assertEqual("hangzhou", audit[3])
        self.assertEqual(1, conn.commits)

    def test_upsert_dept_allows_placeholder_without_id(self):
        conn = _Conn()
        result = oc.upsert_dept(
            conn, "hangzhou", "杭州销售部", actor="ops")
        self.assertEqual(("hangzhou", None, "杭州销售部", 0), result)
        sqls = [s for s, _ in conn.executed]
        self.assertTrue(any("INSERT INTO `dim_org_dept`" in s
                            and "ON DUPLICATE KEY UPDATE" in s for s in sqls))
        audit = _audit_params(conn)
        self.assertEqual("add_dept", audit[1])
        self.assertEqual("dept", audit[2])
        self.assertEqual("hangzhou", audit[3])
        self.assertEqual("杭州销售部", audit[5])

    def test_upsert_dept_keeps_optional_dept_id(self):
        conn = _Conn()
        result = oc.upsert_dept(
            conn, "hangzhou", "杭州销售部", dept_id="1050062512", actor="ops")
        self.assertEqual(("hangzhou", "1050062512", "杭州销售部", 0), result)


class MigrationRegistrationTests(unittest.TestCase):

    def test_catalog_migration_registered(self):
        from common.public_data import live_migrations as lm
        versions = {entry[0]: entry for entry in lm._MIGRATIONS}
        self.assertIn("mart-ops-org-catalog-v1", versions)
        version, schema, statements = versions["mart-ops-org-catalog-v1"]
        self.assertEqual("mart", schema)
        self.assertEqual(oc.org_catalog_ddl_statements(), statements)


if __name__ == "__main__":
    unittest.main()