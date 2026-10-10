# -*- coding: utf-8 -*-
"""法定节假日拉取与裁决行映射（holiday-cn 机器可读源）。

数据源：`NateScarlet/holiday-cn <https://github.com/NateScarlet/holiday-cn>`_
——国务院办公厅《关于 YYYY 年部分节假日安排的通知》的社区机器可读转录
（每年通知发布后数日更新，``{year}.json``）。

2026-10-10 口径分层（运维裁决「法定工作口径 ≠ 播报口径；后端给非技术
同学用」）：本模块只做两件事——拉取（``fetch_holiday_cn``）与映射为
裁决行（``map_days_to_overrides``）。落库归
``calendar_store.import_year_overrides``（DB ``dim_calendar_override``
表，ops-web「工作日历」页与 CLI 共用）；此前的 JSON 种子整年合并写法
已废弃——JSON 种子冻结为 2025-2026 历史基线，未来年份由 DB 裁决层
承载（规则兜底保不断档）。
"""

import json
import time
import urllib.request
from datetime import date

#: holiday-cn 年度文件地址模板。
HOLIDAY_CN_URL = (
    "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"
)

#: 拉取重试（到 raw.githubusercontent.com 间歇 reset，与 git 同纪律）。
_FETCH_ATTEMPTS = 4


class CalendarImportError(RuntimeError):
    """导入失败（网络/格式；消息不含凭据与文件内容）。"""


def fetch_holiday_cn(year, *, opener=None, sleep=time.sleep):
    """拉取并解析 holiday-cn 年度 JSON，返回 ``days`` 数组。

    *opener* 可注入（测试）；缺省 urllib。非 200/格式非法抛
    :class:`CalendarImportError`。
    """
    url = HOLIDAY_CN_URL.format(year=int(year))

    def _default_open(target):
        request = urllib.request.Request(
            target, headers={"User-Agent": "dops-calendar-import"}
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read()

    open_fn = opener or _default_open
    last_error = None
    for attempt in range(_FETCH_ATTEMPTS):
        try:
            payload = json.loads(open_fn(url).decode("utf-8"))
            break
        except Exception as exc:  # 网络/JSON 一律重试后汇总失败
            last_error = exc
            if attempt + 1 < _FETCH_ATTEMPTS:
                sleep(2 * (attempt + 1))
    else:
        raise CalendarImportError(
            f"holiday-cn {year} 拉取失败（{_FETCH_ATTEMPTS} 次）："
            f"{type(last_error).__name__}"
        ) from last_error
    days = payload.get("days") if isinstance(payload, dict) else None
    if not isinstance(days, list) or not days:
        raise CalendarImportError(
            f"holiday-cn {year} 尚未发布或格式不含 days 数组"
        )
    return days


def map_days_to_overrides(year, days):
    """holiday-cn ``days`` → 裁决行 ``[(date, is_workday, name)]``。

    ``isOffDay=true`` → ``(day, 0, name)``（法定节假日休息）；
    ``isOffDay=false`` → ``(day, 1, name+'（调休上班）')``；
    相邻年溢出日期（如 12 月末的次年元旦调休）归其所属年，直接跳过。
    """
    year = int(year)
    overrides = []
    for entry in days:
        try:
            day = date.fromisoformat(str(entry["date"]))
            is_off = bool(entry["isOffDay"])
            name = str(entry.get("name") or "").strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise CalendarImportError(
                "holiday-cn days 条目缺少 date/isOffDay"
            ) from exc
        if day.year != year:
            continue
        overrides.append(
            (day, 0 if is_off else 1, name + ("" if is_off else "（调休上班）"))
        )
    overrides.sort(key=lambda item: item[0])
    return overrides
