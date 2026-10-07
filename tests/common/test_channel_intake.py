"""渠道日销机器人填报（channel_intake）测试：三键解析 + 主流程 + 映射表。"""

import json
import unittest
from datetime import date, datetime

from common.gateway.channel_intake import (
    build_channel_help,
    build_mapping_table,
    build_roster,
    handle_channel_fill,
    parse_fill_text,
)
from common.region_config import RegionConfig

_TODAY = date(2026, 9, 29)  # 周二；默认业务日 = 09-28
_NOW = datetime(2026, 9, 29, 9, 40)

# (store, channel, monthly_target, owners)——贴近真实名册（含渠道内近似名组）。
_ROSTER_SEED = [
    ("JD购喝", "京东", 1500000, "娄灿斌"),
    ("JD习水村酒类专营店", "京东", 700000, "饶佳君"),
    ("JD习水村习酒专卖店", "京东", 600000, "王蕊"),
    ("JD致中和旗舰店", "京东", 200000, "饶佳君"),
    ("JD金沙专卖店", "京东", 150000, "王蕊"),
    ("TM习酒酒类旗舰店", "天猫", 3000000, "钟甜"),
    ("TM惠群贵礼旗舰店", "天猫", 100000, "钟甜"),
    ("PDD习酒旗舰店", "拼多多", 500000, "窦超"),
    ("DY1988旗舰店（店播）", "直播", 10670000, "Yan、Jevon"),
    ("朴朴", "即时零售", 960000, "黄贤宋、杨情情"),
    ("私域", "私域", 250000, "王荧月"),
    ("MC猫超", "猫超", 15000000, "恬恬、懒羊羊"),
]


def make_rows():
    return [
        {
            "store_name": store,
            "channel": channel,
            "monthly_target": target,
            "owners_json": json.dumps(
                [{"name": n} for n in owners.split("、")]
            ),
        }
        for store, channel, target, owners in _ROSTER_SEED
    ]


_ROSTER = build_roster(make_rows())

#: 京东编号（目标降序）：1=购喝 2=习水村酒类 3=习水村习酒 4=致中和 5=金沙


def _parse(text, roster=None):
    return parse_fill_text(text, today=_TODAY, roster=roster or _ROSTER)


class RosterBuildTest(unittest.TestCase):
    def test_numbers_by_target_desc(self):
        self.assertEqual(_ROSTER.numbers[("京东", 1)], "JD购喝")
        self.assertEqual(_ROSTER.numbers[("京东", 2)], "JD习水村酒类专营店")
        self.assertEqual(_ROSTER.numbers[("京东", 5)], "JD金沙专卖店")
        self.assertEqual(_ROSTER.store_numbers["私域"], ("私域", 1))

    def test_owners_index(self):
        self.assertEqual(len(_ROSTER.owners["饶佳君"]), 2)
        self.assertEqual(_ROSTER.owners_casefold["jevon"], "Jevon")


class NumberKeyParseTest(unittest.TestCase):
    def test_channel_number(self):
        entries, rejects = _parse("京东 1 15867")
        self.assertEqual(rejects, [])
        self.assertEqual(entries[0].store_name, "JD购喝")
        self.assertEqual(entries[0].channel, "京东")
        self.assertEqual(entries[0].business_date, date(2026, 9, 28))
        self.assertIsNone(entries[0].recognized_from)

    def test_channel_number_with_date(self):
        entries, rejects = _parse("9.27 京东 2 18461")
        self.assertEqual(rejects, [])
        self.assertEqual(entries[0].store_name, "JD习水村酒类专营店")
        self.assertEqual(entries[0].business_date, date(2026, 9, 27))

    def test_number_out_of_range(self):
        entries, rejects = _parse("京东 99 100")
        self.assertEqual(entries, [])
        self.assertIn("编号不存在", rejects[0].reason)

    def test_bare_number_without_channel(self):
        entries, rejects = _parse("3 15867")
        self.assertEqual(entries, [])
        self.assertEqual(len(rejects), 1)


class OwnerKeyParseTest(unittest.TestCase):
    def test_owner_single_store_direct(self):
        entries, rejects = _parse("娄灿斌 15867")
        self.assertEqual(rejects, [])
        self.assertEqual(entries[0].store_name, "JD购喝")
        self.assertEqual(entries[0].recognized_from, "娄灿斌")

    def test_owner_multi_store_guides_with_numbers(self):
        entries, rejects = _parse("饶佳君 15867")
        self.assertEqual(entries, [])
        self.assertEqual(len(rejects), 1)
        self.assertIn("负责 2 家店", rejects[0].reason)
        self.assertIn("2=JD习水村酒类专营店", rejects[0].reason)
        self.assertIn("4=JD致中和旗舰店", rejects[0].reason)
        self.assertIn("京东 2 金额", rejects[0].reason)

    def test_owner_with_trailing_channel_word(self):
        entries, rejects = _parse("王蕊 京东 100")
        self.assertEqual(entries, [])
        self.assertIn("负责 2 家店", rejects[0].reason)
        self.assertIn("3=JD习水村习酒专卖店", rejects[0].reason)
        self.assertIn("5=JD金沙专卖店", rejects[0].reason)

    def test_owner_casefold_english_name(self):
        # 小写花名命中 casefold 索引；种子里 Jevon 仅 1 店 → 直达
        entries, rejects = _parse("jevon 100")
        self.assertEqual(rejects, [])
        self.assertEqual(entries[0].store_name, "DY1988旗舰店（店播）")
        self.assertEqual(entries[0].recognized_from, "Jevon")


class StoreNameParseTest(unittest.TestCase):
    def test_basic_channel_store_amount(self):
        entries, rejects = _parse("京东 购喝 15867")
        self.assertEqual(rejects, [])
        entry = entries[0]
        self.assertEqual(entry.business_date, date(2026, 9, 28))
        self.assertEqual(entry.channel, "京东")
        self.assertEqual(entry.store_name, "JD购喝")
        self.assertEqual(entry.amount, 15867)
        self.assertEqual(entry.recognized_from, "购喝")

    def test_full_store_name_without_channel_word(self):
        entries, _ = _parse("JD习水村习酒专卖店 15867")
        self.assertEqual(entries[0].store_name, "JD习水村习酒专卖店")
        self.assertIsNone(entries[0].recognized_from)

    def test_unique_store_without_channel_word(self):
        entries, _ = _parse("朴朴 118038")
        self.assertEqual(entries[0].channel, "即时零售")
        self.assertEqual(entries[0].store_name, "朴朴")

    def test_store_name_equals_channel_word(self):
        entries, rejects = _parse("私域 0")
        self.assertEqual(rejects, [])
        self.assertEqual(entries[0].store_name, "私域")
        self.assertEqual(entries[0].amount, 0)

    def test_maochao_matches_store_first(self):
        # 「猫超」整段优先匹配 MC猫超 店（店级粒度，P1 联调确认行为）
        entries, rejects = _parse("猫超 731033")
        self.assertEqual(rejects, [])
        self.assertEqual(entries[0].store_name, "MC猫超")

    def test_channel_level_rejected_for_other_channel(self):
        entries, rejects = _parse("京东 x15867".replace("x", ""))
        # 「京东 15867」：15867 是金额，无店名/编号 → 提示带店名
        self.assertEqual(entries, [])
        self.assertIn("需带店名或编号", rejects[0].reason)

    def test_date_prefix_backfill(self):
        for text in ("9.27 天猫 惠群 82059", "9-27 天猫 惠群 82059",
                     "9/27 天猫 惠群 82059", "9月27日 天猫 惠群 82059"):
            entries, rejects = _parse(text)
            self.assertEqual(rejects, [], text)
            self.assertEqual(entries[0].business_date, date(2026, 9, 27), text)
            self.assertEqual(entries[0].amount, 82059, text)

    def test_date_prefix_out_of_range(self):
        entries, rejects = _parse("12.31 朴朴 100")
        self.assertEqual(entries, [])
        self.assertIn("超出范围", rejects[0].reason)

    def test_ambiguous_cross_channel(self):
        entries, rejects = _parse("习水村 15867")
        self.assertEqual(entries, [])
        self.assertIn("匹配多家店", rejects[0].reason)
        self.assertIn("渠道 编号", rejects[0].reason)

    def test_ambiguous_within_channel(self):
        entries, rejects = _parse("京东 习水村 15867")
        self.assertEqual(entries, [])
        self.assertIn("匹配多家店", rejects[0].reason)

    def test_distinguishing_keyword_resolves(self):
        entries, rejects = _parse("京东 习酒专卖店 15867")
        self.assertEqual(rejects, [])
        self.assertEqual(entries[0].store_name, "JD习水村习酒专卖店")

    def test_unknown_store(self):
        entries, rejects = _parse("京东 不存在的店 100")
        self.assertEqual(entries, [])
        self.assertIn("未找到门店", rejects[0].reason)

    def test_store_digits_not_taken_as_amount(self):
        entries, rejects = _parse("直播 DY1988旗舰店（店播）")
        self.assertEqual(entries, [])
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
        entries, rejects = _parse("京东 1 15867\n朴朴 118038\n私域 0")
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


class MappingTableTest(unittest.TestCase):
    def test_table_contains_channels_numbers_owners(self):
        table = build_mapping_table(_ROSTER)
        self.assertIn("【京东】", table)
        self.assertIn("1=JD购喝（娄灿斌）", table)
        self.assertIn("2=JD习水村酒类专营店（饶佳君）", table)
        self.assertIn("1=朴朴（黄贤宋、杨情情）", table)
        self.assertIn("【猫超】", table)


class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []
        self.lastrowid = 0

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        if "dim_report_roster" in sql:
            # 名册表（2026-10-06 切源）：默认空表 → 回退 legacy owners_json
            self._rows = list(self._conn.roster_table_rows)
        elif "fact_channel_store_target" in sql:
            self._rows = list(self._conn.roster_rows)
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
    def __init__(self, *, roster_rows=None, inbox_latest=None, fact_rows=(),
                 roster_table_rows=()):
        self.roster_rows = roster_rows if roster_rows is not None else make_rows()
        self.inbox_latest = dict(inbox_latest or {})
        self.fact_rows = list(fact_rows)
        self.roster_table_rows = list(roster_table_rows)
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
    def test_record_single_with_number_echo(self):
        conn = _Conn()
        outcome = handle_channel_fill(
            conn, region_cfg=_cfg(), text="京东 1 15867",
            sender_uid="u9", sender_name="娄灿斌",
            conversation_id="conv-qudao", now=_NOW,
        )
        self.assertEqual(outcome.status, "recorded")
        self.assertIn("1=JD购喝", outcome.reply)  # 编号回显
        self.assertIn("15867", outcome.reply)
        self.assertEqual(len(conn.inbox_rows), 1)
        row = conn.inbox_rows[0]
        self.assertEqual(row[4], "京东")
        self.assertEqual(row[5], "JD购喝")
        self.assertEqual(row[6], date(2026, 9, 28))
        self.assertEqual(row[8], "parsed")

    def test_overwrite_marks_previous(self):
        conn = _Conn(inbox_latest={
            (date(2026, 9, 28), "京东", "JD购喝"): 12000,
        })
        outcome = handle_channel_fill(
            conn, region_cfg=_cfg(), text="京东 1 15867",
            sender_uid="u9", sender_name="娄灿斌",
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

    def test_mapping_table_command(self):
        conn = _Conn()
        for text in ("/店铺映射表", "/映射表"):
            outcome = handle_channel_fill(
                conn, region_cfg=_cfg(), text=text, sender_uid="u9",
                sender_name=None, conversation_id="conv-qudao", now=_NOW,
            )
            self.assertEqual(outcome.status, "aux", text)
            self.assertIn("1=JD购喝", outcome.reply, text)

    def test_owner_guide_also_recorded_in_inbox(self):
        conn = _Conn()
        outcome = handle_channel_fill(
            conn, region_cfg=_cfg(), text="饶佳君 15867",
            sender_uid="u9", sender_name=None,
            conversation_id="conv-qudao", now=_NOW,
        )
        self.assertEqual(outcome.status, "no_number")
        self.assertIn("负责 2 家店", outcome.reply)
        self.assertEqual(len(conn.inbox_rows), 1)
        self.assertEqual(conn.inbox_rows[0][8], "rejected")

    def test_owner_fills_own_store(self):
        conn = _Conn()
        outcome = handle_channel_fill(
            conn, region_cfg=_cfg(), text="京东 2 100",
            sender_uid="u-rjj", sender_name="饶佳君",
            conversation_id="conv-qudao", now=_NOW,
        )
        self.assertEqual(outcome.status, "recorded")
        self.assertIn("JD习水村酒类专营店", outcome.reply)

    def test_owner_filling_others_store_denied(self):
        conn = _Conn()
        outcome = handle_channel_fill(
            conn, region_cfg=_cfg(), text="京东 1 100",
            sender_uid="u-rjj", sender_name="饶佳君",
            conversation_id="conv-qudao", now=_NOW,
        )
        self.assertEqual(outcome.status, "no_number")
        self.assertIn("不是你负责的店铺", outcome.reply)
        self.assertIn("JD习水村酒类专营店", outcome.reply)  # 列出本人店
        self.assertEqual(conn.inbox_rows[0][8], "rejected")

    def test_frozen_store_numbers_win_over_target_order(self):
        # 编号冻结（2026-10-07 统一管理方案 §6-A）：行带 store_no 时按冻结
        # 编号——目标大的不再排前；未编号的店排在该渠道已编号之后。
        rows = [
            {"store_name": "大店", "channel": "京东", "monthly_target": 999,
             "store_no": 2, "owners_json": None},
            {"store_name": "小店", "channel": "京东", "monthly_target": 1,
             "store_no": 1, "owners_json": None},
            {"store_name": "新店", "channel": "京东", "monthly_target": 500,
             "store_no": None, "owners_json": None},
        ]
        roster = build_roster(rows)
        self.assertEqual("小店", roster.numbers[("京东", 1)])
        self.assertEqual("大店", roster.numbers[("京东", 2)])
        self.assertEqual("新店", roster.numbers[("京东", 3)])  # 未编号排尾
        self.assertEqual(("京东", 2), roster.store_numbers["大店"])

    def test_roster_table_overrides_legacy_owners(self):
        # 名册切源（2026-10-06）：dim_report_roster 有启用记录时负责人归属以
        # 名册表为准，legacy owners_json 不再生效。名册表把饶佳君改到
        # 京东 1（legacy 拒）→ 放行；京东 2（legacy 放）→ 拒。
        roster_table = [{"entity_key": "JD购喝", "person_name": "饶佳君"}]

        allowed = handle_channel_fill(
            _Conn(roster_table_rows=roster_table), region_cfg=_cfg(),
            text="京东 1 100", sender_uid="u-rjj", sender_name="饶佳君",
            conversation_id="conv-qudao", now=_NOW,
        )
        self.assertEqual("recorded", allowed.status)

        denied = handle_channel_fill(
            _Conn(roster_table_rows=roster_table), region_cfg=_cfg(),
            text="京东 2 100", sender_uid="u-rjj", sender_name="饶佳君",
            conversation_id="conv-qudao", now=_NOW,
        )
        self.assertEqual("no_number", denied.status)
        self.assertIn("不是你负责的店铺", denied.reply)

    def test_co_owner_allowed(self):
        # 共管店：任一共管人可报（朴朴=黄贤宋、杨情情）
        conn = _Conn()
        outcome = handle_channel_fill(
            conn, region_cfg=_cfg(), text="朴朴 100",
            sender_uid="u-yqq", sender_name="杨情情",
            conversation_id="conv-qudao", now=_NOW,
        )
        self.assertEqual(outcome.status, "recorded")

    def test_admin_can_fill_for_others(self):
        conn = _Conn()
        outcome = handle_channel_fill(
            conn, region_cfg=_cfg(), text="京东 2 100",
            sender_uid="u-admin", sender_name="王城",
            conversation_id="conv-qudao", now=_NOW, is_admin=True,
        )
        self.assertEqual(outcome.status, "recorded")
        self.assertIn("JD习水村酒类专营店", outcome.reply)

    def test_unidentified_sender_denied(self):
        conn = _Conn()
        outcome = handle_channel_fill(
            conn, region_cfg=_cfg(), text="朴朴 100",
            sender_uid="u-x", sender_name="路人甲",
            conversation_id="conv-qudao", now=_NOW,
        )
        self.assertEqual(outcome.status, "no_number")
        self.assertIn("无法识别你的身份", outcome.reply)
        self.assertEqual(conn.inbox_rows[0][8], "rejected")

    def test_nick_with_suffix_resolves(self):
        # 带后缀的群昵称（饶佳君-习酒）包含互查唯一命中
        conn = _Conn()
        outcome = handle_channel_fill(
            conn, region_cfg=_cfg(), text="京东 2 100",
            sender_uid="u-rjj", sender_name="饶佳君-习酒",
            conversation_id="conv-qudao", now=_NOW,
        )
        self.assertEqual(outcome.status, "recorded")

    def test_progress_line_present(self):
        conn = _Conn(fact_rows=[
            {"channel": "京东", "sales_amount": 1000,
             "business_date": date(2026, 9, 1)},
        ])
        outcome = handle_channel_fill(
            conn, region_cfg=_cfg(), text="京东 1 15867",
            sender_uid="u9", sender_name="娄灿斌",
            conversation_id="conv-qudao", now=_NOW,
        )
        self.assertIn("📊 京东 本月累计", outcome.reply)


if __name__ == "__main__":
    unittest.main()
