# -*- coding: utf-8 -*-
"""Stream 渠道填报入口 handler（单连接按群路由多 region）。

一个 ``dingtalk-gateway`` 进程只建**一条** Stream 长连接（同一钉钉应用
多连接会争抢事件、串错 handler），所有群的消息都到这里，再按
``conversationId`` 路由到对应区域处理——本类就是那个路由点。

2026-11-18 线下业务线剥离后，本类只承接**线上渠道日销填报**
（``channel_intake``）；线下网点报数（实名门禁、代填、辅助指令、榜单回执）
已迁至 offline-branch 仓，由该仓自己的 Stream 进程接管。同一钉钉应用
仍只能有一条长连接，切换顺序见 offline-branch 的 README「部署」。

异常处理沿用非泄露约定：日志与回执都不含异常原文；连接由
*connection_factory* 按消息创建，成功 commit、异常 rollback。
"""

from datetime import datetime

import dingtalk_stream
from dingtalk_stream import AckMessage

from common.gateway.channel_intake import handle_channel_fill
from common.gateway.intake_common import (
    build_error_reply,
    region_for_conversation,
)


def build_stream_client(app_key, app_secret, handler, *, client_factory=None):
    """构造**单连接** Stream client 并注册渠道填报 handler。

    与现行接法一致（``Credential`` + ``register_callback_handler`` +
    调用方 ``start_forever``）；*client_factory* 仅供测试注入。
    """
    credential = dingtalk_stream.Credential(app_key, app_secret)
    factory = client_factory or dingtalk_stream.DingTalkStreamClient
    client = factory(credential)
    client.register_callback_handler(
        dingtalk_stream.chatbot.ChatbotMessage.TOPIC, handler
    )
    return client


class StreamChannelHandler(dingtalk_stream.ChatbotHandler):
    """按群路由的渠道填报 handler。所有依赖注入，测试无需 Stream 连接。"""

    def __init__(self, *, region_configs, connection_factory,
                 fill_handler=None, now=None, log=print):
        super().__init__()
        self._region_configs = region_configs
        self._connection_factory = connection_factory
        self._fill_handler = fill_handler or handle_channel_fill
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

        conn = self._connection_factory()
        try:
            outcome = self._fill_handler(
                conn,
                region_cfg=region_cfg,
                text=text,
                sender_uid=sender_uid,
                sender_name=getattr(incoming, "sender_nick", None),
                conversation_id=conversation_id,
                now=self._now(),
                is_admin=bool(getattr(incoming, "is_admin", False)),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            self._log("channel fill failed")
            self._safe_reply(incoming, build_error_reply())
            return AckMessage.STATUS_OK, "OK"

        if outcome.reply:  # None = 非填报消息（闲聊），静默不回
            self._safe_reply(incoming, outcome.reply)
        return AckMessage.STATUS_OK, "OK"

    def _safe_reply(self, incoming, text):
        try:
            self.reply_text(text, incoming)
        except Exception:
            self._log("channel reply failed")
