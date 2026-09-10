import os


class LiveRunRejected(RuntimeError):
    pass


def require_live_run(settings, *, live_read, confirm_local_test_write, environ=None):
    values = os.environ if environ is None else environ
    if live_read is not True:
        raise LiveRunRejected("--live-read is required")
    if confirm_local_test_write is not True:
        raise LiveRunRejected("--confirm-local-test-write is required")
    if settings.app_env != "test":
        raise LiveRunRejected("APP_ENV must be test")
    if values.get("INTEGRATION_TEST_RUNNER") != "1":
        raise LiveRunRejected("INTEGRATION_TEST_RUNNER must be 1")
    databases = (
        settings.dingtalk_database,
        settings.wdt_database,
        settings.mart_database,
    )
    if any(database.host != "mysql" for database in databases):
        raise LiveRunRejected("PUBLIC_DATA_RDS_HOST must be mysql")
    names = tuple(database.name for database in databases)
    if names != ("raw_dingtalk_test", "raw_wdt_test", "mart_ops_test"):
        raise LiveRunRejected(
            "live sync requires raw_dingtalk_test, raw_wdt_test, mart_ops_test"
        )
