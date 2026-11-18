# -*- coding: utf-8 -*-
"""填报类 handler 的共享原语（区域路由 / 结果契约 / 数值与错误文案）。

2026-11-18 线下业务线剥离后，本模块承接原先住在
``common.gateway.report_intake`` 里的平台级公共件，供主仓保留的
线上渠道填报（``channel_intake``）与 Stream 入口复用：

* :func:`region_for_conversation` —— 按 ``open_conversation_id`` 反查区域；
* :class:`IntakeOutcome` —— 一条消息的处理结果契约（reply 为回执文本）；
* :func:`fmt_amount` —— 金额显示（整数去掉 ``.0``，其余保留两位）；
* :func:`build_error_reply` —— 异常兜底文案（不含异常原文）。

线下报数自己的口径（实名门禁、代填、回执句式）已随线下仓迁出，不在本模块。
"""

from dataclasses import dataclass

__all__ = [
    "IntakeOutcome",
    "build_error_reply",
    "fmt_amount",
    "region_for_conversation",
]


@dataclass(frozen=True)
class IntakeOutcome:
    """一条填报消息的处理结果（``reply`` 为给发送者的回执文本）。"""

    status: str  # recorded | not_member | no_number | aux
    reply: str
    region: str | None = None
    name: str | None = None
    value: object = None
    overwritten: bool = False


def fmt_amount(value):
    number = float(value)
    if number == int(number):
        return str(int(number))
    return str(round(number, 2))


def build_error_reply():
    return "⚠️ 处理报数时出错，请稍后再试，或直接在表格中填写。"


def region_for_conversation(region_configs, conversation_id):
    """按 ``open_conversation_id`` 反查区域配置；未登记返回 ``None``。"""
    if not conversation_id:
        return None
    for cfg in region_configs.values():
        if cfg.open_conversation_id == conversation_id:
            return cfg
    return None
