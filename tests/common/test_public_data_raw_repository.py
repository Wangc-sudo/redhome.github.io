import math
import unittest
from datetime import datetime, timezone, date
from decimal import Decimal

from common.public_data.raw_repository import DingTalkRawRepository, WdtRawRepository
from common.public_data.mart_repository import MartRepository


class FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, query, parameters=None):
        self.executed.append((query, parameters))

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()

    def cursor(self):
        return self.cursor_instance


# ---------------------------------------------------------------------------
# DingTalkRawRepository tests
# ---------------------------------------------------------------------------

class DingTalkRawRepositoryTests(unittest.TestCase):
    def test_upsert_transforms_registered_types_and_never_uses_manifest_table_sql(self):
        connection = FakeConnection()
        repository = DingTalkRawRepository(connection)
        repository.upsert_records(
            "fin_store_commission",
            [{"id": "record-1", "fields": {
                "\u516c\u53f8\u4e3b\u4f53": "\u7532\u516c\u53f8",
                "\u586b\u5199\u4eba": [{"id": "u1"}],
                "2026-01": "12.3400",
            }}],
            field_mapping={
                "\u516c\u53f8\u4e3b\u4f53": "company_entity",
                "\u586b\u5199\u4eba": "submitted_by",
                "2026-01": "amount_2026_01",
            },
            sync_run_id="00000000-0000-0000-0000-000000000001",
            synced_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

        query, parameters = connection.cursor_instance.executed[-1]
        self.assertIn("INSERT INTO `fin_store_commission`", query)
        self.assertNotIn("from_manifest", query)
        self.assertIn(Decimal("12.3400"), parameters)
        self.assertTrue(
            any(value == '[{"id":"u1"}]' for value in parameters),
            f"Expected canonical JSON in parameters, got: {parameters}",
        )

    def test_decimal_rejects_non_finite(self):
        connection = FakeConnection()
        repository = DingTalkRawRepository(connection)
        with self.assertRaises(ValueError):
            repository.upsert_records(
                "fin_store_commission",
                [{"id": "record-bad", "fields": {"2026-01": float("nan")}}],
                field_mapping={"2026-01": "amount_2026_01"},
                sync_run_id="00000000-0000-0000-0000-000000000001",
                synced_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            )
        # No INSERT should have been executed
        insert_queries = [
            (q, p) for q, p in connection.cursor_instance.executed
            if "INSERT" in q
        ]
        self.assertEqual(insert_queries, [])

    def test_json_serialized_in_canonical_form(self):
        connection = FakeConnection()
        repository = DingTalkRawRepository(connection)
        repository.upsert_records(
            "fin_store_commission",
            [{"id": "record-json", "fields": {
                "\u586b\u5199\u4eba": [{"b": 2, "a": 1}],
            }}],
            field_mapping={"\u586b\u5199\u4eba": "submitted_by"},
            sync_run_id="00000000-0000-0000-0000-000000000001",
            synced_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

        query, parameters = connection.cursor_instance.executed[-1]
        # sort_keys=True, separators=(",",":") → compact canonical form
        self.assertIn('[{"a":1,"b":2}]', parameters)


# ---------------------------------------------------------------------------
# WdtRawRepository tests
# ---------------------------------------------------------------------------

class WdtRawRepositoryTests(unittest.TestCase):
    def test_upsert_uses_method_and_stable_source_id_primary_key(self):
        connection = FakeConnection()
        repository = WdtRawRepository(connection)
        repository.upsert_records(
            method="sales.TradeQuery.queryWithDetail",
            records=[({"trade_no": "T-1", "amount": 2}, "T-1")],
            window_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 9, 1, 0, 50, tzinfo=timezone.utc),
            sync_run_id="00000000-0000-0000-0000-000000000001",
            synced_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

        query, parameters = connection.cursor_instance.executed[-1]
        self.assertIn("ON DUPLICATE KEY UPDATE", query)
        self.assertIn("T-1", parameters)
        self.assertIn('{"amount":2,"trade_no":"T-1"}', parameters)

    def test_summary_queries_only_raw_run_metadata_and_never_payload_json(self):
        connection = FakeConnection()
        repository = WdtRawRepository(connection)
        repository.summary_for_run("00000000-0000-0000-0000-000000000001")

        query, parameters = connection.cursor_instance.executed[-1]
        self.assertIn("sync_run_id", query)
        self.assertNotIn("payload_json", query)
        self.assertEqual(("00000000-0000-0000-0000-000000000001",), parameters)


# ---------------------------------------------------------------------------
# MartRepository tests
# ---------------------------------------------------------------------------

class MartRepositoryTests(unittest.TestCase):
    def test_save_dataset_summary_targets_correct_table(self):
        connection = FakeConnection()
        repository = MartRepository(connection)
        repository.save_dataset_summary(
            sync_run_id="00000000-0000-0000-0000-000000000001",
            source_name="dingtalk",
            dataset_name="fin_store_commission",
            records_read=100,
            raw_records_written=95,
            record_id_digest="a" * 64,
            completed_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

        query, parameters = connection.cursor_instance.executed[-1]
        self.assertIn("INSERT INTO `sync_dataset_summary`", query)
        self.assertNotIn("payload_json", query)
        # Must contain sync_run_id, source_name, dataset_name, counts, digest, timestamp
        self.assertIn("00000000-0000-0000-0000-000000000001", parameters)
        self.assertIn("dingtalk", parameters)
        self.assertIn("fin_store_commission", parameters)
        self.assertIn(100, parameters)
        self.assertIn(95, parameters)

    def test_state_transitions_valid_and_invalid(self):
        connection = FakeConnection()
        repository = MartRepository(connection)
        started_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        finished_at = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)

        # Valid: start → raw_committed
        repository.start_run(
            sync_run_id="00000000-0000-0000-0000-000000000001",
            manifest_sha256="a" * 64,
            started_at=started_at,
        )
        repository.mark_raw_committed("00000000-0000-0000-0000-000000000001")

        # Verify the raw_committed UPDATE
        raw_committed_queries = [
            (q, p) for q, p in connection.cursor_instance.executed
            if "UPDATE" in q and p and "raw_committed" in p
        ]
        self.assertTrue(len(raw_committed_queries) >= 1)

        # Now complete it
        repository.mark_completed(
            "00000000-0000-0000-0000-000000000001",
            finished_at=finished_at,
        )
        completed_queries = [
            (q, p) for q, p in connection.cursor_instance.executed
            if "UPDATE" in q and p and "completed" in p
        ]
        self.assertTrue(len(completed_queries) >= 1)

        # Invalid transition: start a new run then try to mark_completed directly
        repository.start_run(
            sync_run_id="00000000-0000-0000-0000-000000000002",
            manifest_sha256="b" * 64,
            started_at=started_at,
        )
        with self.assertRaises(ValueError):
            repository.mark_completed(
                "00000000-0000-0000-0000-000000000002",
                finished_at=finished_at,
            )


if __name__ == "__main__":
    unittest.main()
