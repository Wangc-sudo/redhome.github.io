"""bi_authz（BI 授权）离线单测：DDL 迁移、自举 seed、读写路径。

全部走 FakeConnection / 临时 seed 文件——不联网、不碰真实库、不读凭据。
钉死的语义：DDL 幂等（连跑两次不炸）、seed 解析校验（缺字段 / 非法
grant_type / 空 grants / 重复主键 抛错）、写路径 upsert + audit 同事务、
发版 seed 恰为唯一 admin 自举记录。
"""

import tempfile
import unittest
from pathlib import Path

from common.public_data.bi_authz import (
    BiAuthzError,
    GrantRecord,
    apply_grant_seed,
    bi_authz_ddl_statements,
    delete_grant,
    fetch_active_members,
    fetch_grants,
    load_grant_seed,
    upsert_grant,
    validate_grant_fields,
)
from common.public_data.live_migrations import (
    _MIGRATIONS,
    _combined_checksum,
    apply_live_migrations,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_SEED = REPOSITORY_ROOT / "docker" / "integration" / "ops.seed.yaml"
ADMIN_USER_ID = "014341566058939427"


class FakeCursor:
    def __init__(self, rows=None):
        self.executed = []
        self._rows = rows if rows is not None else []
        self.rowcount = 1

    def execute(self, query, parameters=None):
        self.executed.append((query, parameters))

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return None

    def close(self):
        pass


class FakeConnection:
    def __init__(self, rows=None):
        self.cursor_instance = FakeCursor(rows)
        self.commit_calls = 0
        self.rollback_calls = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1


def _seed_file(test, text):
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    tmp.write(text)
    tmp.close()
    test.addCleanup(Path(tmp.name).unlink)
    return tmp.name


class BiAuthzMigrationTests(unittest.TestCase):
    """mart-ops-bi-authz-v1：两表 DDL 注册与幂等。"""

    def test_migration_is_registered_after_mart_ops_versions(self):
        versions = [version for version, _, _ in _MIGRATIONS]
        self.assertIn("mart-ops-bi-authz-v1", versions)
        self.assertLess(
            versions.index("mart-ops-v1"),
            versions.index("mart-ops-bi-authz-v1"),
        )
        registered = {version: target for version, target, _ in _MIGRATIONS}
        self.assertEqual("mart", registered["mart-ops-bi-authz-v1"])

    def test_ddl_uses_create_if_not_exists(self):
        for statement in bi_authz_ddl_statements():
            self.assertTrue(statement.startswith("CREATE TABLE IF NOT EXISTS"))

    def test_fresh_database_creates_both_tables_and_records_checksum(self):
        mart = FakeConnection()

        apply_live_migrations(FakeConnection(), FakeConnection(), mart)

        mart_sql = "\n".join(q for q, _ in mart.cursor_instance.executed)
        self.assertIn("CREATE TABLE IF NOT EXISTS `bi_authz_grant`", mart_sql)
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS `bi_authz_grant_audit`", mart_sql
        )
        recorded = [
            params[0] for _, params in mart.cursor_instance.executed if params
        ]
        self.assertIn("mart-ops-bi-authz-v1", recorded)

    def test_replaying_migrations_twice_does_not_rebuild_or_blow_up(self):
        # 幂等 = 跟踪表已记录 + 校验和一致 → 第二次连 CREATE 都不再执行。
        applied = {
            version: _combined_checksum(statements)
            for version, _, statements in _MIGRATIONS
        }
        mart = FakeConnection()

        apply_live_migrations(
            FakeConnection(), FakeConnection(), mart, applied_checksums=applied
        )

        mart_sql = "\n".join(q for q, _ in mart.cursor_instance.executed)
        self.assertNotIn("bi_authz_grant", mart_sql)


class ShippedOpsSeedTests(unittest.TestCase):
    """发版的 ops.seed.yaml：首发布恰为唯一 admin 自举记录（王城）。"""

    def test_shipped_seed_is_exactly_the_bootstrap_admin(self):
        records = load_grant_seed(SHIPPED_SEED)

        self.assertEqual(1, len(records))
        (record,) = records
        self.assertEqual(ADMIN_USER_ID, record.user_id)
        self.assertEqual("admin", record.grant_type)
        self.assertEqual("-", record.grant_key)
        self.assertIn("王城", record.note)


class LoadGrantSeedTests(unittest.TestCase):
    def test_valid_seed_parses_all_fields(self):
        path = _seed_file(self, (
            "version: 1\n"
            "grants:\n"
            "  - user_id: u1\n"
            "    grant_type: scope\n"
            "    grant_key: fin\n"
            "    note: 财务页\n"
            "  - user_id: u2\n"
            "    grant_type: region\n"
            "    grant_key: hangzhou\n"
        ))

        records = load_grant_seed(path)

        self.assertEqual(
            (
                GrantRecord("u1", "scope", "fin", "财务页"),
                GrantRecord("u2", "region", "hangzhou", ""),
            ),
            records,
        )

    def test_rejects_wrong_version(self):
        path = _seed_file(self, "version: 2\ngrants:\n  - user_id: u1\n"
                        "    grant_type: admin\n    grant_key: '-'\n")
        with self.assertRaisesRegex(BiAuthzError, "version"):
            load_grant_seed(path)

    def test_rejects_empty_grants(self):
        path = _seed_file(self, "version: 1\ngrants: []\n")
        with self.assertRaisesRegex(BiAuthzError, "non-empty"):
            load_grant_seed(path)

    def test_rejects_missing_user_id(self):
        path = _seed_file(self, "version: 1\ngrants:\n"
                        "  - grant_type: admin\n    grant_key: '-'\n")
        with self.assertRaisesRegex(BiAuthzError, "user_id"):
            load_grant_seed(path)

    def test_rejects_unknown_grant_type(self):
        path = _seed_file(self, "version: 1\ngrants:\n"
                        "  - user_id: u1\n    grant_type: superuser\n"
                        "    grant_key: '-'\n")
        with self.assertRaisesRegex(BiAuthzError, "grant_type"):
            load_grant_seed(path)

    def test_rejects_duplicate_primary_key(self):
        path = _seed_file(self, (
            "version: 1\ngrants:\n"
            "  - user_id: u1\n    grant_type: scope\n    grant_key: fin\n"
            "  - user_id: u1\n    grant_type: scope\n    grant_key: fin\n"
        ))
        with self.assertRaisesRegex(BiAuthzError, "repeats"):
            load_grant_seed(path)

    def test_error_messages_never_carry_file_contents(self):
        path = _seed_file(self, "version: 1\ngrants:\n"
                        "  - user_id: u1\n    grant_type: superuser\n"
                        "    grant_key: secret-key-value\n")
        with self.assertRaises(BiAuthzError) as caught:
            load_grant_seed(path)
        self.assertNotIn("superuser", str(caught.exception))
        self.assertNotIn("secret-key-value", str(caught.exception))


class ValidateGrantFieldsTests(unittest.TestCase):
    def test_admin_requires_dash_key(self):
        with self.assertRaisesRegex(BiAuthzError, "grant_key"):
            validate_grant_fields("u1", "admin", "everything")

    def test_strips_whitespace(self):
        record = validate_grant_fields(" u1 ", "scope", " fin ")
        self.assertEqual(("u1", "scope", "fin"),
                         (record.user_id, record.grant_type, record.grant_key))


class GrantReadWriteTests(unittest.TestCase):
    def test_fetch_grants_returns_type_key_pairs(self):
        connection = FakeConnection(rows=[("scope", "fin"), ("region", "hq")])

        self.assertEqual(
            (("scope", "fin"), ("region", "hq")),
            fetch_grants(connection, "u1"),
        )
        query, params = connection.cursor_instance.executed[0]
        self.assertIn("FROM `bi_authz_grant`", query)
        self.assertNotIn("INSERT", query)
        self.assertNotIn("DELETE", query)
        self.assertEqual(("u1",), params)

    def test_fetch_grants_supports_dict_rows(self):
        connection = FakeConnection(
            rows=[{"grant_type": "admin", "grant_key": "-"}]
        )
        self.assertEqual((("admin", "-"),), fetch_grants(connection, "u1"))

    def test_upsert_grant_writes_grant_and_audit_in_one_transaction(self):
        connection = FakeConnection()

        written = upsert_grant(
            connection, GrantRecord("u1", "scope", "fin", "财务页"), actor="admin1"
        )

        self.assertEqual(1, written)
        executed = connection.cursor_instance.executed
        grant_sql = "\n".join(q for q, _ in executed)
        self.assertIn("ON DUPLICATE KEY UPDATE", grant_sql)
        self.assertIn("INSERT INTO `bi_authz_grant_audit`", grant_sql)
        audit_params = [p for q, p in executed if "audit" in q][0]
        self.assertEqual("admin1", audit_params[0])
        self.assertEqual("grant", audit_params[1])
        self.assertEqual(1, connection.commit_calls)

    def test_delete_grant_removes_and_audits_in_one_transaction(self):
        connection = FakeConnection()

        deleted = delete_grant(connection, "u1", "scope", "fin", actor="admin1")

        self.assertEqual(1, deleted)
        executed = connection.cursor_instance.executed
        sql = "\n".join(q for q, _ in executed)
        self.assertIn("DELETE FROM `bi_authz_grant`", sql)
        self.assertIn("INSERT INTO `bi_authz_grant_audit`", sql)
        audit_params = [p for q, p in executed if "audit" in q][0]
        self.assertEqual("revoke", audit_params[1])
        self.assertEqual(1, connection.commit_calls)

    def test_fetch_active_members_reads_only_active(self):
        connection = FakeConnection(
            rows=[("u1", "王城", "hq", "总经办")]
        )

        self.assertEqual(
            (("u1", "王城", "hq", "总经办"),),
            fetch_active_members(connection),
        )
        query, _ = connection.cursor_instance.executed[0]
        self.assertIn("FROM `dim_robot_member`", query)
        self.assertIn("`is_active` = 1", query)


class ApplyGrantSeedTests(unittest.TestCase):
    def test_default_upserts_every_record_with_audit(self):
        connection = FakeConnection()
        records = (
            GrantRecord(ADMIN_USER_ID, "admin", "-", "自举管理员 王城"),
        )

        written = apply_grant_seed(connection, records)

        self.assertEqual(1, written)
        executed = connection.cursor_instance.executed
        sql = "\n".join(q for q, _ in executed)
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertIn("INSERT INTO `bi_authz_grant_audit`", sql)
        audit_params = [p for q, p in executed if "audit" in q][0]
        self.assertEqual("seed", audit_params[0])
        grant_params = [p for q, p in executed if "bi_authz_grant`" in q][0]
        self.assertEqual(ADMIN_USER_ID, grant_params[0])

    def test_if_missing_uses_insert_ignore(self):
        connection = FakeConnection()
        records = (GrantRecord("u1", "scope", "fin"),)

        apply_grant_seed(connection, records, if_missing=True)

        sql = "\n".join(q for q, _ in connection.cursor_instance.executed)
        self.assertIn("INSERT IGNORE INTO `bi_authz_grant`", sql)
        self.assertNotIn("ON DUPLICATE KEY UPDATE", sql)

    def test_if_missing_skips_audit_for_existing_records(self):
        connection = FakeConnection()
        connection.cursor_instance.rowcount = 0  # 主键冲突，INSERT IGNORE 跳过
        records = (GrantRecord("u1", "scope", "fin"),)

        written = apply_grant_seed(connection, records, if_missing=True)

        self.assertEqual(0, written)
        sql = "\n".join(q for q, _ in connection.cursor_instance.executed)
        self.assertNotIn("INSERT INTO `bi_authz_grant_audit`", sql)


if __name__ == "__main__":
    unittest.main()
