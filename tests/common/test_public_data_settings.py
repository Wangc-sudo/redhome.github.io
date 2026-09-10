import json
import tempfile
import unittest
from pathlib import Path

from common.public_data.settings import Settings


class SettingsFromEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_path = Path(self.temp_dir.name) / "public-data.json"
        self.config_path.write_text(json.dumps({"datasets": []}), encoding="utf-8")
        self.environ = {
            "APP_ENV": "test",
            "PUBLIC_DATA_RDS_HOST": "localhost",
            "PUBLIC_DATA_RDS_PORT": "3306",
            "PUBLIC_DATA_RDS_USER": "public_data",
            "PUBLIC_DATA_RDS_PASSWORD": "super-secret-password",
            "PUBLIC_DATA_DINGTALK_DATABASE": "dingtalk_test",
            "PUBLIC_DATA_WDT_DATABASE": "wdt_test",
            "PUBLIC_DATA_MART_DATABASE": "mart_test",
            "PUBLIC_DATA_CONFIG": str(self.config_path),
        }

    def test_builds_isolated_test_database_settings(self):
        settings = Settings.from_environment(self.environ)

        self.assertEqual("test", settings.app_env)
        self.assertEqual(self.config_path, settings.source_config_path)
        self.assertEqual("dingtalk_test", settings.dingtalk_database.name)
        self.assertEqual("wdt_test", settings.wdt_database.name)
        self.assertEqual("mart_test", settings.mart_database.name)
        for database in (
            settings.dingtalk_database,
            settings.wdt_database,
            settings.mart_database,
        ):
            self.assertEqual("localhost", database.host)
            self.assertEqual(3306, database.port)
            self.assertEqual("public_data", database.user)
            self.assertEqual("super-secret-password", database.password)

    def test_rejects_unknown_environment_without_exposing_password(self):
        self.environ["APP_ENV"] = "staging"

        with self.assertRaises(ValueError) as raised:
            Settings.from_environment(self.environ)

        self.assertNotIn("super-secret-password", str(raised.exception))

    def test_rejects_each_missing_required_value_without_exposing_password(self):
        for key in self.environ:
            with self.subTest(key=key):
                environment = dict(self.environ)
                environment.pop(key)

                with self.assertRaises(ValueError) as raised:
                    Settings.from_environment(environment)

                self.assertNotIn("super-secret-password", str(raised.exception))

    def test_rejects_invalid_or_noninteger_ports_without_exposing_password(self):
        for port in ("0", "65536", "-1", "3306.0", "not-a-number"):
            with self.subTest(port=port):
                environment = dict(self.environ, PUBLIC_DATA_RDS_PORT=port)

                with self.assertRaises(ValueError) as raised:
                    Settings.from_environment(environment)

                self.assertNotIn("super-secret-password", str(raised.exception))

    def test_test_environment_rejects_each_non_test_database_name(self):
        for key in (
            "PUBLIC_DATA_DINGTALK_DATABASE",
            "PUBLIC_DATA_WDT_DATABASE",
            "PUBLIC_DATA_MART_DATABASE",
        ):
            with self.subTest(key=key):
                environment = dict(self.environ, **{key: "shared_data"})

                with self.assertRaises(ValueError) as raised:
                    Settings.from_environment(environment)

                self.assertNotIn("super-secret-password", str(raised.exception))

    def test_builds_isolated_production_database_settings(self):
        production_environment = dict(
            self.environ,
            APP_ENV="production",
            PUBLIC_DATA_DINGTALK_DATABASE="dingtalk",
            PUBLIC_DATA_WDT_DATABASE="wdt",
            PUBLIC_DATA_MART_DATABASE="mart",
        )

        settings = Settings.from_environment(production_environment)

        self.assertEqual("production", settings.app_env)
        self.assertEqual("dingtalk", settings.dingtalk_database.name)
        self.assertEqual("wdt", settings.wdt_database.name)
        self.assertEqual("mart", settings.mart_database.name)

    def test_production_rejects_each_test_database_name(self):
        production_environment = dict(self.environ, APP_ENV="production")
        for key in (
            "PUBLIC_DATA_DINGTALK_DATABASE",
            "PUBLIC_DATA_WDT_DATABASE",
            "PUBLIC_DATA_MART_DATABASE",
        ):
            with self.subTest(key=key):
                environment = dict(production_environment, **{key: "shared_test"})

                with self.assertRaises(ValueError) as raised:
                    Settings.from_environment(environment)

                self.assertNotIn("super-secret-password", str(raised.exception))

    def test_rejects_invalid_or_non_object_config_json_without_exposing_password(self):
        for content in ("not-json", "[]"):
            with self.subTest(content=content):
                self.config_path.write_text(content, encoding="utf-8")

                with self.assertRaises(ValueError) as raised:
                    Settings.from_environment(self.environ)

                self.assertNotIn("super-secret-password", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
