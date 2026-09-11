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


if __name__ == "__main__":
    unittest.main()
