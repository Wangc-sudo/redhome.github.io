"""Tests for the dingtalk-gateway CLI (stage 4 gateway container entry)."""

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from common.gateway.cli import main
from common.gateway.delivery import DeliverReport


def _settings():
    return Mock(region_seed_path=Path("/app/regions.seed.json"))


def _patch_common():
    return {
        "settings": patch(
            "common.gateway.cli.load_settings", return_value=_settings()
        ),
        "gate": patch("common.gateway.cli.require_gateway_run"),
        "configs": patch(
            "common.gateway.cli.load_region_configs", return_value={}
        ),
        "creds": patch(
            "common.gateway.cli.load_source_credentials",
            return_value={"dingtalk": {
                "app_key": "k", "app_secret": "s", "operator_id": "o",
            }},
        ),
        "deliverer": patch("common.gateway.cli.build_deliverer"),
        "conn": patch("common.gateway.cli.connect_mart"),
        "worker": patch("common.gateway.cli.build_worker"),
    }


class GatewayCliTests(unittest.TestCase):

    def test_requires_live_send_before_any_work(self):
        with patch("common.gateway.cli.load_settings") as load_settings:
            with self.assertRaises(SystemExit) as raised:
                main([
                    "run", "--confirm-local-test-write",
                    "--source-credentials", "/c.json",
                ])
        self.assertNotEqual(0, raised.exception.code)
        load_settings.assert_not_called()

    def test_requires_confirmation_before_any_work(self):
        with patch("common.gateway.cli.load_settings") as load_settings:
            with self.assertRaises(SystemExit) as raised:
                main(["run", "--live-send", "--source-credentials", "/c.json"])
        self.assertNotEqual(0, raised.exception.code)
        load_settings.assert_not_called()

    def test_once_delivers_one_batch_and_prints_a_safe_report(self):
        mocks = _patch_common()
        output = io.StringIO()
        with mocks["settings"], mocks["gate"], mocks["configs"], \
             mocks["creds"], mocks["deliverer"], \
             mocks["conn"] as conn, mocks["worker"] as build_worker:
            build_worker.return_value.deliver_once.return_value = DeliverReport(
                delivered=("hangzhou:remind:2026-09-11",), failed=(),
            )
            with redirect_stdout(output):
                main([
                    "run", "--once", "--live-send",
                    "--confirm-local-test-write",
                    "--source-credentials", "/c.json",
                ])

        text = output.getvalue()
        self.assertIn("status=completed", text)
        self.assertIn("delivered=1", text)
        self.assertIn("failed=0", text)
        conn.return_value.commit.assert_called_once()
        build_worker.return_value.run_forever.assert_not_called()

    def test_long_running_mode_uses_the_interval(self):
        mocks = _patch_common()
        with mocks["settings"], mocks["gate"], mocks["configs"], \
             mocks["creds"], mocks["deliverer"], \
             mocks["conn"] as conn, mocks["worker"] as build_worker:
            main([
                "run", "--live-send", "--confirm-local-test-write",
                "--source-credentials", "/c.json", "--interval", "5",
            ])

        build_worker.return_value.run_forever.assert_called_once_with(
            interval_seconds=5, after_batch=conn.return_value.commit,
        )
        build_worker.return_value.deliver_once.assert_not_called()

    def test_region_seed_is_required(self):
        mocks = _patch_common()
        mocks["settings"] = patch(
            "common.gateway.cli.load_settings",
            return_value=Mock(region_seed_path=None),
        )
        output = io.StringIO()
        with mocks["settings"], mocks["gate"]:
            with redirect_stdout(output):
                with self.assertRaises(SystemExit) as raised:
                    main([
                        "run", "--once", "--live-send",
                        "--confirm-local-test-write",
                        "--source-credentials", "/c.json",
                    ])

        self.assertNotEqual(0, raised.exception.code)
        self.assertIn("code=region_seed_required", output.getvalue())

    def test_skips_when_pipeline_disabled(self):
        from common.public_data.pipeline_config import StaticConfigSource

        mocks = _patch_common()
        output = io.StringIO()
        with mocks["settings"], mocks["gate"], \
             patch("common.gateway.cli.resolve_service_id",
                   return_value="dingtalk-gateway"), \
             patch("common.gateway.cli.build_pipeline_config_source",
                   return_value=StaticConfigSource(
                       {"dingtalk-gateway": {"enabled": False}}
                   )), \
             mocks["conn"] as conn:
            with redirect_stdout(output):
                main([
                    "run", "--once", "--live-send",
                    "--confirm-local-test-write",
                    "--source-credentials", "/c.json",
                ])

        self.assertIn("status=skipped", output.getvalue())
        conn.assert_not_called()

    def test_failure_is_safe(self):
        mocks = _patch_common()
        mocks["creds"] = patch(
            "common.gateway.cli.load_source_credentials",
            side_effect=RuntimeError("secret-path"),
        )
        output = io.StringIO()
        with mocks["settings"], mocks["gate"], mocks["configs"], mocks["creds"]:
            with redirect_stdout(output):
                with self.assertRaises(SystemExit):
                    main([
                        "run", "--once", "--live-send",
                        "--confirm-local-test-write",
                        "--source-credentials", "/c.json",
                    ])

        text = output.getvalue()
        self.assertIn("status=failed", text)
        self.assertNotIn("secret-path", text)
        self.assertNotIn("Traceback", text)


class GatewayStreamModeTests(unittest.TestCase):
    """``run --with-stream``: one process, outbox thread + stream main loop."""

    def test_with_stream_requires_live_read(self):
        with patch("common.gateway.cli.load_settings") as load_settings:
            with self.assertRaises(SystemExit) as raised:
                main([
                    "run", "--live-send", "--confirm-local-test-write",
                    "--with-stream", "--source-credentials", "/c.json",
                ])
        self.assertNotEqual(0, raised.exception.code)
        load_settings.assert_not_called()

    def test_with_stream_starts_outbox_thread_and_stream_main_loop(self):
        mocks = _patch_common()
        output = io.StringIO()
        with mocks["settings"], mocks["gate"], mocks["configs"], \
             mocks["creds"], mocks["deliverer"], \
             mocks["conn"], mocks["worker"] as build_worker, \
             patch("common.gateway.cli.build_stream_handler") as build_handler, \
             patch("common.gateway.cli.build_card_callback_handler") as build_card_handler, \
             patch("common.gateway.cli.build_stream_client") as build_client, \
             patch("common.gateway.cli.threading.Thread") as thread_cls:
            with redirect_stdout(output):
                main([
                    "run", "--live-send", "--confirm-local-test-write",
                    "--with-stream", "--live-read",
                    "--source-credentials", "/c.json",
                ])

        build_handler.assert_called_once()
        build_client.assert_called_once_with(
            "k", "s", build_handler.return_value,
            card_handler=build_card_handler.return_value,
        )
        thread_cls.assert_called_once()
        self.assertEqual(
            thread_cls.call_args.kwargs["target"],
            build_worker.return_value.run_forever,
        )
        self.assertTrue(thread_cls.call_args.kwargs["daemon"])
        thread_cls.return_value.start.assert_called_once()
        build_client.return_value.start_forever.assert_called_once()
        build_worker.return_value.run_forever.assert_not_called()
        text = output.getvalue()
        self.assertIn("status=listening", text)
        self.assertIn("mode=outbox+stream", text)


class RequeueCliTests(unittest.TestCase):
    """``requeue`` 子命令：dry-run 默认只读，--execute 需显式确认。"""

    _FAILED_ROW = {
        "dedupe_key": "hangzhou:remind:2026-09-11",
        "region": "hangzhou",
        "kind": "remind",
        "business_date": "2026-09-11",
        "attempts": 5,
        "last_error": "group_send_failed",
        "created_at": "2026-09-11 10:00:00",
    }

    def _patch_requeue(self, rows=None):
        outbox = Mock()
        outbox.list_failed.return_value = (
            [dict(self._FAILED_ROW)] if rows is None else rows
        )
        return (
            patch("common.gateway.cli.load_settings", return_value=_settings()),
            patch("common.gateway.cli.connect_mart"),
            patch("common.gateway.cli.build_outbox", return_value=outbox),
            outbox,
        )

    def test_execute_requires_confirmation(self):
        with self.assertRaises(SystemExit) as raised:
            main(["requeue", "--all", "--execute"])
        self.assertNotEqual(0, raised.exception.code)

    def test_requires_an_explicit_selection(self):
        with self.assertRaises(SystemExit) as raised:
            main(["requeue"])
        self.assertNotEqual(0, raised.exception.code)

    def test_dry_run_lists_without_writing(self):
        settings, conn, build_outbox, outbox = self._patch_requeue()
        output = io.StringIO()
        with settings, conn as conn_mock, build_outbox:
            with redirect_stdout(output):
                main(["requeue", "--all"])

        text = output.getvalue()
        self.assertIn("status=dry-run", text)
        self.assertIn("matched=1", text)
        self.assertIn("dedupe_key=hangzhou:remind:2026-09-11", text)
        self.assertIn("attempts=5", text)
        self.assertIn("last_error=group_send_failed", text)
        outbox.requeue.assert_not_called()
        conn_mock.return_value.commit.assert_not_called()

    def test_dedupe_key_filter_narrows_the_selection(self):
        settings, conn, build_outbox, outbox = self._patch_requeue()
        output = io.StringIO()
        with settings, conn, build_outbox:
            with redirect_stdout(output):
                main(["requeue", "--dedupe-key", "other:check:2026-09-11"])

        self.assertIn("matched=0", output.getvalue())

    def test_execute_requeues_and_commits(self):
        settings, conn, build_outbox, outbox = self._patch_requeue()
        outbox.requeue.return_value = True
        output = io.StringIO()
        with settings, conn as conn_mock, build_outbox:
            with redirect_stdout(output):
                main([
                    "requeue", "--dedupe-key", "hangzhou:remind:2026-09-11",
                    "--execute", "--confirm-local-test-write",
                ])

        outbox.requeue.assert_called_once_with("hangzhou:remind:2026-09-11")
        conn_mock.return_value.commit.assert_called_once()
        text = output.getvalue()
        self.assertIn("status=completed", text)
        self.assertIn("requeued=1", text)

    def test_failure_is_safe(self):
        output = io.StringIO()
        with patch("common.gateway.cli.load_settings",
                   return_value=_settings()), \
             patch("common.gateway.cli.connect_mart",
                   side_effect=RuntimeError("secret-dsn")):
            with redirect_stdout(output):
                with self.assertRaises(SystemExit):
                    main(["requeue", "--all"])

        text = output.getvalue()
        self.assertIn("status=failed", text)
        self.assertNotIn("secret-dsn", text)
        self.assertNotIn("Traceback", text)


class PublishRegionsCliTests(unittest.TestCase):

    def test_publish_regions_prints_count(self):
        output = io.StringIO()
        with patch("common.gateway.cli.publish_regions", return_value=1) as pub:
            with redirect_stdout(output):
                main(["publish-regions", "--source", "/ops/regions.json"])

        pub.assert_called_once_with("/ops/regions.json", if_missing=False)
        self.assertIn("published=1 regions", output.getvalue())

    def test_publish_regions_failure_is_safe(self):
        output = io.StringIO()
        with patch(
            "common.gateway.cli.publish_regions",
            side_effect=RuntimeError("secret-server"),
        ):
            with redirect_stdout(output):
                with self.assertRaises(SystemExit):
                    main(["publish-regions", "--source", "/ops/regions.json"])

        text = output.getvalue()
        self.assertIn("status=failed", text)
        self.assertNotIn("secret-server", text)


if __name__ == "__main__":
    unittest.main()
