import json
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
import sys
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.daily_robot.core import _load_input, _apply_alias, org_sync


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
