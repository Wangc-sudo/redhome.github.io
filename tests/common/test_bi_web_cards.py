"""Tests for the bi-web card registry (plan Task 3 / stage B Task 7).

The registry is the code-owned half of the ``SQL lives in code, layout
lives in Nacos`` split: sixteen stage-B cards, each bound to a ``run``
function from ``common.bi_web.queries``, each with a known chart kind
and a URL-parameter whitelist mapping param name -> filter source (empty
for the five L1 scalar cards).

``validate_dashboard_config`` closes the loop with Task 2's
``DashboardConfig``: a dashboard that references a card id missing from
the registry fails loudly (``CardConfigError``) instead of rendering a
half cockpit.
"""

import unittest

from common.bi_web import queries
from common.bi_web.cards import (
    KNOWN_CHARTS,
    REGISTRY,
    Card,
    CardConfigError,
    _card,
    validate_dashboard_config,
)
from common.bi_web.config import (
    KNOWN_FILTER_SOURCES,
    CardPlacement,
    DashboardConfig,
)

#: The stage-B card ids: five stage-A cards plus eleven new ones (16 in all).
STAGE_B_CARD_IDS = (
    "kpi_offline_mtd",
    "kpi_channel_mtd",
    "kpi_annual_progress",
    "trend_region_daily",
    "bar_channel_mtd",
    "kpi_offline_dod",
    "kpi_channel_dod",
    "kpi_region_mtd",
    "bar_department_mtd",
    "trend_channel_daily",
    "table_channel_mtd",
    "table_store_mtd",
    "kpi_people_count",
    "kpi_people_completed",
    "kpi_people_rate",
    "table_people_leaderboard",
)

#: The five scalar cards the L1 cockpit places without any URL parameter.
L1_PARAMLESS_CARD_IDS = (
    "kpi_offline_mtd",
    "kpi_channel_mtd",
    "kpi_annual_progress",
    "kpi_offline_dod",
    "kpi_channel_dod",
)

#: card_id -> expected chart kind.
EXPECTED_CHARTS = {
    "kpi_offline_mtd": "scalar",
    "kpi_channel_mtd": "scalar",
    "kpi_annual_progress": "scalar",
    "trend_region_daily": "line",
    "bar_channel_mtd": "bar",
    "kpi_offline_dod": "scalar",
    "kpi_channel_dod": "scalar",
    "kpi_region_mtd": "scalar",
    "bar_department_mtd": "bar",
    "trend_channel_daily": "line",
    "table_channel_mtd": "table",
    "table_store_mtd": "table",
    "kpi_people_count": "scalar",
    "kpi_people_completed": "scalar",
    "kpi_people_rate": "scalar",
    "table_people_leaderboard": "table",
}

#: card_id -> the queries.run_* function it must be bound to.
EXPECTED_RUN_FUNCTIONS = {
    "kpi_offline_mtd": queries.run_kpi_offline_mtd,
    "kpi_channel_mtd": queries.run_kpi_channel_mtd,
    "kpi_annual_progress": queries.run_kpi_annual_progress,
    "trend_region_daily": queries.run_trend_region_daily,
    "bar_channel_mtd": queries.run_bar_channel_mtd,
    "kpi_offline_dod": queries.run_kpi_offline_dod,
    "kpi_channel_dod": queries.run_kpi_channel_dod,
    "kpi_region_mtd": queries.run_kpi_region_mtd,
    "bar_department_mtd": queries.run_bar_department_mtd,
    "trend_channel_daily": queries.run_trend_channel_daily,
    "table_channel_mtd": queries.run_table_channel_mtd,
    "table_store_mtd": queries.run_table_store_mtd,
    "kpi_people_count": queries.run_kpi_people_count,
    "kpi_people_completed": queries.run_kpi_people_completed,
    "kpi_people_rate": queries.run_kpi_people_rate,
    "table_people_leaderboard": queries.run_table_people_leaderboard,
}

#: card_id -> URL-parameter whitelist, param name -> filter source.
EXPECTED_PARAMS_SCHEMA = {
    "kpi_offline_mtd": {},
    "kpi_channel_mtd": {},
    "kpi_annual_progress": {},
    "trend_region_daily": {"region": "regions", "month": "months"},
    "bar_channel_mtd": {"month": "months"},
    "kpi_offline_dod": {},
    "kpi_channel_dod": {},
    "kpi_region_mtd": {"region": "regions", "month": "months"},
    "bar_department_mtd": {"region": "regions", "month": "months"},
    "trend_channel_daily": {"month": "months"},
    "table_channel_mtd": {"month": "months"},
    "table_store_mtd": {"channel": "channels", "month": "months"},
    "kpi_people_count": {"region": "regions", "month": "months"},
    "kpi_people_completed": {"region": "regions", "month": "months"},
    "kpi_people_rate": {"region": "regions", "month": "months"},
    "table_people_leaderboard": {"region": "regions", "month": "months"},
}


def _l1_cockpit():
    """A valid dashboard placing the seven L1 cockpit cards (Task 10 seed)."""
    l1_card_ids = (
        "kpi_offline_dod",
        "kpi_channel_dod",
        "kpi_offline_mtd",
        "kpi_channel_mtd",
        "kpi_annual_progress",
        "trend_region_daily",
        "bar_channel_mtd",
    )
    return DashboardConfig(
        dashboard_id="l1-cockpit",
        title="首页驾驶舱",
        cards=tuple(
            CardPlacement(card=card_id, title=f"t-{card_id}", span=4)
            for card_id in l1_card_ids
        ),
    )


class RegistryTests(unittest.TestCase):
    """Registry completeness and per-card invariants."""

    def test_registry_contains_exactly_the_stage_b_cards(self):
        self.assertEqual(set(STAGE_B_CARD_IDS), set(REGISTRY))
        self.assertEqual(16, len(REGISTRY))
        for card_id in STAGE_B_CARD_IDS:
            self.assertIsInstance(REGISTRY[card_id], Card)

    def test_every_card_uses_a_known_chart(self):
        for card_id in STAGE_B_CARD_IDS:
            with self.subTest(card_id=card_id):
                self.assertIn(REGISTRY[card_id].chart, KNOWN_CHARTS)
                self.assertEqual(
                    EXPECTED_CHARTS[card_id], REGISTRY[card_id].chart
                )

    def test_l1_scalar_cards_have_no_url_parameters(self):
        for card_id in L1_PARAMLESS_CARD_IDS:
            with self.subTest(card_id=card_id):
                self.assertEqual({}, REGISTRY[card_id].params_schema)

    def test_parameterized_card_whitelists_match_spec(self):
        # Iterate the registry's id set, not EXPECTED_PARAMS_SCHEMA: a card
        # forgotten in the expectations dict must KeyError here (drift =
        # red build), not silently skip its whitelist assertion.
        for card_id in STAGE_B_CARD_IDS:
            with self.subTest(card_id=card_id):
                self.assertEqual(
                    EXPECTED_PARAMS_SCHEMA[card_id],
                    REGISTRY[card_id].params_schema,
                )

    def test_params_schema_values_are_known_filter_sources(self):
        for card_id in STAGE_B_CARD_IDS:
            for source in REGISTRY[card_id].params_schema.values():
                with self.subTest(card_id=card_id, source=source):
                    self.assertIn(source, KNOWN_FILTER_SOURCES)

    def test_card_builder_rejects_unknown_filter_source(self):
        with self.assertRaises(CardConfigError):
            _card(
                "bad_card", "scalar", queries.run_kpi_offline_mtd,
                {"region": "regionz"},
            )

    def test_every_card_binds_its_queries_run_function(self):
        # Same drift guard as the whitelist test: index by the registry's
        # id set so a forgotten expectations entry KeyErrors, not skips.
        for card_id in STAGE_B_CARD_IDS:
            with self.subTest(card_id=card_id):
                self.assertEqual(
                    EXPECTED_RUN_FUNCTIONS[card_id], REGISTRY[card_id].run
                )

    def test_known_charts_are_the_four_supported_kinds(self):
        self.assertEqual(("scalar", "line", "bar", "table"), KNOWN_CHARTS)


class ValidateDashboardConfigTests(unittest.TestCase):
    """Dashboard -> registry validation (unknown ids fail loudly)."""

    def test_dashboard_referencing_an_unknown_card_raises(self):
        dashboard = DashboardConfig(
            dashboard_id="l1-cockpit",
            cards=(CardPlacement(card="kpi_offline_mtd"), CardPlacement(card="no_such_card")),
        )

        with self.assertRaises(CardConfigError):
            validate_dashboard_config(dashboard, REGISTRY)

    def test_dashboard_with_only_unknown_cards_raises(self):
        dashboard = DashboardConfig(
            dashboard_id="broken",
            cards=(CardPlacement(card="typo_card"),),
        )

        with self.assertRaises(CardConfigError):
            validate_dashboard_config(dashboard, REGISTRY)

    def test_valid_l1_dashboard_passes(self):
        # Must not raise.
        validate_dashboard_config(_l1_cockpit(), REGISTRY)

    def test_dashboard_without_cards_passes(self):
        dashboard = DashboardConfig(dashboard_id="empty")

        validate_dashboard_config(dashboard, REGISTRY)  # must not raise

    def test_validation_uses_the_given_registry(self):
        """The registry is a parameter, so a subset registry is legal."""
        dashboard = DashboardConfig(
            dashboard_id="one-card",
            cards=(CardPlacement(card="only_card"),),
        )
        subset = {"only_card": Card("only_card", "scalar", lambda conn, params: {}, {})}

        validate_dashboard_config(dashboard, subset)  # must not raise

        with self.assertRaises(CardConfigError):
            validate_dashboard_config(_l1_cockpit(), subset)


if __name__ == "__main__":
    unittest.main()
