import unittest
from pathlib import Path

# 把 repo 根目录加入路径，确保能 import common
REPO_ROOT = Path(__file__).resolve().parents[2]
import sys

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.dingtalk import DingTalkClient, DingTalkError, send_markdown


class FakeClient:
    """模拟 DingTalkClient 的两个 markdown 发送方法。"""

    def __init__(self):
        self.calls = []

    def send_webhook_markdown(self, webhook, title, text, secret="", at_mobiles=None):
        self.calls.append({
            "method": "webhook",
            "webhook": webhook,
            "title": title,
            "text": text,
            "secret": secret,
            "at_mobiles": at_mobiles,
        })
        return {"errcode": 0}

    def send_group_markdown(self, robot_code, conv_id, title, text, at_user_ids=None):
        self.calls.append({
            "method": "groupSend",
            "robot_code": robot_code,
            "conv_id": conv_id,
            "title": title,
            "text": text,
            "at_user_ids": at_user_ids,
        })
        return {"errcode": 0}


class TestSendMarkdown(unittest.TestCase):
    def test_webhook_mode(self):
        client = FakeClient()
        push = {
            "mode": "webhook",
            "webhook": "https://example.com/webhook",
            "secret": "s",
        }
        send_markdown(client, push, "标题", "正文", at_mobiles=["13800138000"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["method"], "webhook")
        self.assertEqual(client.calls[0]["webhook"], "https://example.com/webhook")
        self.assertEqual(client.calls[0]["secret"], "s")
        self.assertEqual(client.calls[0]["at_mobiles"], ["13800138000"])

    def test_group_send_mode(self):
        client = FakeClient()
        push = {
            "mode": "groupSend",
            "robotCode": "rc",
            "openConversationId": "cid",
        }
        send_markdown(client, push, "标题", "正文", at_user_ids=["u1", "u2"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["method"], "groupSend")
        self.assertEqual(client.calls[0]["robot_code"], "rc")
        self.assertEqual(client.calls[0]["conv_id"], "cid")
        self.assertEqual(client.calls[0]["at_user_ids"], ["u1", "u2"])

    def test_default_mode_is_webhook(self):
        client = FakeClient()
        push = {"webhook": "https://example.com/webhook"}
        send_markdown(client, push, "标题", "正文")
        self.assertEqual(client.calls[0]["method"], "webhook")

    def test_unknown_mode_raises(self):
        client = FakeClient()
        push = {"mode": "sms"}
        with self.assertRaises(DingTalkError):
            send_markdown(client, push, "标题", "正文")


if __name__ == "__main__":
    unittest.main()
