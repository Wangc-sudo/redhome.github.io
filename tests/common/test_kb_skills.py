# -*- coding: utf-8 -*-
"""Agent Skills 注册表测试（2026-11-02 标准化 Task 1）。

核心钉住：compile_tools() 产物与迁移前 TOOLS 快照逐字段相等
（tests/common/fixtures/kb_tools_snapshot.json）——flat 模式行为零变化。
"""

import json
import unittest
from pathlib import Path

from common.kb_qa.agent import TOOLS, ToolExecutor
from common.kb_qa.skills import (
    SkillRegistryError, business_tool_names, compile_tools, export_tools,
    get_skill, load_registry, validate_handlers)

SNAPSHOT = Path(__file__).parent / "fixtures" / "kb_tools_snapshot.json"


def _write_skill(root, name, *, enabled=True, tags="[business]",
                 skill_name=None, order=1):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_name or name}\n"
        "description: 测试技能\n"
        f"tags: {tags}\n"
        f"enabled: {'true' if enabled else 'false'}\n"
        f"order: {order}\n"
        "parameters:\n"
        "  type: object\n"
        "  properties: {}\n"
        "---\n"
        "正文\n", encoding="utf-8")


class CompileEquivalenceTests(unittest.TestCase):
    def test_compile_tools_matches_snapshot(self):
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(snapshot, compile_tools())
        self.assertEqual(snapshot, TOOLS)

    def test_registry_covers_snapshot_names(self):
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        expected = {t["function"]["name"] for t in snapshot}
        self.assertEqual(expected, set(load_registry()))

    def test_handlers_exist_for_all_enabled_skills(self):
        validate_handlers(ToolExecutor)  # 不抛即通过
        for name in load_registry():
            self.assertTrue(hasattr(ToolExecutor, f"_tool_{name}"), name)


class WhitelistDerivationTests(unittest.TestCase):
    """Task 2：三处硬编码白名单收敛到注册表后的派生等价。"""

    def test_business_tools_match_legacy_set(self):
        self.assertEqual(
            {"daily_sales", "channel_sales", "channel_yoy", "query_sql",
             "sku_sales", "sku_inventory"}, set(business_tool_names()))

    def test_export_tools_match_legacy_set(self):
        self.assertEqual(
            {"daily_sales", "channel_sales", "channel_yoy", "query_sql",
             "product_stats", "sku_sales", "sku_inventory"},
            set(export_tools()))

    def test_tools_for_narrow_equals_non_business(self):
        from common.kb_web.service import tools_for

        narrow = {t["function"]["name"] for t in tools_for(False)}
        expected = set(load_registry()) - set(business_tool_names())
        self.assertEqual(expected, narrow)
        full = {t["function"]["name"] for t in tools_for(True)}
        self.assertEqual(set(load_registry()), full)

    def test_snapshot_declarations(self):
        self.assertEqual("daily_report_offline",
                         get_skill("daily_sales").snapshot)
        self.assertEqual("dataset", get_skill("daily_sales").snapshot_kind)
        self.assertEqual("kb_product_structured",
                         get_skill("product_stats").snapshot)
        self.assertEqual("knowledge", get_skill("product_stats").snapshot_kind)
        self.assertIsNone(get_skill("query_sql").snapshot)


class DateAnchorFilterTests(unittest.TestCase):
    def test_pure_date_columns_filtered(self):
        from common.kb_qa.result import from_payload

        self.assertIsNone(from_payload("query_sql", {
            "rows": [{"today": "2026-11-02", "yesterday": "2026-11-01"}]}))

    def test_business_columns_kept(self):
        from common.kb_qa.result import from_payload

        result = from_payload("query_sql", {
            "rows": [{"person_name": "余云涛", "monthly_target": 500000},
                     {"person_name": "李树军", "monthly_target": 930000}]})
        self.assertIsNotNone(result)


class RegistryValidationTests(unittest.TestCase):
    def test_name_must_match_directory(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root, "alpha", skill_name="beta")
            with self.assertRaises(SkillRegistryError):
                load_registry(root)

    def test_invalid_tags_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root, "alpha", tags="[unknown_tag]")
            with self.assertRaises(SkillRegistryError):
                load_registry(root)

    def test_enabled_false_excluded_from_compile(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root, "alpha", order=1)
            _write_skill(root, "beta", enabled=False, order=2)
            names = [t["function"]["name"]
                     for t in compile_tools(directory=root)]
            self.assertEqual(["alpha"], names)
            both = [t["function"]["name"]
                    for t in compile_tools(enabled_only=False, directory=root)]
            self.assertEqual(["alpha", "beta"], both)

    def test_missing_handler_raises(self):
        class _Empty:
            pass

        with self.assertRaises(SkillRegistryError):
            validate_handlers(_Empty)


if __name__ == "__main__":
    unittest.main()
