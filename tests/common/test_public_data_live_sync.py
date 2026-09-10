"""Tests for the LiveSyncService orchestration layer."""

import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, call, patch

from common.public_data.live_sync import LiveSyncService
from common.public_data.manifest import (
    DingTalkSheet,
    FieldMapping,
    SourceManifest,
    WdtDataset,
)


def _dt_sheet(*, dataset="test_dataset"):
    return DingTalkSheet(
        base_id="base_abc",
        sheet_id="sheet_xyz",
        sheet_name="Test Sheet",
        dataset=dataset,
        target_table="fin_store_commission",
        max_pages=5,
        fields=(
            FieldMapping(
                source_name="field_a",
                column="col_a",
                source_type="text",
            ),
        ),
    )


def _wdt_dataset(*, dataset="wdt_sales"):
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


def _manifest(sheets=(), datasets=()):
    return SourceManifest(
        dingtalk_sheets=tuple(sheets),
        wdt_datasets=tuple(datasets),
        sha256="a" * 64,
    )


_NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
_RUN_ID = "00000000-0000-0000-0000-000000000001"


class LiveSyncServiceTests(unittest.TestCase):

    def _service(self):
        self.dingtalk_gateway = Mock()
        self.wdt_gateway = Mock()
        self.dingtalk_repo = Mock()
        self.wdt_repo = Mock()
        self.mart_repo = Mock()

        self.dingtalk_conn = Mock(name="dingtalk_conn")
        self.wdt_conn = Mock(name="wdt_conn")

        self.connections = Mock()
        self.connections.dingtalk = self.dingtalk_conn
        self.connections.wdt = self.wdt_conn

        return LiveSyncService(
            dingtalk_gateway=self.dingtalk_gateway,
            wdt_gateway=self.wdt_gateway,
            dingtalk_repository=self.dingtalk_repo,
            wdt_repository=self.wdt_repo,
            mart_repository=self.mart_repo,
            connections=self.connections,
            now=lambda: _NOW,
            new_run_id=lambda: _RUN_ID,
        )

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    @patch("common.public_data.live_sync.transaction")
    @patch("common.public_data.live_sync.named_lock")
    def test_successful_sync_order(self, mock_lock, mock_txn):
        """A full sync follows the prescribed operation order."""
        mock_lock.side_effect = [
            _ctx(),  # DingTalk lock
            _ctx(),  # WDT lock
        ]
        mock_txn.side_effect = [
            _ctx(),  # DingTalk transaction
            _ctx(),  # WDT transaction
        ]

        sheet = _dt_sheet()
        wdt_ds = _wdt_dataset()
        manifest = _manifest(sheets=[sheet], datasets=[wdt_ds])

        svc = self._service()

        self.dingtalk_gateway.read_records.return_value = [
            {"id": "r1", "fields": {"field_a": "v1"}},
        ]
        self.wdt_gateway.read_dataset.return_value = [
            ({"trade_no": "T1"}, "T1"),
        ]

        # Inject an event logger into every call that matters for ordering.
        events = []
        self.mart_repo.start_run.side_effect = _log(events, "start_run")
        self.dingtalk_gateway.validate_sheet.side_effect = _log(
            events, "validate"
        )
        mock_lock.side_effect = _log_with_ctx(events, "lock")
        self.dingtalk_gateway.read_records.side_effect = _log_return(
            events, "read_dingtalk", [{"id": "r1", "fields": {"field_a": "v1"}}]
        )
        mock_txn.side_effect = _log_with_ctx(events, "txn")
        self.dingtalk_repo.upsert_records.side_effect = _log(
            events, "upsert_dingtalk"
        )
        self.mart_repo.mark_raw_committed.side_effect = _log(
            events, "raw_committed"
        )
        self.mart_repo.save_dataset_summary.side_effect = _log(
            events, "summary"
        )
        self.wdt_gateway.read_dataset.side_effect = _log_return(
            events, "read_wdt", [({"trade_no": "T1"}, "T1")]
        )
        self.wdt_repo.upsert_records.side_effect = _log(
            events, "upsert_wdt"
        )
        self.mart_repo.mark_completed.side_effect = _log(
            events, "completed"
        )

        result = svc.sync(manifest)

        self.assertEqual(events, [
            "start_run",
            "validate",
            "lock",
            "read_dingtalk",
            "txn",
            "upsert_dingtalk",
            "raw_committed",
            "summary",
            "lock",
            "read_wdt",
            "txn",
            "upsert_wdt",
            "raw_committed",
            "summary",
            "completed",
        ])

        # Verify key call arguments
        self.mart_repo.start_run.assert_called_once_with(
            sync_run_id=_RUN_ID,
            manifest_sha256="a" * 64,
            started_at=_NOW,
        )
        self.dingtalk_gateway.validate_sheet.assert_called_once_with(sheet)
        self.assertEqual(mock_lock.call_args_list, [
            call(self.dingtalk_conn, "public-data:dingtalk:test_dataset"),
            call(self.wdt_conn, "public-data:wdt:wdt_sales"),
        ])
        self.mart_repo.mark_completed.assert_called_once_with(
            sync_run_id=_RUN_ID,
            finished_at=_NOW,
        )
        self.assertEqual(result.run_id, _RUN_ID)
        self.assertEqual(len(result.datasets), 2)

    # ------------------------------------------------------------------
    # Failure paths
    # ------------------------------------------------------------------

    @patch("common.public_data.live_sync.transaction")
    @patch("common.public_data.live_sync.named_lock")
    def test_source_read_failure_marks_failed(self, mock_lock, mock_txn):
        """If the gateway raises during read, mark_failed is called."""
        mock_lock.side_effect = [_ctx()]
        mock_txn.side_effect = []

        sheet = _dt_sheet()
        manifest = _manifest(sheets=[sheet])

        svc = self._service()
        self.dingtalk_gateway.read_records.side_effect = RuntimeError(
            "connection lost"
        )

        with self.assertRaises(RuntimeError):
            svc.sync(manifest)

        self.mart_repo.mark_failed.assert_called_once_with(
            sync_run_id=_RUN_ID,
            failure_code="source_read_failed",
            finished_at=_NOW,
        )
        self.dingtalk_repo.upsert_records.assert_not_called()
        self.mart_repo.mark_completed.assert_not_called()

    @patch("common.public_data.live_sync.transaction")
    @patch("common.public_data.live_sync.named_lock")
    def test_projection_failure_marks_pending(self, mock_lock, mock_txn):
        """If raw upsert succeeds but mart summary fails, mark_projection_pending."""
        mock_lock.side_effect = [_ctx()]
        mock_txn.side_effect = [_ctx()]

        sheet = _dt_sheet()
        manifest = _manifest(sheets=[sheet])

        svc = self._service()
        self.dingtalk_gateway.read_records.return_value = [
            {"id": "r1", "fields": {}},
        ]
        self.mart_repo.save_dataset_summary.side_effect = RuntimeError(
            "db timeout"
        )

        with self.assertRaises(RuntimeError):
            svc.sync(manifest)

        self.mart_repo.mark_projection_pending.assert_called_once_with(
            sync_run_id=_RUN_ID,
            failure_code="projection_failed",
            finished_at=_NOW,
        )
        self.mart_repo.mark_raw_committed.assert_called_once_with(_RUN_ID)
        self.mart_repo.mark_failed.assert_not_called()
        self.mart_repo.mark_completed.assert_not_called()

    # ------------------------------------------------------------------
    # Projection recovery
    # ------------------------------------------------------------------

    def test_rebuild_projection_from_raw(self):
        """rebuild_projection queries raw repos and completes the run."""
        sheet = _dt_sheet()
        wdt_ds = _wdt_dataset()
        manifest = _manifest(sheets=[sheet], datasets=[wdt_ds])

        svc = self._service()
        self.dingtalk_repo.summary_for_run.return_value = [
            {"source_record_id": "r1"},
            {"source_record_id": "r2"},
        ]
        self.wdt_repo.summary_for_run.return_value = [
            {"source_record_id": "T1"},
        ]

        result = svc.rebuild_projection(_RUN_ID, manifest)

        # Gateways must NOT be invoked during recovery.
        self.dingtalk_gateway.read_records.assert_not_called()
        self.dingtalk_gateway.validate_sheet.assert_not_called()
        self.wdt_gateway.read_dataset.assert_not_called()

        # Raw repos were queried.
        self.dingtalk_repo.summary_for_run.assert_called_once_with(_RUN_ID)
        self.wdt_repo.summary_for_run.assert_called_once_with(_RUN_ID)

        # Two summaries saved (one per dataset).
        self.assertEqual(self.mart_repo.save_dataset_summary.call_count, 2)
        self.mart_repo.save_dataset_summary.assert_any_call(
            sync_run_id=_RUN_ID,
            source_name="dingtalk",
            dataset_name="test_dataset",
            records_read=2,
            raw_records_written=2,
            record_id_digest=unittest.mock.ANY,
            completed_at=_NOW,
        )
        self.mart_repo.save_dataset_summary.assert_any_call(
            sync_run_id=_RUN_ID,
            source_name="wdt",
            dataset_name="wdt_sales",
            records_read=1,
            raw_records_written=1,
            record_id_digest=unittest.mock.ANY,
            completed_at=_NOW,
        )

        # Run marked completed.
        self.mart_repo.mark_completed.assert_called_once_with(
            sync_run_id=_RUN_ID,
            finished_at=_NOW,
        )

        self.assertEqual(result.run_id, _RUN_ID)
        self.assertEqual(len(result.datasets), 2)


# -----------------------------------------------------------------------
# Helpers for building an ordered event log
# -----------------------------------------------------------------------

def _ctx():
    """Return a context-manager stub suitable for ``with`` statements."""
    from contextlib import contextmanager

    @contextmanager
    def _nop():
        yield

    return _nop()


def _log(events, tag):
    """side_effect callback that appends *tag* to *events*."""
    def _inner(*_args, **_kwargs):
        events.append(tag)
    return _inner


def _log_return(events, tag, value):
    """side_effect callback that appends *tag* and returns *value*."""
    def _inner(*_args, **_kwargs):
        events.append(tag)
        return value
    return _inner


def _log_with_ctx(events, tag):
    """side_effect callback that appends *tag* and returns a no-op context manager."""
    def _inner(*_args, **_kwargs):
        events.append(tag)
        return _ctx()
    return _inner


if __name__ == "__main__":
    unittest.main()
