# -*- coding: utf-8 -*-
"""
工作日历工具（数字中台公共层）
==============================
统一各机器人的「休息日/工作日」判定口径。
日历配置来自各业务 config.json 的 calendar 段: {month, restDays: [日期...]}

用法:
    from common.calendar_utils import Calendar
    cal = Calendar(month=9, rest_days=[6, 13, 19, 25, 26, 27])
    cal.check_month()          # 当月不符抛 CalendarError
    cal.workdays()             # 全月工作日列表
    cal.is_rest(day)           # 是否休息日
    cal.elapsed(include_today) # 已过工作日列表（早报口径）
"""
from datetime import datetime


class CalendarError(RuntimeError):
    pass


class Calendar:
    def __init__(self, month, rest_days):
        self.month = int(month)
        self.rest_days = set(int(d) for d in rest_days)
        # 简化口径：1~30 日；如遇 31 天月份需在业务侧确认
        self._workdays = [d for d in range(1, 31) if d not in self.rest_days]

    @classmethod
    def from_config(cls, cal_cfg):
        """从 config.json 的 calendar 段构造: {month, restDays}"""
        return cls(cal_cfg["month"], cal_cfg.get("restDays", []))

    def check_month(self, now=None):
        """当前月份与配置不符时抛 CalendarError（换月需建新表更新配置）"""
        now = now or datetime.now()
        if now.month != self.month:
            raise CalendarError(
                f"当前月份{now.month}与配置月份{self.month}不符，请为新月建表并更新 config")

    def workdays(self):
        return list(self._workdays)

    def is_rest(self, day):
        return day in self.rest_days

    def elapsed(self, include_today=False, now=None):
        """已过工作日列表。默认不含今日（早报口径：18点前只统计截至昨日）"""
        now = now or datetime.now()
        return [d for d in self._workdays
                if d < now.day or (include_today and d == now.day)]

    def today_state(self, now=None):
        """返回 (now, day, err)。day=None 表示休息日/跨月不可用，err 为提示文本"""
        now = now or datetime.now()
        try:
            self.check_month(now)
        except CalendarError as e:
            return now, None, str(e)
        if self.is_rest(now.day):
            return now, None, None
        return now, now.day, None

    @property
    def total(self):
        return len(self._workdays)
