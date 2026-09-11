import os


class LiveRunRejected(RuntimeError):
    pass


def _require_local_test_target(settings, values):
    """Shared target checks: every flag-independent gate lives here.

    Both gates must guarantee the same thing -- that a write can only ever
    land on the throwaway local test databases.
    """
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


def require_live_run(settings, *, live_read, confirm_local_test_write, environ=None):
    values = os.environ if environ is None else environ
    if live_read is not True:
        raise LiveRunRejected("--live-read is required")
    if confirm_local_test_write is not True:
        raise LiveRunRejected("--confirm-local-test-write is required")
    _require_local_test_target(settings, values)


def require_extract_run(settings, *, confirm_local_test_write, environ=None):
    """Gate for the extraction layer: same guarantees, minus ``--live-read``.

    Extraction reads the local ``raw_*`` databases and never contacts a
    source, so demanding a live-read acknowledgement would be a false
    signal; everything that actually protects the target still applies.
    """
    values = os.environ if environ is None else environ
    if confirm_local_test_write is not True:
        raise LiveRunRejected("--confirm-local-test-write is required")
    _require_local_test_target(settings, values)


def require_business_run(settings, *, confirm_local_test_write, environ=None):
    """Gate for business-line tasks (robot): same guarantees as extraction.

    The robot reads ``mart_ops`` and writes ``robot_outbox`` only -- it
    makes no external calls at all, so no live-read acknowledgement exists
    to demand; everything that protects the target still applies.
    """
    values = os.environ if environ is None else environ
    if confirm_local_test_write is not True:
        raise LiveRunRejected("--confirm-local-test-write is required")
    _require_local_test_target(settings, values)


def require_gateway_run(settings, *, live_send, confirm_local_test_write, environ=None):
    """Gate for the delivery gateway: external *sends* need their own flag.

    The gateway does not read sources, but it **writes to an external
    system** (group messages / DING) -- the dangerous direction -- so it
    demands an explicit ``--live-send`` acknowledgement instead of
    ``--live-read``.
    """
    values = os.environ if environ is None else environ
    if live_send is not True:
        raise LiveRunRejected("--live-send is required")
    if confirm_local_test_write is not True:
        raise LiveRunRejected("--confirm-local-test-write is required")
    _require_local_test_target(settings, values)
