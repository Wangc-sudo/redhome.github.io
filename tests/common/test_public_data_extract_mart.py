"""Tests for the extraction layer (raw_dingtalk -> mart_ops)."""

import json
import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

from common.calendar_utils import month_days
from common.public_data.extract_mart import (
    CALENDAR_DATASET,
    EXTRACT_SOURCE_NAME,
    MartExtractRepository,
    MartExtractService,
    MartExtractError,
    _drop_placeholder_channel_rows,
    _normalize_region_keys,
)
from common.public_data.mart_extract_schema import (
    DIM_CALENDAR,
    DIM_ROBOT_MEMBER,
    EXTRACT_DATASETS,
    ExtractDataset,
    FACT_CHANNEL_DAILY_SALES,
    FACT_DAILY_REPORT_OFFLINE,
    FACT_FIN_OFFLINE_DEPOSIT,
    FACT_FIN_PLATFORM_DEPOSIT,
    FACT_FIN_PREPAYMENT_INVOICE,
    FACT_FIN_RECEIVABLES_AGING,
    FACT_FIN_STORE_EXPENSE,
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
        self.count_override = None
        self._fetchone_rows = list(rows)

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def executemany(self, sql, sequence):
        self.executemany_calls.append((sql, list(sequence)))

    def fetchone(self):
        if self.executed and "SELECT COUNT(" in self.executed[-1][0]:
            if self.count_override is not None:
                return {"row_count": self.count_override}
            return {"row_count": sum(
                len(sequence) for sql, sequence in self.executemany_calls
                if "INSERT INTO `__extract_s_" in sql
            )}
        if self._fetchone_rows:
            return self._fetchone_rows.pop(0)
        return None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        self.closed = True


class _WindowCountCursor(_RecordingCursor):
    def __init__(self, count_rows):
        super().__init__()
        self.count_rows = list(count_rows)

    def fetchone(self):
        return self.count_rows.pop(0) if self.count_rows else None


class _FakeConnection:
    def __init__(self, rows=()):
        self.cursor_instance = _RecordingCursor(rows)
        self.cursor_classes = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, cursor_class=None):
        self.cursor_classes.append(cursor_class)
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _capture_replace_batches(repository, *, error=None):
    """Consume streaming batches and retain expanded rows for field assertions."""
    repository.expanded_rows = []
    repository.expanded_batches = []

    def replace(_target_table, _columns, batches):
        written = 0
        for batch in batches:
            rows = list(batch)
            repository.expanded_batches.append(rows)
            repository.expanded_rows.extend(rows)
            written += len(rows)
        if error is not None:
            raise error
        return written

    repository.replace_table_batches.side_effect = replace
    return repository


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
            "fin_store_expense": FACT_FIN_STORE_EXPENSE,
            "wdt_dim_product_mirror": "dim_product",
            "wdt_order_line_fact": "fact_order_line",
            "wdt_order_cancel_fact": "fact_order_cancel_line",
            "wdt_stockout_line_fact": "fact_stockout_line",
            "wdt_refund_line_fact": "fact_refund_line",
            "wdt_sales_daily": "fact_sales_daily",
            "wdt_inventory_sku_daily": "fact_inventory_sku_daily",
            "wdt_purchase_inbound": "fact_purchase_inbound",
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
        self.assertEqual(
            dataset_by_name("fin_store_expense").kind, "melt_store_expense"
        )
        self.assertEqual(
            dataset_by_name("fin_store_expense").source_table,
            "fin_store_commission",
        )
        dim_mirror = dataset_by_name("wdt_dim_product_mirror")
        self.assertEqual(dim_mirror.kind, "dim_mirror")
        self.assertEqual(dim_mirror.source, "wdt")
        self.assertEqual(dim_mirror.source_table, "dim_product")
        order_line = dataset_by_name("wdt_order_line_fact")
        self.assertEqual(order_line.kind, "order_line_expand")
        self.assertEqual(order_line.source, "wdt")
        self.assertEqual(order_line.source_table, "wdt_records")
        order_cancel = dataset_by_name("wdt_order_cancel_fact")
        self.assertEqual(order_cancel.kind, "order_cancel_expand")
        self.assertEqual(order_cancel.source, "wdt")
        self.assertEqual(order_cancel.source_table, "wdt_records")
        self.assertEqual(order_cancel.target_table, "fact_order_cancel_line")
        # 注册顺序即执行顺序：镜像必须先于订单行展开（品牌反查依赖）。
        names = [d.dataset for d in EXTRACT_DATASETS]
        self.assertLess(
            names.index("wdt_dim_product_mirror"),
            names.index("wdt_order_line_fact"),
        )
        # 取消单同源 wdt_records、只用交易级字段，紧随主投影之后。
        self.assertLess(
            names.index("wdt_order_line_fact"),
            names.index("wdt_order_cancel_fact"),
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
            "fact_stockout_line",
            "fact_refund_line",
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

        # executemany 批量写入（排查报告 §2.1 P1）：单次 round-trip。
        self.assertEqual(self.mart.cursor_instance.executed, [])
        (sql, sequence), = self.mart.cursor_instance.executemany_calls
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertNotIn("`source_record_id` = VALUES(`source_record_id`)", sql)
        (params,) = sequence
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

    @staticmethod
    def _streaming_order_line_columns():
        from common.public_data.extract_order_line import _ORDER_LINE_COLUMNS
        return list(_ORDER_LINE_COLUMNS) + ["synced_at", "sync_run_id"]

    def test_replace_table_batches_reconciles_and_publishes_staging(self):
        repo = self._repo()
        columns = self._streaming_order_line_columns()
        batches = iter([
            [{"trade_no": "T-1", "line_no": 1}],
            [],
            [{"trade_no": "T-2", "line_no": 1},
             {"trade_no": "T-2", "line_no": 2}],
        ])

        written = repo.replace_table_batches(
            "fact_order_line", columns, batches,
        )

        self.assertEqual(written, 3)
        cursor = self.mart.cursor_instance
        executed = [sql for sql, _ in cursor.executed]
        create_sql = executed[0]
        staging = create_sql.split("`")[1]
        backup = staging.replace("__extract_s_", "__extract_b_", 1)
        self.assertTrue(staging.startswith("__extract_s_"))
        self.assertEqual(len(staging), len("__extract_s_") + 32)
        self.assertEqual(
            create_sql,
            f"CREATE TABLE `{staging}` LIKE `fact_order_line`",
        )
        self.assertEqual(len(cursor.executemany_calls), 2)
        self.assertEqual(
            [len(sequence) for _, sequence in cursor.executemany_calls],
            [1, 2],
        )
        for insert_sql, _ in cursor.executemany_calls:
            self.assertTrue(
                insert_sql.startswith(f"INSERT INTO `{staging}` ("),
                insert_sql,
            )
        self.assertIn(
            f"SELECT COUNT(*) AS `row_count` FROM `{staging}`", executed,
        )
        self.assertIn(
            f"RENAME TABLE `fact_order_line` TO `{backup}`, "
            f"`{staging}` TO `fact_order_line`",
            executed,
        )
        self.assertEqual(executed[-1], f"DROP TABLE `{backup}`")
        self.assertFalse(any("DELETE FROM `fact_order_line`" in sql
                             for sql in executed))

    def test_replace_table_batches_insert_failure_preserves_old_table(self):
        class _FailInsertCursor(_RecordingCursor):
            def executemany(self, sql, sequence):
                raise RuntimeError("insert failed")

        repo = self._repo()
        self.mart.cursor_instance = _FailInsertCursor()

        with self.assertRaisesRegex(RuntimeError, "insert failed"):
            repo.replace_table_batches(
                "fact_order_line",
                self._streaming_order_line_columns(),
                iter([[{"trade_no": "T-1", "line_no": 1}]]),
            )

        executed = [sql for sql, _ in self.mart.cursor_instance.executed]
        self.assertFalse(any(sql.startswith("RENAME TABLE") for sql in executed))
        self.assertFalse(any("DELETE FROM `fact_order_line`" in sql
                             for sql in executed))
        self.assertTrue(any(
            sql.startswith("DROP TABLE IF EXISTS `__extract_s_")
            for sql in executed
        ))

    def test_replace_table_batches_count_failure_preserves_old_table(self):
        repo = self._repo()
        self.mart.cursor_instance.count_override = 0

        with self.assertRaisesRegex(MartExtractError, "staging row count mismatch"):
            repo.replace_table_batches(
                "fact_order_line",
                self._streaming_order_line_columns(),
                iter([[{"trade_no": "T-1", "line_no": 1}]]),
            )

        executed = [sql for sql, _ in self.mart.cursor_instance.executed]
        self.assertTrue(any("SELECT COUNT(" in sql for sql in executed))
        self.assertFalse(any(sql.startswith("RENAME TABLE") for sql in executed))
        self.assertFalse(any("DELETE FROM `fact_order_line`" in sql
                             for sql in executed))
        self.assertTrue(any(
            sql.startswith("DROP TABLE IF EXISTS `__extract_s_")
            for sql in executed
        ))

    def test_window_replay_preserves_outside_and_null_rows_before_publishing(self):
        repo = self._repo()
        cursor = _WindowCountCursor([
            {"row_count": 6, "window_count": 2},
            {"row_count": 5},
        ])
        self.mart.cursor_instance = cursor

        result = repo.replace_table_window_batches(
            "fact_order_line", self._streaming_order_line_columns(),
            iter([[{"trade_no": "T-new", "line_no": 1}]]),
            window_start=date(2026, 10, 26), window_end=date(2026, 10, 26),
        )

        self.assertEqual(result, {
            "before_count": 6, "window_before": 2, "window_after": 1,
            "after_count": 5, "records_written": 1,
        })
        count_sql, count_params = cursor.executed[0]
        self.assertIn("`trade_time` >= %s", count_sql)
        self.assertIn("`trade_time` < %s", count_sql)
        self.assertEqual(
            count_params, (date(2026, 10, 26), date(2026, 10, 27)),
        )
        copy_sql, copy_params = next(
            (sql, params) for sql, params in cursor.executed
            if sql.startswith("INSERT INTO `__extract_s_")
            and "SELECT" in sql and "FROM `fact_order_line`" in sql
        )
        self.assertIn("`trade_time` < %s", copy_sql)
        self.assertIn("`trade_time` >= %s", copy_sql)
        self.assertIn("`trade_time` IS NULL", copy_sql)
        self.assertEqual(
            copy_params, (date(2026, 10, 26), date(2026, 10, 27)),
        )
        executed = [sql for sql, _ in cursor.executed]
        self.assertTrue(any(sql.startswith("RENAME TABLE") for sql in executed))

    def test_window_replay_count_failure_never_renames(self):
        repo = self._repo()
        cursor = _WindowCountCursor([
            {"row_count": 4, "window_count": 1},
            {"row_count": 99},
        ])
        self.mart.cursor_instance = cursor

        with self.assertRaisesRegex(MartExtractError, "staging row count mismatch"):
            repo.replace_table_window_batches(
                "fact_order_line", self._streaming_order_line_columns(),
                iter([[{"trade_no": "T-new", "line_no": 1}]]),
                window_start=date(2026, 10, 26), window_end=date(2026, 10, 26),
            )

        executed = [sql for sql, _ in cursor.executed]
        self.assertFalse(any(sql.startswith("RENAME TABLE") for sql in executed))
        self.assertTrue(any(
            sql.startswith("DROP TABLE IF EXISTS `__extract_s_")
            for sql in executed
        ))


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class RegionKeyNormalizeTests(unittest.TestCase):
    """fact 表 region 列统一为键：AI 表显示名止于 raw 层（2026-09-22 双跑发现）。"""

    @staticmethod
    def _dataset(name):
        return next(d for d in EXTRACT_DATASETS if d.dataset == name)

    def test_display_names_map_to_region_keys(self):
        rows = [
            {"region": "杭州", "responsible_person": "余发兴"},
            {"region": "绍兴 ", "responsible_person": "某人"},
            {"region": "万科&大莲花&团购", "responsible_person": "万科体验馆·零售"},
            # 2026-10-16：线下其他区日报表（inject「其他区」→ other）
            {"region": "其他区", "responsible_person": "李树军"},
            {"region": "未登记区域", "responsible_person": "某人"},
        ]
        _normalize_region_keys(self._dataset("daily_report_offline"), rows)
        self.assertEqual(
            [r["region"] for r in rows],
            ["hangzhou", "shaoxing", "vanke", "other", "未登记区域"],
        )

    def test_other_datasets_are_untouched(self):
        rows = [{"region": "杭州"}]
        _normalize_region_keys(self._dataset("channel_daily_sales"), rows)
        self.assertEqual(rows[0]["region"], "杭州")


class ChannelPlaceholderRowTests(unittest.TestCase):
    """渠道日销预置空行/未来日期行过滤（2026-10-23 运维裁决，写入侧根治）。

    AI 表被上游预置到月底的空行（sales_amount NULL）不该进 fact——否则
    MAX(business_date) 锚点被带偏到月底，智能体会把未来日期当成今天。
    """

    @staticmethod
    def _dataset(name="channel_daily_sales"):
        return next(d for d in EXTRACT_DATASETS if d.dataset == name)

    def test_null_and_blank_amount_rows_dropped(self):
        rows = [
            {"business_date": "2026-10-20", "sales_amount": None},
            {"business_date": "2026-10-20", "sales_amount": ""},
            {"business_date": "2026-10-20", "sales_amount": "   "},
            {"business_date": "2026-10-20", "sales_amount": 0},
            {"business_date": "2026-10-20", "sales_amount": 12345},
        ]
        kept = _drop_placeholder_channel_rows(self._dataset(), rows)
        self.assertEqual(len(kept), 2)
        self.assertEqual([r["sales_amount"] for r in kept], [0, 12345])

    def test_future_dated_rows_dropped(self):
        rows = [
            {"business_date": date(2099, 1, 1), "sales_amount": 100},
            {"business_date": "2099-01-02", "sales_amount": 100},
            {"business_date": "2020-01-01", "sales_amount": 100},
            {"business_date": None, "sales_amount": 100},
        ]
        kept = _drop_placeholder_channel_rows(self._dataset(), rows)
        self.assertEqual(len(kept), 2)

    def test_other_datasets_are_untouched(self):
        rows = [{"business_date": "2099-01-01", "sales_amount": None}]
        self.assertEqual(
            _drop_placeholder_channel_rows(
                self._dataset("daily_report_offline"), rows),
            rows,
        )


class OtherMemberResolveTests(unittest.TestCase):
    """其他区日报成员字段归位（2026-10-19 表重构，用户裁决）：
    成员字段 JSON → 姓名；部门按通讯录注入；月目标走 dim_report_target。"""

    @staticmethod
    def _dataset():
        return next(d for d in EXTRACT_DATASETS
                    if d.dataset == "daily_report_offline")

    def test_parse_user_field_shapes(self):
        from common.public_data.extract_mart import _parse_user_field
        # 云上实测形态（2026-10-19）：单引号 repr + unionId/name 键
        self.assertEqual(
            {"user_id": None, "union_id": "xAteG0Vyyii", "name": "余云涛"},
            _parse_user_field("[{'unionId': 'xAteG0Vyyii', 'name': '余云涛'}]"))
        # 严格 JSON + userId 键也认
        self.assertEqual(
            {"user_id": "224529", "union_id": None, "name": "余云涛"},
            _parse_user_field(
                '[{"userId": "224529", "name": "余云涛"}]'))
        self.assertEqual(
            {"user_id": "u1", "union_id": None, "name": "张三"},
            _parse_user_field([{"userId": "u1", "name": "张三"}]))
        self.assertIsNone(_parse_user_field("李树军"))    # 历史文本行放行
        self.assertIsNone(_parse_user_field(None))
        self.assertIsNone(_parse_user_field("[]"))
        self.assertIsNone(_parse_user_field('[{"x": 1}]'))

    def test_other_row_resolved_dept_and_target_injected(self):
        from common.public_data.extract_mart import _resolve_other_member_rows

        class _Cur:
            def execute(self, sql, params=None):
                if "dim_robot_member" in sql:
                    self._rows = [{"dept_name": "省外销售部", "name": "余云涛"}]
                elif "dim_report_target" in sql:
                    # dim 万元口径（v5 迁移后）：50 万 → fact 500000 元
                    self._rows = [{"monthly_target": 50}]
                else:  # pragma: no cover - 防呆
                    raise AssertionError(sql)

            def fetchall(self):
                return self._rows

            def close(self):
                return None

        class _Conn:
            def cursor(self):
                return _Cur()

        rows = [
            {"region": "other",
             "responsible_person":
                 '[{"userId": "22452911181385570818", "name": "余云涛"}]',
             "department": None, "business_date": "2026-10-19",
             "sales_amount": "1200", "monthly_target": None},
            {"region": "hangzhou", "responsible_person": "余发兴"},
            {"region": "other", "responsible_person": "李树军",
             "business_date": "2026-10-19", "monthly_target": None},
        ]
        _resolve_other_member_rows(self._dataset(), rows, _Conn())
        self.assertEqual("余云涛", rows[0]["responsible_person"])
        self.assertEqual("省外销售部", rows[0]["department"])
        self.assertEqual(500000, rows[0]["monthly_target"])
        # 非 other 行与历史文本行原样放行
        self.assertEqual("余发兴", rows[1]["responsible_person"])
        self.assertEqual("李树军", rows[2]["responsible_person"])
        self.assertIsNone(rows[2]["monthly_target"])


class MartExtractServiceTests(unittest.TestCase):

    def _service(self, *, datasets=None, calendar_months=(), org_rows=()):
        self.repository = Mock()
        self.repository.read_dataset.return_value = [
            {"source_record_id": "r1", "region": "hangzhou"},
        ]
        # 默认「从未同步过通讯录」：返回空列表，跳过 dim_robot_member 步骤。
        self.repository.read_org_members.return_value = list(org_rows)
        self.repository.read_org_leader_payloads.return_value = []
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
        self.repository.upsert_dim_calendar.assert_not_called()
        self.repository.replace_dim_calendar.assert_not_called()

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_calendar_derives_workdays_from_rest_days(self, _lock, _txn):
        service = self._service(
            datasets=(),
            calendar_months=[(2026, 9, [6, 13, 19, 25, 26, 27], "local")],
        )
        service.extract()

        rows = self.repository.upsert_dim_calendar.call_args.args[0]
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
        rows = self.repository.upsert_dim_calendar.call_args.args[0]
        self.assertEqual(len(rows), 29)

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_calendar_merges_db_override_layer(self, _lock, _txn):
        """DB 裁决层（2026-10-10）：extract 重建时 dim_calendar_override
        逐日赢（合并点在写库前、digest 覆盖裁决行）。"""
        def _merge(connection, rows):
            return [
                (day, 0 if day.day == 5 else is_workday, source, note)
                for day, is_workday, source, note in rows
            ]

        with patch(
            "common.public_data.calendar_store.merge_overrides",
            side_effect=_merge,
        ) as merge:
            service = self._service(
                datasets=(),
                calendar_months=[(2026, 9, [], "local")],
            )
            service.extract()

        merge.assert_called_once()
        rows = self.repository.upsert_dim_calendar.call_args.args[0]
        by_day = {day.day: is_workday for day, is_workday, _, _ in rows}
        self.assertEqual(by_day[5], 0)

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

    def test_dept_names_prefer_catalog_table(self):
        # 目录表有行 → 用目录，不再查成员表（2026-10-24 部门目录采集）
        repo = self._repo(
            [{"dept_id": 1050135497, "dept_name": "线下运营中心"}])
        names = repo.read_org_dept_names()
        self.assertEqual({"1050135497": "线下运营中心"}, names)
        executed = self.raw.cursor_instance.executed
        self.assertEqual(1, len(executed))
        self.assertIn("dingtalk_org_dept", executed[0][0])

    def test_dept_names_fall_back_to_member_history_when_catalog_empty(self):
        # 目录表空 → 回退成员历史映射
        repo = self._repo([])
        names = repo.read_org_dept_names()
        self.assertEqual({}, names)
        executed = self.raw.cursor_instance.executed
        self.assertEqual(2, len(executed))
        self.assertIn("dingtalk_org_member", executed[1][0])

    def test_dept_names_fall_back_when_catalog_table_missing(self):
        # 目录表不存在（迁移未跑）→ 回退成员历史映射，不阻断
        class _BoomCursor(_RecordingCursor):
            def execute(self, sql, params=None):
                if "dingtalk_org_dept" in sql:
                    raise RuntimeError("1146")
                super().execute(sql, params)

        raw = _FakeConnection()
        raw.cursor_instance = _BoomCursor(
            [{"dept_id": "9", "dept_name": "历史部"}])
        repo = MartExtractRepository(raw, _FakeConnection())
        self.assertEqual({"9": "历史部"}, repo.read_org_dept_names())

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
        # job_number（2026-10-19 登录用户名真源）落在 union_id 之后。
        self.assertIn("`job_number`", insert_sql)
        self.assertEqual(sequence, [(
            "u1", "张三", None, None, "hangzhou", "1049728636", "杭中",
            1, _NOW, _RUN_ID,
        )])


class EcomPeopleExtractTests(unittest.TestCase):
    """电商人员业绩 + 店铺月目标投影并入 extract-mart（2026-09-23）。"""

    def _service(self, detail_rows, target_rows):
        self.repository = Mock()
        self.repository.read_org_members.return_value = []
        self.repository.read_org_leader_payloads.return_value = []
        self.repository.read_ecom_source.return_value = (
            list(detail_rows), list(target_rows),
        )
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

    _DETAIL = [{
        "channel": "天猫", "store_name": "旗舰店",
        "business_date": "2026-09-10", "sales_amount": 100.0,
        "responsible_person": '[{"name": "张三"}]',
    }]
    _TARGET = [{
        "store_name": "旗舰店", "channel": "天猫",
        "monthly_target": 300000.0,
        "responsible_person": '[{"name": "张三"}]',
    }]

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_fact_rows_and_store_targets_written(self, _lock, _txn):
        service = self._service(self._DETAIL, self._TARGET)
        with patch(
            "common.public_data.extract_ecom_people.upsert_rows",
            return_value={"inserted": 1, "updated": 0},
        ) as upsert, patch(
            "common.public_data.extract_ecom_people.replace_store_targets",
            return_value=1,
        ) as replace:
            result = service.extract()

        fact_rows = upsert.call_args.args[1]
        self.assertEqual(len(fact_rows), 1)
        self.assertEqual(fact_rows[0]["responsible_person"], "张三")
        self.assertEqual(fact_rows[0]["monthly_target"], 300000.0)
        replace.assert_called_once()
        self.assertEqual(replace.call_args.kwargs["sync_run_id"], _RUN_ID)

        kwargs = self.mart_repository.save_dataset_summary.call_args.kwargs
        self.assertEqual(kwargs["dataset_name"], "ecom_people")
        self.assertEqual(kwargs["records_read"], 2)
        self.assertEqual(kwargs["raw_records_written"], 2)
        self.assertEqual(len(result.datasets), 1)
        self.mart_repository.mark_completed.assert_called_once()

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_empty_raw_skips_instead_of_wiping(self, _lock, _txn):
        service = self._service([], [])
        with patch(
            "common.public_data.extract_ecom_people.upsert_rows"
        ) as upsert, patch(
            "common.public_data.extract_ecom_people.replace_store_targets"
        ) as replace:
            result = service.extract()
        upsert.assert_not_called()
        replace.assert_not_called()
        self.assertEqual(result.datasets, [])


class OrgMemberExtractTests(unittest.TestCase):

    def _service(self, org_rows):
        self.repository = Mock()
        self.repository.read_org_members.return_value = list(org_rows)
        # 领导投影（2026-10-24）：Mock 默认返回值是 truthy 的 Mock 会触发
        # 领导提取——显式置空，保持既有用例只关注成员维度。
        self.repository.read_org_leader_payloads.return_value = []
        self.repository.read_ecom_source.return_value = ([], [])
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

        self.repository.upsert_dim_robot_member.assert_called_once()
        rows = self.repository.upsert_dim_robot_member.call_args.args[0]
        self.assertEqual(rows[0]["region"], "hangzhou")

        # 成员摘要（领导投影在本用例被置空跳过，save_dataset_summary
        # 只被调一次）。
        self.mart_repository.save_dataset_summary.assert_called_once()
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

        self.repository.upsert_dim_robot_member.assert_not_called()
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


# ---------------------------------------------------------------------------
# Incremental extraction
# ---------------------------------------------------------------------------

class IncrementalRepositoryTests(unittest.TestCase):

    def _repo(self, rows=()):
        self.raw = _FakeConnection(rows)
        self.mart = _FakeConnection(rows)
        return MartExtractRepository(self.raw, self.mart)

    def test_read_dataset_applies_the_window_filter(self):
        repo = self._repo()
        repo.read_dataset(dataset_by_name("daily_report_offline"), since=_NOW)

        sql, params = self.raw.cursor_instance.executed[0]
        self.assertIn("WHERE `synced_at` >= %s", sql)
        self.assertEqual(params, (_NOW,))

    def test_read_dataset_without_a_window_scans_the_full_table(self):
        repo = self._repo()
        repo.read_dataset(dataset_by_name("daily_report_offline"))

        sql, params = self.raw.cursor_instance.executed[0]
        self.assertNotIn("WHERE", sql)
        self.assertIsNone(params)

    def test_last_extract_started_at_scopes_to_the_extract_line(self):
        repo = self._repo(rows=[{"started_at": _NOW}])
        self.assertEqual(
            repo.last_extract_started_at("daily_report_offline"), _NOW
        )

        sql, params = self.mart.cursor_instance.executed[0]
        self.assertIn("FROM `sync_runs`", sql)
        self.assertIn("JOIN `sync_dataset_summary`", sql)
        self.assertIn("ORDER BY r.`started_at` DESC", sql)
        self.assertEqual(params, (EXTRACT_SOURCE_NAME, "daily_report_offline"))

    def test_last_extract_started_at_returns_none_without_history(self):
        repo = self._repo(rows=[])
        self.assertIsNone(repo.last_extract_started_at("daily_report_offline"))

    def test_last_summary_digest_returns_the_latest_digest(self):
        repo = self._repo(rows=[{"record_id_digest": "d" * 64}])
        self.assertEqual(
            repo.last_summary_digest("daily_report_offline"), "d" * 64
        )

        sql, params = self.mart.cursor_instance.executed[0]
        self.assertIn("ORDER BY `completed_at` DESC", sql)
        self.assertEqual(params, (EXTRACT_SOURCE_NAME, "daily_report_offline"))

    def test_upsert_dim_calendar_upserts_then_prunes_stale_dates(self):
        repo = self._repo()
        repo.upsert_dim_calendar(
            [(date(2026, 9, 5), 1, "local", None)],
            sync_run_id=_RUN_ID,
            synced_at=_NOW,
        )

        insert_sql, sequence = self.mart.cursor_instance.executemany_calls[0]
        self.assertIn("INSERT INTO `dim_calendar`", insert_sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", insert_sql)
        self.assertEqual(
            sequence, [(date(2026, 9, 5), 1, "local", None, _NOW, _RUN_ID)]
        )
        delete_sql, delete_params = self.mart.cursor_instance.executed[0]
        self.assertIn(f"DELETE FROM `{DIM_CALENDAR}`", delete_sql)
        self.assertIn("`business_date` NOT IN", delete_sql)
        self.assertEqual(delete_params, (date(2026, 9, 5),))

    def test_upsert_dim_calendar_prunes_rows_missing_from_a_shrunk_seed(self):
        """源集缩小方向：种子缩容后，残留日期必须被 NOT IN 剪枝删除。"""
        repo = self._repo()
        # 第一次写入 9/5 与 9/6；第二次种子只剩 9/6。
        repo.upsert_dim_calendar(
            [(date(2026, 9, 5), 1, "local", None),
             (date(2026, 9, 6), 0, "local", None)],
            sync_run_id=_RUN_ID,
            synced_at=_NOW,
        )
        repo.upsert_dim_calendar(
            [(date(2026, 9, 6), 0, "local", None)],
            sync_run_id=_RUN_ID,
            synced_at=_NOW,
        )

        delete_sql, delete_params = self.mart.cursor_instance.executed[-1]
        self.assertIn("`business_date` NOT IN", delete_sql)
        # 剪枝集合只含新种子：不在其中的 9/5 会被删除，目标表无残留。
        self.assertEqual(delete_params, (date(2026, 9, 6),))

    def test_upsert_dim_robot_member_prunes_members_missing_from_a_shrunk_snapshot(self):
        """源集缩小方向：快照缩容后，离职成员必须被 NOT IN 剪枝删除。"""
        repo = self._repo()
        repo.upsert_dim_robot_member(
            [
                {"user_id": "u1", "name": "张三", "region": "hangzhou"},
                {"user_id": "u2", "name": "李四", "region": "hangzhou"},
            ],
            sync_run_id=_RUN_ID,
            synced_at=_NOW,
        )
        repo.upsert_dim_robot_member(
            [{"user_id": "u2", "name": "李四", "region": "hangzhou"}],
            sync_run_id=_RUN_ID,
            synced_at=_NOW,
        )

        delete_sql, delete_params = self.mart.cursor_instance.executed[-1]
        self.assertIn("`user_id` NOT IN", delete_sql)
        # 剪枝集合只含在职成员：u1 会被删除，目标表无残留。
        self.assertEqual(delete_params, ("u2",))

    def test_upsert_dim_calendar_with_empty_rows_clears_the_dim(self):
        repo = self._repo()
        repo.upsert_dim_calendar([], sync_run_id=_RUN_ID, synced_at=_NOW)

        sql, _ = self.mart.cursor_instance.executed[0]
        self.assertEqual(sql, f"DELETE FROM `{DIM_CALENDAR}`")
        self.assertEqual(self.mart.cursor_instance.executemany_calls, [])

    def test_upsert_dim_robot_member_upserts_then_prunes_departed(self):
        repo = self._repo()
        repo.upsert_dim_robot_member(
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

        insert_sql, sequence = self.mart.cursor_instance.executemany_calls[0]
        self.assertIn("INSERT INTO `dim_robot_member`", insert_sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", insert_sql)
        self.assertIn("`job_number` = VALUES(`job_number`)", insert_sql)
        self.assertEqual(sequence, [(
            "u1", "张三", None, None, "hangzhou", "1049728636", "杭中",
            1, _NOW, _RUN_ID,
        )])
        delete_sql, delete_params = self.mart.cursor_instance.executed[0]
        self.assertIn(f"DELETE FROM `{DIM_ROBOT_MEMBER}`", delete_sql)
        self.assertIn("`user_id` NOT IN", delete_sql)
        self.assertEqual(delete_params, ("u1",))

    def test_save_skipped_summary_writes_zero_written_and_the_flag(self):
        repo = self._repo()
        repo.save_skipped_summary(
            sync_run_id=_RUN_ID,
            dataset_name="daily_report_offline",
            records_read=3,
            record_id_digest="d" * 64,
            completed_at=_NOW,
        )

        sql, params = self.mart.cursor_instance.executed[0]
        self.assertIn("`skipped`", sql)
        self.assertEqual(
            params,
            (_RUN_ID, EXTRACT_SOURCE_NAME, "daily_report_offline",
             3, 0, "d" * 64, _NOW, 1),
        )


class IncrementalExtractTests(unittest.TestCase):

    def _service(
        self, *, rows, since=None, previous_digest=None,
        datasets=None, has_skipped=True, full_rebuild=False,
    ):
        self.repository = Mock()
        self.repository.read_dataset.return_value = list(rows)
        self.repository.read_org_members.return_value = []
        self.repository.read_org_leader_payloads.return_value = []
        self.repository.read_ecom_source.return_value = ([], [])
        self.repository.last_extract_started_at.return_value = since
        self.repository.last_summary_digest.return_value = previous_digest
        self.repository.has_skipped_column.return_value = has_skipped
        self.mart_repository = Mock()
        self.mart_connection = _FakeConnection()
        return MartExtractService(
            repository=self.repository,
            mart_repository=self.mart_repository,
            mart_connection=self.mart_connection,
            now=lambda: _NOW,
            new_run_id=lambda: _RUN_ID,
            datasets=(
                [dataset_by_name("daily_report_offline")]
                if datasets is None else datasets
            ),
            full_rebuild=full_rebuild,
        )

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_unchanged_digest_skips_the_write_and_marks_the_summary(
        self, _lock, _txn,
    ):
        """digest 命中历史摘要：不写 mart，摘要 records_written=0/skipped。"""
        rows = [{"source_record_id": "r1", "region": "hangzhou"}]
        digest = MartExtractService._compute_digest(["r1"])
        service = self._service(rows=rows, since=None, previous_digest=digest)

        result = service.extract()

        self.repository.upsert_fact.assert_not_called()
        self.mart_repository.save_dataset_summary.assert_not_called()
        kwargs = self.repository.save_skipped_summary.call_args.kwargs
        self.assertEqual(kwargs["dataset_name"], "daily_report_offline")
        self.assertEqual(kwargs["records_read"], 1)
        self.assertEqual(kwargs["record_id_digest"], digest)
        self.assertTrue(result.datasets[0]["skipped"])
        self.assertEqual(result.datasets[0]["raw_records_written"], 0)
        self.mart_repository.mark_completed.assert_called_once()

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_missing_skipped_column_falls_back_to_a_plain_summary(
        self, _lock, _txn,
    ):
        """skipped 列未迁移：降级写普通摘要（fail-open），run 不失败。"""
        rows = [{"source_record_id": "r1", "region": "hangzhou"}]
        digest = MartExtractService._compute_digest(["r1"])
        service = self._service(
            rows=rows, since=None, previous_digest=digest, has_skipped=False,
        )

        with self.assertLogs(
            "common.public_data.extract_mart", level="WARNING"
        ) as captured:
            service.extract()

        self.repository.save_skipped_summary.assert_not_called()
        kwargs = self.mart_repository.save_dataset_summary.call_args.kwargs
        self.assertEqual(kwargs["raw_records_written"], 0)
        self.assertTrue(any("skipped" in line for line in captured.output))

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_window_filter_passes_the_watermark_to_the_read(
        self, _lock, _txn,
    ):
        service = self._service(
            rows=[{"source_record_id": "r1", "region": "hangzhou"}],
            since=_NOW,
        )

        service.extract()

        self.assertEqual(
            self.repository.read_dataset.call_args.kwargs["since"], _NOW
        )
        self.repository.upsert_fact.assert_called_once()

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_empty_window_skips_the_write(self, _lock, _txn):
        service = self._service(rows=[], since=_NOW)

        result = service.extract()

        self.repository.upsert_fact.assert_not_called()
        kwargs = self.repository.save_skipped_summary.call_args.kwargs
        self.assertEqual(kwargs["records_read"], 0)
        self.assertTrue(result.datasets[0]["skipped"])

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_dataset_without_a_watermark_column_degrades_with_an_audit_trail(
        self, _lock, _txn,
    ):
        """无水位列的数据集：显式降级全量 + WARNING 记录原因，不静默。"""
        unknown = ExtractDataset(
            dataset="mystery",
            source_table="mystery_raw",
            target_table="fact_mystery",
            columns=(("region", "region"),),
        )
        service = self._service(
            rows=[{"source_record_id": "r1", "region": "hangzhou"}],
            since=_NOW,
            datasets=[unknown],
        )

        with self.assertLogs(
            "common.public_data.extract_mart", level="WARNING"
        ) as captured:
            service.extract()

        self.assertIsNone(
            self.repository.read_dataset.call_args.kwargs["since"]
        )
        self.repository.upsert_fact.assert_called_once()
        self.assertTrue(any("降级" in line for line in captured.output))

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_full_rebuild_reads_everything_and_never_skips(self, _lock, _txn):
        """一键回退：full_rebuild 关闭窗口与 digest 跳过。"""
        rows = [{"source_record_id": "r1", "region": "hangzhou"}]
        digest = MartExtractService._compute_digest(["r1"])
        service = self._service(
            rows=rows, previous_digest=digest, full_rebuild=True,
        )

        service.extract()

        self.assertIsNone(
            self.repository.read_dataset.call_args.kwargs["since"]
        )
        self.repository.upsert_fact.assert_called_once()
        self.repository.save_skipped_summary.assert_not_called()


class IncrementalCalendarTests(unittest.TestCase):

    def _service(self, *, previous_digest=None, full_rebuild=False):
        self.repository = Mock()
        self.repository.read_org_members.return_value = []
        self.repository.read_org_leader_payloads.return_value = []
        self.repository.read_ecom_source.return_value = ([], [])
        self.repository.last_summary_digest.return_value = previous_digest
        self.repository.has_skipped_column.return_value = True
        self.mart_repository = Mock()
        self.mart_connection = _FakeConnection()
        return MartExtractService(
            repository=self.repository,
            mart_repository=self.mart_repository,
            mart_connection=self.mart_connection,
            now=lambda: _NOW,
            new_run_id=lambda: _RUN_ID,
            datasets=(),
            calendar_months=[(2024, 2, [], "local")],
            full_rebuild=full_rebuild,
        )

    @staticmethod
    def _expected_digest():
        return MartExtractService._content_digest(
            f"{day.isoformat()}|1|local|" for day in month_days(2024, 2)
        )

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_unchanged_calendar_skips_the_write(self, _lock, _txn):
        service = self._service(previous_digest=self._expected_digest())

        result = service.extract()

        self.repository.upsert_dim_calendar.assert_not_called()
        self.repository.replace_dim_calendar.assert_not_called()
        kwargs = self.repository.save_skipped_summary.call_args.kwargs
        self.assertEqual(kwargs["dataset_name"], CALENDAR_DATASET)
        self.assertEqual(kwargs["records_read"], 29)
        self.assertTrue(result.datasets[0]["skipped"])

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_changed_calendar_is_upserted(self, _lock, _txn):
        service = self._service(previous_digest="0" * 64)

        service.extract()

        self.repository.upsert_dim_calendar.assert_called_once()
        self.repository.replace_dim_calendar.assert_not_called()

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_full_rebuild_keeps_the_replace_semantics(self, _lock, _txn):
        service = self._service(
            previous_digest=self._expected_digest(), full_rebuild=True,
        )

        service.extract()

        self.repository.replace_dim_calendar.assert_called_once()
        self.repository.upsert_dim_calendar.assert_not_called()


class IncrementalOrgMemberTests(unittest.TestCase):

    _ROWS = [{
        "user_id": "u1",
        "name": "张三",
        "region": "hangzhou",
        "dept_id": "1049728636",
        "dept_name": "杭中",
    }]

    def _service(self, *, previous_digest=None):
        self.repository = Mock()
        self.repository.read_org_members.return_value = list(self._ROWS)
        # 领导投影（2026-10-24）置空：本类只关注成员维度增量语义。
        self.repository.read_org_leader_payloads.return_value = []
        self.repository.read_ecom_source.return_value = ([], [])
        self.repository.last_summary_digest.return_value = previous_digest
        self.repository.has_skipped_column.return_value = True
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
    def test_unchanged_org_snapshot_skips_the_write(self, _lock, _txn):
        # digest 形态含 job_number（2026-10-19 登录用户名真源）：末段为
        # 空即该员工在通讯录里没有工号。
        digest = MartExtractService._content_digest(
            ["u1|张三|hangzhou|1049728636|杭中||"]
        )
        service = self._service(previous_digest=digest)

        result = service.extract()

        self.repository.upsert_dim_robot_member.assert_not_called()
        kwargs = self.repository.save_skipped_summary.call_args.kwargs
        self.assertEqual(kwargs["dataset_name"], DIM_ROBOT_MEMBER)
        self.assertTrue(result.datasets[0]["skipped"])

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_changed_org_snapshot_is_upserted(self, _lock, _txn):
        service = self._service(previous_digest="0" * 64)

        service.extract()

        self.repository.upsert_dim_robot_member.assert_called_once()
        self.repository.replace_dim_robot_member.assert_not_called()


class DeptLeaderExtractTests(unittest.TestCase):
    """dim_dept_leader 投影（2026-10-24）：部门 × 领导关系。"""

    def _service(self, leader_payloads, *, previous_digest=None,
                 dept_names=None):
        self.repository = Mock()
        self.repository.read_org_members.return_value = []
        self.repository.read_org_leader_payloads.return_value = list(
            leader_payloads)
        # 部门名目录（全历史快照映射）；默认空。
        self.repository.read_org_dept_names.return_value = dict(
            dept_names or {})
        self.repository.read_ecom_source.return_value = ([], [])
        self.repository.last_summary_digest.return_value = previous_digest
        self.repository.has_skipped_column.return_value = True
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

    @staticmethod
    def _payload(user_id, name, dept_id, dept_name, leader_depts):
        return {
            "user_id": user_id,
            "name": name,
            "dept_id": dept_id,
            "dept_name": dept_name,
            "payload_json": json.dumps({
                "leader_in_dept": [
                    {"dept_id": d, "leader": True} for d in leader_depts
                ] + [{"dept_id": "999", "leader": False}],  # 非领导不投影
            }),
        }

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_leaders_projected_multi_dept(self, _lock, _txn):
        # 一人带两个部门 → 两行；部门名来自全历史快照目录（领导管的
        # 纯管理节点部门——无直属成员——也能映射到名字）
        service = self._service(
            [
                self._payload("u1", "谢坚钰", "1049919540", "总经办",
                              ["1049919540", "1050135497"]),
                self._payload("u2", "许辉", "1050193465", "电商组",
                              ["1050193465"]),
            ],
            dept_names={"1049919540": "总经办", "1050135497": "线下运营中心",
                        "1050193465": "电商组"},
        )
        result = service.extract()

        self.repository.replace_dim_dept_leaders.assert_called_once()
        rows = self.repository.replace_dim_dept_leaders.call_args.args[0]
        self.assertEqual(3, len(rows))
        by_key = {(r["dept_id"], r["user_id"]): r for r in rows}
        self.assertEqual(
            "线下运营中心",
            by_key[("1050135497", "u1")]["dept_name"])
        self.assertEqual("总经办", by_key[("1049919540", "u1")]["dept_name"])
        self.assertEqual("电商组", by_key[("1050193465", "u2")]["dept_name"])

        summaries = [
            call.kwargs["dataset_name"]
            for call in self.mart_repository.save_dataset_summary.mock_calls
            if "dataset_name" in call.kwargs
        ]
        self.assertIn("dim_dept_leader", summaries)
        leader_summary = next(
            d for d in result.datasets if d["dataset"] == "dim_dept_leader")
        self.assertEqual(3, leader_summary["records_read"])

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_empty_raw_skips_leaders_too(self, _lock, _txn):
        service = self._service([])
        service.extract()
        self.repository.replace_dim_dept_leaders.assert_not_called()

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_no_leaders_writes_empty_and_summarises(self, _lock, _txn):
        # 快照有人但无人是领导 → 表清空（卸任语义）+ 摘要照常
        service = self._service([
            {"user_id": "u1", "name": "张三", "dept_id": "1049919540",
             "dept_name": "总经办",
             "payload_json": json.dumps({"leader_in_dept": []})},
        ])
        result = service.extract()
        self.repository.replace_dim_dept_leaders.assert_called_once()
        rows = self.repository.replace_dim_dept_leaders.call_args.args[0]
        self.assertEqual([], rows)
        leader_summary = next(
            d for d in result.datasets if d["dataset"] == "dim_dept_leader")
        self.assertEqual(0, leader_summary["records_read"])

    @patch("common.public_data.extract_mart.transaction", return_value=_Ctx())
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_unchanged_leader_snapshot_skips_write(self, _lock, _txn):
        payloads = [self._payload("u1", "谢坚钰", "1049919540", "总经办",
                                  ["1049919540"])]
        digest = MartExtractService._content_digest(["1049919540|u1|总经办"])
        service = self._service(payloads, previous_digest=digest,
                                dept_names={"1049919540": "总经办"})
        service.extract()
        self.repository.replace_dim_dept_leaders.assert_not_called()


class WdtQualityReplayTests(unittest.TestCase):

    @patch("common.public_data.extract_order_line.project_order_lines")
    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_dry_run_forwards_precheck_raw_batches(self, _lock, project):
        repository = Mock()
        mart_repository = Mock()
        service = MartExtractService(
            repository=repository,
            mart_repository=mart_repository,
            mart_connection=_FakeConnection(),
            now=lambda: _NOW,
            new_run_id=Mock(return_value=_RUN_ID),
            datasets=[dataset_by_name("wdt_order_line_fact")],
            calendar_months=(),
        )
        project.return_value = {
            "records_read": 1, "records_new": 0,
            "_replay_diff": {
                "before_count": 1, "window_before": 1,
                "window_after": 1, "after_count": 1, "records_written": 0,
            },
        }
        raw_batches = [[{"source_record_id": "raw-1", "payload_json": "{}"}]]

        result = service.replay_dataset(
            "wdt_order_line_fact",
            window_start=date(2026, 10, 26), window_end=date(2026, 10, 26),
            dry_run=True, rules=[], overrides={}, raw_batches=raw_batches,
        )

        self.assertIs(raw_batches, project.call_args.kwargs["raw_batches"])
        self.assertEqual(0, result["records_written"])
        mart_repository.start_run.assert_not_called()
        repository.read_wdt_trade_batches.assert_not_called()


class ChannelQualityReplayTests(unittest.TestCase):

    def _service(self):
        self.repository = Mock()
        self.repository.read_dataset.return_value = []
        self.mart_repository = Mock()
        self.mart_connection = _FakeConnection()
        self.new_run_id = Mock(return_value=_RUN_ID)
        return MartExtractService(
            repository=self.repository,
            mart_repository=self.mart_repository,
            mart_connection=self.mart_connection,
            now=lambda: _NOW,
            new_run_id=self.new_run_id,
            datasets=[dataset_by_name("channel_daily_sales")],
            calendar_months=(),
        )

    @patch("common.public_data.extract_mart.named_lock", return_value=_Ctx())
    def test_dry_run_has_no_sync_run_or_business_writes(self, _lock):
        service = self._service()
        self.repository.read_dataset.return_value = [{
            "source_record_id": "ai-1", "channel": "抖音", "store_name": "甲",
            "business_date": date(2026, 10, 26), "sales_amount": 10,
        }]
        self.repository.window_row_counts.return_value = {
            "before_count": 5, "window_before": 2,
        }

        result = service.replay_dataset(
            "channel_daily_sales", window_start=date(2026, 10, 26),
            window_end=date(2026, 10, 26), dry_run=True, rules=[], overrides={},
        )

        self.assertIsNone(result["sync_run_id"])
        self.assertEqual(result["records_read"], 1)
        self.assertEqual(result["records_written"], 0)
        self.assertEqual(result["diff"]["window_after"], 1)
        self.assertEqual(result["diff"]["after_count"], 4)
        self.new_run_id.assert_not_called()
        self.mart_repository.start_run.assert_not_called()
        self.mart_repository.save_dataset_summary.assert_not_called()
        self.mart_repository.mark_raw_committed.assert_not_called()
        self.mart_repository.mark_completed.assert_not_called()
        self.repository.upsert_fact.assert_not_called()
        self.repository.replace_table_window_batches.assert_not_called()
        executed = [sql for sql, _ in self.mart_connection.cursor_instance.executed]
        self.assertFalse(any(sql.startswith("RENAME TABLE") for sql in executed))

    def test_dry_run_rejects_missing_or_reversed_window_before_reading(self):
        service = self._service()
        windows = (
            (None, date(2026, 10, 26)),
            (date(2026, 10, 26), None),
            (date(2026, 10, 27), date(2026, 10, 26)),
        )
        for window_start, window_end in windows:
            with self.subTest(window_start=window_start, window_end=window_end):
                with self.assertRaisesRegex(MartExtractError, "invalid business window"):
                    service.replay_dataset(
                        "channel_daily_sales", window_start=window_start,
                        window_end=window_end, dry_run=True, rules=[], overrides={},
                    )
        self.repository.read_dataset.assert_not_called()
        self.new_run_id.assert_not_called()
        self.mart_repository.start_run.assert_not_called()

    def test_formal_replay_filters_after_robot_merge_and_returns_real_counts(self):
        service = self._service()
        inside = date(2026, 10, 26)
        outside = date(2026, 10, 25)
        self.repository.read_dataset.return_value = [
            {"source_record_id": "ai-zero", "channel": "抖音",
             "store_name": "零元店", "business_date": inside, "sales_amount": 0},
            {"source_record_id": "ai-negative", "channel": "京东",
             "store_name": "退货店", "business_date": inside, "sales_amount": -12},
            {"source_record_id": "ai-outside", "channel": "天猫",
             "store_name": "窗口外", "business_date": outside, "sales_amount": 30},
        ]
        inbox = {
            (inside, "拼多多", "机器人店"): {
                "id": 30, "channel": "拼多多", "store_name": "机器人店",
                "business_date": inside, "sales_amount": 88,
            },
            (outside, "拼多多", "窗口外机器人"): {
                "id": 31, "channel": "拼多多", "store_name": "窗口外机器人",
                "business_date": outside, "sales_amount": 99,
            },
        }
        published = []

        def replace_window(_table, _columns, batches, **_kwargs):
            published.extend(row for batch in batches for row in batch)
            written = len(published)
            return {
                "before_count": 8, "window_before": 2,
                "window_after": written, "after_count": 6 + written,
                "records_written": written,
            }

        self.repository.replace_table_window_batches.side_effect = replace_window
        with patch(
            "common.public_data.extract_mart.named_lock", return_value=_Ctx(),
        ), patch(
            "common.public_data.channel_robot_inbox.fetch_latest_inbox",
            return_value=inbox,
        ), patch(
            "common.public_data.channel_robot_inbox.fetch_raw_business_keys",
            return_value={},
        ):
            result = service.replay_dataset(
                "channel_daily_sales", window_start=inside, window_end=inside,
                dry_run=False, rules=[], overrides={},
            )

        by_id = {row["source_record_id"]: row for row in published}
        self.assertEqual(set(by_id), {"ai-zero", "ai-negative", "robot:30"})
        self.assertEqual(by_id["ai-zero"]["sales_amount"], 0)
        self.assertEqual(by_id["ai-negative"]["sales_amount"], -12)
        self.assertEqual(by_id["robot:30"]["sales_amount"], 88)
        self.assertEqual(result["sync_run_id"], _RUN_ID)
        self.assertEqual(result["records_read"], 3)
        self.assertEqual(result["records_written"], 3)
        self.mart_repository.start_run.assert_called_once()
        self.mart_repository.mark_raw_committed.assert_called_once_with(_RUN_ID)
        self.assertEqual(
            self.mart_repository.save_dataset_summary.call_args.kwargs[
                "raw_records_written"
            ],
            3,
        )
        self.mart_repository.mark_completed.assert_called_once()
        self.mart_repository.mark_failed.assert_not_called()

    def test_summary_failure_terminates_as_projection_pending_not_failed(self):
        service = self._service()
        self.repository.read_dataset.return_value = [{
            "source_record_id": "ai-1", "channel": "抖音", "store_name": "甲",
            "business_date": date(2026, 10, 26), "sales_amount": 1,
        }]
        self.repository.replace_table_window_batches.return_value = {
            "before_count": 1, "window_before": 1, "window_after": 1,
            "after_count": 1, "records_written": 1,
        }
        self.mart_repository.save_dataset_summary.side_effect = RuntimeError(
            "summary db",
        )

        with patch(
            "common.public_data.extract_mart.named_lock", return_value=_Ctx(),
        ):
            with self.assertRaisesRegex(RuntimeError, "summary db"):
                service.replay_dataset(
                    "channel_daily_sales", window_start=date(2026, 10, 26),
                    window_end=date(2026, 10, 26), dry_run=False,
                    rules=[], overrides={},
                )

        self.mart_repository.mark_raw_committed.assert_called_once_with(_RUN_ID)
        self.mart_repository.mark_projection_pending.assert_called_once_with(
            sync_run_id=_RUN_ID, failure_code="projection_failed", finished_at=_NOW,
        )
        self.mart_repository.mark_failed.assert_not_called()
        self.mart_repository.mark_completed.assert_not_called()
        self.assertEqual(self.mart_connection.rollbacks, 0)


if __name__ == "__main__":
    unittest.main()
