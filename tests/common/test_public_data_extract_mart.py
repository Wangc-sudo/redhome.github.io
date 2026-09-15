"""Tests for the extraction layer (raw_dingtalk -> mart_ops)."""

import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

from common.public_data.extract_mart import (
    CALENDAR_DATASET,
    EXTRACT_SOURCE_NAME,
    MartExtractRepository,
    MartExtractService,
    MartExtractError,
)
from common.public_data.mart_extract_schema import (
    DIM_CALENDAR,
    EXTRACT_DATASETS,
    FACT_CHANNEL_DAILY_SALES,
    FACT_DAILY_REPORT_OFFLINE,
    FACT_FIN_OFFLINE_DEPOSIT,
    FACT_FIN_PLATFORM_DEPOSIT,
    FACT_FIN_PREPAYMENT_INVOICE,
    FACT_FIN_RECEIVABLES_AGING,
    FACT_FIN_STORE_FUNDS,
    dataset_by_name,
    ddl_statements,
)


_NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
_RUN_ID = "00000000-0000-0000-0000-000000000009"


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _RecordingCursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed = []
        self.executemany_calls = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def executemany(self, sql, sequence):
        self.executemany_calls.append((sql, list(sequence)))

    def fetchall(self):
        return list(self.rows)

    def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self, rows=()):
        self.cursor_instance = _RecordingCursor(rows)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------

class ExtractSchemaTests(unittest.TestCase):

    def test_registered_datasets_match_stage_b1_scope(self):
        targets = {d.dataset: d.target_table for d in EXTRACT_DATASETS}
        self.assertEqual(targets, {
            "daily_report_offline": FACT_DAILY_REPORT_OFFLINE,
            "channel_daily_sales": FACT_CHANNEL_DAILY_SALES,
            "fin_offline_receivables_aging": FACT_FIN_RECEIVABLES_AGING,
            "fin_ecommerce_prepayment_supplier_invoice": FACT_FIN_PREPAYMENT_INVOICE,
            "fin_offline_deposit_other_receivables": FACT_FIN_OFFLINE_DEPOSIT,
            "fin_ecommerce_platform_deposit": FACT_FIN_PLATFORM_DEPOSIT,
            "fin_ecommerce_store_funds_balance": FACT_FIN_STORE_FUNDS,
            "wdt_dim_product_mirror": "dim_product",
            "wdt_order_line_fact": "fact_order_line",
        })

        self.assertEqual(dataset_by_name("daily_report_offline").kind, "fact")
        self.assertEqual(dataset_by_name("daily_report_offline").source, "dingtalk")
        for name in (
            "fin_offline_receivables_aging",
            "fin_ecommerce_prepayment_supplier_invoice",
            "fin_offline_deposit_other_receivables",
            "fin_ecommerce_platform_deposit",
        ):
            self.assertEqual(dataset_by_name(name).kind, "snapshot")
        self.assertEqual(
            dataset_by_name("fin_ecommerce_store_funds_balance").kind,
            "melt_store_funds",
        )
        dim_mirror = dataset_by_name("wdt_dim_product_mirror")
        self.assertEqual(dim_mirror.kind, "dim_mirror")
        self.assertEqual(dim_mirror.source, "wdt")
        self.assertEqual(dim_mirror.source_table, "dim_product")
        order_line = dataset_by_name("wdt_order_line_fact")
        self.assertEqual(order_line.kind, "order_line_expand")
        self.assertEqual(order_line.source, "wdt")
        self.assertEqual(order_line.source_table, "wdt_records")
        # 注册顺序即执行顺序：镜像必须先于订单行展开（品牌反查依赖）。
        names = [d.dataset for d in EXTRACT_DATASETS]
        self.assertLess(
            names.index("wdt_dim_product_mirror"),
            names.index("wdt_order_line_fact"),
        )

    def test_retired_and_technical_columns_are_not_projected(self):
        """The projection is an allow-list, so dropped columns cannot leak in."""
        offline = dataset_by_name("daily_report_offline")
        self.assertNotIn("achievement_rate", offline.target_columns)
        self.assertNotIn("dingtalk_record_id", offline.target_columns)
        self.assertNotIn("synced_at", offline.target_columns)
        self.assertNotIn("sync_run_id", offline.target_columns)

        channel = dataset_by_name("channel_daily_sales")
        self.assertNotIn("parent_record_refs", channel.target_columns)

    def test_unknown_dataset_is_rejected(self):
        with self.assertRaises(KeyError):
            dataset_by_name("not_a_dataset")

    def test_ddl_covers_every_extract_table(self):
        statements = ddl_statements()
        joined = "\n".join(statements)
        for table in (
            FACT_DAILY_REPORT_OFFLINE,
            FACT_CHANNEL_DAILY_SALES,
            FACT_FIN_RECEIVABLES_AGING,
            FACT_FIN_PREPAYMENT_INVOICE,
            FACT_FIN_OFFLINE_DEPOSIT,
            FACT_FIN_PLATFORM_DEPOSIT,
            FACT_FIN_STORE_FUNDS,
            DIM_CALENDAR,
            "dim_robot_member",
            "dim_product",
            "fact_order_line",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS `{table}`", joined)
        for column in (
            "ending_balance",
            "ap_estimated_amount",
            "uninvoiced_amount",
            "statement_date",
            "month",
            "paid_amount",
            "platform_subsidy",
            "shop_subsidy",
            "line_no",
            "brand_name",
        ):
            self.assertIn(f"`{column}`", joined)
        projection_tables = (
            FACT_FIN_RECEIVABLES_AGING,
            FACT_FIN_PREPAYMENT_INVOICE,
            FACT_FIN_OFFLINE_DEPOSIT,
            FACT_FIN_PLATFORM_DEPOSIT,
            FACT_FIN_STORE_FUNDS,
            "dim_product",
            "fact_order_line",
        )
        for table in projection_tables:
            ddl = next(
                statement for statement in statements
                if f"CREATE TABLE IF NOT EXISTS `{table}`" in statement
            )
            self.assertIn("`synced_at`", ddl)
            self.assertIn("`sync_run_id`", ddl)
        order_line_ddl = next(
            statement for statement in statements
            if "CREATE TABLE IF NOT EXISTS `fact_order_line`" in statement
        )
        self.assertIn("PRIMARY KEY (`trade_no`, `line_no`)", order_line_ddl)
        # The extract line brings its own summary value into the shared table.
        self.assertIn("ENUM('dingtalk','wdt','extract')", joined)

    def test_calendar_ddl_has_no_achievement_rate_leak(self):
        self.assertNotIn("achievement_rate", "\n".join(ddl_statements()))


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class MartExtractRepositoryTests(unittest.TestCase):

    def _repo(self, rows=()):
        self.raw = _FakeConnection(rows)
        self.mart = _FakeConnection()
        return MartExtractRepository(self.raw, self.mart)

    def test_read_dataset_selects_only_whitelisted_columns(self):
        repo = self._repo()
        repo.read_dataset(dataset_by_name("daily_report_offline"))

        sql, _ = self.raw.cursor_instance.executed[0]
        self.assertIn("`dingtalk_record_id` AS `source_record_id`", sql)
        self.assertIn("`region` AS `region`", sql)
        self.assertNotIn("achievement_rate", sql)
        self.assertIn("FROM `daily_report_offline`", sql)

    def test_read_dataset_returns_target_keyed_dicts(self):
        repo = self._repo(rows=[{"source_record_id": "r1", "region": "hangzhou"}])
        rows = repo.read_dataset(dataset_by_name("daily_report_offline"))
        self.assertEqual(rows, [{"source_record_id": "r1", "region": "hangzhou"}])

    def test_upsert_fact_is_idempotent_and_ordered(self):
        repo = self._repo()
        dataset = dataset_by_name("daily_report_offline")
        repo.upsert_fact(
            dataset,
            [{"source_record_id": "r1", "region": "hangzhou", "note": "n"}],
            sync_run_id=_RUN_ID,
            synced_at=_NOW,
        )

        sql, params = self.mart.cursor_instance.executed[0]
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertNotIn("`source_record_id` = VALUES(`source_record_id`)", sql)
        self.assertEqual(params[0], "r1")
        self.assertEqual(params[1], "hangzhou")
        self.assertEqual(params[-2], _NOW)
        self.assertEqual(params[-1], _RUN_ID)

    def test_replace_dim_calendar_deletes_then_inserts(self):
        repo = self._repo()
        repo.replace_dim_calendar(
            [(date(2026, 9, 5), 1, "local", None)],
            sync_run_id=_RUN_ID,
            synced_at=_NOW,
        )

        sql, _ = self.mart.cursor_instance.executed[0]
        self.assertEqual(sql, f"DELETE FROM `{DIM_CALENDAR}`")
        insert_sql, sequence = self.mart.cursor_instance.executemany_calls[0]
        self.assertIn("INSERT INTO `dim_calendar`", insert_sql)
        self.assertEqual(sequence, [(date(2026, 9, 5), 1, "local", None, _NOW, _RUN_ID)])

    def test_replace_dim_calendar_skips_insert_when_empty(self):
        repo = self._repo()
        repo.replace_dim_calendar([], sync_run_id=_RUN_ID, synced_at=_NOW)
        self.assertEqual(self.mart.cursor_instance.executemany_calls, [])


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class MartExtractServiceTests(unittest.TestCase):

    def _service(self, *, datasets=None, calendar_months=(), org_rows=()):
        self.repository = Mock()
        self.repository.read_dataset.return_value = [
            {"source_record_id": "r1", "region": "hangzhou"},
        ]
        # 默认「从未同步过通讯录」：返回空列表，跳过 dim_robot_member 步骤。
        self.repository.read_org_members.return_value = list(org_rows)
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
    def test_extract_runs_every_dataset_then_completes(self, _lock, _txn):
        service = self._service(datasets=[dataset_by_name("daily_report_offline")])
        result = service.extract()

        self.assertEqual(result.run_id, _RUN_ID)
        self.mart_repository.start_run.assert_called_once()
        self.mart_repository.mark_raw_committed.assert_called_once()
        self.mart_repository.mark_completed.assert_called_once_with(
            sync_run_id=_RUN_ID, finished_at=_NOW,
        )
        self.mart_repository.mark_failed.assert_not_called()
        self.assertTrue(self.mart_connection.commits >= 1)

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_summaries_are_attributed_to_the_extract_line(self, _lock, _txn):
        service = self._service(datasets=[dataset_by_name("daily_report_offline")])
        service.extract()

        kwargs = self.mart_repository.save_dataset_summary.call_args.kwargs
        self.assertEqual(kwargs["source_name"], EXTRACT_SOURCE_NAME)
        self.assertEqual(kwargs["dataset_name"], "daily_report_offline")
        self.assertEqual(kwargs["records_read"], 1)
        self.assertEqual(kwargs["raw_records_written"], 1)

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_calendar_is_skipped_without_a_seed(self, _lock, _txn):
        service = self._service(datasets=[dataset_by_name("daily_report_offline")])
        service.extract()
        self.repository.replace_dim_calendar.assert_not_called()

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_calendar_derives_workdays_from_rest_days(self, _lock, _txn):
        service = self._service(
            datasets=(),
            calendar_months=[(2026, 9, [6, 13, 19, 25, 26, 27], "local")],
        )
        service.extract()

        rows = self.repository.replace_dim_calendar.call_args.args[0]
        self.assertEqual(len(rows), 30)
        by_day = {business_date.day: is_workday
                  for business_date, is_workday, _, _ in rows}
        self.assertEqual(by_day[6], 0)
        self.assertEqual(by_day[19], 0)
        self.assertEqual(by_day[5], 1)
        self.assertEqual(by_day[30], 1)
        sources = {source for _, _, source, _ in rows}
        self.assertEqual(sources, {"local"})

        kwargs = self.mart_repository.save_dataset_summary.call_args.kwargs
        self.assertEqual(kwargs["dataset_name"], CALENDAR_DATASET)
        self.assertEqual(kwargs["records_read"], 30)

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_february_uses_the_real_month_length(self, _lock, _txn):
        service = self._service(
            datasets=(),
            calendar_months=[(2024, 2, [], "local")],
        )
        service.extract()
        rows = self.repository.replace_dim_calendar.call_args.args[0]
        self.assertEqual(len(rows), 29)

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_summary_failure_marks_projection_pending(self, _lock, _txn):
        service = self._service(datasets=[dataset_by_name("daily_report_offline")])
        self.mart_repository.save_dataset_summary.side_effect = RuntimeError("db")

        with self.assertRaises(RuntimeError):
            service.extract()

        self.mart_repository.mark_projection_pending.assert_called_once()
        self.mart_repository.mark_completed.assert_not_called()

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_read_failure_marks_run_failed(self, _lock, _txn):
        service = self._service(datasets=[dataset_by_name("daily_report_offline")])
        self.repository.read_dataset.side_effect = RuntimeError("raw down")

        with self.assertRaises(RuntimeError):
            service.extract()

        kwargs = self.mart_repository.mark_failed.call_args.kwargs
        self.assertEqual(kwargs["failure_code"], "extract_failed")
        self.mart_repository.mark_completed.assert_not_called()

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_mart_extract_error_supplies_its_own_failure_code(self, _lock, _txn):
        service = self._service(datasets=[dataset_by_name("daily_report_offline")])
        self.repository.read_dataset.side_effect = MartExtractError("bad_plan")

        with self.assertRaises(MartExtractError):
            service.extract()

        self.assertEqual(
            self.mart_repository.mark_failed.call_args.kwargs["failure_code"],
            "bad_plan",
        )

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_plan_digest_is_stable_and_covers_the_calendar(self, _lock, _txn):
        plain = self._service(datasets=[dataset_by_name("daily_report_offline")])
        self.assertEqual(plain._plan_digest(), plain._plan_digest())
        self.assertEqual(len(plain._plan_digest()), 64)

        calendar = self._service(
            datasets=[dataset_by_name("daily_report_offline")],
            calendar_months=[(2026, 9, [6], "local")],
        )
        self.assertNotEqual(plain._plan_digest(), calendar._plan_digest())


class OrgMemberRepositoryTests(unittest.TestCase):

    def _repo(self, rows=()):
        self.raw = _FakeConnection(rows)
        self.mart = _FakeConnection()
        return MartExtractRepository(self.raw, self.mart)

    def test_read_org_members_scopes_to_the_latest_run(self):
        repo = self._repo()
        repo.read_org_members()

        sql, _ = self.raw.cursor_instance.executed[0]
        self.assertIn("FROM `dingtalk_org_member`", sql)
        self.assertIn("ORDER BY `synced_at` DESC LIMIT 1", sql)
        self.assertIn("`sync_run_id` = (", sql)

    def test_replace_dim_robot_member_deletes_then_inserts_active_rows(self):
        repo = self._repo()
        repo.replace_dim_robot_member(
            [{
                "user_id": "u1",
                "name": "张三",
                "region": "hangzhou",
                "dept_id": "1049728636",
                "dept_name": "杭中",
            }],
            sync_run_id=_RUN_ID,
            synced_at=_NOW,
        )

        sql, _ = self.mart.cursor_instance.executed[0]
        self.assertEqual(sql, "DELETE FROM `dim_robot_member`")
        insert_sql, sequence = self.mart.cursor_instance.executemany_calls[0]
        self.assertIn("INSERT INTO `dim_robot_member`", insert_sql)
        self.assertEqual(sequence, [(
            "u1", "张三", "hangzhou", "1049728636", "杭中", 1, _NOW, _RUN_ID,
        )])


class OrgMemberExtractTests(unittest.TestCase):

    def _service(self, org_rows):
        self.repository = Mock()
        self.repository.read_org_members.return_value = list(org_rows)
        self.mart_repository = Mock()
        self.mart_connection = _FakeConnection()
        return MartExtractService(
            repository=self.repository,
            mart_repository=self.mart_repository,
            mart_connection=self.mart_connection,
            now=lambda: _NOW,
            new_run_id=lambda: _RUN_ID,
            datasets=(),
            calendar_months=(),
        )

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_org_snapshot_is_projected_and_summarised(self, _lock, _txn):
        service = self._service([{
            "user_id": "u1",
            "name": "张三",
            "region": "hangzhou",
            "dept_id": "1049728636",
            "dept_name": "杭中",
        }])
        result = service.extract()

        self.repository.replace_dim_robot_member.assert_called_once()
        rows = self.repository.replace_dim_robot_member.call_args.args[0]
        self.assertEqual(rows[0]["region"], "hangzhou")

        kwargs = self.mart_repository.save_dataset_summary.call_args.kwargs
        self.assertEqual(kwargs["source_name"], EXTRACT_SOURCE_NAME)
        self.assertEqual(kwargs["dataset_name"], "dim_robot_member")
        self.assertEqual(kwargs["records_read"], 1)
        self.assertEqual(len(result.datasets), 1)

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_empty_raw_skips_the_dim_instead_of_wiping_it(self, _lock, _txn):
        service = self._service([])
        result = service.extract()

        self.repository.replace_dim_robot_member.assert_not_called()
        self.mart_repository.save_dataset_summary.assert_not_called()
        self.mart_repository.mark_completed.assert_called_once()
        self.assertEqual(result.datasets, [])

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_org_summary_failure_marks_projection_pending(self, _lock, _txn):
        service = self._service([{
            "user_id": "u1", "name": "张三", "region": "hangzhou",
        }])
        self.mart_repository.save_dataset_summary.side_effect = RuntimeError("db")

        with self.assertRaises(RuntimeError):
            service.extract()

        self.mart_repository.mark_projection_pending.assert_called_once()
        self.mart_repository.mark_completed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
