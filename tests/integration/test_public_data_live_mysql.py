import os
import unittest
from datetime import datetime, timezone

from common.public_data.db import LockUnavailable, connect, named_lock, transaction
from common.public_data.live_migrations import apply_live_migrations
from common.public_data.live_sync import LiveSyncService
from common.public_data.manifest import (
    DingTalkSheet,
    FieldMapping,
    SourceManifest,
    WdtDataset,
)
from common.public_data.mart_repository import MartRepository
from common.public_data.raw_repository import DingTalkRawRepository, WdtRawRepository
from common.public_data.settings import Settings


# ---------------------------------------------------------------------------
# Fake gateways for LiveSyncService tests
# ---------------------------------------------------------------------------

class _FakeDingTalkGateway:
    def __init__(self, records=None):
        self._records = records or []

    def validate_sheet(self, sheet):
        pass

    def read_records(self, sheet):
        return list(self._records)


class _FakeWdtGateway:
    def __init__(self, records=None):
        self._records = records or []

    def read_dataset(self, dataset):
        return list(self._records)


class _BrokenGateway:
    def __getattr__(self, name):
        raise RuntimeError("gateway intentionally unavailable")


class _Connections:
    def __init__(self, dingtalk, wdt, mart):
        self.dingtalk = dingtalk
        self.wdt = wdt
        self.mart = mart


class _FailingMartRepository:
    """Wraps a real MartRepository; raises on the first save_dataset_summary."""

    def __init__(self, real_repo):
        self._real = real_repo
        self._summary_calls = 0

    def __getattr__(self, name):
        return getattr(self._real, name)

    def save_dataset_summary(self, **kwargs):
        self._summary_calls += 1
        if self._summary_calls == 1:
            raise RuntimeError("simulated projection failure")
        return self._real.save_dataset_summary(**kwargs)


# ---------------------------------------------------------------------------
# Shared manifest / data factories
# ---------------------------------------------------------------------------

def _build_manifest():
    sheet = DingTalkSheet(
        base_id="test-base",
        sheet_id="test-sheet",
        sheet_name="Test Sheet",
        dataset="test-commission",
        target_table="fin_store_commission",
        max_pages=1,
        fields=(
            FieldMapping(
                source_name="\u95e8\u5e97",
                column="store_name",
                source_type="text",
            ),
            FieldMapping(
                source_name="\u4f63\u91d1",
                column="amount_2026_01",
                source_type="currency",
            ),
        ),
    )
    wdt_dataset = WdtDataset(
        dataset="test-trade",
        method="sales.TradeQuery.queryWithDetail",
        target_table="wdt_records",
        record_id_path="order.order_no",
        page_size=40,
        max_pages=1,
        window_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        window_end=datetime(2026, 1, 1, 0, 50, tzinfo=timezone.utc),
        max_window_minutes=50,
        params={},
    )
    return SourceManifest(
        dingtalk_sheets=(sheet,),
        wdt_datasets=(wdt_dataset,),
        sha256="0" * 64,
    )


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

#: 本文件内所有固定 run_id（LiveSyncService 用例的 new_run_id）。
#: MySQL 卷持久化时，残留行会让二次运行在 ``start_run`` 撞上主键冲突；
#: setUp/tearDown 对这批 id 做幂等清理，保证测试可重复运行。
_FIXED_RUN_IDS = (
    "isolation-test-run-0001",
    "idempotent-full-run-a",
    "idempotent-full-run-b",
    "projection-recovery-run-0001",
    "should-not-be-used",
)


def _purge_fixed_sync_runs(connection):
    """删除固定 run_id 的摘要行与 run 行（先子表后父表），并提交。"""

    placeholders = ", ".join(["%s"] * len(_FIXED_RUN_IDS))
    cursor = connection.cursor()
    try:
        for table in ("sync_dataset_summary", "sync_runs"):
            cursor.execute(
                f"DELETE FROM `{table}` "
                f"WHERE `sync_run_id` IN ({placeholders})",
                _FIXED_RUN_IDS,
            )
    finally:
        cursor.close()
    connection.commit()


@unittest.skipUnless(
    os.environ.get("INTEGRATION_TEST_RUNNER") == "1",
    "Requires the Docker Compose MySQL integration environment.",
)
class PublicDataLiveMySQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = Settings.from_environment()
        cls.dingtalk_connection = connect(cls.settings.dingtalk_database)
        cls.wdt_connection = connect(cls.settings.wdt_database)
        cls.mart_connection = connect(cls.settings.mart_database)
        apply_live_migrations(
            cls.dingtalk_connection,
            cls.wdt_connection,
            cls.mart_connection,
        )

    @classmethod
    def tearDownClass(cls):
        cls.dingtalk_connection.close()
        cls.wdt_connection.close()
        cls.mart_connection.close()

    def setUp(self):
        # 预清：上一轮残留（含中途崩溃的卷）不影响本次运行。
        _purge_fixed_sync_runs(self.mart_connection)

    def tearDown(self):
        # 后清：不留固定 run_id 行，持久卷上可反复运行。
        _purge_fixed_sync_runs(self.mart_connection)

    # -- helpers -------------------------------------------------------------

    def _table_names(self, connection):
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT TABLE_NAME FROM information_schema.tables "
                "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
            )
            return [row["TABLE_NAME"] for row in cursor.fetchall()]
        finally:
            cursor.close()

    def _column_names(self, connection, table_name):
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.columns "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
                "ORDER BY COLUMN_NAME",
                (table_name,),
            )
            return {row["COLUMN_NAME"] for row in cursor.fetchall()}
        finally:
            cursor.close()

    def _count_rows(self, connection, sql, params=()):
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            return cursor.fetchone()["cnt"]
        finally:
            cursor.close()

    def _query_values(self, connection, sql, params=()):
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            return [row[next(iter(row.keys()))] for row in cursor.fetchall()]
        finally:
            cursor.close()

    # -- existing schema-isolation tests -------------------------------------

    def test_finance_tables_exist_only_in_raw_dingtalk(self):
        tables = self._table_names(self.dingtalk_connection)
        self.assertIn("fin_store_commission", tables)
        self.assertIn("dingtalk_schema_snapshots", tables)

        wdt_tables = self._table_names(self.wdt_connection)
        self.assertNotIn("fin_store_commission", wdt_tables)
        self.assertNotIn("dingtalk_schema_snapshots", wdt_tables)

        mart_tables = self._table_names(self.mart_connection)
        self.assertNotIn("fin_store_commission", mart_tables)
        self.assertNotIn("dingtalk_schema_snapshots", mart_tables)

    def test_wdt_records_exists_only_in_raw_wdt(self):
        tables = self._table_names(self.wdt_connection)
        self.assertIn("wdt_records", tables)

        dingtalk_tables = self._table_names(self.dingtalk_connection)
        self.assertNotIn("wdt_records", dingtalk_tables)

        mart_tables = self._table_names(self.mart_connection)
        self.assertNotIn("wdt_records", mart_tables)

    def test_mart_tables_exist_only_in_mart_ops(self):
        tables = self._table_names(self.mart_connection)
        self.assertIn("sync_runs", tables)
        self.assertIn("sync_dataset_summary", tables)

        dingtalk_tables = self._table_names(self.dingtalk_connection)
        self.assertNotIn("sync_runs", dingtalk_tables)
        self.assertNotIn("sync_dataset_summary", dingtalk_tables)

        wdt_tables = self._table_names(self.wdt_connection)
        self.assertNotIn("sync_runs", wdt_tables)
        self.assertNotIn("sync_dataset_summary", wdt_tables)

    def test_migration_tracking_records_checksums(self):
        for connection in (
            self.dingtalk_connection,
            self.wdt_connection,
            self.mart_connection,
        ):
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SELECT version, checksum FROM pd_live_schema_migration"
                )
                rows = cursor.fetchall()
            finally:
                cursor.close()

            self.assertGreater(len(rows), 0)
            for row in rows:
                self.assertEqual(64, len(row["checksum"]))

    # -- new E2E integration tests -------------------------------------------

    def test_dingtalk_upsert_idempotency(self):
        """Upserting the same DingTalk record twice yields one row with the
        newest sync_run_id."""
        records = [
            {
                "id": "rec-001",
                "fields": {"\u95e8\u5e97": "A01", "\u4f63\u91d1": "100.50"},
            }
        ]
        field_mapping = {"\u95e8\u5e97": "store_name", "\u4f63\u91d1": "amount_2026_01"}
        now = datetime.now(timezone.utc)
        repo = DingTalkRawRepository(self.dingtalk_connection)

        repo.upsert_records(
            "fin_store_commission",
            records,
            field_mapping=field_mapping,
            sync_run_id="idempotency-run-1",
            synced_at=now,
        )
        self.dingtalk_connection.commit()

        repo.upsert_records(
            "fin_store_commission",
            records,
            field_mapping=field_mapping,
            sync_run_id="idempotency-run-2",
            synced_at=now,
        )
        self.dingtalk_connection.commit()

        cnt = self._count_rows(
            self.dingtalk_connection,
            "SELECT COUNT(*) AS cnt FROM fin_store_commission "
            "WHERE dingtalk_record_id = %s",
            ("rec-001",),
        )
        self.assertEqual(1, cnt)

        rows = self._query_values(
            self.dingtalk_connection,
            "SELECT sync_run_id FROM fin_store_commission "
            "WHERE dingtalk_record_id = %s",
            ("rec-001",),
        )
        self.assertEqual("idempotency-run-2", rows[0])

    def test_wdt_transaction_rollback(self):
        """An exception inside the transaction context rolls back the WDT
        INSERT so no row is persisted."""
        records = [({"order_id": "wdt-001", "amount": "99.99"}, "stable-wdt-001")]
        try:
            with transaction(self.wdt_connection):
                repo = WdtRawRepository(self.wdt_connection)
                repo.upsert_records(
                    "sales.TradeQuery.queryWithDetail",
                    records,
                    window_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    window_end=datetime(2026, 1, 1, 0, 50, tzinfo=timezone.utc),
                    sync_run_id="rollback-test-run",
                    synced_at=datetime.now(timezone.utc),
                )
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass

        cnt = self._count_rows(
            self.wdt_connection,
            "SELECT COUNT(*) AS cnt FROM wdt_records "
            "WHERE source_record_id = %s",
            ("stable-wdt-001",),
        )
        self.assertEqual(0, cnt)

    def test_named_lock_exclusion_on_raw_dingtalk(self):
        """A named lock acquired on one connection blocks a second connection
        until it is released."""
        lock_name = "public-data:dingtalk:finance_store_commission"
        conn_a = connect(self.settings.dingtalk_database)
        conn_b = connect(self.settings.dingtalk_database)
        try:
            with named_lock(conn_a, lock_name, timeout_seconds=0):
                with self.assertRaises(LockUnavailable):
                    with named_lock(conn_b, lock_name, timeout_seconds=0):
                        pass
            # After release, conn_b can acquire.
            with named_lock(conn_b, lock_name, timeout_seconds=0):
                pass
        finally:
            conn_a.close()
            conn_b.close()

    def test_database_isolation_via_live_sync(self):
        """Running a full sync through LiveSyncService does not leak tables
        or columns across databases."""
        manifest = _build_manifest()
        fake_dingtalk = _FakeDingTalkGateway(
            records=[
                {
                    "id": "iso-rec-001",
                    "fields": {"\u95e8\u5e97": "X1", "\u4f63\u91d1": "10.00"},
                }
            ]
        )
        fake_wdt = _FakeWdtGateway(
            records=[({"order_id": "iso-wdt-001"}, "iso-stable-001")]
        )
        dingtalk_repo = DingTalkRawRepository(self.dingtalk_connection)
        wdt_repo = WdtRawRepository(self.wdt_connection)
        mart_repo = MartRepository(self.mart_connection)
        connections = _Connections(
            self.dingtalk_connection, self.wdt_connection, self.mart_connection,
        )

        service = LiveSyncService(
            dingtalk_gateway=fake_dingtalk,
            wdt_gateway=fake_wdt,
            dingtalk_repository=dingtalk_repo,
            wdt_repository=wdt_repo,
            mart_repository=mart_repo,
            connections=connections,
            now=lambda: datetime(2026, 6, 1, tzinfo=timezone.utc),
            new_run_id=lambda: "isolation-test-run-0001",
        )
        service.sync(manifest)

        # raw_dingtalk must NOT have wdt_records.
        dingtalk_tables = set(self._table_names(self.dingtalk_connection))
        self.assertNotIn("wdt_records", dingtalk_tables)

        # raw_wdt must NOT have fin_store_commission.
        wdt_tables = set(self._table_names(self.wdt_connection))
        self.assertNotIn("fin_store_commission", wdt_tables)

        # mart sync_dataset_summary must NOT have business or payload columns.
        mart_cols = self._column_names(self.mart_connection, "sync_dataset_summary")
        self.assertNotIn("payload_json", mart_cols)
        self.assertNotIn("store_name", mart_cols)
        self.assertNotIn("source_record_id", mart_cols)

    def test_idempotent_full_sync_cycle(self):
        """Running the same manifest twice produces identical digest values,
        a single raw row per record, and completed status for both runs."""
        manifest = _build_manifest()
        fake_dingtalk = _FakeDingTalkGateway(
            records=[
                {
                    "id": "idem-rec-001",
                    "fields": {"\u95e8\u5e97": "S1", "\u4f63\u91d1": "50.00"},
                }
            ]
        )
        fake_wdt = _FakeWdtGateway(
            records=[({"order_id": "idem-wdt-001"}, "idem-stable-001")]
        )

        run_ids = ["idempotent-full-run-a", "idempotent-full-run-b"]
        counter = {"n": 0}

        def next_run_id():
            idx = counter["n"]
            counter["n"] += 1
            return run_ids[idx]

        now_values = [
            datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 0, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 0, 0, 2, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 0, 0, 3, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 0, 0, 4, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 0, 0, 5, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 0, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 0, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 0, 1, 2, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 0, 1, 3, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 0, 1, 4, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 0, 1, 5, tzinfo=timezone.utc),
        ]
        now_counter = {"n": 0}

        def next_now():
            idx = now_counter["n"]
            now_counter["n"] += 1
            return now_values[idx]

        dingtalk_repo = DingTalkRawRepository(self.dingtalk_connection)
        wdt_repo = WdtRawRepository(self.wdt_connection)
        mart_repo = MartRepository(self.mart_connection)
        connections = _Connections(
            self.dingtalk_connection, self.wdt_connection, self.mart_connection,
        )

        service = LiveSyncService(
            dingtalk_gateway=fake_dingtalk,
            wdt_gateway=fake_wdt,
            dingtalk_repository=dingtalk_repo,
            wdt_repository=wdt_repo,
            mart_repository=mart_repo,
            connections=connections,
            now=next_now,
            new_run_id=next_run_id,
        )

        result_a = service.sync(manifest)

        cnt = self._count_rows(
            self.dingtalk_connection,
            "SELECT COUNT(*) AS cnt FROM fin_store_commission "
            "WHERE dingtalk_record_id = %s",
            ("idem-rec-001",),
        )
        self.assertEqual(1, cnt)

        result_b = service.sync(manifest)

        cnt_after = self._count_rows(
            self.dingtalk_connection,
            "SELECT COUNT(*) AS cnt FROM fin_store_commission "
            "WHERE dingtalk_record_id = %s",
            ("idem-rec-001",),
        )
        self.assertEqual(1, cnt_after)

        # Both sync_runs must be completed.
        statuses = self._query_values(
            self.mart_connection,
            "SELECT status FROM sync_runs "
            "WHERE sync_run_id IN (%s, %s) ORDER BY sync_run_id",
            (run_ids[0], run_ids[1]),
        )
        self.assertEqual(["completed", "completed"], statuses)

        # Digests must match (same input data -> same digest).
        digests = self._query_values(
            self.mart_connection,
            "SELECT record_id_digest FROM sync_dataset_summary "
            "WHERE sync_run_id IN (%s, %s) AND dataset_name = %s "
            "AND source_name = %s "
            "ORDER BY sync_run_id",
            (run_ids[0], run_ids[1], "test-commission", "dingtalk"),
        )
        self.assertEqual(2, len(digests))
        self.assertEqual(digests[0], digests[1])

    def test_projection_recovery(self):
        """After a projection failure leaves a run in projection_pending,
        rebuild_projection recovers it to completed using only local raw
        data -- no gateway calls required."""
        manifest = _build_manifest()
        fake_dingtalk = _FakeDingTalkGateway(
            records=[
                {
                    "id": "proj-rec-001",
                    "fields": {"\u95e8\u5e97": "P1", "\u4f63\u91d1": "25.00"},
                }
            ]
        )
        fake_wdt = _FakeWdtGateway(
            records=[({"order_id": "proj-wdt-001"}, "proj-stable-001")]
        )

        # Phase 1: run sync with a mart repo that fails on the first
        # save_dataset_summary call.  Raw data is committed but the run
        # ends in projection_pending.
        real_mart = MartRepository(self.mart_connection)
        failing_mart = _FailingMartRepository(real_mart)
        dingtalk_repo = DingTalkRawRepository(self.dingtalk_connection)
        wdt_repo = WdtRawRepository(self.wdt_connection)
        connections = _Connections(
            self.dingtalk_connection, self.wdt_connection, self.mart_connection,
        )

        recovery_run_id = "projection-recovery-run-0001"
        service = LiveSyncService(
            dingtalk_gateway=fake_dingtalk,
            wdt_gateway=fake_wdt,
            dingtalk_repository=dingtalk_repo,
            wdt_repository=wdt_repo,
            mart_repository=failing_mart,
            connections=connections,
            now=lambda: datetime(2026, 8, 1, tzinfo=timezone.utc),
            new_run_id=lambda: recovery_run_id,
        )

        with self.assertRaises(RuntimeError):
            service.sync(manifest)

        status = self._query_values(
            self.mart_connection,
            "SELECT status FROM sync_runs WHERE sync_run_id = %s",
            (recovery_run_id,),
        )
        self.assertEqual(["projection_pending"], status)

        # Phase 2: rebuild projection with gateways intentionally
        # unavailable.  Recovery must succeed from local raw data only.
        fresh_mart = MartRepository(self.mart_connection)
        recovery_service = LiveSyncService(
            dingtalk_gateway=_BrokenGateway(),
            wdt_gateway=_BrokenGateway(),
            dingtalk_repository=dingtalk_repo,
            wdt_repository=wdt_repo,
            mart_repository=fresh_mart,
            connections=connections,
            now=lambda: datetime(2026, 8, 1, 0, 5, 0, tzinfo=timezone.utc),
            new_run_id=lambda: "should-not-be-used",
        )
        recovery_service.rebuild_projection(recovery_run_id, manifest)

        recovered_status = self._query_values(
            self.mart_connection,
            "SELECT status FROM sync_runs WHERE sync_run_id = %s",
            (recovery_run_id,),
        )
        self.assertEqual(["completed"], recovered_status)


if __name__ == "__main__":
    unittest.main()
