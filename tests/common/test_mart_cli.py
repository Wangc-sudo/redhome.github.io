"""Tests for the channel/platform CLI entry (``common.daily_robot.mart_cli``).

2026-11-18 线下业务线剥离后，主仓 CLI 只剩渠道（channel-daily /
channel-missing）与平台看门（calendar-watch / freshness-check）命令；
线下提醒/催办/榜单/页面/月目标的命令与其用例已迁 offline-branch 仓。
"""

import unittest
from datetime import datetime
from unittest.mock import Mock

from common.daily_robot.mart_cli import _force_suffix


class ForceSuffixTests(unittest.TestCase):
    """--force（run-once 手动触发）：另起去重键后缀强制重发（2026-10-08 裁决）。"""

    def test_force_flag_yields_manual_suffix(self):
        args = Mock(force=True)
        now = datetime(2026, 10, 8, 9, 30, 5)
        self.assertEqual(_force_suffix(args, now), "manual-20261008093005")

    def test_default_no_suffix(self):
        now = datetime(2026, 10, 8, 9, 30, 5)
        self.assertIsNone(_force_suffix(Mock(force=False), now))
        self.assertIsNone(_force_suffix(Mock(spec=[]), now))  # 无 force 属性


if __name__ == "__main__":
    unittest.main()
