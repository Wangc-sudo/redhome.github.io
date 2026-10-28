"""Tests for the non-leaking live sync CLI."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from common.public_data.cli import load_source_credentials, main
from common.public_data.org_read import OrgSeedEntry


class BuildExtractServiceTests(unittest.TestCase):
    @patch("common.public_data.live_migrations.apply_live_migrations")
    @patch("common.public_data.db.connect")
    def test_passes_local_wdt_connection_to_repository(self, connect, migrate):
        from common.public_data.cli import build_extract_service

        raw, wdt, mart = Mock(), Mock(), Mock()
        connect.side_effect = [raw, wdt, mart]
        service = build_extract_service(Mock())
        self.assertIs(service._repository.wdt_connection, wdt)
        migrate.assert_called_once_with(raw, wdt, mart)


class BuildDataQualityRerunWorkerTests(unittest.TestCase):
    @patch("common.public_data.live_migrations.apply_live_migrations")
    @patch("common.public_data.db.connect")
    def test_assembles_shared_connections_and_closes_them(self, connect, migrate):
        from common.public_data.cli import build_data_quality_rerun_worker

        settings = Mock()
        raw, wdt, mart = Mock(), Mock(), Mock()
        connect.side_effect = [raw, wdt, mart]

        with build_data_quality_rerun_worker(settings) as worker:
            self.assertIs(worker.repository.connection, mart)
            self.assertIs(worker.runner.repository, worker.repository)
            self.assertIs(worker.runner.source.raw, raw)
            self.assertIs(worker.runner.source.wdt, wdt)
            replay = worker.replay_service
            self.assertIs(replay._repository.raw_connection, raw)
            self.assertIs(replay._repository.wdt_connection, wdt)
            self.assertIs(replay._repository._mart, mart)
            self.assertIs(replay._mart_repository._connection, mart)
            self.assertIs(replay._mart_connection, mart)
            self.assertIs(worker.outbox._connection, mart)
            self.assertIs(worker.lock_connection, mart)
            for connection in (raw, wdt, mart):
                connection.close.assert_not_called()

        migrate.assert_called_once_with(raw, wdt, mart)
        for connection in (raw, wdt, mart):
            connection.close.assert_called_once_with()

    @patch("common.public_data.live_migrations.apply_live_migrations")
    @patch("common.public_data.db.connect")
    def test_closes_all_connections_when_running_raises(self, connect, migrate):
        from common.public_data.cli import build_data_quality_rerun_worker

        raw, wdt, mart = Mock(), Mock(), Mock()
        connect.side_effect = [raw, wdt, mart]
        with self.assertRaisesRegex(RuntimeError, "run failed"):
            with build_data_quality_rerun_worker(Mock()):
                raise RuntimeError("run failed")

        migrate.assert_called_once_with(raw, wdt, mart)
        for connection in (raw, wdt, mart):
            connection.close.assert_called_once_with()

    @patch("common.public_data.live_migrations.apply_live_migrations")
    @patch("common.public_data.db.connect")
    def test_closes_partial_connections_when_a_later_connect_fails(
        self, connect, migrate
    ):
        from common.public_data.cli import build_data_quality_rerun_worker

        raw = Mock()
        connect.side_effect = [raw, RuntimeError("database unavailable")]
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            with build_data_quality_rerun_worker(Mock()):
                pass

        raw.close.assert_called_once_with()
        migrate.assert_not_called()

    @patch("common.public_data.live_migrations.apply_live_migrations")
    @patch("common.public_data.db.connect")
    def test_closes_all_connections_when_builder_initialization_fails(
        self, connect, migrate
    ):
        from common.public_data.cli import build_data_quality_rerun_worker

        raw, wdt, mart = Mock(), Mock(), Mock()
        connect.side_effect = [raw, wdt, mart]
        migrate.side_effect = RuntimeError("migration failed")
        with self.assertRaisesRegex(RuntimeError, "migration failed"):
            with build_data_quality_rerun_worker(Mock()):
                pass

        for connection in (raw, wdt, mart):
            connection.close.assert_called_once_with()


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
             patch("common.public_data.cli.load_multi_org_seed") as load_org, \
             patch("common.public_data.cli.build_service") as build_service:
            load_manifest.return_value.dingtalk_org = manifest_org
            load_org.return_value = (OrgSeedEntry(
                key="default", credential="dingtalk",
                regions=(("hangzhou", (1049728636,)),)),)
            build_service.return_value.sync.return_value = {
                "run_id": "r", "datasets": [],
            }
            main([
                "live-sync", "--live-read", "--confirm-local-test-write",
                "--source-credentials", "/creds.json",
            ])
        return load_org, build_service

    def test_declared_and_seeded_passes_seeds_to_the_service(self):
        load_org, build_service = self._run(
            settings=Mock(org_seed_path=Path("/app/org.seed.json")),
            manifest_org=Mock(),
        )
        load_org.assert_called_once_with(Path("/app/org.seed.json"))
        seeds = build_service.call_args.kwargs["org_seeds"]
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0].key, "default")
        self.assertEqual(seeds[0].credential, "dingtalk")

    def test_declared_without_a_seed_passes_empty_seeds(self):
        load_org, build_service = self._run(
            settings=Mock(org_seed_path=None),
            manifest_org=Mock(),
        )
        load_org.assert_not_called()
        self.assertEqual(build_service.call_args.kwargs["org_seeds"], ())

    def test_undeclared_org_never_reads_the_seed(self):
        load_org, build_service = self._run(
            settings=Mock(org_seed_path=Path("/app/org.seed.json")),
            manifest_org=None,
        )
        load_org.assert_not_called()
        self.assertEqual(build_service.call_args.kwargs["org_seeds"], ())


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


class DataQualityCliTests(unittest.TestCase):
    def test_requires_confirmation_before_any_work(self):
        with patch("common.public_data.cli.load_settings") as load_settings:
            with self.assertRaises(SystemExit) as raised:
                main(["data-quality-check", "--dataset", "channel_daily_sales"])
        self.assertNotEqual(0, raised.exception.code)
        load_settings.assert_not_called()

    def test_seeds_rules_and_prints_safe_summary(self):
        output = io.StringIO()
        runner = Mock()
        runner.run.return_value = {
            "run_id": "dq-run-1", "dataset": "channel_daily_sales",
            "rule_version": "abc123", "status": "finished",
            "alert_status": "warning", "records_read": 10,
            "records_passed": 9, "records_quarantined": 1, "issue_count": 1,
        }
        with patch("common.public_data.cli.load_settings"), \
             patch("common.public_data.cli.require_extract_run"), \
             patch("common.public_data.cli.build_data_quality_runner",
                   return_value=runner):
            with redirect_stdout(output):
                main([
                    "data-quality-check", "--confirm-local-test-write",
                    "--dataset", "channel_daily_sales",
                    "--window-start", "2026-10-26", "--window-end", "2026-10-26",
                ])
        runner.repository.seed_default_rules.assert_called_once_with(actor="system")
        self.assertEqual("2026-10-26", runner.run.call_args.kwargs["window_start"])
        self.assertEqual("channel_daily_sales", runner.run.call_args.args[0])
        text = output.getvalue()
        self.assertIn("run_id=dq-run-1", text)
        self.assertIn("records_quarantined=1", text)
        self.assertNotIn("payload_json", text)

    def test_failure_is_safe(self):
        output = io.StringIO()
        runner = Mock()
        runner.run.side_effect = RuntimeError("secret-host")
        with patch("common.public_data.cli.load_settings"), \
             patch("common.public_data.cli.require_extract_run"), \
             patch("common.public_data.cli.build_data_quality_runner",
                   return_value=runner):
            with redirect_stdout(output):
                with self.assertRaises(SystemExit):
                    main([
                        "data-quality-check", "--confirm-local-test-write",
                        "--dataset", "wdt_order_line_fact",
                    ])
        text = output.getvalue()
        self.assertIn("code=data_quality_error", text)
        self.assertNotIn("secret-host", text)
        self.assertNotIn("Traceback", text)


class DataQualityRerunCliTests(unittest.TestCase):
    def _invoke(self, result=None, *, side_effect=None, enabled=True):
        settings = Mock()
        worker = Mock()
        if side_effect is not None:
            worker.run_once.side_effect = side_effect
        else:
            worker.run_once.return_value = result
        builder = MagicMock()
        builder.return_value.__enter__.return_value = worker
        output = io.StringIO()

        with patch("common.public_data.cli.load_settings", return_value=settings), \
             patch("common.public_data.cli.require_extract_run") as gate, \
             patch("common.public_data.cli.resolve_service_id",
                   return_value="dq-rerun-worker"), \
             patch("common.public_data.cli._pipeline_enabled",
                   return_value=enabled), \
             patch("common.public_data.cli.build_data_quality_rerun_worker",
                   builder):
            with redirect_stdout(output):
                try:
                    exit_code = main([
                        "data-quality-rerun", "--confirm-local-test-write",
                    ])
                except SystemExit as exc:
                    exit_code = exc.code

        gate.assert_called_once_with(
            settings, confirm_local_test_write=True,
        )
        if enabled:
            builder.assert_called_once_with(settings)
            worker.run_once.assert_called_once_with()
            worker.replay_service.replay_dataset.assert_not_called()
        else:
            builder.assert_not_called()
            worker.run_once.assert_not_called()
        return exit_code, output.getvalue()

    def test_requires_confirmation_before_settings_or_worker_construction(self):
        with patch("common.public_data.cli.load_settings") as load_settings, \
             patch("common.public_data.cli.build_data_quality_rerun_worker") as builder:
            with self.assertRaises(SystemExit) as raised:
                main(["data-quality-rerun"])

        self.assertEqual(1, raised.exception.code)
        load_settings.assert_not_called()
        builder.assert_not_called()

    def test_idle_and_lock_unavailable_are_successful_skips(self):
        cases = (
            ({"status": "idle", "run_id": None}, "status=idle"),
            (
                {"status": "skipped", "reason": "lock_unavailable",
                 "run_id": None},
                "status=skipped reason=lock_unavailable",
            ),
        )
        for result, expected in cases:
            with self.subTest(result=result):
                exit_code, output = self._invoke(result)
                self.assertIsNone(exit_code)
                self.assertEqual(expected, output.strip())

    def test_finished_and_dry_run_results_use_worker_and_print_safe_summary(self):
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                result = {
                    "run_id": "dq-rerun-1", "dataset": "channel_daily_sales",
                    "status": "finished", "dry_run": dry_run,
                    "alert_status": "normal", "records_read": 4,
                    "records_passed": 4, "records_quarantined": 0,
                    "records_written": 0 if dry_run else 3,
                    "issue_count": 0, "issues_closed": 1,
                    "diff": {"payload_json": "secret-row"},
                    "samples": ["secret-sample"],
                }
                exit_code, output = self._invoke(result)

                self.assertIsNone(exit_code)
                self.assertIn("run_id=dq-rerun-1", output)
                self.assertIn("status=finished", output)
                self.assertIn(
                    f"records_written={0 if dry_run else 3}", output,
                )
                self.assertNotIn("diff", output)
                self.assertNotIn("secret-row", output)
                self.assertNotIn("secret-sample", output)

    def test_quality_gate_failure_returns_one_without_printing_diff(self):
        result = {
            "run_id": "dq-rerun-2", "dataset": "channel_daily_sales",
            "status": "failed", "failure_code": "quality_gate_blocked",
            "diff": {"before": "SELECT secret payload"},
        }
        exit_code, output = self._invoke(result)

        self.assertEqual(1, exit_code)
        self.assertIn("run_id=dq-rerun-2", output)
        self.assertIn("status=failed", output)
        self.assertIn("code=quality_gate_blocked", output)
        self.assertNotIn("diff", output)
        self.assertNotIn("SELECT", output)
        self.assertNotIn("secret", output)
        self.assertNotIn("Traceback", output)

    def test_worker_exception_returns_one_with_only_a_generic_safe_error(self):
        exit_code, output = self._invoke(
            side_effect=RuntimeError(
                "SELECT payload_json; password=secret; https://internal-host"
            )
        )

        self.assertEqual(1, exit_code)
        self.assertEqual(
            "run_id=unknown status=failed code=data_quality_rerun_error",
            output.strip(),
        )
        for sensitive in (
            "SELECT", "payload_json", "password", "secret", "https://",
            "Traceback",
        ):
            self.assertNotIn(sensitive, output)

    def test_disabled_pipeline_does_not_construct_or_run_worker(self):
        exit_code, output = self._invoke(enabled=False)

        self.assertIsNone(exit_code)
        self.assertEqual(
            "service=dq-rerun-worker status=skipped reason=disabled",
            output.strip(),
        )


class MigrateCliTests(unittest.TestCase):
    """`migrate`：manual（raw_manual）连接为可选（生产 1044 实测）。"""

    def _settings(self, manual="raw_manual"):
        return Mock(
            dingtalk_database="raw_dingtalk",
            wdt_database="raw_wdt",
            mart_database="mart_ops",
            manual_database=manual,
        )

    def test_continues_without_manual_when_that_database_is_denied(self):
        output = io.StringIO()
        raw, wdt, mart = Mock(), Mock(), Mock()
        with patch("common.public_data.cli.load_settings",
                   return_value=self._settings()), \
             patch("common.public_data.db.connect") as connect, \
             patch("common.public_data.live_migrations.apply_live_migrations") as migrate:
            # 生产账号对 raw_manual 无权限：第 4 次连接抛 1044 类异常
            connect.side_effect = [raw, wdt, mart, RuntimeError("Access denied")]
            with redirect_stdout(output):
                main(["migrate"])

        migrate.assert_called_once_with(
            raw, wdt, mart, manual_connection=None)
        self.assertIn("migrations applied", output.getvalue())

    def test_passes_manual_connection_through_when_reachable(self):
        raw, wdt, mart, manual = Mock(), Mock(), Mock(), Mock()
        with patch("common.public_data.cli.load_settings",
                   return_value=self._settings()), \
             patch("common.public_data.db.connect") as connect, \
             patch("common.public_data.live_migrations.apply_live_migrations") as migrate:
            connect.side_effect = [raw, wdt, mart, manual]
            main(["migrate"])

        migrate.assert_called_once_with(
            raw, wdt, mart, manual_connection=manual)

    def test_skips_manual_connect_when_not_configured(self):
        raw, wdt, mart = Mock(), Mock(), Mock()
        with patch("common.public_data.cli.load_settings",
                   return_value=self._settings(manual=None)), \
             patch("common.public_data.db.connect") as connect, \
             patch("common.public_data.live_migrations.apply_live_migrations") as migrate:
            connect.side_effect = [raw, wdt, mart]
            main(["migrate"])

        self.assertEqual(3, connect.call_count)
        migrate.assert_called_once_with(
            raw, wdt, mart, manual_connection=None)


class BackfillRegionTargetsCliTests(unittest.TestCase):
    """`backfill-region-targets`：Nacos monthlyTargets → dim_report_target（万元）。"""

    def test_requires_confirmation_before_any_work(self):
        with patch("common.public_data.cli.load_settings") as settings:
            with self.assertRaises(SystemExit) as ctx:
                main(["backfill-region-targets", "--month", "2026-10"])
        self.assertNotEqual(0, ctx.exception.code)
        settings.assert_not_called()

    def test_dry_run_reports_without_confirmation(self):
        output = io.StringIO()
        settings = Mock(region_seed_path=Path("/seed.json"),
                        mart_database="mart_ops")
        conn = Mock()
        with patch("common.public_data.cli.load_settings",
                   return_value=settings), \
             patch("common.public_data.db.connect", return_value=conn), \
             patch("common.region_config.load_region_seed",
                   return_value={"vanke": "cfg"}), \
             patch("common.region_config.build_nacos_region_overlay",
                   return_value=None), \
             patch("common.region_config.apply_region_overlay",
                   return_value={"vanke": "cfg"}), \
             patch("common.public_data.report_roster.backfill_targets_from_region_config",
                   return_value={"vanke": {"inserted": 2, "skipped": 0}}) as backfill:
            with redirect_stdout(output):
                main(["backfill-region-targets", "--month", "2026-10",
                      "--dry-run"])

        backfill.assert_called_once_with(
            conn, {"vanke": "cfg"}, "2026-10",
            actor="cli:backfill-targets", dry_run=True)
        self.assertIn("mode=dry-run", output.getvalue())
        self.assertIn("inserted=2", output.getvalue())

    def test_region_seed_required(self):
        settings = Mock(region_seed_path=None, mart_database="mart_ops")
        with patch("common.public_data.cli.load_settings",
                   return_value=settings), \
             patch("common.public_data.db.connect") as connect:
            with self.assertRaises(SystemExit):
                main(["backfill-region-targets", "--month", "2026-10",
                      "--confirm-local-test-write"])
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
