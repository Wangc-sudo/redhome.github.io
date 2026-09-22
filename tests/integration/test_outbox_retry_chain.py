"""端到端（内存 fake）：outbox 投递失败重试链路。

robot 侧 ``OutboxRepository.enqueue`` 入队 -> gateway 侧
``OutboxDeliveryWorker`` 轮询 ``fetch_pending`` -> fake 钉钉投递器 ->
``mark_delivered`` / ``register_failure`` 回写。DB 用一个内存表 fake 承载
``OutboxRepository`` 的全部 SQL 语义（INSERT IGNORE 幂等、状态守卫 UPDATE、
attempts 达上限转 failed），不依赖 Docker/MySQL。

守门断言：

* 失败重试：投递异常 -> attempts+1 且保持 pending -> 下轮再取 -> 达上限转
  failed -> 不再被取出；
* 瞬时故障可恢复：失败若干次后成功 -> delivered，不再重发；
* 单行失败不中断批次（同批其余行照常投递）；
* 非泄露：``last_error`` 只有错误码，绝不包含异常原文里的 URL/凭据；
  ``DeliverReport`` 只携带 dedupe_key。
"""

import json
import unittest
from datetime import date, datetime, timedelta, timezone

from common.gateway.delivery import OutboxDeliveryWorker
from common.public_data.outbox_repository import (
    DEFAULT_MAX_ATTEMPTS,
    OutboxRepository,
)


_NOW = datetime(2026, 9, 16, 9, tzinfo=timezone.utc)
_DAY = date(2026, 9, 16)

#: 只会在异常原文里出现的泄露标记；任何落库字段都不得包含它。
_LEAK_MARKER = "https://oapi.dingtalk.com/robot/send?access_token=SECRET-TOKEN"


class _InMemoryOutboxTable:
    """内存版 ``robot_outbox``：唯一键 dedupe_key，行内带状态机字段。"""

    def __init__(self):
        self.rows = {}

    def insert_ignore(self, params):
        (dedupe_key, region, kind, business_date, title,
         body_md, at_user_ids, created_at) = params
        if dedupe_key in self.rows:
            return 0
        self.rows[dedupe_key] = {
            "dedupe_key": dedupe_key,
            "region": region,
            "kind": kind,
            "business_date": business_date,
            "title": title,
            "body_md": body_md,
            "at_user_ids": at_user_ids,
            "status": "pending",
            "attempts": 0,
            "last_error": None,
            "delivered_at": None,
            "created_at": created_at,
        }
        return 1

    def fetch_pending(self, max_attempts, limit):
        candidates = [
            row for row in self.rows.values()
            if row["status"] == "pending" and row["attempts"] < max_attempts
        ]
        candidates.sort(key=lambda row: row["created_at"])
        selected = []
        for row in candidates[:limit]:
            copied = dict(row)
            copied.pop("status", None)
            copied.pop("last_error", None)
            copied.pop("delivered_at", None)
            copied.pop("created_at", None)
            selected.append(copied)
        return selected

    def mark_delivered(self, delivered_at, dedupe_key):
        row = self.rows.get(dedupe_key)
        if row is None or row["status"] != "pending":
            return 0
        row["status"] = "delivered"
        row["delivered_at"] = delivered_at
        return 1

    def register_failure(self, error_code, max_attempts, dedupe_key):
        row = self.rows.get(dedupe_key)
        if row is None or row["status"] != "pending":
            return 0
        row["attempts"] += 1
        row["last_error"] = error_code
        if row["attempts"] >= max_attempts:
            row["status"] = "failed"
        return 1


class _OutboxCursor:
    """按 SQL 文本分发到内存表（仅覆盖 OutboxRepository 的 4 类语句）。"""

    def __init__(self, table):
        self._table = table
        self.rowcount = 0
        self._rows = []
        self._one = None

    def execute(self, sql, params=None):
        self.rowcount = 0
        self._rows = []
        self._one = None
        if "INSERT IGNORE" in sql:
            self.rowcount = self._table.insert_ignore(params)
        elif "SET `status` = 'delivered'" in sql:
            self.rowcount = self._table.mark_delivered(*params)
        elif "`attempts` = `attempts` + 1" in sql:
            self.rowcount = self._table.register_failure(*params)
        elif "SELECT `status`" in sql:
            row = self._table.rows.get(params[0])
            self._one = None if row is None else {"status": row["status"]}
        elif "`status` = 'pending'" in sql:
            self._rows = self._table.fetch_pending(params[0], params[1])
        else:  # pragma: no cover - 守门：仓储新增语句必须显式建模
            raise AssertionError(f"unmodelled outbox SQL: {sql}")

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._one

    def close(self):
        pass


class _OutboxConnection:
    def __init__(self, table):
        self._cursor = _OutboxCursor(table)

    def cursor(self):
        return self._cursor


class _FakeDeliverer:
    """fake 钉钉投递器：可编程失败次数，异常原文携带泄露标记。"""

    def __init__(self, failures_before_success=0, always_fail=False):
        self._remaining_failures = failures_before_success
        self._always_fail = always_fail
        self.group_calls = []
        self.ding_calls = []

    def _maybe_fail(self):
        if self._always_fail or self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise RuntimeError(f"send failed: POST {_LEAK_MARKER} -> 500")

    def send_group(self, *, region, title, body_md, at_user_ids):
        self.group_calls.append(
            {"region": region, "title": title,
             "body_md": body_md, "at_user_ids": list(at_user_ids)}
        )
        self._maybe_fail()

    def send_ding(self, *, region, user_ids, content):
        self.ding_calls.append(
            {"region": region, "user_ids": list(user_ids), "content": content}
        )
        self._maybe_fail()


def _build(*, deliverer):
    table = _InMemoryOutboxTable()
    repository = OutboxRepository(_OutboxConnection(table))
    clock = {"now": _NOW}
    worker = OutboxDeliveryWorker(
        outbox=repository,
        deliverer=deliverer,
        now=lambda: clock["now"],
    )
    return table, repository, worker, clock


def _enqueue(repository, *, kind="remind", suffix=None, region="hangzhou",
             created_at=_NOW):
    return repository.enqueue(
        region=region,
        kind=kind,
        business_date=_DAY,
        title="销售日报提醒",
        body_md="请填写今日销售日报",
        at_user_ids=["u1"],
        created_at=created_at,
        dedupe_suffix=suffix,
    )


class OutboxHappyPathTests(unittest.TestCase):

    def test_enqueue_deliver_and_idempotent_reenqueue(self):
        table, repository, worker, _ = _build(deliverer=_FakeDeliverer())

        self.assertTrue(_enqueue(repository))
        # 幂等：同日同 kind 重跑不重复入队、不覆盖原行。
        self.assertFalse(_enqueue(repository))
        self.assertEqual(1, len(table.rows))

        report = worker.deliver_once()

        key = "hangzhou:remind:2026-09-16"
        self.assertEqual((key,), report.delivered)
        self.assertEqual((), report.failed)
        row = table.rows[key]
        self.assertEqual("delivered", row["status"])
        self.assertEqual(_NOW, row["delivered_at"])
        self.assertEqual(1, len(worker._deliverer.group_calls))
        # 终态行不再被取出。
        self.assertEqual([], repository.fetch_pending())

    def test_report_and_payload_carry_no_leak_marker(self):
        table, repository, worker, _ = _build(deliverer=_FakeDeliverer())
        _enqueue(repository)

        report = worker.deliver_once()

        blob = json.dumps(
            {"delivered": report.delivered, "failed": report.failed,
             "rows": list(table.rows.values())},
            ensure_ascii=False, default=str,
        )
        self.assertNotIn(_LEAK_MARKER, blob)


class OutboxRetryChainTests(unittest.TestCase):

    def test_failure_keeps_pending_and_retries_until_delivered(self):
        deliverer = _FakeDeliverer(failures_before_success=2)
        table, repository, worker, clock = _build(deliverer=deliverer)
        _enqueue(repository)
        key = "hangzhou:remind:2026-09-16"

        first = worker.deliver_once()
        self.assertEqual((), first.delivered)
        self.assertEqual((key,), first.failed)
        self.assertEqual("pending", table.rows[key]["status"])
        self.assertEqual(1, table.rows[key]["attempts"])

        # 下一轮：行仍在待投递队列（重试语义）。
        self.assertEqual([key], [r["dedupe_key"] for r in repository.fetch_pending()])
        clock["now"] = _NOW + timedelta(minutes=1)
        second = worker.deliver_once()
        self.assertEqual((key,), second.failed)
        self.assertEqual(2, table.rows[key]["attempts"])

        third = worker.deliver_once()
        self.assertEqual((key,), third.delivered)
        self.assertEqual("delivered", table.rows[key]["status"])
        self.assertEqual(clock["now"], table.rows[key]["delivered_at"])
        self.assertEqual([], repository.fetch_pending())

    def test_repeated_failure_escalates_to_failed_at_the_cap(self):
        table, repository, worker, _ = _build(
            deliverer=_FakeDeliverer(always_fail=True)
        )
        _enqueue(repository)
        key = "hangzhou:remind:2026-09-16"

        for round_index in range(DEFAULT_MAX_ATTEMPTS):
            report = worker.deliver_once()
            self.assertEqual((key,), report.failed, round_index)

        row = table.rows[key]
        self.assertEqual(DEFAULT_MAX_ATTEMPTS, row["attempts"])
        self.assertEqual("failed", row["status"])
        # 终态 failed：不再被轮询取出，也不会再触发外呼。
        calls_before = len(worker._deliverer.group_calls)
        self.assertEqual([], repository.fetch_pending())
        self.assertEqual(0, worker.deliver_once().failed_count)
        self.assertEqual(calls_before, len(worker._deliverer.group_calls))

    def test_one_failure_does_not_break_the_batch(self):
        deliverer = _FakeDeliverer(failures_before_success=1)
        table, repository, worker, _ = _build(deliverer=deliverer)
        _enqueue(repository, suffix="a", created_at=_NOW)
        _enqueue(repository, suffix="b",
                 created_at=_NOW + timedelta(seconds=1))

        report = worker.deliver_once()

        key_a = "hangzhou:remind:2026-09-16:a"
        key_b = "hangzhou:remind:2026-09-16:b"
        self.assertEqual((key_a,), report.failed)
        self.assertEqual((key_b,), report.delivered)
        self.assertEqual("pending", table.rows[key_a]["status"])
        self.assertEqual("delivered", table.rows[key_b]["status"])
        self.assertEqual(2, len(deliverer.group_calls))

    def test_ding_kind_uses_ding_channel_and_its_error_code(self):
        deliverer = _FakeDeliverer(always_fail=True)
        table, repository, worker, _ = _build(deliverer=deliverer)
        _enqueue(repository, kind="ding")
        key = "hangzhou:ding:2026-09-16"

        worker.deliver_once()

        self.assertEqual(1, len(deliverer.ding_calls))
        self.assertEqual("ding_send_failed", table.rows[key]["last_error"])


class OutboxNonLeakTests(unittest.TestCase):

    def test_last_error_stores_only_the_error_code(self):
        table, repository, worker, _ = _build(
            deliverer=_FakeDeliverer(always_fail=True)
        )
        _enqueue(repository)
        key = "hangzhou:remind:2026-09-16"

        worker.deliver_once()

        self.assertEqual("group_send_failed", table.rows[key]["last_error"])
        stored = json.dumps(table.rows[key], ensure_ascii=False, default=str)
        self.assertNotIn(_LEAK_MARKER, stored)
        self.assertNotIn("Traceback", stored)
        self.assertNotIn("500", table.rows[key]["last_error"])


if __name__ == "__main__":
    unittest.main()
