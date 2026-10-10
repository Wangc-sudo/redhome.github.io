# -*- coding: utf-8 -*-
"""法定节假日导入（C 层）：holiday-cn 机器可读源 → ``calendar.seed.json``。

数据源：`NateScarlet/holiday-cn <https://github.com/NateScarlet/holiday-cn>`_
——国务院办公厅《关于 YYYY 年部分节假日安排的通知》的社区机器可读转录
（每年通知发布后数日更新，``{year}.json``）。

映射（与 2026 种子「每月第 3 个周六大休」锚点口径一致）：
* ``bigRestSaturdays`` = 当月第 3 个周六（2026 种子锚点约定，待 HR
  确认期间的大小休占位；运维裁决特例如 2026-10 的 [17,31] 靠人工
  在 git 上调整，不属于导入职责）；
* ``holidays`` = 数据源 ``isOffDay=true`` 的日号（法定假/调休休息；
  与周日/大休周六重叠由并集自然吸收）；
* ``makeupWorkdays`` = 数据源 ``isOffDay=false`` 的日号（调休上班）；
推导式 ``rest = 周日 ∪ 大休周六 ∪ holidays − makeupWorkdays`` 与
种子现行生成规则逐位一致（有自证锁定）。holiday-cn 只覆盖法定安排，
公司大小休规则不由它承载，故大休周六按锚点约定推导而非全休。

用法::

    python -m common.public_data.cli calendar-import --year 2027
    python -m common.public_data.cli calendar-import --year 2027 --apply

缺省 dry-run（打印将要写入的月份与休息日）；``--apply`` 合并写回种子
（同年月份整年替换，其余年份不动；git diff 即评审面）。
"""

import json
import time
import urllib.request
from datetime import date
from pathlib import Path

from common.calendar_utils import (
    _WEEKDAY_SAT,
    _WEEKDAY_SUN,
    generate_rest_days,
    load_calendar_seed,
    month_days,
)

#: holiday-cn 年度文件地址模板。
HOLIDAY_CN_URL = (
    "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"
)

#: 拉取重试（本机到 raw.githubusercontent.com 间歇 reset，与 git 同纪律）。
_FETCH_ATTEMPTS = 4


class CalendarImportError(RuntimeError):
    """导入失败（网络/格式/自证不符；消息不含凭据与文件内容）。"""


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


def map_year_to_seed_months(year, days):
    """holiday-cn ``days`` → 种子 months 条目（整年 12 个月，年月升序）。

    每条含 ``bigRestSaturdays`` / ``holidays`` / ``makeupWorkdays`` /
    ``_说明``（自证文本，与既有种子风格一致）。
    """
    year = int(year)
    by_month = {month: {"holidays": [], "makeupWorkdays": []}
                for month in range(1, 13)}
    for entry in days:
        try:
            day = date.fromisoformat(str(entry["date"]))
            is_off = bool(entry["isOffDay"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CalendarImportError("holiday-cn days 条目缺少 date/isOffDay") from exc
        if day.year != year:
            continue  # 相邻年溢出日期（如 12 月末的次年元旦调休）归其所属年
        bucket = by_month[day.month]
        bucket["holidays" if is_off else "makeupWorkdays"].append(day.day)

    months = []
    for month in range(1, 13):
        saturdays = [
            d.day for d in month_days(year, month) if d.weekday() == _WEEKDAY_SAT
        ]
        # 2026 种子锚点约定：每月第 3 个周六大休（每月恒 ≥4 个周六）。
        big_rest = [saturdays[2]]
        holidays = sorted(set(by_month[month]["holidays"]))
        makeup = sorted(set(by_month[month]["makeupWorkdays"]))
        rest = generate_rest_days(
            year, month, big_rest_saturdays=big_rest,
            holidays=holidays, makeup_workdays=makeup,
        )
        months.append({
            "year": year,
            "month": month,
            "bigRestSaturdays": big_rest,
            "holidays": holidays,
            "makeupWorkdays": makeup,
            "_说明": (
                f"holiday-cn {year} 导入（机器可读转录国务院办公厅通知）："
                f"基准周日休 + 大休周六{big_rest}（第 3 个周六锚点约定，"
                f"待 HR 确认）+ 节假日{holidays} − 调休{makeup} ⇒ {rest}"
            ),
        })
    return months


def merge_seed_document(document, new_months, *, year):
    """把 *new_months*（整年）合并进种子文档：同年替换、其余保留、按年月
    升序。返回 ``(document, {"replaced": n, "added": n})``。"""
    existing = [
        item for item in document.get("months", [])
        if not (isinstance(item, dict) and int(item.get("year", 0)) == int(year))
    ]
    replaced = len(document.get("months", [])) - len(existing)
    merged = existing + list(new_months)
    merged.sort(key=lambda item: (int(item["year"]), int(item["month"])))
    document["months"] = merged
    return document, {"replaced": replaced, "added": len(new_months) - replaced}


def import_year(seed_path, year, *, apply=False, fetcher=None):
    """导入 *year* 法定节假日到种子文件。

    dry-run（缺省）不落盘，返回 ``{"applied", "stats", "months"}``；
    ``apply=True`` 写回并做双自证（推导休息日 == 周末+法定安排目标集；
    写后 ``load_calendar_seed(fallback=False)`` 可解析）。
    """
    seed_path = Path(seed_path)
    days = (fetcher or fetch_holiday_cn)(year)
    new_months = map_year_to_seed_months(year, days)

    try:
        document = json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalendarImportError("日历种子不是可读的 JSON 文件") from exc
    document, stats = merge_seed_document(document, new_months, year=year)

    # 自证 ①：推导休息日 == 「周日 ∪ 大休周六 ∪ isOffDay − 调休上班」
    # 目标集（独立于 generate_rest_days 复算，防映射错位/去重失误）。
    off_days = {d["date"] for d in days if d.get("isOffDay")}
    work_days = {d["date"] for d in days if not d.get("isOffDay")}
    for item in new_months:
        expected = [
            d for d in month_days(item["year"], item["month"])
            if (d.weekday() == _WEEKDAY_SUN
                or d.day in item["bigRestSaturdays"]
                or d.isoformat() in off_days)
            and d.isoformat() not in work_days
        ]
        derived = generate_rest_days(
            item["year"], item["month"],
            big_rest_saturdays=item["bigRestSaturdays"],
            holidays=item["holidays"],
            makeup_workdays=item["makeupWorkdays"],
        )
        if derived != [d.day for d in expected]:
            raise CalendarImportError(
                f"自证失败：{item['year']}-{item['month']:02d} 推导休息日"
                "与 holiday-cn 目标集不一致"
            )

    if not apply:
        return {"applied": False, "stats": stats, "months": new_months}

    seed_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # 自证 ②：写后可被标准加载链解析（不触发兜底，纯种子内容）
    load_calendar_seed(seed_path, fallback=False)
    return {"applied": True, "stats": stats, "months": new_months}
