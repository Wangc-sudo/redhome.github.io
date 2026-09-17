#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群播报用的零依赖字符图表（纯标准库，适配钉钉 markdown 渲染环境）。

渠道日报机器人整体保持"零第三方依赖"（见 requirements.txt），本模块沿用
同一约束：不引入 matplotlib / playwright，全部图表用 Unicode 块字符在
字符串中绘制，因此 ECS 上无需安装任何字体或浏览器。

钉钉 markdown 对 HTML 的支持面很窄（仅有 <font color="warning"> 等少数标签
可用，且不支持自定义 CSS/内联样式），所以可视化只能落到字符层面。这里的
每个函数都是"给定数据 -> 返回一行/一块文本"的纯函数，方便单测。

惯例：半天块的偶数宽度（▉░）在钉钉客户端里比等宽 ASCII 更稳，故默认用它。
"""
from __future__ import annotations

# Unicode 字符的选择经过实测取舍：
# - ▉/░ 对比度高，在小字号下仍能辨认，用于横向条形与进度条；
# - ▁▂▃▄▅▆▇█ 是 8 级高度阶梯，用于 sparkline（趋势迷你图），比 ▉ 省一半宽度；
# - ▲▼ 用于环比方向，钉钉不会把它渲染成 emoji（无变体选择器），宽度稳定。
BLOCK_FILL = "\u2589"
BLOCK_EMPTY = "\u2591"
SPARK_LEVELS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
ARROW_UP = "\u25b2"
ARROW_DOWN = "\u25bc"
ARROW_FLAT = "\u2013"


def bar(value, max_value, width=10):
    """横向条形图：``▉▉▉▉▉░░░░░``。

    *value* 相对 *max_value* 的比例决定实心块数量，四舍五入而非截断，避免
    小渠道永远拿不到一格。*max_value* 非正时返回全空条 —— 全渠道零销售是
    可能的（例如月初未录数据），此时不该抛异常中断播报。
    """
    if not max_value or max_value <= 0 or not value or value <= 0:
        return BLOCK_EMPTY * width
    ratio = value / max_value
    n = int(round(min(max(ratio, 0.0), 1.0) * width))
    return BLOCK_FILL * n + BLOCK_EMPTY * (width - n)


def gauge(rate, width=10):
    """达成率进度条：``▓▓▓░░``，*rate* 为 0~1 的小数（超 100% 画满）。"""
    if rate is None:
        return BLOCK_EMPTY * width
    n = int(round(min(max(rate, 0.0), 1.0) * width))
    return BLOCK_FILL * n + BLOCK_EMPTY * (width - n)


def sparkline(values, width=None):
    """趋势迷你图：把序列映射到 8 级高度阶梯，如 ``▁▃▅▇▅▃▂``。

    全平序列（最大==最小）会落到最低一级，视觉上是一条平线，符合直觉。
    空序列返回 ``--`` 由调用方决定如何展示，这里不抛错。
    """
    nums = [float(v) for v in values if v is not None]
    if len(nums) < 2:
        return "--"
    lo, hi = min(nums), max(nums)
    if hi == lo:
        return SPARK_LEVELS[0] * len(nums)
    span = hi - lo
    step = len(SPARK_LEVELS) - 1
    out = "".join(
        SPARK_LEVELS[int(round((v - lo) / span * step))] for v in nums
    )
    if width and len(out) > width:
        out = out[-width:]
    return out


def pct_change(cur, base):
    """环比百分比文本：返回 ``▲12.3%`` / ``▼12.3%`` / ``--``。

    *base* 为 0 或 None 时无法计算环比（除零），返回 ``--``；这种情形在
    新渠道首日、长期停摆渠道上很常见，静默降级比显示 "inf%" 安全。
    """
    if not base or cur is None:
        return "--"
    rate = (cur - base) / abs(base) * 100
    arrow = ARROW_UP if rate >= 0 else ARROW_DOWN
    if abs(rate) < 0.05:
        arrow = ARROW_FLAT
    return f"{arrow}{abs(rate):.1f}%"


def pct_change_value(cur, base):
    """环比数值（带符号的原始增减额），用于"昨日 X 万 → 今日 Y 万"类文案。"""
    if cur is None or base is None:
        return None
    return cur - base


def wan(v):
    """金额格式化：>=1万显示 X.X万，否则显示整数千分位。None -> ``--``。"""
    if v is None:
        return "--"
    return f"{v / 10000:.1f}万" if abs(v) >= 10000 else f"{v:,.0f}"


def pct(rate, digits=1):
    """比率格式化：0.39 -> ``39.0%``；None -> ``--``。"""
    if rate is None:
        return "--"
    return f"{rate * 100:.{digits}f}%"
