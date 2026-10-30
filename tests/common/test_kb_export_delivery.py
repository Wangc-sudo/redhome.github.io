# -*- coding: utf-8 -*-

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.kb_qa.export_delivery import (
    GroupExportSender,
    build_group_export_sender,
    is_export_only_request,
    is_export_request,
)


class ExportIntentTests(unittest.TestCase):
    def test_direct_query_and_followup_are_distinguished(self):
        self.assertTrue(is_export_request("查询各渠道昨天销售额并导出 Excel"))
        self.assertFalse(is_export_only_request("查询各渠道昨天销售额并导出 Excel"))
        for text in ("导出 Excel", "把刚才的结果导出Excel", "请下载以上表格"):
            with self.subTest(text=text):
                self.assertTrue(is_export_only_request(text), text)
        self.assertFalse(is_export_request("昨天销售额是多少"))


class _Exports:
    def __init__(self, path, *, snapshot_id="s" * 32):
        self.path = path
        self.snapshot_id = snapshot_id
        self.created = []
        self.generated = []
        self.sent = []

    def latest_snapshot_id(self, uid, conversation_id):
        return self.snapshot_id

    def create(self, snapshot_id, uid, conversation_id, authorizer=None):
        self.created.append((snapshot_id, uid, conversation_id))
        return "e" * 32

    def generate(self, export_id, authorizer=None):
        self.generated.append(export_id)

    def file_for_send(self, export_id, uid, conversation_id, authorizer=None):
        return self.path, "小易查询结果.xlsx"

    def mark_sent(self, export_id, uid, conversation_id, authorizer=None):
        self.sent.append(export_id)

    def status(self, export_id, uid, conversation_id, authorizer=None):
        return {"row_count": 2}


class _Client:
    def __init__(self, errcode=0):
        self.errcode = errcode
        self.calls = []

    def send_group_message(self, robot_code, conversation_id, msg_key, msg_param):
        self.calls.append((robot_code, conversation_id, msg_key, msg_param))
        return {"errcode": self.errcode}


class GroupExportSenderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "random.xlsx"
        self.path.write_bytes(b"frozen-xlsx")

    def _sender(self, exports, client):
        return GroupExportSender(
            export_service=exports,
            dingtalk_client=client,
            robot_code="robot",
            oapi_token_factory=lambda: "token",
            log=lambda _message: None,
        )

    def test_sample_file_delivery_and_sent_audit(self):
        exports = _Exports(self.path)
        client = _Client()
        with patch("common.kb_qa.export_delivery.media.upload_media", return_value="@media") as upload:
            result = self._sender(exports, client).deliver_latest("u1", "c1")
        self.assertTrue(result["sent"])
        self.assertIn("2 行", result["reply"])
        self.assertEqual(["e" * 32], exports.sent)
        self.assertEqual(("file", "小易查询结果.xlsx", b"frozen-xlsx"), upload.call_args.args[1:])
        self.assertEqual("sampleFile", client.calls[0][2])
        self.assertEqual({
            "mediaId": "@media", "fileName": "小易查询结果.xlsx", "fileType": "xlsx",
        }, client.calls[0][3])

    def test_dingtalk_rejection_is_not_marked_sent(self):
        exports = _Exports(self.path)
        with patch("common.kb_qa.export_delivery.media.upload_media", return_value="@media"):
            result = self._sender(exports, _Client(errcode=400)).deliver_latest("u1", "c1")
        self.assertFalse(result["sent"])
        self.assertEqual([], exports.sent)

    def test_missing_snapshot_has_controlled_reply(self):
        exports = _Exports(self.path, snapshot_id=None)
        result = self._sender(exports, _Client()).deliver_latest("u1", "c1")
        self.assertFalse(result["sent"])
        self.assertIn("暂无可导出", result["reply"])
        self.assertEqual([], exports.created)

    def test_group_switch_defaults_off_and_can_build_independently(self):
        env = {
            "KB_DINGTALK_APP_KEY": "ak",
            "KB_DINGTALK_APP_SECRET": "as",
            "KB_EXPORT_DIR": self.directory.name,
        }
        self.assertIsNone(build_group_export_sender(
            connection_factory=lambda: None,
            reply_client=_Client(),
            robot_code="ak",
            environ=env,
        ))
        env["KB_EXPORT_GROUP_ENABLE"] = "1"
        self.assertIsNone(build_group_export_sender(
            connection_factory=lambda: None,
            reply_client=_Client(),
            robot_code="ak",
            environ=env,
        ))
        env["KB_EXPORT_ENABLE"] = "1"
        sender = build_group_export_sender(
            connection_factory=lambda: None,
            reply_client=_Client(),
            robot_code="ak",
            environ=env,
        )
        self.assertIsInstance(sender, GroupExportSender)


if __name__ == "__main__":
    unittest.main()
