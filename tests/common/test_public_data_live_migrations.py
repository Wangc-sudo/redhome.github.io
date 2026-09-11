import unittest

from common.public_data.finance_schema import table_definition
from common.public_data.live_migrations import LiveMigrationError, apply_live_migrations


class FakeCursor:
    def __init__(self):
        self.executed = []
        self.rows = {}

    def execute(self, query, parameters=None):
        self.executed.append((query, parameters))

    def fetchone(self):
        return None

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()
        self.commit_calls = 0
        self.rollback_calls = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1


class FinanceSchemaTests(unittest.TestCase):
    def test_registered_finance_table_has_documented_columns_and_indexes(self):
        table = table_definition("fin_store_commission")

        self.assertEqual("fin_store_commission", table.name)
        column_tuples = [(c.name, c.mysql_type) for c in table.columns]
        self.assertIn(("company_entity", "VARCHAR(255)"), column_tuples)
        self.assertIn("idx_company_store", [idx.name for idx in table.indexes])
        self.assertIn("dingtalk_record_id", [c.name for c in table.technical_columns])

    def test_all_twenty_tables_are_registered(self):
        expected = {
            "fin_store_commission",
            "fin_tax_declaration_2026",
            "fin_tax_sales_reconciliation_2026",
            "fin_tax_stamp_duty",
            "fin_tax_uninvoiced_sales_summary",
            "fin_tax_uninvoiced_sales",
            "fin_tax_prior_period_invoice",
            "fin_tax_pre_invoice",
            "fin_tax_input_invoice",
            "fin_ecommerce_prepayment_supplier_invoice",
            "fin_ecommerce_promotion_recharge_balance",
            "fin_ecommerce_platform_deposit",
            "fin_ecommerce_store_funds_balance",
            "fin_billion_subsidy_pdd",
            "fin_billion_subsidy_douyin",
            "fin_daily_funds",
            "fin_offline_receivables_aging",
            "fin_offline_deposit_other_receivables",
            "daily_report_offline",
            "channel_daily_sales",
        }
        from common.public_data.finance_schema import all_table_definitions
        registered = {t.name for t in all_table_definitions()}
        self.assertEqual(expected, registered)

    def test_daily_funds_has_155_day_columns(self):
        table = table_definition("fin_daily_funds")
        day_columns = [c for c in table.columns if c.name.startswith("day_")]
        self.assertEqual(155, len(day_columns))

    def test_unregistered_table_raises(self):
        with self.assertRaisesRegex(KeyError, "unregistered"):
            table_definition("nonexistent_table")

    def test_source_type_expectations_are_present(self):
        table = table_definition("fin_store_commission")
        source_types = {c.name: c.source_type for c in table.columns if c.source_type}
        self.assertEqual("text", source_types["company_entity"])
        self.assertEqual("user", source_types["submitted_by"])


class LiveMigrationTests(unittest.TestCase):
    def test_migrations_use_only_registered_identifiers_and_record_checksums(self):
        dingtalk = FakeConnection()
        wdt = FakeConnection()
        mart = FakeConnection()

        apply_live_migrations(dingtalk, wdt, mart)

        dingtalk_sql = "\n".join(query for query, _ in dingtalk.cursor_instance.executed)
        wdt_sql = "\n".join(query for query, _ in wdt.cursor_instance.executed)
        mart_sql = "\n".join(query for query, _ in mart.cursor_instance.executed)
        self.assertIn("CREATE TABLE IF NOT EXISTS `fin_store_commission`", dingtalk_sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS `dingtalk_schema_snapshots`", dingtalk_sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS `dingtalk_org_member`", dingtalk_sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS `wdt_records`", wdt_sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS `sync_runs`", mart_sql)
        self.assertNotIn("from_manifest", dingtalk_sql + wdt_sql + mart_sql)

    def test_rejects_checksum_drift_before_running_changed_schema(self):
        connection = FakeConnection()
        with self.assertRaisesRegex(LiveMigrationError, "checksum"):
            apply_live_migrations(
                connection, connection, connection,
                applied_checksums={"raw-dingtalk-v1": "0" * 64},
            )

    def test_migration_creates_schema_migration_tracking_table(self):
        dingtalk = FakeConnection()
        wdt = FakeConnection()
        mart = FakeConnection()

        apply_live_migrations(dingtalk, wdt, mart)

        dingtalk_sql = "\n".join(query for query, _ in dingtalk.cursor_instance.executed)
        self.assertIn("CREATE TABLE IF NOT EXISTS `pd_live_schema_migration`", dingtalk_sql)

    def test_each_database_gets_only_its_tables(self):
        dingtalk = FakeConnection()
        wdt = FakeConnection()
        mart = FakeConnection()

        apply_live_migrations(dingtalk, wdt, mart)

        wdt_sql = "\n".join(query for query, _ in wdt.cursor_instance.executed)
        mart_sql = "\n".join(query for query, _ in mart.cursor_instance.executed)
        self.assertNotIn("fin_store_commission", wdt_sql)
        self.assertNotIn("fin_store_commission", mart_sql)
        self.assertNotIn("wdt_records", mart_sql)
        self.assertNotIn("dingtalk_org_member", wdt_sql)
        self.assertNotIn("dingtalk_org_member", mart_sql)
        self.assertIn("sync_runs", mart_sql)
