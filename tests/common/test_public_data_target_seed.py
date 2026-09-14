"""Tests for the version-controlled annual-target seed and its replay.

Three groups (plan Task 1):

* ``load_target_seed`` -- seed parsing/validation (errors never echo the
  file contents, ``_说明`` keys are allowed and ignored);
* ``replace_dim_target`` -- the wholesale ``dim_target`` replay on a fake
  connection (delete-then-parameterized-insert inside one transaction);
* ``LoadTargetCliTests`` -- the ``load-target`` subcommand and its gates.

Plus the repository seed regression (wan-yuan -> yuan conversion) and the
mart migration registration for the ``dim_target`` DDL.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from common.public_data.cli import main
from common.public_data.live_migrations import apply_live_migrations
from common.public_data.target_seed import (
    TargetRow,
    TargetSeedError,
    load_target_seed,
    replace_dim_target,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_SEED_PATH = REPO_ROOT / "docker" / "integration" / "target.seed.json"


class FakeCursor:
    def __init__(self, events):
        self.executed = []
        self.events = events
        self.closed = False

    def execute(self, query, parameters=None):
        self.executed.append((query, parameters))
        self.events.append(("execute", query))

    def executemany(self, query, parameters=None):
        self.executed.append((query, parameters))
        self.events.append(("executemany", query))

    def fetchone(self):
        return None

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self.events = []
        self.cursor_instance = FakeCursor(self.events)
        self.commit_calls = 0
        self.rollback_calls = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commit_calls += 1
        self.events.append("commit")

    def rollback(self):
        self.rollback_calls += 1
        self.events.append("rollback")


def _valid_seed():
    return {
        "version": 1,
        "targets": [
            {
                "scope": "line",
                "scope_key": "offline",
                "year": 2026,
                "annual_target": 210410000,
                "note": "offline target",
            },
            {
                "scope": "line",
                "scope_key": "channel",
                "year": 2026,
                "annual_target": 549800000,
                "note": "channel target",
            },
            {
                "scope": "line",
                "scope_key": "restaurant",
                "year": 2026,
                "annual_target": 9300000,
            },
        ],
    }


class LoadTargetSeedTests(unittest.TestCase):
    """Seed parsing and validation."""

    def _write_seed(self, data):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(data, tmp, ensure_ascii=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def test_valid_seed_yields_three_target_rows(self):
        path = self._write_seed(_valid_seed())

        rows = load_target_seed(path)

        self.assertEqual(3, len(rows))
        offline, channel, restaurant = rows
        for row in rows:
            self.assertIsInstance(row, TargetRow)
            self.assertIsInstance(row.annual_target, Decimal)
            self.assertEqual("line", row.scope)
            self.assertEqual(2026, row.year)
        self.assertEqual("offline", offline.scope_key)
        self.assertEqual(Decimal("210410000"), offline.annual_target)
        self.assertEqual("offline target", offline.note)
        self.assertEqual("channel", channel.scope_key)
        self.assertEqual(Decimal("549800000"), channel.annual_target)
        self.assertEqual("restaurant", restaurant.scope_key)
        self.assertEqual(Decimal("9300000"), restaurant.annual_target)
        self.assertEqual("", restaurant.note)

    def test_missing_file_raises_target_seed_error(self):
        with self.assertRaises(TargetSeedError):
            load_target_seed(REPO_ROOT / "tests" / "common" / "no-such-seed.json")

    def test_top_level_not_a_mapping_raises(self):
        path = self._write_seed([{"scope": "line"}])

        with self.assertRaises(TargetSeedError):
            load_target_seed(path)

    def test_missing_or_wrong_version_raises(self):
        missing = _valid_seed()
        del missing["version"]
        wrong = _valid_seed()
        wrong["version"] = 2
        for data in (missing, wrong):
            with self.subTest(version=data.get("version", "<missing>")):
                path = self._write_seed(data)

                with self.assertRaises(TargetSeedError):
                    load_target_seed(path)

    def test_targets_not_a_list_raises(self):
        data = _valid_seed()
        data["targets"] = {"scope_key": "offline"}

        path = self._write_seed(data)

        with self.assertRaises(TargetSeedError):
            load_target_seed(path)

    def test_empty_targets_list_raises(self):
        # An empty replay would silently wipe dim_target (delete-all +
        # insert-nothing), so the seed must be rejected up front.
        data = _valid_seed()
        data["targets"] = []

        path = self._write_seed(data)

        with self.assertRaises(TargetSeedError):
            load_target_seed(path)

    def test_row_missing_required_fields_raises(self):
        for field in ("scope", "scope_key", "year", "annual_target"):
            with self.subTest(field=field):
                data = _valid_seed()
                del data["targets"][1][field]

                path = self._write_seed(data)

                with self.assertRaises(TargetSeedError):
                    load_target_seed(path)

    def test_row_that_is_not_a_mapping_raises(self):
        data = _valid_seed()
        data["targets"][1] = ["line", "offline", 2026]

        path = self._write_seed(data)

        with self.assertRaises(TargetSeedError):
            load_target_seed(path)

    def test_year_out_of_range_raises(self):
        for year in (2019, 2101):
            with self.subTest(year=year):
                data = _valid_seed()
                data["targets"][0]["year"] = year

                path = self._write_seed(data)

                with self.assertRaises(TargetSeedError):
                    load_target_seed(path)

    def test_non_integer_year_raises(self):
        for year in ("2026", True, 2026.0):
            with self.subTest(year=year):
                data = _valid_seed()
                data["targets"][0]["year"] = year

                path = self._write_seed(data)

                with self.assertRaises(TargetSeedError):
                    load_target_seed(path)

    def test_negative_annual_target_raises(self):
        data = _valid_seed()
        data["targets"][0]["annual_target"] = -1

        path = self._write_seed(data)

        with self.assertRaises(TargetSeedError):
            load_target_seed(path)

    def test_non_numeric_annual_target_raises(self):
        for value in ("abc", None, {"amount": 1}, float("nan"), float("inf")):
            with self.subTest(value=value):
                data = _valid_seed()
                data["targets"][0]["annual_target"] = value

                path = self._write_seed(data)

                with self.assertRaises(TargetSeedError):
                    load_target_seed(path)

    def test_duplicate_scope_scope_key_year_raises(self):
        data = _valid_seed()
        data["targets"][1]["scope_key"] = data["targets"][0]["scope_key"]

        path = self._write_seed(data)

        with self.assertRaises(TargetSeedError):
            load_target_seed(path)

    def test_underscore_note_keys_are_allowed_and_ignored(self):
        data = _valid_seed()
        data["_说明"] = "top-level documentation key"
        data["targets"][0]["_说明"] = "row-level documentation key"

        path = self._write_seed(data)

        rows = load_target_seed(path)

        self.assertEqual(3, len(rows))
        self.assertEqual("offline target", rows[0].note)

    def test_error_messages_do_not_contain_file_contents(self):
        data = _valid_seed()
        data["targets"][1]["scope_key"] = data["targets"][0]["scope_key"]
        data["targets"][1]["note"] = "SECRET-LEAK-CHECK"

        path = self._write_seed(data)

        with self.assertRaises(TargetSeedError) as ctx:
            load_target_seed(path)

        message = str(ctx.exception)
        self.assertNotIn("SECRET-LEAK-CHECK", message)
        self.assertNotIn("offline", message)


REPLAY_ROWS = (
    TargetRow(
        scope="line",
        scope_key="offline",
        year=2026,
        annual_target=Decimal("210410000"),
        note="offline target",
    ),
    TargetRow(
        scope="line",
        scope_key="channel",
        year=2026,
        annual_target=Decimal("549800000"),
        note="channel target",
    ),
    TargetRow(
        scope="line",
        scope_key="restaurant",
        year=2026,
        annual_target=Decimal("9300000"),
    ),
)


class ReplaceDimTargetTests(unittest.TestCase):
    """The wholesale dim_target replay on a fake connection."""

    def test_deletes_all_rows_then_inserts_parameters_inside_one_transaction(self):
        connection = FakeConnection()

        written = replace_dim_target(connection, REPLAY_ROWS)

        self.assertEqual(3, written)
        events = connection.events
        self.assertEqual(("execute", "DELETE FROM `dim_target`"), events[0])
        self.assertEqual("executemany", events[1][0])
        self.assertTrue(events[1][1].startswith("INSERT INTO `dim_target`"))
        self.assertEqual("commit", events[2])
        self.assertEqual(1, connection.commit_calls)
        self.assertEqual(0, connection.rollback_calls)
        self.assertTrue(connection.cursor_instance.closed)

        insert_query, insert_params = connection.cursor_instance.executed[1]
        self.assertEqual(5, insert_query.count("%s"))
        # Values are bound parameters, never spliced into the SQL text.
        for text in (
            "offline", "channel", "restaurant", "line",
            "offline target", "210410000", "2026",
        ):
            self.assertNotIn(text, insert_query)
        self.assertEqual(
            [
                ("line", "offline", 2026, Decimal("210410000"), "offline target"),
                ("line", "channel", 2026, Decimal("549800000"), "channel target"),
                ("line", "restaurant", 2026, Decimal("9300000"), ""),
            ],
            insert_params,
        )

    def test_rolls_back_and_reraises_when_the_insert_fails(self):
        connection = FakeConnection()

        def broken_executemany(query, parameters=None):
            raise RuntimeError("insert failed")

        connection.cursor_instance.executemany = broken_executemany

        with self.assertRaises(RuntimeError):
            replace_dim_target(connection, REPLAY_ROWS)

        self.assertEqual(0, connection.commit_calls)
        self.assertEqual(1, connection.rollback_calls)
        self.assertEqual(("execute", "DELETE FROM `dim_target`"), connection.events[0])
        self.assertEqual("rollback", connection.events[-1])
        self.assertTrue(connection.cursor_instance.closed)


class DimTargetMigrationTests(unittest.TestCase):
    """The dim_target DDL lands on the mart database, versioned append-only."""

    def test_dim_target_ddl_lands_on_the_mart_database_only(self):
        dingtalk = FakeConnection()
        wdt = FakeConnection()
        mart = FakeConnection()

        apply_live_migrations(dingtalk, wdt, mart)

        mart_sql = "\n".join(query for query, _ in mart.cursor_instance.executed)
        dingtalk_sql = "\n".join(query for query, _ in dingtalk.cursor_instance.executed)
        wdt_sql = "\n".join(query for query, _ in wdt.cursor_instance.executed)
        self.assertIn("CREATE TABLE IF NOT EXISTS `dim_target`", mart_sql)
        self.assertIn("`annual_target` DECIMAL(20,4) NOT NULL", mart_sql)
        self.assertIn("PRIMARY KEY (`scope`, `scope_key`, `year`)", mart_sql)
        self.assertNotIn("dim_target", dingtalk_sql)
        self.assertNotIn("dim_target", wdt_sql)

    def test_dim_target_migration_is_versioned_in_the_tracking_table(self):
        mart = FakeConnection()

        apply_live_migrations(FakeConnection(), FakeConnection(), mart)

        tracked = [
            parameters[0]
            for query, parameters in mart.cursor_instance.executed
            if query.startswith("INSERT INTO `pd_live_schema_migration`")
        ]
        self.assertIn("mart-ops-dim-target-v1", tracked)


class TargetSeedFileTests(unittest.TestCase):
    """The repository seed fixes the wan-yuan -> yuan conversion as a regression."""

    def test_repository_seed_carries_the_converted_annual_targets(self):
        rows = load_target_seed(REPO_SEED_PATH)

        by_scope_key = {row.scope_key: row for row in rows}
        self.assertEqual({"offline", "channel", "restaurant"}, set(by_scope_key))
        self.assertEqual(Decimal("210410000"), by_scope_key["offline"].annual_target)
        self.assertEqual(Decimal("549800000"), by_scope_key["channel"].annual_target)
        self.assertEqual(Decimal("9300000"), by_scope_key["restaurant"].annual_target)
        for row in rows:
            self.assertEqual("line", row.scope)
            self.assertEqual(2026, row.year)


class LoadTargetCliTests(unittest.TestCase):
    """The load-target subcommand and its safety/wiring gates."""

    def _patch_common(self, settings):
        return (
            patch("common.public_data.cli.load_settings", return_value=settings),
            patch("common.public_data.cli.require_extract_run"),
        )

    def test_load_target_requires_confirmation_before_any_work(self):
        with patch("common.public_data.cli.load_settings") as load_settings, \
             patch("common.public_data.cli.load_target_seed") as load_seed:
            with self.assertRaises(SystemExit) as raised:
                main(["load-target"])

        self.assertNotEqual(0, raised.exception.code)
        load_settings.assert_not_called()
        load_seed.assert_not_called()

    def test_load_target_replays_the_seed_into_the_mart_database(self):
        output = io.StringIO()
        settings = Mock()
        rows = (
            TargetRow("line", "offline", 2026, Decimal("210410000"), "offline target"),
            TargetRow("line", "channel", 2026, Decimal("549800000")),
        )
        connections = [FakeConnection(), FakeConnection(), FakeConnection()]
        load_patch, gate_patch = self._patch_common(settings)
        with load_patch, gate_patch, \
             patch("common.public_data.cli.load_target_seed",
                   return_value=rows) as load_seed, \
             patch("common.public_data.cli.replace_dim_target",
                   return_value=3) as replace, \
             patch("common.public_data.cli.connect", side_effect=connections):
            with redirect_stdout(output):
                main(["load-target", "--confirm-local-test-write"])

        text = output.getvalue()
        self.assertIn("targets_written=3", text)
        self.assertIn("status=completed", text)
        load_seed.assert_called_once_with("docker/integration/target.seed.json")
        replace.assert_called_once_with(connections[2], rows)

    def test_load_target_accepts_a_custom_seed_path(self):
        output = io.StringIO()
        settings = Mock()
        load_patch, gate_patch = self._patch_common(settings)
        with load_patch, gate_patch, \
             patch("common.public_data.cli.load_target_seed", return_value=()) as load_seed, \
             patch("common.public_data.cli.replace_dim_target", return_value=0), \
             patch("common.public_data.cli.connect",
                   side_effect=[FakeConnection() for _ in range(3)]):
            with redirect_stdout(output):
                main(["load-target", "--seed", "/seeds/custom.json",
                      "--confirm-local-test-write"])

        load_seed.assert_called_once_with("/seeds/custom.json")

    def test_load_target_failure_is_safe(self):
        output = io.StringIO()
        settings = Mock()
        rows = (TargetRow("line", "offline", 2026, Decimal("210410000")),)
        connections = [FakeConnection(), FakeConnection(), FakeConnection()]
        load_patch, gate_patch = self._patch_common(settings)
        with load_patch, gate_patch, \
             patch("common.public_data.cli.load_target_seed", return_value=rows), \
             patch("common.public_data.cli.replace_dim_target",
                   side_effect=RuntimeError("secret-host")), \
             patch("common.public_data.cli.connect", side_effect=connections):
            with redirect_stdout(output):
                with self.assertRaises(SystemExit) as raised:
                    main(["load-target", "--confirm-local-test-write"])

        self.assertNotEqual(0, raised.exception.code)
        text = output.getvalue()
        self.assertIn("status=failed", text)
        self.assertNotIn("secret-host", text)
        self.assertNotIn("Traceback", text)


if __name__ == "__main__":
    unittest.main()
