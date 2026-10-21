# -*- coding: utf-8 -*-
"""org_proxy_grant（代填授权层）单测：关键字路由假库 + 纯函数断言。"""
import unittest

from common.public_data import org_proxy_grant as pg


class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0
        self._rows = []
        self._one = None

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        upper = sql.lstrip().upper()
        if upper.startswith(("UPDATE", "DELETE")):
            self.rowcount = self._conn.affected
            self._rows = []
            self._one = None
            return
        self._rows = self._conn.router(sql, params)
        self.rowcount = len(self._rows)
        self._one = self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._one

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    """按 SQL 关键字路由假库；boom=True 模拟读表故障。"""

    def __init__(self, grants=(), affected=1, boom=False):
        self.grants = list(grants)
        self.affected = affected
        self.boom = boom
        self.executed = []

    def router(self, sql, params):
        if self.boom:
            raise RuntimeError("db down")
        if "dim_org_proxy_grant_audit" in sql:
            return []
        if "dim_org_proxy_grant" not in sql:
            return []
        params = params or ()
        rows = list(self.grants)
        if "SELECT 1" in sql and "`target_region` = %s" in sql:
            uid, region, person = params
            rows = [g for g in rows
                    if g["proxy_user_id"] == uid and g.get("enabled", 1)
                    and g["target_region"] == region
                    and (not g.get("target_person")
                         or g["target_person"] == person)]
        elif "`proxy_user_id` = %s" in sql and params:
            rows = [g for g in rows
                    if g["proxy_user_id"] == params[0] and g.get("enabled", 1)]
        return [self._as(g) for g in rows]

    @staticmethod
    def _as(g):
        return {
            "id": g.get("id", 1),
            "proxy_user_id": g["proxy_user_id"],
            "proxy_name": g.get("proxy_name"),
            "target_region": g["target_region"],
            "target_person": g.get("target_person") or None,
            "enabled": g.get("enabled", 1),
            "note": g.get("note"),
            "updated_by": None,
            "updated_at": None,
        }

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


def _grant(proxy_user_id="u-sc", target_region="other", target_person="",
           proxy_name="沈聪", enabled=1):
    return {"proxy_user_id": proxy_user_id, "target_region": target_region,
            "target_person": target_person, "proxy_name": proxy_name,
            "enabled": enabled}


class DdlTests(unittest.TestCase):
    def test_ddl_creates_grant_and_audit_tables(self):
        statements = pg.org_proxy_grant_ddl_statements()
        self.assertEqual(2, len(statements))
        self.assertIn("dim_org_proxy_grant`", statements[0])
        self.assertIn("dim_org_proxy_grant_audit`", statements[1])


class WriteTests(unittest.TestCase):
    def test_upsert_inserts_and_audits_in_one_transaction(self):
        conn = _Conn()
        pg.upsert_proxy_grant(conn, "u-sc", "other", proxy_name="沈聪",
                              actor="ops")
        inserts = [s for s, _ in conn.executed if s.lstrip().startswith("INSERT")]
        self.assertTrue(any("dim_org_proxy_grant`" in s
                            and "audit" not in s for s in inserts))
        self.assertTrue(any("dim_org_proxy_grant_audit" in s for s in inserts))

    def test_upsert_rejects_bad_region_or_empty_user(self):
        with self.assertRaises(pg.OrgProxyGrantError):
            pg.upsert_proxy_grant(_Conn(), "  ", "other", actor="ops")
        with self.assertRaises(pg.OrgProxyGrantError):
            pg.upsert_proxy_grant(_Conn(), "u-sc", "Hang Zhou", actor="ops")

    def test_set_enabled_and_delete_audit(self):
        conn = _Conn(grants=[_grant()], affected=1)
        self.assertTrue(pg.set_proxy_grant_enabled(
            conn, "u-sc", "other", "", False, actor="ops"))
        self.assertTrue(pg.delete_proxy_grant(conn, "u-sc", "other", "",
                                              actor="ops"))
        actions = [p[1] for s, p in conn.executed
                   if "dim_org_proxy_grant_audit" in s]
        self.assertIn("disable", actions)
        self.assertIn("revoke", actions)


class PermissionTests(unittest.TestCase):
    def test_is_proxy_for_region_level_grant(self):
        conn = _Conn(grants=[_grant(target_person="")])
        # 整区域授权 → 该区域任何人
        self.assertTrue(pg.is_proxy_for(conn, "u-sc", region="other"))
        self.assertTrue(pg.is_proxy_for(conn, "u-sc", region="other",
                                        person="余云涛"))
        # 别的区域不行
        self.assertFalse(pg.is_proxy_for(conn, "u-sc", region="hangzhou"))

    def test_is_proxy_for_person_level_grant(self):
        conn = _Conn(grants=[_grant(target_person="余云涛")])
        self.assertTrue(pg.is_proxy_for(conn, "u-sc", region="other",
                                        person="余云涛"))
        # 收窄到人 → 同区域其他人不行
        self.assertFalse(pg.is_proxy_for(conn, "u-sc", region="other",
                                         person="李树军"))

    def test_is_proxy_for_fail_closed_on_error(self):
        self.assertFalse(pg.is_proxy_for(_Conn(boom=True), "u-sc",
                                         region="other"))

    def test_fetch_proxy_targets_returns_enabled(self):
        conn = _Conn(grants=[_grant(), _grant(target_region="hangzhou")])
        targets = pg.fetch_proxy_targets(conn, "u-sc")
        regions = {t["target_region"] for t in targets}
        self.assertEqual({"other", "hangzhou"}, regions)


if __name__ == "__main__":
    unittest.main()
