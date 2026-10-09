"""channel_robot_inbox 测试：归并纯逻辑 + inbox 读写 + extract 钩子。"""

import unittest
from datetime import date, datetime

from common.public_data.channel_robot_inbox import (
    ROBOT_RUN_ID,
    business_key,
    fetch_latest_inbox,
    fetch_raw_business_keys,
    insert_inbox,
    merge_channel_rows,
)

_D1 = date(2026, 9, 28)
_NOW = datetime(2026, 9, 29, 9, 40)


def _inbox(inbox_id, channel, store, amount, day=_D1, created=None):
    return {
        "id": inbox_id, "channel": channel, "store_name": store,
        "business_date": day, "sales_amount": amount,
        "created_at": created or _NOW,
    }


def _raw_row(record_id, channel, store, amount=None, day=_D1):
    return {
        "source_record_id": record_id, "channel": channel,
        "store_name": store, "business_date": day, "sales_amount": amount,
        "promotion_cost": None, "roi": None, "responsible_person": None,
    }


class MergeChannelRowsTest(unittest.TestCase):
    def test_no_inbox_passthrough(self):
        rows = [_raw_row("ai-1", "京东", "JD某店")]
        self.assertEqual(merge_channel_rows(rows, {}, {}), rows)

    def test_window_row_overwritten_in_place(self):
        # AI 预置空行（sales_amount=None）在窗口内 → robot 兜底填值
        rows = [_raw_row("ai-1", "京东", "JD某店")]
        inbox = {business_key(_D1, "京东", "JD某店"):
                 _inbox(7, "京东", "JD某店", 15867)}
        merged = merge_channel_rows(rows, inbox, {})
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source_record_id"], "ai-1")  # 复用 AI id
        self.assertEqual(merged[0]["sales_amount"], 15867)
        self.assertEqual(merged[0]["_sync_run_id"], ROBOT_RUN_ID)

    def test_window_row_with_ai_value_wins(self):
        # 2026-10-09 裁决：AI 表为真源——窗口内 AI 行有值 → robot 不覆盖
        rows = [_raw_row("ai-1", "京东", "JD某店", 500)]
        inbox = {business_key(_D1, "京东", "JD某店"):
                 _inbox(7, "京东", "JD某店", 15867)}
        merged = merge_channel_rows(rows, inbox, {})
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["sales_amount"], 500)
        self.assertNotIn("_sync_run_id", merged[0])

    def test_out_of_window_ai_row_reuses_record_id(self):
        # AI 预置 NULL 行不在 extract 窗口内：raw_key_index 提供 recordId
        inbox = {business_key(_D1, "猫超", None):
                 _inbox(8, "猫超", None, 731033)}
        merged = merge_channel_rows(
            [], inbox,
            {business_key(_D1, "猫超", None):
             {"source_record_id": "ai-ms", "sales_amount": None}})
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source_record_id"], "ai-ms")
        self.assertIsNone(merged[0]["store_name"])
        self.assertEqual(merged[0]["sales_amount"], 731033)
        self.assertEqual(merged[0]["_sync_run_id"], ROBOT_RUN_ID)

    def test_out_of_window_ai_value_wins_robot_skipped(self):
        # 窗口外 AI 行已有值 → 真源，robot 整键跳过（不生成覆盖行）
        inbox = {business_key(_D1, "京东", "JD某店"):
                 _inbox(7, "京东", "JD某店", 15867)}
        merged = merge_channel_rows(
            [], inbox,
            {business_key(_D1, "京东", "JD某店"):
             {"source_record_id": "ai-1", "sales_amount": 500}})
        self.assertEqual(merged, [])

    def test_brand_new_store_gets_robot_id(self):
        inbox = {business_key(_D1, "京东", "JD新店"):
                 _inbox(9, "京东", "JD新店", 100)}
        merged = merge_channel_rows([], inbox, {})
        self.assertEqual(merged[0]["source_record_id"], "robot:9")

    def test_raw_rows_untouched_without_key_match(self):
        rows = [_raw_row("ai-1", "京东", "JD某店", 500)]
        inbox = {business_key(_D1, "天猫", "TM某店"):
                 _inbox(7, "天猫", "TM某店", 100)}
        merged = merge_channel_rows(rows, inbox, {})
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["sales_amount"], 500)
        self.assertNotIn("_sync_run_id", merged[0])

    def test_input_rows_not_mutated(self):
        rows = [_raw_row("ai-1", "京东", "JD某店")]
        inbox = {business_key(_D1, "京东", "JD某店"):
                 _inbox(7, "京东", "JD某店", 15867)}
        merge_channel_rows(rows, inbox, {})
        self.assertIsNone(rows[0]["sales_amount"])  # 原列表不被改


class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []
        self.lastrowid = 0

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        if sql.lstrip().startswith("INSERT"):
            self._conn.inbox_rows.append(params)
            self.lastrowid = len(self._conn.inbox_rows)
            self._rows = []
        elif "channel_sales_robot_inbox" in sql:
            self._rows = list(self._conn.inbox_select)
        elif "channel_daily_sales" in sql:
            self._rows = list(self._conn.raw_rows)
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _Conn:
    def __init__(self, *, inbox_select=(), raw_rows=()):
        self.inbox_select = list(inbox_select)
        self.raw_rows = list(raw_rows)
        self.inbox_rows = []
        self.executed = []

    def cursor(self):
        return _Cursor(self)


class InboxIoTest(unittest.TestCase):
    def test_insert_returns_id_and_writes_columns(self):
        conn = _Conn()
        inbox_id = insert_inbox(
            conn, conversation_id="conv-q", sender_userid="u1",
            sender_name="饶佳君", raw_text="京东 习水村 15867",
            channel="京东", store_name="JD习水村酒类专营店",
            business_date=_D1, sales_amount=15867, status="parsed",
            reject_reason=None, now=_NOW,
        )
        self.assertEqual(inbox_id, 1)
        params = conn.inbox_rows[0]
        self.assertEqual(params[0], "conv-q")
        self.assertEqual(params[6], _D1)
        self.assertEqual(params[8], "parsed")

    def test_fetch_latest_keeps_newest_per_key(self):
        older = _inbox(1, "京东", "JD某店", 100,
                       created=datetime(2026, 9, 29, 8, 0))
        newer = _inbox(2, "京东", "JD某店", 200,
                       created=datetime(2026, 9, 29, 9, 0))
        conn = _Conn(inbox_select=[older, newer])
        latest = fetch_latest_inbox(conn)
        self.assertEqual(len(latest), 1)
        self.assertEqual(
            latest[business_key(_D1, "京东", "JD某店")]["sales_amount"], 200
        )

    def test_fetch_latest_fail_open_on_missing_table(self):
        class _BadCursor(_Cursor):
            def execute(self, sql, params=None):
                raise RuntimeError("table missing")

        class _BadConn(_Conn):
            def cursor(self):
                return _BadCursor(self)

        self.assertEqual(fetch_latest_inbox(_BadConn()), {})

    def test_fetch_raw_business_keys(self):
        conn = _Conn(raw_rows=[
            {"source_record_id": "ai-1", "channel": "京东",
             "store_name": "JD某店", "business_date": _D1,
             "sales_amount": 500},
            {"source_record_id": "ai-2", "channel": "猫超",
             "store_name": None, "business_date": _D1,
             "sales_amount": None},
        ])
        index = fetch_raw_business_keys(conn)
        self.assertEqual(
            index[business_key(_D1, "京东", "JD某店")],
            {"source_record_id": "ai-1", "sales_amount": 500},
        )
        self.assertEqual(
            index[business_key(_D1, "猫超", None)],
            {"source_record_id": "ai-2", "sales_amount": None},
        )


if __name__ == "__main__":
    unittest.main()
