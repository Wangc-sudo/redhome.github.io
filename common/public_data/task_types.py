# -*- coding: utf-8 -*-
"""定时任务结果契约（跨业务线共享）。

2026-11-18 线下业务线剥离：``TaskOutcome`` 原本定义在
``common.daily_robot.mart_tasks``（线下提醒/催办），但线上渠道到齐检查
（``common.daily_robot.channel_missing``）也复用它作为「一次检查的结果」。
线下实现迁出后，把这条纯数据契约下沉到平台层，两边各自引用即可。
"""

from dataclasses import dataclass, field
from datetime import date

__all__ = ["TaskOutcome"]


@dataclass(frozen=True)
class TaskOutcome:
    """一次 remind/check 的结果（供 CLI 打印与测试断言）。"""

    status: str  # "enqueued" | "already_sent" | "all_filled" | "rest_day"
    kind: str
    business_date: date
    unfilled: tuple = field(default_factory=tuple)
    enqueued: bool = False
