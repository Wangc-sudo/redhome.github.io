"""榜单页日期选择月历（2026-10-09 运维裁决：日历点日期翻看，蓝=工作日）。

纯渲染模块（无 IO）：调用方（mart_cli leaderboard-html）负责扫描输出目录
里已存在的存档页 ``<区域>-YYYY-MM-DD.html`` 并传入；本模块只拼 HTML。

链接语义：

* **蓝色加粗可点** = 工作日且已有当天存档页（含本页日期）；
* 蓝色不可点 = 工作日但存档尚未生成（未来日 / 功能上线前）；
* 灰色 = 非工作日（周末/节假日，``dim_calendar`` 口径）；
* 本页日期高亮描边；上/下月导航仅在该月已有存档时出现。
"""

import calendar as _calendar
import html as _html
from datetime import date

#: 存档页文件名中的日期形态（``<stem>-2026-10-09.html``）。
ARCHIVE_DATE_FMT = "%Y-%m-%d"

_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


def archive_name(stem, day):
    """存档页文件名：``<stem>-YYYY-MM-DD.html``。"""
    return f"{stem}-{day.strftime(ARCHIVE_DATE_FMT)}.html"


def build_date_nav_panel(*, page_date, workday_dates, available_dates,
                         archive_stem):
    """月历导航面板（完整 ``<div class="panel">``）。

    *page_date* 本页业务日；*workday_dates* 当月工作日（``date`` 集合，
    ``dim_calendar`` 真源）；*available_dates* 已存在存档页的日期集合
    （调用方需自行并入 *page_date*）；*archive_stem* 存档文件名前缀
    （= 区域名，相对链接，与榜单页同目录）。
    """
    if not isinstance(page_date, date) or not archive_stem:
        return ""
    year, month = page_date.year, page_date.month
    stem = _html.escape(archive_stem)
    available = set(available_dates or ()) | {page_date}
    workdays = set(workday_dates or ())

    # ---- 上/下月导航（仅在该侧已有存档时出现，指向最近存档日）----
    month_first = date(year, month, 1)
    prev_days = [d for d in available if d < month_first]
    next_first = (
        date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    )
    next_days = [d for d in available if d >= next_first]
    prev_link = (
        f'<a href="{stem}-{max(prev_days):%Y-%m-%d}.html">'
        f'« {max(prev_days):%Y-%m}</a>'
        if prev_days else ""
    )
    next_link = (
        f'<a href="{stem}-{min(next_days):%Y-%m-%d}.html">'
        f'{min(next_days):%Y-%m} »</a>'
        if next_days else ""
    )

    # ---- 月历格子 ----
    rows = []
    for week in _calendar.Calendar(firstweekday=0).monthdatescalendar(
            year, month):
        cells = []
        for day in week:
            if day.month != month:
                cells.append(f'<td><span class="adj">{day.day}</span></td>')
                continue
            cur = ' class="cur"' if day == page_date else ""
            if day not in workdays:
                cells.append(f'<td{cur}><span class="off">{day.day}</span></td>')
            elif day in available:
                cells.append(
                    f'<td{cur}><a href="{stem}-{day:%Y-%m-%d}.html">'
                    f'{day.day}</a></td>'
                )
            else:
                cells.append(f'<td{cur}><span class="wd">{day.day}</span></td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")

    head = "".join(f"<th>{w}</th>" for w in _WEEKDAYS)
    return (
        '<div class="panel">\n'
        '  <div class="cal-nav">'
        f'<span class="cal-side">{prev_link}</span>'
        f'<h2 style="margin:0">选择日期（蓝色为工作日）· {year}年{month}月</h2>'
        f'<span class="cal-side">{next_link}</span></div>\n'
        f'  <table class="cal"><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>\n'
        '  <div class="small muted" style="margin-top:6px;text-align:center">'
        '蓝色加粗可点击 = 翻看当天榜单；蓝色 = 工作日（当天榜单未生成）；'
        '灰色 = 非工作日</div>\n'
        '</div>'
    )


#: 注入榜单页 <style> 的月历样式（注意：榜单页 style 段在 f-string 内，
#: 调用方拼接时本串不含花括号，无需转义）。
DATE_NAV_CSS = (
    ".cal{border-collapse:separate;border-spacing:4px;width:auto;"
    "margin:0 auto}\n"
    ".cal th{border:none;padding:4px 6px;text-align:center}\n"
    ".cal td{border:none;padding:0;text-align:center}\n"
    ".cal a,.cal span{display:inline-block;width:34px;height:34px;"
    "line-height:34px;border-radius:8px;font-size:13px;text-decoration:none}\n"
    ".cal a{background:#e8f0fe;color:#1a73e8;font-weight:700}\n"
    ".cal a:hover{background:#d2e3fc}\n"
    ".cal .wd{color:#1a73e8;font-weight:700}\n"
    ".cal .off{color:#b8bdc7}\n"
    ".cal .adj{color:#d6dae0}\n"
    ".cal .cur a,.cal .cur span{outline:2px solid #1a73e8}\n"
    ".cal-nav{display:flex;justify-content:space-between;align-items:center;"
    "margin-bottom:8px}\n"
    ".cal-nav a{color:#1a73e8;text-decoration:none;font-size:13px}\n"
    ".cal-side{min-width:64px}\n"
)
