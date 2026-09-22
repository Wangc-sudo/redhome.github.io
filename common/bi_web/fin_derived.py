# -*- coding: utf-8 -*-
"""资金安全页（需求⑩）派生口径——后端算，前端只渲染。

方案来源：``docs/superpowers/specs/2026-09-16-fund-safety-draft.md`` §3。
与 :mod:`common.bi_web.derived` 同形态：**零 DB 依赖**的纯函数模块，财务
口径不进 derived.py，保持 CubeSchema §2 边界纯净。``queries.py`` 的五张
``*_fin_*`` 卡只查事实（只读 mart ``fact_fin_*``），派生一律调用这里。

severity 映射约定（与前端 AlertChip 契约一致，值域沿用
``derived.SEVERITY_DOMAIN`` 的四档）：

* 超期命中 → ``"p1"``；
* 分量缺失不可算 → ``"p2"``（数据缺陷显式化，前端「—」+ 角标）；
* 正常 → ``"ok"``；
* ``"p0"`` 保留给挂零语义（``derived.severity`` 的 done==0），fin 卡不用。

金额单位跟随调用方（元），本模块不做任何万换算。
"""

from datetime import datetime

#: 超期判定阈值（天）：应收 >60 天账龄桶 / 预付未到票挂账 >60 天。
#: **行业默认值（需求对照 §5.1），待财务确认**——确认后只改本常量，
#: 不动 SQL 与派生函数（draft §5「行业默认值先行，业务确认或改数」）。
OVERDUE_DAYS_THRESHOLD = 60


def _as_date(value):
    """``datetime`` → ``date``（DB 层可能返回 datetime）。"""
    if isinstance(value, datetime):
        return value.date()
    return value


def aging_over_60(ending_balance, aging_0_30, aging_31_60):
    """应收 >60 天账龄桶 = 期末余额 − 0-30 天桶 − 31-60 天桶（差额推导）。

    raw 侧只有 ``0-30`` / ``31-60`` 两列，没有 ``>60`` 列（draft §2.3
    已知缺口），故由后端差额推导——**差额口径待财务确认**。
    任一分量缺失 → ``None``（§5：不可算，前端「—」，**不静默补 0**）。
    """
    if ending_balance is None or aging_0_30 is None or aging_31_60 is None:
        return None
    return float(ending_balance) - float(aging_0_30) - float(aging_31_60)


def days_outstanding(statement_date, today):
    """挂账天数 = ``today`` − ``statement_date``（自然日，含对账日当天为 0）。

    ``statement_date`` 为 ``None`` —— raw 日期不可解析（投影层已计数进
    ``statement_date_unparsed``）—— → ``None``（**不猜日期**，前端「—」）；
    ``today`` 为 ``None`` 同样 → ``None``（调用方不应传，防御处理）。
    """
    start = _as_date(statement_date)
    anchor = _as_date(today)
    if start is None or anchor is None:
        return None
    return (anchor - start).days


def aging_severity(over_60, overdue_amount):
    """应收账龄行 severity：超期命中 → p1；分量缺失不可算 → p2；正常 → ok。

    * ``over_60`` 为 ``None``（账龄分量缺失，差额不可算）→ ``"p2"``；
    * ``over_60 > 0``（>60 天桶有余额，阈值见 ``OVERDUE_DAYS_THRESHOLD``）
      或 ``overdue_amount > 0``（逾期金额列有余额）→ ``"p1"``；
    * 其余 → ``"ok"``。``overdue_amount`` 缺失按无逾期列处理（不升级）。
    """
    if over_60 is None:
        return "p2"
    if float(over_60) > 0 or float(overdue_amount or 0) > 0:
        return "p1"
    return "ok"


def uninvoiced_severity(uninvoiced_amount, days):
    """预付/未到票行 severity：未到票 >0 且挂账 >60 天 → p1；不可算 → p2。

    超期标准 **>60 天为行业默认值（待财务确认）**，取严格大于
    （``OVERDUE_DAYS_THRESHOLD``）。``uninvoiced_amount`` 或 ``days``
    为 ``None``（选填未填 / 对账日不可解析）→ ``"p2"``：不可判超期，
    按数据缺陷关注，不静默当正常。
    """
    if uninvoiced_amount is None or days is None:
        return "p2"
    if float(uninvoiced_amount) > 0 and int(days) > OVERDUE_DAYS_THRESHOLD:
        return "p1"
    return "ok"


def deposit_severity(status):
    """保证金行 severity：非正常合作/运营状态 → p2 关注；正常 → ok。

    状态**原文透传**（枚举不固化）；severity 的判定规则保守化：状态缺失
    或不含「正常」字样 → ``"p2"``（可退未退关注项），含「正常」→ ``"ok"``。
    「正常」取值清单待财务/运营确认（draft §3.4 登记项）——确认后只改
    本函数，不动 SQL。
    """
    if status is None or "正常" not in str(status):
        return "p2"
    return "ok"
