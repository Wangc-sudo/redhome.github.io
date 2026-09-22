# -*- coding: utf-8 -*-
"""Stream 报数入口 handler（spec §9：单连接按群路由多 region）。

一个 ``dingtalk-gateway`` 进程只建**一条** Stream 长连接（同一钉钉应用
多连接会争抢事件、串错 handler），所有群的消息都到这里，再按
``conversationId`` 路由到对应区域处理——本类就是那个路由点。

职责边界（薄）：解析消息 → 路由 → 委托 :func:`handle_report` → 回执。
所有业务判断都在 :mod:`common.gateway.report_intake`，本类不含口径。

异常处理沿用非泄露约定：日志与回执都不含异常原文；连接由
*connection_factory* 按消息创建，成功 commit、异常 rollback。
"""

from datetime import datetime

import dingtalk_stream
from dingtalk_stream import AckMessage

from common.gateway.card_menu import is_menu_request, send_menu_card
from common.gateway.report_intake import (
    build_error_reply,
    handle_report,
    region_for_conversation,
)


def build_stream_client(app_key, app_secret, handler, *, client_factory=None,
                        card_handler=None):
    """构造**单连接** Stream client 并注册报数 handler（spec §9）。

    与现行 listener 的接法一致（``Credential`` + ``register_callback_handler``
    + 调用方 ``start_forever``）；*client_factory* 仅供测试注入。
    *card_handler*：互动卡片按钮回调 handler（同一连接加注册卡片 topic）。
    """
    credential = dingtalk_stream.Credential(app_key, app_secret)
    factory = client_factory or dingtalk_stream.DingTalkStreamClient
    client = factory(credential)
    client.register_callback_handler(
        dingtalk_stream.chatbot.ChatbotMessage.TOPIC, handler
    )
    if card_handler is not None:
        client.register_callback_handler(
            dingtalk_stream.Card_Callback_Router_Topic, card_handler
        )
    return client


class StreamReportHandler(dingtalk_stream.ChatbotHandler):
    """按群路由的报数 handler。所有依赖注入，测试无需 Stream 连接。"""

    def __init__(self, *, region_configs, connection_factory,
                 report_handler=None, now=None, log=print):
        super().__init__()
        self._region_configs = region_configs
        self._connection_factory = connection_factory
        self._report_handler = report_handler or handle_report
        self._now = now or datetime.now
        self._log = log

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        try:
            incoming = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
        except Exception:
            return AckMessage.STATUS_OK, "OK"

        text = (incoming.text.content or "").strip() if incoming.text else ""
        conversation_id = getattr(incoming, "conversation_id", "") or ""
        sender_uid = incoming.sender_staff_id or ""

        # 临时可见性（PoC 排障）：每条入站消息记录路由键与前缀repr，
        # 便于区分「消息未投递」与「解析未命中」。稳定后可移除。
        self._log(
            f"recv conv={'set' if conversation_id else 'unset'} "
            f"len={len(text)} head={text[:6]!r}"
        )

        region_cfg = region_for_conversation(
            self._region_configs, conversation_id
        )
        if region_cfg is None:
            # 未登记的群：静默 ACK（只记安全日志，不回消息）。
            self._log("unrouted conversation")
            return AckMessage.STATUS_OK, "OK"

        # 互动卡片菜单（PoC）：/菜单 发按钮卡片；失败降级为文字提示。
        if is_menu_request(text):
            try:
                send_menu_card(self, incoming)
            except Exception:
                self._log("menu card send failed")
                self._safe_reply(
                    incoming, "菜单卡片发送失败，可直接发 /帮助 使用文字指令。"
                )
            return AckMessage.STATUS_OK, "OK"

        conn = self._connection_factory()
        try:
            outcome = self._report_handler(
                conn,
                region_cfg=region_cfg,
                text=text,
                sender_uid=sender_uid,
                now=self._now(),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            self._log("report intake failed")
            self._safe_reply(incoming, build_error_reply())
            return AckMessage.STATUS_OK, "OK"

        self._safe_reply(incoming, outcome.reply)
        return AckMessage.STATUS_OK, "OK"

    def _safe_reply(self, incoming, text):
        try:
            self.reply_text(text, incoming)
        except Exception:
            self._log("report reply failed")
