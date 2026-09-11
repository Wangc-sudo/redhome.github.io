"""Tests for the Stream report handler (single connection, per-group routing)."""

import asyncio
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from dingtalk_stream import AckMessage

from common.gateway.report_intake import IntakeOutcome
from common.gateway.stream_handler import StreamReportHandler
from common.region_config import RegionConfig


_STATUS_OK = AckMessage.STATUS_OK


_CFG = RegionConfig(
    region="hangzhou", display="杭州",
    table_url="https://example.com/table",
    robot_code="rc", open_conversation_id="conv-hz",
    aliases={}, cc_user_ids=(),
)


def _callback():
    return Mock(data={"some": "payload"})


def _incoming(text="12800", conversation_id="conv-hz", sender_uid="u1"):
    incoming = Mock()
    incoming.text = Mock(content=text)
    incoming.conversation_id = conversation_id
    incoming.sender_staff_id = sender_uid
    return incoming


def _handler(*, report_handler=None, conn=None, replies=None):
    logs = []
    conn = conn or Mock()
    handler = StreamReportHandler(
        region_configs={"hangzhou": _CFG},
        connection_factory=lambda: conn,
        report_handler=report_handler,
        now=lambda: datetime(2026, 9, 11, 17, 30),
        log=logs.append,
    )
    replies = replies if replies is not None else []
    handler.reply_text = lambda text, incoming: replies.append(text)
    return handler, conn, replies, logs


def _run(handler, callback, incoming):
    with patch(
        "dingtalk_stream.ChatbotMessage.from_dict", return_value=incoming
    ):
        return asyncio.run(handler.process(callback))


class StreamHandlerTests(unittest.TestCase):

    def test_routes_and_replies_the_outcome(self):
        outcome = IntakeOutcome("recorded", "✅ 已记录", region="hangzhou",
                                name="张三", value=12800)
        report_handler = Mock(return_value=outcome)
        handler, conn, replies, _ = _handler(report_handler=report_handler)

        status, _ = _run(handler, _callback(), _incoming())

        self.assertEqual(status, _STATUS_OK)
        kwargs = report_handler.call_args.kwargs
        self.assertIs(kwargs["region_cfg"], _CFG)
        self.assertEqual(kwargs["text"], "12800")
        self.assertEqual(kwargs["sender_uid"], "u1")
        conn.commit.assert_called_once()
        self.assertEqual(replies, ["✅ 已记录"])

    def test_unknown_conversation_is_acked_silently(self):
        report_handler = Mock()
        handler, conn, replies, logs = _handler(report_handler=report_handler)

        status, _ = _run(handler, _callback(), _incoming(conversation_id="conv-x"))

        self.assertEqual(status, _STATUS_OK)
        report_handler.assert_not_called()
        self.assertEqual(replies, [])
        self.assertEqual(logs, ["unrouted conversation"])

    def test_intake_failure_rolls_back_and_replies_generic_error(self):
        report_handler = Mock(side_effect=RuntimeError("secret-detail"))
        handler, conn, replies, logs = _handler(report_handler=report_handler)

        status, _ = _run(handler, _callback(), _incoming())

        self.assertEqual(status, _STATUS_OK)
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()
        self.assertEqual(len(replies), 1)
        self.assertIn("⚠️", replies[0])
        self.assertNotIn("secret-detail", replies[0])
        self.assertNotIn("secret-detail", " ".join(logs))

    def test_unparseable_callback_is_acked_without_work(self):
        report_handler = Mock()
        handler, _, replies, _ = _handler(report_handler=report_handler)

        with patch(
            "dingtalk_stream.ChatbotMessage.from_dict",
            side_effect=ValueError("bad payload"),
        ):
            status, _ = asyncio.run(handler.process(_callback()))

        self.assertEqual(status, _STATUS_OK)
        report_handler.assert_not_called()
        self.assertEqual(replies, [])

    def test_reply_failure_is_logged_not_raised(self):
        outcome = IntakeOutcome("recorded", "✅", region="hangzhou")
        handler, conn, _, logs = _handler(
            report_handler=Mock(return_value=outcome)
        )
        handler.reply_text = Mock(side_effect=RuntimeError("network"))

        status, _ = _run(handler, _callback(), _incoming())

        self.assertEqual(status, _STATUS_OK)
        conn.commit.assert_called_once()
        self.assertIn("report reply failed", logs)


if __name__ == "__main__":
    unittest.main()
