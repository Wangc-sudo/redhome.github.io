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


class ToggleDeleteTests(unittest.TestCase):

    def test_disable_region_writes_audit(self):
        conn = _Conn(affected=1)
        affected = oc.set_catalog_enabled(
            conn, "region", region_key="hangzhou", enabled=False, actor="ops")
        self.assertEqual(1, affected)
        sqls = [s for s, _ in conn.executed]
        self.assertTrue(any("UPDATE `dim_org_region`" in s
                            and "`enabled` = %s" in s for s in sqls))
        audit = _audit_params(conn)
        self.assertEqual("ops", audit[0])
        self.assertEqual("disable_region", audit[1])
        self.assertEqual("region", audit[2])
        self.assertEqual("hangzhou", audit[3])

    def test_enable_region_action_label(self):
        conn = _Conn(affected=1)
        oc.set_catalog_enabled(
            conn, "region", region_key="hangzhou", enabled=True, actor="ops")
        audit = _audit_params(conn)
        self.assertEqual("enable_region", audit[1])

    def test_disable_dept_requires_dept_name(self):
        conn = _Conn()
        with self.assertRaises(oc.OrgCatalogError):
            oc.set_catalog_enabled(
                conn, "dept", region_key="hangzhou", enabled=False,
                actor="ops")
        self.assertEqual(0, conn.commits)

    def test_disable_dept_writes_audit(self):
        conn = _Conn(affected=1)
        oc.set_catalog_enabled(
            conn, "dept", region_key="hangzhou", dept_name="杭州销售部",
            enabled=False, actor="ops")
        sqls = [s for s, _ in conn.executed]
        self.assertTrue(any("UPDATE `dim_org_dept`" in s for s in sqls))
        audit = _audit_params(conn)
        self.assertEqual("disable_dept", audit[1])
        self.assertEqual("hangzhou", audit[3])
        self.assertEqual("杭州销售部", audit[5])

    def test_delete_region_cascades_depts(self):
        conn = _Conn(affected=1)
        oc.delete_catalog_entry(
            conn, "region", region_key="chengdu", actor="ops")
        sqls = [s for s, _ in conn.executed]
        # 先删该区域下部门，再删区域
        dept_del = next(i for i, s in enumerate(sqls)
                        if "DELETE FROM `dim_org_dept`" in s
                        and "region_key" in s)
        region_del = next(i for i, s in enumerate(sqls)
                          if "DELETE FROM `dim_org_region`" in s)
        self.assertLess(dept_del, region_del)
        audit = _audit_params(conn)
        self.assertEqual("delete_region", audit[1])
        self.assertEqual("chengdu", audit[3])

    def test_delete_dept_requires_name(self):
        conn = _Conn()
        with self.assertRaises(oc.OrgCatalogError):
            oc.delete_catalog_entry(
                conn, "dept", region_key="hangzhou", actor="ops")
        self.assertEqual(0, conn.commits)

    def test_delete_dept_writes_audit(self):
        conn = _Conn(affected=1)
        oc.delete_catalog_entry(
            conn, "dept", region_key="hangzhou", dept_name="杭州销售部",
            actor="ops")
        audit = _audit_params(conn)
        self.assertEqual("delete_dept", audit[1])
        self.assertEqual("hangzhou", audit[3])
        self.assertEqual("杭州销售部", audit[5])

    def test_invalid_target_type_rejected(self):
        conn = _Conn()
        with self.assertRaises(oc.OrgCatalogError):
            oc.set_catalog_enabled(
                conn, "bogus", region_key="hangzhou", enabled=False,
                actor="ops")


class MigrationRegistrationTests(unittest.TestCase):

    def test_catalog_migration_registered(self):
        from common.public_data import live_migrations as lm
        versions = {entry[0]: entry for entry in lm._MIGRATIONS}
        self.assertIn("mart-ops-org-catalog-v1", versions)
        version, schema, statements = versions["mart-ops-org-catalog-v1"]
        self.assertEqual("mart", schema)
        self.assertEqual(oc.org_catalog_ddl_statements(), statements)


class RegionRegistryMigrationTests(unittest.TestCase):
    """mart-ops-region-registry-v1：运行属性列 + 区域并集回填（D2 区域真源）。"""

    def test_statements_alter_columns_then_insert_all_regions(self):
        statements = oc.org_catalog_region_run_v1_statements()
        self.assertEqual(18, len(statements))
        alters, inserts = statements[:9], statements[9:]
        for sql in alters:
            self.assertTrue(sql.startswith("ALTER TABLE `dim_org_region`"))
        for column in ("display", "table_url", "robot_code",
                       "open_conversation_id", "remind_hour", "check_hour",
                       "dept_order", "leaderboard_url", "store_exclude"):
            self.assertTrue(any(f"`{column}`" in s for s in alters), column)
        for sql in inserts:
            self.assertIn("INSERT IGNORE INTO `dim_org_region`", sql)
        for key in ("hangzhou", "shaoxing", "vanke", "junpin", "qudao",
                    "offline_all", "dining", "hq", "other"):
            self.assertTrue(any(f"'{key}'" in s for s in inserts),
                            f"{key} missing from registry backfill")

    def test_region_registry_migration_registered(self):
        from common.public_data import live_migrations as lm
        registered = {v: (t, s) for v, t, s in lm._MIGRATIONS}
        self.assertIn("mart-ops-region-registry-v1", registered)
        target, statements = registered["mart-ops-region-registry-v1"]
        self.assertEqual("mart", target)
        self.assertEqual(oc.org_catalog_region_run_v1_statements(), statements)


class RelayTests(unittest.TestCase):
    """播报改投测试区（2026-10-19 用户裁决）：relay_to 列 + set/fetch。"""

    def test_migration_shape(self):
        statements = oc.org_catalog_relay_v1_statements()
        self.assertEqual(2, len(statements))
        self.assertIn("relay_to", statements[0])
        self.assertIn("ALTER TABLE", statements[0])
        # 测试区注册行（INSERT IGNORE 不覆盖人工行）
        self.assertIn("'test'", statements[1])
        self.assertIn("INSERT IGNORE", statements[1])

    def test_set_and_fetch_relay(self):
        conn = _Conn({"relay_to` IS NOT NULL": [
            {"region_key": "hangzhou", "relay_to": "test"},
            {"region_key": "vanke", "relay_to": "test"},
        ]})
        affected = oc.set_region_relay(
            conn, "hangzhou", "test", actor="admin")
        self.assertEqual(1, affected)
        # 审计同行（action=set_relay）
        params = _audit_params(conn)
        self.assertIn("set_relay", params)
        self.assertEqual(
            {"hangzhou": "test", "vanke": "test"},
            oc.fetch_relay_map(conn),
        )

    def test_clear_relay(self):
        conn = _Conn()
        affected = oc.set_region_relay(conn, "hangzhou", None, actor="admin")
        self.assertEqual(1, affected)
        update_sql, params = next(
            (s, p) for s, p in conn.executed if s.lstrip().startswith("UPDATE"))
        self.assertIsNone(params[0])

    def test_relay_guards(self):
        conn = _Conn()
        with self.assertRaises(oc.OrgCatalogError):
            oc.set_region_relay(conn, "hangzhou", "shaoxing", actor="a")
        with self.assertRaises(oc.OrgCatalogError):
            oc.set_region_relay(conn, "test", "test", actor="a")
        with self.assertRaises(oc.OrgCatalogError):
            oc.set_region_relay(conn, "BAD KEY", "test", actor="a")
        # 未启用/未登记区域：affected=0
        conn = _Conn(affected=0)
        self.assertEqual(
            0, oc.set_region_relay(conn, "ghost", "test", actor="a"))


class BroadcastWriteTests(unittest.TestCase):
    """播报配置写面（2026-10-26 权威源迁 DB 页面入口）。"""

    def test_text_int_json_fields_write_with_audit(self):
        conn = _Conn()
        changed = oc.update_region_broadcast(conn, "hangzhou", {
            "robot_code": "rc-new",
            "remind_hour": 19,
            "dept_order": ["杭中", "滨萧"],
            "monthly_targets": {"万科·零售": 10.0},
        }, actor="ops")
        self.assertEqual(
            ("dept_order", "monthly_targets", "remind_hour", "robot_code"),
            changed)
        update_sql, params = next(
            (s, p) for s, p in conn.executed
            if s.startswith("UPDATE `dim_org_region`"))
        self.assertIn("`robot_code`=%s", update_sql)
        self.assertIn('["杭中", "滨萧"]',
                      params[params.index("rc-new") + 2])
        audit = _audit_params(conn)
        self.assertIn("update_broadcast", str(audit))

    def test_null_clears_column(self):
        conn = _Conn()
        oc.update_region_broadcast(conn, "hangzhou",
                                   {"broadcast_exclude": None}, actor="ops")
        _, params = next((s, p) for s, p in conn.executed
                         if s.startswith("UPDATE"))
        self.assertIn(None, params)

    def test_unknown_field_rejected(self):
        conn = _Conn()
        with self.assertRaises(oc.OrgCatalogError):
            oc.update_region_broadcast(conn, "hangzhou",
                                       {"password": "x"}, actor="ops")
        self.assertEqual(0, conn.commits)

    def test_unregistered_region_rejected(self):
        conn = _Conn(affected=0)
        with self.assertRaises(oc.OrgCatalogError):
            oc.update_region_broadcast(conn, "ghost",
                                       {"robot_code": "x"}, actor="ops")

    def test_hour_out_of_range_rejected(self):
        conn = _Conn()
        with self.assertRaises(oc.OrgCatalogError):
            oc.update_region_broadcast(conn, "hangzhou",
                                       {"remind_hour": 25}, actor="ops")


class RegionDeptWriteTests(unittest.TestCase):
    """归属映射写面（2026-10-26）：全量替换 + 增删差集审计。"""

    def test_replace_writes_rows_and_diff_audit(self):
        conn = _Conn({"FROM `dim_org_region_dept`": [{"dept_id": 1049728636}]})
        result = oc.replace_region_depts(
            conn, "hangzhou", [1049728636, 1050062512], actor="ops")
        self.assertEqual([1050062512], result["added"])
        self.assertEqual([], result["removed"])
        sqls = [s for s, _ in conn.executed]
        self.assertTrue(any(s.startswith("DELETE FROM `dim_org_region_dept`")
                            for s in sqls))
        inserts = [p for s, p in conn.executed
                   if s.startswith("INSERT INTO `dim_org_region_dept`")]
        self.assertEqual(2, len(inserts))
        self.assertEqual(("hangzhou", 1049728636, 0), inserts[0][:3])
        self.assertEqual(("hangzhou", 1050062512, 1), inserts[1][:3])
        audit = _audit_params(conn)
        self.assertIn("set_region_depts", str(audit))

    def test_empty_list_clears_region(self):
        conn = _Conn({"FROM `dim_org_region_dept`": [{"dept_id": 9}]})
        result = oc.replace_region_depts(conn, "hangzhou", [], actor="ops")
        self.assertEqual([9], result["removed"])

    def test_bad_ids_rejected(self):
        conn = _Conn()
        for bad in ([1, 1], ["x"], [0], [-3]):
            with self.assertRaises(oc.OrgCatalogError):
                oc.replace_region_depts(conn, "hangzhou", bad, actor="ops")
        self.assertEqual(0, conn.commits)


if __name__ == "__main__":
    unittest.main()