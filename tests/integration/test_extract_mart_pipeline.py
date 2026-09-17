"""端到端（内存 fake）：raw -> extract -> mart 管线与只读边界。

不依赖 Docker/MySQL：raw / mart 两侧用录制型假连接，控制面
（``sync_runs`` / ``sync_dataset_summary``）用录制型假仓储，走真实的
``MartExtractService`` 编排。守门断言：

* raw 连接上只出现 SELECT，且只读**已注册**的 raw 源表（列白名单投影）；
* mart 连接上的写语句只触碰已注册的 mart 目标表（``fact_*`` / ``dim_*``），
  绝不出现 raw 源表名；
* run 状态机按序迁移：start -> raw_committed -> （每数据集摘要）-> completed；
* 摘要失败 -> ``projection_pending``；投影前失败 -> ``failed`` + rollback；
* 未注册的源表 / mart 表一律拒绝（只读校验的入口闸门）。
"""

import hashlib
import re
import unittest
from datetime import date, datetime, timezone

from common.public_data.extract_mart import (
    CALENDAR_DATASET,
    MartExtractError,
    MartExtractRepository,
    MartExtractService,
)
from common.public_data.mart_extract_schema import (
    DIM_CALENDAR,
    DIM_ROBOT_MEMBER,
    EXTRACT_DATASETS,
    dataset_by_name,
)


_NOW = datetime(2026, 9, 16, 8, tzinfo=timezone.utc)
_RUN_ID = "00000000-0000-0000-0000-0000000000e2"

#: mart 侧允许被写语句触碰的表（注册目标表 + 提取层自有的两张维度表）。
_MART_WRITABLE_TABLES = frozenset(
    {d.target_table for d in EXTRACT_DATASETS} | {DIM_CALENDAR, DIM_ROBOT_MEMBER}
)

def _statement_table(sql):
    """写语句的**目标表**（仅语句首部，不碰 ON DUPLICATE KEY UPDATE 子句）。"""
    stripped = sql.lstrip()
    for verb in ("INSERT INTO", "DELETE FROM", "UPDATE"):
        if stripped.startswith(verb):
            match = re.match(rf"{re.escape(verb)}\s+`([^`]+)`", stripped)
            if match:
                return match.group(1)
    return None


class _RawCursor:
    """raw 侧游标：按 SQL 路由到预置行，只回答 SELECT。"""

    def __init__(self, *, dataset_rows=(), org_rows=()):
        self._dataset_rows = list(dataset_rows)
        self._org_rows = list(org_rows)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        last = self.executed[-1][0]
        if "dingtalk_org_member" in last:
            return list(self._org_rows)
        return list(self._dataset_rows)

    def close(self):
        pass


class _MartCursor:
    """mart 侧游标：录制全部语句；GET_LOCK 恒成功；水位/摘要探测可播种。"""

    def __init__(self, *, watermark_rows=(), digest_rows=(), has_skipped=False):
        self.executed = []
        self.executemany_calls = []
        self._watermark_rows = list(watermark_rows)
        self._digest_rows = list(digest_rows)
        self._has_skipped = has_skipped

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def executemany(self, sql, sequence):
        self.executemany_calls.append((sql, list(sequence)))

    def fetchone(self):
        return {"acquired": 1}

    def fetchall(self):
        last = self.executed[-1][0]
        if "information_schema" in last:
            return [{"n": 1 if self._has_skipped else 0}]
        if "sync_runs" in last:
            return list(self._watermark_rows)
        if "sync_dataset_summary" in last:
            return list(self._digest_rows)
        return []

    def close(self):
        pass


class _FakeConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _RecordingMartRepository:
    """录制控制面调用顺序的假 ``mart_repository``。"""

    def __init__(self, *, fail_on_summary=False):
        self.calls = []
        self._fail_on_summary = fail_on_summary

    def start_run(self, *, sync_run_id, manifest_sha256, started_at):
        self.calls.append(("start_run", sync_run_id))

    def mark_raw_committed(self, sync_run_id):
        self.calls.append(("mark_raw_committed", sync_run_id))

    def mark_completed(self, *, sync_run_id, finished_at):
        self.calls.append(("mark_completed", sync_run_id))

    def mark_failed(self, *, sync_run_id, failure_code, finished_at):
        self.calls.append(("mark_failed", sync_run_id, failure_code))

    def mark_projection_pending(self, *, sync_run_id, failure_code=None, finished_at=None):
        self.calls.append(("mark_projection_pending", sync_run_id, failure_code))

    def save_dataset_summary(self, *, sync_run_id, source_name, dataset_name,
                             records_read, raw_records_written,
                             record_id_digest, completed_at):
        if self._fail_on_summary:
            raise RuntimeError("summary store unreachable")
        self.calls.append(
            ("save_dataset_summary", dataset_name, records_read,
             raw_records_written)
        )


def _daily_report_rows():
    return [
        {
            "source_record_id": "rec-001",
            "region": "hangzhou",
            "responsible_person": "张三",
            "department": "销售一部",
            "business_date": date(2026, 9, 15),
            "sales_amount": 1200,
            "daily_target": 1000,
            "monthly_target": 30000,
            "note": None,
        },
        {
            "source_record_id": "rec-002",
            "region": "shaoxing",
            "responsible_person": "李四",
            "department": "销售二部",
            "business_date": date(2026, 9, 15),
            "sales_amount": 800,
            "daily_target": 1000,
            "monthly_target": 30000,
            "note": "含退货",
        },
    ]


def _build(*, dataset_rows=(), org_rows=(), fail_on_summary=False,
           calendar_months=(), watermark_rows=(), digest_rows=()):
    raw_cursor = _RawCursor(dataset_rows=dataset_rows, org_rows=org_rows)
    mart_cursor = _MartCursor(
        watermark_rows=watermark_rows, digest_rows=digest_rows,
    )
    raw_conn = _FakeConnection(raw_cursor)
    mart_conn = _FakeConnection(mart_cursor)
    mart_repo = _RecordingMartRepository(fail_on_summary=fail_on_summary)
    service = MartExtractService(
        repository=MartExtractRepository(raw_conn, mart_conn),
        mart_repository=mart_repo,
        mart_connection=mart_conn,
        now=lambda: _NOW,
        new_run_id=lambda: _RUN_ID,
        datasets=(dataset_by_name("daily_report_offline"),),
        calendar_months=calendar_months,
    )
    return service, raw_cursor, mart_cursor, mart_conn, mart_repo


def _mart_write_tables(mart_cursor):
    """mart 连接上所有写语句（INSERT/DELETE/UPDATE）引用的表名集合。"""
    tables = set()
    statements = [sql for sql, _ in mart_cursor.executed]
    statements += [sql for sql, _ in mart_cursor.executemany_calls]
    for sql in statements:
        if sql.lstrip().split(" ", 1)[0] in ("INSERT", "DELETE", "UPDATE"):
            table = _statement_table(sql)
            if table is not None:
                tables.add(table)
    return tables


class ExtractPipelineTests(unittest.TestCase):

    def test_fact_dataset_flows_raw_to_mart_with_ordered_run_states(self):
        service, raw_cursor, mart_cursor, mart_conn, mart_repo = _build(
            dataset_rows=_daily_report_rows(),
        )

        result = service.extract()

        self.assertEqual(_RUN_ID, result.run_id)
        datasets = {entry["dataset"]: entry for entry in result.datasets}
        self.assertEqual(2, datasets["daily_report_offline"]["records_read"])
        self.assertEqual(2, datasets["daily_report_offline"]["raw_records_written"])

        # 状态机顺序：start -> raw_committed -> 摘要 -> completed。
        names = [call[0] for call in mart_repo.calls]
        self.assertEqual("start_run", names[0])
        self.assertEqual("mark_completed", names[-1])
        self.assertLess(names.index("mark_raw_committed"),
                        names.index("save_dataset_summary"))
        self.assertLess(names.index("save_dataset_summary"),
                        names.index("mark_completed"))
        self.assertGreaterEqual(mart_conn.commits, 2)
        self.assertEqual(0, mart_conn.rollbacks)

        # mart 写入幂等：ON DUPLICATE KEY UPDATE。逐行 execute 与批量
        # executemany（2026-09-17 排查报告 §2.1 P1）都接受，写法与
        # ``_mart_write_tables`` 一致（两条通道都扫）。
        statements = [sql for sql, _ in mart_cursor.executed]
        statements += [sql for sql, _ in mart_cursor.executemany_calls]
        upsert_sql = next(
            sql for sql in statements if "ON DUPLICATE KEY UPDATE" in sql
        )
        self.assertIn("`fact_daily_report_offline`", upsert_sql)

    def test_raw_side_is_select_only_and_column_whitelisted(self):
        service, raw_cursor, _, _, _ = _build(dataset_rows=_daily_report_rows())

        service.extract()

        self.assertTrue(raw_cursor.executed)
        dataset = dataset_by_name("daily_report_offline")
        for sql, _ in raw_cursor.executed:
            self.assertTrue(sql.lstrip().startswith("SELECT"), sql)
        projection_sql = next(
            sql for sql, _ in raw_cursor.executed
            if f"FROM `{dataset.source_table}`" in sql
        )
        # 列白名单：作废列与技术列绝不出现在投影 SQL 里。
        self.assertNotIn("achievement_rate", projection_sql)
        self.assertNotIn("parent_record_refs", projection_sql)
        for column in re.findall(r"`([^`]+)`\s+AS", projection_sql):
            self.assertIn(column, {"dingtalk_record_id", *dataset.source_columns})

    def test_mart_writes_touch_only_registered_mart_tables(self):
        service, _, mart_cursor, _, _ = _build(
            dataset_rows=_daily_report_rows(),
            calendar_months=((2026, 9, (1, 2), "seed"),),
        )

        service.extract()

        written = _mart_write_tables(mart_cursor)
        self.assertTrue(written)
        self.assertTrue(written <= _MART_WRITABLE_TABLES, written)
        # raw 源表名（无 fact_ 前缀）绝不出现在 mart 写语句里。
        raw_sources = {d.source_table for d in EXTRACT_DATASETS} - _MART_WRITABLE_TABLES
        self.assertTrue(written.isdisjoint(raw_sources), written)

    def test_calendar_and_org_dimensions_are_replaced_in_mart(self):
        service, _, mart_cursor, _, mart_repo = _build(
            dataset_rows=_daily_report_rows(),
            org_rows=[
                {"user_id": "u1", "name": "王五", "region": "hangzhou",
                 "dept_id": "d1", "dept_name": "销售一部"},
            ],
            calendar_months=((2026, 9, (1,), "seed"),),
        )

        result = service.extract()

        datasets = {entry["dataset"] for entry in result.datasets}
        self.assertIn(CALENDAR_DATASET, datasets)
        self.assertIn(DIM_ROBOT_MEMBER, datasets)
        replaced = {
            table
            for sql, _ in mart_cursor.executemany_calls
            for table in (_statement_table(sql),)
            if table is not None
        }
        self.assertIn(DIM_CALENDAR, replaced)
        self.assertIn(DIM_ROBOT_MEMBER, replaced)

    def test_incremental_window_filters_raw_read_by_watermark(self):
        """增量默认路径：有历史水位 -> raw 读取带 synced_at 窗口过滤。"""
        watermark = datetime(2026, 9, 15, 8, tzinfo=timezone.utc)
        service, raw_cursor, _, _, _ = _build(
            dataset_rows=_daily_report_rows(),
            watermark_rows=[{"started_at": watermark}],
        )

        service.extract()

        sql, params = next(
            (sql, params) for sql, params in raw_cursor.executed
            if "FROM `daily_report_offline`" in sql
        )
        self.assertIn("WHERE `synced_at` >= %s", sql)
        self.assertEqual((watermark,), params)

    def test_unchanged_digest_skips_mart_writes_but_keeps_summary(self):
        """内容 digest 命中历史摘要 -> 跳过 mart 写入，摘要仍落库。"""
        rows = _daily_report_rows()
        digest = hashlib.sha256(
            "\n".join(sorted(row["source_record_id"] for row in rows))
            .encode("utf-8")
        ).hexdigest()
        service, _, mart_cursor, _, mart_repo = _build(
            dataset_rows=rows,
            digest_rows=[{"record_id_digest": digest}],
        )

        result = service.extract()

        entry = next(
            item for item in result.datasets
            if item["dataset"] == "daily_report_offline"
        )
        self.assertTrue(entry["skipped"])
        self.assertEqual(0, entry["raw_records_written"])
        fact_writes = [
            sql for sql, _ in mart_cursor.executed
            if sql.lstrip().startswith("INSERT")
            and "`fact_daily_report_offline`" in sql
        ]
        self.assertEqual([], fact_writes)
        # skipped 列未迁移 -> 降级为普通摘要（raw_records_written=0）。
        summary = next(
            call for call in mart_repo.calls
            if call[0] == "save_dataset_summary"
        )
        self.assertEqual(0, summary[3])

    def test_summary_failure_marks_projection_pending(self):
        service, _, mart_cursor, mart_conn, mart_repo = _build(
            dataset_rows=_daily_report_rows(),
            fail_on_summary=True,
        )

        with self.assertRaisesRegex(RuntimeError, "summary store unreachable"):
            service.extract()

        names = [call[0] for call in mart_repo.calls]
        self.assertIn("mark_projection_pending", names)
        self.assertNotIn("mark_completed", names)
        self.assertNotIn("mark_failed", names)

    def test_projection_failure_marks_failed_and_rolls_back(self):
        service, raw_cursor, _, mart_conn, mart_repo = _build()
        # 让 raw 读取在投影前失败（源表消失）。
        raw_cursor._dataset_rows = None

        with self.assertRaises(TypeError):
            service.extract()

        names = [call[0] for call in mart_repo.calls]
        self.assertIn("mark_failed", names)
        self.assertNotIn("mark_completed", names)
        failure = next(call for call in mart_repo.calls if call[0] == "mark_failed")
        self.assertEqual("extract_failed", failure[2])
        self.assertGreaterEqual(mart_conn.rollbacks, 1)


class ExtractReadonlyGateTests(unittest.TestCase):
    """只读校验的入口闸门：未注册的表名一律拒绝。"""

    def setUp(self):
        self.raw_conn = _FakeConnection(_RawCursor())
        self.mart_conn = _FakeConnection(_MartCursor())
        self.repository = MartExtractRepository(self.raw_conn, self.mart_conn)

    def test_unregistered_raw_table_is_rejected(self):
        for table in ("raw_dingtalk", "credentials", "robot_outbox"):
            with self.subTest(table=table):
                with self.assertRaises(MartExtractError):
                    self.repository.read_table(table)
        self.assertEqual([], self.raw_conn.cursor_instance.executed)

    def test_unregistered_mart_table_is_rejected(self):
        with self.assertRaises(MartExtractError):
            self.repository.read_mart_table("fact_daily_report_offline")
        with self.assertRaises(MartExtractError):
            self.repository.replace_table(
                "robot_outbox", ("dedupe_key",), [{"dedupe_key": "x"}]
            )

    def test_wdt_read_requires_registered_table_and_configured_connection(self):
        with self.assertRaises(MartExtractError):
            self.repository.read_wdt_table("wdt_records")
        with self.assertRaises(MartExtractError):
            self.repository.read_wdt_trades("trades")


if __name__ == "__main__":
    unittest.main()
