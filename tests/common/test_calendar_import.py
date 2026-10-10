# -*- coding: utf-8 -*-
"""calendar_import（holiday-cn 拉取 + 裁决行映射）离线单测。"""

import json
import unittest
from datetime import date

from common.public_data.calendar_import import (
    CalendarImportError,
    fetch_holiday_cn,
    map_days_to_overrides,
)

#: 2026 年 1-2 月真实法定安排（国务院办公厅通知）：元旦 1/1-3 休、
#: 1/4（周日）补班；春节 2/15-23 休、2/14（周六）与 2/28（周六）补班。
_FIXTURE_2026 = (
    [{"date": f"2026-01-0{d}", "name": "元旦", "isOffDay": True}
     for d in (1, 2, 3)]
    + [{"date": "2026-01-04", "name": "元旦调休", "isOffDay": False}]
    + [{"date": f"2026-02-{d:02d}", "name": "春节", "isOffDay": True}
       for d in range(15, 24)]
    + [{"date": "2026-02-14", "name": "春节调休", "isOffDay": False},
       {"date": "2026-02-28", "name": "春节调休", "isOffDay": False}]
)


class MapDaysToOverridesTests(unittest.TestCase):
    def test_holidays_and_makeup_workdays(self):
        overrides = map_days_to_overrides(2026, _FIXTURE_2026)
        jan = [row for row in overrides if row[0].month == 1]
        self.assertEqual(
            [(date(2026, 1, d), 0, "元旦") for d in (1, 2, 3)]
            + [(date(2026, 1, 4), 1, "元旦调休（调休上班）")],
            jan,
        )
        feb = [row for row in overrides if row[0].month == 2]
        self.assertEqual(9, sum(1 for _, is_workday, _ in feb if not is_workday))
        self.assertEqual(2, sum(1 for _, is_workday, _ in feb if is_workday))
        # 按日期升序
        self.assertEqual(
            sorted(day for day, _, _ in overrides),
            [day for day, _, _ in overrides],
        )

    def test_out_of_year_days_are_ignored(self):
        days = _FIXTURE_2026 + [
            {"date": "2025-12-29", "name": "元旦调休", "isOffDay": False},
            {"date": "2027-01-01", "name": "元旦", "isOffDay": True},
        ]
        overrides = map_days_to_overrides(2026, days)
        self.assertTrue(all(day.year == 2026 for day, _, _ in overrides))

    def test_malformed_entry_raises(self):
        with self.assertRaises(CalendarImportError):
            map_days_to_overrides(2026, [{"date": "2026-01-01"}])


class FetchHolidayCnTests(unittest.TestCase):
    def test_retries_then_succeeds(self):
        attempts = []

        def flaky(url):
            attempts.append(url)
            if len(attempts) < 3:
                raise OSError("reset")
            return json.dumps({"days": _FIXTURE_2026}).encode("utf-8")

        days = fetch_holiday_cn(2026, opener=flaky, sleep=lambda s: None)
        self.assertEqual(_FIXTURE_2026, days)
        self.assertEqual(3, len(attempts))

    def test_raises_after_all_attempts(self):
        def down(url):
            raise OSError("reset")

        with self.assertRaises(CalendarImportError):
            fetch_holiday_cn(2026, opener=down, sleep=lambda s: None)

    def test_rejects_unpublished_year(self):
        def not_found(url):
            return b'{"days": []}'

        with self.assertRaises(CalendarImportError):
            fetch_holiday_cn(2099, opener=not_found, sleep=lambda s: None)


if __name__ == "__main__":
    unittest.main()
