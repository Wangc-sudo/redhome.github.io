import unittest
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import Mock, patch

from common.public_data.extract_finance import (
    BALANCE_MONTHS, parse_statement_date, project_snapshot, project_store_funds_melt,
)
from common.public_data.extract_mart import MartExtractError
from common.public_data.mart_extract_schema import dataset_by_name
from tests.common import test_public_data_extract_mart as helpers

_NOW, _RUN_ID, _Ctx = helpers._NOW, helpers._RUN_ID, helpers._Ctx


class FinanceProjectionTests(unittest.TestCase):
    def test_dates(self):
        for value, expected in (
            ("2026-09-01", date(2026, 9, 1)), (" 2026/09/01 ", date(2026, 9, 1)),
            (datetime(2026, 9, 1, 12), date(2026, 9, 1)),
            (date(2026, 9, 1), date(2026, 9, 1)),
            ("2026.9.1", None), ("2026-02-30", None), ("", None), (None, None),
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_statement_date(value), expected)

    def test_snapshot_preserves_raw_date_and_does_not_mutate_input(self):
        repo = Mock()
        ds = dataset_by_name("fin_ecommerce_prepayment_supplier_invoice")
        raw = [
            {"source_record_id": "1", "statement_date_raw": "2026/09/01"},
            {"source_record_id": "2", "statement_date_raw": "bad date"},
            {"source_record_id": "3", "statement_date_raw": None},
        ]
        repo.read_dataset.return_value = raw
        repo.replace_table.return_value = 3
        result = project_snapshot(repo, ds, _RUN_ID, _NOW)
        table, columns, rows = repo.replace_table.call_args.args
        self.assertEqual(table, ds.target_table)
        self.assertIn("statement_date_raw", columns)
        self.assertNotIn("source_record_id", columns)
        self.assertEqual(rows[0]["statement_date"], date(2026, 9, 1))
        self.assertEqual(rows[1]["statement_date_raw"], "bad date")
        self.assertEqual(rows[1]["synced_at"], _NOW)
        self.assertNotIn("synced_at", raw[0])
        self.assertEqual(result["statement_date_unparsed"], 1)

    def test_melt_preserves_zero_and_negative_and_skips_only_empty(self):
        repo = Mock()
        ds = dataset_by_name("fin_ecommerce_store_funds_balance")
        raw = {"dingtalk_record_id": "1", "store_name": "shop"}
        raw.update({f"balance_{ym}": Decimal("1.25") for ym in BALANCE_MONTHS})
        raw.update(balance_202601=None, balance_202602=" ", balance_202603=Decimal("0"), balance_202604=Decimal("-2"))
        repo.read_table.return_value = [raw]
        repo.replace_table.return_value = 6
        result = project_store_funds_melt(repo, ds, _RUN_ID, _NOW)
        rows = repo.replace_table.call_args.args[2]
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["month"], "2026-03")
        self.assertEqual(rows[0]["balance"], Decimal("0"))
        self.assertEqual(rows[1]["balance"], Decimal("-2"))
        self.assertEqual(result["records_read"], 1)
        self.assertEqual(result["records_new"], 6)
        self.assertEqual(result["records_skipped"], 2)


class FinanceRepositoryTests(unittest.TestCase):
    _repo = helpers.MartExtractRepositoryTests._repo

    def test_replace_is_atomic_and_empty_snapshot_clears_table(self):
        repo = self._repo()
        ds = dataset_by_name("fin_offline_receivables_aging")
        columns = [*ds.target_columns, "synced_at", "sync_run_id"]
        self.assertEqual(repo.replace_table(ds.target_table, columns, [{"ending_balance": Decimal("5")}]), 1)
        self.assertEqual(self.mart.commits, 1)
        self.assertIn("DELETE FROM", self.mart.cursor_instance.executed[0][0])
        repo.replace_table(ds.target_table, columns, [])
        self.assertEqual(len(self.mart.cursor_instance.executemany_calls), 1)
        self.assertEqual(self.mart.commits, 2)

    def test_insert_failure_rolls_back(self):
        repo = self._repo()
        ds = dataset_by_name("fin_offline_receivables_aging")
        self.mart.cursor_instance.executemany = Mock(side_effect=RuntimeError("insert failed"))
        with self.assertRaises(RuntimeError):
            repo.replace_table(ds.target_table, [*ds.target_columns, "synced_at", "sync_run_id"], [{}])
        self.assertEqual(self.mart.rollbacks, 1)
        self.assertEqual(self.mart.commits, 0)

    def test_unregistered_identifiers_rejected_before_sql(self):
        repo = self._repo()
        with self.assertRaises(MartExtractError):
            repo.read_table("unknown")
        with self.assertRaises(MartExtractError):
            repo.replace_table("unknown", ["value"], [])
        self.assertEqual(self.mart.cursor_instance.executed, [])
        self.assertEqual(self.raw.cursor_instance.executed, [])

    def test_wdt_connection_requires_configuration(self):
        with self.assertRaises(MartExtractError):
            _ = self._repo().wdt_connection


class FinanceServiceTests(unittest.TestCase):
    _service = helpers.MartExtractServiceTests._service

    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_lock_covers_snapshot_write(self, _lock):
        active = []

        class Lock:
            def __enter__(self):
                active.append(True)

            def __exit__(self, *args):
                active.pop()

        _lock.return_value = Lock()
        service = self._service(datasets=[dataset_by_name("fin_offline_receivables_aging")])

        def write(*args):
            self.assertEqual(active, [True])
            return 1

        self.repository.replace_table.side_effect = write
        service.extract()

    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_melt_saves_input_and_output_counts(self, _lock):
        ds = dataset_by_name("fin_ecommerce_store_funds_balance")
        service = self._service(datasets=[ds])
        self.repository.read_table.return_value = [{"dingtalk_record_id": "r1", "balance_202601": 0, "balance_202602": 10}]
        self.repository.replace_table.return_value = 2
        result = service.extract()
        args = self.mart_repository.save_dataset_summary.call_args.kwargs
        self.assertEqual(args["records_read"], 1)
        self.assertEqual(args["raw_records_written"], 2)
        self.assertEqual(result.datasets[0]["raw_records_written"], 2)
        self.repository.upsert_fact.assert_not_called()
        self.mart_repository.mark_completed.assert_called_once()

    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_snapshot_summary_failure_is_projection_pending(self, _lock):
        service = self._service(datasets=[dataset_by_name("fin_offline_receivables_aging")])
        self.repository.replace_table.return_value = 1
        self.mart_repository.save_dataset_summary.side_effect = RuntimeError("summary failed")
        with self.assertRaises(RuntimeError):
            service.extract()
        self.mart_repository.mark_projection_pending.assert_called_once()
        self.mart_repository.mark_failed.assert_not_called()

    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_snapshot_write_failure_is_failed(self, _lock):
        service = self._service(datasets=[dataset_by_name("fin_offline_receivables_aging")])
        self.repository.replace_table.side_effect = RuntimeError("write failed")
        with self.assertRaises(RuntimeError):
            service.extract()
        self.mart_repository.mark_failed.assert_called_once()
        self.mart_repository.mark_completed.assert_not_called()

    def test_unknown_kind_fails_without_reading(self):
        ds = replace(dataset_by_name("daily_report_offline"), kind="unknown")
        service = self._service(datasets=[ds])
        with self.assertRaises(MartExtractError):
            service.extract()
        self.repository.read_dataset.assert_not_called()
