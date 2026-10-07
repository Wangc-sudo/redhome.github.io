"""Tests for the Nacos-backed pipeline registry configuration."""

import os
import tempfile
import unittest

from common.public_data.pipeline_config import (
    DEFAULT_GROUP,
    FileConfigSource,
    NacosConfigSource,
    PipelineConfigError,
    StaticConfigSource,
    backfill_categories,
    build_config_source,
    parse_pipeline_config,
    publish_pipelines,
    resolve_service_id,
)


class _FakeNacosClient:
    def __init__(self, configs=None):
        self.configs = dict(configs or {})
        self.published = []

    def get_config(self, data_id, group):
        return self.configs.get((data_id, group))

    def publish_config(self, data_id, group, content, config_type=None):
        self.configs[(data_id, group)] = content
        self.published.append((data_id, group, content, config_type))


class ParsePipelineConfigTests(unittest.TestCase):
    def test_none_yields_enabled_default(self):
        config = parse_pipeline_config("sync-wdt", None)
        self.assertTrue(config.enabled)
        self.assertEqual(config.service_id, "sync-wdt")
        self.assertEqual(config.kind, "apps")
        self.assertEqual(config.sources, ())

    def test_valid_mapping(self):
        config = parse_pipeline_config("sync-wdt", {
            "enabled": False,
            "kind": "apps",
            "sources": ["wdt"],
            "schedule": "0 2 * * *",
            "reads": ["raw_wdt"],
            "description": "d",
        })
        self.assertFalse(config.enabled)
        self.assertEqual(config.sources, ("wdt",))
        self.assertEqual(config.reads, ("raw_wdt",))
        self.assertEqual(config.schedule, "0 2 * * *")

    def test_rejects_non_mapping(self):
        with self.assertRaises(PipelineConfigError):
            parse_pipeline_config("x", ["not", "a", "map"])

    def test_rejects_non_boolean_enabled(self):
        with self.assertRaises(PipelineConfigError):
            parse_pipeline_config("x", {"enabled": "yes"})

    def test_rejects_unknown_kind(self):
        with self.assertRaises(PipelineConfigError):
            parse_pipeline_config("x", {"kind": "worker"})

    def test_rejects_unknown_source(self):
        with self.assertRaises(PipelineConfigError):
            parse_pipeline_config("x", {"sources": ["ftp"]})

    def test_to_mapping_round_trips(self):
        config = parse_pipeline_config("x", {"enabled": False, "sources": ["wdt"]})
        self.assertEqual(parse_pipeline_config("x", config.to_mapping()), config)

    def test_depends_on_parsed_and_round_trips(self):
        config = parse_pipeline_config(
            "sync-wdt", {"depends_on": ["roll-manifest"]}
        )
        self.assertEqual(config.depends_on, ("roll-manifest",))
        self.assertEqual(
            parse_pipeline_config("sync-wdt", config.to_mapping()), config
        )

    def test_rejects_invalid_depends_on(self):
        with self.assertRaises(PipelineConfigError):
            parse_pipeline_config("x", {"depends_on": "roll-manifest"})
        with self.assertRaises(PipelineConfigError):
            parse_pipeline_config("x", {"depends_on": [""]})
        with self.assertRaises(PipelineConfigError):
            parse_pipeline_config("x", {"depends_on": [42]})


class ConfigSourceTests(unittest.TestCase):
    def test_static_source_default_is_enabled(self):
        source = StaticConfigSource({"sync-wdt": {"enabled": False}})
        self.assertFalse(source.get_pipeline("sync-wdt").enabled)
        self.assertTrue(source.get_pipeline("unknown").enabled)

    def test_file_source_reads_seed(self):
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        )
        tmp.write("sync-wdt:\n  enabled: false\n  sources: [wdt]\n")
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)

        source = FileConfigSource(tmp.name)
        self.assertFalse(source.get_pipeline("sync-wdt").enabled)
        self.assertTrue(source.get_pipeline("other").enabled)

    def test_nacos_source_reads_published_entry(self):
        client = _FakeNacosClient({("sync-wdt.yaml", DEFAULT_GROUP): "enabled: false\n"})
        source = NacosConfigSource(server="nacos:8848", client=client)
        self.assertFalse(source.get_pipeline("sync-wdt").enabled)

    def test_nacos_source_falls_back_when_missing(self):
        fallback = StaticConfigSource({"sync-wdt": {"enabled": False}})
        source = NacosConfigSource(
            server="nacos:8848", client=_FakeNacosClient(), fallback=fallback
        )
        self.assertFalse(source.get_pipeline("sync-wdt").enabled)

    def test_nacos_source_falls_back_when_unreachable(self):
        class _Boom:
            def get_config(self, data_id, group):
                raise RuntimeError("connection refused")

        fallback = StaticConfigSource({"sync-wdt": {"enabled": False}})
        source = NacosConfigSource(
            server="nacos:8848", client=_Boom(), fallback=fallback
        )
        self.assertFalse(source.get_pipeline("sync-wdt").enabled)

    def test_nacos_source_defaults_enabled_without_fallback(self):
        source = NacosConfigSource(server="nacos:8848", client=_FakeNacosClient())
        self.assertTrue(source.get_pipeline("sync-wdt").enabled)


class BuildConfigSourceTests(unittest.TestCase):
    def test_nacos_when_server_configured(self):
        source = build_config_source({"PUBLIC_DATA_NACOS_SERVER": "nacos:8848"})
        self.assertIsInstance(source, NacosConfigSource)

    def test_seed_file_when_only_seed_configured(self):
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        )
        tmp.write("sync-wdt:\n  enabled: false\n")
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)

        source = build_config_source({"PUBLIC_DATA_PIPELINE_SEED": tmp.name})
        self.assertIsInstance(source, FileConfigSource)
        self.assertFalse(source.get_pipeline("sync-wdt").enabled)

    def test_default_when_nothing_configured(self):
        source = build_config_source({})
        self.assertTrue(source.get_pipeline("sync-wdt").enabled)

    def test_resolve_service_id_precedence(self):
        self.assertEqual(resolve_service_id({"PUBLIC_DATA_SERVICE_ID": "a"}), "a")
        self.assertEqual(
            resolve_service_id({"PUBLIC_DATA_SERVICE_ID": "a"}, override="b"), "b"
        )
        self.assertIsNone(resolve_service_id({}))


class PublishPipelinesTests(unittest.TestCase):
    def test_publishes_each_entry(self):
        client = _FakeNacosClient()
        count = publish_pipelines(client, {"sync-wdt": {"enabled": True}})
        self.assertEqual(count, 1)
        self.assertEqual(len(client.published), 1)
        data_id, group, content, config_type = client.published[0]
        self.assertEqual(data_id, "sync-wdt.yaml")
        self.assertEqual(group, DEFAULT_GROUP)
        self.assertEqual(config_type, "yaml")
        self.assertIn("enabled: true", content)

    def test_if_missing_skips_existing(self):
        client = _FakeNacosClient({("sync-wdt.yaml", DEFAULT_GROUP): "enabled: true\n"})
        count = publish_pipelines(
            client, {"sync-wdt": {"enabled": False}}, if_missing=True
        )
        self.assertEqual(count, 0)

    def test_rejects_invalid_entry(self):
        with self.assertRaises(PipelineConfigError):
            publish_pipelines(_FakeNacosClient(), {"x": {"enabled": "nope"}})


class CategoryFieldTests(unittest.TestCase):
    def test_category_parsed_and_round_trips(self):
        config = parse_pipeline_config("robot-hangzhou", {"category": "催办"})
        self.assertEqual(config.category, "催办")
        self.assertEqual(config.to_mapping()["category"], "催办")
        self.assertEqual(
            parse_pipeline_config("robot-hangzhou", config.to_mapping()), config
        )

    def test_category_defaults_to_none_and_omitted(self):
        config = parse_pipeline_config("sync-wdt", {"kind": "apps"})
        self.assertIsNone(config.category)
        self.assertNotIn("category", config.to_mapping())

    def test_rejects_unknown_category(self):
        with self.assertRaises(PipelineConfigError):
            parse_pipeline_config("x", {"category": "别的"})


class BackfillCategoriesTests(unittest.TestCase):
    def setUp(self):
        self.client = _FakeNacosClient({
            ("sync-wdt.yaml", DEFAULT_GROUP):
                "enabled: true\nkind: apps\nschedule: '0 2 * * *'\n",
            ("robot-hangzhou.yaml", DEFAULT_GROUP):
                "enabled: false\nkind: business\ncategory: 催办\n",
            ("mystery-line.yaml", DEFAULT_GROUP): "kind: business\n",
        })

    def test_apply_fills_missing_and_preserves_fields(self):
        result = backfill_categories(
            self.client,
            ("sync-wdt", "robot-hangzhou", "mystery-line", "ghost"),
            apply=True,
        )
        self.assertEqual(["sync-wdt"], result["filled"])
        self.assertEqual(["robot-hangzhou"], result["skipped"])
        self.assertEqual(["mystery-line"], result["unclassified"])

        import yaml
        data = yaml.safe_load(self.client.configs[("sync-wdt.yaml", DEFAULT_GROUP)])
        self.assertEqual("同步", data["category"])
        self.assertEqual("apps", data["kind"])        # 原文字段原样保留
        self.assertEqual("0 2 * * *", data["schedule"])
        # 已配置/未归类/无条目的均不写
        self.assertEqual(1, len(self.client.published))

    def test_dry_run_writes_nothing(self):
        result = backfill_categories(self.client, ("sync-wdt",), apply=False)
        self.assertEqual(["sync-wdt"], result["filled"])
        self.assertEqual([], self.client.published)

    def test_invalid_existing_category_not_clobbered(self):
        client = _FakeNacosClient({
            ("sync-wdt.yaml", DEFAULT_GROUP): "category: 别的\n"
        })
        result = backfill_categories(client, ("sync-wdt",), apply=True)
        self.assertEqual(["sync-wdt"], result["unclassified"])
        self.assertEqual([], client.published)


class SeedCategoryConsistencyTests(unittest.TestCase):
    def test_seed_categories_match_taxonomy_derivation(self):
        """seed 每条目的 category 合法且与 service_id 推导一致（防双源漂移）。"""
        from pathlib import Path

        from common.public_data.pipeline_config import load_seed
        from common.public_data.pipeline_taxonomy import (
            SUPPORTED_CATEGORIES,
            derive_category,
        )

        seed = load_seed(Path("docker/integration/pipelines.seed.yaml"))
        self.assertTrue(seed)
        for service_id, raw in seed.items():
            category = raw.get("category")
            self.assertIn(category, SUPPORTED_CATEGORIES, service_id)
            self.assertEqual(derive_category(service_id), category, service_id)


if __name__ == "__main__":
    unittest.main()
