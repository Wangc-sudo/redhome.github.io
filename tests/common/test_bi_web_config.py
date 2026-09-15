"""Tests for the bi-web dashboard registry configuration (stage A plan
Task 2; stage B plan Task 1).

Groups:

* ``ParseDashboardConfigTests`` -- strict parsing of one dashboard entry
  (unknown keys rejected so a ``car:`` typo cannot silently drop a card,
  ``_说明`` documentation keys allowed and ignored);
* ``StageBParseTests`` -- stage B fields (``nav_order`` / ``filters`` /
  ``on_click``) under the same strict rules;
* ``DashboardIdsTests`` -- the ``dashboard_ids()`` enumeration contract
  across the backends;
* ``StaticDashboardSourceTests`` / ``LoadSeedTests`` /
  ``FileDashboardSourceTests`` / ``NacosDashboardSourceTests`` -- the
  ConfigSource backends (the Nacos group is the fixed ``BI`` group);
* ``BuildDashboardConfigSourceTests`` -- env-driven backend selection
  (``PUBLIC_DATA_NACOS_GROUP`` is deliberately never read);
* ``PublishDashboardsTests`` -- the first-boot Nacos import;
* ``PublishBiCliTests`` -- the ``publish-bi`` subcommand.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from common.bi_web.cards import REGISTRY, validate_dashboard_config
from common.bi_web.config import (
    BI_GROUP,
    CardPlacement,
    DashboardConfig,
    DashboardConfigError,
    DashboardConfigSource,
    FileDashboardSource,
    NacosDashboardSource,
    StaticDashboardSource,
    build_dashboard_config_source,
    load_seed,
    parse_dashboard_config,
    publish_dashboards,
)
from common.public_data.cli import main


# spec section 5.2 -- the l1-cockpit original text.
_L1_COCKPIT_YAML = """\
l1-cockpit:
  title: "首页驾驶舱"
  enabled: true
  refresh_seconds: 300
  cards:
    - card: kpi_offline_mtd
      title: "线下本月累计销售"
      span: 4
    - card: kpi_channel_mtd
      title: "电商渠道本月累计销售"
      span: 4
    - card: kpi_annual_progress
      title: "年度目标达成进度"
      span: 4
    - card: trend_region_daily
      title: "区域日销趋势"
      span: 8
    - card: bar_channel_mtd
      title: "渠道本月排行"
      span: 4
"""


def _l1_cockpit():
    """The l1-cockpit entry as a mapping (parsed from the spec text)."""
    return yaml.safe_load(_L1_COCKPIT_YAML)["l1-cockpit"]


def _l1_cockpit_entry_yaml():
    """The l1-cockpit entry as standalone YAML text (a Nacos dataId body)."""
    return yaml.safe_dump(_l1_cockpit(), allow_unicode=True, sort_keys=False)


class _FakeNacosClient:
    def __init__(self, configs=None):
        self.configs = dict(configs or {})
        self.published = []
        self.requests = []

    def get_config(self, data_id, group):
        self.requests.append((data_id, group))
        return self.configs.get((data_id, group))

    def publish_config(self, data_id, group, content, config_type=None):
        self.configs[(data_id, group)] = content
        self.published.append((data_id, group, content, config_type))


class _UnreachableClient:
    def get_config(self, data_id, group):
        raise RuntimeError("connection refused")


class _SeedFileTestCase(unittest.TestCase):
    """Shared helper: write a temp seed file that is unlinked on cleanup."""

    def _write_seed(self, text):
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        )
        tmp.write(text)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name


class ParseDashboardConfigTests(unittest.TestCase):
    """Strict parsing of a single dashboard entry."""

    def test_valid_l1_cockpit_mapping(self):
        config = parse_dashboard_config("l1-cockpit", _l1_cockpit())

        self.assertIsInstance(config, DashboardConfig)
        self.assertEqual("l1-cockpit", config.dashboard_id)
        self.assertEqual("首页驾驶舱", config.title)
        self.assertTrue(config.enabled)
        self.assertEqual(300, config.refresh_seconds)
        self.assertIsInstance(config.cards, tuple)
        self.assertEqual(5, len(config.cards))
        first = config.cards[0]
        self.assertIsInstance(first, CardPlacement)
        self.assertEqual("kpi_offline_mtd", first.card)
        self.assertEqual("线下本月累计销售", first.title)
        self.assertEqual(4, first.span)
        self.assertEqual("电商渠道本月累计销售", config.cards[1].title)
        self.assertEqual("trend_region_daily", config.cards[3].card)
        self.assertEqual("区域日销趋势", config.cards[3].title)
        self.assertEqual(8, config.cards[3].span)
        self.assertEqual("bar_channel_mtd", config.cards[4].card)
        self.assertEqual(4, config.cards[4].span)

    def test_minimal_mapping_uses_field_defaults(self):
        config = parse_dashboard_config("x", {"cards": [{"card": "kpi_offline_mtd"}]})

        self.assertEqual("", config.title)
        self.assertTrue(config.enabled)
        self.assertEqual(300, config.refresh_seconds)
        self.assertEqual(1, len(config.cards))
        self.assertEqual("", config.cards[0].title)
        self.assertEqual(4, config.cards[0].span)

    def test_none_yields_minimal_default(self):
        config = parse_dashboard_config("l1-cockpit", None)

        self.assertIsInstance(config, DashboardConfig)
        self.assertEqual("l1-cockpit", config.dashboard_id)
        self.assertEqual("", config.title)
        self.assertTrue(config.enabled)
        self.assertEqual(300, config.refresh_seconds)
        self.assertEqual((), config.cards)

    def test_rejects_non_mapping_entry(self):
        for data in (["not", "a", "map"], "enabled: true", 42, True):
            with self.subTest(data=data):
                with self.assertRaises(DashboardConfigError):
                    parse_dashboard_config("l1-cockpit", data)

    def test_rejects_non_boolean_enabled(self):
        for enabled in ("true", 1, None):
            with self.subTest(enabled=enabled):
                with self.assertRaises(DashboardConfigError):
                    parse_dashboard_config("x", {"enabled": enabled})

    def test_rejects_invalid_refresh_seconds(self):
        for seconds in (0, 86401, "300", True, 300.5, None):
            with self.subTest(seconds=seconds):
                with self.assertRaises(DashboardConfigError):
                    parse_dashboard_config("x", {"refresh_seconds": seconds})

    def test_accepts_refresh_seconds_boundaries(self):
        for seconds in (60, 86400):
            with self.subTest(seconds=seconds):
                config = parse_dashboard_config("x", {"refresh_seconds": seconds})
                self.assertEqual(seconds, config.refresh_seconds)

    def test_rejects_invalid_span(self):
        for span in (0, 13, "4", True, 4.5, None):
            with self.subTest(span=span):
                data = {"cards": [{"card": "kpi_offline_mtd", "span": span}]}
                with self.assertRaises(DashboardConfigError):
                    parse_dashboard_config("x", data)

    def test_accepts_span_boundaries(self):
        for span in (1, 12):
            with self.subTest(span=span):
                data = {"cards": [{"card": "kpi_offline_mtd", "span": span}]}
                config = parse_dashboard_config("x", data)
                self.assertEqual(span, config.cards[0].span)

    def test_rejects_cards_that_are_not_a_list(self):
        for cards in ({"card": "kpi_offline_mtd"}, "kpi_offline_mtd", 42, None):
            with self.subTest(cards=cards):
                with self.assertRaises(DashboardConfigError):
                    parse_dashboard_config("x", {"cards": cards})

    def test_rejects_card_element_that_is_not_a_mapping(self):
        for element in (["card", "kpi_offline_mtd"], "kpi_offline_mtd", 42):
            with self.subTest(element=element):
                with self.assertRaises(DashboardConfigError):
                    parse_dashboard_config("x", {"cards": [element]})

    def test_rejects_card_without_card_key(self):
        data = {"cards": [{"title": "线下本月累计销售", "span": 4}]}

        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", data)

    def test_rejects_non_string_card_name(self):
        for card in (42, "", None):
            with self.subTest(card=card):
                with self.assertRaises(DashboardConfigError):
                    parse_dashboard_config("x", {"cards": [{"card": card}]})

    def test_rejects_non_string_titles(self):
        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", {"title": 42})
        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", {"cards": [{"card": "c", "title": 42}]})

    def test_rejects_unknown_dashboard_key(self):
        # A ``car:`` typo must not silently become a dashboard without cards.
        data = _l1_cockpit()
        data["car"] = [{"card": "kpi_offline_mtd"}]

        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("l1-cockpit", data)

    def test_rejects_unknown_card_key(self):
        data = {"cards": [{"card": "kpi_offline_mtd", "span": 4, "titel": "typo"}]}

        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", data)

    def test_underscore_note_keys_are_allowed_and_ignored(self):
        data = _l1_cockpit()
        data["_说明"] = "top-level documentation key"
        data["cards"][0]["_说明"] = "card-level documentation key"

        config = parse_dashboard_config("l1-cockpit", data)

        self.assertEqual(5, len(config.cards))
        self.assertEqual("kpi_offline_mtd", config.cards[0].card)
        self.assertEqual(4, config.cards[0].span)

    def test_error_messages_do_not_contain_field_values(self):
        typo = _l1_cockpit()
        typo["car"] = "SECRET-LEAK-CHECK"
        with self.assertRaises(DashboardConfigError) as ctx:
            parse_dashboard_config("l1-cockpit", typo)
        message = str(ctx.exception)
        self.assertNotIn("SECRET-LEAK-CHECK", message)
        self.assertNotIn("car", message)

        bad_span = {"cards": [{"card": "c", "title": "SECRET-LEAK-CHECK", "span": 13}]}
        with self.assertRaises(DashboardConfigError) as ctx:
            parse_dashboard_config("x", bad_span)
        self.assertNotIn("SECRET-LEAK-CHECK", str(ctx.exception))


class StageBParseTests(unittest.TestCase):
    """阶段 B 字段：nav_order / filters / on_click（与既有字段同一套严格规则）。"""

    def test_defaults_nav_order_zero_and_no_filters(self):
        config = parse_dashboard_config("l2-region", {"title": "区域下钻"})

        self.assertEqual(0, config.nav_order)
        self.assertEqual((), config.filters)

    def test_parses_nav_order(self):
        config = parse_dashboard_config("l2-region", {"nav_order": 10})

        self.assertEqual(10, config.nav_order)

    def test_parses_filters_into_filter_spec_tuple(self):
        data = {
            "filters": [
                {"param": "region", "source": "regions", "label": "区域"},
                {"param": "month", "source": "months"},
            ]
        }

        config = parse_dashboard_config("l2-region", data)

        self.assertEqual(2, len(config.filters))
        first, second = config.filters
        self.assertEqual("region", first.param)
        self.assertEqual("regions", first.source)
        self.assertEqual("区域", first.label)
        self.assertEqual("", second.label)

    def test_parses_on_click_param(self):
        data = {
            "filters": [{"param": "channel", "source": "channels"}],
            "cards": [{"card": "bar_channel_mtd", "on_click": {"param": "channel"}}],
        }

        config = parse_dashboard_config("l2-channel", data)

        self.assertEqual("channel", config.cards[0].on_click)

    def test_rejects_invalid_nav_order(self):
        for nav_order in (-1, "10", True, 10.5, None):
            with self.subTest(nav_order=nav_order):
                with self.assertRaises(DashboardConfigError):
                    parse_dashboard_config("x", {"nav_order": nav_order})

    def test_rejects_filters_that_are_not_a_list(self):
        for filters in ({"param": "region"}, "regions", 42):
            with self.subTest(filters=filters):
                with self.assertRaises(DashboardConfigError):
                    parse_dashboard_config("x", {"filters": filters})

    def test_rejects_filter_element_that_is_not_a_mapping(self):
        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", {"filters": ["region"]})

    def test_rejects_filter_without_param_or_source(self):
        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", {"filters": [{"source": "regions"}]})
        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", {"filters": [{"param": "region"}]})

    def test_rejects_unknown_filter_source(self):
        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config(
                "x", {"filters": [{"param": "region", "source": "regionz"}]}
            )

    def test_rejects_unknown_filter_key(self):
        data = {"filters": [{"param": "region", "source": "regions", "lable": "区域"}]}

        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", data)

    def test_rejects_non_string_filter_label(self):
        data = {"filters": [{"param": "region", "source": "regions", "label": 42}]}

        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", data)

    def test_rejects_duplicate_filter_param_on_one_page(self):
        data = {
            "filters": [
                {"param": "region", "source": "regions"},
                {"param": "region", "source": "regions"},
            ]
        }

        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", data)

    def test_rejects_on_click_param_missing_from_page_filters(self):
        data = {
            "filters": [{"param": "month", "source": "months"}],
            "cards": [{"card": "bar_channel_mtd", "on_click": {"param": "channel"}}],
        }

        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("l2-channel", data)

    def test_rejects_on_click_that_is_not_a_mapping(self):
        data = {
            "filters": [{"param": "channel", "source": "channels"}],
            "cards": [{"card": "bar_channel_mtd", "on_click": "channel"}],
        }

        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", data)

    def test_rejects_on_click_without_param_key(self):
        data = {
            "filters": [{"param": "channel", "source": "channels"}],
            "cards": [{"card": "bar_channel_mtd", "on_click": {}}],
        }

        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", data)

    def test_underscore_note_keys_stay_allowed_in_new_fields(self):
        data = {
            "nav_order": 10,
            "filters": [{"param": "region", "source": "regions", "_说明": "x"}],
            "cards": [{"card": "bar_channel_mtd", "on_click": {"param": "region", "_说明": "x"}}],
        }

        config = parse_dashboard_config("x", data)

        self.assertEqual(10, config.nav_order)
        self.assertEqual("region", config.cards[0].on_click)

    def test_error_messages_do_not_leak_field_values(self):
        with self.assertRaises(DashboardConfigError) as ctx:
            parse_dashboard_config(
                "x", {"filters": [{"param": "region", "source": "SECRET-LEAK-CHECK"}]}
            )
        self.assertNotIn("SECRET-LEAK-CHECK", str(ctx.exception))


class DashboardIdsTests(_SeedFileTestCase):
    """阶段 B 导航枚举：dashboard_ids() 各后端行为。"""

    def test_static_source_lists_its_ids_sorted(self):
        source = StaticDashboardSource({"l2-region": {}, "l1-cockpit": {}})

        self.assertEqual(("l1-cockpit", "l2-region"), source.dashboard_ids())

    def test_file_source_delegates_to_the_seed_mapping(self):
        path = self._write_seed("l1-cockpit:\n  title: 首页驾驶舱\n")

        source = FileDashboardSource(path)

        self.assertEqual(("l1-cockpit",), source.dashboard_ids())

    def test_nacos_source_delegates_to_the_fallback(self):
        fallback = StaticDashboardSource({"l1-cockpit": {}})
        source = NacosDashboardSource(
            server="nacos:8848", client=_FakeNacosClient(), fallback=fallback
        )

        self.assertEqual(("l1-cockpit",), source.dashboard_ids())

    def test_nacos_source_without_fallback_lists_nothing(self):
        source = NacosDashboardSource(server="nacos:8848", client=_FakeNacosClient())

        self.assertEqual((), source.dashboard_ids())

    def test_base_contract_defaults_to_no_ids(self):
        class _Bare(DashboardConfigSource):
            def get_dashboard(self, dashboard_id):
                return None

        self.assertEqual((), _Bare().dashboard_ids())


class StaticDashboardSourceTests(unittest.TestCase):
    def test_default_is_enabled_with_no_cards(self):
        source = StaticDashboardSource({})

        config = source.get_dashboard("l1-cockpit")

        self.assertIsInstance(config, DashboardConfig)
        self.assertTrue(config.enabled)
        self.assertEqual((), config.cards)

    def test_entry_overrides_default_for_other_ids(self):
        source = StaticDashboardSource({"l1-cockpit": {"enabled": False}})

        self.assertFalse(source.get_dashboard("l1-cockpit").enabled)
        self.assertTrue(source.get_dashboard("l2-region").enabled)

    def test_rejects_non_mapping_entry(self):
        with self.assertRaises(DashboardConfigError):
            StaticDashboardSource({"l1-cockpit": "not-a-mapping"})


class LoadSeedTests(_SeedFileTestCase):
    def test_load_seed_returns_the_mapping(self):
        path = self._write_seed("l1-cockpit:\n  title: 首页驾驶舱\n")

        self.assertEqual(
            {"l1-cockpit": {"title": "首页驾驶舱"}}, load_seed(path)
        )

    def test_load_seed_rejects_non_mapping_top_level(self):
        path = self._write_seed("- just\n- a\n- list\n")

        with self.assertRaises(DashboardConfigError):
            load_seed(path)


#: The version-controlled dashboard seed, shipped with the repository and
#: published to Nacos group=BI on first boot (same convention as
#: ``calendar.seed.json`` / ``target.seed.json``).
_REPO_BI_SEED_PATH = (
    Path(__file__).resolve().parents[2] / "docker" / "integration" / "bi.seed.yaml"
)


class BiSeedFileTests(unittest.TestCase):
    """The shipped seed must not drift from the card registry.

    Reads the repository file directly (the ``calendar.seed.json``
    precedent): parsing succeeds, every placed card id exists in the real
    ``REGISTRY``, every filter source is known, and each page's card
    sequence / spans / filters / nav_order match the shipped stage-B
    layout -- so seed/registry drift is a red build, not a 503 at
    startup.
    """

    def test_seed_parses_places_only_registry_cards_with_the_planned_spans(self):
        mapping = load_seed(_REPO_BI_SEED_PATH)

        self.assertEqual(
            {"l1-cockpit", "l2-region", "l2-channel", "l2-product",
             "l2-people"},
            set(mapping),
        )
        configs = {
            dashboard_id: parse_dashboard_config(dashboard_id, mapping[dashboard_id])
            for dashboard_id in mapping
        }
        for config in configs.values():
            validate_dashboard_config(config, REGISTRY)
        # 翻成 false 的页面会 404 且从导航消失，宿主测试无其他信号可拦。
        self.assertTrue(all(config.enabled for config in configs.values()))

        l1 = configs["l1-cockpit"]
        self.assertEqual(0, l1.nav_order)
        self.assertEqual((), l1.filters)
        # 结果 → 变化 → 风险：本月累计与目标在前，缺口/告警（派生口径）
        # 紧跟其后，日环比降为条线辅助。
        self.assertEqual(11, len(l1.cards))
        self.assertEqual(
            ("kpi_offline_mtd", "kpi_channel_mtd", "kpi_annual_progress",
             "trend_region_daily", "bar_channel_mtd", "table_channel_mtd",
             "pie_sku_mtd", "anomaly_top", "kpi_shortfall",
             "kpi_offline_dod", "kpi_channel_dod"),
            tuple(placement.card for placement in l1.cards),
        )
        self.assertEqual(
            (4, 4, 4, 8, 4, 12, 6, 6, 6, 3, 3),
            tuple(placement.span for placement in l1.cards),
        )

        region = configs["l2-region"]
        self.assertEqual(10, region.nav_order)
        self.assertEqual(
            (("region", "regions", "区域"), ("month", "months", "月份")),
            tuple(
                (spec.param, spec.source, spec.label) for spec in region.filters
            ),
        )
        self.assertEqual(
            ("kpi_region_mtd", "trend_region_daily", "bar_department_mtd"),
            tuple(placement.card for placement in region.cards),
        )
        self.assertEqual(
            (4, 8, 12), tuple(placement.span for placement in region.cards)
        )

        channel = configs["l2-channel"]
        self.assertEqual(20, channel.nav_order)
        self.assertEqual(
            (("channel", "channels", "渠道"), ("month", "months", "月份")),
            tuple(
                (spec.param, spec.source, spec.label) for spec in channel.filters
            ),
        )
        self.assertEqual(
            ("bar_channel_mtd", "trend_channel_daily", "table_channel_mtd",
             "table_store_mtd"),
            tuple(placement.card for placement in channel.cards),
        )
        self.assertEqual("channel", channel.cards[0].on_click)
        self.assertIsNone(channel.cards[1].on_click)
        self.assertEqual(
            (4, 8, 6, 6), tuple(placement.span for placement in channel.cards)
        )

        product = configs["l2-product"]
        self.assertEqual(25, product.nav_order)
        self.assertEqual(
            (("month", "months", "月份"), ("brand", "brands", "品牌"),
             ("channel", "sku_channels", "渠道")),
            tuple(
                (spec.param, spec.source, spec.label) for spec in product.filters
            ),
        )
        self.assertEqual(
            ("kpi_sku_mtd", "table_sku_hot_total", "table_sku_hot_brand",
             "table_sku_hot_channel"),
            tuple(placement.card for placement in product.cards),
        )
        self.assertEqual(
            (4, 8, 6, 6), tuple(placement.span for placement in product.cards)
        )

        people = configs["l2-people"]
        self.assertEqual(30, people.nav_order)
        self.assertEqual(
            (("region", "regions", "区域"), ("month", "months", "月份")),
            tuple(
                (spec.param, spec.source, spec.label) for spec in people.filters
            ),
        )
        self.assertEqual(
            ("kpi_people_count", "kpi_people_completed", "kpi_people_rate",
             "table_people_leaderboard"),
            tuple(placement.card for placement in people.cards),
        )
        self.assertEqual(
            (4, 4, 4, 12), tuple(placement.span for placement in people.cards)
        )


class FileDashboardSourceTests(_SeedFileTestCase):
    def test_reads_the_seed_yaml(self):
        path = self._write_seed(_L1_COCKPIT_YAML)

        source = FileDashboardSource(path)
        config = source.get_dashboard("l1-cockpit")

        self.assertEqual("首页驾驶舱", config.title)
        self.assertTrue(config.enabled)
        self.assertEqual(300, config.refresh_seconds)
        self.assertEqual(
            ("kpi_offline_mtd", "kpi_channel_mtd", "kpi_annual_progress",
             "trend_region_daily", "bar_channel_mtd"),
            tuple(card.card for card in config.cards),
        )
        self.assertEqual((4, 4, 4, 8, 4), tuple(card.span for card in config.cards))
        self.assertEqual("区域日销趋势", config.cards[3].title)
        # Unknown dashboards get the minimal default.
        self.assertTrue(source.get_dashboard("l2-region").enabled)

    def test_bad_yaml_raises(self):
        path = self._write_seed("l1-cockpit:\n  cards: [unbalanced\n")

        with self.assertRaises(DashboardConfigError):
            FileDashboardSource(path)

    def test_empty_seed_yields_defaults(self):
        path = self._write_seed("")

        source = FileDashboardSource(path)

        config = source.get_dashboard("l1-cockpit")
        self.assertTrue(config.enabled)
        self.assertEqual((), config.cards)


class NacosDashboardSourceTests(unittest.TestCase):
    def test_reads_a_published_entry(self):
        client = _FakeNacosClient(
            {("l1-cockpit.yaml", BI_GROUP): _l1_cockpit_entry_yaml()}
        )
        source = NacosDashboardSource(server="nacos:8848", client=client)

        config = source.get_dashboard("l1-cockpit")

        self.assertEqual("首页驾驶舱", config.title)
        self.assertTrue(config.enabled)
        self.assertEqual(300, config.refresh_seconds)
        self.assertEqual(5, len(config.cards))
        self.assertEqual(8, config.cards[3].span)

    def test_queries_the_bi_group_with_yaml_data_id(self):
        client = _FakeNacosClient()
        source = NacosDashboardSource(server="nacos:8848", client=client)

        source.get_dashboard("l1-cockpit")

        self.assertEqual([("l1-cockpit.yaml", BI_GROUP)], client.requests)

    def test_falls_back_when_the_client_raises(self):
        fallback = StaticDashboardSource({"l1-cockpit": {"enabled": False}})
        source = NacosDashboardSource(
            server="nacos:8848", client=_UnreachableClient(), fallback=fallback
        )

        self.assertFalse(source.get_dashboard("l1-cockpit").enabled)

    def test_falls_back_when_the_content_is_empty(self):
        client = _FakeNacosClient({("l1-cockpit.yaml", BI_GROUP): ""})
        fallback = StaticDashboardSource({"l1-cockpit": {"enabled": False}})
        source = NacosDashboardSource(
            server="nacos:8848", client=client, fallback=fallback
        )

        self.assertFalse(source.get_dashboard("l1-cockpit").enabled)

    def test_get_dashboard_is_cached_within_the_ttl(self):
        client = _FakeNacosClient(
            {("l1-cockpit.yaml", BI_GROUP): _l1_cockpit_entry_yaml()}
        )
        source = NacosDashboardSource(server="nacos:8848", client=client)

        source.get_dashboard("l1-cockpit")
        source.get_dashboard("l1-cockpit")

        self.assertEqual(1, len(client.requests))

    def test_get_dashboard_refetches_after_the_ttl(self):
        now = [0.0]
        client = _FakeNacosClient(
            {("l1-cockpit.yaml", BI_GROUP): _l1_cockpit_entry_yaml()}
        )
        source = NacosDashboardSource(
            server="nacos:8848", client=client,
            ttl_seconds=30.0, monotonic=lambda: now[0],
        )

        source.get_dashboard("l1-cockpit")
        now[0] = 31.0
        source.get_dashboard("l1-cockpit")

        self.assertEqual(2, len(client.requests))

    def test_fallback_resolution_after_a_failed_read_is_cached(self):
        # Nacos down -> the fallback/minimal default resolution IS cached
        # within the TTL: a dead registry must cost its connection penalty
        # at most once per window (that penalty is the 2026-09-14 20s bug).
        class _FlakyClient:
            def __init__(self):
                self.calls = 0

            def get_config(self, data_id, group):
                self.calls += 1
                raise RuntimeError("connection refused")

        client = _FlakyClient()
        source = NacosDashboardSource(server="nacos:8848", client=client)

        source.get_dashboard("l1-cockpit")
        source.get_dashboard("l1-cockpit")

        self.assertEqual(1, client.calls)

    def test_parse_errors_are_never_cached(self):
        client = _FakeNacosClient(
            {("l1-cockpit.yaml", BI_GROUP): "cards: [unbalanced\n"}
        )
        source = NacosDashboardSource(server="nacos:8848", client=client)

        for _ in range(2):
            with self.assertRaises(DashboardConfigError):
                source.get_dashboard("l1-cockpit")

        self.assertEqual(2, len(client.requests))

    def test_dashboard_ids_are_cached(self):
        class _CountingSource(StaticDashboardSource):
            def __init__(self, mapping):
                super().__init__(mapping)
                self.calls = 0

            def dashboard_ids(self):
                self.calls += 1
                return super().dashboard_ids()

        fallback = _CountingSource({"l1-cockpit": {}})
        source = NacosDashboardSource(
            server="nacos:8848", client=_FakeNacosClient(), fallback=fallback
        )

        source.dashboard_ids()
        source.dashboard_ids()

        self.assertEqual(1, fallback.calls)

    def test_minimal_default_when_missing_empty_or_unreachable(self):
        clients = (
            _FakeNacosClient(),  # entry missing
            _FakeNacosClient({("l1-cockpit.yaml", BI_GROUP): ""}),  # empty body
            _UnreachableClient(),  # registry down
        )
        for client in clients:
            with self.subTest(client=type(client).__name__):
                source = NacosDashboardSource(server="nacos:8848", client=client)

                config = source.get_dashboard("l1-cockpit")

                self.assertEqual("l1-cockpit", config.dashboard_id)
                self.assertTrue(config.enabled)
                self.assertEqual((), config.cards)


class BuildDashboardConfigSourceTests(_SeedFileTestCase):
    def test_nacos_backend_when_server_configured(self):
        source = build_dashboard_config_source(
            {"PUBLIC_DATA_NACOS_SERVER": "nacos:8848"}
        )

        self.assertIsInstance(source, NacosDashboardSource)

    def test_nacos_backend_uses_the_bi_group_not_the_pipelines_group(self):
        # compose pins PUBLIC_DATA_NACOS_GROUP to PIPELINES for the sync
        # services; the dashboard registry must ignore it entirely.
        source = build_dashboard_config_source({
            "PUBLIC_DATA_NACOS_SERVER": "nacos:8848",
            "PUBLIC_DATA_NACOS_GROUP": "PIPELINES",
        })

        self.assertIsInstance(source, NacosDashboardSource)
        self.assertEqual(BI_GROUP, source._group)

    def test_nacos_backend_falls_back_to_the_seed_file(self):
        path = self._write_seed("l1-cockpit:\n  enabled: false\n")
        source = build_dashboard_config_source({
            "PUBLIC_DATA_NACOS_SERVER": "nacos:8848",
            "PUBLIC_DATA_BI_SEED": path,
        })

        self.assertIsInstance(source, NacosDashboardSource)
        self.assertIsInstance(source._fallback, FileDashboardSource)

    def test_missing_seed_file_yields_no_fallback(self):
        source = build_dashboard_config_source({
            "PUBLIC_DATA_NACOS_SERVER": "nacos:8848",
            "PUBLIC_DATA_BI_SEED": "/no/such/bi.seed.yaml",
        })

        self.assertIsInstance(source, NacosDashboardSource)
        self.assertIsNone(source._fallback)

    def test_file_backend_when_only_seed_configured(self):
        path = self._write_seed("l1-cockpit:\n  enabled: false\n")
        source = build_dashboard_config_source({"PUBLIC_DATA_BI_SEED": path})

        self.assertIsInstance(source, FileDashboardSource)
        self.assertFalse(source.get_dashboard("l1-cockpit").enabled)

    def test_static_empty_backend_when_nothing_configured(self):
        source = build_dashboard_config_source({})

        self.assertIsInstance(source, StaticDashboardSource)
        config = source.get_dashboard("l1-cockpit")
        self.assertTrue(config.enabled)
        self.assertEqual((), config.cards)


class PublishDashboardsTests(unittest.TestCase):
    def test_publishes_each_entry(self):
        client = _FakeNacosClient()

        count = publish_dashboards(client, {"l1-cockpit": _l1_cockpit()})

        self.assertEqual(1, count)
        data_id, group, content, config_type = client.published[0]
        self.assertEqual("l1-cockpit.yaml", data_id)
        self.assertEqual(BI_GROUP, group)
        self.assertEqual("yaml", config_type)
        self.assertIn("kpi_offline_mtd", content)
        self.assertIn("trend_region_daily", content)

    def test_publishes_every_entry_without_if_missing(self):
        client = _FakeNacosClient(
            {("l1-cockpit.yaml", BI_GROUP): "enabled: true\n"}
        )
        mapping = {
            "l1-cockpit": {"enabled": False},
            "l2-region": {"title": "区域看板"},
        }

        count = publish_dashboards(client, mapping)

        self.assertEqual(2, count)
        self.assertEqual(
            {"l1-cockpit.yaml", "l2-region.yaml"},
            {data_id for data_id, _, _, _ in client.published},
        )

    def test_if_missing_skips_existing_data_ids(self):
        client = _FakeNacosClient(
            {("l1-cockpit.yaml", BI_GROUP): "enabled: true\n"}
        )
        mapping = {
            "l1-cockpit": {"enabled": False},
            "l2-region": {"title": "区域看板"},
        }

        count = publish_dashboards(client, mapping, if_missing=True)

        self.assertEqual(1, count)
        self.assertEqual(
            [("l2-region.yaml", BI_GROUP)],
            [(data_id, group) for data_id, group, _, _ in client.published],
        )

    def test_rejects_invalid_entry(self):
        with self.assertRaises(DashboardConfigError):
            publish_dashboards(_FakeNacosClient(), {"x": {"enabled": "nope"}})


class PublishBiCliTests(unittest.TestCase):
    def test_calls_the_wrapper_with_the_default_seed_and_prints_count(self):
        output = io.StringIO()
        with patch("common.public_data.cli.publish_bi_seed",
                   return_value=2) as publish:
            with redirect_stdout(output):
                main(["publish-bi"])

        publish.assert_called_once_with(
            "docker/integration/bi.seed.yaml", if_missing=False
        )
        self.assertIn("published=2 dashboards", output.getvalue())

    def test_forwards_seed_and_if_missing_flags(self):
        output = io.StringIO()
        with patch("common.public_data.cli.publish_bi_seed",
                   return_value=0) as publish:
            with redirect_stdout(output):
                main(["publish-bi", "--seed", "/seeds/bi.yaml", "--if-missing"])

        publish.assert_called_once_with("/seeds/bi.yaml", if_missing=True)
        self.assertIn("published=0 dashboards", output.getvalue())

    def test_failure_is_safe(self):
        output = io.StringIO()
        with patch("common.public_data.cli.publish_bi_seed",
                   side_effect=RuntimeError("secret-host")):
            with redirect_stdout(output):
                with self.assertRaises(SystemExit) as raised:
                    main(["publish-bi", "--seed", "/seeds/bi.yaml"])

        self.assertNotEqual(0, raised.exception.code)
        text = output.getvalue()
        self.assertIn("status=failed", text)
        self.assertIn("code=config_error", text)
        self.assertNotIn("secret-host", text)
        self.assertNotIn("Traceback", text)


if __name__ == "__main__":
    unittest.main()
