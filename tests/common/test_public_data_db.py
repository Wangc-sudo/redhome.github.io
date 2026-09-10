import unittest
from unittest.mock import patch

from common.public_data.db import LockUnavailable, connect, named_lock, transaction
from common.public_data.settings import DatabaseSettings


class FakeCursor:
    def __init__(self, lock_result):
        self.lock_result = lock_result
        self.executed = []
        self.closed = False

    def execute(self, query, parameters):
        self.executed.append((query, parameters))

    def fetchone(self):
        return self.lock_result

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, lock_result=None):
        self.cursor_instance = FakeCursor(lock_result)
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1

    def close(self):
        self.close_calls += 1


class DatabaseBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.database_settings = DatabaseSettings(
            host="localhost",
            port=3306,
            user="public_data",
            password="super-secret-password",
            name="mart_test",
        )

    @patch("common.public_data.db.pymysql.connect")
    def test_connect_uses_only_the_required_pymysql_options(self, mocked_connect):
        connect(self.database_settings)

        from common.public_data import db

        mocked_connect.assert_called_once_with(
            host="localhost",
            port=3306,
            user="public_data",
            password="super-secret-password",
            database="mart_test",
            charset="utf8mb4",
            cursorclass=db.pymysql.cursors.DictCursor,
            autocommit=False,
        )

    def test_transaction_commits_and_leaves_connection_open_after_success(self):
        connection = FakeConnection()

        with transaction(connection) as active_connection:
            self.assertIs(connection, active_connection)

        self.assertEqual(1, connection.commit_calls)
        self.assertEqual(0, connection.rollback_calls)
        self.assertEqual(0, connection.close_calls)

    def test_transaction_rolls_back_reraises_and_leaves_connection_open_on_error(self):
        connection = FakeConnection()

        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with transaction(connection):
                raise RuntimeError("body failed")

        self.assertEqual(0, connection.commit_calls)
        self.assertEqual(1, connection.rollback_calls)
        self.assertEqual(0, connection.close_calls)

    def test_named_lock_releases_parameterized_lock_after_success(self):
        connection = FakeConnection({"acquired": 1})
        lock_name = "public-data:daily-report"

        with named_lock(connection, lock_name, timeout_seconds=12):
            pass

        self.assertEqual(
            [
                ("SELECT GET_LOCK(%s, %s) AS acquired", (lock_name, 12)),
                ("SELECT RELEASE_LOCK(%s)", (lock_name,)),
            ],
            connection.cursor_instance.executed,
        )
        self.assertTrue(connection.cursor_instance.closed)
        self.assertEqual(0, connection.close_calls)

    def test_named_lock_releases_lock_after_body_error(self):
        connection = FakeConnection({"acquired": 1})
        lock_name = "public-data:broken'lock"

        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with named_lock(connection, lock_name):
                raise RuntimeError("body failed")

        self.assertEqual(
            ("SELECT RELEASE_LOCK(%s)", (lock_name,)),
            connection.cursor_instance.executed[-1],
        )
        self.assertEqual(0, connection.close_calls)

    def test_named_lock_releases_and_raises_when_unavailable(self):
        connection = FakeConnection({"acquired": 0})
        lock_name = "public-data:unavailable"

        with self.assertRaises(LockUnavailable):
            with named_lock(connection, lock_name):
                pass

        self.assertEqual(
            [
                ("SELECT GET_LOCK(%s, %s) AS acquired", (lock_name, 30)),
                ("SELECT RELEASE_LOCK(%s)", (lock_name,)),
            ],
            connection.cursor_instance.executed,
        )
        self.assertTrue(connection.cursor_instance.closed)
        self.assertEqual(0, connection.close_calls)

    def test_named_lock_rejects_non_integer_success_values(self):
        for acquired in (True, 1.0):
            with self.subTest(acquired=acquired):
                connection = FakeConnection({"acquired": acquired})
                lock_name = "public-data:invalid-success"

                with self.assertRaises(LockUnavailable):
                    with named_lock(connection, lock_name):
                        pass

                self.assertEqual(
                    [
                        ("SELECT GET_LOCK(%s, %s) AS acquired", (lock_name, 30)),
                        ("SELECT RELEASE_LOCK(%s)", (lock_name,)),
                    ],
                    connection.cursor_instance.executed,
                )
                self.assertTrue(connection.cursor_instance.closed)


if __name__ == "__main__":
    unittest.main()
