"""渠道月目标程序导入（channel_target）离线单测：sqlite 内存库 + 临时 JSON。"""

import json
import sqlite3
import unittest

from common.public_data.channel_target import (
    ChannelTargetError,
    load_target_rows,
    replace_snapshot,
)


class _PctCursor:
    def __init__(self, sqlite_cursor):
        self._c = sqlite_cursor

    def execute(self, sql, params=None):
        self._c.execute(sql.replace("%s", "?"), params or ())

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    @property
    def rowcount(self):
        return self._c.rowcount

    def close(self):
        self._c.close()


class _SqliteConn:
    def __init__(self, sqlite_conn):
        self._conn = sqlite_conn
        self._conn.row_factory = sqlite3.Row

    def cursor(self):
        return _PctCursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        pass


def _make_db(with_roster=False):
    conn = sqlite3.connect(":memory:")
    wrapper = _SqliteConn(conn)
    wrapper.cursor().execute(
        "CREATE TABLE channel_monthly_target ("
        " store_name VARCHAR(128), channel VARCHAR(32),"
        " monthly_target REAL, responsible_person TEXT,"
        " dingtalk_record_id VARCHAR(128), synced_at VARCHAR(32),"
        " sync_run_id VARCHAR(64))"
    )
    if with_roster:
        wrapper.cursor().execute(
            "CREATE TABLE dim_report_roster ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " scope VARCHAR(32) NOT NULL, entity_type VARCHAR(16) NOT NULL,"
            " entity_key VARCHAR(128) NOT NULL, person_name VARCHAR(64) NOT NULL,"
            " aliases TEXT DEFAULT NULL, enabled INTEGER NOT NULL DEFAULT 1,"
            " note VARCHAR(255) DEFAULT NULL, updated_by VARCHAR(64) DEFAULT NULL,"
            " updated_at VARCHAR(32) DEFAULT NULL,"
            " channel VARCHAR(32) DEFAULT NULL, store_no INT DEFAULT NULL,"
            " UNIQUE (scope, entity_type, entity_key, person_name))"
        )
        wrapper.cursor().execute(
            "CREATE TABLE dim_report_roster_audit ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " actor VARCHAR(64) NOT NULL, action VARCHAR(16) NOT NULL,"
            " scope VARCHAR(32) NOT NULL, entity_type VARCHAR(16) NOT NULL,"
            " entity_key VARCHAR(128) NOT NULL, person_name VARCHAR(64) NOT NULL,"
            " detail VARCHAR(255) DEFAULT NULL, created_at VARCHAR(32) NOT NULL)"
        )
    conn.commit()
    return wrapper


def _write_json(tmp_path, payload):
    path = tmp_path / "target.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


_VALID = {
    "rows": [
        {"channel": "京东", "store_name": "JD购喝",
         "monthly_target": 1500000, "owners": ["娄灿斌"]},
        {"channel": "猫超", "store_name": "MC猫超",
         "monthly_target": None, "owners": []},
    ]
}


class LoadTargetRowsTests(unittest.TestCase):
    def test_valid_file_normalizes(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = _write_json(Path(tmp), _VALID)
            rows = load_target_rows(path)
        self.assertEqual(2, len(rows))
        self.assertEqual("JD购喝", rows[0]["store_name"])
        self.assertEqual(1500000, rows[0]["monthly_target"])
        self.assertEqual(["娄灿斌"], rows[0]["owners"])
        self.assertIsNone(rows[1]["monthly_target"])

    def test_invalid_inputs_rejected(self):
        import tempfile
        from pathlib import Path

        cases = [
            {"rows": []},                                    # 空 rows
            {"rows": [{"channel": "商超", "store_name": "店",
                       "monthly_target": 1, "owners": []}]},  # 未注册渠道
            {"rows": [{"channel": "京东", "store_name": " ",
                       "monthly_target": 1, "owners": []}]},  # 空店名
            {"rows": [{"channel": "京东", "store_name": "店",
                       "monthly_target": -1, "owners": []}]},  # 负目标
            {"rows": [{"channel": "京东", "store_name": "店",
                       "monthly_target": 1, "owners": "张三"}]},  # owners 非列表
            {"rows": [{"channel": "京东", "store_name": "店",
                       "monthly_target": 1, "owners": []},
                      {"channel": "天猫", "store_name": "店",
                       "monthly_target": 1, "owners": []}]},  # 店名重复
        ]
        for payload in cases:
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_json(Path(tmp), payload)
                with self.assertRaises(ChannelTargetError, msg=payload):
                    load_target_rows(path)


class ReplaceSnapshotTests(unittest.TestCase):
    def test_replace_swaps_snapshot_atomically(self):
        db = _make_db()
        cur = db.cursor()
        cur.execute(
            "INSERT INTO channel_monthly_target VALUES "
            "('旧店A', '京东', 1, '[]', 'ai:1', '2026-09-01', 'sync-1'),"
            "('旧店B', '天猫', 2, '[]', 'ai:2', '2026-09-01', 'sync-1')"
        )
        db.commit()

        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_json(Path(tmp), _VALID)
            new_rows = load_target_rows(path)

        deleted, inserted = replace_snapshot(db, new_rows)
        self.assertEqual((2, 2), (deleted, inserted))

        cur = db.cursor()
        cur.execute(
            "SELECT store_name, channel, monthly_target, responsible_person,"
            " dingtalk_record_id, sync_run_id FROM channel_monthly_target"
            " ORDER BY store_name"
        )
        result = cur.fetchall()
        self.assertEqual(2, len(result))
        stores = [r["store_name"] for r in result]
        self.assertNotIn("旧店A", stores)
        self.assertNotIn("旧店B", stores)
        jd = [r for r in result if r["store_name"] == "JD购喝"][0]
        self.assertEqual("京东", jd["channel"])
        self.assertEqual(1500000, jd["monthly_target"])
        self.assertEqual([{"name": "娄灿斌"}], json.loads(jd["responsible_person"]))
        self.assertEqual("manual:JD购喝", jd["dingtalk_record_id"])
        self.assertEqual("manual:load-channel-target", jd["sync_run_id"])


class PublishSnapshotTests(unittest.TestCase):
    """发布快照（S2）：目标行 + 名册投影 owners + 发布审计。"""

    def _roster_db(self):
        db = _make_db(with_roster=True)
        cur = db.cursor()
        cur.execute(
            "INSERT INTO dim_report_roster "
            "(scope, entity_type, entity_key, person_name, enabled) VALUES "
            "('qudao', 'store', 'JD购喝', '娄灿斌', 1),"
            "('qudao', 'store', 'JD购喝', '共管人', 1),"
            "('qudao', 'store', 'JD金沙', '停用人', 0)"
        )
        db.commit()
        return db

    def test_validate_target_rows_rules(self):
        from common.public_data.channel_target import validate_target_rows
        self.assertEqual(1, len(validate_target_rows(
            [{"channel": "京东", "store_name": "店", "monthly_target": None}]
        )))
        for bad in (
            [], [{"channel": "商超", "store_name": "店", "monthly_target": 1}],
            [{"channel": "京东", "store_name": " ", "monthly_target": 1}],
            [{"channel": "京东", "store_name": "店", "monthly_target": -1}],
            [{"channel": "京东", "store_name": "店", "monthly_target": 1},
             {"channel": "天猫", "store_name": "店", "monthly_target": 1}],
        ):
            with self.assertRaises(ChannelTargetError, msg=bad):
                validate_target_rows(bad)

    def test_publish_projects_owners_from_roster(self):
        from common.public_data.channel_target import publish_snapshot
        db = self._roster_db()
        deleted, inserted, missing = publish_snapshot(db, [
            {"channel": "京东", "store_name": "JD购喝", "monthly_target": 1500000},
            {"channel": "京东", "store_name": "JD无负责人店", "monthly_target": 100},
        ], actor="admin")

        self.assertEqual((0, 2), (deleted, inserted))
        self.assertEqual(["JD无负责人店"], missing)
        cur = db.cursor()
        cur.execute(
            "SELECT store_name, responsible_person, sync_run_id "
            "FROM channel_monthly_target ORDER BY store_name"
        )
        rows = {r["store_name"]: r for r in cur.fetchall()}
        owners = json.loads(rows["JD购喝"]["responsible_person"])
        self.assertEqual([{"name": "共管人"}, {"name": "娄灿斌"}],
                         sorted(owners, key=lambda x: x["name"]))
        self.assertEqual([], json.loads(rows["JD无负责人店"]["responsible_person"]))
        self.assertEqual("manual:load-channel-target", rows["JD购喝"]["sync_run_id"])
        # 发布审计
        cur.execute(
            "SELECT action, scope, detail FROM dim_report_roster_audit"
        )
        audit = cur.fetchall()
        self.assertEqual(1, len(audit))
        self.assertEqual("publish", audit[0]["action"])
        self.assertIn("合计 1500100", audit[0]["detail"])
        self.assertIn("无负责人 1 店", audit[0]["detail"])


if __name__ == "__main__":
    unittest.main()
