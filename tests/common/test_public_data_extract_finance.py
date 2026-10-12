import unittest
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import Mock, patch

from common.public_data import extract_finance
from common.public_data.extract_finance import (
    BALANCE_MONTHS, _parse_name, load_store_channel_mapping,
    parse_statement_date, project_snapshot, project_store_expense_melt,
    project_store_funds_melt,
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


class StoreExpenseMeltTests(unittest.TestCase):
    """双源费用宽表 → fact_fin_store_expense 窄表（2026-10-12 签认预填版）。"""

    def _commission_row(self, **over):
        row = {
            "dingtalk_record_id": "c1",
            "store_name": "习酒",
            "platform": "{'name': '天猫', 'id': 1}",
            "fee_category": "{'name': '扣点', 'id': 2}",
            "fee_item": "{'name': '平台扣点', 'id': 3}",
        }
        row.update({f"amount_2026_{m:02d}": None for m in range(1, 13)})
        row.update(over)
        return row

    def _recharge_row(self, **over):
        row = {
            "dingtalk_record_id": "r1",
            "store_name": "习酒",
            "platform": "{'name': '天猫', 'id': 1}",
            "fee_item": "推广费充值",
        }
        row.update({f"amount_2026_{m:02d}": None for m in range(1, 13)})
        row.update(over)
        return row

    def _melt(self, commission, recharge=(), mapping=None):
        repo = Mock()
        ds = dataset_by_name("fin_store_expense")
        repo.read_table.return_value = commission
        with patch.object(
            extract_finance, "load_store_channel_mapping",
            return_value=mapping or {},
        ), patch.object(
            extract_finance, "_read_recharge_rows", return_value=list(recharge)
        ):
            result = project_store_expense_melt(repo, ds, _RUN_ID, _NOW)
        return repo, result

    def test_wide_to_narrow_parses_pseudo_json_and_maps_channel(self):
        repo, result = self._melt(
            [self._commission_row(
                amount_2026_01=Decimal("100.5"), amount_2026_03=Decimal("0")
            )],
            mapping={("习酒", "天猫"): ("TM习酒酒类旗舰店", "天猫")},
        )
        table, columns, rows = repo.replace_table.call_args.args
        self.assertEqual(table, "fact_fin_store_expense")
        self.assertEqual(
            columns,
            list(extract_finance._STORE_EXPENSE_COLUMNS) + ["synced_at", "sync_run_id"],
        )
        self.assertEqual(
            [(r["month"], r["expense_amount"]) for r in rows],
            [("2026-01", Decimal("100.5")), ("2026-03", Decimal("0"))],
        )
        row = rows[0]
        self.assertEqual(row["source"], "store_commission")
        self.assertEqual(row["fin_platform"], "天猫")
        self.assertEqual(row["fee_category"], "扣点")
        self.assertEqual(row["fee_item"], "平台扣点")
        self.assertEqual(row["mart_store_name"], "TM习酒酒类旗舰店")
        self.assertEqual(row["mart_channel"], "天猫")
        self.assertEqual(result["records_read"], 1)
        self.assertEqual(result["records_skipped"], 10)
        self.assertEqual(result["unmapped_fin_stores"], [])

    def test_unmapped_store_lands_in_unmapped_bucket(self):
        repo, result = self._melt(
            [self._commission_row(
                store_name="得物", platform="{'name': '天猫', 'id': 9}",
                amount_2026_02=Decimal("12"),
            )]
        )
        _, _, rows = repo.replace_table.call_args.args
        self.assertIsNone(rows[0]["mart_store_name"])
        self.assertIsNone(rows[0]["mart_channel"])
        self.assertEqual(result["unmapped_fin_stores"], ["得物（天猫）"])

    def test_recharge_source_and_duplicate_business_key_amounts_are_summed(self):
        # 多法人主体重复提报同一业务键 → 金额求和，不炸 replace_table
        commission = [
            self._commission_row(amount_2026_01=Decimal("10")),
            self._commission_row(dingtalk_record_id="c2", amount_2026_01=Decimal("15")),
        ]
        repo, result = self._melt(
            commission,
            recharge=[self._recharge_row(amount_2026_01=Decimal("7"))],
            mapping={("习酒", "天猫"): ("TM习酒酒类旗舰店", "天猫")},
        )
        _, _, rows = repo.replace_table.call_args.args
        self.assertEqual(len(rows), 2)
        by_source = {r["source"]: r["expense_amount"] for r in rows}
        self.assertEqual(by_source["store_commission"], Decimal("25"))
        self.assertEqual(by_source["promotion_recharge"], Decimal("7"))
        recharge_row = next(r for r in rows if r["source"] == "promotion_recharge")
        self.assertIsNone(recharge_row["fee_category"])  # 源表无此列
        self.assertEqual(recharge_row["fee_item"], "推广费充值")
        self.assertEqual(recharge_row["mart_channel"], "天猫")
        self.assertEqual(result["records_read"], 3)

    def test_plain_text_columns_pass_through(self):
        repo, _ = self._melt(
            [self._commission_row(
                platform="天猫", fee_item="平台扣点", amount_2026_05=Decimal("3")
            )]
        )
        _, _, rows = repo.replace_table.call_args.args
        self.assertEqual(rows[0]["fin_platform"], "天猫")
        self.assertEqual(rows[0]["fee_item"], "平台扣点")


class ParseNameTests(unittest.TestCase):

    def test_pseudo_json_plain_and_blank(self):
        self.assertEqual(_parse_name("{'name': '天猫', 'id': 1}"), "天猫")
        self.assertEqual(_parse_name("天猫"), "天猫")
        self.assertIsNone(_parse_name(None))
        self.assertIsNone(_parse_name("  "))
        self.assertIsNone(_parse_name("{'name': '', 'id': 1}"))


class StoreChannelSeedTests(unittest.TestCase):

    def test_signed_off_seed_loads_and_is_structurally_sound(self):
        # 预填版签认推进（2026-10-12）；provisional 行后续直接改 JSON。
        # 结构性断言而非值断言：种子是人工维护文件，测试钉结构不钉内容。
        mapping = load_store_channel_mapping()
        self.assertTrue(mapping)
        for (fin_store, fin_platform), (mart_store, mart_channel) in mapping.items():
            self.assertTrue(fin_store and fin_platform, (fin_store, fin_platform))
            self.assertTrue(
                mart_store is None or isinstance(mart_store, str), mart_store
            )
            self.assertTrue(
                mart_channel is None or isinstance(mart_channel, str), mart_channel
            )


class StoreExpenseRepositoryTests(unittest.TestCase):
    _repo = helpers.MartExtractRepositoryTests._repo

    def test_read_table_allows_the_expense_source(self):
        repo = self._repo(rows=[{"dingtalk_record_id": "c1"}])
        self.assertEqual(
            repo.read_table("fin_store_commission"),
            [{"dingtalk_record_id": "c1"}],
        )

    def test_replace_accepts_expense_columns_and_rejects_unknown(self):
        repo = self._repo()
        ds = dataset_by_name("fin_store_expense")
        columns = list(extract_finance._STORE_EXPENSE_COLUMNS) + ["synced_at", "sync_run_id"]
        self.assertEqual(
            repo.replace_table(
                ds.target_table, columns,
                [{"month": "2026-01", "expense_amount": Decimal("1")}],
            ),
            1,
        )
        with self.assertRaises(MartExtractError):
            repo.replace_table(ds.target_table, columns + ["hacker"], [{"hacker": "x"}])
