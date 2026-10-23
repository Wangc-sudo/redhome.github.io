# -*- coding: utf-8 -*-
"""RoutedStreamHandler（2026-10-24 机器人合并）：单连接总路由的分发与断言。"""

import asyncio
import unittest
from unittest.mock import Mock, patch

from dingtalk_stream import AckMessage

from common.gateway.stream_router import (
    RoutedStreamHandler,
    assert_no_group_overlap,
)
from common.region_config import RegionConfig

_STATUS_OK = AckMessage.STATUS_OK

_CFG_HZ = RegionConfig(
    region="hangzhou", display="杭州",
    table_url="https://example.com/table",
    robot_code="rc", open_conversation_id="conv-report",
    aliases={}, cc_user_ids=(),
)


class _SpyHandler:
    """记录被委托的 callback。"""

    def __init__(self):
        self.calls = []

    async def process(self, callback):
        self.calls.append(callback)
        return _STATUS_OK, "OK"


def _run(handler, conversation_id):
    incoming = Mock()
    incoming.conversation_id = conversation_id
    callback = Mock(data={"conversationId": conversation_id})
    with patch(
        "dingtalk_stream.ChatbotMessage.from_dict", return_value=incoming
    ):
        return asyncio.run(handler.process(callback))


def _router(kb_ids=("conv-kb",)):
    report = _SpyHandler()
    kb = _SpyHandler()
    handler = RoutedStreamHandler(
        report_handler=report, kb_handler=kb,
        region_configs={"hangzhou": _CFG_HZ},
        kb_conversation_ids=kb_ids,
        log=lambda _: None,
    )
    return handler, report, kb


class RoutedStreamHandlerTests(unittest.TestCase):
    def test_report_group_goes_to_report_handler(self):
        handler, report, kb = _router()
        status, _ = _run(handler, "conv-report")
        self.assertEqual(_STATUS_OK, status)
        self.assertEqual(1, len(report.calls))
        self.assertEqual(0, len(kb.calls))

    def test_kb_group_goes_to_kb_handler(self):
        handler, report, kb = _router()
        status, _ = _run(handler, "conv-kb")
        self.assertEqual(_STATUS_OK, status)
        self.assertEqual(0, len(report.calls))
        self.assertEqual(1, len(kb.calls))

    def test_unregistered_group_is_silent(self):
        logs = []
        handler, report, kb = _router()
        handler._log = logs.append
        status, _ = _run(handler, "conv-unknown")
        self.assertEqual(_STATUS_OK, status)
        self.assertEqual([], report.calls)
        self.assertEqual([], kb.calls)
        self.assertEqual(1, len(logs))

    def test_report_group_wins_over_kb_on_overlap(self):
        """同群两边都登记时（断言被绕过的运行期兜底）：报数优先。"""
        cfg = RegionConfig(
            region="hangzhou", display="杭州",
            table_url="https://example.com/table",
            robot_code="rc", open_conversation_id="conv-both",
            aliases={}, cc_user_ids=(),
        )
        report = _SpyHandler()
        kb = _SpyHandler()
        handler = RoutedStreamHandler(
            report_handler=report, kb_handler=kb,
            region_configs={"hangzhou": cfg},
            kb_conversation_ids=("conv-both",),
            log=lambda _: None,
        )
        _run(handler, "conv-both")
        self.assertEqual(1, len(report.calls))
        self.assertEqual(0, len(kb.calls))

    def test_malformed_callback_is_silent_ack(self):
        handler, report, kb = _router()
        with patch(
            "dingtalk_stream.ChatbotMessage.from_dict",
            side_effect=ValueError("bad"),
        ):
            status, _ = asyncio.run(handler.process(Mock(data={})))
        self.assertEqual(_STATUS_OK, status)
        self.assertEqual([], report.calls)
        self.assertEqual([], kb.calls)


class GroupOverlapAssertionTests(unittest.TestCase):
    def test_overlap_rejected(self):
        with self.assertRaises(RuntimeError):
            assert_no_group_overlap({"hangzhou": _CFG_HZ}, ("conv-report",))

    def test_disjoint_passes(self):
        assert_no_group_overlap({"hangzhou": _CFG_HZ}, ("conv-kb",))
        assert_no_group_overlap({"hangzhou": _CFG_HZ}, ())
        assert_no_group_overlap({"hangzhou": _CFG_HZ}, None)

    def test_blank_conversation_ids_ignored(self):
        cfg = RegionConfig(
            region="hq", display="总部", table_url="", robot_code="",
            open_conversation_id="", aliases={}, cc_user_ids=(),
        )
        assert_no_group_overlap({"hq": cfg}, ("conv-kb",))


class MergeSwitchTests(unittest.TestCase):
    def test_disabled_by_default(self):
        from common.gateway.cli import kb_qa_merge_enabled

        self.assertFalse(kb_qa_merge_enabled({}))
        self.assertFalse(kb_qa_merge_enabled({"GATEWAY_KB_QA_ENABLE": "0"}))
        self.assertFalse(kb_qa_merge_enabled({"GATEWAY_KB_QA_ENABLE": " "}))

    def test_enabled_values(self):
        from common.gateway.cli import kb_qa_merge_enabled

        for value in ("1", "true", "YES"):
            with self.subTest(value=value):
                self.assertTrue(
                    kb_qa_merge_enabled({"GATEWAY_KB_QA_ENABLE": value}))


if __name__ == "__main__":
    unittest.main()
