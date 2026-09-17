import unittest
from types import SimpleNamespace

from common.public_data.mart_routing import (
    DIMS_READ_ENV,
    FACTS_READ_ENV,
    FACTS_WRITE_ENV,
    QUEUE_ENV,
    MartRoutingError,
    MartSplitSwitches,
    dims_read_database,
    facts_read_database,
    facts_write_databases,
    qualified_table,
    queue_database,
    resolve_dims_database,
    resolve_facts_database,
    resolve_queue_database,
)
from common.public_data.settings import DatabaseSettings


def _db(name):
    return DatabaseSettings(
        host="localhost", port=3306, user="public_data", password="x", name=name
    )


def _settings(
    mart="mart_test", facts=None, dims=None, queue=None
) -> SimpleNamespace:
    return SimpleNamespace(
        mart_database=_db(mart),
        mart_facts_database=_db(facts) if facts else None,
        mart_dims_database=_db(dims) if dims else None,
        mart_queue_database=_db(queue) if queue else None,
    )


class MartSplitSwitchesTests(unittest.TestCase):
    def test_defaults_are_all_legacy_side(self):
        switches = MartSplitSwitches.from_environment({})

        self.assertEqual("off", switches.facts_write)
        self.assertEqual("old", switches.facts_read)
        self.assertEqual("old", switches.dims_read)
        self.assertEqual("off", switches.queue)

    def test_parses_each_switch_case_insensitively(self):
        switches = MartSplitSwitches.from_environment(
            {
                FACTS_WRITE_ENV: " DUAL ",
                FACTS_READ_ENV: "New",
                DIMS_READ_ENV: "new",
                QUEUE_ENV: "ON",
            }
        )

        self.assertEqual("dual", switches.facts_write)
        self.assertEqual("new", switches.facts_read)
        self.assertEqual("new", switches.dims_read)
        self.assertEqual("on", switches.queue)

    def test_rejects_unknown_values_without_silent_fallback(self):
        for env_var, value in (
            (FACTS_WRITE_ENV, "both"),
            (FACTS_READ_ENV, "new-ish"),
            (DIMS_READ_ENV, "1"),
            (QUEUE_ENV, "enabled"),
        ):
            with self.subTest(env_var=env_var):
                with self.assertRaisesRegex(MartRoutingError, env_var):
                    MartSplitSwitches.from_environment({env_var: value})


class ResolveFallbackTests(unittest.TestCase):
    def test_unconfigured_split_databases_fall_back_to_legacy_mart(self):
        settings = _settings()

        self.assertEqual("mart_test", resolve_facts_database(settings).name)
        self.assertEqual("mart_test", resolve_dims_database(settings).name)
        self.assertEqual("mart_test", resolve_queue_database(settings).name)

    def test_configured_split_databases_win(self):
        settings = _settings(
            facts="mart_facts_test", dims="mart_dims_test", queue="mart_queue_test"
        )

        self.assertEqual("mart_facts_test", resolve_facts_database(settings).name)
        self.assertEqual("mart_dims_test", resolve_dims_database(settings).name)
        self.assertEqual("mart_queue_test", resolve_queue_database(settings).name)


class RoutingTests(unittest.TestCase):
    def test_facts_write_off_targets_only_legacy(self):
        settings = _settings(facts="mart_facts_test")
        switches = MartSplitSwitches(facts_write="off")

        targets = facts_write_databases(settings, switches)

        self.assertEqual(("mart_test",), tuple(db.name for db in targets))

    def test_facts_write_dual_targets_legacy_then_new(self):
        settings = _settings(facts="mart_facts_test")
        switches = MartSplitSwitches(facts_write="dual")

        targets = facts_write_databases(settings, switches)

        self.assertEqual(
            ("mart_test", "mart_facts_test"), tuple(db.name for db in targets)
        )

    def test_facts_write_dual_dedupes_when_new_falls_back_to_legacy(self):
        settings = _settings()
        switches = MartSplitSwitches(facts_write="dual")

        targets = facts_write_databases(settings, switches)

        self.assertEqual(("mart_test",), tuple(db.name for db in targets))

    def test_facts_write_new_targets_only_new_schema(self):
        settings = _settings(facts="mart_facts_test")
        switches = MartSplitSwitches(facts_write="new")

        targets = facts_write_databases(settings, switches)

        self.assertEqual(("mart_facts_test",), tuple(db.name for db in targets))

    def test_read_switches_pick_side(self):
        settings = _settings(facts="mart_facts_test", dims="mart_dims_test")

        self.assertEqual(
            "mart_test",
            facts_read_database(settings, MartSplitSwitches(facts_read="old")).name,
        )
        self.assertEqual(
            "mart_facts_test",
            facts_read_database(settings, MartSplitSwitches(facts_read="new")).name,
        )
        self.assertEqual(
            "mart_test",
            dims_read_database(settings, MartSplitSwitches(dims_read="old")).name,
        )
        self.assertEqual(
            "mart_dims_test",
            dims_read_database(settings, MartSplitSwitches(dims_read="new")).name,
        )

    def test_queue_switch_points_outbox_at_new_schema(self):
        settings = _settings(queue="mart_queue_test")

        self.assertEqual(
            "mart_test",
            queue_database(settings, MartSplitSwitches(queue="off")).name,
        )
        self.assertEqual(
            "mart_queue_test",
            queue_database(settings, MartSplitSwitches(queue="on")).name,
        )

    def test_qualified_table_renders_cross_schema_name(self):
        self.assertEqual(
            "`mart_dims_test`.`dim_robot_member`",
            qualified_table(_db("mart_dims_test"), "dim_robot_member"),
        )


if __name__ == "__main__":
    unittest.main()
