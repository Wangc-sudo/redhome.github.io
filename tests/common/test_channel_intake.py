"""渠道日销机器人填报（channel_intake）测试：解析纯逻辑 + 主流程。"""

import unittest
from datetime import date, datetime

from common.gateway.channel_intake import (
    FillEntry,
    FillReject,
    build_channel_help,
    handle_channel_fill,
    parse_fill_text,
)
from common.region_config import RegionConfig

_TODAY = date(2026, 9, 29)  # 周二；默认业务日 = 09-28
_NOW = datetime(2026, 9, 29, 9, 40)

_ROSTER = {
    "JD习水村习酒专卖店": "京东",
    "JD习水村酒类专营店": "京东",
    "JD购喝": "京东",
    "TM习酒酒类旗舰店": "天猫",
    "TM惠群贵礼旗舰店": "天猫",
    "PDD习酒旗舰店": "拼多多",
    "DY1988旗舰店（店播）": "直播",
    "朴朴": "即时零售",
    "私域": "私域",
}


def _parse(text, roster=None):
    return parse_fill_text(text, today=_TODAY, roster=roster or _ROSTER)


class ParseFillTextTest(unittest.TestCase):
    def test_basic_channel_store_amount(self):
        entries, rejects = _parse("京东 购喝 15867")
        self.assertEqual(rejects, [])
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.business_date, date(2026, 9, 28))  # 默认 T-1
        self.assertEqual(entry.channel, "京东")
        self.assertEqual(entry.store_name, "JD购喝")
        self.assertEqual(entry.amount, 15867)
        self.assertEqual(entry.recognized_from, "购喝")

    def test_ambiguous_within_channel_rejected(self):
        # 渠道词过滤后仍歧义（真实名册：京东两家「习水村」店）
        entries, rejects = _parse("京东 习水村 15867")
        self.assertEqual(entries, [])
        self.assertEqual(len(rejects), 1)
        self.assertIn("匹配多家店", rejects[0].reason)

    def test_distinguishing_keyword_resolves_within_channel(self):
        entries, rejects = _parse("京东 习酒专卖店 15867")
        self.assertEqual(rejects, [])
        self.assertEqual(entries[0].store_name, "JD习水村习酒专卖店")

    def test_full_store_name_without_channel_word(self):
        entries, _ = _parse("JD习水村习酒专卖店 15867")
        self.assertEqual(entries[0].store_name, "JD习水村习酒专卖店")
        self.assertIsNone(entries[0].recognized_from)  # 全名不算模糊识别

    def test_unique_store_without_channel_word(self):
        entries, _ = _parse("朴朴 118038")
        self.assertEqual(entries[0].channel, "即时零售")
        self.assertEqual(entries[0].store_name, "朴朴")

    def test_store_name_equals_channel_word(self):
        # 「私域 0」：店名=渠道词，整段匹配优先，不能误判渠道级
        entries, rejects = _parse("私域 0")
        self.assertEqual(rejects, [])
        self.assertEqual(entries[0].store_name, "私域")
        self.assertEqual(entries[0].channel, "私域")
        self.assertEqual(entries[0].amount, 0)  # 0 合法

    def test_channel_level_only_maochao(self):
        entries, rejects = _parse("猫超 731033")
        self.assertEqual(rejects, [])
        self.assertIsNone(entries[0].store_name)
        self.assertEqual(entries[0].channel, "猫超")

    def test_channel_level_rejected_for_other_channel(self):
        entries, rejects = _parse("京东 15867")
        self.assertEqual(entries, [])
        self.assertEqual(len(rejects), 1)
        self.assertIn("需带店名", rejects[0].reason)

    def test_date_prefix_backfill(self):
        for text in ("9.27 天猫 惠群 82059", "9-27 天猫 惠群 82059",
                     "9/27 天猫 惠群 82059", "9月27日 天猫 惠群 82059"):
            entries, rejects = _parse(text)
            self.assertEqual(rejects, [], text)
            self.assertEqual(entries[0].business_date, date(2026, 9, 27), text)
            self.assertEqual(entries[0].amount, 82059, text)

    def test_date_prefix_future_rolls_back_and_rejected(self):
        # 12.31 相对 09-29 回推到 2025-12-31，超 30 天 → 拒绝
        entries, rejects = _parse("12.31 朴朴 100")
        self.assertEqual(entries, [])
        self.assertIn("超出范围", rejects[0].reason)

    def test_ambiguous_store_rejected_with_candidates(self):
        entries, rejects = _parse("习水村 15867")  # 跨渠道两家店
        self.assertEqual(entries, [])
        self.assertEqual(len(rejects), 1)
        self.assertIn("匹配多家店", rejects[0].reason)
        self.assertIn("JD习水村习酒专卖店", rejects[0].reason)

    def test_unknown_store_rejected(self):
        entries, rejects = _parse("京东 不存在的店 100")
        self.assertEqual(entries, [])
        self.assertIn("未找到门店", rejects[0].reason)

    def test_store_digits_not_taken_as_amount(self):
        # DY1988：粘连数字不当金额 → 金额未识别
        entries, rejects = _parse("直播 DY1988旗舰店（店播）")
        self.assertEqual(entries, [])
        self.assertEqual(len(rejects), 1)
        self.assertIn("金额未识别", rejects[0].reason)

    def test_store_with_digits_and_amount(self):
        entries, rejects = _parse("DY1988旗舰店（店播） 72252")
        self.assertEqual(rejects, [])
        self.assertEqual(entries[0].store_name, "DY1988旗舰店（店播）")
        self.assertEqual(entries[0].amount, 72252)

    def test_thousands_separator_and_decimal(self):
        entries, _ = _parse("朴朴 118,038")
        self.assertEqual(entries[0].amount, 118038)
        entries, _ = _parse("朴朴 2912.50")
        self.assertEqual(entries[0].amount, 2912.5)

    def test_multi_line(self):
        entries, rejects = _parse("京东 购喝 15867\n朴朴 118038\n私域 0")
        self.assertEqual(rejects, [])
        self.assertEqual(len(entries), 3)

    def test_chitchat_without_digits_ignored(self):
        entries, rejects = _parse("大家早上好")
        self.assertEqual(entries, [])
        self.assertEqual(rejects, [])

    def test_leading_mention_stripped(self):
        entries, rejects = _parse("@日报小机器人 朴朴 100")
        self.assertEqual(rejects, [])
        self.assertEqual(entries[0].amount, 100)

    def test_amount_must_be_separated(self):
        # 金额与店名粘连 → 无金额
        entries, rejects = _parse("朴朴118038")
        self.assertEqual(entries, [])
        self.assertEqual(len(rejects), 1)


class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []
        self.lastrowid = 0

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        if "fact_channel_store_target" in sql:
            self._rows = [
                {"store_name": s, "channel": c}
                for s, c in self._conn.roster.items()
            ]
        elif "channel_sales_robot_inbox" in sql and sql.lstrip().startswith(
            "SELECT"
        ):
            key = (params[0], params[1], params[2])
            self._rows = [
                {"sales_amount": v}
                for k, v in self._conn.inbox_latest.items() if k == key
            ]
        elif "fact_channel_daily_sales" in sql:
            totals = {}
            for row in self._conn.fact_rows:
                totals[row["channel"]] = (
                    totals.get(row["channel"], 0.0) + float(row["sales_amount"])
                )
            self._rows = [
                {"channel": channel, "s": total}
                for channel, total in totals.items()
            ]
        else:  # INSERT inbox
            self._rows = []
            self._conn.inbox_rows.append(params)
            self.lastrowid = len(self._conn.inbox_rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _Conn:
    def __init__(self, *, roster=None, inbox_latest=None, fact_rows=()):
        self.roster = roster if roster is not None else dict(_ROSTER)
        # {(date, channel, store): amount} —— 模拟同键最新 parsed 行
        self.inbox_latest = dict(inbox_latest or {})
        self.fact_rows = list(fact_rows)
        self.inbox_rows = []
        self.executed = []

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        pass


def _cfg():
    return RegionConfig(
        region="qudao", display="渠道日报", table_url="https://example.com",
        robot_code="rc", open_conversation_id="conv-qudao",
        aliases={}, cc_user_ids=(),
    )


class HandleChannelFillTest(unittest.TestCase):
    def test_record_single(self):
        conn = _Conn()
        outcome = handle_channel_fill(
            conn, region_cfg=_cfg(), text="京东 购喝 15867",
            sender_uid="u9", sender_name="饶佳君",
            conversation_id="conv-qudao", now=_NOW,
        )
        self.assertEqual(outcome.status, "recorded")
        self.assertIn("JD购喝", outcome.reply)
        self.assertIn("15867", outcome.reply)
        self.assertEqual(len(conn.inbox_rows), 1)
        row = conn.inbox_rows[0]
        self.assertEqual(row[3], "京东 购喝 15867")  # raw_text
        self.assertEqual(row[4], "京东")
        self.assertEqual(row[5], "JD购喝")
        self.assertEqual(row[6], date(2026, 9, 28))
        self.assertEqual(row[8], "parsed")

    def test_overwrite_marks_previous(self):
        conn = _Conn(inbox_latest={
            (date(2026, 9, 28), "京东", "JD购喝"): 12000,
        })
        outcome = handle_channel_fill(
            conn, region_cfg=_cfg(), text="京东 购喝 15867",
            sender_uid="u9", sender_name=None,
            conversation_id="conv-qudao", now=_NOW,
        )
        self.assertTrue(outcome.overwritten)
        self.assertIn("覆盖旧值", outcome.reply)

    def test_chitchat_ignored(self):
        conn = _Conn()
        outcome = handle_channel_fill(
            conn, region_cfg=_cfg(), text="收到", sender_uid="u9",
            sender_name=None, conversation_id="conv-qudao", now=_NOW,
        )
        self.assertEqual(outcome.status, "ignored")
        self.assertIsNone(outcome.reply)
        self.assertEqual(conn.inbox_rows, [])

    def test_slash_goes_to_channel_help(self):
        conn = _Conn()
        outcome = handle_channel_fill(
            conn, region_cfg=_cfg(), text="/帮助", sender_uid="u9",
            sender_name=None, conversation_id="conv-qudao", now=_NOW,
        )
        self.assertEqual(outcome.status, "aux")
        self.assertEqual(outcome.reply, build_channel_help())
        self.assertEqual(conn.inbox_rows, [])

    def test_reject_only_also_recorded_in_inbox(self):
        conn = _Conn()
        outcome = handle_channel_fill(
            conn, region_cfg=_cfg(), text="京东 不存在的店 100",
            sender_uid="u9", sender_name=None,
            conversation_id="conv-qudao", now=_NOW,
        )
        self.assertEqual(outcome.status, "no_number")
        self.assertIn("未找到门店", outcome.reply)
        self.assertEqual(len(conn.inbox_rows), 1)
        self.assertEqual(conn.inbox_rows[0][8], "rejected")

    def test_progress_line_present(self):
        conn = _Conn(fact_rows=[
            {"channel": "京东", "sales_amount": 1000,
             "business_date": date(2026, 9, 1)},
        ])
        outcome = handle_channel_fill(
            conn, region_cfg=_cfg(), text="京东 购喝 15867",
            sender_uid="u9", sender_name=None,
            conversation_id="conv-qudao", now=_NOW,
        )
        self.assertIn("📊 京东 本月累计", outcome.reply)


if __name__ == "__main__":
    unittest.main()
