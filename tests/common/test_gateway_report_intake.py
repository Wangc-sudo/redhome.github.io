"""Tests for the Stream report intake (parse -> gate -> mart write -> reply)."""

import unittest
from datetime import date, datetime
from decimal import Decimal

from common.gateway.report_intake import (
    STREAM_RUN_ID,
    _parse_backfill_date,
    build_format_hint,
    build_multi_recorded_reply,
    build_not_member_reply,
    build_not_workday_reply,
    handle_report,
    parse_aux_command,
    parse_report_amount,
    parse_report_metrics,
    region_for_conversation,
)
from common.daily_robot.mart_tasks import MartTaskError
from common.region_config import RegionConfig


_DAY = date(2026, 9, 11)  # 周五
_NOW = datetime(2026, 9, 11, 17, 30)
_WORKDAYS = {date(2026, 9, d) for d in range(1, 31) if d not in (6, 13, 19, 25, 26, 27)}


class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []
        self._one = None

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        if "dim_calendar" in sql:
            self._rows = self._conn.workday_rows
        elif "dim_robot_member" in sql:
            self._rows = self._conn.member_rows
            self._one = self._rows[0] if self._rows else None
        elif "bi_authz_grant" in sql:
            self._rows = []
            self._one = {"x": 1} if self._conn.admin else None
        elif "ORDER BY (`monthly_target` IS NULL)" in sql:
            # 当日已有行（业务键）查询
            self._rows = []
            self._one = self._conn.existing_row
        elif sql.lstrip().startswith("SELECT"):
            # 月事实查询（带 region 键的行按查询参数过滤，模拟生产 WHERE）
            rows = self._conn.fact_rows
            if params:
                rows = [r for r in rows
                        if "region" not in r or r["region"] == params[0]]
            self._rows = rows
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._one

    def close(self):
        pass


class _Conn:
    def __init__(self, *, workdays=_WORKDAYS, members=(), facts=(), existing=None,
                 admin=False):
        self.workday_rows = [{"business_date": d} for d in workdays]
        self.member_rows = list(members)
        self.fact_rows = list(facts)
        self.existing_row = existing
        self.executed = []
        self.commits = 0
        self.admin = admin

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1


def _cfg():
    return RegionConfig(
        region="hangzhou", display="杭州",
        table_url="https://example.com/table",
        robot_code="rc", open_conversation_id="conv-hz",
        aliases={"张三丰": "老张"}, cc_user_ids=(),
    )


def _member(user_id="u1", name="张三", region="hangzhou", dept="杭中"):
    return {
        "user_id": user_id, "name": name, "region": region,
        "dept_id": "1049728636", "dept_name": dept,
    }


class ParseAmountTests(unittest.TestCase):

    def test_parses_plain_and_grouped_numbers(self):
        self.assertEqual(parse_report_amount("12800"), 12800)
        self.assertEqual(parse_report_amount("12,800"), 12800)
        self.assertEqual(parse_report_amount("12，800"), 12800)

    def test_parses_zero_negative_and_decimal(self):
        self.assertEqual(parse_report_amount("报 0"), 0)
        self.assertEqual(parse_report_amount("-50"), -50)
        self.assertEqual(parse_report_amount("1.5"), 1.5)

    def test_takes_the_first_number(self):
        self.assertEqual(parse_report_amount("@提醒事项 12800 今天销量"), 12800)

    def test_integral_floats_become_int(self):
        self.assertEqual(parse_report_amount("100.0"), 100)
        self.assertIsInstance(parse_report_amount("100.0"), int)

    def test_no_number_returns_none(self):
        self.assertIsNone(parse_report_amount("今天没有数字"))
        self.assertIsNone(parse_report_amount(""))
        self.assertIsNone(parse_report_amount(None))


class ReplyTextTests(unittest.TestCase):
    """与 listener.py 的文案逐字对拍。"""

    def test_gate_reply(self):
        self.assertEqual(
            build_not_member_reply(),
            "⛔ 报数功能仅限销售日报责任人使用。\n"
            "如需填写日报请联系管理员，或在表格中直接填写。",
        )

    def test_rest_day_reply(self):
        self.assertEqual(build_not_workday_reply(), "今天不是销售日报工作日，无需报数～")

    def test_format_hint(self):
        self.assertEqual(
            build_format_hint("张三"),
            "张三 你好～报数格式：@提醒事项 数字\n"
            "例如：@提醒事项 12800（当天无销量报 0）\n"
            "查看全部功能：/帮助 ｜ 按钮菜单：/菜单",
        )

    def test_format_hint_personalized_for_store_member(self):
        self.assertEqual(
            build_format_hint("王城", store="万科体验馆"),
            "王城 你好～报数格式：@提醒事项 数字\n"
            "例如：@提醒事项 12800（当天无销量报 0）\n"
            "查看全部功能：/帮助 ｜ 按钮菜单：/菜单\n"
            "你的门店是万科体验馆，也可以这样报：万科体验馆 零售 7560，"
            "或 万科体验馆 团购 1200",
        )

    def test_format_hint_personalized_for_root_dept(self):
        reply = build_format_hint(
            "王城", stores=["万科体验馆", "莲荷里体验馆"]
        )
        self.assertIn("多门店报数示例：万科体验馆 零售 7560；莲荷里体验馆 零售 7560", reply)

    def test_multi_recorded_reply_shows_recognition_note(self):
        reply = build_multi_recorded_reply(
            month=9, day=23, weekday="二",
            writes=[("莲荷里体验馆·零售", 500, None, None, "（识别：莲荷）")],
        )
        self.assertIn("莲荷里体验馆·零售：500（识别：莲荷）", reply)

    def test_multi_recorded_reply_accepts_legacy_4_tuple(self):
        reply = build_multi_recorded_reply(
            month=9, day=23, weekday="二",
            writes=[("万科体验馆·零售", 100, None, None)],
        )
        self.assertIn("万科体验馆·零售：100", reply)


class RegionRoutingTests(unittest.TestCase):

    def test_routes_by_open_conversation_id(self):
        configs = {"hangzhou": _cfg()}
        self.assertIs(
            region_for_conversation(configs, "conv-hz"), configs["hangzhou"]
        )

    def test_unknown_conversation_returns_none(self):
        self.assertIsNone(region_for_conversation({"hangzhou": _cfg()}, "conv-x"))
        self.assertIsNone(region_for_conversation({"hangzhou": _cfg()}, ""))


class HandleReportTests(unittest.TestCase):

    def test_rest_day_gets_the_rest_reply(self):
        conn = _Conn(workdays={date(2026, 9, 10)})
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="12800", sender_uid="u1", now=_NOW,
        )
        self.assertEqual(outcome.status, "not_workday")
        self.assertEqual(outcome.reply, "今天不是销售日报工作日，无需报数～")

    def test_missing_calendar_fails_loudly(self):
        conn = _Conn(workdays=set())
        with self.assertRaisesRegex(MartTaskError, "2026-09"):
            handle_report(
                conn, region_cfg=_cfg(), text="1", sender_uid="u1", now=_NOW,
            )

    def test_unknown_sender_is_rejected(self):
        conn = _Conn(members=[])
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="12800", sender_uid="u9", now=_NOW,
        )
        self.assertEqual(outcome.status, "not_member")
        self.assertTrue(outcome.reply.startswith("⛔"))

    def test_member_of_another_region_is_rejected(self):
        conn = _Conn(members=[_member(region="shaoxing")])
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="12800", sender_uid="u1", now=_NOW,
        )
        self.assertEqual(outcome.status, "not_member")

    def test_missing_number_gets_the_format_hint(self):
        conn = _Conn(members=[_member()])
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="今天没数", sender_uid="u1", now=_NOW,
        )
        self.assertEqual(outcome.status, "no_number")
        self.assertEqual(outcome.reply, build_format_hint("张三"))

    def test_new_report_inserts_a_stream_row(self):
        conn = _Conn(members=[_member()])
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="12800", sender_uid="u1", now=_NOW,
        )

        self.assertEqual(outcome.status, "recorded")
        self.assertEqual(outcome.value, 12800)
        self.assertFalse(outcome.overwritten)

        insert_sql, params = next(
            (s, p) for s, p in conn.executed if s.lstrip().startswith("INSERT")
        )
        self.assertIn("fact_daily_report_offline", insert_sql)
        self.assertEqual(params[0], "stream:hangzhou:u1:2026-09-11")
        self.assertEqual(params[1:6],
                         ("hangzhou", "张三", "杭中", _DAY, 12800))
        self.assertIsNone(params[6])  # monthly_target（有表区域由 AI 表行携带）
        self.assertEqual(params[8], STREAM_RUN_ID)

        self.assertIn("✅ 已记录 9月11日（周五）销量：12800", outcome.reply)
        self.assertNotIn("🔁", outcome.reply)
        self.assertTrue(outcome.reply.endswith("祝您下班愉快 🎉"))

    def test_report_writes_the_table_name_via_aliases(self):
        conn = _Conn(members=[_member(name="张三丰")])
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="100", sender_uid="u1", now=_NOW,
        )
        _, params = next(
            (s, p) for s, p in conn.executed if s.lstrip().startswith("INSERT")
        )
        self.assertEqual(params[2], "老张")
        self.assertEqual(outcome.status, "recorded")

    def test_insert_snapshots_the_configured_monthly_target(self):
        """无 AI 表区域：月目标由 region 配置快照进新事实行。"""
        cfg = RegionConfig(
            region="vanke", display="万科&大莲花&团购",
            table_url="https://example.com/board",
            robot_code="rc", open_conversation_id="conv-vk",
            aliases={}, cc_user_ids=(),
            monthly_targets={"张三": 300000},
        )
        conn = _Conn(members=[_member(region="vanke", dept="体验中心")])
        outcome = handle_report(
            conn, region_cfg=cfg, text="12800", sender_uid="u1", now=_NOW,
        )
        self.assertEqual(outcome.status, "recorded")
        insert_sql, params = next(
            (s, p) for s, p in conn.executed if s.lstrip().startswith("INSERT")
        )
        self.assertIn("`monthly_target`", insert_sql)
        self.assertEqual(params[1:3], ("vanke", "张三"))
        self.assertEqual(params[6], 300000)

    def test_update_path_preserves_the_existing_target(self):
        """业务键原地更新只动 sales_amount，monthly_target 列不被覆盖。"""
        cfg = RegionConfig(
            region="vanke", display="万科&大莲花&团购",
            table_url="https://example.com/board",
            robot_code="rc", open_conversation_id="conv-vk",
            aliases={}, cc_user_ids=(),
            monthly_targets={"张三": 300000},
        )
        conn = _Conn(
            members=[_member(region="vanke", dept="体验中心")],
            existing={"source_record_id": "stream:vanke:u1:2026-09-11",
                      "sales_amount": Decimal("100")},
        )
        outcome = handle_report(
            conn, region_cfg=cfg, text="200", sender_uid="u1", now=_NOW,
        )
        self.assertTrue(outcome.overwritten)
        update_sql, params = next(
            (s, p) for s, p in conn.executed if s.lstrip().startswith("UPDATE")
        )
        self.assertNotIn("monthly_target", update_sql)
        self.assertEqual(params[0], 200)

    def test_overwrite_updates_the_existing_row_and_notes_it(self):
        conn = _Conn(
            members=[_member()],
            existing={
                "source_record_id": "melt-r1#11",
                "sales_amount": Decimal("100.0000"),
            },
        )
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="200", sender_uid="u1", now=_NOW,
        )

        self.assertTrue(outcome.overwritten)
        update_sql, params = next(
            (s, p) for s, p in conn.executed if s.lstrip().startswith("UPDATE")
        )
        self.assertIn("SET `sales_amount` = %s", update_sql)
        self.assertEqual(params[0], 200)
        self.assertEqual(params[2], "melt-r1#11")
        # 业务键更新：不产生新的 stream 行。
        self.assertFalse(any(
            s.lstrip().startswith("INSERT") for s, _ in conn.executed
        ))
        self.assertIn("🔁 已覆盖你之前填报的 100", outcome.reply)

    def test_same_value_rewrite_has_no_overwrite_note(self):
        conn = _Conn(
            members=[_member()],
            existing={"source_record_id": "r1", "sales_amount": Decimal("200")},
        )
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="200", sender_uid="u1", now=_NOW,
        )
        self.assertFalse(outcome.overwritten)
        self.assertNotIn("🔁", outcome.reply)

    def test_progress_line_uses_the_shared_metrics(self):
        conn = _Conn(
            members=[_member()],
            existing={"source_record_id": "melt#11",
                      "sales_amount": Decimal("200")},
            facts=[
                {"responsible_person": "张三", "department": "杭中",
                 "business_date": date(2026, 9, 1), "sales_amount": 100,
                 "monthly_target": 3000},
                {"responsible_person": "张三", "department": "杭中",
                 "business_date": date(2026, 9, 11), "sales_amount": 200,
                 "monthly_target": 3000},
            ],
        )
        # 9/1 已填 100；今日报 200（与同值，无覆盖提示）→ 累计 300/3000。
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="200", sender_uid="u1", now=_NOW,
        )
        self.assertIn("📊 本月累计 300 / 目标 3000，完成 10.0%", outcome.reply)

    def test_progress_line_is_omitted_without_a_target(self):
        conn = _Conn(
            members=[_member()],
            existing={"source_record_id": "s1",
                      "sales_amount": Decimal("200")},
            facts=[{
                "responsible_person": "张三", "department": "杭中",
                "business_date": date(2026, 9, 11), "sales_amount": 200,
                "monthly_target": None,
            }],
        )
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="200", sender_uid="u1", now=_NOW,
        )
        self.assertNotIn("📊", outcome.reply)


def _vanke_cfg(monthly_targets=None):
    return RegionConfig(
        region="vanke", display="万科&大莲花&团购",
        table_url="", robot_code="rc", open_conversation_id="conv-vk",
        aliases={}, cc_user_ids=(),
        dept_order=("体验中心",),
        monthly_targets=monthly_targets or {},
    )


class ParseMetricsTests(unittest.TestCase):

    def test_store_colon_metric_pairs(self):
        self.assertEqual(
            parse_report_metrics("万科体验馆：零售 0，团购 0"),
            [("万科体验馆", "零售", 0, None), ("万科体验馆", "团购", 0, None)],
        )

    def test_store_slash_number_defaults_to_retail(self):
        self.assertEqual(
            parse_report_metrics("万科体验馆/7560 团购/0"),
            [("万科体验馆", "零售", 7560, None), ("万科体验馆", "团购", 0, None)],
        )

    def test_metric_only_has_no_store(self):
        self.assertEqual(
            parse_report_metrics("零售7560 团购0"),
            [(None, "零售", 7560, None), (None, "团购", 0, None)],
        )
        self.assertEqual(parse_report_metrics("团购/134"), [(None, "团购", 134, None)])

    def test_bare_number_before_metric_defaults_to_retail(self):
        self.assertEqual(
            parse_report_metrics("3060。团购/816"),
            [(None, "零售", 3060, None), (None, "团购", 816, None)],
        )

    def test_store_aliases_map_to_directory_names(self):
        for spoken in ("酱酒体验馆/400", "酱香体验馆/400", "大莲花/400", "莲荷里/400"):
            self.assertEqual(
                parse_report_metrics(spoken),
                [("莲荷里体验馆", "零售", 400, None)],
                spoken,
            )

    def test_fuzzy_store_prefix_autocompletes_with_echo(self):
        self.assertEqual(
            parse_report_metrics("莲荷 500"),
            [("莲荷里体验馆", "零售", 500, "莲荷")],
        )
        self.assertEqual(
            parse_report_metrics("莲荷 100 团200"),
            [("莲荷里体验馆", "零售", 100, "莲荷"),
             ("莲荷里体验馆", "团购", 200, "莲荷")],
        )

    def test_fuzzy_store_unmatched_word_is_noise(self):
        # 「体验馆」不是任何候选的前缀（是后缀）→ 不补全，走单金额旧路径
        self.assertIsNone(parse_report_metrics("体验馆 500"))
        # 词长 <2 不补全
        self.assertIsNone(parse_report_metrics("酱 500"))

    def test_single_char_metric_tolerance_with_guard(self):
        self.assertEqual(
            parse_report_metrics("万科 团100"),
            [("万科体验馆", "团购", 100, None)],
        )
        self.assertEqual(
            parse_report_metrics("万科 零100"),
            [("万科体验馆", "零售", 100, None)],
        )
        # 单字后非数字 → 不成指标（防「零食」「团队」误判）
        self.assertIsNone(parse_report_metrics("零食100"))
        self.assertIsNone(parse_report_metrics("团队100"))

    def test_no_label_returns_none_for_legacy_path(self):
        self.assertIsNone(parse_report_metrics("765"))
        self.assertIsNone(parse_report_metrics("12800"))
        self.assertIsNone(parse_report_metrics(""))
        self.assertIsNone(parse_report_metrics(None))

    def test_grouped_amounts(self):
        self.assertEqual(
            parse_report_metrics("零售 12,800"),
            [(None, "零售", 12800, None)],
        )


class MultiMetricIntakeTests(unittest.TestCase):

    @staticmethod
    def _inserts(conn):
        return [p for sql, p in conn.executed if sql.lstrip().startswith("INSERT")]

    def test_store_member_writes_one_row_per_metric(self):
        conn = _Conn(members=[_member(user_id="u2", name="张燕芳",
                                      region="vanke", dept="万科体验馆")])
        cfg = _vanke_cfg({"万科体验馆·零售": 100000, "万科体验馆·团购": 20000})
        outcome = handle_report(
            conn, region_cfg=cfg, text="零售7560 团购0", sender_uid="u2", now=_NOW,
        )
        self.assertEqual(outcome.status, "recorded")
        inserts = self._inserts(conn)
        self.assertEqual(len(inserts), 2)
        self.assertEqual(inserts[0][0], "stream:vanke:u2:2026-09-11:零售")
        self.assertEqual(inserts[1][0], "stream:vanke:u2:2026-09-11:团购")
        # responsible_person = 数据格；monthly_target 快照
        self.assertEqual(inserts[0][2], "万科体验馆·零售")
        self.assertEqual(inserts[0][6], 100000)
        self.assertEqual(inserts[1][2], "万科体验馆·团购")
        self.assertEqual(inserts[1][6], 20000)
        self.assertEqual(inserts[0][8], STREAM_RUN_ID)
        self.assertIn("万科体验馆·零售：7560", outcome.reply)
        self.assertIn("万科体验馆·团购：0", outcome.reply)

    def test_store_name_in_message_scopes_following_metrics(self):
        conn = _Conn(members=[_member(user_id="u2", name="张燕芳",
                                      region="vanke", dept="万科体验馆")])
        outcome = handle_report(
            conn, region_cfg=_vanke_cfg(),
            text="万科体验馆/3060。团购/816", sender_uid="u2", now=_NOW,
        )
        self.assertEqual(outcome.status, "recorded")
        inserts = self._inserts(conn)
        self.assertEqual(
            [(p[0], p[4], p[5]) for p in inserts],
            [("stream:vanke:u2:2026-09-11:零售", date(2026, 9, 11), 3060),
             ("stream:vanke:u2:2026-09-11:团购", date(2026, 9, 11), 816)],
        )

    def test_alias_store_maps_to_directory_dept(self):
        conn = _Conn(members=[_member(user_id="u3", name="林燕山",
                                      region="vanke", dept="莲荷里体验馆")])
        outcome = handle_report(
            conn, region_cfg=_vanke_cfg(),
            text="酱酒体验馆/1526  团购/0", sender_uid="u3", now=_NOW,
        )
        self.assertEqual(outcome.status, "recorded")
        cells = [p[2] for p in self._inserts(conn)]
        self.assertEqual(cells, ["莲荷里体验馆·零售", "莲荷里体验馆·团购"])

    def test_other_stores_report_is_denied(self):
        conn = _Conn(members=[_member(user_id="u2", name="张燕芳",
                                      region="vanke", dept="万科体验馆")])
        outcome = handle_report(
            conn, region_cfg=_vanke_cfg(),
            text="莲荷里体验馆/400", sender_uid="u2", now=_NOW,
        )
        self.assertEqual(outcome.status, "not_member")
        self.assertIn("不能报莲荷里体验馆", outcome.reply)
        self.assertEqual(self._inserts(conn), [])

    def test_root_dept_member_may_report_any_store(self):
        conn = _Conn(members=[_member(user_id="u4", name="吴金澎",
                                      region="vanke", dept="体验中心")])
        outcome = handle_report(
            conn, region_cfg=_vanke_cfg(),
            text="万科体验馆：零售 100，团购 50", sender_uid="u4", now=_NOW,
        )
        self.assertEqual(outcome.status, "recorded")
        cells = [p[2] for p in self._inserts(conn)]
        self.assertEqual(cells, ["万科体验馆·零售", "万科体验馆·团购"])

    def test_overwrite_hint_per_cell(self):
        conn = _Conn(
            members=[_member(user_id="u2", name="张燕芳",
                              region="vanke", dept="万科体验馆")],
            existing={"source_record_id": "s1", "sales_amount": Decimal("100")},
        )
        outcome = handle_report(
            conn, region_cfg=_vanke_cfg(), text="团购200", sender_uid="u2", now=_NOW,
        )
        self.assertEqual(outcome.status, "recorded")
        self.assertTrue(outcome.overwritten)
        self.assertIn("覆盖旧值", outcome.reply)

    def test_legacy_single_amount_still_uses_person_cell(self):
        conn = _Conn(members=[_member(region="vanke", dept="万科体验馆")])
        outcome = handle_report(
            conn, region_cfg=_vanke_cfg(), text="765", sender_uid="u1", now=_NOW,
        )
        self.assertEqual(outcome.status, "recorded")
        inserts = self._inserts(conn)
        self.assertEqual(inserts[0][0], "stream:vanke:u1:2026-09-11")
        self.assertEqual(inserts[0][2], "张三")


class AdminGateTests(unittest.TestCase):
    """admin grant 例外：跨区报数放行（运维测试/代录），其余门禁不变。"""

    def test_admin_may_report_across_regions(self):
        conn = _Conn(
            members=[_member(user_id="u-admin", name="王城", region="hq", dept="总经办")],
            admin=True,
        )
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="100", sender_uid="u-admin", now=_NOW,
        )
        self.assertEqual(outcome.status, "recorded")
        inserts = [p for sql, p in conn.executed if sql.lstrip().startswith("INSERT")]
        self.assertEqual(inserts[0][0], "stream:hangzhou:u-admin:2026-09-11")

    def test_non_admin_region_mismatch_is_still_rejected(self):
        conn = _Conn(
            members=[_member(user_id="u-hq", name="路人", region="hq", dept="总经办")],
            admin=False,
        )
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="100", sender_uid="u-hq", now=_NOW,
        )
        self.assertEqual(outcome.status, "not_member")

    def test_admin_without_member_record_is_rejected(self):
        conn = _Conn(members=[], admin=True)
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="100", sender_uid="u-ghost", now=_NOW,
        )
        self.assertEqual(outcome.status, "not_member")


class AuxParseTests(unittest.TestCase):

    def test_non_slash_is_not_aux(self):
        self.assertIsNone(parse_aux_command("12800"))
        self.assertIsNone(parse_aux_command("零售 100"))
        self.assertIsNone(parse_aux_command(None))

    def test_query_commands(self):
        self.assertEqual(parse_aux_command("/帮助"), ("帮助", None))
        self.assertEqual(parse_aux_command("/未填"), ("未填", None))
        self.assertEqual(parse_aux_command("/我的"), ("我的", None))
        self.assertEqual(parse_aux_command("/门店"), ("门店", None))
        # 未识别的 / 指令落帮助菜单，不进报数路径
        self.assertEqual(parse_aux_command("/随便 100"), ("帮助", None))

    def test_at_mention_prefix_is_tolerated(self):
        # 钉钉投递文本可能保留 @机器人 前缀
        self.assertEqual(parse_aux_command("@提醒事项 /帮助"), ("帮助", None))
        command, match = parse_aux_command("@提醒事项 /补签 张三 12800")
        self.assertEqual(command, "补签")
        self.assertEqual(match.group("name"), "张三")

    def test_zero_width_and_joined_at_mention_are_tolerated(self):
        # 客户端插入零宽字符（​）或与指令连写都不得失效
        self.assertEqual(parse_aux_command("@提醒事项​/帮助"), ("帮助", None))
        self.assertEqual(parse_aux_command("@提醒事项 /帮助"), ("帮助", None))
        self.assertEqual(parse_aux_command("​/未填"), ("未填", None))

    def test_backfill_args(self):
        command, match = parse_aux_command("/补签 张三 12800")
        self.assertEqual(command, "补签")
        self.assertEqual(match.group("name"), "张三")
        self.assertIsNone(match.group("day_token"))
        self.assertEqual(match.group("body"), "12800")
        # 金额不被误切出日期段
        _, match = parse_aux_command("/补签 张三 9-20 12,800")
        self.assertEqual(match.group("day_token"), "9-20")
        self.assertEqual(match.group("body"), "12,800")
        # 参数不齐 → None（走用法提示）
        self.assertEqual(parse_aux_command("/补签 张三"), ("补签", None))

    def test_backfill_date_parsing(self):
        today = date(2026, 9, 23)
        for token in ("9-20", "9/20", "9月20", "9月20日", "0920"):
            self.assertEqual(_parse_backfill_date(token, today),
                             date(2026, 9, 20), token)
        self.assertEqual(_parse_backfill_date(None, today), today)
        # 恰好 30 天边界
        self.assertEqual(_parse_backfill_date("8-24", today), date(2026, 8, 24))
        self.assertIsNone(_parse_backfill_date("8-23", today))
        # 未来日期 → 落到去年 → 超 30 天拒绝
        self.assertIsNone(_parse_backfill_date("9-24", today))
        self.assertIsNone(_parse_backfill_date("12-31", today))
        self.assertIsNone(_parse_backfill_date("13-01", today))


class AuxCommandTests(unittest.TestCase):

    @staticmethod
    def _inserts(conn):
        return [p for sql, p in conn.executed if sql.lstrip().startswith("INSERT")]

    def test_help_lists_all_commands(self):
        conn = _Conn(members=[_member()])
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="/帮助", sender_uid="u1", now=_NOW,
        )
        self.assertEqual(outcome.status, "aux")
        for keyword in ("/未填", "/我的", "/门店", "/补签", "报数格式"):
            self.assertIn(keyword, outcome.reply)

    def test_help_personalized_for_store_member(self):
        conn = _Conn(members=[_member(user_id="u2", name="张燕芳",
                                      region="vanke", dept="万科体验馆")])
        outcome = handle_report(
            conn, region_cfg=_vanke_cfg(), text="/帮助", sender_uid="u2",
            now=_NOW,
        )
        self.assertIn("你的门店是万科体验馆", outcome.reply)

    def test_unfilled_member_based(self):
        conn = _Conn(
            members=[_member(user_id="u1", name="张三丰"),
                     _member(user_id="u4", name="李四")],
            facts=[{"responsible_person": "老张", "department": "杭中",
                    "business_date": date(2026, 9, 11), "sales_amount": 100,
                    "monthly_target": None}],
        )
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="/未填", sender_uid="u1", now=_NOW,
        )
        self.assertEqual(outcome.status, "aux")
        self.assertIn("李四", outcome.reply)
        self.assertNotIn("张三丰", outcome.reply)

    def test_unfilled_cell_based_for_store_region(self):
        conn = _Conn(
            members=[_member(user_id="u2", name="张燕芳", region="vanke",
                             dept="万科体验馆")],
            facts=[{"responsible_person": "万科体验馆·零售",
                    "department": "万科体验馆",
                    "business_date": date(2026, 9, 11), "sales_amount": 100,
                    "monthly_target": None}],
        )
        outcome = handle_report(
            conn, region_cfg=_vanke_cfg(), text="/未填", sender_uid="u2",
            now=_NOW,
        )
        self.assertEqual(outcome.status, "aux")
        self.assertIn("万科体验馆·团购", outcome.reply)
        self.assertNotIn("万科体验馆·零售、", outcome.reply)

    def test_mine_single_table_region(self):
        conn = _Conn(
            members=[_member(name="张三丰")],
            facts=[{"responsible_person": "老张", "department": "杭中",
                    "business_date": date(2026, 9, 10), "sales_amount": 500,
                    "monthly_target": 1000}],
        )
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="/我的", sender_uid="u1", now=_NOW,
        )
        self.assertEqual(outcome.status, "aux")
        self.assertIn("📊", outcome.reply)
        self.assertIn("老张", outcome.reply)

    def test_mine_without_data(self):
        conn = _Conn(members=[_member()])
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="/我的", sender_uid="u1", now=_NOW,
        )
        self.assertIn("暂无数据或无目标", outcome.reply)

    def test_stores_only_for_multi_metric_region(self):
        conn = _Conn(members=[_member()])
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="/门店", sender_uid="u1", now=_NOW,
        )
        self.assertIn("暂无多门店板块", outcome.reply)

    def test_stores_admin_cross_region_view(self):
        # 管理员在本区域无多门店数据时，跨区列出有数据的区域
        conn = _Conn(
            members=[_member(user_id="u1", name="王城")],
            facts=[{"region": "vanke",
                    "responsible_person": "万科体验馆·零售",
                    "department": "万科体验馆",
                    "business_date": date(2026, 9, 10), "sales_amount": 500,
                    "monthly_target": 1000}],
            admin=True,
        )
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="/门店", sender_uid="u1", now=_NOW,
            all_region_cfgs=[_cfg(), _vanke_cfg()],
        )
        self.assertEqual(outcome.status, "aux")
        self.assertIn("管理员跨区视图", outcome.reply)
        self.assertIn("万科体验馆·零售", outcome.reply)

    def test_stores_non_admin_no_cross_region(self):
        conn = _Conn(
            members=[_member()],
            facts=[{"region": "vanke",
                    "responsible_person": "万科体验馆·零售",
                    "department": "万科体验馆",
                    "business_date": date(2026, 9, 10), "sales_amount": 500,
                    "monthly_target": 1000}],
        )
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="/门店", sender_uid="u1", now=_NOW,
            all_region_cfgs=[_cfg(), _vanke_cfg()],
        )
        self.assertIn("暂无多门店板块", outcome.reply)
        self.assertNotIn("万科体验馆", outcome.reply)

    def test_backfill_requires_admin(self):
        conn = _Conn(members=[_member()])  # admin=False
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="/补签 张三丰 12800",
            sender_uid="u1", now=_NOW,
        )
        self.assertEqual(outcome.status, "not_member")
        self.assertEqual(self._inserts(conn), [])

    def test_backfill_writes_as_target_member(self):
        conn = _Conn(
            members=[_member(user_id="u1", name="王城"),
                     _member(user_id="u9", name="张三丰")],
            admin=True,
        )
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="/补签 张三丰 9-10 12800",
            sender_uid="u1", now=_NOW,
        )
        self.assertEqual(outcome.status, "recorded")
        inserts = self._inserts(conn)
        self.assertEqual(len(inserts), 1)
        # 署名=目标成员（业务键与本人自报一致）
        self.assertEqual(inserts[0][0], "stream:hangzhou:u9:2026-09-10")
        self.assertEqual(inserts[0][2], "老张")  # 经 aliases 映射表内用名
        self.assertEqual(inserts[0][4], date(2026, 9, 10))
        self.assertEqual(inserts[0][5], 12800)
        self.assertIn("已代录", outcome.reply)
        self.assertIn("王城", outcome.reply)
        self.assertIn("张三丰", outcome.reply)

    def test_backfill_defaults_to_today(self):
        conn = _Conn(
            members=[_member(user_id="u1", name="王城"),
                     _member(user_id="u9", name="张三丰")],
            admin=True,
        )
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="/补签 张三丰 100",
            sender_uid="u1", now=_NOW,
        )
        self.assertEqual(outcome.status, "recorded")
        self.assertEqual(self._inserts(conn)[0][4], date(2026, 9, 11))

    def test_backfill_rejects_beyond_30_days(self):
        conn = _Conn(
            members=[_member(user_id="u1", name="王城"),
                     _member(user_id="u9", name="张三丰")],
            admin=True,
        )
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="/补签 张三丰 8-10 12800",
            sender_uid="u1", now=_NOW,
        )
        self.assertEqual(outcome.status, "aux")
        self.assertIn("超出范围", outcome.reply)
        self.assertEqual(self._inserts(conn), [])

    def test_backfill_rejects_non_workday(self):
        conn = _Conn(
            members=[_member(user_id="u1", name="王城"),
                     _member(user_id="u9", name="张三丰")],
            admin=True,
        )
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="/补签 张三丰 9-6 12800",
            sender_uid="u1", now=_NOW,
        )
        self.assertIn("不是工作日", outcome.reply)
        self.assertEqual(self._inserts(conn), [])

    def test_backfill_rejects_unknown_member(self):
        conn = _Conn(members=[_member(user_id="u1", name="王城")], admin=True)
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="/补签 不存在 100",
            sender_uid="u1", now=_NOW,
        )
        self.assertIn("未找到成员", outcome.reply)
        self.assertEqual(self._inserts(conn), [])

    def test_backfill_overwrite_shows_old_value(self):
        conn = _Conn(
            members=[_member(user_id="u1", name="王城"),
                     _member(user_id="u9", name="张三丰")],
            existing={"source_record_id": "stream:hangzhou:u9:2026-09-11",
                      "sales_amount": 300},
            admin=True,
        )
        outcome = handle_report(
            conn, region_cfg=_cfg(), text="/补签 张三丰 500",
            sender_uid="u1", now=_NOW,
        )
        self.assertTrue(outcome.overwritten)
        self.assertIn("🔁 覆盖旧值 300", outcome.reply)

    def test_backfill_multi_metric_body(self):
        conn = _Conn(
            members=[_member(user_id="u1", name="王城", region="vanke",
                             dept="体验中心"),
                     _member(user_id="u8", name="林燕山", region="vanke",
                             dept="莲荷里体验馆")],
            admin=True,
        )
        outcome = handle_report(
            conn, region_cfg=_vanke_cfg(),
            text="/补签 林燕山 莲荷 零售 500", sender_uid="u1", now=_NOW,
        )
        self.assertEqual(outcome.status, "recorded")
        inserts = self._inserts(conn)
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][0],
                         "stream:vanke:u8:2026-09-11:零售")
        self.assertEqual(inserts[0][2], "莲荷里体验馆·零售")


if __name__ == "__main__":
    unittest.main()
