"""榜单页日期选择月历（page_calendar）离线单测。

2026-10 历：1 日周四；工作日假定为周一~周五（测试自造集合，不查库）。
"""

import unittest
from datetime import date, timedelta

from common.daily_robot.page_calendar import (
    archive_name,
    build_date_nav_panel,
)


def _october_weekdays():
    days = set()
    day = date(2026, 10, 1)
    while day.month == 10:
        if day.weekday() < 5:
            days.add(day)
        day += timedelta(days=1)
    return days


class ArchiveNameTests(unittest.TestCase):
    def test_format(self):
        self.assertEqual("hangzhou-2026-10-09.html",
                         archive_name("hangzhou", date(2026, 10, 9)))


class BuildDateNavPanelTests(unittest.TestCase):
    def test_workday_link_weekend_gray_current_highlight(self):
        panel = build_date_nav_panel(
            page_date=date(2026, 10, 9),          # 周五，工作日
            workday_dates=_october_weekdays(),
            available_dates={date(2026, 10, 9)},  # 仅当天有存档
            archive_stem="hangzhou",
        )
        # 当天：蓝色加粗链接 + 高亮描边
        self.assertIn(
            '<td class="cur"><a href="hangzhou-2026-10-09.html">9</a></td>',
            panel)
        # 已过的工作日但无存档：蓝字不可点（10-08 周四）
        self.assertIn('<td><span class="wd">8</span></td>', panel)
        # 周末灰（10-10 周六、10-11 周日）
        self.assertIn('<td><span class="off">10</span></td>', panel)
        self.assertIn('<td><span class="off">11</span></td>', panel)
        # 无跨月存档 → 无上下月导航
        self.assertNotIn("«", panel)
        self.assertNotIn("»", panel)
        self.assertIn("蓝色为工作日", panel)

    def test_archived_workday_is_clickable(self):
        panel = build_date_nav_panel(
            page_date=date(2026, 10, 9),
            workday_dates=_october_weekdays(),
            available_dates={date(2026, 10, 8), date(2026, 10, 9)},
            archive_stem="qudao",
        )
        self.assertIn('<a href="qudao-2026-10-08.html">8</a>', panel)

    def test_prev_next_month_nav_follows_available(self):
        available = {date(2026, 9, 30), date(2026, 10, 9),
                     date(2026, 11, 2)}
        panel = build_date_nav_panel(
            page_date=date(2026, 10, 9),
            workday_dates=_october_weekdays(),
            available_dates=available,
            archive_stem="hangzhou",
        )
        # 上月导航指向 9 月最近存档日；下月指向 11 月最早存档日
        self.assertIn('href="hangzhou-2026-09-30.html">« 2026-09</a>', panel)
        self.assertIn('href="hangzhou-2026-11-02.html">2026-11 »</a>', panel)

    def test_guards(self):
        self.assertEqual("", build_date_nav_panel(
            page_date=date(2026, 10, 9), workday_dates=set(),
            available_dates=set(), archive_stem=""))
        # 邻月溢出错位灰显（9-28~9-30 出现在 10 月网格第一周）
        panel = build_date_nav_panel(
            page_date=date(2026, 10, 9), workday_dates=_october_weekdays(),
            available_dates=set(), archive_stem="hangzhou")
        self.assertIn('<td><span class="adj">28</span></td>', panel)


if __name__ == "__main__":
    unittest.main()
