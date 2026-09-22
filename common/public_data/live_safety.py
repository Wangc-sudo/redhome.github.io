import os

from common.public_data.deploy_targets import (
    ENV_VAR,
    local_dispose_hosts,
    target_profile,
)
from common.public_data.ipv4_egress import (
    ENV_ALLOW_IPV6,
    allow_ipv6,
    ipv6_egress_stalled,
)


class LiveRunRejected(RuntimeError):
    pass


def _require_local_dispose_target(settings, values):
    """local-dispose profile: the original throwaway-local-DB gate, verbatim.

    A write can only ever land on the throwaway local test databases.
    """
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


def _require_cloud_managed_target(settings, values, profile):
    """cloud-managed profile: writes may only land on the managed cloud target.

    The local-dispose guarantees are re-expressed for the cloud target:
    no test-runner marker, no local dispose host, exact production names.
    """
    if values.get("INTEGRATION_TEST_RUNNER") == "1":
        raise LiveRunRejected(
            "cloud-managed target must not run inside the integration test runner"
        )
    databases = (
        settings.dingtalk_database,
        settings.wdt_database,
        settings.mart_database,
    )
    if any(database.host in local_dispose_hosts() for database in databases):
        raise LiveRunRejected(
            "cloud-managed target requires a managed host, not a local dispose target"
        )
    names = tuple(database.name for database in databases)
    if names != profile["database_names"]:
        raise LiveRunRejected(
            "cloud-managed target requires " + ", ".join(profile["database_names"])
        )


def _require_target(settings, values):
    """Dispatch to the active target profile's checks.

    ``APP_ENV`` must match the resolved profile's environment; the
    cloud-managed profile additionally requires the explicit
    ``PUBLIC_DATA_TARGET=cloud-managed`` opt-in (production writes are
    never enabled by ``APP_ENV`` alone).
    """
    target_name, profile = target_profile(settings.app_env, values)
    if settings.app_env != profile["app_env"]:
        raise LiveRunRejected(
            f"APP_ENV must be {profile['app_env']} for target {target_name}"
        )
    if target_name == "cloud-managed" and not (
        values.get(ENV_VAR) or ""
    ).strip():
        raise LiveRunRejected(
            f"production writes require explicit {ENV_VAR}=cloud-managed"
        )
    if target_name == "local-dispose":
        _require_local_dispose_target(settings, values)
    else:
        _require_cloud_managed_target(settings, values, profile)


def require_ipv4_egress(*, environ=None, stalled=None):
    """IPv6 出网门禁：仅在显式放行 IPv6 时才真探测。

    默认基线由 ``force_ipv4`` 兜底（AAAA 已被过滤，无卡死路径），无需
    探测；显式 ``PUBLIC_DATA_ALLOW_IPV6`` 时探测真实环境，「AAAA 优先
    + IPv6 出网不通」即 fail-fast，杜绝 2026-09-22 式的静默空转。
    """
    values = os.environ if environ is None else environ
    if not allow_ipv6(values):
        return
    is_stalled = ipv6_egress_stalled() if stalled is None else stalled()
    if is_stalled:
        raise LiveRunRejected(
            "ipv6_egress_stall: DNS prefers AAAA but IPv6 egress is dead; "
            f"unset {ENV_ALLOW_IPV6} to force IPv4, or fix IPv6 egress"
        )


def require_live_run(
    settings, *, live_read, confirm_local_test_write, environ=None, stalled=None
):
    values = os.environ if environ is None else environ
    if live_read is not True:
        raise LiveRunRejected("--live-read is required")
    if confirm_local_test_write is not True:
        raise LiveRunRejected("--confirm-local-test-write is required")
    _require_target(settings, values)
    require_ipv4_egress(environ=values, stalled=stalled)


def require_extract_run(settings, *, confirm_local_test_write, environ=None):
    """Gate for the extraction layer: same guarantees, minus ``--live-read``.

    Extraction reads the local ``raw_*`` databases and never contacts a
    source, so demanding a live-read acknowledgement would be a false
    signal; everything that actually protects the target still applies.
    """
    values = os.environ if environ is None else environ
    if confirm_local_test_write is not True:
        raise LiveRunRejected("--confirm-local-test-write is required")
    _require_target(settings, values)


def require_business_run(settings, *, confirm_local_test_write, environ=None):
    """Gate for business-line tasks (robot): same guarantees as extraction.

    The robot reads ``mart_ops`` and writes ``robot_outbox`` only -- it
    makes no external calls at all, so no live-read acknowledgement exists
    to demand; everything that protects the target still applies.
    """
    values = os.environ if environ is None else environ
    if confirm_local_test_write is not True:
        raise LiveRunRejected("--confirm-local-test-write is required")
    _require_target(settings, values)


def require_gateway_run(
    settings, *, live_send, confirm_local_test_write, environ=None, stalled=None
):
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
    _require_target(settings, values)
    require_ipv4_egress(environ=values, stalled=stalled)
