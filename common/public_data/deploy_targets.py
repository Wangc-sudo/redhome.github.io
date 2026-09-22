"""Deploy target profiles -- the single table both ``settings`` validation
and ``live_safety`` gates consult (上云部署规划 §5.1 方案 B).

``local-dispose``
    现状：写入只允许落在本地一次性 dispose 库（compose 的 ``mysql`` 服务，
    三个 ``*_test`` 库，且必须跑在集成测试容器里）。

``cloud-managed``
    新增：写入指向受控的云上目标（天翼云 RDS，正式库名）。必须显式设置
    ``PUBLIC_DATA_TARGET=cloud-managed`` 才会被门禁放行——生产写路径是
    显式 opt-in，不靠 ``APP_ENV=production`` 单独推导。

``PUBLIC_DATA_TARGET`` 未设置时按 ``APP_ENV`` 推导（test→local-dispose、
production→cloud-managed），以此保证既有配置零改动即旧行为；显式设置时
必须与 ``APP_ENV`` 一致，不一致即 loud failure。
"""

import os

ENV_VAR = "PUBLIC_DATA_TARGET"

_TARGETS = {
    "local-dispose": {
        "app_env": "test",
        #: 库名必须携带的后缀
        "database_suffix": "_test",
        #: 门禁是否要求 INTEGRATION_TEST_RUNNER=1
        "requires_runner": True,
        #: 门禁放行的精确库名三元组（dingtalk / wdt / mart）
        "database_names": ("raw_dingtalk_test", "raw_wdt_test", "mart_ops_test"),
        #: 门禁放行的 host；None 表示「只要不是本地丢弃目标即可」
        "host": "mysql",
    },
    "cloud-managed": {
        "app_env": "production",
        "database_suffix": None,
        "requires_runner": False,
        "database_names": ("raw_dingtalk", "raw_wdt", "mart_ops"),
        "host": None,
    },
}

#: cloud-managed 下仍然禁止指向的本地丢弃目标
_LOCAL_DISPOSE_HOSTS = ("mysql", "localhost", "127.0.0.1", "::1")


class UnknownDeployTarget(ValueError):
    pass


def _derive_from_app_env(app_env):
    for name, profile in _TARGETS.items():
        if profile["app_env"] == app_env:
            return name
    raise UnknownDeployTarget(f"cannot derive deploy target from APP_ENV={app_env!r}")


def resolve_target_name(app_env, environ=None):
    """Resolve the effective target name for ``app_env``.

    Unset ``PUBLIC_DATA_TARGET`` derives from ``APP_ENV`` (backwards
    compatible); an explicit value must exist in the table and be
    consistent with ``APP_ENV``.
    """
    values = os.environ if environ is None else environ
    explicit = (values.get(ENV_VAR) or "").strip()
    if not explicit:
        return _derive_from_app_env(app_env)
    if explicit not in _TARGETS:
        raise UnknownDeployTarget(
            f"{ENV_VAR} must be one of {sorted(_TARGETS)} (got {explicit!r})"
        )
    expected_env = _TARGETS[explicit]["app_env"]
    if expected_env != app_env:
        raise UnknownDeployTarget(
            f"{ENV_VAR}={explicit} requires APP_ENV={expected_env} (got {app_env!r})"
        )
    return explicit


def target_profile(app_env, environ=None):
    """Return ``(name, profile)`` for the effective target."""
    name = resolve_target_name(app_env, environ)
    return name, _TARGETS[name]


def local_dispose_hosts():
    return _LOCAL_DISPOSE_HOSTS
