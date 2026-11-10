"""BI annual 查询域；SQL 与返回契约自原 queries.py 等价迁移。"""

from common.bi_web.query_domains.common import *
from common.bi_web.query_domains.offline import *
from common.bi_web.query_domains.channel import *


_ANNUAL_TARGET_BY_LINE_SQL = textwrap.dedent(
    """
    SELECT scope_key, COALESCE(SUM(annual_target), 0) AS target
    FROM dim_target
    WHERE scope = 'line'
    AND year = YEAR(CURDATE())
    GROUP BY scope_key
    ORDER BY scope_key
    """
).strip()


_RESTAURANT_ANNUAL_SQL = textwrap.dedent(
    """
    SELECT COALESCE(SUM(value), 0) AS total, COUNT(*) AS rows_found
    FROM fact_manual_report
    WHERE dataset = 'restaurant_monthly'
    AND metric = 'revenue'
    AND period_start >= MAKEDATE(YEAR(CURDATE()), 1)
    """
).strip()


def annual_target_total(connection) -> Decimal:
    """年度目标分母: scope='line' 全部业务线年度目标之和（769,510,000
    元量级 with the repository seed：线下 21041 万 + 电商渠道 54980 万
    + 餐饮 930 万）。

    2026-09-21 前只取 offline/channel 两线；餐饮线目标从此计入——
    分子侧找不到数据按 0 标红（:func:`run_kpi_annual_progress`），
    不是把线从目标里抹掉。
    """
    return sum(
        (
            Decimal(row["target"] or 0)
            for row in _fetch_rows(connection, _ANNUAL_TARGET_BY_LINE_SQL)
        ),
        Decimal(0),
    )


def restaurant_annual_revenue(connection):
    """餐饮线年累计收入（人工月报）：``(Σrevenue, 是否有数据行)``。

    空表 ⇒ ``(0, False)``——「未接入」不是「0 销量」，由卡片标红。
    """
    rows = _fetch_rows(connection, _RESTAURANT_ANNUAL_SQL)
    if not rows:
        return Decimal(0), False
    row = rows[0]
    return Decimal(row["total"] or 0), bool(row["rows_found"])


_ANNUAL_LINE_SOURCES = {
    "offline": ("线下", lambda connection: (offline_annual_total(connection), True)),
    "channel": ("电商渠道", lambda connection: (channel_annual_total(connection), True)),
    "restaurant": ("餐饮", restaurant_annual_revenue),
}


def run_kpi_annual_progress(connection, params) -> dict:
    """⑫年度目标达成进度: 全业务线年累计 ÷ 全业务线年度目标。

    分子 = 各业务线年累计之和（线下=日报、电商渠道=渠道表、餐饮=
    人工月报，口径与各线卡一致）；分母 = ``dim_target`` scope='line'
    全部业务线。``lines`` 逐线列出 {name, value, target, has_data}：
    找不到数据的线值 0、has_data=False——前端标红为 0（「未接入」
    不是「0 销量」）；rate 为 None 当分母为 0。
    """
    targets = {
        row["scope_key"]: Decimal(row["target"] or 0)
        for row in _fetch_rows(connection, _ANNUAL_TARGET_BY_LINE_SQL)
    }
    lines = []
    value = Decimal(0)
    target = Decimal(0)
    ordered_keys = list(_ANNUAL_LINE_SOURCES) + sorted(
        key for key in targets if key not in _ANNUAL_LINE_SOURCES
    )
    for scope_key in ordered_keys:
        label, resolver = _ANNUAL_LINE_SOURCES.get(
            scope_key, (scope_key, lambda connection: (Decimal(0), False))
        )
        line_value, has_data = resolver(connection)
        line_target = targets.get(scope_key, Decimal(0))
        value += line_value
        target += line_target
        lines.append(
            {
                "name": label,
                "value": float(line_value),
                "target": float(line_target),
                "has_data": bool(has_data),
            }
        )
    rate = None if target == 0 else float(value) / float(target)
    return {
        "chart": "scalar",
        "value": float(value),
        "target": float(target),
        "rate": rate,
        "lines": lines,
        "unit": _UNIT,
    }

__all__ = ('_ANNUAL_TARGET_BY_LINE_SQL', '_RESTAURANT_ANNUAL_SQL', 'annual_target_total', 'restaurant_annual_revenue', '_ANNUAL_LINE_SOURCES', 'run_kpi_annual_progress')
