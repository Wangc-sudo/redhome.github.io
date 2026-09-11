"""Tests for the mart-backed leaderboard collection and view adapter."""

import unittest
from datetime import date, datetime
from unittest.mock import patch

from common.daily_robot.leaderboard import (
    build_bc_markdown,
    build_html,
    render_bc_markdown,
)
from common.daily_robot.mart_leaderboard import (
    build_leaderboard_view,
    mart_collect,
)
from common.daily_robot.mart_tasks import MartTaskError
from common.region_config import RegionConfig


class _RouterCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        self._conn.queries.append((sql, params))
        if "dim_calendar" in sql:
            self._rows = self._conn.workday_rows
        else:
            self._rows = self._conn.fact_rows

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _RouterConn:
    def __init__(self, *, workdays=(), facts=()):
        self.workday_rows = [{"business_date": d} for d in workdays]
        self.fact_rows = list(facts)
        self.queries = []

    def cursor(self):
        return _RouterCursor(self)


# 2026-09 休息日 [6,13,19,25,26,27]（spec §10 自证场景）。
_REST = (6, 13, 19, 25, 26, 27)
_WORKDAYS = {date(2026, 9, d) for d in range(1, 31) if d not in _REST}
_DAY = date(2026, 9, 11)  # 周五；已过工作日 {1,2,3,4,5,7,8,9,10}


def _fact(name, dept, day, sales, target):
    return {
        "responsible_person": name,
        "department": dept,
        "business_date": date(2026, 9, day),
        "sales_amount": sales,
        "monthly_target": target,
    }


def _fixture_facts():
    return [
        _fact("张三", "杭中", 1, 100, 3000),
        _fact("张三", "杭中", 2, 100, 3000),
        _fact("张三", "杭中", 3, 100, 3000),
        _fact("李四", "滨萧", 1, 500, 1000),
        _fact("李四", "滨萧", 2, None, 1000),
        _fact("  赵六  ", "余杭", 1, 0, 100),       # 姓名带空格 + 显式 0
        _fact("王五", "", 1, 50, None),             # 无部门 + 无目标
        _fact("杭中合计", "杭中", 1, 9999, 9999),   # 合计行必须被跳过
    ]


def _cfg():
    return RegionConfig(
        region="hangzhou", display="杭州",
        table_url="https://example.com/table",
        robot_code="rc", open_conversation_id="conv",
        aliases={}, cc_user_ids=(),
        dept_order=("杭中", "滨萧", "余杭"),
        dept_label={"杭州运营总监": "运营总监"},
        broadcast_exclude=("王五",),
        leaderboard_url="https://pages.example/lb",
    )


class MartCollectTests(unittest.TestCase):

    def _collect(self, **kwargs):
        conn = _RouterConn(workdays=_WORKDAYS, facts=_fixture_facts())
        return mart_collect(conn, region="hangzhou", business_date=_DAY, **kwargs)

    def test_parity_with_legacy_collect_semantics(self):
        data = self._collect()

        self.assertEqual(data.elapsed, (1, 2, 3, 4, 5, 7, 8, 9, 10))
        people = {p["name"]: p for p in data.people}

        # 合计行被跳过；姓名 strip 后归并。
        self.assertNotIn("杭中合计", people)
        self.assertIn("赵六", people)

        self.assertEqual(people["张三"]["dept"], "杭中")
        self.assertEqual(people["张三"]["completed"], 300.0)
        self.assertEqual(people["张三"]["unfilled"], 6)
        self.assertAlmostEqual(people["张三"]["rate"], 0.1)

        self.assertEqual(people["李四"]["unfilled"], 8)
        self.assertAlmostEqual(people["李四"]["rate"], 0.5)

        # 显式 0 = 已填；无部门 → 未分组；无目标 → target 0、rate None。
        self.assertEqual(people["赵六"]["unfilled"], 8)
        self.assertEqual(people["赵六"]["rate"], 0.0)
        self.assertEqual(people["王五"]["dept"], "未分组")
        self.assertEqual(people["王五"]["target"], 0)
        self.assertIsNone(people["王五"]["rate"])

        # 排序：rate desc（None 垫底）、completed desc、target desc。
        self.assertEqual(
            [p["name"] for p in data.people],
            ["李四", "张三", "赵六", "王五"],
        )

    def test_missing_calendar_fails_loudly(self):
        conn = _RouterConn(workdays=set(), facts=[])
        with self.assertRaisesRegex(MartTaskError, "2026-09"):
            mart_collect(conn, region="hangzhou", business_date=_DAY)

    def test_include_today(self):
        data = self._collect(include_today=True)
        self.assertIn(11, data.elapsed)


class LeaderboardViewTests(unittest.TestCase):

    def test_rest_days_are_derived_from_dim_calendar(self):
        view = build_leaderboard_view(_cfg(), _WORKDAYS, year=2026, month=9)
        self.assertEqual(view["calendar"], {
            "month": 9, "restDays": [6, 13, 19, 25, 26, 27],
        })

    def test_region_dict_mirrors_the_legacy_config_shape(self):
        view = build_leaderboard_view(_cfg(), _WORKDAYS, year=2026, month=9)
        self.assertEqual(view["region"], {
            "name": "hangzhou",
            "displayName": "杭州",
            "deptOrder": ["杭中", "滨萧", "余杭"],
            "deptLabel": {"杭州运营总监": "运营总监"},
            "broadcastExclude": ["王五"],
        })


class RenderParityTests(unittest.TestCase):

    def _data(self):
        conn = _RouterConn(workdays=_WORKDAYS, facts=_fixture_facts())
        return mart_collect(conn, region="hangzhou", business_date=_DAY)

    def test_wrapper_delegates_to_render_byte_for_byte(self):
        """重构无行为变化：build_bc_markdown == collect + render。"""
        now = datetime(2026, 9, 11, 8, 30)
        data = self._data()
        view = build_leaderboard_view(_cfg(), data.workdays, year=2026, month=9)
        config = {**view, "dingtalk": {}, "base": {}}

        with patch(
            "common.daily_robot.leaderboard.collect",
            return_value=(now, list(data.elapsed), list(data.people)),
        ):
            via_wrapper = build_bc_markdown(config, url="https://pages.example/lb")
        direct = render_bc_markdown(
            view["region"], view["calendar"], now,
            list(data.elapsed), list(data.people),
            url="https://pages.example/lb",
        )
        self.assertEqual(via_wrapper, direct)

    def test_broadcast_markdown_content_off_mart_data(self):
        now = datetime(2026, 9, 11, 8, 30)  # 周五
        data = self._data()
        view = build_leaderboard_view(_cfg(), data.workdays, year=2026, month=9)
        md = render_bc_markdown(
            view["region"], view["calendar"], now,
            list(data.elapsed), list(data.people),
            url=_cfg().leaderboard_url,
        )

        lines = md.splitlines()
        self.assertEqual(
            lines[0], "### 📊 杭州销售完成率榜（9月10日 周五）"
        )
        self.assertIn("时间进度 **37.5%**（9/24 工作日）", lines[2])
        # 部门排序：滨萧(50%) > 杭中(10%) > 余杭(0%)；王五被播报排除。
        dept_rows = [l for l in lines if l.startswith("| ")][1:]
        self.assertIn("| 1 | 滨萧 | 500 / 1,000 | **50.0%** | +12.5% / 133% |",
                      dept_rows)
        self.assertIn("| 2 | 杭中 | 300 / 3,000 | **10.0%** | -27.5% / 27% |",
                      dept_rows)
        self.assertTrue(all("王五" not in l for l in dept_rows))
        self.assertEqual(
            lines[-1],
            "📊 [点击查看完整榜单（个人明细）](<https://pages.example/lb>)",
        )

    def test_broadcast_markdown_omits_link_without_url(self):
        now = datetime(2026, 9, 11, 8, 30)
        data = self._data()
        view = build_leaderboard_view(_cfg(), data.workdays, year=2026, month=9)
        md = render_bc_markdown(
            view["region"], view["calendar"], now,
            list(data.elapsed), list(data.people), url=None,
        )
        self.assertNotIn("点击查看完整榜单", md)

    def test_html_renders_off_the_mart_view(self):
        """pages 侧就绪证明：既有 build_html 直接吃 mart 视图。"""
        now = datetime(2026, 9, 11, 8, 30)
        data = self._data()
        view = build_leaderboard_view(_cfg(), data.workdays, year=2026, month=9)
        page = build_html(view, now, list(data.elapsed), list(data.people))

        self.assertIn("杭州销售日报 · 完成率榜单", page)
        self.assertIn("张三", page)
        self.assertIn("杭中", page)
        self.assertIn("口径说明", page)
        self.assertIn("9 / 24 个工作日", page)


if __name__ == "__main__":
    unittest.main()
