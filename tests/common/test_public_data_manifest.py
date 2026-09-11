import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.public_data.live_safety import (
    LiveRunRejected,
    require_extract_run,
    require_live_run,
)
from common.public_data.manifest import ManifestError, load_manifest
from common.public_data.settings import DatabaseSettings, Settings


def _manifest():
    return {
        "version": 1,
        "dingtalk": {
            "bases": [
                {
                    "base_id": "base-1",
                    "sheets": [
                        {
                            "sheet_id": "sheet-1",
                            "sheet_name": "店铺扣点费用管理",
                            "dataset": "finance_store_commission",
                            "target_table": "fin_store_commission",
                            "max_pages": 2,
                            "field_mapping": {
                                "公司主体": {"column": "company_entity", "source_type": "text"},
                                "费用项目": {"column": "fee_item", "source_type": "text"},
                            },
                        }
                    ],
                }
            ],
        },
        "wdt": {
            "datasets": [
                {
                    "dataset": "trade-window",
                    "method": "sales.TradeQuery.queryWithDetail",
                    "target_table": "wdt_records",
                    "record_id_path": "trade_no",
                    "page_size": 100,
                    "max_pages": 2,
                    "window_start": "2026-09-01T00:00:00Z",
                    "window_end": "2026-09-01T00:50:00Z",
                    "max_window_minutes": 50,
                    "params": {"time_type": "2"},
                }
            ],
        },
    }


def _settings(**overrides):
    values = {
        "APP_ENV": "test",
        "PUBLIC_DATA_RDS_HOST": "mysql",
        "PUBLIC_DATA_RDS_PORT": "3306",
        "PUBLIC_DATA_RDS_USER": "public_data_test",
        "PUBLIC_DATA_RDS_PASSWORD": "local-only",
        "PUBLIC_DATA_DINGTALK_DATABASE": "raw_dingtalk_test",
        "PUBLIC_DATA_WDT_DATABASE": "raw_wdt_test",
        "PUBLIC_DATA_MART_DATABASE": "mart_ops_test",
    }
    values.update(overrides)
    conn = {
        "host": values["PUBLIC_DATA_RDS_HOST"],
        "port": int(values["PUBLIC_DATA_RDS_PORT"]),
        "user": values["PUBLIC_DATA_RDS_USER"],
        "password": values["PUBLIC_DATA_RDS_PASSWORD"],
    }
    return Settings(
        app_env=values["APP_ENV"],
        dingtalk_database=DatabaseSettings(name=values["PUBLIC_DATA_DINGTALK_DATABASE"], **conn),
        wdt_database=DatabaseSettings(name=values["PUBLIC_DATA_WDT_DATABASE"], **conn),
        mart_database=DatabaseSettings(name=values["PUBLIC_DATA_MART_DATABASE"], **conn),
        source_config_path=Path("/tmp/unused-manifest.json"),
    )


class ManifestTests(unittest.TestCase):
    def test_loads_version_one_with_registered_tables_and_methods(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(_manifest()), encoding="utf-8")
            manifest = load_manifest(path)

        self.assertEqual("finance_store_commission", manifest.dingtalk_sheets[0].dataset)
        self.assertEqual("sales.TradeQuery.queryWithDetail", manifest.wdt_datasets[0].method)

    def test_rejects_unknown_version(self):
        value = _manifest()
        value["version"] = 2
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ManifestError, "version"):
                load_manifest(path)

    def test_rejects_unknown_top_level_key(self):
        value = _manifest()
        value["extra"] = "bad"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ManifestError, "unknown"):
                load_manifest(path)

    def test_rejects_unallowed_wdt_method(self):
        value = _manifest()
        value["wdt"]["datasets"][0]["method"] = "stock.adjust"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ManifestError, "method"):
                load_manifest(path)

    def test_rejects_overlapping_wdt_windows(self):
        value = _manifest()
        duplicate = dict(value["wdt"]["datasets"][0])
        duplicate["dataset"] = "overlap"
        duplicate["window_start"] = "2026-09-01T00:25:00Z"
        value["wdt"]["datasets"].append(duplicate)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ManifestError, "overlap"):
                load_manifest(path)

    def test_rejects_non_utc_timestamp(self):
        value = _manifest()
        value["wdt"]["datasets"][0]["window_start"] = "2026-09-01T00:00:00+08:00"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ManifestError, "UTC"):
                load_manifest(path)

    def test_manifest_has_deterministic_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            raw = json.dumps(_manifest(), ensure_ascii=False, sort_keys=True)
            path.write_text(raw, encoding="utf-8")
            m1 = load_manifest(path)
            m2 = load_manifest(path)

        self.assertEqual(m1.sha256, m2.sha256)
        self.assertEqual(64, len(m1.sha256))


class OrgManifestTests(unittest.TestCase):
    """``dingtalk.org``: the contact-directory declaration."""

    _SHIPPED = (
        Path(__file__).resolve().parents[2]
        / "docker" / "integration" / "source-manifest.json"
    )

    def _write(self, value):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "manifest.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_absent_org_is_none(self):
        manifest = load_manifest(self._write(_manifest()))
        self.assertIsNone(manifest.dingtalk_org)

    def test_loads_org_declaration(self):
        value = _manifest()
        value["dingtalk"]["org"] = {
            "dataset": "org_directory",
            "target_table": "dingtalk_org_member",
        }
        manifest = load_manifest(self._write(value))
        self.assertEqual("org_directory", manifest.dingtalk_org.dataset)
        self.assertEqual("dingtalk_org_member", manifest.dingtalk_org.target_table)

    def test_rejects_org_with_wrong_target_table(self):
        value = _manifest()
        value["dingtalk"]["org"] = {
            "dataset": "org_directory",
            "target_table": "fin_store_commission",
        }
        with self.assertRaisesRegex(ManifestError, "dingtalk_org_member"):
            load_manifest(self._write(value))

    def test_rejects_org_dataset_colliding_with_a_sheet(self):
        value = _manifest()
        value["dingtalk"]["org"] = {
            "dataset": "finance_store_commission",
            "target_table": "dingtalk_org_member",
        }
        with self.assertRaisesRegex(ManifestError, "duplicate dataset"):
            load_manifest(self._write(value))

    def test_rejects_non_object_org(self):
        value = _manifest()
        value["dingtalk"]["org"] = ["org_directory"]
        with self.assertRaisesRegex(ManifestError, "dingtalk.org"):
            load_manifest(self._write(value))

    def test_rejects_org_without_dataset(self):
        value = _manifest()
        value["dingtalk"]["org"] = {"target_table": "dingtalk_org_member"}
        with self.assertRaisesRegex(ManifestError, "dataset"):
            load_manifest(self._write(value))

    def test_shipped_manifest_declares_the_org_directory(self):
        manifest = load_manifest(self._SHIPPED)
        self.assertIsNotNone(manifest.dingtalk_org)
        self.assertEqual("org_directory", manifest.dingtalk_org.dataset)
        self.assertEqual("dingtalk_org_member", manifest.dingtalk_org.target_table)


class ExtractSafetyTests(unittest.TestCase):
    """``require_extract_run``: the extraction layer's write gate."""

    def test_accepts_confirm_and_the_local_test_boundary(self):
        settings = _settings()
        with patch.dict(os.environ, {"INTEGRATION_TEST_RUNNER": "1"}, clear=True):
            require_extract_run(settings, confirm_local_test_write=True)

    def test_rejects_missing_confirm_flag(self):
        settings = _settings()
        with patch.dict(os.environ, {"INTEGRATION_TEST_RUNNER": "1"}, clear=True):
            with self.assertRaisesRegex(LiveRunRejected, "--confirm-local-test-write"):
                require_extract_run(settings, confirm_local_test_write=False)

    def test_rejects_non_mysql_host(self):
        settings = _settings(PUBLIC_DATA_RDS_HOST="127.0.0.1")
        with patch.dict(os.environ, {"INTEGRATION_TEST_RUNNER": "1"}, clear=True):
            with self.assertRaisesRegex(LiveRunRejected, "PUBLIC_DATA_RDS_HOST"):
                require_extract_run(settings, confirm_local_test_write=True)

    def test_rejects_wrong_database_name(self):
        settings = _settings(PUBLIC_DATA_MART_DATABASE="mart_ops_shadow_test")
        with patch.dict(os.environ, {"INTEGRATION_TEST_RUNNER": "1"}, clear=True):
            with self.assertRaisesRegex(LiveRunRejected, "mart_ops_test"):
                require_extract_run(settings, confirm_local_test_write=True)


class LiveSafetyTests(unittest.TestCase):
    def test_accepts_both_flags_and_exact_local_docker_test_boundary(self):
        settings = _settings()
        with patch.dict(os.environ, {"INTEGRATION_TEST_RUNNER": "1"}, clear=True):
            require_live_run(settings, live_read=True, confirm_local_test_write=True)

    def test_rejects_missing_live_read_flag(self):
        settings = _settings()
        with patch.dict(os.environ, {"INTEGRATION_TEST_RUNNER": "1"}, clear=True):
            with self.assertRaisesRegex(LiveRunRejected, "--live-read"):
                require_live_run(settings, live_read=False, confirm_local_test_write=True)

    def test_rejects_missing_confirm_flag(self):
        settings = _settings()
        with patch.dict(os.environ, {"INTEGRATION_TEST_RUNNER": "1"}, clear=True):
            with self.assertRaisesRegex(LiveRunRejected, "--confirm-local-test-write"):
                require_live_run(settings, live_read=True, confirm_local_test_write=False)

    def test_rejects_non_mysql_host(self):
        settings = _settings(PUBLIC_DATA_RDS_HOST="127.0.0.1")
        with patch.dict(os.environ, {"INTEGRATION_TEST_RUNNER": "1"}, clear=True):
            with self.assertRaisesRegex(LiveRunRejected, "PUBLIC_DATA_RDS_HOST"):
                require_live_run(settings, live_read=True, confirm_local_test_write=True)

    def test_rejects_wrong_database_name(self):
        settings = _settings(PUBLIC_DATA_WDT_DATABASE="raw_wdt_shadow_test")
        with patch.dict(os.environ, {"INTEGRATION_TEST_RUNNER": "1"}, clear=True):
            with self.assertRaisesRegex(LiveRunRejected, "raw_wdt_test"):
                require_live_run(settings, live_read=True, confirm_local_test_write=True)

    def test_rejects_missing_integration_runner(self):
        settings = _settings()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(LiveRunRejected, "INTEGRATION_TEST_RUNNER"):
                require_live_run(settings, live_read=True, confirm_local_test_write=True)
