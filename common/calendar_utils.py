# -*- coding: utf-8 -*-
"""
工作日历工具（数字中台公共层）
==============================
统一各机器人的「休息日/工作日」判定口径。

日历来源（``calendar.source``）：
    local          随 config 下发的当月休息日（现行默认，由规则生成）
    yonyou_tplus   用友 T+ 排班（占位，未接入）——业务组排班特殊，由 T+ 承载

配置形态::

    "calendar": {
      "month": 9,
      "source": "local",
      "restDays": [6, 13, 19, 25, 26, 27],
      "rule": {                      # 可选：restDays 的生成依据，二者须一致
        "bigRestSaturdays": [19],    # 大休周的周六（小休周周六上班）
        "holidays": [25, 26, 27],    # 法定节假日
        "makeupWorkdays": [20]       # 调休上班（从休息日中剔除）
      },
      "yonyouTplus": {               # 占位：T+ 接入后填写
        "baseUrl": "", "appKey": "", "appSecret": "", "tenantId": "", "schemeId": ""
      }
    }

用法::

    from common.calendar_utils import Calendar
    cal = Calendar(month=9, rest_days=[6, 13, 19, 25, 26, 27])
    cal = Calendar.from_config(config["calendar"])
    cal.check_month()          # 当月不符抛 CalendarError
    cal.workdays()             # 全月工作日列表
    cal.is_rest(day)           # 是否休息日
    cal.elapsed(include_today) # 已过工作日列表（早报口径）

休息日由规则推导（避免手工列举漂移）::

    from common.calendar_utils import generate_rest_days
    generate_rest_days(2026, 9, big_rest_saturdays=[19],
                       holidays=[25, 26, 27], makeup_workdays=[20])
    # -> [6, 13, 19, 25, 26, 27]
"""
import calendar as _pycal
from datetime import datetime

_WEEKDAY_SAT = 5
_WEEKDAY_SUN = 6

SOURCE_LOCAL = "local"
SOURCE_YONYOU_TP = "yonyou_tplus"


class CalendarError(RuntimeError):
    pass


class CalendarSourceUnavailable(CalendarError):
    """日历数据源尚未接入（占位数据源被调用时抛此异常）。"""


def generate_rest_days(year, month, big_rest_saturdays=(), holidays=(), makeup_workdays=()):
    """按「大小休 + 法定节假日 + 调休」规则生成当月休息日号列表（升序）。

    规则（与现行 config 的 restDays 逐位对齐）：
      1. 基准：每周日休息 —— 小休周只休周日，周六上班；
      2. 大休周：``big_rest_saturdays`` 指定的周六一并休息（周日已在基准内）；
      3. 法定节假日：``holidays`` 直接计入休息；
      4. 调休上班：``makeup_workdays`` 从休息集合中剔除。

    ```bigRestSaturdays=[19] + holidays=[25,26,27] - makeupWorkdays=[20]``
    即得 2026-09 的 ``[6, 13, 19, 25, 26, 27]``。

    :param year: 年份
    :param month: 月份
    :param big_rest_saturdays: 大休周的周六日号，须为当月真实的周六
    :param holidays: 法定节假日日号
    :param makeup_workdays: 调休上班日号
    :return: 升序休息日号列表
    """
    year, month = int(year), int(month)
    last_day = _pycal.monthrange(year, month)[1]

    def _checked(days, label):
        out = []
        for raw in days:
            d = int(raw)
            if not 1 <= d <= last_day:
                raise CalendarError(
                    f"{label} 含 {d}，超出 {year}-{month:02d} 当月范围(1~{last_day})")
            out.append(d)
        return out

    saturdays = _checked(big_rest_saturdays, "bigRestSaturdays")
    holidays = _checked(holidays, "holidays")
    makeup_workdays = _checked(makeup_workdays, "makeupWorkdays")

    rest = {d for d in range(1, last_day + 1)
            if datetime(year, month, d).weekday() == _WEEKDAY_SUN}
    for d in saturdays:
        if datetime(year, month, d).weekday() != _WEEKDAY_SAT:
            raise CalendarError(
                f"bigRestSaturdays 含 {year}-{month:02d}-{d:02d}，该日不是周六")
        rest.add(d)
    rest.update(holidays)
    rest.difference_update(makeup_workdays)
    return sorted(rest)


def check_rule_matches_rest_days(cal_cfg, year=None):
    """校验 ``calendar.rule`` 推导的休息日与 ``restDays`` 一致。

    返回差异提示列表（空列表表示一致或未配置 rule）。
    """
    rule = cal_cfg.get("rule")
    if not rule:
        return []
    year = int(rule.get("year") or year or datetime.now().year)
    derived = generate_rest_days(
        year,
        cal_cfg["month"],
        rule.get("bigRestSaturdays", []),
        rule.get("holidays", []),
        rule.get("makeupWorkdays", []),
    )
    declared = sorted(int(d) for d in cal_cfg.get("restDays", []))
    if derived == declared:
        return []
    return [
        f"calendar.rule 推导 {year}-{int(cal_cfg['month']):02d} 休息日={derived}，"
        f"与 restDays={declared} 不一致"
    ]


class CalendarSource:
    """工作日历来源契约。"""

    name = "abstract"

    def load(self, year, month):
        """返回该年月的休息日号列表（升序）。"""
        raise NotImplementedError


class LocalRestDays(CalendarSource):
    """随 config 下发的当月休息日（现行默认）。

    在用友 T+ 排班源接入前，由它代替 T+ 提供工作日口径；``restDays`` 建议由
    :func:`generate_rest_days` 生成，并以 ``calendar.rule`` 记录生成依据。
    """

    name = SOURCE_LOCAL

    def __init__(self, cal_cfg):
        self.month = int(cal_cfg["month"])
        self._rest = sorted(int(d) for d in cal_cfg.get("restDays", []))

    def load(self, year, month):
        return list(self._rest)


class YonyouTplusSchedule(CalendarSource):
    """用友 T+ 排班源（占位，未接入）。

    业务组的排班比较特殊，不由钉钉考勤承载——实测 2026 年 6/7/8/9 四个月钉钉排班
    记录恒为 0（详见 spec §10），故改由用友 T+ 承载，作为工作日口径的长期真源。

    接入清单：
      - 凭据与地址：``baseUrl`` / ``appKey`` / ``appSecret`` / ``tenantId``(账套)
        / ``schemeId``(排班方案)；
      - 取数：按 ``(year, month)`` 拉取每日排班，映射为休息日号列表；
      - 韧性：结果按 ``(year, month)`` 缓存；T+ 不可用时**回退**
        :class:`LocalRestDays` 并告警，不得静默降级。
    """

    name = SOURCE_YONYOU_TP

    def __init__(self, tp_cfg=None):
        self.cfg = tp_cfg or {}

    def load(self, year, month):
        raise CalendarSourceUnavailable(
            "用友 T+ 排班源尚未接入（占位）：请保持 calendar.source=\"local\"，"
            "由 restDays + rule 代替；T+ 凭据与排班方案就绪后在本类实现取数。")


def calendar_source(cal_cfg):
    """按 ``calendar.source`` 构造数据源；未知来源直接报错，避免静默走错口径。"""
    name = (cal_cfg.get("source") or SOURCE_LOCAL).strip()
    if name == SOURCE_LOCAL:
        return LocalRestDays(cal_cfg)
    if name == SOURCE_YONYOU_TP:
        return YonyouTplusSchedule(cal_cfg.get("yonyouTplus"))
    raise CalendarError(
        f"未知的 calendar.source={name!r}，仅支持 {SOURCE_LOCAL!r} / {SOURCE_YONYOU_TP!r}")


class Calendar:
    def __init__(self, month, rest_days):
        self.month = int(month)
        self.rest_days = set(int(d) for d in rest_days)
        # 简化口径：1~30 日；如遇 31 天月份需在业务侧确认
        self._workdays = [d for d in range(1, 31) if d not in self.rest_days]

    @classmethod
    def from_config(cls, cal_cfg, year=None, now=None):
        """从 config.json 的 calendar 段构造（``calendar.source`` 分派，缺省 local）。

        走用友 T+ 源且尚未接入时抛 :class:`CalendarSourceUnavailable`。
        """
        month = int(cal_cfg["month"])
        year = int(year or (now or datetime.now()).year)
        return cls(month, calendar_source(cal_cfg).load(year, month))

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
