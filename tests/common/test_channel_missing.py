"""渠道门店到齐校验与催办（channel_missing）测试。"""

import unittest
from datetime import date, datetime
from unittest.mock import Mock

from common.daily_robot.channel_missing import (
    MISSING,
    OK,
    ZERO,
    build_missing_message,
    channel_states,
    expected_stores,
    month_unfilled,
    owners_for,
    parse_owner_entries,
    resolve_at_user_ids,
    run_channel_missing,
    store_states,
)
from common.region_config import _parse_region


_BD = date(2026, 9, 27)
_NOW = datetime(2026, 9, 28, 10, 40)  # 北京 naive


# ---------------------------------------------------------------------------
# 纯逻辑：负责人集合解析
# ---------------------------------------------------------------------------

class ParseOwnerEntriesTests(unittest.TestCase):

    def test_json_array_keeps_union_id(self):
        entries = parse_owner_entries(
            '[{"name": "张三", "unionId": "u-1"}, {"name": "李四"}]'
        )
        self.assertEqual(entries, [
            {"name": "张三", "union_id": "u-1"},
            {"name": "李四", "union_id": None},
        ])

    def test_null_and_empty(self):
        self.assertEqual(parse_owner_entries(None), [])
        self.assertEqual(parse_owner_entries("null"), [])
        self.assertEqual(parse_owner_entries("[]"), [])
        self.assertEqual(parse_owner_entries("  "), [])

    def test_comma_fallback_and_dedupe(self):
        entries = parse_owner_entries("张三, 李四,张三")
        self.assertEqual(entries, [
            {"name": "张三", "union_id": None},
            {"name": "李四", "union_id": None},
        ])


# ---------------------------------------------------------------------------
# 纯逻辑：点名册 / 状态判定
# ---------------------------------------------------------------------------

class ExpectedStoresTests(unittest.TestCase):

    def test_union_of_target_and_recent_minus_exclude(self):
        roster = expected_stores(
            [
                {"store_name": "天猫旗舰", "channel": "天猫"},
                {"store_name": "MC猫超", "channel": "猫超"},
            ],
            [
                {"store_name": "京东POP", "channel": "京东"},
                {"store_name": "天猫旗舰", "channel": "天猫"},
            ],
            exclude=("京东POP",),
        )
        self.assertEqual(
            roster, {"天猫旗舰": "天猫", "MC猫超": "猫超"}
        )


class StoreStatesTests(unittest.TestCase):

    def test_four_way_classification(self):
        states = store_states([
            {"store_name": "有销售额", "sales_amount": "123.45"},
            {"store_name": "填了零", "sales_amount": 0},
            {"store_name": "空值", "sales_amount": None},
        ])
        self.assertEqual(states["有销售额"], OK)
        self.assertEqual(states["填了零"], ZERO)
        self.assertEqual(states["空值"], MISSING)
        # 无行 → 不在 states 里，调用方按 MISSING 处理
        self.assertNotIn("没填", states)

    def test_same_store_takes_best_state(self):
        states = store_states([
            {"store_name": "店", "sales_amount": 0},
            {"store_name": "店", "sales_amount": 5},
        ])
        self.assertEqual(states["店"], OK)


class ChannelStatesTests(unittest.TestCase):

    def test_storeless_rows_rollup_by_channel(self):
        states = channel_states([
            {"store_name": "", "channel": "猫超", "sales_amount": 100},
            {"store_name": "天猫旗舰", "channel": "天猫", "sales_amount": 0},
            {"store_name": None, "channel": "即时零售", "sales_amount": None},
        ])
        self.assertEqual(states, {"猫超": OK, "即时零售": MISSING})


class OwnersForTests(unittest.TestCase):

    def test_detail_row_wins(self):
        entries = owners_for(
            "天猫旗舰", "天猫",
            daily_rows=[{
                "store_name": "天猫旗舰",
                "responsible_person": '[{"name": "张三", "unionId": "u-1"}]',
            }],
            target_meta={"天猫旗舰": {"owners_json": '[{"name": "李四"}]'}},
            channel_owners={"天猫": ["王五"]},
        )
        self.assertEqual(entries, [{"name": "张三", "union_id": "u-1"}])

    def test_store_target_fallback(self):
        entries = owners_for(
            "天猫旗舰", "天猫",
            daily_rows=[],
            target_meta={"天猫旗舰": {"owners_json": '[{"name": "李四"}]'}},
            channel_owners={"天猫": ["王五"]},
        )
        self.assertEqual(entries, [{"name": "李四", "union_id": None}])

    def test_channel_level_fallback(self):
        entries = owners_for(
            "MC猫超", "猫超",
            daily_rows=[],
            target_meta={},
            channel_owners={"猫超": ["王五"]},
        )
        self.assertEqual(entries, [{"name": "王五", "union_id": None}])


class ResolveAtUserIdsTests(unittest.TestCase):

    def test_three_levels_and_ambiguity(self):
        resolved, unmatched = resolve_at_user_ids(
            [
                {"name": "有union", "union_id": "u-1"},
                {"name": "唯一姓名", "union_id": None},
                {"name": "歧义名", "union_id": None},
                {"name": "查无此人", "union_id": "u-9"},
            ],
            union_map={"u-1": "staff-1"},
            name_map={
                "唯一姓名": ["staff-2"],
                "歧义名": ["staff-3", "staff-4"],
                # unionId 未命中时不回落同 unionId，走姓名级
                "查无此人": [],
            },
        )
        self.assertEqual(resolved, [("staff-1", "有union"), ("staff-2", "唯一姓名")])
        self.assertEqual(unmatched, ["歧义名", "查无此人"])

    def test_dedupe_by_user_id(self):
        resolved, _ = resolve_at_user_ids(
            [
                {"name": "张三", "union_id": "u-1"},
                {"name": "张三", "union_id": "u-1"},
            ],
            union_map={"u-1": "staff-1"},
            name_map={},
        )
        self.assertEqual(resolved, [("staff-1", "张三")])


class MonthUnfilledTests(unittest.TestCase):

    def test_zero_stores_and_owner_aggregation(self):
        roster = {"天猫旗舰": "天猫", "京东POP": "京东", "拼多多店": "拼多多"}
        target_meta = {
            "天猫旗舰": {"owners_json": '[{"name": "张三"}]'},
            "京东POP": {"owners_json": '[{"name": "李四"}]'},
            "拼多多店": {"owners_json": '[{"name": "李四"}]'},
        }
        # 李四名下两店一店有填报 → 不算零填报员工；张三全月零 → 算
        zero_stores, zero_owners = month_unfilled(
            roster, target_meta, {"京东POP": 3}
        )
        self.assertEqual(
            [s["store"] for s in zero_stores], ["天猫旗舰", "拼多多店"]
        )
        self.assertEqual(
            zero_owners, [{"name": "张三", "stores": ["天猫旗舰"]}]
        )

    def test_store_without_owners_not_aggregated(self):
        zero_stores, zero_owners = month_unfilled({"新店": "直播"}, {}, {})
        self.assertEqual([s["store"] for s in zero_stores], ["新店"])
        self.assertEqual(zero_owners, [])


class BuildMessageTests(unittest.TestCase):

    def test_two_sections_and_unmatched(self):
        title, body = build_missing_message(
            display="渠道", business_date=_BD,
            missing_rows=[{
                "channel": "天猫", "store": "天猫旗舰",
                "owner_names": ["张三"],
            }],
            zero_rows=[{
                "channel": "京东", "store": "京东POP",
                "owner_names": ["李四"],
            }],
            unmatched_names=["王五"],
            url="http://example/table",
        )
        self.assertEqual(title, "渠道日销到齐检查")
        self.assertIn("未上报数据", body)
        self.assertIn("上报为 0", body)
        self.assertIn("| 天猫 | 天猫旗舰 | 张三 |", body)
        self.assertIn("| 京东 | 京东POP | 李四 |", body)
        self.assertIn("（王五 未匹配到钉钉账号，请手动提醒）", body)
        self.assertIn("[点此填写](http://example/table)", body)
        self.assertIn("已填报请忽略", body)

    def test_month_zero_owners_section(self):
        _, body = build_missing_message(
            display="渠道", business_date=_BD,
            missing_rows=[{
                "channel": "天猫", "store": "天猫旗舰",
                "owner_names": ["张三"],
            }],
            zero_rows=[], unmatched_names=[], url="http://example/table",
            month_zero_owners=[
                {"name": "张三", "stores": ["天猫旗舰", "天猫专营"]},
            ],
        )
        self.assertIn("本月至今零填报", body)
        self.assertIn("| 张三 | 天猫旗舰、天猫专营 |", body)

    def test_month_section_absent_by_default(self):
        _, body = build_missing_message(
            display="渠道", business_date=_BD,
            missing_rows=[{
                "channel": "天猫", "store": "天猫旗舰",
                "owner_names": ["张三"],
            }],
            zero_rows=[], unmatched_names=[], url="http://example/table",
        )
        self.assertNotIn("本月至今零填报", body)


# ---------------------------------------------------------------------------
# run_channel_missing（fake conn 路由查询）
# ---------------------------------------------------------------------------

def _make_conn(*, gate_hit=True, target_rows=(), recent_rows=(),
               daily_rows=(), union_rows=(), member_rows=(),
               month_rows=(), union_column_ok=True):
    def router(sql, params):
        if "sync_dataset_summary" in sql:
            return [{"hit": 1}] if gate_hit else []
        if "fact_channel_store_target" in sql:
            return list(target_rows)
        if "COUNT(DISTINCT" in sql:
            return list(month_rows)
        if "DISTINCT" in sql:
            return list(recent_rows)
        if "fact_channel_daily_sales" in sql:
            return list(daily_rows)
        if "union_id" in sql:
            if not union_column_ok:
                raise RuntimeError("unknown column union_id")
            return list(union_rows)
        if "dim_robot_member" in sql:
            return list(member_rows)
        raise AssertionError(f"unexpected sql: {sql}")

    class _Cursor:
        def __init__(self):
            self._rows = []

        def execute(self, sql, params=None):
            self._rows = router(sql, params)

        def fetchall(self):
            return self._rows

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cursor()

    return _Conn()


class RunChannelMissingTests(unittest.TestCase):

    _TARGET = (
        {"store_name": "天猫旗舰", "channel": "天猫",
         "owners_json": '[{"name": "张三", "unionId": "u-1"}]'},
        {"store_name": "京东POP", "channel": "京东",
         "owners_json": '[{"name": "李四"}]'},
        {"store_name": "拼多多店", "channel": "拼多多",
         "owners_json": '[{"name": "赵六"}]'},
    )

    def _run(self, conn, outbox=None, **kwargs):
        report = {}
        outcome = run_channel_missing(
            conn, outbox if outbox is not None else Mock(),
            region="qudao", display="渠道",
            business_date=_BD, now=_NOW,
            table_url="http://example/table",
            report=report,
            **kwargs,
        )
        return outcome, report

    def test_sync_gate_fail_closed(self):
        conn = _make_conn(gate_hit=False)
        outcome, _ = self._run(conn)
        self.assertEqual(outcome.status, "skipped_sync_failed")

    def test_missing_and_zero_enqueued_once(self):
        outbox = Mock()
        outbox.enqueue.return_value = True
        conn = _make_conn(
            target_rows=self._TARGET,
            daily_rows=[
                # 京东POP 填 0 → 零销售段；拼多多店 >0 → 到齐；天猫旗舰无行 → 缺
                {"channel": "京东", "store_name": "京东POP",
                 "sales_amount": 0, "responsible_person": None},
                {"channel": "拼多多", "store_name": "拼多多店",
                 "sales_amount": 88, "responsible_person": None},
            ],
            union_rows=[{"union_id": "u-1", "user_id": "staff-1"}],
            member_rows=[{"name": "张三", "user_id": "staff-1"}],
        )
        outcome, report = self._run(conn, outbox)
        self.assertEqual(outcome.status, "enqueued")
        self.assertEqual([r["store"] for r in report["missing"]], ["天猫旗舰"])
        self.assertEqual([r["store"] for r in report["zero"]], ["京东POP"])
        self.assertEqual(report["at_user_ids"], ["staff-1"])  # unionId 命中
        kwargs = outbox.enqueue.call_args.kwargs
        self.assertEqual(kwargs["kind"], "channel_missing")
        self.assertIsNone(kwargs.get("dedupe_suffix"))  # 同日只发一条
        self.assertEqual(kwargs["at_user_ids"], ["staff-1"])

    def test_all_filled_silent(self):
        conn = _make_conn(
            target_rows=self._TARGET,
            daily_rows=[
                {"channel": "天猫", "store_name": "天猫旗舰",
                 "sales_amount": 1, "responsible_person": None},
                {"channel": "京东", "store_name": "京东POP",
                 "sales_amount": 2, "responsible_person": None},
                {"channel": "拼多多", "store_name": "拼多多店",
                 "sales_amount": 3, "responsible_person": None},
            ],
        )
        outcome, report = self._run(conn)
        self.assertEqual(outcome.status, "all_filled")
        self.assertEqual(report["missing"], [])

    def test_month_zero_in_report_and_message(self):
        outbox = Mock()
        outbox.enqueue.return_value = True
        conn = _make_conn(
            target_rows=self._TARGET,
            daily_rows=[
                # 天猫旗舰昨日缺 → 触发发消息；本月仅拼多多店有填报
                {"channel": "京东", "store_name": "京东POP",
                 "sales_amount": 2, "responsible_person": None},
                {"channel": "拼多多", "store_name": "拼多多店",
                 "sales_amount": 3, "responsible_person": None},
            ],
            month_rows=[{"store_name": "拼多多店", "days": 5}],
            member_rows=[{"name": "张三", "user_id": "staff-1"}],
        )
        outcome, report = self._run(conn, outbox)
        self.assertEqual(outcome.status, "enqueued")
        # 拼多多店本月有填报 → 赵六不算；张三/李四名下店全月零填报
        self.assertEqual(report["month_zero_owners"], [
            {"name": "张三", "stores": ["天猫旗舰"]},
            {"name": "李四", "stores": ["京东POP"]},
        ])
        self.assertIn("本月至今零填报", report["body_md"])
        self.assertIn("| 李四 | 京东POP |", report["body_md"])

    def test_idempotent_second_enqueue_returns_false(self):
        outbox = Mock()
        outbox.enqueue.return_value = False  # dedupe 命中
        conn = _make_conn(
            target_rows=self._TARGET,
            daily_rows=[],
            member_rows=[],
        )
        outcome, _ = self._run(conn, outbox)
        self.assertEqual(outcome.status, "already_sent")
        self.assertFalse(outcome.enqueued)

    def test_store_exclude_applies(self):
        conn = _make_conn(
            target_rows=self._TARGET,
            daily_rows=[
                {"channel": "京东", "store_name": "京东POP",
                 "sales_amount": 2, "responsible_person": None},
                {"channel": "拼多多", "store_name": "拼多多店",
                 "sales_amount": 3, "responsible_person": None},
            ],
        )
        outcome, report = self._run(conn, store_exclude=("天猫旗舰",))
        self.assertEqual(outcome.status, "all_filled")
        self.assertNotIn("天猫旗舰", report["roster"])

    def test_channel_level_storeless_row_covers_roster_store(self):
        # 猫超式整渠道留空：名册店无店铺行，但渠道有留空行 → 按渠道判
        conn = _make_conn(
            target_rows=(
                {"store_name": "MC猫超", "channel": "猫超",
                 "owners_json": '[{"name": "黄贤宋"}]'},
            ),
            daily_rows=[
                {"channel": "猫超", "store_name": "",
                 "sales_amount": 50, "responsible_person": None},
            ],
        )
        outcome, _ = self._run(conn)
        self.assertEqual(outcome.status, "all_filled")

    def test_name_fallback_when_union_column_missing(self):
        outbox = Mock()
        outbox.enqueue.return_value = True
        conn = _make_conn(
            target_rows=self._TARGET,
            daily_rows=[],
            union_column_ok=False,  # 迁移未应用 → 整级跳过
            member_rows=[{"name": "张三", "user_id": "staff-1"}],
        )
        outcome, report = self._run(conn, outbox)
        self.assertEqual(outcome.status, "enqueued")
        self.assertEqual(report["at_user_ids"], ["staff-1"])  # 姓名级命中
        self.assertIn("李四", report["unmatched"])  # 无成员记录 → 文本点名
        self.assertIn("赵六", report["unmatched"])

    def test_at_limit_overflow_ccs(self):
        target = tuple(
            {"store_name": f"店{i}", "channel": "天猫",
             "owners_json": f'[{{"name": "人{i}"}}]'}
            for i in range(3)
        )
        members = [{"name": f"人{i}", "user_id": f"staff-{i}"} for i in range(3)]
        outbox = Mock()
        outbox.enqueue.return_value = True
        conn = _make_conn(
            target_rows=target, daily_rows=[], member_rows=members,
        )
        outcome, report = self._run(
            conn, outbox, at_limit=2, cc_user_ids=("boss-1",),
        )
        self.assertEqual(outcome.status, "enqueued")
        self.assertEqual(report["at_user_ids"], ["staff-0", "staff-1"])
        self.assertEqual(report["cc_user_ids"], ["boss-1"])
        self.assertIn("人2", report["unmatched"])  # 超出上限落文本
        kwargs = outbox.enqueue.call_args.kwargs
        self.assertEqual(kwargs["at_user_ids"], ["staff-0", "staff-1", "boss-1"])

    def test_dry_enqueues_nothing(self):
        outbox = Mock()
        conn = _make_conn(
            target_rows=self._TARGET, daily_rows=[], member_rows=[],
        )
        outcome, report = self._run(conn, outbox, dry=True)
        self.assertEqual(outcome.status, "dry")
        outbox.enqueue.assert_not_called()
        self.assertEqual(len(report["missing"]), 3)


# ---------------------------------------------------------------------------
# region_config：storeExclude 解析与回写
# ---------------------------------------------------------------------------

class StoreExcludeConfigTests(unittest.TestCase):

    _RAW = {
        "display": "渠道",
        "tableUrl": "http://example/table",
        "robotCode": "ding-x",
        "openConversationId": "cid-x",
        "aliases": {},
        "ccUserIds": [],
        "storeExclude": ["测试店"],
    }

    def test_parse_store_exclude(self):
        cfg = _parse_region("qudao", self._RAW, "regions.qudao")
        self.assertEqual(cfg.store_exclude, ("测试店",))

    def test_default_empty(self):
        raw = {k: v for k, v in self._RAW.items() if k != "storeExclude"}
        cfg = _parse_region("qudao", raw, "regions.qudao")
        self.assertEqual(cfg.store_exclude, ())

    def test_to_mapping_roundtrip(self):
        from common.region_config import _to_mapping

        cfg = _parse_region("qudao", self._RAW, "regions.qudao")
        self.assertEqual(_to_mapping(cfg)["storeExclude"], ["测试店"])


if __name__ == "__main__":
    unittest.main()
