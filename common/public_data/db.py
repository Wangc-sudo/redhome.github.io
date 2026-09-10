from contextlib import contextmanager

import pymysql


class LockUnavailable(Exception):
    """Raised when a MySQL named lock cannot be acquired."""


def connect(database_settings):
    return pymysql.connect(
        host=database_settings.host,
        port=database_settings.port,
        user=database_settings.user,
        password=database_settings.password,
        database=database_settings.name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


@contextmanager
def transaction(connection):
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


@contextmanager
def named_lock(connection, lock_name, timeout_seconds=30):
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT GET_LOCK(%s, %s) AS acquired", (lock_name, timeout_seconds)
        )
        result = cursor.fetchone()
        acquired = result.get("acquired") if isinstance(result, dict) else None
        if type(acquired) is not int or acquired != 1:
            raise LockUnavailable("MySQL named lock is unavailable")
        yield
    finally:
        try:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
        finally:
            cursor.close()
