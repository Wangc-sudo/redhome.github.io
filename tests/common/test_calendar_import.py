# -*- coding: utf-8 -*-
"""calendar_import（holiday-cn 法定节假日导入，三层防线 C 层）离线单测。"""

import json
import tempfile
import unittest
from pathlib import Path

from common.calendar_utils import generate_rest_days, load_calendar_seed
from common.public_data.calendar_import import (
    CalendarImportError,
    fetch_holiday_cn,
    import_year,
    map_year_to_seed_months,
    merge_seed_document,
)

#: 2026 年 1-2 月真实法定安排（国务院办公厅通知，与种子 2026-01/02 同源）：
#: 元旦 1/1-3 休、1/4（周日）补班；春节 2/15-23 休、2/14（周六）与
#: 2/28（周六）补班。
_FIXTURE_2026 = (
    [{"date": f"2026-01-0{d}", "name": "元旦", "isOffDay": True}
     for d in (1, 2, 3)]
    + [{"date": "2026-01-04", "name": "元旦调休", "isOffDay": False}]
    + [{"date": f"2026-02-{d:02d}", "name": "春节", "isOffDay": True}
       for d in range(15, 24)]
    + [{"date": "2026-02-14", "name": "春节调休", "isOffDay": False},
       {"date": "2026-02-28", "name": "春节调休", "isOffDay": False}]
)


def _seed_file(document):
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    with handle:
        handle.write(json.dumps(document, ensure_ascii=False))
    return Path(handle.name)


class MapYearToSeedMonthsTests(unittest.TestCase):
    def test_matches_shipped_seed_2026_jan_feb(self):
        """映射结果与手工转录的 2026-01/02 种子逐位一致（ground truth）。"""
        months = {
            item["month"]: item
            for item in map_year_to_seed_months(2026, _FIXTURE_2026)
        }
        jan = months[1]
        self.assertEqual([17], jan["bigRestSaturdays"])  # 第 3 个周六
        self.assertEqual([1, 2, 3], jan["holidays"])
        self.assertEqual([4], jan["makeupWorkdays"])
        self.assertEqual(
            [1, 2, 3, 11, 17, 18, 25],
            generate_rest_days(
                2026, 1, big_rest_saturdays=jan["bigRestSaturdays"],
                holidays=jan["holidays"], makeup_workdays=jan["makeupWorkdays"],
            ),
        )
        feb = months[2]
        self.assertEqual([21], feb["bigRestSaturdays"])
        self.assertEqual(list(range(15, 24)), feb["holidays"])
        self.assertEqual([14, 28], feb["makeupWorkdays"])
        self.assertEqual(
            [1, 8, 15, 16, 17, 18, 19, 20, 21, 22, 23],
            generate_rest_days(
                2026, 2, big_rest_saturdays=feb["bigRestSaturdays"],
                holidays=feb["holidays"], makeup_workdays=feb["makeupWorkdays"],
            ),
        )

    def test_out_of_year_days_are_ignored(self):
        days = _FIXTURE_2026 + [
            {"date": "2025-12-29", "name": "元旦调休", "isOffDay": False},
            {"date": "2027-01-01", "name": "元旦", "isOffDay": True},
        ]
        months = map_year_to_seed_months(2026, days)
        # 溢出日期归其所属年：2026-12 不混入 2025 的调休、2026 各月不混入
        # 2027 的节假日（2026-01 的 makeupWorkdays=[4] 是元旦补班真值）
        self.assertEqual([], months[11]["holidays"])
        self.assertEqual([], months[11]["makeupWorkdays"])
        self.assertEqual([1, 2, 3], months[0]["holidays"])


class MergeSeedDocumentTests(unittest.TestCase):
    def test_same_year_replaced_others_kept_and_sorted(self):
        document = {
            "version": 1, "source": "local",
            "months": [
                {"year": 2025, "month": 12, "holidays": []},
                {"year": 2026, "month": 1, "holidays": [99]},
            ],
        }
        new_months = [
            {"year": 2026, "month": month, "holidays": []}
            for month in range(1, 13)
        ]
        _, stats = merge_seed_document(document, new_months, year=2026)
        self.assertEqual({"replaced": 1, "added": 11}, stats)
        keys = [(m["year"], m["month"]) for m in document["months"]]
        self.assertEqual(
            [(2025, 12)] + [(2026, m) for m in range(1, 13)], keys)
        self.assertNotIn(99, document["months"][1]["holidays"])


class MergeBroadcastAdjustTests(unittest.TestCase):
    """合并保留播报口径裁决修正层（法定字段整年替换，人工裁决永不覆盖）。"""

    def test_merge_preserves_broadcast_adjust(self):
        document = {
            "version": 1, "source": "local",
            "months": [{
                "year": 2026, "month": 10,
                "bigRestSaturdays": [17, 31], "holidays": [1],
                "makeupWorkdays": [],
                "broadcastAdjust": {"workdays": [7], "restDays": [31],
                                     "note": "裁决"},
            }],
        }
        new_months = map_year_to_seed_months(2026, _FIXTURE_2026)
        merge_seed_document(document, new_months, year=2026)
        october = next(m for m in document["months"] if m["month"] == 10)
        # 法定字段被导入值替换（大休周六回到第 3 周六锚点、法定假照官方）
        self.assertEqual([17], october["bigRestSaturdays"])
        # 修正层原样保留并写入自证
        self.assertEqual(
            {"workdays": [7], "restDays": [31], "note": "裁决"},
            october["broadcastAdjust"],
        )
        self.assertIn("broadcastAdjust 保留", october["_说明"])
        self.assertIn("裁决", october["_说明"])
        # 无修正层的月份不添加该键
        january = next(m for m in document["months"] if m["month"] == 1)
        self.assertNotIn("broadcastAdjust", january)

    def test_apply_then_load_derives_adjusted_rest(self):
        document = {
            "version": 1, "source": "local",
            "months": [{
                "year": 2026, "month": 1,
                "bigRestSaturdays": [17], "holidays": [1, 2, 3],
                "makeupWorkdays": [4],
                "broadcastAdjust": {"restDays": [10], "note": "测试裁决"},
            }],
        }
        path = _seed_file(document)
        import_year(path, 2026, apply=True, fetcher=lambda year: _FIXTURE_2026)
        rows = load_calendar_seed(path, fallback=False)
        by_month = {(y, m): rest for y, m, rest, _ in rows}
        # 1 月法定 rest [1,2,3,11,17,18,25] + 修正 restDays 10
        self.assertEqual(
            [1, 2, 3, 10, 11, 17, 18, 25], by_month[(2026, 1)])


class ImportYearTests(unittest.TestCase):
    def _document(self):
        return {
            "version": 1, "source": "local",
            "months": [
                {"year": 2026, "month": 1,
                 "bigRestSaturdays": [17], "holidays": [1, 2, 3],
                 "makeupWorkdays": [4]},
                {"year": 2026, "month": 2,
                 "bigRestSaturdays": [21],
                 "holidays": list(range(15, 24)),
                 "makeupWorkdays": [14, 28]},
            ],
        }

    def test_dry_run_does_not_write(self):
        path = _seed_file(self._document())
        before = path.read_text(encoding="utf-8")
        result = import_year(
            path, 2026, apply=False, fetcher=lambda year: _FIXTURE_2026)
        self.assertFalse(result["applied"])
        self.assertEqual({"replaced": 2, "added": 10}, result["stats"])
        self.assertEqual(before, path.read_text(encoding="utf-8"))

    def test_apply_writes_and_reparses(self):
        path = _seed_file(self._document())
        result = import_year(
            path, 2026, apply=True, fetcher=lambda year: _FIXTURE_2026)
        self.assertTrue(result["applied"])
        # 自证 ②：写后可被标准加载链解析（不含兜底，纯种子 12 个月）
        rows = load_calendar_seed(path, fallback=False)
        self.assertEqual(
            [(2026, month) for month in range(1, 13)],
            [(year, month) for year, month, _, _ in rows],
        )

    def test_fetch_retries_then_succeeds(self):
        attempts = []

        def flaky(url):
            attempts.append(url)
            if len(attempts) < 3:
                raise OSError("reset")
            return json.dumps({"days": _FIXTURE_2026}).encode("utf-8")

        days = fetch_holiday_cn(2026, opener=flaky, sleep=lambda s: None)
        self.assertEqual(_FIXTURE_2026, days)
        self.assertEqual(3, len(attempts))

    def test_fetch_raises_after_all_attempts(self):
        def down(url):
            raise OSError("reset")

        with self.assertRaises(CalendarImportError):
            fetch_holiday_cn(2026, opener=down, sleep=lambda s: None)

    def test_fetch_rejects_unpublished_year(self):
        def not_found(url):
            return b'{"days": []}'

        with self.assertRaises(CalendarImportError):
            fetch_holiday_cn(2099, opener=not_found, sleep=lambda s: None)


if __name__ == "__main__":
    unittest.main()
