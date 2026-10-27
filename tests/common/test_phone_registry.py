# -*- coding: utf-8 -*-
"""公司电信号码台账（dim_phone_number / dim_phone_binding）单测。

2026-10-26 收尾补齐：模块此前只有 DDL 门禁覆盖。本文件钉住——
校验规则（号码形态 / 类型枚举 / 固话不认领）、单条绑定写入与停用、
台账页/绑定页读面的 SQL 形态（关键词 LIKE、JOIN 持卡人）、审计同事务。
"""

import unittest

from common.public_data import phone_registry as pr


class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0
        self._rows = []

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        upper = sql.lstrip().upper()
        if upper.startswith(("UPDATE", "DELETE", "INSERT")):
            self.rowcount = self._conn.affected
            self._rows = []
        else:
            self._rows = self._conn.router(sql, params)
            self.rowcount = len(self._rows)

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _Conn:
    def __init__(self, rows_by_keyword=None, *, affected=1):
        self._rows_by_keyword = rows_by_keyword or {}
        self.affected = affected
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def router(self, sql, params):
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


class ValidateTests(unittest.TestCase):

    def test_rejects_bad_phone(self):
        for bad in ("", "  ", "abc", "手机号138"):
            with self.subTest(bad=bad):
                with self.assertRaises(pr.PhoneRegistryError):
                    pr.validate_phone_record(bad)

    def test_rejects_unknown_type_and_field(self):
        with self.assertRaises(pr.PhoneRegistryError):
            pr.validate_phone_record("13300001111", phone_type="靓号")
        with self.assertRaises(pr.PhoneRegistryError):
            pr.validate_phone_record("13300001111", password="x")  # 无密码列

    def test_landline_never_needs_claim(self):
        record = pr.validate_phone_record(
            "057188001122", phone_type="固话", needs_claim=True)
        self.assertFalse(record.needs_claim)

    def test_mobile_defaults(self):
        record = pr.validate_phone_record("13300001111")
        self.assertEqual("未知", record.phone_type)
        self.assertTrue(record.enabled)


class BindingWriteTests(unittest.TestCase):

    def test_upsert_binding_writes_and_audits_same_transaction(self):
        conn = _Conn()
        pr.upsert_phone_binding(
            conn, "13300001111", "快手", "习水村酒类专营店",
            note="主账号", actor="ops")
        sqls = [sql for sql, _ in conn.executed]
        self.assertTrue(any("INSERT INTO `dim_phone_binding`" in s
                            and "ON DUPLICATE KEY UPDATE" in s for s in sqls))
        self.assertTrue(any("dim_phone_number_audit" in s for s in sqls))
        audit = next(p for s, p in conn.executed
                     if "dim_phone_number_audit" in s)
        self.assertEqual(("ops", "binding_save", "13300001111"),
                         (audit[0], audit[1], audit[2]))
        self.assertEqual(1, conn.commits)
        self.assertEqual(0, conn.rollbacks)

    def test_upsert_binding_validates_inputs(self):
        conn = _Conn()
        for kwargs in (
            dict(phone_no="bad", channel_category="快手",
                 platform_account="x"),
            dict(phone_no="13300001111", channel_category="",
                 platform_account="x"),
            dict(phone_no="13300001111", channel_category="快手",
                 platform_account="  "),
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(pr.PhoneRegistryError):
                    pr.upsert_phone_binding(conn, actor="ops", **kwargs)
        self.assertEqual(0, conn.commits)

    def test_set_binding_enabled_audits_only_when_changed(self):
        conn = _Conn(affected=1)
        changed = pr.set_binding_enabled(conn, 5, False, actor="ops")
        self.assertEqual(1, changed)
        audit = next(p for s, p in conn.executed
                     if "dim_phone_number_audit" in s)
        self.assertEqual("disable", audit[1])
        self.assertEqual("binding#5", audit[2])

        quiet = _Conn(affected=0)
        self.assertEqual(0, pr.set_binding_enabled(quiet, 9, True,
                                                   actor="ops"))
        self.assertFalse(any("dim_phone_number_audit" in s
                             for s, _ in quiet.executed))


class PhoneToggleTests(unittest.TestCase):

    def test_set_phone_enabled_update_and_audit(self):
        conn = _Conn(affected=1)
        changed = pr.set_phone_enabled(conn, "13300001111", False,
                                       actor="ops")
        self.assertEqual(1, changed)
        update = next((s, p) for s, p in conn.executed
                      if s.lstrip().startswith("UPDATE"))
        self.assertIn("dim_phone_number" , update[0])
        self.assertEqual((0, "ops"), (update[1][0], update[1][1]))
        audit = next(p for s, p in conn.executed
                     if "dim_phone_number_audit" in s)
        self.assertEqual(("ops", "disable", "13300001111"),
                         (audit[0], audit[1], audit[2]))


class ReadSurfaceTests(unittest.TestCase):

    def test_phone_list_keyword_filters_seven_columns(self):
        conn = _Conn({"FROM `dim_phone_number`": []})
        pr.fetch_phone_list(conn, keyword="吴昱洁")
        sql, params = conn.executed[0]
        self.assertEqual(7, sql.count("LIKE %s"))
        self.assertEqual(["%吴昱洁%"] * 7, list(params))

    def test_phone_list_unclaimed_only(self):
        conn = _Conn()
        pr.fetch_phone_list(conn, unclaimed_only=True)
        sql, _ = conn.executed[0]
        self.assertIn("`needs_claim` = 1", sql)

    def test_phone_list_no_filter_plain_query(self):
        conn = _Conn({"FROM `dim_phone_number`": [
            {"phone_no": "13300001111", "actual_user": "张三"},
        ]})
        rows = pr.fetch_phone_list(conn)
        sql, params = conn.executed[0]
        self.assertNotIn("WHERE", sql)
        self.assertEqual((), params)
        self.assertEqual("13300001111", rows[0]["phone_no"])

    def test_bindings_join_holder_and_keyword(self):
        conn = _Conn({"FROM `dim_phone_binding`": [
            {"id": 5, "platform_account": "习水村酒类专营店",
             "actual_user": "陈典潮"},
        ]})
        rows = pr.fetch_bindings(conn, keyword="习水村")
        sql, params = conn.executed[0]
        self.assertIn("LEFT JOIN `dim_phone_number`", sql)
        self.assertEqual(3, sql.count("LIKE %s"))
        self.assertEqual(["%习水村%"] * 3, list(params))
        self.assertEqual("陈典潮", rows[0]["actual_user"])

    def test_bindings_no_keyword_returns_all_including_disabled(self):
        conn = _Conn()
        pr.fetch_bindings(conn)
        sql, _ = conn.executed[0]
        # 管理面读面要含停用行（不做 enabled=1 过滤）
        self.assertNotIn("enabled` = 1", sql)

    def test_fetch_phone_audit_orders_desc_with_limit(self):
        conn = _Conn()
        pr.fetch_phone_audit(conn, limit=50)
        sql, params = conn.executed[0]
        self.assertIn("ORDER BY `created_at` DESC", sql)
        self.assertEqual((50,), params)


class SearchRegistryTests(unittest.TestCase):
    """search_registry（kb-qa phone_lookup 工具读面，2026-10-26）。

    精确映射场景：纯数字=号码片段（尾号）匹配，其余=持卡人/开户名/设备/
    部门模糊匹配 + 命中人名下绑定 IN 展开；只回启用行。
    """

    def test_digit_keyword_is_phone_fragment_match(self):
        conn = _Conn()
        pr.search_registry(conn, "5690")
        sql, params = conn.executed[0]
        self.assertIn("`phone_no` LIKE %s", sql)
        self.assertIn("`enabled` = 1", sql)
        self.assertEqual(("%5690%", 5), params)
        sql2, params2 = conn.executed[1]
        self.assertIn("b.`phone_no` LIKE %s", sql2)
        self.assertIn("`enabled` = 1", sql2)
        self.assertEqual(("%5690%", 10), params2)

    def test_name_keyword_matches_holder_and_expands_in(self):
        conn = _Conn({"FROM `dim_phone_number`": [
            {"phone_no": "13300001111", "actual_user": "张三"},
        ]})
        result = pr.search_registry(conn, "张三")
        self.assertEqual(1, len(result["phones"]))
        sql, _ = conn.executed[0]
        self.assertIn("`actual_user` LIKE %s", sql)
        self.assertIn("`account_name` LIKE %s", sql)
        sql2, params2 = conn.executed[1]
        self.assertIn("b.`platform_account` LIKE %s", sql2)
        self.assertIn("b.`phone_no` IN (%s)", sql2)
        self.assertEqual(["%张三%", "%张三%", "13300001111", 10],
                         list(params2))

    def test_name_keyword_without_phones_skips_in(self):
        conn = _Conn()
        pr.search_registry(conn, "查无此人")
        sql2, params2 = conn.executed[1]
        self.assertNotIn(" IN (", sql2)
        self.assertEqual(["%查无此人%", "%查无此人%", 10], list(params2))

    def test_empty_keyword_short_circuits(self):
        conn = _Conn()
        self.assertEqual({"phones": [], "bindings": []},
                         pr.search_registry(conn, "  "))
        self.assertEqual([], conn.executed)


class ResolveHolderUserIdsTests(unittest.TestCase):
    """resolve_holder_user_ids（群回复 @ 持卡人，2026-10-26）。"""

    def test_maps_names_to_user_ids_keeping_order(self):
        conn = _Conn({"FROM `dim_robot_member`": [
            {"name": "张三", "user_id": "u-zhang"},
            {"name": "李四", "user_id": "u-li"},
        ]})
        ids = pr.resolve_holder_user_ids(conn, ["李四", "张三"])
        self.assertEqual(["u-li", "u-zhang"], ids)
        sql, params = conn.executed[0]
        self.assertIn("`is_active` = 1", sql)
        self.assertIn("`name` IN (%s, %s)", sql)
        self.assertEqual(("李四", "张三"), params)

    def test_ambiguous_same_name_skipped(self):
        # 同名两个 userId → 不 @（@ 错人比不 @ 更糟）
        conn = _Conn({"FROM `dim_robot_member`": [
            {"name": "张伟", "user_id": "u1"},
            {"name": "张伟", "user_id": "u2"},
            {"name": "张三", "user_id": "u-zhang"},
        ]})
        self.assertEqual(["u-zhang"],
                         pr.resolve_holder_user_ids(conn, ["张伟", "张三"]))

    def test_empty_names_short_circuit(self):
        conn = _Conn()
        self.assertEqual([], pr.resolve_holder_user_ids(conn, ["", "  "]))
        self.assertEqual([], conn.executed)


if __name__ == "__main__":
    unittest.main()
