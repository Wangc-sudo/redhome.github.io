import os
import unittest
from pathlib import Path

# 把 repo 根目录加入路径，确保能 import common
REPO_ROOT = Path(__file__).resolve().parents[2]
import sys

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common import test_group


class TestResolveTarget(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("TEST_MODE", None)

    def test_prod_mode_returns_original_config(self):
        os.environ.pop("TEST_MODE", None)
        original = {"robotCode": "rc", "openConversationId": "prod-cid"}
        # 通过注入测试配置绕过真实文件
        test_group.load_test_groups = lambda path=None: {
            "default": {
                "groupName": "功能验证群",
                "groupNumber": "197925004762",
                "openConversationId": "test-cid",
            }
        }
        result = test_group.resolve_target(original)
        self.assertEqual(result, original)

    def test_test_mode_overrides_open_conversation_id(self):
        os.environ["TEST_MODE"] = "1"
        original = {"robotCode": "rc", "openConversationId": "prod-cid"}
        test_group.load_test_groups = lambda path=None: {
            "default": {
                "groupName": "功能验证群",
                "groupNumber": "197925004762",
                "openConversationId": "test-cid",
            }
        }
        result = test_group.resolve_target(original)
        self.assertEqual(result["robotCode"], "rc")
        self.assertEqual(result["openConversationId"], "test-cid")
        self.assertEqual(result["groupName"], "功能验证群")
        self.assertEqual(result["mode"], "groupSend")

    def test_explicit_mode_overrides_env(self):
        os.environ["TEST_MODE"] = "1"
        original = {"robotCode": "rc", "openConversationId": "prod-cid"}
        test_group.load_test_groups = lambda path=None: {
            "default": {"openConversationId": "test-cid"}
        }
        result = test_group.resolve_target(original, mode="prod")
        self.assertEqual(result, original)

    def test_webhook_test_group(self):
        os.environ["TEST_MODE"] = "1"
        original = {
            "mode": "webhook",
            "webhook": "https://prod.example.com/webhook",
            "secret": "prod-secret",
        }
        test_group.load_test_groups = lambda path=None: {
            "default": {
                "groupName": "功能验证群",
                "webhook": "https://test.example.com/webhook",
                "secret": "test-secret",
            }
        }
        result = test_group.resolve_target(original)
        self.assertEqual(result["mode"], "webhook")
        self.assertEqual(result["webhook"], "https://test.example.com/webhook")
        self.assertEqual(result["secret"], "test-secret")
        self.assertNotIn("openConversationId", result)


if __name__ == "__main__":
    unittest.main()
