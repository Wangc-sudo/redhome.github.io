"""dim_channel_map：抬头→渠道的解析（渠道管线迁 WDT 第 2 步）。"""
import unittest

from common.public_data.channel_map import (
    DEFAULT_MAP_ROWS,
    MATCH_BUYER_NICK,
    MATCH_SHOP,
    PANEL_B2B,
    PANEL_CHANNEL,
    UNMAPPED_CHANNELS,
    channel_map_ddl_statements,
    fetch_channel_map,
    normalize_match_value,
    resolve_channel,
)


class _FakeCursor:
    def __init__(self, rows=None, exc=None):
        self._rows = rows or []
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _sql, _params=None):
        if self._exc is not None:
            raise self._exc
        return len(self._rows)

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self, rows=None, exc=None):
        self._rows = rows
        self._exc = exc

    def cursor(self):
        return _FakeCursor(self._rows, self._exc)


class NormalizeTests(unittest.TestCase):
    """云上实锤：同一主体混用全角/半角括号，不归一化会漏匹配。"""

    def test_fullwidth_parentheses_and_spaces(self):
        self.assertEqual(
            normalize_match_value("浙江天猫网络科技有限公司（寄售）"),
            normalize_match_value("浙江天猫网络科技有限公司 (寄售)"),
        )
        self.assertEqual(
            normalize_match_value(" 深圳丰享信息技术有限公司 "),
            "深圳丰享信息技术有限公司",
        )

    def test_none_and_empty(self):
        self.assertEqual(normalize_match_value(None), "")
        self.assertEqual(normalize_match_value(""), "")


class ResolveTests(unittest.TestCase):
    @staticmethod
    def _rule(channel, value, mode="exact", match_type=MATCH_BUYER_NICK,
              priority=100):
        return {
            "channel_key": channel, "match_type": match_type,
            "match_mode": mode, "match_value": value, "priority": priority,
        }

    def test_exact_match(self):
        rules = [self._rule("丰享", "深圳丰享信息技术有限公司")]
        self.assertEqual(
            resolve_channel({"buyer_nick": "深圳丰享信息技术有限公司"}, rules),
            "丰享",
        )

    def test_prefix_match(self):
        rules = [self._rule("直播", "直播间寄样", mode="prefix")]
        self.assertEqual(
            resolve_channel({"buyer_nick": "直播间寄样（待定）"}, rules),
            "直播",
        )

    def test_priority_wins(self):
        rules = [
            self._rule("A", "抬头", priority=50),
            self._rule("B", "抬头", priority=10),
        ]
        self.assertEqual(resolve_channel({"buyer_nick": "抬头"}, rules), "B")

    def test_shop_type_uses_shop_column(self):
        rules = [self._rule("京东", "某店铺", match_type=MATCH_SHOP)]
        self.assertIsNone(resolve_channel({"buyer_nick": "某店铺"}, rules))
        self.assertEqual(
            resolve_channel({"shop_name": "某店铺"}, rules), "京东")

    def test_unmapped_returns_none(self):
        self.assertIsNone(resolve_channel({"buyer_nick": "未知抬头"},
                                          [self._rule("猫超", "天猫")]))
        self.assertIsNone(resolve_channel({}, []))


class FetchTests(unittest.TestCase):
    def test_rows_are_normalized_to_dicts(self):
        rows = [{
            "id": 1, "channel_key": "猫超", "channel_label": "猫超",
            "panel": PANEL_CHANNEL, "match_type": MATCH_BUYER_NICK,
            "match_mode": "exact", "match_value": "抬头", "priority": 10,
            "note": None, "enabled": 1,
        }]
        got = fetch_channel_map(_FakeConnection(rows))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["channel_key"], "猫超")

    def test_missing_table_is_fail_soft(self):
        self.assertEqual(
            fetch_channel_map(_FakeConnection(exc=RuntimeError("no table"))),
            [],
        )


class SeedRowsTests(unittest.TestCase):
    """默认映射真值（云上 probe，2026-10-23）：结构与口径钉。"""

    def test_unique_match_keys(self):
        keys = [(row[3], normalize_match_value(row[5])) for row in DEFAULT_MAP_ROWS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_panels_are_known(self):
        for row in DEFAULT_MAP_ROWS:
            self.assertIn(row[2], (PANEL_CHANNEL, PANEL_B2B))

    def test_huangxiansong_merges_into_private_domain(self):
        """黄贤宋（私域+海康不可分）→ 私域（含海康）。"""
        hit = [row for row in DEFAULT_MAP_ROWS if row[5] == "黄贤宋"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0][0], "私域")
        self.assertEqual(hit[0][1], "私域（含海康）")

    def test_b2b_channels_land_on_their_own_panel(self):
        """丰享/海澜之家/酱云汇 单独成一个面板（2026-10-23 裁决）。"""
        for label in ("丰享", "海澜之家", "酱云汇"):
            hit = [row for row in DEFAULT_MAP_ROWS if row[0] == label]
            self.assertTrue(hit, label)
            for row in hit:
                self.assertEqual(row[2], PANEL_B2B)

    def test_ddl_is_replayable(self):
        statements = channel_map_ddl_statements()
        self.assertEqual(len(statements), 2)
        for sql in statements:
            self.assertIn("CREATE TABLE IF NOT EXISTS", sql)

    def test_unmapped_channels_recorded(self):
        """B2C 零售单 buyer_nick 为空，拼多多/抖音暂无抬头可映射。"""
        self.assertIn("拼多多", UNMAPPED_CHANNELS)


if __name__ == "__main__":
    unittest.main()
