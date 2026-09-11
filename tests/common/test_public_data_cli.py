"""Tests for the non-leaking live sync CLI."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from common.public_data.cli import load_source_credentials, main


class LoadSourceCredentialsTests(unittest.TestCase):
    """Tests for credential file validation."""

    def _write_credentials(self, data):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(data, tmp)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def test_valid_credentials_are_loaded(self):
        path = self._write_credentials({
            "dingtalk": {
                "app_key": "key1",
                "app_secret": "secret1",
                "operator_id": "op1",
            },
            "wdt": {
                "sid": "sid1",
                "app_key": "key2",
                "app_secret": "secret2",
            },
        })
        creds = load_source_credentials(path)
        self.assertEqual(creds["dingtalk"]["app_key"], "key1")
        self.assertEqual(creds["wdt"]["sid"], "sid1")

    def test_rejects_missing_dingtalk_keys(self):
        path = self._write_credentials({
            "dingtalk": {"app_key": "key1"},
            "wdt": {"sid": "s", "app_key": "k", "app_secret": "s"},
        })
        with self.assertRaises(ValueError) as ctx:
            load_source_credentials(path)
        self.assertIn("invalid source credentials", str(ctx.exception))

    def test_rejects_missing_wdt_keys(self):
        path = self._write_credentials({
            "dingtalk": {"app_key": "k", "app_secret": "s", "operator_id": "o"},
            "wdt": {"sid": "s"},
        })
        with self.assertRaises(ValueError) as ctx:
            load_source_credentials(path)
        self.assertIn("invalid source credentials", str(ctx.exception))

    def test_rejects_empty_string_values(self):
        path = self._write_credentials({
            "dingtalk": {"app_key": "", "app_secret": "s", "operator_id": "o"},
            "wdt": {"sid": "s", "app_key": "k", "app_secret": "s"},
        })
        with self.assertRaises(ValueError) as ctx:
            load_source_credentials(path)
        self.assertIn("invalid source credentials", str(ctx.exception))

    def test_rejects_non_string_values(self):
        path = self._write_credentials({
            "dingtalk": {"app_key": 123, "app_secret": "s", "operator_id": "o"},
            "wdt": {"sid": "s", "app_key": "k", "app_secret": "s"},
        })
        with self.assertRaises(ValueError) as ctx:
            load_source_credentials(path)
        self.assertIn("invalid source credentials", str(ctx.exception))

    def test_error_message_does_not_contain_values(self):
        path = self._write_credentials({
            "dingtalk": {"app_key": "SUPERSECRET"},
            "wdt": {"sid": "s", "app_key": "k", "app_secret": "s"},
        })
        with self.assertRaises(ValueError) as ctx:
            load_source_credentials(path)
        message = str(ctx.exception)
        self.assertNotIn("SUPERSECRET", message)
        self.assertIn("invalid source credentials", message)

    def test_rejects_missing_top_level_section(self):
        path = self._write_credentials({
            "dingtalk": {"app_key": "k", "app_secret": "s", "operator_id": "o"},
        })
        with self.assertRaises(ValueError) as ctx:
            load_source_credentials(path)
        self.assertIn("invalid source credentials", str(ctx.exception))

    def test_source_filter_requires_only_that_section(self):
        path = self._write_credentials({
            "wdt": {"sid": "sid1", "app_key": "k", "app_secret": "s"},
        })
        creds = load_source_credentials(path, source="wdt")
        self.assertEqual(creds["wdt"]["sid"], "sid1")
        with self.assertRaises(ValueError):
            load_source_credentials(path, source="dingtalk")


class LiveSyncCliTests(unittest.TestCase):
    def test_live_sync_rejects_missing_confirmation_before_credentials_or_gateways(self):
        with patch("common.public_data.cli.load_settings") as load_settings, \
             patch("common.public_data.cli.load_source_credentials") as load_credentials, \
             patch("common.public_data.cli.build_gateways") as build_gateways:
            with self.assertRaises(SystemExit) as raised:
                main(["live-sync", "--live-read"])

        self.assertNotEqual(0, raised.exception.code)
        load_settings.assert_not_called()
        load_credentials.assert_not_called()
        build_gateways.assert_not_called()

    def test_live_sync_rejects_missing_live_read_flag(self):
        with patch("common.public_data.cli.load_settings") as load_settings:
            with self.assertRaises(SystemExit) as raised:
                main(["live-sync", "--confirm-local-test-write",
                       "--source-credentials", "/tmp/creds.json"])

        self.assertNotEqual(0, raised.exception.code)
        load_settings.assert_not_called()

    def test_live_sync_prints_only_safe_summary(self):
        output = io.StringIO()
        with patch("common.public_data.cli.load_settings"), \
             patch("common.public_data.cli.require_live_run"), \
             patch("common.public_data.cli.load_manifest"), \
             patch("common.public_data.cli.load_source_credentials"), \
             patch("common.public_data.cli.build_service") as build_service:
            build_service.return_value.sync.return_value = {
                "run_id": "00000000-0000-0000-0000-000000000001",
                "datasets": [{
                    "source": "wdt",
                    "dataset": "trade-window",
                    "records_read": 2,
                    "raw_records_written": 2,
                    "record_id_digest": "a" * 64,
                }],
            }
            with redirect_stdout(output):
                main([
                    "live-sync",
                    "--live-read",
                    "--confirm-local-test-write",
                    "--source-credentials", "/run/live-input/source-credentials.json",
                ])

        text = output.getvalue()
        self.assertIn("trade-window", text)
        self.assertIn("records_read=2", text)
        self.assertNotIn("appSecret", text)
        self.assertNotIn("payload_json", text)

    def test_live_sync_returns_nonzero_on_sync_failure(self):
        output = io.StringIO()
        with patch("common.public_data.cli.load_settings"), \
             patch("common.public_data.cli.require_live_run"), \
             patch("common.public_data.cli.load_manifest"), \
             patch("common.public_data.cli.load_source_credentials"), \
             patch("common.public_data.cli.build_service") as build_service:
            build_service.return_value.sync.side_effect = RuntimeError("boom")
            with redirect_stdout(output):
                with self.assertRaises(SystemExit) as raised:
                    main([
                        "live-sync",
                        "--live-read",
                        "--confirm-local-test-write",
                        "--source-credentials", "/run/live-input/source-credentials.json",
                    ])

        self.assertNotEqual(0, raised.exception.code)
        text = output.getvalue()
        self.assertIn("status=failed", text)
        self.assertNotIn("boom", text)

    def test_live_sync_failure_does_not_leak_traceback(self):
        output = io.StringIO()
        with patch("common.public_data.cli.load_settings"), \
             patch("common.public_data.cli.require_live_run"), \
             patch("common.public_data.cli.load_manifest"), \
             patch("common.public_data.cli.load_source_credentials"), \
             patch("common.public_data.cli.build_service") as build_service:
            build_service.return_value.sync.side_effect = RuntimeError("secret-info")
            with redirect_stdout(output):
                with self.assertRaises(SystemExit):
                    main([
                        "live-sync",
                        "--live-read",
                        "--confirm-local-test-write",
                        "--source-credentials", "/run/live-input/source-credentials.json",
                    ])

        text = output.getvalue()
        self.assertNotIn("Traceback", text)
        self.assertNotIn("secret-info", text)

    def test_live_sync_passes_source_to_credentials_and_sync(self):
        with patch("common.public_data.cli.load_settings"), \
             patch("common.public_data.cli.require_live_run"), \
             patch("common.public_data.cli.load_manifest"), \
             patch("common.public_data.cli.load_source_credentials") as load_credentials, \
             patch("common.public_data.cli.build_service") as build_service:
            build_service.return_value.sync.return_value = {
                "run_id": "00000000-0000-0000-0000-000000000003",
                "datasets": [],
            }
            main([
                "live-sync", "--live-read", "--confirm-local-test-write",
                "--source-credentials", "/creds.json", "--source", "wdt",
            ])

        load_credentials.assert_called_once_with("/creds.json", source="wdt")
        sync_kwargs = build_service.return_value.sync.call_args.kwargs
        self.assertEqual(sync_kwargs.get("source"), "wdt")


class PipelineGateCliTests(unittest.TestCase):
    """The registry enable gate around live-sync."""

    def test_live_sync_skips_when_pipeline_disabled(self):
        from common.public_data.pipeline_config import StaticConfigSource

        output = io.StringIO()
        with patch("common.public_data.cli.load_settings"), \
             patch("common.public_data.cli.require_live_run"), \
             patch("common.public_data.cli.resolve_service_id",
                   return_value="sync-wdt"), \
             patch("common.public_data.cli.build_pipeline_config_source") as build_source, \
             patch("common.public_data.cli.load_manifest") as load_manifest, \
             patch("common.public_data.cli.build_service") as build_service:
            build_source.return_value = StaticConfigSource({"sync-wdt": {"enabled": False}})
            with redirect_stdout(output):
                main([
                    "live-sync", "--live-read", "--confirm-local-test-write",
                    "--source-credentials", "/creds.json",
                ])

        text = output.getvalue()
        self.assertIn("service=sync-wdt", text)
        self.assertIn("status=skipped", text)
        load_manifest.assert_not_called()
        build_service.assert_not_called()

    def test_live_sync_runs_when_pipeline_enabled(self):
        from common.public_data.pipeline_config import StaticConfigSource

        with patch("common.public_data.cli.load_settings"), \
             patch("common.public_data.cli.require_live_run"), \
             patch("common.public_data.cli.resolve_service_id",
                   return_value="sync-wdt"), \
             patch("common.public_data.cli.build_pipeline_config_source") as build_source, \
             patch("common.public_data.cli.load_manifest"), \
             patch("common.public_data.cli.load_source_credentials"), \
             patch("common.public_data.cli.build_service") as build_service:
            build_source.return_value = StaticConfigSource({"sync-wdt": {"enabled": True}})
            build_service.return_value.sync.return_value = {"run_id": "r", "datasets": []}
            main([
                "live-sync", "--live-read", "--confirm-local-test-write",
                "--source-credentials", "/creds.json",
            ])

        build_service.return_value.sync.assert_called_once()

    def test_live_sync_fails_open_when_registry_errors(self):
        with patch("common.public_data.cli.load_settings"), \
             patch("common.public_data.cli.require_live_run"), \
             patch("common.public_data.cli.resolve_service_id",
                   return_value="sync-wdt"), \
             patch("common.public_data.cli.build_pipeline_config_source",
                   side_effect=RuntimeError("nacos down")), \
             patch("common.public_data.cli.load_manifest"), \
             patch("common.public_data.cli.load_source_credentials"), \
             patch("common.public_data.cli.build_service") as build_service:
            build_service.return_value.sync.return_value = {"run_id": "r", "datasets": []}
            main([
                "live-sync", "--live-read", "--confirm-local-test-write",
                "--source-credentials", "/creds.json",
            ])

        build_service.return_value.sync.assert_called_once()

    def test_service_flag_overrides_env(self):
        with patch("common.public_data.cli.load_settings"), \
             patch("common.public_data.cli.require_live_run"), \
             patch("common.public_data.cli.resolve_service_id") as resolve, \
             patch("common.public_data.cli.load_manifest"), \
             patch("common.public_data.cli.load_source_credentials"), \
             patch("common.public_data.cli.build_service") as build_service:
            resolve.return_value = None
            build_service.return_value.sync.return_value = {"run_id": "r", "datasets": []}
            main([
                "live-sync", "--live-read", "--confirm-local-test-write",
                "--source-credentials", "/creds.json", "--service", "sync-dingtalk",
            ])

        resolve.assert_called_once_with("sync-dingtalk")


class PublishPipelinesCliTests(unittest.TestCase):
    def test_publish_pipelines_prints_count(self):
        output = io.StringIO()
        with patch("common.public_data.cli.publish_pipeline_seed",
                   return_value=4) as publish:
            with redirect_stdout(output):
                main(["publish-pipelines", "--seed", "/seed.yaml"])

        publish.assert_called_once_with("/seed.yaml", if_missing=False)
        self.assertIn("published=4", output.getvalue())

    def test_publish_pipelines_failure_is_safe(self):
        output = io.StringIO()
        with patch("common.public_data.cli.publish_pipeline_seed",
                   side_effect=RuntimeError("secret-host")):
            with redirect_stdout(output):
                with self.assertRaises(SystemExit):
                    main(["publish-pipelines", "--seed", "/seed.yaml"])

        text = output.getvalue()
        self.assertIn("status=failed", text)
        self.assertNotIn("secret-host", text)


class RebuildProjectionCliTests(unittest.TestCase):
    def test_rebuild_projection_requires_confirmation(self):
        with patch("common.public_data.cli.load_settings") as load_settings:
            with self.assertRaises(SystemExit) as raised:
                main(["rebuild-projection", "some-run-id"])

        self.assertNotEqual(0, raised.exception.code)
        load_settings.assert_not_called()

    def test_rebuild_projection_prints_safe_summary(self):
        output = io.StringIO()
        with patch("common.public_data.cli.load_settings"), \
             patch("common.public_data.cli.require_live_run"), \
             patch("common.public_data.cli.load_manifest"), \
             patch("common.public_data.cli.build_service") as build_service:
            build_service.return_value.rebuild_projection.return_value = {
                "run_id": "00000000-0000-0000-0000-000000000002",
                "datasets": [{
                    "source": "wdt",
                    "dataset": "trade-window",
                    "records_read": 5,
                }],
            }
            with redirect_stdout(output):
                main([
                    "rebuild-projection",
                    "00000000-0000-0000-0000-000000000002",
                    "--confirm-local-test-write",
                ])

        text = output.getvalue()
        self.assertIn("trade-window", text)
        self.assertIn("status=completed", text)


if __name__ == "__main__":
    unittest.main()
