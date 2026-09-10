import os
import unittest

from common.public_data.db import LockUnavailable, connect, named_lock, transaction
from common.public_data.settings import Settings


@unittest.skipUnless(
    os.environ.get("INTEGRATION_TEST_RUNNER") == "1",
    "Requires the Docker Compose MySQL integration environment.",
)
class PublicDataMySQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = Settings.from_environment()

    def test_connects_to_each_isolated_test_database(self):
        self.assertEqual("test", self.settings.app_env)

        for database_settings in (
            self.settings.dingtalk_database,
            self.settings.wdt_database,
            self.settings.mart_database,
        ):
            with self.subTest(database=database_settings.name):
                connection = connect(database_settings)
                try:
                    cursor = connection.cursor()
                    try:
                        cursor.execute("SELECT DATABASE() AS database_name")
                        result = cursor.fetchone()
                    finally:
                        cursor.close()
                finally:
                    connection.close()

                self.assertEqual(database_settings.name, result["database_name"])

    def test_transaction_commits_and_rolls_back_against_mysql(self):
        connection = connect(self.settings.mart_database)
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "CREATE TEMPORARY TABLE integration_transaction_probe "
                    "(label VARCHAR(32) NOT NULL)"
                )
            finally:
                cursor.close()

            with transaction(connection):
                cursor = connection.cursor()
                try:
                    cursor.execute(
                        "INSERT INTO integration_transaction_probe (label) VALUES (%s)",
                        ("committed",),
                    )
                finally:
                    cursor.close()

            with self.assertRaisesRegex(RuntimeError, "force rollback"):
                with transaction(connection):
                    cursor = connection.cursor()
                    try:
                        cursor.execute(
                            "INSERT INTO integration_transaction_probe (label) VALUES (%s)",
                            ("rolled-back",),
                        )
                    finally:
                        cursor.close()
                    raise RuntimeError("force rollback")

            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT label FROM integration_transaction_probe ORDER BY label"
                )
                rows = cursor.fetchall()
            finally:
                cursor.close()
        finally:
            connection.close()

        self.assertEqual(["committed"], [row["label"] for row in rows])

    def test_named_lock_excludes_second_connection_then_releases(self):
        lock_name = "public-data-integration:named-lock"
        first_connection = connect(self.settings.mart_database)
        second_connection = connect(self.settings.mart_database)
        try:
            with named_lock(first_connection, lock_name, timeout_seconds=0):
                with self.assertRaises(LockUnavailable):
                    with named_lock(second_connection, lock_name, timeout_seconds=0):
                        pass

            with named_lock(second_connection, lock_name, timeout_seconds=0):
                pass
        finally:
            first_connection.close()
            second_connection.close()


if __name__ == "__main__":
    unittest.main()
