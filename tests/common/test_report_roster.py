"""填报人名册（report_roster）离线单测：sqlite 内存库替代 MySQL。

MySQL 方言经一层游标翻译：``%s`` → ``?``、``INSERT IGNORE`` → ``INSERT OR
IGNORE``、``ON DUPLICATE KEY UPDATE`` → sqlite ``ON CONFLICT ... DO UPDATE``
（含 ``VALUES(col)`` → ``excluded.col``）。无需真实 MySQL。
"""

import re
import sqlite3
import unittest

from common.public_data import report_roster
from common.public_data.report_roster import (
    ReportRosterError,
    RosterEntry,
    validate_roster_fields,
)


# -- sqlite 适配 ------------------------------------------------------------

class _PctCursor:
    def __init__(self, sqlite_cursor):
        self._c = sqlite_cursor

    def execute(self, sql, params=None):
        sql = sql.replace("%s", "?")
        sql = sql.replace("INSERT IGNORE INTO", "INSERT OR IGNORE INTO")
        if "ON DUPLICATE KEY UPDATE" in sql:
            sql = sql.replace(
                "ON DUPLICATE KEY UPDATE",
                "ON CONFLICT(`scope`, `entity_type`, `entity_key`, `person_name`)"
                " DO UPDATE SET",
            )
            sql = re.sub(r"VALUES\(`(\w+)`\)", r"excluded.`\1`", sql)
        self._c.execute(sql, params or ())

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


def _make_db(with_targets=False):
    conn = sqlite3.connect(":memory:")
    wrapper = _SqliteConn(conn)
    wrapper.cursor().execute(
        "CREATE TABLE dim_report_roster ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " scope VARCHAR(32) NOT NULL,"
        " entity_type VARCHAR(16) NOT NULL,"
        " entity_key VARCHAR(128) NOT NULL,"
        " person_name VARCHAR(64) NOT NULL,"
        " aliases TEXT DEFAULT NULL,"
        " enabled INTEGER NOT NULL DEFAULT 1,"
        " note VARCHAR(255) DEFAULT NULL,"
        " updated_by VARCHAR(64) DEFAULT NULL,"
        " updated_at VARCHAR(32) DEFAULT NULL,"
        " channel VARCHAR(32) DEFAULT NULL,"
        " store_no INT DEFAULT NULL,"
        " UNIQUE (scope, entity_type, entity_key, person_name))"
    )
    wrapper.cursor().execute(
        "CREATE TABLE dim_report_roster_audit ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " actor VARCHAR(64) NOT NULL,"
        " action VARCHAR(16) NOT NULL,"
        " scope VARCHAR(32) NOT NULL,"
        " entity_type VARCHAR(16) NOT NULL,"
        " entity_key VARCHAR(128) NOT NULL,"
        " person_name VARCHAR(64) NOT NULL,"
        " detail VARCHAR(255) DEFAULT NULL,"
        " created_at VARCHAR(32) NOT NULL)"
    )
    if with_targets:
        wrapper.cursor().execute(
            "CREATE TABLE fact_channel_store_target ("
            " store_name VARCHAR(128), owners_json TEXT,"
            " channel VARCHAR(32), monthly_target REAL)"
        )
    conn.commit()
    return wrapper


def _entry(**kw):
    base = dict(scope="qudao", entity_type="store", entity_key="京东1店",
                person_name="饶佳君")
    base.update(kw)
    return validate_roster_fields(**base)


# -- 写面校验 ----------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def test_valid_normalizes(self):
        entry = validate_roster_fields(
            "qudao", "store", "  京东1店  ", "饶佳君",
            aliases=("小饶", "小饶", "rj"), note=" 共管 ",
        )
        self.assertEqual(entry.entity_key, "京东1店")
        self.assertEqual(entry.aliases, ("小饶", "rj"))  # 去重保序
        self.assertEqual(entry.note, "共管")

    def test_invalid_scope_and_type(self):
        with self.assertRaises(ReportRosterError):
            validate_roster_fields("nope", "store", "店", "张三")
        with self.assertRaises(ReportRosterError):
            validate_roster_fields("qudao", "alien", "店", "张三")

    def test_invalid_key_and_person(self):
        with self.assertRaises(ReportRosterError):
            validate_roster_fields("qudao", "store", "   ", "张三")
        with self.assertRaises(ReportRosterError):
            validate_roster_fields("qudao", "store", "店", "  ")
        with self.assertRaises(ReportRosterError):
            validate_roster_fields("qudao", "store", "店", "张 三")

    def test_alias_rules(self):
        with self.assertRaises(ReportRosterError):
            validate_roster_fields("qudao", "store", "店", "张三",
                                   aliases=("张三",))  # 与本人重复
        with self.assertRaises(ReportRosterError):
            validate_roster_fields("qudao", "store", "店", "张三",
                                   aliases=tuple(f"a{i}" for i in range(9)))


# -- CRUD + 审计 --------------------------------------------------------------

class CrudTests(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()

    def _audit_actions(self):
        return [
            (r["actor"], r["action"], r["person_name"])
            for r in report_roster.fetch_roster_audit(self.db)
        ]

    def test_upsert_insert_and_reenable(self):
        entry = _entry()
        report_roster.upsert_roster_entry(self.db, entry, actor="admin")
        rows = report_roster.fetch_roster(self.db)
        self.assertEqual(1, len(rows))
        self.assertEqual(1, rows[0]["enabled"])

        # 停用后再 upsert 同自然键 → 重新启用（不产生第二行）
        report_roster.set_roster_enabled(self.db, rows[0]["id"], False,
                                         actor="admin")
        report_roster.upsert_roster_entry(self.db, entry, actor="admin")
        rows = report_roster.fetch_roster(self.db)
        self.assertEqual(1, len(rows))
        self.assertEqual(1, rows[0]["enabled"])

        actions = self._audit_actions()
        self.assertEqual(
            [("admin", "add", "饶佳君"), ("admin", "disable", "饶佳君"),
             ("admin", "add", "饶佳君")],
            actions,
        )

    def test_toggle_and_delete_hit_miss(self):
        report_roster.upsert_roster_entry(self.db, _entry(), actor="admin")
        row = report_roster.fetch_roster(self.db)[0]

        self.assertTrue(
            report_roster.set_roster_enabled(self.db, row["id"], False,
                                             actor="admin"))
        self.assertFalse(
            report_roster.set_roster_enabled(self.db, 999, False, actor="admin"))
        self.assertTrue(
            report_roster.delete_roster_entry(self.db, row["id"], actor="admin"))
        self.assertFalse(
            report_roster.delete_roster_entry(self.db, row["id"], actor="admin"))
        self.assertEqual((), report_roster.fetch_roster(self.db))
        self.assertIn(("admin", "delete", "饶佳君"), self._audit_actions())

    def test_fetch_filter_by_scope(self):
        report_roster.upsert_roster_entry(self.db, _entry(), actor="admin")
        report_roster.upsert_roster_entry(
            self.db, _entry(scope="hangzhou", entity_type="person",
                            entity_key="杭州", person_name="王城"),
            actor="admin",
        )
        self.assertEqual(2, len(report_roster.fetch_roster(self.db)))
        self.assertEqual(
            1, len(report_roster.fetch_roster(self.db, scope="hangzhou"))
        )
        with self.assertRaises(ReportRosterError):
            report_roster.fetch_roster(self.db, scope="nope")


# -- 消费面 -------------------------------------------------------------------

class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.db = _make_db()

    def test_store_owner_map_enabled_only(self):
        report_roster.upsert_roster_entry(self.db, _entry(), actor="admin")
        report_roster.upsert_roster_entry(
            self.db, _entry(person_name="共管人"), actor="admin")
        disabled = _entry(entity_key="天猫2店", person_name="停用人")
        report_roster.upsert_roster_entry(self.db, disabled, actor="admin")
        row = [r for r in report_roster.fetch_roster(self.db)
               if r["person_name"] == "停用人"][0]
        report_roster.set_roster_enabled(self.db, row["id"], False,
                                         actor="admin")

        owner_map = report_roster.fetch_store_owner_map(self.db, "qudao")
        self.assertEqual({"饶佳君", "共管人"}, owner_map["京东1店"])
        self.assertNotIn("天猫2店", owner_map)  # 停用不进名册源

    def test_person_allowed_fail_open_then_enforce(self):
        # 无记录 → fail-open 放行任何人
        self.assertTrue(
            report_roster.person_allowed(self.db, "hangzhou", "任何人"))

        report_roster.upsert_roster_entry(
            self.db,
            _entry(scope="hangzhou", entity_type="person", entity_key="杭州",
                   person_name="王城"),
            actor="admin",
        )
        self.assertTrue(
            report_roster.person_allowed(self.db, "hangzhou", "王城"))
        self.assertFalse(
            report_roster.person_allowed(self.db, "hangzhou", "张三"))
        # aliases 映射后的表内用名在册也放行
        self.assertTrue(
            report_roster.person_allowed(self.db, "hangzhou", "小王",
                                         aliases={"小王": "王城"}))
        # 别的 scope 仍 fail-open
        self.assertTrue(
            report_roster.person_allowed(self.db, "shaoxing", "张三"))


# -- 名册对齐（sync_from_targets） ---------------------------------------------

class SyncFromTargetsTests(unittest.TestCase):
    def test_sync_adds_missing_and_disables_stale(self):
        db = _make_db(with_targets=True)
        cur = db.cursor()
        # fact 目标：京东1店=[饶佳君,共管人]、天猫2店=[夏惠敏]
        cur.execute(
            "INSERT INTO fact_channel_store_target (store_name, owners_json) "
            "VALUES (?, ?), (?, ?)",
            ("京东1店", '["饶佳君", "共管人"]', "天猫2店", '["夏惠敏"]'),
        )
        # 名册现状：京东1店/饶佳君(启用)、京东1店/旧人(启用)、天猫2店/夏惠敏(停用)
        db.commit()
        report_roster.upsert_roster_entry(db, _entry(), actor="admin")
        report_roster.upsert_roster_entry(
            db, _entry(person_name="旧人"), actor="admin")
        report_roster.upsert_roster_entry(
            db, _entry(entity_key="天猫2店", person_name="夏惠敏"), actor="admin")
        stale = [r for r in report_roster.fetch_roster(db)
                 if r["person_name"] == "夏惠敏"][0]
        report_roster.set_roster_enabled(db, stale["id"], False, actor="admin")

        added, disabled = report_roster.sync_from_targets(db, actor="admin")

        self.assertEqual((2, 1), (added, disabled))  # 增共管人+重启夏惠敏；停旧人
        owner_map = report_roster.fetch_store_owner_map(db, "qudao")
        self.assertEqual({"饶佳君", "共管人"}, owner_map["京东1店"])
        self.assertEqual({"夏惠敏"}, owner_map["天猫2店"])

        actions = [r["action"] for r in report_roster.fetch_roster_audit(db)]
        self.assertEqual(2, actions.count("add") - 3)  # 初始 3 add 之外又 +2
        self.assertIn("disable", actions)

        # 幂等：再跑全零
        self.assertEqual((0, 0), report_roster.sync_from_targets(db, actor="admin"))


# -- v2：编号冻结（channel / store_no） -----------------------------------------

class StoreNumberTests(unittest.TestCase):
    def _db_with_fact(self):
        db = _make_db(with_targets=True)
        cur = db.cursor()
        cur.execute(
            "INSERT INTO fact_channel_store_target "
            "(store_name, owners_json, channel, monthly_target) VALUES "
            "('JD购喝', '[]', '京东', 1500000),"
            "('JD金沙', '[]', '京东', 200000),"
            "('TM旗舰', '[]', '天猫', 4700000)",
        )
        db.commit()
        return db

    def test_upsert_derives_channel_and_assigns_store_no(self):
        db = self._db_with_fact()
        report_roster.upsert_roster_entry(db, _entry(entity_key="JD购喝"),
                                        actor="admin")
        report_roster.upsert_roster_entry(
            db, _entry(entity_key="JD购喝", person_name="共管人"), actor="admin")
        report_roster.upsert_roster_entry(db, _entry(entity_key="JD金沙"),
                                        actor="admin")
        report_roster.upsert_roster_entry(db, _entry(entity_key="TM旗舰"),
                                        actor="admin")

        rows = {r["entity_key"]: r for r in report_roster.fetch_roster(db)}
        self.assertEqual("京东", rows["JD购喝"]["channel"])
        self.assertEqual(1, rows["JD购喝"]["store_no"])
        self.assertEqual(1, rows["JD购喝"]["store_no"])  # 同店同号
        self.assertEqual(2, rows["JD金沙"]["store_no"])  # 渠道内 max+1
        self.assertEqual("天猫", rows["TM旗舰"]["channel"])
        self.assertEqual(1, rows["TM旗舰"]["store_no"])  # 渠道独立计数
        # 共管链接与首链接同店同号
        nos = {r["store_no"] for r in report_roster.fetch_roster(db)
               if r["entity_key"] == "JD购喝"}
        self.assertEqual({1}, nos)

    def test_upsert_new_store_without_channel_is_unnumbered(self):
        db = _make_db()  # 无 fact 表 → 反查不到：允许未归属（不派号）
        report_roster.upsert_roster_entry(
            db, _entry(entity_key="全新店"), actor="admin")
        row = report_roster.fetch_roster(db)[0]
        self.assertIsNone(row["channel"])
        self.assertIsNone(row["store_no"])
        # 显式给 channel 则正常派号
        entry = validate_roster_fields("qudao", "store", "全新店B", "张三",
                                       channel="京东")
        report_roster.upsert_roster_entry(db, entry, actor="admin")
        row = [r for r in report_roster.fetch_roster(db)
               if r["entity_key"] == "全新店B"][0]
        self.assertEqual("京东", row["channel"])
        self.assertEqual(1, row["store_no"])

    def test_backfill_freezes_current_numbering(self):
        db = self._db_with_fact()
        # 模拟 v1 存量（无 channel/store_no）
        cur = db.cursor()
        for store, person in (("JD购喝", "娄灿斌"), ("JD金沙", "王蕊"),
                              ("TM旗舰", "钟甜")):
            cur.execute(
                "INSERT INTO dim_report_roster "
                "(scope, entity_type, entity_key, person_name, enabled) "
                "VALUES ('qudao', 'store', ?, ?, 1)",
                (store, person),
            )
        db.commit()

        patched, assigned = report_roster.backfill_store_numbers(db, actor="seed")
        self.assertEqual((3, 3), (patched, assigned))
        numbers = report_roster.fetch_store_numbers(db)
        self.assertEqual(("京东", 1), numbers["JD购喝"])   # 目标高者 1 号
        self.assertEqual(("京东", 2), numbers["JD金沙"])
        self.assertEqual(("天猫", 1), numbers["TM旗舰"])
        # 幂等：再跑全零
        self.assertEqual((0, 0),
                         report_roster.backfill_store_numbers(db, actor="seed"))

    def test_sync_store_roster_with_explicit_map(self):
        db = self._db_with_fact()
        added, disabled = report_roster.sync_store_roster(
            db, {"JD购喝": {"娄灿斌"}, "TM旗舰": {"钟甜"}}, actor="admin")
        self.assertEqual((2, 0), (added, disabled))
        # 再同步：JD购喝 换负责人 → 停娄灿斌、增新人
        added, disabled = report_roster.sync_store_roster(
            db, {"JD购喝": {"新人"}, "TM旗舰": {"钟甜"}}, actor="admin")
        self.assertEqual((1, 1), (added, disabled))
        owner_map = report_roster.fetch_store_owner_map(db, "qudao")
        self.assertEqual({"新人"}, owner_map["JD购喝"])
        self.assertEqual({"钟甜"}, owner_map["TM旗舰"])


# -- 种子导入 ------------------------------------------------------------------

class SeedTests(unittest.TestCase):
    def test_seed_from_channel_targets_idempotent(self):
        db = _make_db(with_targets=True)
        cur = db.cursor()
        cur.execute(
            "INSERT INTO fact_channel_store_target (store_name, owners_json) "
            "VALUES (?, ?), (?, ?), (?, ?)",
            ("京东1店", '["饶佳君", "共管人"]',
             "天猫2店", '[{"name": "夏惠敏", "unionId": "u1"}]',
             "空店", None),
        )
        db.commit()

        inserted, skipped = report_roster.seed_from_channel_targets(db)
        self.assertEqual((3, 0), (inserted, skipped))
        # 幂等：重跑全跳过
        inserted2, skipped2 = report_roster.seed_from_channel_targets(db)
        self.assertEqual((0, 3), (inserted2, skipped2))

        owner_map = report_roster.fetch_store_owner_map(db, "qudao")
        self.assertEqual({"饶佳君", "共管人"}, owner_map["京东1店"])
        self.assertEqual({"夏惠敏"}, owner_map["天猫2店"])
        self.assertNotIn("空店", owner_map)


if __name__ == "__main__":
    unittest.main()
