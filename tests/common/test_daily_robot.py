import json
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
    _load_input, _apply_alias, _parse_num, org_sync,
    today_info, load_state, save_state, do_remind, do_check,
)


class TestLoadInput(unittest.TestCase):
    def test_loads_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "snap.json"
            p.write_text(
                json.dumps({"deptUserList": [{"userInfo": {"name": "Alice", "userId": "u1"}}]}),
                encoding="utf-8",
            )
            self.assertEqual(_load_input(p), {"Alice": "u1"})

    def test_missing_file_returns_none(self):
        self.assertIsNone(_load_input("/nonexistent/path.json"))


class TestApplyAlias(unittest.TestCase):
    def test_alias_replacement(self):
        self.assertEqual(_apply_alias("Alice", {"Alice": "A"}), "A")

    def test_no_alias_returns_name(self):
        self.assertEqual(_apply_alias("Alice", {}), "Alice")


class TestOrgSync(unittest.TestCase):
    def _config(self, log_dir):
        return {
            "logDir": str(log_dir),
            "org": {
                "aliases": {},
                "archives": {},
                "lastSync": "",
            },
            "members": {},
        }

    def _write_snapshot(self, directory, data):
        p = Path(directory) / "snap.json"
        p.write_text(json.dumps({"deptUserList": data}), encoding="utf-8")
        return str(p)

    def test_adds_new_member_to_active_region(self):
        with tempfile.TemporaryDirectory() as d:
            snap = self._write_snapshot(
                d, [{"userInfo": {"name": "Alice", "userId": "u1"}}]
            )
            config = self._config(d)
            changed, changes = org_sync(config, {"shaoxing": snap}, "shaoxing")
            self.assertTrue(changed)
            self.assertEqual(config["members"], {"Alice": "u1"})
            self.assertEqual(config["org"]["archives"]["shaoxing"], {"Alice": "u1"})
            self.assertTrue(any("新增" in c for c in changes))

    def test_no_changes_when_snapshot_matches_archive(self):
        with tempfile.TemporaryDirectory() as d:
            snap = self._write_snapshot(
                d, [{"userInfo": {"name": "Alice", "userId": "u1"}}]
            )
            config = self._config(d)
            config["org"]["archives"]["shaoxing"] = {"Alice": "u1"}
            config["members"]["Alice"] = "u1"
            changed, changes = org_sync(config, {"shaoxing": snap}, "shaoxing")
            self.assertFalse(changed)
            self.assertEqual(changes, [])

    def test_removes_member_from_active_region(self):
        with tempfile.TemporaryDirectory() as d:
            snap = self._write_snapshot(d, [])
            config = self._config(d)
            config["org"]["archives"]["shaoxing"] = {"Alice": "u1"}
            config["members"]["Alice"] = "u1"
            changed, changes = org_sync(config, {"shaoxing": snap}, "shaoxing")
            self.assertTrue(changed)
            self.assertEqual(config["members"], {})
            self.assertEqual(config["org"]["archives"]["shaoxing"], {})
            self.assertTrue(any("离职" in c for c in changes))

    def test_updates_members_when_user_id_changes(self):
        with tempfile.TemporaryDirectory() as d:
            snap = self._write_snapshot(
                d, [{"userInfo": {"name": "Alice", "userId": "u2"}}]
            )
            config = self._config(d)
            config["org"]["archives"]["shaoxing"] = {"Alice": "u1"}
            config["members"]["Alice"] = "u1"
            changed, changes = org_sync(config, {"shaoxing": snap}, "shaoxing")
            self.assertTrue(changed)
            self.assertEqual(config["members"], {"Alice": "u2"})
            self.assertEqual(config["org"]["archives"]["shaoxing"], {"Alice": "u2"})
            self.assertTrue(any("ID变更" in c for c in changes))


class TestParseNum(unittest.TestCase):
    def test_integer_string(self):
        self.assertEqual(_parse_num("123"), 123.0)

    def test_float_string(self):
        self.assertEqual(_parse_num("123.45"), 123.45)

    def test_comma_separator(self):
        self.assertEqual(_parse_num("1,234.5"), 1234.5)

    def test_none_returns_none(self):
        self.assertIsNone(_parse_num(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_num(""))

    def test_invalid_string_returns_none(self):
        self.assertIsNone(_parse_num("abc"))


class TestTodayInfo(unittest.TestCase):
    def _cal(self, month=9, rest=None):
        return {"month": month, "restDays": rest or []}

    def test_normal_workday(self):
        now = datetime(2026, 9, 1)
        _, day, err = today_info(self._cal(), now)
        self.assertEqual(day, 1)
        self.assertIsNone(err)

    def test_rest_day_returns_none_day(self):
        now = datetime(2026, 9, 6)
        _, day, err = today_info(self._cal(rest=[6]), now)
        self.assertIsNone(day)
        self.assertIsNone(err)

    def test_month_mismatch_returns_error(self):
        now = datetime(2026, 10, 1)
        _, day, err = today_info(self._cal(month=9), now)
        self.assertIsNone(day)
        self.assertIn("不符", err)


class TestStateFile(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            sf = Path(d) / "state.json"
            save_state(sf, {"key": "value"})
            self.assertEqual(load_state(sf), {"key": "value"})

    def test_load_missing_returns_empty(self):
        self.assertEqual(load_state("/nonexistent/state.json"), {})

    def test_load_corrupt_raises(self):
        with tempfile.TemporaryDirectory() as d:
            sf = Path(d) / "state.json"
            sf.write_text("not json", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                load_state(sf)


class TestDoRemind(unittest.TestCase):
    def _config(self, tmp):
        return {
            "logDir": str(tmp),
            "stateFile": str(Path(tmp) / "state.json"),
            "region": {"name": "test", "displayName": "测试"},
            "base": {"tableUrl": "https://example.com"},
            "members": {"Alice": "u1", "Bob": "u2"},
            "robot": {"mode": "groupSend", "robotCode": "rc", "openConversationId": "oc"},
            "dingtalk": {"appKey": "k", "appSecret": "s"},
        }

    @patch("common.daily_robot.core.send_group")
    @patch("common.daily_robot.core.fetch_status")
    def test_sends_reminder_to_unfilled(self, mock_fetch, mock_send):
        mock_fetch.return_value = (["Alice"], ["Bob"], 2, [])
        with tempfile.TemporaryDirectory() as d:
            config = self._config(d)
            state = {}
            now = datetime(2026, 9, 1, 18, 30)
            do_remind(config, state, now, 1)
            mock_send.assert_called_once()
            args = mock_send.call_args
            self.assertIn("Bob", args[0][2])
            self.assertIn("remind_20260901", state)
            self.assertIsNotNone(state["remind_20260901"])

    @patch("common.daily_robot.core.send_group")
    @patch("common.daily_robot.core.fetch_status")
    def test_skips_when_all_filled(self, mock_fetch, mock_send):
        mock_fetch.return_value = (["Alice", "Bob"], [], 2, [])
        with tempfile.TemporaryDirectory() as d:
            config = self._config(d)
            state = {}
            do_remind(config, state, datetime(2026, 9, 1, 18, 30), 1)
            mock_send.assert_not_called()

    @patch("common.daily_robot.core.send_group")
    @patch("common.daily_robot.core.fetch_status")
    def test_skips_when_already_sent(self, mock_fetch, mock_send):
        with tempfile.TemporaryDirectory() as d:
            config = self._config(d)
            state = {"remind_20260901": "2026-09-01T18:30:00"}
            do_remind(config, state, datetime(2026, 9, 1, 18, 30), 1)
            mock_fetch.assert_not_called()
            mock_send.assert_not_called()


class TestDoCheck(unittest.TestCase):
    def _config(self, tmp):
        return {
            "logDir": str(tmp),
            "stateFile": str(Path(tmp) / "state.json"),
            "region": {"name": "test", "displayName": "测试"},
            "base": {"tableUrl": "https://example.com"},
            "members": {"Alice": "u1", "Bob": "u2"},
            "ccUsers": {"沈聪": "u9"},
            "robot": {"mode": "groupSend", "robotCode": "rc", "openConversationId": "oc"},
            "dingtalk": {"appKey": "k", "appSecret": "s"},
        }

    @patch("common.daily_robot.core.send_group")
    @patch("common.daily_robot.core.fetch_status")
    def test_sends_check_with_cc(self, mock_fetch, mock_send):
        mock_fetch.return_value = (["Alice"], ["Bob"], 2, [])
        with tempfile.TemporaryDirectory() as d:
            config = self._config(d)
            state = {}
            now = datetime(2026, 9, 1, 20, 0)
            do_check(config, state, now, 1)
            mock_send.assert_called_once()
            at_ids = mock_send.call_args[1].get("at_ids") or mock_send.call_args[0][3]
            self.assertIn("u2", at_ids)
            self.assertIn("u9", at_ids)

    @patch("common.daily_robot.core.send_group")
    @patch("common.daily_robot.core.fetch_status")
    def test_skips_when_all_filled(self, mock_fetch, mock_send):
        mock_fetch.return_value = (["Alice", "Bob"], [], 2, [])
        with tempfile.TemporaryDirectory() as d:
            config = self._config(d)
            state = {}
            do_check(config, state, datetime(2026, 9, 1, 20, 0), 1)
            mock_send.assert_not_called()
