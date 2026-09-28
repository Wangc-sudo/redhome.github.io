"""``--dataset`` 过滤的公共匹配逻辑（live-sync / extract-mart 共用）。

模式语法：精确匹配，或以 ``*`` 结尾的前缀通配（如 ``channel_daily_sales*``）。
"""


def matches_any(name, patterns):
    """*name* 是否命中 *patterns* 中的任意一个模式。"""
    for pattern in patterns:
        if pattern.endswith("*"):
            if name.startswith(pattern[:-1]):
                return True
        elif name == pattern:
            return True
    return False
