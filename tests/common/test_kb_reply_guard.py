# -*- coding: utf-8 -*-
"""答复出口挂载（app 层）单测：边界直答短路 + 出口过滤 observe/enforce。

方案 §3 A2、§4 B3（2026-11-02）。用路由假连接与 spy agent_runner，不连真库。
"""

import unittest
from datetime import datetime
from unittest import mock

from common.kb_qa.app import KbStreamHandler
from common.kb_qa.focus import BOUNDARY_ENV
from common.kb_qa.reply_filter import MODE_ENV, MODE_ENFORCE, MODE_OBSERVE

_NOISY = (
    "问题：9月销售及同比\n"
    "9 月销售 120 万元。\n"
    "⚠️ 两点特别说明：\n"
    "1. 口径为实付金额\n"
    "2. 部分退款不回扣\n"
    "其他渠道供参考：拼多多XX店 30 万元。"
)


class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def execute(self, sql, params=()):
        self._conn.executed.append(sql)
        self._rows = list(self._conn.route(sql) or [])

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _Conn:
    def __init__(self, route=None):
        self.route = route or (lambda sql: [])
        self.executed = []
        self.cursor_instance = _Cursor(self)

    def cursor(self):
        return self.cursor_instance


def _handler(route=None, answer=None):
    logs = []
    calls = []

    def agent_runner(conn, text, history, meta=None):
        calls.append(text)
        return answer

    handler = KbStreamHandler(connection_factory=lambda: _Conn(route),
                              log=logs.append, agent_runner=agent_runner)
    return handler, logs, calls


class BoundaryShortCircuitTests(unittest.TestCase):
    def _route(self, min_time=datetime(2026, 8, 22, 9, 0)):
        return lambda sql: ([{"min_value": min_time}]
                            if "MIN(`trade_time`)" in sql else [])

    def test_yoy_before_data_start_skips_agent(self):
        handler, logs, calls = _handler(self._route())
        meta = {}
        reply = handler._try_agent(handler._connection_factory(),
                                   "9月销售及同比", None, meta=meta)
        self.assertIn("没有去年数据", reply)
        self.assertIn("2026-08-22", reply)
        self.assertEqual([], calls)          # 没跑工具
        self.assertEqual("boundary", meta["recall_source"])

    def test_yoy_within_data_start_runs_agent(self):
        route = self._route(datetime(2022, 6, 1, 9, 0))
        handler, _, calls = _handler(route, answer="9 月同比 +10%")
        reply = handler._try_agent(handler._connection_factory(),
                                   "9月销售及同比", None, meta={})
        self.assertEqual("9 月同比 +10%", reply)
        self.assertEqual(["9月销售及同比"], calls)

    def test_switch_off_keeps_old_path(self):
        handler, _, calls = _handler(self._route(), answer="agent 答案")
        with mock.patch.dict("os.environ", {BOUNDARY_ENV: "0"}):
            reply = handler._try_agent(handler._connection_factory(),
                                       "9月销售及同比", None, meta={})
        self.assertEqual("agent 答案", reply)
        self.assertEqual(1, len(calls))

    def test_non_yoy_question_never_probes(self):
        handler, _, _ = _handler(self._route(), answer="120 万元")
        conn = handler._connection_factory()
        handler._try_agent(conn, "9月销售额", None, meta={})
        self.assertEqual([], conn.executed)


class ReplyFilterMountTests(unittest.TestCase):
    def test_observe_is_default_and_only_logs(self):
        handler, logs, _ = _handler(answer=_NOISY)
        with mock.patch.dict("os.environ", {}, clear=False):
            reply = handler._try_agent(handler._connection_factory(),
                                       "9月销售及同比", None, meta={})
        self.assertEqual(_NOISY, reply)       # 未替换
        self.assertTrue(any("reply filter diff" in line for line in logs))

    def test_enforce_filters(self):
        handler, logs, _ = _handler(answer=_NOISY)
        with mock.patch.dict("os.environ", {MODE_ENV: MODE_ENFORCE},
                             clear=False):
            reply = handler._try_agent(handler._connection_factory(),
                                       "9月销售及同比", None, meta={})
        self.assertNotIn("问题：", reply)
        self.assertNotIn("部分退款不回扣", reply)
        self.assertNotIn("拼多多XX店", reply)
        self.assertIn("9 月销售 120 万元", reply)

    def test_observe_mode_env_still_original(self):
        handler, _, _ = _handler(answer=_NOISY)
        with mock.patch.dict("os.environ", {MODE_ENV: MODE_OBSERVE},
                             clear=False):
            reply = handler._try_agent(handler._connection_factory(),
                                       "9月销售及同比", None, meta={})
        self.assertEqual(_NOISY, reply)

    def test_template_answer_not_mangled(self):
        """规则模板通路（参数卡/澄清卡）不过滤——误伤比不过滤更贵。"""
        handler, _, _ = _handler(answer="📦 习酒窖藏1988\n箱规：6\n【商品资料】")
        with mock.patch.dict("os.environ", {MODE_ENV: MODE_ENFORCE},
                             clear=False):
            reply = handler._try_agent(handler._connection_factory(),
                                       "习酒窖藏1988 箱规", None, meta={})
        self.assertIn("箱规：6", reply)
        self.assertIn("【商品资料】", reply)

    def test_agent_failure_still_fails_open(self):
        def boom(conn, text, history, meta=None):
            raise RuntimeError("llm down")
        logs = []
        handler = KbStreamHandler(connection_factory=lambda: _Conn(),
                                  log=logs.append, agent_runner=boom)
        self.assertIsNone(handler._try_agent(
            handler._connection_factory(), "9月销售额", None, meta={}))
        self.assertTrue(any("agent failed" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
