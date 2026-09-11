"""Tests for the mart-backed remind/check tasks (stage 4).

The message builders must stay byte-identical with ``core.do_remind`` /
``core.do_check`` -- switching the data source must not change what the
group sees.
"""

import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock

from common.daily_robot.mart_tasks import (
    MartTaskError,
    build_check_message,
    build_ding_content,
    build_reminder_message,
    run_check,
    run_remind,
)


_DAY = date(2026, 9, 11)  # 周五
_NOW = datetime(2026, 9, 11, 18, 30, tzinfo=timezone.utc)
_URL = "https://example.com/table"


class _RouterCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        self._conn.queries.append((sql, params))
        if "dim_calendar" in sql:
            self._rows = self._conn.workday_rows
        elif "dim_robot_member" in sql:
            self._rows = self._conn.member_rows
        else:
            self._rows = self._conn.fact_rows

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _RouterConn:
    def __init__(self, *, workdays=(), members=(), filled=()):
        self.workday_rows = [{"business_date": d} for d in workdays]
        self.member_rows = list(members)
        self.fact_rows = [{"responsible_person": n} for n in filled]
        self.queries = []

    def cursor(self):
        return _RouterCursor(self)


def _members(*pairs):
    return [{"user_id": uid, "name": name} for uid, name in pairs]


# ---------------------------------------------------------------------------
# 消息构建（与 core.py 模板逐字对拍）
# ---------------------------------------------------------------------------

class MessageBuilderTests(unittest.TestCase):

    def test_reminder_message_matches_core_template(self):
        title, body = build_reminder_message(
            display="杭州", month=9, day=11, weekday="五",
            unfilled=["张三", "李四"], url=_URL,
        )
        self.assertEqual(title, "销售日报填写提醒")
        self.assertEqual(body, (
            "### 📋 销售日报填写提醒（杭州 9月11日 周五）\n"
            "\n"
            "以下 **2** 位同事还未填写今日销售日报，请尽快填写：\n"
            "\n"
            "**张三、李四**\n"
            "\n"
            "也可直接在群里 **@提醒事项 + 数字** 报数（如 `@提醒事项 12800`，报 0 也行）\n"
            "\n"
            "[点此填写](https://example.com/table)"
        ))

    def test_reminder_message_with_missing_note(self):
        _, body = build_reminder_message(
            display="杭州", month=9, day=11, weekday="五",
            unfilled=["张三"], url=_URL, missing=["王五"],
        )
        self.assertTrue(body.endswith(
            "\n\n（王五 未在通讯录映射中，无法@，请手动提醒）"
        ))

    def test_check_message_matches_core_template(self):
        title, body = build_check_message(
            display="杭州", month=9, day=11,
            unfilled=["张三", "李四"], url=_URL,
        )
        self.assertEqual(title, "销售日报未填写")
        self.assertEqual(body, (
            "### ⏰ 销售日报未填写（杭州 9月11日）\n"
            "\n"
            "截至 20:00，以下 **2** 位同事仍未填写：\n"
            "\n"
            "**张三、李四**\n"
            "\n"
            "已同步 DING 提醒以上人员，请在群里 @提醒事项 报数或直接填写。\n"
            "[点此填写](https://example.com/table)"
        ))

    def test_ding_content_matches_core_template(self):
        self.assertEqual(
            build_ding_content(
                display="杭州", month=9, day=11, weekday="五", url=_URL,
            ),
            "【销售日报催办】杭州 9月11日（周五）：你还未填写今日销售日报，"
            "请在群里 @提醒事项 报数或填写表格 https://example.com/table",
        )


# ---------------------------------------------------------------------------
# run_remind
# ---------------------------------------------------------------------------

class RunRemindTests(unittest.TestCase):

    def _outbox(self, enqueued=True):
        outbox = Mock()
        outbox.enqueue.return_value = enqueued
        return outbox

    def test_rest_day_skips_without_enqueue(self):
        conn = _RouterConn(workdays={date(2026, 9, 10)})  # 9/11 不在其中
        outbox = self._outbox()

        outcome = run_remind(
            conn, outbox, region="hangzhou", display="杭州", table_url=_URL,
            business_date=_DAY, now=_NOW,
        )

        self.assertEqual(outcome.status, "rest_day")
        self.assertFalse(outcome.enqueued)
        outbox.enqueue.assert_not_called()

    def test_missing_calendar_month_fails_loudly(self):
        conn = _RouterConn(workdays=set())
        with self.assertRaisesRegex(MartTaskError, "2026-09"):
            run_remind(
                conn, self._outbox(), region="hangzhou", display="杭州",
                table_url=_URL, business_date=_DAY, now=_NOW,
            )

    def test_all_filled_enqueues_nothing(self):
        conn = _RouterConn(
            workdays={_DAY},
            members=_members(("u1", "张三")),
            filled={"张三"},
        )
        outbox = self._outbox()

        outcome = run_remind(
            conn, outbox, region="hangzhou", display="杭州", table_url=_URL,
            business_date=_DAY, now=_NOW,
        )

        self.assertEqual(outcome.status, "all_filled")
        outbox.enqueue.assert_not_called()

    def test_unfilled_enqueues_remind_row_with_at_ids(self):
        conn = _RouterConn(
            workdays={_DAY},
            members=_members(("u1", "张三"), ("u2", "李四")),
            filled={"张三"},
        )
        outbox = self._outbox()

        outcome = run_remind(
            conn, outbox, region="hangzhou", display="杭州", table_url=_URL,
            business_date=_DAY, now=_NOW,
        )

        self.assertEqual(outcome.status, "enqueued")
        self.assertEqual(outcome.unfilled, ("李四",))
        kwargs = outbox.enqueue.call_args.kwargs
        self.assertEqual(kwargs["region"], "hangzhou")
        self.assertEqual(kwargs["kind"], "remind")
        self.assertEqual(kwargs["business_date"], _DAY)
        self.assertEqual(kwargs["title"], "销售日报填写提醒")
        self.assertIn("**李四**", kwargs["body_md"])
        self.assertEqual(kwargs["at_user_ids"], ["u2"])
        self.assertEqual(kwargs["created_at"], _NOW)

    def test_duplicate_enqueue_reports_already_sent(self):
        conn = _RouterConn(
            workdays={_DAY},
            members=_members(("u1", "张三")),
            filled=set(),
        )
        outcome = run_remind(
            conn, self._outbox(enqueued=False), region="hangzhou",
            display="杭州", table_url=_URL, business_date=_DAY, now=_NOW,
        )
        self.assertEqual(outcome.status, "already_sent")
        self.assertFalse(outcome.enqueued)

    def test_aliases_map_table_names_back_to_members(self):
        conn = _RouterConn(
            workdays={_DAY},
            members=_members(("u1", "张三丰")),
            filled={"老张"},
        )
        outcome = run_remind(
            conn, self._outbox(), region="hangzhou", display="杭州",
            table_url=_URL, business_date=_DAY, now=_NOW,
            aliases={"张三丰": "老张"},
        )
        self.assertEqual(outcome.status, "all_filled")


# ---------------------------------------------------------------------------
# run_check
# ---------------------------------------------------------------------------

class RunCheckTests(unittest.TestCase):

    def test_check_enqueues_group_and_ding_rows(self):
        conn = _RouterConn(
            workdays={_DAY},
            members=_members(("u1", "张三"), ("u2", "李四")),
            filled={"张三"},
        )
        outbox = Mock()
        outbox.enqueue.return_value = True

        outcome = run_check(
            conn, outbox, region="hangzhou", display="杭州", table_url=_URL,
            business_date=_DAY, now=_NOW, cc_user_ids=["cc-shen"],
        )

        self.assertEqual(outcome.status, "enqueued")
        self.assertEqual(outbox.enqueue.call_count, 2)

        check_kwargs = outbox.enqueue.call_args_list[0].kwargs
        self.assertEqual(check_kwargs["kind"], "check")
        # 群消息 @未填人 + cc
        self.assertEqual(check_kwargs["at_user_ids"], ["u2", "cc-shen"])
        self.assertEqual(check_kwargs["title"], "销售日报未填写")

        ding_kwargs = outbox.enqueue.call_args_list[1].kwargs
        self.assertEqual(ding_kwargs["kind"], "ding")
        # DING 只发未填人，不含 cc
        self.assertEqual(ding_kwargs["at_user_ids"], ["u2"])
        self.assertIn("【销售日报催办】杭州 9月11日（周五）", ding_kwargs["body_md"])

    def test_check_partial_duplicate_still_enqueues_the_missing_row(self):
        """check 行已存在但 ding 行不在时，只补 ding——比单一 state key 更细。"""
        conn = _RouterConn(
            workdays={_DAY},
            members=_members(("u1", "张三")),
            filled=set(),
        )
        outbox = Mock()
        outbox.enqueue.side_effect = [False, True]

        outcome = run_check(
            conn, outbox, region="hangzhou", display="杭州", table_url=_URL,
            business_date=_DAY, now=_NOW,
        )

        self.assertEqual(outcome.status, "enqueued")
        self.assertTrue(outcome.enqueued)

    def test_check_all_filled_enqueues_nothing(self):
        conn = _RouterConn(
            workdays={_DAY},
            members=_members(("u1", "张三")),
            filled={"张三"},
        )
        outbox = Mock()

        outcome = run_check(
            conn, outbox, region="hangzhou", display="杭州", table_url=_URL,
            business_date=_DAY, now=_NOW,
        )

        self.assertEqual(outcome.status, "all_filled")
        outbox.enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
