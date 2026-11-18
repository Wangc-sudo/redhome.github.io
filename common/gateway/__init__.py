# -*- coding: utf-8 -*-
"""dingtalk-gateway（apps 线）：钉钉交互唯一入口。

2026-11-18 起主仓只承接**线上渠道填报**与 ``robot_outbox`` 投递；线下网点
报数（含实名门禁、代填、辅助指令回执）已迁 offline-branch 仓。
"""
from .delivery import (
    DeliveryError,
    DeliveryErrorCode,
    DeliverReport,
    OutboxDeliveryWorker,
)
from .dingtalk_deliverer import DingTalkDeliverer, DwsCommandDingSender
from .intake_common import IntakeOutcome, region_for_conversation
from .stream_handler import StreamChannelHandler

__all__ = [
    "DeliveryError",
    "DeliveryErrorCode",
    "DeliverReport",
    "OutboxDeliveryWorker",
    "DingTalkDeliverer",
    "DwsCommandDingSender",
    "IntakeOutcome",
    "region_for_conversation",
    "StreamChannelHandler",
]
