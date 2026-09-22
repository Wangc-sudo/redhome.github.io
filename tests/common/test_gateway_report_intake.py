"""Tests for the Stream report intake (parse -> gate -> mart write -> reply)."""

import unittest
from datetime import date, datetime
from decimal import Decimal

from common.gateway.report_intake import (
    STREAM_RUN_ID,
    build_format_hint,
    build_not_member_reply,
    build_not_workday_reply,
    handle_report,
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
            # 月事实查询
            self._rows = self._conn.fact_rows
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
            "例如：@提醒事项 12800（当天无销量报 0）",
        )


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
            [("万科体验馆", "零售", 0), ("万科体验馆", "团购", 0)],
        )

    def test_store_slash_number_defaults_to_retail(self):
        self.assertEqual(
            parse_report_metrics("万科体验馆/7560 团购/0"),
            [("万科体验馆", "零售", 7560), ("万科体验馆", "团购", 0)],
        )

    def test_metric_only_has_no_store(self):
        self.assertEqual(
            parse_report_metrics("零售7560 团购0"),
            [(None, "零售", 7560), (None, "团购", 0)],
        )
        self.assertEqual(parse_report_metrics("团购/134"), [(None, "团购", 134)])

    def test_bare_number_before_metric_defaults_to_retail(self):
        self.assertEqual(
            parse_report_metrics("3060。团购/816"),
            [(None, "零售", 3060), (None, "团购", 816)],
        )

    def test_store_aliases_map_to_directory_names(self):
        for spoken in ("酱酒体验馆/400", "酱香体验馆/400", "大莲花/400", "莲荷里/400"):
            self.assertEqual(
                parse_report_metrics(spoken),
                [("莲荷里体验馆", "零售", 400)],
                spoken,
            )

    def test_no_label_returns_none_for_legacy_path(self):
        self.assertIsNone(parse_report_metrics("765"))
        self.assertIsNone(parse_report_metrics("12800"))
        self.assertIsNone(parse_report_metrics(""))
        self.assertIsNone(parse_report_metrics(None))

    def test_grouped_amounts(self):
        self.assertEqual(
            parse_report_metrics("零售 12,800"),
            [(None, "零售", 12800)],
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


if __name__ == "__main__":
    unittest.main()
