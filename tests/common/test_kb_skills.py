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
    SkillRegistryError, compile_tools, load_registry, validate_handlers)

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
