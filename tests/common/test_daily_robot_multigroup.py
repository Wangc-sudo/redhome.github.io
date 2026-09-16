import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]
import sys
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot.core import (
    iter_groups, fetch_status, send_group, do_remind, do_check, org_sync,
)


def _group(key, name, projects, members=None, cc=None):
    return {
        "key": key,
        "name": name,
        "projects": projects,
        "robot": {"mode": "groupSend", "robotCode": f"rc_{key}", "openConversationId": f"oc_{key}"},
        "members": members or {},
        "ccUsers": cc or {},
        "broadcastExclude": [],
        "totalPrefixes": projects,
    }


def _multi_config(tmp):
    return {
        "logDir": str(tmp),
        "stateFile": str(Path(tmp) / "state.json"),
        "dingtalk": {"appKey": "k", "appSecret": "s"},
        "base": {"baseId": "b", "tableId": "t", "tableUrl": "https://example.com"},
        "calendar": {"month": 9, "restDays": []},
        "org": {"aliases": {}, "archives": {}, "lastSync": ""},
        "groups": [
            _group("junpin", "君品雅院日报表填写", ["君品雅院"], {"张三": "u1"}, {"沈聪": "u9"}),
            _group("xishui", "习水雅院日报填写群", ["习水雅院"], {"李四": "u2"}),
            _group("wanke", "万科&大莲花&团购日报群", ["万科", "大莲花", "团购"], {"王五": "u3"}),
        ],
        "verify": {"openConversationId": "oc_verify", "groupName": "功能验证群"},
    }


class TestIterGroups(unittest.TestCase):
    def test_single_group_wrapped(self):
        config = {
            "region": {"name": "hangzhou", "displayName": "杭州", "totalPrefixes": ["杭中"]},
            "robot": {"mode": "groupSend", "robotCode": "rc", "openConversationId": "oc"},
            "members": {"Alice": "u1"},
            "ccUsers": {"沈聪": "u9"},
        }
        groups = iter_groups(config)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g["name"], "杭州")
        self.assertIsNone(g["projects"])
        self.assertEqual(g["members"], {"Alice": "u1"})
        self.assertEqual(g["robot"]["openConversationId"], "oc")
        self.assertEqual(g["ccUsers"], {"沈聪": "u9"})

    def test_multi_groups_returned(self):
        with tempfile.TemporaryDirectory() as d:
            config = _multi_config(d)
            groups = iter_groups(config)
            self.assertEqual([g["key"] for g in groups], ["junpin", "xishui", "wanke"])
            self.assertEqual(groups[2]["projects"], ["万科", "大莲花", "团购"])


class TestFetchStatusProjects(unittest.TestCase):
    def _mock_client(self, day_col):
        client = MagicMock()
        client.list_fields.return_value = [{"name": day_col}, {"name": "责任人"}, {"name": "项目部"}]
        client.list_records.return_value = [
            {"id": "r1", "fields": {"责任人": "张三", "项目部": "君品雅院", day_col: "100"}},
            {"id": "r2", "fields": {"责任人": "李四", "项目部": "习水雅院", day_col: ""}},
            {"id": "r3", "fields": {"责任人": "王五", "项目部": "万科", day_col: "200"}},
            {"id": "r4", "fields": {"责任人": "君品雅院合计", "项目部": "君品雅院", day_col: "100"}},
        ]
        return client

    @patch("common.daily_robot.core.DingTalkClient")
    def test_projects_filter(self, mock_dt):
        day_col = f"{datetime.now().day}日"
        mock_dt.from_config.return_value = self._mock_client(day_col)
        config = {"dingtalk": {}, "base": {"baseId": "b", "tableId": "t"}}

        filled, unfilled, total, skipped = fetch_status(config, projects=["君品雅院"])
        self.assertEqual(filled, ["张三"])
        self.assertEqual(unfilled, [])
        self.assertIn("君品雅院合计", skipped)

        filled, unfilled, _, _ = fetch_status(config, projects=["万科", "大莲花", "团购"])
        self.assertEqual(filled, ["王五"])
        self.assertEqual(unfilled, [])

        filled, unfilled, _, _ = fetch_status(config)
        self.assertEqual(filled, ["张三", "王五"])
        self.assertEqual(unfilled, ["李四"])


class TestDoRemindMultiGroup(unittest.TestCase):
    @patch("common.daily_robot.core.send_group")
    @patch("common.daily_robot.core.fetch_status")
    def test_remind_only_own_group(self, mock_fetch, mock_send):
        mock_fetch.return_value = (["别人"], ["张三"], 2, [])
        with tempfile.TemporaryDirectory() as d:
            config = _multi_config(d)
            state = {}
            now = datetime(2026, 9, 16, 18, 30)
            group = config["groups"][0]
            do_remind(config, state, now, 16, group=group)

            # 仅按本群 projects 取数
            mock_fetch.assert_called_once_with(config, projects=["君品雅院"])
            # 仅推本群 robot、仅 @ 本群成员
            args = mock_send.call_args
            self.assertEqual(args[0][1]["openConversationId"], "oc_junpin")
            self.assertEqual(args[1].get("at_ids"), ["u1"])
            self.assertIn("张三", args[0][3])
            # state key 带群后缀，不影响其他群
            self.assertIn("remind_20260916_junpin", state)
            self.assertNotIn("remind_20260916", state)

    @patch("common.daily_robot.core.send_group")
    @patch("common.daily_robot.core.fetch_status")
    def test_remind_groups_isolated(self, mock_fetch, mock_send):
        mock_fetch.return_value = ([], ["李四"], 1, [])
        with tempfile.TemporaryDirectory() as d:
            config = _multi_config(d)
            state = {}
            now = datetime(2026, 9, 16, 18, 30)
            for g in config["groups"]:
                do_remind(config, state, now, 16, group=g)
            self.assertEqual(mock_send.call_count, 3)
            targets = [c[0][1]["openConversationId"] for c in mock_send.call_args_list]
            self.assertEqual(targets, ["oc_junpin", "oc_xishui", "oc_wanke"])
            for key in ("junpin", "xishui", "wanke"):
                self.assertIn(f"remind_20260916_{key}", state)


class TestDoCheckMultiGroup(unittest.TestCase):
    @patch("common.daily_robot.core.send_group")
    @patch("common.daily_robot.core.fetch_status")
    def test_check_ding_and_cc_only_own_group(self, mock_fetch, mock_send):
        mock_fetch.return_value = ([], ["张三"], 1, [])
        with tempfile.TemporaryDirectory() as d:
            config = _multi_config(d)
            state = {}
            now = datetime(2026, 9, 16, 20, 0)
            group = config["groups"][0]
            do_check(config, state, now, 16, group=group)

            mock_fetch.assert_called_once_with(config, projects=["君品雅院"])
            args = mock_send.call_args
            self.assertEqual(args[0][1]["openConversationId"], "oc_junpin")
            at_ids = args[1].get("at_ids") or args[0][4]
            # DING 仅本群成员 + 本群知会人，不串其他群
            self.assertIn("u1", at_ids)
            self.assertIn("u9", at_ids)
            self.assertNotIn("u2", at_ids)
            self.assertNotIn("u3", at_ids)
            self.assertIn("check_20260916_junpin", state)


class TestOrgSyncMultiGroup(unittest.TestCase):
    def _write_snapshot(self, directory, names):
        p = Path(directory) / "snap.json"
        p.write_text(
            json.dumps({"deptUserList": [
                {"userInfo": {"name": n, "userId": f"uid_{n}"}} for n in names
            ]}),
            encoding="utf-8",
        )
        return str(p)

    def _mock_client(self):
        client = MagicMock()
        client.list_records.return_value = [
            {"id": "r1", "fields": {"责任人": "张三", "项目部": "君品雅院"}},
            {"id": "r2", "fields": {"责任人": "李四", "项目部": "习水雅院"}},
            {"id": "r3", "fields": {"责任人": "王五", "项目部": "团购"}},
            {"id": "r4", "fields": {"责任人": "赵六", "项目部": "其他项目"}},
            {"id": "r5", "fields": {"责任人": "合计", "项目部": ""}},
        ]
        return client

    @patch("common.daily_robot.core.DingTalkClient")
    def test_one_snapshot_routed_to_three_groups(self, mock_dt):
        mock_dt.from_config.return_value = self._mock_client()
        with tempfile.TemporaryDirectory() as d:
            snap = self._write_snapshot(d, ["张三", "李四", "王五", "赵六"])
            config = _multi_config(d)
            for g in config["groups"]:
                g["members"] = {}

            changed, changes = org_sync(config, {"canyin": snap}, "canyin")

            self.assertTrue(changed)
            junpin, xishui, wanke = config["groups"]
            # 按表内项目部正确路由（团购 ∈ wanke.projects）
            self.assertEqual(junpin["members"], {"张三": "uid_张三"})
            self.assertEqual(xishui["members"], {"李四": "uid_李四"})
            self.assertEqual(wanke["members"], {"王五": "uid_王五"})
            # 赵六项目部无匹配群，不进入任何群
            all_members = {}
            for g in config["groups"]:
                all_members.update(g["members"])
            self.assertNotIn("赵六", all_members)
            # 档案仍记录全部门
            self.assertEqual(len(config["org"]["archives"]["canyin"]), 4)

    @patch("common.daily_robot.core.DingTalkClient")
    def test_remove_clears_all_groups(self, mock_dt):
        mock_dt.from_config.return_value = self._mock_client()
        with tempfile.TemporaryDirectory() as d:
            snap = self._write_snapshot(d, [])
            config = _multi_config(d)
            config["org"]["archives"]["canyin"] = {"张三": "uid_张三"}
            config["groups"][0]["members"] = {"张三": "uid_张三"}

            changed, changes = org_sync(config, {"canyin": snap}, "canyin")

            self.assertTrue(changed)
            self.assertEqual(config["groups"][0]["members"], {})


class TestSendGroupTestMode(unittest.TestCase):
    @patch("common.daily_robot.core.send_markdown")
    @patch("common.daily_robot.core.DingTalkClient")
    def test_test_mode_redirects_to_verify(self, mock_dt, mock_md):
        with tempfile.TemporaryDirectory() as d:
            config = _multi_config(d)
            group = config["groups"][1]
            with patch.dict(os.environ, {"TEST_MODE": "1"}):
                send_group(config, group["robot"], "标题", "正文")
            robot = mock_md.call_args[0][1]
            # TEST_MODE=1 一律重定向 verify 群，禁止发正式群
            self.assertEqual(robot["openConversationId"], "oc_verify")
            self.assertEqual(robot["groupName"], "功能验证群")
            self.assertNotEqual(robot["openConversationId"], "oc_xishui")

    @patch("common.daily_robot.core.send_markdown")
    @patch("common.daily_robot.core.DingTalkClient")
    def test_prod_mode_uses_group_robot(self, mock_dt, mock_md):
        with tempfile.TemporaryDirectory() as d:
            config = _multi_config(d)
            group = config["groups"][1]
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TEST_MODE", None)
                send_group(config, group["robot"], "标题", "正文")
            robot = mock_md.call_args[0][1]
            self.assertEqual(robot["openConversationId"], "oc_xishui")

    @patch("common.daily_robot.core.send_markdown")
    @patch("common.daily_robot.core.DingTalkClient")
    def test_legacy_positional_call(self, mock_dt, mock_md):
        """旧调用 send_group(config, title, text) 仍可用（杭州/绍兴零回归）。"""
        config = {
            "logDir": tempfile.gettempdir(),
            "dingtalk": {"appKey": "k", "appSecret": "s"},
            "robot": {"mode": "groupSend", "robotCode": "rc", "openConversationId": "oc_old"},
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_MODE", None)
            send_group(config, "标题", "正文")
        args = mock_md.call_args[0]
        self.assertEqual(args[1]["openConversationId"], "oc_old")
        self.assertEqual(args[2], "标题")
        self.assertEqual(args[3], "正文")


if __name__ == "__main__":
    unittest.main()
