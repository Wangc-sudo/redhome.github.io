"""Tests for the mart-backed robot CLI (stage 4 robot container entry)."""

import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from common.daily_robot.mart_cli import main, route_by_hour
from common.daily_robot.mart_tasks import TaskOutcome
from common.region_config import RegionConfig


_CFG = RegionConfig(
    region="hangzhou",
    display="杭州",
    table_url="https://example.com/table",
    robot_code="rc",
    open_conversation_id="conv",
    aliases={"张三丰": "老张"},
    cc_user_ids=("cc-1",),
    remind_hour=18,
    check_hour=20,
    leaderboard_url="https://pages.example/lb",
)


def _settings():
    return Mock(region_seed_path=Path("/app/regions.seed.json"))


def _patch_common(**kwargs):
    """Patch every seam of the CLI; returns the patch mocks as a dict."""
    mocks = {
        "settings": patch(
            "common.daily_robot.mart_cli.load_settings",
            return_value=kwargs.get("settings", _settings()),
        ),
        "gate": patch("common.daily_robot.mart_cli.require_business_run"),
        "configs": patch(
            "common.daily_robot.mart_cli.load_region_configs",
            return_value=kwargs.get("configs", {"hangzhou": _CFG}),
        ),
        "conn": patch("common.daily_robot.mart_cli.connect_mart"),
        "outbox": patch("common.daily_robot.mart_cli.build_outbox"),
        "remind": patch(
            "common.daily_robot.mart_cli.run_remind_task",
            return_value=kwargs.get(
                "remind_outcome",
                TaskOutcome("enqueued", "remind", date(2026, 9, 11),
                            unfilled=("李四",), enqueued=True),
            ),
        ),
        "check": patch(
            "common.daily_robot.mart_cli.run_check_task",
            return_value=TaskOutcome("enqueued", "check", date(2026, 9, 11),
                                     unfilled=("李四",), enqueued=True),
        ),
    }
    return mocks


class RouteByHourTests(unittest.TestCase):

    def test_remind_hour_routes_to_remind(self):
        self.assertEqual(route_by_hour(18, _CFG), ("remind",))

    def test_check_hour_routes_to_check(self):
        self.assertEqual(route_by_hour(20, _CFG), ("check",))

    def test_other_hours_have_no_task(self):
        self.assertEqual(route_by_hour(12, _CFG), ())


class MartCliTests(unittest.TestCase):

    def test_requires_confirmation_before_any_work(self):
        with patch("common.daily_robot.mart_cli.load_settings") as load_settings:
            with self.assertRaises(SystemExit) as raised:
                main(["remind"])
        self.assertNotEqual(0, raised.exception.code)
        load_settings.assert_not_called()

    def test_region_is_required(self):
        mocks = _patch_common()
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), \
             mocks["settings"], mocks["gate"], mocks["configs"]:
            with redirect_stdout(output):
                with self.assertRaises(SystemExit) as raised:
                    main(["remind", "--confirm-local-test-write"])

        self.assertNotEqual(0, raised.exception.code)
        self.assertIn("code=region_required", output.getvalue())

    def test_remind_runs_and_prints_a_safe_outcome(self):
        mocks = _patch_common()
        output = io.StringIO()
        with mocks["settings"], mocks["gate"], mocks["configs"], \
             mocks["conn"] as conn, mocks["outbox"], mocks["remind"] as remind:
            with redirect_stdout(output):
                main([
                    "remind", "--confirm-local-test-write",
                    "--region", "hangzhou",
                ])

        text = output.getvalue()
        self.assertIn("region=hangzhou", text)
        self.assertIn("kind=remind", text)
        self.assertIn("status=enqueued", text)
        self.assertIn("unfilled=1", text)
        conn.return_value.commit.assert_called_once()
        kwargs = remind.call_args.kwargs
        self.assertEqual(kwargs["region"], "hangzhou")
        self.assertEqual(kwargs["display"], "杭州")
        self.assertEqual(kwargs["aliases"], {"张三丰": "老张"})
        self.assertEqual(kwargs["business_date"], date.today())

    def test_check_passes_cc_and_aliases(self):
        mocks = _patch_common()
        with mocks["settings"], mocks["gate"], mocks["configs"], \
             mocks["conn"], mocks["outbox"], mocks["check"] as check:
            main([
                "check", "--confirm-local-test-write", "--region", "hangzhou",
            ])

        kwargs = check.call_args.kwargs
        self.assertEqual(kwargs["cc_user_ids"], ("cc-1",))
        self.assertEqual(kwargs["aliases"], {"张三丰": "老张"})

    def test_once_routes_by_the_current_hour(self):
        mocks = _patch_common()
        output = io.StringIO()
        with mocks["settings"], mocks["gate"], mocks["configs"], \
             mocks["conn"], mocks["outbox"], \
             mocks["remind"] as remind, mocks["check"] as check, \
             patch("common.daily_robot.mart_cli.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 9, 11, 18, 30)
            mock_dt.fromisoformat = date.fromisoformat
            with redirect_stdout(output):
                main([
                    "once", "--confirm-local-test-write", "--region", "hangzhou",
                ])

        remind.assert_called_once()
        check.assert_not_called()
        self.assertIn("kind=remind", output.getvalue())

    def test_once_off_hours_runs_nothing(self):
        mocks = _patch_common()
        output = io.StringIO()
        with mocks["settings"], mocks["gate"], mocks["configs"], \
             mocks["remind"] as remind, \
             patch("common.daily_robot.mart_cli.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 9, 11, 12, 0)
            with redirect_stdout(output):
                main([
                    "once", "--confirm-local-test-write", "--region", "hangzhou",
                ])

        remind.assert_not_called()
        self.assertIn("status=no_task", output.getvalue())

    def test_date_override(self):
        mocks = _patch_common()
        with mocks["settings"], mocks["gate"], mocks["configs"], \
             mocks["conn"], mocks["outbox"], mocks["remind"] as remind:
            main([
                "remind", "--confirm-local-test-write", "--region", "hangzhou",
                "--date", "2026-09-10",
            ])

        self.assertEqual(
            remind.call_args.kwargs["business_date"], date(2026, 9, 10)
        )

    def test_skips_when_pipeline_disabled(self):
        from common.public_data.pipeline_config import StaticConfigSource

        mocks = _patch_common()
        output = io.StringIO()
        with mocks["settings"], mocks["gate"], \
             patch("common.daily_robot.mart_cli.resolve_service_id",
                   return_value="robot-hangzhou"), \
             patch("common.daily_robot.mart_cli.build_pipeline_config_source",
                   return_value=StaticConfigSource(
                       {"robot-hangzhou": {"enabled": False}}
                   )), \
             mocks["conn"] as conn:
            with redirect_stdout(output):
                main([
                    "once", "--confirm-local-test-write", "--region", "hangzhou",
                ])

        self.assertIn("status=skipped", output.getvalue())
        conn.assert_not_called()

    def test_failure_is_safe(self):
        mocks = _patch_common()
        mocks["remind"] = patch(
            "common.daily_robot.mart_cli.run_remind_task",
            side_effect=RuntimeError("secret-host"),
        )
        output = io.StringIO()
        with mocks["settings"], mocks["gate"], mocks["configs"], \
             mocks["conn"], mocks["outbox"], mocks["remind"]:
            with redirect_stdout(output):
                with self.assertRaises(SystemExit):
                    main([
                        "remind", "--confirm-local-test-write",
                        "--region", "hangzhou",
                    ])

        text = output.getvalue()
        self.assertIn("status=failed", text)
        self.assertNotIn("secret-host", text)
        self.assertNotIn("Traceback", text)


class LeaderboardCliTests(unittest.TestCase):
    """The leaderboard broadcast subcommand."""

    def test_leaderboard_enqueues_the_broadcast(self):
        from common.daily_robot.mart_leaderboard import LeaderboardData

        data = LeaderboardData(
            business_date=date(2026, 9, 11),
            elapsed=(1, 2, 3),
            people=({"name": "李四", "dept": "滨萧", "target": 1000,
                     "completed": 500.0, "unfilled": 8, "rate": 0.5},),
            workdays=frozenset({date(2026, 9, 10)}),
        )
        output = io.StringIO()
        with patch("common.daily_robot.mart_cli.load_settings",
                   return_value=_settings()), \
             patch("common.daily_robot.mart_cli.require_business_run"), \
             patch("common.daily_robot.mart_cli.load_region_configs",
                   return_value={"hangzhou": _CFG}), \
             patch("common.daily_robot.mart_cli.connect_mart") as conn, \
             patch("common.daily_robot.mart_cli.mart_collect_data",
                   return_value=data), \
             patch("common.daily_robot.mart_cli.build_view",
                   return_value={"region": {}, "calendar": {}}), \
             patch("common.daily_robot.mart_cli.render_bc",
                   return_value="MD") as render, \
             patch("common.daily_robot.mart_cli.build_outbox") as build_outbox, \
             patch("common.daily_robot.mart_cli.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 9, 11, 8, 30)
            mock_dt.fromisoformat = date.fromisoformat
            build_outbox.return_value.enqueue.return_value = True
            with redirect_stdout(output):
                main([
                    "leaderboard", "--confirm-local-test-write",
                    "--region", "hangzhou",
                ])

        kwargs = build_outbox.return_value.enqueue.call_args.kwargs
        self.assertEqual(kwargs["kind"], "leaderboard")
        self.assertEqual(kwargs["region"], "hangzhou")
        self.assertEqual(kwargs["title"], "销售完成率榜")
        self.assertEqual(kwargs["body_md"], "MD")
        self.assertEqual(kwargs["dedupe_suffix"], "0830")
        self.assertEqual(kwargs["business_date"], date(2026, 9, 11))
        conn.return_value.commit.assert_called_once()
        self.assertIn("kind=leaderboard", output.getvalue())
        self.assertIn("status=enqueued", output.getvalue())
        self.assertIn("people=1", output.getvalue())

        # 榜单链接来自 region 配置的 leaderboardUrl。
        self.assertEqual(
            render.call_args.kwargs["url"], _CFG.leaderboard_url
        )

    def test_leaderboard_requires_confirmation(self):
        with patch("common.daily_robot.mart_cli.load_settings") as load_settings:
            with self.assertRaises(SystemExit) as raised:
                main(["leaderboard"])
        self.assertNotEqual(0, raised.exception.code)
        load_settings.assert_not_called()


class LeaderboardHtmlCliTests(unittest.TestCase):
    """The leaderboard-html page subcommand."""

    def test_writes_the_page_and_prints_a_safe_summary(self):
        import tempfile

        from common.daily_robot.mart_leaderboard import LeaderboardData

        data = LeaderboardData(
            business_date=date(2026, 9, 11),
            elapsed=(1, 2, 3),
            people=({"name": "李四", "dept": "滨萧", "target": 1000,
                     "completed": 500.0, "unfilled": 8, "rate": 0.5},),
            workdays=frozenset({date(2026, 9, 10)}),
        )
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
             patch("common.daily_robot.mart_cli.load_settings",
                   return_value=_settings()), \
             patch("common.daily_robot.mart_cli.require_business_run"), \
             patch("common.daily_robot.mart_cli.load_region_configs",
                   return_value={"hangzhou": _CFG}), \
             patch("common.daily_robot.mart_cli.connect_mart"), \
             patch("common.daily_robot.mart_cli.mart_collect_data",
                   return_value=data), \
             patch("common.daily_robot.mart_cli.build_view",
                   return_value={"region": {}, "calendar": {}}), \
             patch("common.daily_robot.mart_cli.build_html_page",
                   return_value="<html>完成率榜单</html>") as build_page, \
             patch("common.daily_robot.mart_cli.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 9, 11, 8, 30)
            mock_dt.fromisoformat = date.fromisoformat
            target = Path(tmp) / "nested" / "hangzhou.html"
            with redirect_stdout(output):
                main([
                    "leaderboard-html", "--confirm-local-test-write",
                    "--region", "hangzhou", "--output", str(target),
                ])

            page = target.read_text(encoding="utf-8")

        self.assertEqual(page, "<html>完成率榜单</html>")
        build_page.assert_called_once()
        text = output.getvalue()
        self.assertIn("kind=leaderboard-html", text)
        self.assertIn("status=written", text)
        self.assertIn("people=1", text)

    def test_leaderboard_html_requires_confirmation(self):
        with patch("common.daily_robot.mart_cli.load_settings") as load_settings:
            with self.assertRaises(SystemExit) as raised:
                main(["leaderboard-html", "--output", "/tmp/x.html"])
        self.assertNotEqual(0, raised.exception.code)
        load_settings.assert_not_called()


if __name__ == "__main__":
    unittest.main()
