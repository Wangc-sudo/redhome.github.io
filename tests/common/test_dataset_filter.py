"""``--dataset`` 过滤测试：live-sync / extract-mart / CLI 透传 / mart_cli yesterday。"""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from common.daily_robot.mart_cli import _resolve_business_date
from common.public_data.cli import main as cli_main
from common.public_data.dataset_filter import matches_any
from common.public_data.extract_mart import MartExtractError, MartExtractService
from common.public_data.live_sync import LiveSyncError, LiveSyncService
from common.public_data.manifest import (
    DingTalkSheet,
    FieldMapping,
    OrgDataset,
    SourceManifest,
    WdtDataset,
)
from common.public_data.mart_extract_schema import dataset_by_name


_NOW = datetime(2026, 9, 28, 2, 31, tzinfo=timezone.utc)
_RUN_ID = "00000000-0000-0000-0000-0000000000f1"


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeConnection:
    def __init__(self):
        self.commits = 0

    def cursor(self):
        return Mock()

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _dt_sheet(dataset):
    return DingTalkSheet(
        base_id="base_abc",
        sheet_id="sheet_xyz",
        sheet_name="Test Sheet",
        dataset=dataset,
        target_table=f"raw_{dataset}",
        max_pages=5,
        fields=(
            FieldMapping(source_name="field_a", column="col_a",
                         source_type="text"),
        ),
    )


def _wdt_dataset(dataset):
    return WdtDataset(
        dataset=dataset,
        method="sales.TradeQuery.queryWithDetail",
        target_table="wdt_records",
        record_id_path="order.trade_no",
        page_size=100,
        max_pages=10,
        window_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        window_end=datetime(2026, 9, 1, 1, tzinfo=timezone.utc),
        max_window_minutes=60,
        params={},
    )


def _manifest(sheets=(), datasets=(), org=None):
    return SourceManifest(
        dingtalk_sheets=tuple(sheets),
        wdt_datasets=tuple(datasets),
        sha256="a" * 64,
        dingtalk_org=org,
    )


# ---------------------------------------------------------------------------
# matches_any
# ---------------------------------------------------------------------------

class MatchesAnyTests(unittest.TestCase):

    def test_exact_match(self):
        self.assertTrue(matches_any("channel_daily_sales",
                                    ("channel_daily_sales",)))
        self.assertFalse(matches_any("channel_daily_sales_ext",
                                     ("channel_daily_sales",)))

    def test_prefix_wildcard(self):
        patterns = ("channel_daily_sales*",)
        self.assertTrue(matches_any("channel_daily_sales", patterns))
        self.assertTrue(matches_any("channel_daily_sales_ext", patterns))
        self.assertFalse(matches_any("channel_monthly_target", patterns))

    def test_star_alone_matches_everything(self):
        self.assertTrue(matches_any("anything", ("*",)))

    def test_any_of_multiple_patterns(self):
        patterns = ("channel_monthly_target", "channel_daily_sales*")
        self.assertTrue(matches_any("channel_monthly_target", patterns))
        self.assertTrue(matches_any("channel_daily_sales_a", patterns))
        self.assertFalse(matches_any("daily_report_offline", patterns))


# ---------------------------------------------------------------------------
# live-sync datasets 过滤
# ---------------------------------------------------------------------------

class LiveSyncDatasetFilterTests(unittest.TestCase):

    def _service(self, *, wdt_gateway=None, org_gateway=None):
        self.dingtalk_gateway = Mock()
        self.dingtalk_gateway.read_records.return_value = [
            {"id": "r1", "fields": {"field_a": "v1"}},
        ]
        self.wdt_gateway = (
            wdt_gateway if wdt_gateway is not None else Mock()
        )
        self.dingtalk_repo = Mock()
        self.wdt_repo = Mock()
        self.mart_repo = Mock()
        self.connections = Mock()
        self.connections.dingtalk = Mock(name="dingtalk_conn")
        self.connections.wdt = Mock(name="wdt_conn")
        self.connections.mart = Mock(name="mart_conn")
        return LiveSyncService(
            dingtalk_gateway=self.dingtalk_gateway,
            wdt_gateway=self.wdt_gateway,
            dingtalk_repository=self.dingtalk_repo,
            wdt_repository=self.wdt_repo,
            mart_repository=self.mart_repo,
            connections=self.connections,
            now=lambda: _NOW,
            new_run_id=lambda: _RUN_ID,
            org_gateway=org_gateway,
        )

    @patch("common.public_data.live_sync.transaction", return_value=_Ctx())
    @patch("common.public_data.live_sync.named_lock", return_value=_Ctx())
    def test_exact_match_selects_only_that_sheet(self, _lock, _txn):
        svc = self._service()
        manifest = _manifest(sheets=[
            _dt_sheet("channel_daily_sales"),
            _dt_sheet("daily_report_offline"),
        ])
        result = svc.sync(manifest, datasets=["channel_daily_sales"])

        self.assertEqual(
            [d["dataset"] for d in result.datasets], ["channel_daily_sales"]
        )
        self.assertEqual(self.dingtalk_gateway.read_records.call_count, 1)

    @patch("common.public_data.live_sync.transaction", return_value=_Ctx())
    @patch("common.public_data.live_sync.named_lock", return_value=_Ctx())
    def test_prefix_wildcard_matches_multiple(self, _lock, _txn):
        svc = self._service()
        manifest = _manifest(sheets=[
            _dt_sheet("channel_daily_sales"),
            _dt_sheet("channel_daily_sales_ext"),
            _dt_sheet("daily_report_offline"),
        ])
        result = svc.sync(manifest, datasets=["channel_daily_sales*"])

        self.assertEqual(
            sorted(d["dataset"] for d in result.datasets),
            ["channel_daily_sales", "channel_daily_sales_ext"],
        )

    def test_no_match_raises_before_starting_run(self):
        svc = self._service()
        manifest = _manifest(sheets=[_dt_sheet("channel_daily_sales")])
        with self.assertRaisesRegex(LiveSyncError, "no datasets matched"):
            svc.sync(manifest, datasets=["nope"])
        self.mart_repo.start_run.assert_not_called()

    @patch("common.public_data.live_sync.transaction", return_value=_Ctx())
    @patch("common.public_data.live_sync.named_lock", return_value=_Ctx())
    def test_none_keeps_legacy_both_lines(self, _lock, _txn):
        svc = self._service()
        self.wdt_gateway.read_dataset.return_value = [({"t": "T1"}, "T1")]
        manifest = _manifest(
            sheets=[_dt_sheet("channel_daily_sales")],
            datasets=[_wdt_dataset("wdt_sales")],
        )
        result = svc.sync(manifest, datasets=None)
        self.assertEqual(
            sorted(d["source"] for d in result.datasets),
            ["dingtalk", "wdt"],
        )

    @patch("common.public_data.live_sync.transaction", return_value=_Ctx())
    @patch("common.public_data.live_sync.named_lock", return_value=_Ctx())
    def test_filter_selecting_only_dingtalk_needs_no_wdt_gateway(
        self, _lock, _txn
    ):
        svc = self._service()
        svc._wdt_gateway = None  # 过滤后未选中 wdt 线，不应要求 gateway
        manifest = _manifest(
            sheets=[_dt_sheet("channel_daily_sales")],
            datasets=[_wdt_dataset("wdt_sales")],
        )
        result = svc.sync(manifest, datasets=["channel_daily_sales"])
        self.assertEqual(len(result.datasets), 1)

    def test_filter_selecting_wdt_without_gateway_raises(self):
        svc = self._service()
        svc._wdt_gateway = None
        manifest = _manifest(datasets=[_wdt_dataset("wdt_sales")])
        with self.assertRaisesRegex(LiveSyncError, "wdt gateway"):
            svc.sync(manifest, source="wdt", datasets=["wdt_*"])

    @patch("common.public_data.live_sync.transaction", return_value=_Ctx())
    @patch("common.public_data.live_sync.named_lock", return_value=_Ctx())
    def test_org_dataset_filtered_out(self, _lock, _txn):
        svc = self._service()  # org_gateway=None：若 org 被选中会抛错
        manifest = _manifest(
            sheets=[_dt_sheet("channel_daily_sales")],
            org=OrgDataset(
                dataset="org_directory", target_table="dingtalk_org_member"
            ),
        )
        result = svc.sync(manifest, datasets=["channel_daily_sales"])
        self.assertEqual(
            [d["dataset"] for d in result.datasets], ["channel_daily_sales"]
        )


# ---------------------------------------------------------------------------
# extract-mart datasets 过滤
# ---------------------------------------------------------------------------

class ExtractDatasetFilterTests(unittest.TestCase):

    def _service(self, *, datasets=None, calendar_months=()):
        self.repository = Mock()
        self.repository.read_dataset.return_value = [
            {"source_record_id": "r1", "region": "hangzhou"},
        ]
        self.repository.read_org_members.return_value = []
        self.repository.read_ecom_source.return_value = ([], [])
        self.mart_repository = Mock()
        self.mart_connection = _FakeConnection()
        kwargs = {} if datasets is None else {"datasets": datasets}
        return MartExtractService(
            repository=self.repository,
            mart_repository=self.mart_repository,
            mart_connection=self.mart_connection,
            now=lambda: _NOW,
            new_run_id=lambda: _RUN_ID,
            calendar_months=calendar_months,
            **kwargs,
        )

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_exact_dataset_only(self, _lock, _txn):
        service = self._service()
        result = service.extract(datasets=["channel_daily_sales"])

        self.assertEqual(self.repository.read_dataset.call_count, 1)
        called = self.repository.read_dataset.call_args.args[0]
        self.assertEqual(called.dataset, "channel_daily_sales")
        self.repository.read_org_members.assert_not_called()
        self.repository.read_ecom_source.assert_not_called()
        self.mart_repository.mark_completed.assert_called_once()
        self.assertEqual(len(result.datasets), 1)

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_channel_monthly_target_alias_runs_ecom_step(self, _lock, _txn):
        service = self._service()
        service.extract(datasets=["channel_monthly_target"])

        self.repository.read_ecom_source.assert_called_once()
        self.repository.read_dataset.assert_not_called()
        self.repository.read_org_members.assert_not_called()

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_combined_channel_filter(self, _lock, _txn):
        service = self._service()
        service.extract(
            datasets=["channel_daily_sales", "channel_monthly_target"]
        )
        self.assertEqual(self.repository.read_dataset.call_count, 1)
        self.repository.read_ecom_source.assert_called_once()
        self.repository.read_org_members.assert_not_called()

    def test_no_match_raises_before_starting_run(self):
        service = self._service()
        with self.assertRaisesRegex(MartExtractError, "no extract datasets"):
            service.extract(datasets=["nope"])
        self.mart_repository.start_run.assert_not_called()

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_none_keeps_legacy_full_plan(self, _lock, _txn):
        service = self._service(
            datasets=[dataset_by_name("channel_daily_sales")]
        )
        service.extract()
        self.repository.read_dataset.assert_called_once()
        self.repository.read_org_members.assert_called_once()
        self.repository.read_ecom_source.assert_called_once()


# ---------------------------------------------------------------------------
# CLI 透传
# ---------------------------------------------------------------------------

class CliDatasetFlagTests(unittest.TestCase):

    def test_live_sync_passes_datasets_through(self):
        with patch("common.public_data.cli.load_settings"), \
             patch("common.public_data.cli.require_live_run"), \
             patch("common.public_data.cli.apply_ipv4_baseline"), \
             patch("common.public_data.cli.load_manifest"), \
             patch("common.public_data.cli.load_source_credentials"), \
             patch("common.public_data.cli.build_service") as build_service:
            build_service.return_value.sync.return_value = {
                "run_id": "r", "datasets": [],
            }
            cli_main([
                "live-sync", "--live-read", "--confirm-local-test-write",
                "--source-credentials", "/creds.json",
                "--source", "dingtalk",
                "--dataset", "channel_daily_sales*",
                "--dataset", "channel_monthly_target",
            ])
        kwargs = build_service.return_value.sync.call_args.kwargs
        self.assertEqual(kwargs.get("source"), "dingtalk")
        self.assertEqual(
            kwargs.get("datasets"),
            ["channel_daily_sales*", "channel_monthly_target"],
        )

    def test_live_sync_default_datasets_is_none(self):
        with patch("common.public_data.cli.load_settings"), \
             patch("common.public_data.cli.require_live_run"), \
             patch("common.public_data.cli.apply_ipv4_baseline"), \
             patch("common.public_data.cli.load_manifest"), \
             patch("common.public_data.cli.load_source_credentials"), \
             patch("common.public_data.cli.build_service") as build_service:
            build_service.return_value.sync.return_value = {
                "run_id": "r", "datasets": [],
            }
            cli_main([
                "live-sync", "--live-read", "--confirm-local-test-write",
                "--source-credentials", "/creds.json",
            ])
        kwargs = build_service.return_value.sync.call_args.kwargs
        self.assertIsNone(kwargs.get("datasets"))

    def test_extract_mart_passes_datasets_through(self):
        with patch("common.public_data.cli.load_settings"), \
             patch("common.public_data.cli.require_extract_run"), \
             patch("common.public_data.cli.build_extract_service") as build:
            build.return_value.extract.return_value = {
                "run_id": "r", "datasets": [],
            }
            cli_main([
                "extract-mart", "--confirm-local-test-write",
                "--dataset", "channel_daily_sales",
                "--dataset", "channel_monthly_target",
            ])
        kwargs = build.return_value.extract.call_args.kwargs
        self.assertEqual(
            kwargs.get("datasets"),
            ["channel_daily_sales", "channel_monthly_target"],
        )


# ---------------------------------------------------------------------------
# mart_cli --date yesterday
# ---------------------------------------------------------------------------

class ResolveBusinessDateTests(unittest.TestCase):

    def test_yesterday_minus_one_day(self):
        args = SimpleNamespace(date="yesterday")
        now = datetime(2026, 9, 28, 10, 45)
        self.assertEqual(
            _resolve_business_date(args, now).isoformat(), "2026-09-27"
        )

    def test_yesterday_case_insensitive(self):
        args = SimpleNamespace(date="Yesterday")
        now = datetime(2026, 3, 1, 8, 30)
        self.assertEqual(
            _resolve_business_date(args, now).isoformat(), "2026-02-28"
        )

    def test_explicit_date_unchanged(self):
        args = SimpleNamespace(date="2026-09-20")
        now = datetime(2026, 9, 28, 10, 45)
        self.assertEqual(
            _resolve_business_date(args, now).isoformat(), "2026-09-20"
        )

    def test_default_is_today(self):
        args = SimpleNamespace(date=None)
        now = datetime(2026, 9, 28, 10, 45)
        self.assertEqual(
            _resolve_business_date(args, now).isoformat(), "2026-09-28"
        )


if __name__ == "__main__":
    unittest.main()
