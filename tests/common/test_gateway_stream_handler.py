"""Tests for the Stream channel-fill handler (single connection, per-group routing).

2026-11-18 线下业务线剥离后，主仓 Stream 只接线上渠道填报
（``channel_intake``）；线下报数 handler 的用例随线下仓迁出。
"""

import asyncio
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from dingtalk_stream import AckMessage

from common.gateway.intake_common import IntakeOutcome
from common.gateway.stream_handler import (
    StreamChannelHandler,
    build_stream_client,
)
from common.region_config import RegionConfig


_STATUS_OK = AckMessage.STATUS_OK


_CFG = RegionConfig(
    region="qudao", display="渠道日报",
    table_url="https://example.com/table",
    robot_code="rc", open_conversation_id="conv-qudao",
    aliases={}, cc_user_ids=(),
)


def _callback():
    return Mock(data={"some": "payload"})


def _incoming(text="京东 购喝 15867", conversation_id="conv-qudao",
              sender_uid="u1"):
    incoming = Mock()
    incoming.text = Mock(content=text)
    incoming.conversation_id = conversation_id
    incoming.sender_staff_id = sender_uid
    incoming.sender_nick = "外部人员"
    return incoming


def _handler(*, fill_handler=None, conn=None):
    logs = []
    conn = conn or Mock()
    handler = StreamChannelHandler(
        region_configs={"qudao": _CFG},
        connection_factory=lambda: conn,
        fill_handler=fill_handler,
        now=lambda: datetime(2026, 9, 29, 9, 40),
        log=logs.append,
    )
    replies = []
    handler.reply_text = lambda text, incoming: replies.append(text)
    return handler, conn, replies, logs


def _run(handler, callback, incoming):
    with patch(
        "dingtalk_stream.ChatbotMessage.from_dict", return_value=incoming
    ):
        return asyncio.run(handler.process(callback))


class StreamChannelHandlerTests(unittest.TestCase):

    def test_routes_and_replies_the_outcome(self):
        outcome = IntakeOutcome("recorded", "✅ 已记录 1 条", region="qudao")
        fill = Mock(return_value=outcome)
        handler, conn, replies, _ = _handler(fill_handler=fill)

        status, _ = _run(handler, _callback(), _incoming())

        self.assertEqual(status, _STATUS_OK)
        kwargs = fill.call_args.kwargs
        self.assertIs(kwargs["region_cfg"], _CFG)
        self.assertEqual(kwargs["text"], "京东 购喝 15867")
        self.assertEqual(kwargs["sender_uid"], "u1")
        self.assertEqual(kwargs["sender_name"], "外部人员")
        self.assertEqual(kwargs["conversation_id"], "conv-qudao")
        conn.commit.assert_called_once()
        self.assertEqual(replies, ["✅ 已记录 1 条"])

    def test_silent_outcome_sends_no_reply(self):
        handler, conn, replies, _ = _handler(
            fill_handler=Mock(return_value=IntakeOutcome("ignored", None,
                                                         region="qudao"))
        )

        status, _ = _run(handler, _callback(), _incoming(text="大家早上好"))

        self.assertEqual(status, _STATUS_OK)
        conn.commit.assert_called_once()
        self.assertEqual(replies, [])

    def test_unknown_conversation_is_acked_silently(self):
        fill = Mock()
        handler, conn, replies, logs = _handler(fill_handler=fill)

        status, _ = _run(handler, _callback(), _incoming(conversation_id="conv-x"))

        self.assertEqual(status, _STATUS_OK)
        fill.assert_not_called()
        conn.commit.assert_not_called()
        self.assertEqual(replies, [])
        self.assertEqual(logs[-1], "unrouted conversation")
        self.assertTrue(logs[0].startswith("recv conv="))

    def test_fill_failure_rolls_back_and_replies_generic_error(self):
        handler, conn, replies, logs = _handler(
            fill_handler=Mock(side_effect=RuntimeError("secret-detail"))
        )

        status, _ = _run(handler, _callback(), _incoming())

        self.assertEqual(status, _STATUS_OK)
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()
        self.assertEqual(len(replies), 1)
        self.assertIn("⚠️", replies[0])
        self.assertNotIn("secret-detail", replies[0])
        self.assertNotIn("secret-detail", " ".join(logs))

    def test_unparseable_callback_is_acked_without_work(self):
        fill = Mock()
        handler, _, replies, _ = _handler(fill_handler=fill)

        with patch(
            "dingtalk_stream.ChatbotMessage.from_dict",
            side_effect=ValueError("bad payload"),
        ):
            status, _ = asyncio.run(handler.process(_callback()))

        self.assertEqual(status, _STATUS_OK)
        fill.assert_not_called()
        self.assertEqual(replies, [])

    def test_reply_failure_is_logged_not_raised(self):
        handler, conn, _, logs = _handler(
            fill_handler=Mock(return_value=IntakeOutcome("recorded", "✅",
                                                         region="qudao"))
        )
        handler.reply_text = Mock(side_effect=RuntimeError("network"))

        status, _ = _run(handler, _callback(), _incoming())

        self.assertEqual(status, _STATUS_OK)
        conn.commit.assert_called_once()
        self.assertIn("channel reply failed", logs)


class BuildStreamClientTests(unittest.TestCase):

    def test_registers_the_handler_on_the_chatbot_topic(self):
        client = Mock()
        factory = Mock(return_value=client)
        handler = Mock()

        built = build_stream_client("key", "secret", handler,
                                    client_factory=factory)

        self.assertIs(built, client)
        self.assertEqual(client.register_callback_handler.call_count, 1)


if __name__ == "__main__":
    unittest.main()
