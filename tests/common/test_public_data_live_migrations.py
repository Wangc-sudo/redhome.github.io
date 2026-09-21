import re
import unittest

from common.public_data.finance_schema import table_definition
from common.public_data.live_migrations import LiveMigrationError, apply_live_migrations
from common.public_data.mart_extract_schema import order_line_channel_ddl_statements


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

    def test_all_twenty_one_tables_are_registered(self):
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
            "channel_monthly_target",
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
    def test_existing_extract_v1_upgrades_without_checksum_drift(self):
        from common.public_data.live_migrations import _MIGRATIONS, _combined_checksum

        original_checksum = "2b1ddff896128c831c055c8af666f390eaf0e645e7e6e15deab6ca5b44ade9d7"
        legacy = next(sql for version, _, sql in _MIGRATIONS if version == "mart-extract-v1")
        self.assertEqual(_combined_checksum(legacy), original_checksum)
        dingtalk, wdt, mart = FakeConnection(), FakeConnection(), FakeConnection()
        apply_live_migrations(dingtalk, wdt, mart, applied_checksums={"mart-extract-v1": original_checksum})
        executed = mart.cursor_instance.executed
        sql = "\n".join(query for query, _ in executed)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS `fact_daily_report_offline`", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS `fact_fin_receivables_aging`", sql)
        self.assertTrue(any(params and params[0] == "mart-extract-finance-v1" for _, params in executed))

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


class SummarySkippedMigrationTests(unittest.TestCase):
    """sync_dataset_summary.skipped 列（mart-ops-summary-skipped-v1）。

    提取层增量化用该列显式标记「跳过」摘要；列是纯追加、带默认值，
    未迁移时提取代码降级为普通摘要（fail-open）。
    """

    def test_skipped_migration_is_registered_after_mart_ops_v1(self):
        from common.public_data.live_migrations import _MIGRATIONS

        versions = [version for version, _, _ in _MIGRATIONS]
        self.assertIn("mart-ops-summary-skipped-v1", versions)
        self.assertLess(
            versions.index("mart-ops-v1"),
            versions.index("mart-ops-summary-skipped-v1"),
        )

    def test_fresh_database_adds_the_skipped_column(self):
        dingtalk, wdt, mart = FakeConnection(), FakeConnection(), FakeConnection()

        apply_live_migrations(dingtalk, wdt, mart)

        mart_sql = "\n".join(query for query, _ in mart.cursor_instance.executed)
        self.assertIn("ALTER TABLE `sync_dataset_summary`", mart_sql)
        self.assertIn("ADD COLUMN `skipped` TINYINT(1) NOT NULL DEFAULT 0", mart_sql)
        recorded = [
            params[0] for _, params in mart.cursor_instance.executed if params
        ]
        self.assertIn("mart-ops-summary-skipped-v1", recorded)

    def test_applied_skipped_migration_is_not_replayed(self):
        # ALTER 没有 IF NOT EXISTS：已应用版本必须按校验和跳过。
        from common.public_data.live_migrations import (
            _MIGRATIONS,
            _combined_checksum,
        )

        applied = {
            version: _combined_checksum(statements)
            for version, _, statements in _MIGRATIONS
        }
        dingtalk, wdt, mart = FakeConnection(), FakeConnection(), FakeConnection()

        apply_live_migrations(dingtalk, wdt, mart, applied_checksums=applied)

        mart_sql = "\n".join(query for query, _ in mart.cursor_instance.executed)
        self.assertNotIn("ADD COLUMN `skipped`", mart_sql)


class OrderLineChannelMigrationTests(unittest.TestCase):
    """fact_order_line 的店铺/渠道维度（迁移 mart-extract-order-line-v2）。

    表结构与投影列白名单是同一份契约：迁移少建（或多建）一列，投影写入
    就会在 ``replace_table`` 的列校验处失败，所以两边钉在一起测。
    """

    _COLUMN_RE = re.compile(r"^\s*`([a-z_]+)`\s+[A-Z]", re.MULTILINE)
    _ADD_COLUMN_RE = re.compile(r"ADD COLUMN `([a-z_]+)`")

    def test_channel_migration_is_registered_after_the_table(self):
        from common.public_data.live_migrations import _MIGRATIONS

        versions = [version for version, _, _ in _MIGRATIONS]
        self.assertIn("mart-extract-order-line-v2", versions)
        self.assertLess(
            versions.index("mart-extract-order-line-v1"),
            versions.index("mart-extract-order-line-v2"),
        )

    def test_channel_migration_only_adds_the_two_columns(self):
        statements = order_line_channel_ddl_statements()

        self.assertEqual(1, len(statements))
        sql = statements[0]
        self.assertTrue(sql.startswith("ALTER TABLE `fact_order_line`"))
        self.assertIn("ADD COLUMN `shop_name`", sql)
        self.assertIn("ADD COLUMN `channel_name`", sql)
        self.assertIn("ADD KEY `idx_order_line_channel` (`channel_name`)", sql)
        # 只补列不重建表：v1 的 CREATE 文本一旦改动就会被判成校验和漂移。
        self.assertNotIn("CREATE TABLE", sql)

    def test_fresh_database_records_the_channel_migration(self):
        dingtalk, wdt, mart = FakeConnection(), FakeConnection(), FakeConnection()

        apply_live_migrations(dingtalk, wdt, mart)

        recorded = [
            params[0] for _, params in mart.cursor_instance.executed if params
        ]
        self.assertIn("mart-extract-order-line-v1", recorded)
        self.assertIn("mart-extract-order-line-v2", recorded)

    def test_applied_channel_migration_is_not_replayed(self):
        # ALTER 没有 IF NOT EXISTS：已应用版本必须按校验和跳过，重跑会因
        # 列已存在而失败。
        from common.public_data.live_migrations import (
            _MIGRATIONS,
            _combined_checksum,
        )

        applied = {
            version: _combined_checksum(statements)
            for version, _, statements in _MIGRATIONS
        }
        dingtalk, wdt, mart = FakeConnection(), FakeConnection(), FakeConnection()

        apply_live_migrations(dingtalk, wdt, mart, applied_checksums=applied)

        mart_sql = "\n".join(query for query, _ in mart.cursor_instance.executed)
        self.assertNotIn("ALTER TABLE `fact_order_line`", mart_sql)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS `fact_order_line`", mart_sql)

    def test_table_columns_match_the_projection_whitelist(self):
        from common.public_data.extract_order_line import _ORDER_LINE_COLUMNS
        from common.public_data.mart_extract_schema import order_line_ddl_statements

        _, fact_order_line = order_line_ddl_statements()
        columns = set(self._COLUMN_RE.findall(fact_order_line))
        for statement in order_line_channel_ddl_statements():
            columns |= set(self._ADD_COLUMN_RE.findall(statement))

        self.assertEqual(
            set(_ORDER_LINE_COLUMNS) | {"synced_at", "sync_run_id"}, columns
        )


class MartSplitMigrationTests(unittest.TestCase):
    """mart 拆库 M1：mart_facts / mart_dims / mart_queue 三个新 schema。

    只建表不搬数据；热路径（apply_live_migrations）不感知这三个 target，
    由 apply_mart_split_migrations 按批次显式推进（设计稿 §2.3/§2.6）。
    """

    def test_split_targets_are_registered_after_legacy_mart_versions(self):
        from common.public_data.live_migrations import _MIGRATIONS

        registered = {version: target for version, target, _ in _MIGRATIONS}
        self.assertEqual("mart_facts", registered["mart-facts-v1"])
        self.assertEqual("mart_dims", registered["mart-dims-v1"])
        self.assertEqual("mart_queue", registered["mart-queue-v1"])

        versions = [version for version, _, _ in _MIGRATIONS]
        self.assertLess(
            versions.index("mart-ops-manual-report-v1"),
            versions.index("mart-facts-v1"),
        )

    def test_apply_split_migrations_builds_each_schema_with_its_tables(self):
        from common.public_data.live_migrations import apply_mart_split_migrations

        facts, dims, queue = FakeConnection(), FakeConnection(), FakeConnection()

        apply_mart_split_migrations(facts, dims, queue)

        facts_sql = "\n".join(q for q, _ in facts.cursor_instance.executed)
        for table in (
            "fact_daily_report_offline",
            "fact_channel_daily_sales",
            "fact_fin_receivables_aging",
            "fact_fin_prepayment_invoice",
            "fact_fin_offline_deposit",
            "fact_fin_platform_deposit",
            "fact_fin_store_funds",
            "fact_order_line",
            "fact_manual_report",
        ):
            self.assertIn(
                f"CREATE TABLE IF NOT EXISTS `{table}`", facts_sql, table
            )
        # 空库先建 v1 表、同版本内补渠道列（CREATE + ALTER 顺序执行）。
        self.assertIn("ALTER TABLE `fact_order_line`", facts_sql)
        self.assertLess(
            facts_sql.index("CREATE TABLE IF NOT EXISTS `fact_order_line`"),
            facts_sql.index("ALTER TABLE `fact_order_line`"),
        )

        dims_sql = "\n".join(q for q, _ in dims.cursor_instance.executed)
        for table in (
            "dim_calendar",
            "dim_product",
            "dim_robot_member",
            "dim_target",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS `{table}`", dims_sql, table)
        # 控制面与队列不进 dims。
        self.assertNotIn("sync_runs", dims_sql)
        self.assertNotIn("robot_outbox", dims_sql)

        queue_sql = "\n".join(q for q, _ in queue.cursor_instance.executed)
        self.assertIn("CREATE TABLE IF NOT EXISTS `robot_outbox`", queue_sql)
        self.assertNotIn("fact_", queue_sql)

        for connection in (facts, dims, queue):
            sql = "\n".join(q for q, _ in connection.cursor_instance.executed)
            self.assertIn(
                "CREATE TABLE IF NOT EXISTS `pd_live_schema_migration`", sql
            )

        recorded_facts = [
            params[0] for _, params in facts.cursor_instance.executed if params
        ]
        self.assertIn("mart-facts-v1", recorded_facts)
        self.assertNotIn("mart-dims-v1", recorded_facts)

    def test_live_migrations_do_not_touch_split_targets(self):
        dingtalk, wdt, mart = FakeConnection(), FakeConnection(), FakeConnection()

        apply_live_migrations(dingtalk, wdt, mart)

        recorded = [
            params[0] for _, params in mart.cursor_instance.executed if params
        ]
        self.assertNotIn("mart-facts-v1", recorded)
        self.assertNotIn("mart-dims-v1", recorded)
        self.assertNotIn("mart-queue-v1", recorded)

    def test_applied_split_migrations_are_not_replayed(self):
        from common.public_data.live_migrations import (
            _MIGRATIONS,
            _combined_checksum,
            apply_mart_split_migrations,
        )

        applied = {
            version: _combined_checksum(statements)
            for version, _, statements in _MIGRATIONS
        }
        facts, dims, queue = FakeConnection(), FakeConnection(), FakeConnection()

        apply_mart_split_migrations(facts, dims, queue, applied_checksums=applied)

        facts_sql = "\n".join(q for q, _ in facts.cursor_instance.executed)
        self.assertNotIn("fact_daily_report_offline", facts_sql)
        dims_sql = "\n".join(q for q, _ in dims.cursor_instance.executed)
        self.assertNotIn("dim_calendar", dims_sql)

    def test_split_migrations_reject_checksum_drift(self):
        from common.public_data.live_migrations import apply_mart_split_migrations

        with self.assertRaisesRegex(LiveMigrationError, "checksum"):
            apply_mart_split_migrations(
                FakeConnection(),
                FakeConnection(),
                FakeConnection(),
                applied_checksums={"mart-facts-v1": "0" * 64},
            )
