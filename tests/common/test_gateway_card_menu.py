"""Tests for the interactive card menu (PoC): buttons, callback routing."""

import asyncio
import json
import unittest
from datetime import datetime
from unittest.mock import Mock

from dingtalk_stream import AckMessage

from common.gateway.card_menu import (
    MenuCardCallbackHandler,
    build_menu_buttons,
    extract_aux_action,
    is_menu_request,
)
from common.gateway.report_intake import IntakeOutcome
from common.region_config import RegionConfig

_STATUS_OK = AckMessage.STATUS_OK

_CFG = RegionConfig(
    region="hangzhou", display="杭州",
    table_url="https://example.com/table",
    robot_code="rc", open_conversation_id="conv-hz",
    aliases={}, cc_user_ids=(),
)


class MenuRequestTests(unittest.TestCase):

    def test_menu_words(self):
        self.assertTrue(is_menu_request("/菜单"))
        self.assertTrue(is_menu_request("/menu"))
        self.assertTrue(is_menu_request("@提醒事项 /菜单"))
        self.assertTrue(is_menu_request("  /菜单  "))

    def test_non_menu(self):
        self.assertFalse(is_menu_request("/未填"))
        self.assertFalse(is_menu_request("12800"))
        self.assertFalse(is_menu_request(None))
        self.assertFalse(is_menu_request(""))


class MenuButtonTests(unittest.TestCase):

    def test_buttons_carry_request_action_and_aux_key(self):
        buttons = build_menu_buttons()
        self.assertEqual(len(buttons), 4)
        aux_keys = {b["action"]["privateData"]["aux"] for b in buttons}
        self.assertEqual(aux_keys, {"未填", "我的", "门店", "帮助"})
        for button in buttons:
            self.assertEqual(button["action"]["actionType"], "request")
            self.assertIn("text", button)

    def test_backfill_is_not_on_card(self):
        aux_keys = {b["action"]["privateData"]["aux"] for b in build_menu_buttons()}
        self.assertNotIn("补签", aux_keys)


class ExtractActionTests(unittest.TestCase):

    def test_content_as_json_string(self):
        data = {
            "userId": "u-admin",
            "outTrackId": "card-1",
            "content": json.dumps({
                "openConversationId": "conv-hz",
                "cardPrivateData": {
                    "action": {"privateData": {"aux": "我的"}}
                },
            }),
        }
        self.assertEqual(
            extract_aux_action(data), ("我的", "u-admin", "conv-hz", "card-1")
        )

    def test_content_as_dict(self):
        data = {
            "userId": "u1",
            "content": {
                "openConversationId": "conv-hz",
                "cardPrivateData": {"action": {"privateData": {"aux": "未填"}}},
                "cardInstanceId": "card-2",
            },
        }
        self.assertEqual(
            extract_aux_action(data), ("未填", "u1", "conv-hz", "card-2")
        )

    def test_missing_private_data(self):
        aux, user_id, conv, card_id = extract_aux_action({"userId": "u1"})
        self.assertIsNone(aux)
        aux, *_ = extract_aux_action(None)
        self.assertIsNone(aux)


def _handler(*, report_handler=None, conn=None, card_factory=None):
    logs = []
    conn = conn or Mock()
    updates = []

    if card_factory is None:
        def card_factory(client, incoming):
            card = Mock()
            card.update = lambda md, buttons, tips="": updates.append(md)
            return card

    handler = MenuCardCallbackHandler(
        region_configs={"hangzhou": _CFG},
        connection_factory=lambda: conn,
        report_handler=report_handler,
        now=lambda: datetime(2026, 9, 11, 17, 30),
        log=logs.append,
        card_factory=card_factory,
    )
    return handler, conn, updates, logs


def _callback(aux="未填", user_id="u1", conv="conv-hz"):
    return Mock(data={
        "userId": user_id,
        "outTrackId": "card-1",
        "content": json.dumps({
            "openConversationId": conv,
            "cardPrivateData": {"action": {"privateData": {"aux": aux}}},
        }),
    })


class MenuCardCallbackTests(unittest.TestCase):

    def test_routes_button_to_aux_command_and_updates_card(self):
        outcome = IntakeOutcome("aux", "📋 还有 2 位未填报", region="hangzhou")
        report_handler = Mock(return_value=outcome)
        handler, conn, updates, _ = _handler(report_handler=report_handler)

        status, message = asyncio.run(handler.process(_callback(aux="我的")))

        self.assertEqual(status, _STATUS_OK)
        self.assertEqual(message, "已更新")
        kwargs = report_handler.call_args.kwargs
        self.assertIs(kwargs["region_cfg"], _CFG)
        self.assertEqual(kwargs["text"], "/我的")
        self.assertEqual(kwargs["sender_uid"], "u1")
        conn.commit.assert_called_once()
        self.assertEqual(updates, ["📋 还有 2 位未填报"])

    def test_backfill_is_rejected_on_card(self):
        report_handler = Mock()
        handler, _, updates, _ = _handler(report_handler=report_handler)

        status, _ = asyncio.run(handler.process(_callback(aux="补签")))

        self.assertEqual(status, _STATUS_OK)
        report_handler.assert_not_called()
        self.assertEqual(updates, [])

    def test_unrouted_conversation_is_skipped(self):
        report_handler = Mock()
        handler, _, _, logs = _handler(report_handler=report_handler)

        asyncio.run(handler.process(_callback(conv="conv-x")))

        report_handler.assert_not_called()
        self.assertIn("card callback unrouted", logs)

    def test_handler_failure_rolls_back_and_updates_error(self):
        report_handler = Mock(side_effect=RuntimeError("secret-detail"))
        handler, conn, updates, logs = _handler(report_handler=report_handler)

        status, message = asyncio.run(handler.process(_callback()))

        self.assertEqual(status, _STATUS_OK)
        conn.rollback.assert_called_once()
        self.assertEqual(len(updates), 1)
        self.assertIn("⚠️", updates[0])
        self.assertNotIn("secret-detail", updates[0])
        self.assertNotIn("secret-detail", " ".join(logs))

    def test_missing_aux_is_acked_silently(self):
        report_handler = Mock()
        handler, _, updates, _ = _handler(report_handler=report_handler)

        status, _ = asyncio.run(handler.process(Mock(data={"userId": "u1"})))

        self.assertEqual(status, _STATUS_OK)
        report_handler.assert_not_called()
        self.assertEqual(updates, [])


class StreamMenuBranchTests(unittest.TestCase):
    """stream_handler 的 /菜单 拦截（不走进报数路径）。"""

    def test_menu_request_sends_card_not_report(self):
        from common.gateway.stream_handler import StreamReportHandler

        report_handler = Mock()
        sent = []
        handler = StreamReportHandler(
            region_configs={"hangzhou": _CFG},
            connection_factory=lambda: Mock(),
            report_handler=report_handler,
            log=lambda *a: None,
        )
        handler.reply_markdown_button = (
            lambda incoming, markdown, buttons, tips="": sent.append(buttons)
        )
        incoming = Mock()
        incoming.text = Mock(content="/菜单")
        incoming.conversation_id = "conv-hz"
        incoming.sender_staff_id = "u1"

        from unittest.mock import patch
        with patch(
            "dingtalk_stream.ChatbotMessage.from_dict", return_value=incoming
        ):
            status, _ = asyncio.run(handler.process(Mock(data={})))

        self.assertEqual(status, _STATUS_OK)
        report_handler.assert_not_called()
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(sent[0]), 4)

    def test_card_handler_is_registered_on_same_client(self):
        import dingtalk_stream
        from common.gateway.stream_handler import build_stream_client

        handler, card_handler = Mock(), Mock()
        client = Mock()
        build_stream_client(
            "k", "s", handler, client_factory=Mock(return_value=client),
            card_handler=card_handler,
        )

        topics = [
            call.args[0]
            for call in client.register_callback_handler.call_args_list
        ]
        self.assertEqual(
            topics,
            [dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
             dingtalk_stream.Card_Callback_Router_Topic],
        )


if __name__ == "__main__":
    unittest.main()
