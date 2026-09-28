# -*- coding: utf-8 -*-
"""渠道播报收敛（2026-09-23）：榜单页板块与 BI 共用的查询层。

设计稿：docs/superpowers/specs/2026-09-17-broadcast-alerts-db-sourcing-design.md
读取契约（§6.3）：播报/页面/BI 只允许经本包读 ``mart_ops``，群里那行数字
和看板那个柱子出自同一个 SELECT；返回值带 ``as_of`` 与 ``stale`` 标记。
"""

import os

#: 动销窗口天数（用户裁定 2026-09-17 §9：默认 15，7..30 可调）。
#: 投影层（fact_inventory_sku_daily.daily_avg/days_left/stock_state）、
#: 页面板块、BI 看板必须共用这一个取值，根绝双口径分裂。
_DEFAULT_MOVING_WINDOW_DAYS = 15


def moving_window_days(environ=None) -> int:
    env = os.environ if environ is None else environ
    raw = (env.get("MOVING_WINDOW_DAYS") or "").strip()
    days = int(raw) if raw else _DEFAULT_MOVING_WINDOW_DAYS
    if not 7 <= days <= 30:
        raise ValueError(f"MOVING_WINDOW_DAYS must be in 7..30 (got {days})")
    return days
