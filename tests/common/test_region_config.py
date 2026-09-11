"""Tests for the business-region config (seed + Nacos overlay)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.region_config import (
    RegionConfig,
    RegionConfigError,
    apply_region_overlay,
    build_nacos_region_overlay,
    load_region_seed,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_PATH = _REPO_ROOT / "docker" / "integration" / "regions.seed.json"


def _seed_file(test_case, document):
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    with handle:
        handle.write(
            document if isinstance(document, str)
            else json.dumps(document, ensure_ascii=False)
        )
    test_case.addCleanup(os.unlink, handle.name)
    return handle.name


def _region_doc(**overrides):
    doc = {
        "display": "杭州",
        "tableUrl": "https://example.com/table",
        "robotCode": "rc",
        "openConversationId": "conv",
        "aliases": {"张三丰": "老张"},
        "ccUserIds": ["cc-1"],
        "remindHour": 18,
        "checkHour": 20,
    }
    doc.update(overrides)
    return doc


def _write_seed(test_case, regions):
    return _seed_file(test_case, {"version": 1, "regions": regions})


class ShippedRegionSeedTests(unittest.TestCase):

    def test_shipped_seed_defines_hangzhou(self):
        configs = load_region_seed(_SEED_PATH)
        self.assertEqual(list(configs), ["hangzhou"])
        cfg = configs["hangzhou"]
        self.assertEqual(cfg.display, "杭州")
        self.assertEqual((cfg.remind_hour, cfg.check_hour), (18, 20))

    def test_shipped_seed_keeps_placeholders_out_of_git(self):
        """真实 robotCode / conversationId 只走 Nacos，不进种子。"""
        document = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
        for cfg in document["regions"].values():
            self.assertTrue(cfg["robotCode"].startswith("<"))
            self.assertTrue(cfg["openConversationId"].startswith("<"))

    def test_shipped_seed_carries_leaderboard_fields(self):
        cfg = load_region_seed(_SEED_PATH)["hangzhou"]
        self.assertEqual(cfg.dept_order, ("杭中", "滨萧", "余杭"))
        self.assertEqual(cfg.dept_label, {"杭州运营总监": "运营总监"})
        self.assertEqual(cfg.broadcast_exclude, ("余发兴",))


class LoadRegionSeedTests(unittest.TestCase):

    def test_parses_a_full_region(self):
        path = _write_seed(self, {"r1": _region_doc()})
        cfg = load_region_seed(path)["r1"]
        self.assertEqual(cfg.region, "r1")
        self.assertEqual(cfg.aliases, {"张三丰": "老张"})
        self.assertEqual(cfg.cc_user_ids, ("cc-1",))
        self.assertEqual(cfg.remind_hour, 18)

    def test_defaults_hours(self):
        doc = _region_doc()
        del doc["remindHour"], doc["checkHour"]
        path = _write_seed(self, {"r1": doc})
        cfg = load_region_seed(path)["r1"]
        self.assertEqual((cfg.remind_hour, cfg.check_hour), (18, 20))

    def test_rejects_wrong_version(self):
        path = _seed_file(self, {"version": 2, "regions": {"r1": _region_doc()}})
        with self.assertRaises(RegionConfigError):
            load_region_seed(path)

    def test_rejects_empty_regions(self):
        path = _seed_file(self, {"version": 1, "regions": {}})
        with self.assertRaises(RegionConfigError):
            load_region_seed(path)

    def test_rejects_missing_required_field(self):
        doc = _region_doc()
        del doc["robotCode"]
        path = _write_seed(self, {"r1": doc})
        with self.assertRaises(RegionConfigError):
            load_region_seed(path)

    def test_rejects_bad_hour(self):
        path = _write_seed(self, {"r1": _region_doc(remindHour=24)})
        with self.assertRaises(RegionConfigError):
            load_region_seed(path)

    def test_rejects_non_string_map_aliases(self):
        path = _write_seed(self, {"r1": _region_doc(aliases={"a": 1})})
        with self.assertRaises(RegionConfigError):
            load_region_seed(path)

    def test_leaderboard_fields_default_to_empty(self):
        path = _write_seed(self, {"r1": _region_doc()})
        cfg = load_region_seed(path)["r1"]
        self.assertEqual(cfg.dept_order, ())
        self.assertEqual(cfg.dept_label, {})
        self.assertEqual(cfg.broadcast_exclude, ())
        self.assertEqual(cfg.leaderboard_url, "")

    def test_rejects_bad_leaderboard_fields(self):
        for override in (
            {"deptOrder": "杭中"},
            {"deptLabel": ["运营总监"]},
            {"broadcastExclude": [1]},
            {"leaderboardUrl": 42},
        ):
            path = _write_seed(self, {"r1": _region_doc(**override)})
            with self.assertRaises(RegionConfigError):
                load_region_seed(path)


class RegionOverlayTests(unittest.TestCase):

    def _configs(self):
        return {
            "hangzhou": RegionConfig(
                region="hangzhou", display="杭州",
                table_url="https://seed/table", robot_code="rc-seed",
                open_conversation_id="conv-seed", aliases={}, cc_user_ids=(),
            ),
        }

    def test_overlay_overrides_fields_and_keeps_the_rest(self):
        merged = apply_region_overlay(
            self._configs(),
            lambda data_id: {"robotCode": "rc-nacos"},
        )
        cfg = merged["hangzhou"]
        self.assertEqual(cfg.robot_code, "rc-nacos")
        self.assertEqual(cfg.open_conversation_id, "conv-seed")

    def test_overlay_miss_keeps_seed_values(self):
        merged = apply_region_overlay(self._configs(), lambda data_id: None)
        self.assertEqual(merged["hangzhou"].robot_code, "rc-seed")

    def test_overlay_error_is_fail_open(self):
        def boom(data_id):
            raise RuntimeError("nacos down")

        merged = apply_region_overlay(self._configs(), boom)
        self.assertEqual(merged["hangzhou"].robot_code, "rc-seed")

    def test_invalid_overlay_content_raises(self):
        with self.assertRaises(RegionConfigError):
            apply_region_overlay(
                self._configs(), lambda data_id: {"remindHour": 99},
            )

    def test_remote_only_regions_are_ignored(self):
        """区域集合由种子定义：Nacos 不得新增区域。"""
        calls = []
        merged = apply_region_overlay(
            self._configs(),
            lambda data_id: calls.append(data_id) or None,
        )
        self.assertEqual(list(merged), ["hangzhou"])
        self.assertEqual(calls, ["region-hangzhou.yaml"])

    def test_build_overlay_is_none_without_a_server(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(build_nacos_region_overlay())


if __name__ == "__main__":
    unittest.main()
