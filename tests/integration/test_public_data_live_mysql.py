import os
import unittest

from common.public_data.db import connect
from common.public_data.live_migrations import apply_live_migrations
from common.public_data.settings import Settings


@unittest.skipUnless(
    os.environ.get("INTEGRATION_TEST_RUNNER") == "1",
    "Requires the Docker Compose MySQL integration environment.",
)
class PublicDataLiveMySQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = Settings.from_environment()
        cls.dingtalk_connection = connect(cls.settings.dingtalk_database)
        cls.wdt_connection = connect(cls.settings.wdt_database)
        cls.mart_connection = connect(cls.settings.mart_database)
        apply_live_migrations(
            cls.dingtalk_connection,
            cls.wdt_connection,
            cls.mart_connection,
        )

    @classmethod
    def tearDownClass(cls):
        cls.dingtalk_connection.close()
        cls.wdt_connection.close()
        cls.mart_connection.close()

    def _table_names(self, connection):
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT TABLE_NAME FROM information_schema.tables "
                "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
            )
            return [row["TABLE_NAME"] for row in cursor.fetchall()]
        finally:
            cursor.close()

    def test_finance_tables_exist_only_in_raw_dingtalk(self):
        tables = self._table_names(self.dingtalk_connection)
        self.assertIn("fin_store_commission", tables)
        self.assertIn("dingtalk_schema_snapshots", tables)

        wdt_tables = self._table_names(self.wdt_connection)
        self.assertNotIn("fin_store_commission", wdt_tables)
        self.assertNotIn("dingtalk_schema_snapshots", wdt_tables)

        mart_tables = self._table_names(self.mart_connection)
        self.assertNotIn("fin_store_commission", mart_tables)
        self.assertNotIn("dingtalk_schema_snapshots", mart_tables)

    def test_wdt_records_exists_only_in_raw_wdt(self):
        tables = self._table_names(self.wdt_connection)
        self.assertIn("wdt_records", tables)

        dingtalk_tables = self._table_names(self.dingtalk_connection)
        self.assertNotIn("wdt_records", dingtalk_tables)

        mart_tables = self._table_names(self.mart_connection)
        self.assertNotIn("wdt_records", mart_tables)

    def test_mart_tables_exist_only_in_mart_ops(self):
        tables = self._table_names(self.mart_connection)
        self.assertIn("sync_runs", tables)
        self.assertIn("sync_dataset_summary", tables)

        dingtalk_tables = self._table_names(self.dingtalk_connection)
        self.assertNotIn("sync_runs", dingtalk_tables)
        self.assertNotIn("sync_dataset_summary", dingtalk_tables)

        wdt_tables = self._table_names(self.wdt_connection)
        self.assertNotIn("sync_runs", wdt_tables)
        self.assertNotIn("sync_dataset_summary", wdt_tables)

    def test_migration_tracking_records_checksums(self):
        for connection in (
            self.dingtalk_connection,
            self.wdt_connection,
            self.mart_connection,
        ):
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT version, checksum FROM pd_live_schema_migration"
                )
                rows = cursor.fetchall()
            finally:
                cursor.close()

            self.assertGreater(len(rows), 0)
            for row in rows:
                self.assertEqual(64, len(row["checksum"]))


if __name__ == "__main__":
    unittest.main()
