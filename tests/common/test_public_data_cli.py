"""Tests for the non-leaking live sync CLI."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

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


class LiveSyncOrgCliTests(unittest.TestCase):
    """Org-seed wiring of the live-sync subcommand."""

    def _run(self, *, settings, manifest_org):
        with patch("common.public_data.cli.load_settings", return_value=settings), \
             patch("common.public_data.cli.require_live_run"), \
             patch("common.public_data.cli.load_manifest") as load_manifest, \
             patch("common.public_data.cli.load_source_credentials"), \
             patch("common.public_data.cli.load_org_seed") as load_org, \
             patch("common.public_data.cli.build_service") as build_service:
            load_manifest.return_value.dingtalk_org = manifest_org
            load_org.return_value = (("hangzhou", (1049728636,)),)
            build_service.return_value.sync.return_value = {
                "run_id": "r", "datasets": [],
            }
            main([
                "live-sync", "--live-read", "--confirm-local-test-write",
                "--source-credentials", "/creds.json",
            ])
        return load_org, build_service

    def test_declared_and_seeded_passes_regions_to_the_service(self):
        load_org, build_service = self._run(
            settings=Mock(org_seed_path=Path("/app/org.seed.json")),
            manifest_org=Mock(),
        )
        load_org.assert_called_once_with(Path("/app/org.seed.json"))
        self.assertEqual(
            build_service.call_args.args[3], (("hangzhou", (1049728636,)),),
        )

    def test_declared_without_a_seed_passes_empty_regions(self):
        load_org, build_service = self._run(
            settings=Mock(org_seed_path=None),
            manifest_org=Mock(),
        )
        load_org.assert_not_called()
        self.assertEqual(build_service.call_args.args[3], ())

    def test_undeclared_org_never_reads_the_seed(self):
        load_org, build_service = self._run(
            settings=Mock(org_seed_path=Path("/app/org.seed.json")),
            manifest_org=None,
        )
        load_org.assert_not_called()
        self.assertEqual(build_service.call_args.args[3], ())


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


class ExtractMartCliTests(unittest.TestCase):
    """The extraction-layer subcommand and its safety/wiring gates."""

    def _patch_common(self, settings):
        return (
            patch("common.public_data.cli.load_settings", return_value=settings),
            patch("common.public_data.cli.require_extract_run"),
        )

    def test_extract_mart_requires_confirmation_before_any_work(self):
        with patch("common.public_data.cli.load_settings") as load_settings:
            with self.assertRaises(SystemExit) as raised:
                main(["extract-mart"])

        self.assertNotEqual(0, raised.exception.code)
        load_settings.assert_not_called()

    def test_extract_mart_prints_only_a_safe_summary(self):
        output = io.StringIO()
        settings = Mock(calendar_seed_path=None)
        load_patch, gate_patch = self._patch_common(settings)
        with load_patch, gate_patch, \
             patch("common.public_data.cli.build_extract_service") as build_service:
            build_service.return_value.extract.return_value = {
                "run_id": "00000000-0000-0000-0000-000000000009",
                "datasets": [{
                    "source": "extract",
                    "dataset": "fact_daily_report_offline",
                    "records_read": 3,
                    "raw_records_written": 3,
                    "record_id_digest": "b" * 64,
                }],
            }
            with redirect_stdout(output):
                main(["extract-mart", "--confirm-local-test-write"])

        text = output.getvalue()
        self.assertIn("fact_daily_report_offline", text)
        self.assertIn("records_read=3", text)
        self.assertIn("status=completed", text)
        self.assertNotIn("payload_json", text)

    def test_extract_mart_reads_the_calendar_seed_when_configured(self):
        settings = Mock(calendar_seed_path=Path("/app/calendar.seed.json"))
        load_patch, gate_patch = self._patch_common(settings)
        with load_patch, gate_patch, \
             patch("common.public_data.cli.load_calendar_seed",
                   return_value=[(2026, 9, [6, 13, 19], "local")]) as load_seed, \
             patch("common.public_data.cli.build_extract_service") as build_service:
            build_service.return_value.extract.return_value = {
                "run_id": "r", "datasets": [],
            }
            main(["extract-mart", "--confirm-local-test-write"])

        load_seed.assert_called_once_with(Path("/app/calendar.seed.json"))
        self.assertEqual(
            build_service.call_args.args[1], ((2026, 9, [6, 13, 19], "local"),),
        )

    def test_extract_mart_skips_the_calendar_step_without_a_seed(self):
        settings = Mock(calendar_seed_path=None)
        load_patch, gate_patch = self._patch_common(settings)
        with load_patch, gate_patch, \
             patch("common.public_data.cli.load_calendar_seed") as load_seed, \
             patch("common.public_data.cli.build_extract_service") as build_service:
            build_service.return_value.extract.return_value = {
                "run_id": "r", "datasets": [],
            }
            main(["extract-mart", "--confirm-local-test-write"])

        load_seed.assert_not_called()
        self.assertEqual(build_service.call_args.args[1], ())

    def test_extract_mart_failure_is_safe(self):
        output = io.StringIO()
        settings = Mock(calendar_seed_path=None)
        load_patch, gate_patch = self._patch_common(settings)
        with load_patch, gate_patch, \
             patch("common.public_data.cli.build_extract_service") as build_service:
            build_service.return_value.extract.side_effect = RuntimeError("secret-host")
            with redirect_stdout(output):
                with self.assertRaises(SystemExit) as raised:
                    main(["extract-mart", "--confirm-local-test-write"])

        self.assertNotEqual(0, raised.exception.code)
        text = output.getvalue()
        self.assertIn("status=failed", text)
        self.assertNotIn("secret-host", text)
        self.assertNotIn("Traceback", text)

    def test_extract_mart_skips_when_pipeline_disabled(self):
        from common.public_data.pipeline_config import StaticConfigSource

        output = io.StringIO()
        settings = Mock(calendar_seed_path=None)
        load_patch, gate_patch = self._patch_common(settings)
        with load_patch, gate_patch, \
             patch("common.public_data.cli.resolve_service_id",
                   return_value="extract-mart"), \
             patch("common.public_data.cli.build_pipeline_config_source") as build_source, \
             patch("common.public_data.cli.build_extract_service") as build_service:
            build_source.return_value = StaticConfigSource(
                {"extract-mart": {"enabled": False}}
            )
            with redirect_stdout(output):
                main(["extract-mart", "--confirm-local-test-write"])

        text = output.getvalue()
        self.assertIn("service=extract-mart", text)
        self.assertIn("status=skipped", text)
        build_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
