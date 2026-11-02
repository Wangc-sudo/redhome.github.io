# -*- coding: utf-8 -*-
"""评测集格式校验（CI 零 IO）：cases.jsonl 结构、去重、工具名在注册表内。"""

import json
import unittest
from pathlib import Path

from common.kb_qa.skills import load_registry

CASES = Path(__file__).parents[1] / "kb_qa_eval" / "cases.jsonl"


class EvalCasesFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = [json.loads(line) for line in
                     CASES.read_text(encoding="utf-8").splitlines()
                     if line.strip()]

    def test_at_least_100_cases(self):
        self.assertGreaterEqual(len(self.cases), 100)

    def test_schema_and_unique_questions(self):
        questions = set()
        for case in self.cases:
            self.assertEqual(
                {"question", "expected_tools", "must_contain",
                 "must_not_contain"}, set(case))
            self.assertTrue(case["question"].strip())
            self.assertNotIn(case["question"], questions)
            questions.add(case["question"])
            for key in ("expected_tools", "must_contain",
                        "must_not_contain"):
                self.assertIsInstance(case[key], list)
                for token in case[key]:
                    self.assertIsInstance(token, str)

    def test_expected_tools_in_registry(self):
        names = set(load_registry())
        for case in self.cases:
            for tool in case["expected_tools"]:
                self.assertIn(tool, names, case["question"])

    def test_no_contradictory_assertions(self):
        for case in self.cases:
            overlap = set(case["must_contain"]) & set(
                case["must_not_contain"])
            self.assertFalse(overlap, case["question"])


if __name__ == "__main__":
    unittest.main()
