"""Direct gate tests for live_safety target profiles (上云方案 B).

Covers both profiles: the original ``local-dispose`` behaviour (unchanged)
and the new ``cloud-managed`` production path, including its explicit
``PUBLIC_DATA_TARGET=cloud-managed`` opt-in requirement.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from common.public_data.deploy_targets import UnknownDeployTarget
from common.public_data.live_safety import (
    LiveRunRejected,
    require_business_run,
    require_extract_run,
    require_gateway_run,
    require_live_run,
)
from common.public_data.settings import Settings


def _settings(app_env, host, names):
    return SimpleNamespace(
        app_env=app_env,
        dingtalk_database=SimpleNamespace(host=host, name=names[0]),
        wdt_database=SimpleNamespace(host=host, name=names[1]),
        mart_database=SimpleNamespace(host=host, name=names[2]),
    )


TEST_NAMES = ("raw_dingtalk_test", "raw_wdt_test", "mart_ops_test")
PROD_NAMES = ("raw_dingtalk", "raw_wdt", "mart_ops")


class LocalDisposeGateTests(unittest.TestCase):
    """TARGET 未设置、APP_ENV=test → 原有行为逐字保留。"""

    def setUp(self):
        self.settings = _settings("test", "mysql", TEST_NAMES)
        self.environ = {"INTEGRATION_TEST_RUNNER": "1"}

    def test_all_gates_pass_on_local_dispose_target(self):
        require_live_run(
            self.settings,
            live_read=True,
            confirm_local_test_write=True,
            environ=self.environ,
        )
        require_extract_run(
            self.settings, confirm_local_test_write=True, environ=self.environ
        )
        require_business_run(
            self.settings, confirm_local_test_write=True, environ=self.environ
        )
        require_gateway_run(
            self.settings,
            live_send=True,
            confirm_local_test_write=True,
            environ=self.environ,
        )

    def test_rejects_without_runner_marker(self):
        with self.assertRaises(LiveRunRejected):
            require_extract_run(
                self.settings, confirm_local_test_write=True, environ={}
            )

    def test_rejects_non_mysql_host(self):
        settings = _settings("test", "192.168.0.7", TEST_NAMES)
        with self.assertRaises(LiveRunRejected):
            require_extract_run(
                settings, confirm_local_test_write=True, environ=self.environ
            )

    def test_rejects_non_test_names(self):
        settings = _settings("test", "mysql", PROD_NAMES)
        with self.assertRaises(LiveRunRejected):
            require_extract_run(
                settings, confirm_local_test_write=True, environ=self.environ
            )

    def test_production_without_explicit_target_is_rejected(self):
        settings = _settings("production", "192.168.0.7", PROD_NAMES)
        with self.assertRaises(LiveRunRejected):
            require_extract_run(
                settings, confirm_local_test_write=True, environ={}
            )


class CloudManagedGateTests(unittest.TestCase):
    """显式 PUBLIC_DATA_TARGET=cloud-managed 的生产路径。"""

    def setUp(self):
        self.settings = _settings("production", "192.168.0.7", PROD_NAMES)
        self.environ = {"PUBLIC_DATA_TARGET": "cloud-managed"}

    def test_all_gates_pass_on_cloud_managed_target(self):
        require_live_run(
            self.settings,
            live_read=True,
            confirm_local_test_write=True,
            environ=self.environ,
        )
        require_extract_run(
            self.settings, confirm_local_test_write=True, environ=self.environ
        )
        require_business_run(
            self.settings, confirm_local_test_write=True, environ=self.environ
        )
        require_gateway_run(
            self.settings,
            live_send=True,
            confirm_local_test_write=True,
            environ=self.environ,
        )

    def test_rejects_runner_marker(self):
        environment = dict(self.environ, INTEGRATION_TEST_RUNNER="1")
        with self.assertRaises(LiveRunRejected):
            require_extract_run(
                self.settings, confirm_local_test_write=True, environ=environment
            )

    def test_rejects_local_dispose_hosts(self):
        for host in ("mysql", "localhost", "127.0.0.1", "::1"):
            with self.subTest(host=host):
                settings = _settings("production", host, PROD_NAMES)
                with self.assertRaises(LiveRunRejected):
                    require_extract_run(
                        settings, confirm_local_test_write=True, environ=self.environ
                    )

    def test_rejects_test_suffixed_names(self):
        settings = _settings("production", "192.168.0.7", TEST_NAMES)
        with self.assertRaises(LiveRunRejected):
            require_extract_run(
                settings, confirm_local_test_write=True, environ=self.environ
            )

    def test_flags_still_enforced(self):
        with self.assertRaises(LiveRunRejected):
            require_live_run(
                self.settings,
                live_read=False,
                confirm_local_test_write=True,
                environ=self.environ,
            )
        with self.assertRaises(LiveRunRejected):
            require_gateway_run(
                self.settings,
                live_send=False,
                confirm_local_test_write=True,
                environ=self.environ,
            )
        with self.assertRaises(LiveRunRejected):
            require_extract_run(
                self.settings, confirm_local_test_write=False, environ=self.environ
            )

    def test_target_env_mismatch_with_app_env_is_rejected(self):
        settings = _settings("test", "mysql", TEST_NAMES)
        with self.assertRaises(UnknownDeployTarget):
            require_extract_run(
                settings, confirm_local_test_write=True, environ=self.environ
            )

    def test_unknown_target_value_is_rejected(self):
        with self.assertRaises(UnknownDeployTarget):
            require_extract_run(
                self.settings,
                confirm_local_test_write=True,
                environ={"PUBLIC_DATA_TARGET": "somewhere-else"},
            )


class DeployTargetSettingsTests(unittest.TestCase):
    """settings.py 的库名后缀铁律查同一张表；显式 TARGET 一致性在此校验。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_path = Path(self.temp_dir.name) / "public-data.json"
        self.config_path.write_text(json.dumps({"datasets": []}), encoding="utf-8")
        self.environ = {
            "APP_ENV": "production",
            "PUBLIC_DATA_TARGET": "cloud-managed",
            "PUBLIC_DATA_RDS_HOST": "192.168.0.7",
            "PUBLIC_DATA_RDS_PORT": "13049",
            "PUBLIC_DATA_RDS_USER": "yyhl-it",
            "PUBLIC_DATA_RDS_PASSWORD": "super-secret-password",
            "PUBLIC_DATA_DINGTALK_DATABASE": "raw_dingtalk",
            "PUBLIC_DATA_WDT_DATABASE": "raw_wdt",
            "PUBLIC_DATA_MART_DATABASE": "mart_ops",
            "PUBLIC_DATA_CONFIG": str(self.config_path),
        }

    def test_cloud_managed_production_settings_build(self):
        settings = Settings.from_environment(self.environ)

        self.assertEqual("production", settings.app_env)
        self.assertEqual("raw_dingtalk", settings.dingtalk_database.name)
        self.assertEqual(13049, settings.dingtalk_database.port)

    def test_explicit_target_mismatch_with_app_env_is_rejected(self):
        environment = dict(self.environ, APP_ENV="test")

        with self.assertRaises(ValueError):
            Settings.from_environment(environment)

    def test_unknown_target_value_is_rejected(self):
        environment = dict(self.environ, PUBLIC_DATA_TARGET="somewhere-else")

        with self.assertRaises(ValueError):
            Settings.from_environment(environment)

    def test_production_rejects_test_suffixed_names_under_cloud_managed(self):
        environment = dict(self.environ, PUBLIC_DATA_MART_DATABASE="mart_ops_test")

        with self.assertRaises(ValueError):
            Settings.from_environment(environment)


if __name__ == "__main__":
    unittest.main()
