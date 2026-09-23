# -*- coding: utf-8 -*-
"""transforms.melt_records 与 manifest._parse_transform 单测。"""

import unittest
from datetime import date

from common.public_data.manifest import ManifestError, _parse_transform
from common.public_data.transforms import melt_records


def _transform(date_columns, **extra):
    t = {
        "type": "melt",
        "year": 2026,
        "month": 9,
        "date_columns": date_columns,
        "value_column": "sales_amount",
        "inject": {"region": "线下总经办&省外"},
    }
    t.update(extra)
    return t


def _record(fields, rid="r1"):
    return {"id": rid, "fields": fields}


class MeltDailyTest(unittest.TestCase):
    """daily 缺省语义：与旧版行为逐字一致（无序、原值透传、None 跳过）。"""

    def test_default_daily_passthrough(self):
        t = _transform({"2日": 2, "1日": 1})
        records = [_record({"板块": "省外", "1日": "100", "2日": "200", "3日": None})]
        rows = melt_records(records, t)
        self.assertEqual(len(rows), 2)
        # 保持 date_columns 声明顺序，不重排
        self.assertEqual(rows[0]["fields"]["business_date"], date(2026, 9, 2))
        self.assertEqual(rows[0]["fields"]["sales_amount"], "200")  # 原值透传
        self.assertEqual(rows[1]["fields"]["sales_amount"], "100")
        self.assertEqual(rows[0]["fields"]["region"], "线下总经办&省外")
        self.assertEqual(rows[0]["id"], "r1_02")


class MeltCumulativeTest(unittest.TestCase):
    def test_first_cell_asis_then_diffs(self):
        t = _transform(
            {"1日": 1, "2日": 2, "3日": 3}, value_semantics="cumulative"
        )
        records = [_record({"板块": "省外", "1日": "100", "2日": "250", "3日": "400"})]
        rows = melt_records(records, t)
        amounts = [r["fields"]["sales_amount"] for r in rows]
        self.assertEqual(amounts, [100.0, 150.0, 150.0])
        self.assertEqual(
            [r["fields"]["business_date"] for r in rows],
            [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)],
        )

    def test_gap_days_diff_against_last_filled(self):
        # 18/19 日未填：20日 = 20日格 − 17日格
        t = _transform(
            {"17日": 17, "18日": 18, "19日": 19, "20日": 20},
            value_semantics="cumulative",
        )
        records = [
            _record({"板块": "总经办", "17日": "3322147", "20日": "3500000"})
        ]
        rows = melt_records(records, t)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["fields"]["sales_amount"], 3322147.0)
        self.assertEqual(rows[1]["fields"]["sales_amount"], 177853.0)

    def test_unsorted_date_columns_sorted_for_diff(self):
        # date_columns 声明乱序：差值必须按日期序计算
        t = _transform(
            {"3日": 3, "1日": 1, "2日": 2}, value_semantics="cumulative"
        )
        records = [_record({"板块": "省外", "1日": 100, "2日": 300, "3日": 600})]
        rows = melt_records(records, t)
        self.assertEqual(
            [r["fields"]["sales_amount"] for r in rows], [100.0, 200.0, 300.0]
        )

    def test_thousands_separator_and_blank_and_non_numeric(self):
        t = _transform(
            {"1日": 1, "2日": 2, "3日": 3, "4日": 4},
            value_semantics="cumulative",
        )
        records = [
            _record({"板块": "省外", "1日": "3,322,147", "2日": "", "3日": "—", "4日": "3400000"})
        ]
        rows = melt_records(records, t)
        # 空串/非数字按未填跳过；4日 diff 对 1日
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["fields"]["sales_amount"], 3322147.0)
        self.assertEqual(rows[1]["fields"]["sales_amount"], 3400000.0 - 3322147.0)

    def test_decreasing_cumulative_yields_negative(self):
        t = _transform({"1日": 1, "2日": 2}, value_semantics="cumulative")
        records = [_record({"板块": "省外", "1日": 500, "2日": 400})]
        rows = melt_records(records, t)
        self.assertEqual(rows[1]["fields"]["sales_amount"], -100.0)

    def test_id_reflects_day(self):
        t = _transform({"17日": 17}, value_semantics="cumulative")
        rows = melt_records([_record({"板块": "省外", "17日": "1"}, rid="abc")], t)
        self.assertEqual(rows[0]["id"], "abc_17")


class ParseTransformSemanticsTest(unittest.TestCase):
    def _raw(self, semantics=None):
        raw = {
            "type": "melt",
            "year": 2026,
            "month": 9,
            "date_columns": {"1日": 1},
            "value_column": "sales_amount",
        }
        if semantics is not None:
            raw["value_semantics"] = semantics
        return raw

    def test_default_daily(self):
        parsed = _parse_transform(self._raw(), "sheet[0]")
        self.assertEqual(parsed["value_semantics"], "daily")

    def test_cumulative_accepted(self):
        parsed = _parse_transform(self._raw("cumulative"), "sheet[0]")
        self.assertEqual(parsed["value_semantics"], "cumulative")

    def test_invalid_rejected(self):
        with self.assertRaisesRegex(ManifestError, "value_semantics"):
            _parse_transform(self._raw("weekly"), "sheet[0]")


if __name__ == "__main__":
    unittest.main()
