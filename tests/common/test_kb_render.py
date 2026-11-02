# -*- coding: utf-8 -*-
"""显示层（common/kb_qa/render.py + run_agent 接线）测试。

事故锚点（2026-11-02 用户截图）：
- 群通道表格列粘连「10/016」= 日期 + 订单行数；
- 合计行「155 14,907.39」不同量纲裸数字并排被连读。
"""

import json
import unittest
from datetime import datetime

from common.kb_qa import agent as agent_module
from common.kb_qa.render import render_table


def _jz_rows():
    return [
        {"goods_name": "53°100ml*20金质习酒第四代（带盒）",
         "qty": 72, "amt": 11995.19, "orders": 67},
        {"goods_name": "53°500ml*2*3金质习酒礼盒（双瓶装）",
         "qty": 79, "amt": 2735.40, "orders": 36},
        {"goods_name": "53°500ml*6习酒金质（2026款）",
         "qty": 4, "amt": 176.80, "orders": 2},
    ]


class RenderTableTests(unittest.TestCase):
    def test_headers_units_and_total_line(self):
        block = render_table(_jz_rows())
        self.assertIn("| 商品 | 销量(件) | 实付金额(元) | 订单数 |", block)
        self.assertIn("11,995.19", block)
        # 合计由代码计算、各量纲带单位，不再裸数字并排
        self.assertIn("合计：销量 155 件、实付金额 14,907.39 元、订单数 105",
                      block)

    def test_every_row_has_column_separators(self):
        rows = [{"d": "2026-10-01", "line_cnt": 6, "qty": 31, "amt": 804.0},
                {"d": "2026-10-02", "line_cnt": 5, "qty": 26, "amt": 808.0}]
        block = render_table(rows)
        for line in block.splitlines():
            if line.startswith("|"):
                self.assertEqual(5, line.count("|"))  # 4 列 = 5 根竖线
        self.assertIn("| 2026-10-01 | 6 | 31 | 804.00 |", block)

    def test_single_row_no_total(self):
        block = render_table([{"qty": 5, "amt": 10.0}])
        self.assertNotIn("合计", block)

    def test_truncation_note_and_no_total(self):
        rows = [{"qty": 1, "amt": 1.0} for _ in range(30)]
        block = render_table(rows)
        self.assertIn("仅显示前 25 行，共 30 行", block)
        self.assertNotIn("合计", block)

    def test_partial_result_no_total(self):
        # returned < total_rows：部分行求和是误导，不出合计
        block = render_table(_jz_rows(), total_rows=60)
        self.assertNotIn("合计", block)

    def test_existing_total_row_not_duplicated(self):
        rows = [{"goods_name": "金质", "qty": 5},
                {"goods_name": "合计", "qty": 5}]
        block = render_table(rows)
        self.assertNotIn("合计：", block)

    def test_non_additive_columns_no_total(self):
        block = render_table([{"channel": "京东", "roi": 1.5},
                              {"channel": "天猫", "roi": 2.0}])
        self.assertNotIn("合计", block)

    def test_empty_rows_returns_empty(self):
        self.assertEqual("", render_table([]))
        self.assertEqual("", render_table(None))

    def test_none_and_date_formatting(self):
        rows = [{"d": datetime(2026, 10, 1), "qty": None},
                {"d": "2026-10-02 00:00:00", "qty": 3}]
        block = render_table(rows)
        self.assertIn("| 2026-10-01 | — |", block)
        self.assertIn("| 2026-10-02 | 3 |", block)
        # None 不当 0 也不阻断合计：只对真实数值求和
        self.assertIn("合计：销量 3 件", block)


class _RowsExecutor:
    """按工具名返回预置 JSON 的替身。"""

    def __init__(self, payloads):
        self._payloads = payloads
        self.calls = []

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        return json.dumps(self._payloads.get(name, {}), ensure_ascii=False)


def _llm(responses, final="最终答案"):
    state = {"i": 0}

    class _Llm:
        def chat_with_tools(self, messages, *, tools, **kw):
            response = responses[min(state["i"], len(responses) - 1)]
            state["i"] += 1
            return response

        def chat(self, messages, **kw):
            return final

    return _Llm()


def _sql_call():
    return {"content": None, "tool_calls": [{
        "id": "c1", "type": "function",
        "function": {"name": "query_sql", "arguments": '{"sql":"SELECT 1"}'}}]}


class AgentDisplayTests(unittest.TestCase):
    def test_query_sql_appends_rendered_block(self):
        executor = _RowsExecutor({"query_sql": {
            "total_rows": 2, "returned": 2,
            "rows": [{"d": "2026-10-01", "qty": 31, "amt": 804.0},
                     {"d": "2026-10-02", "qty": 26, "amt": 808.0}]}})
        responses = [_sql_call(), {"content": "10 月全期如下。", "tool_calls": []}]
        answer = agent_module.run_agent(
            _llm(responses), "致中和京东 10/1-10/21 销量", executor=executor)
        self.assertTrue(answer.startswith("10 月全期如下。"))
        self.assertIn("| 日期 | 销量(件) | 实付金额(元) |", answer)
        self.assertIn("合计：销量 57 件、实付金额 1,612.00 元", answer)

    def test_non_display_tool_unaffected(self):
        executor = _RowsExecutor({"daily_sales": {
            "rows": [{"qty": 1}], "found": True}})
        responses = [
            {"content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "daily_sales", "arguments": "{}"}}]},
            {"content": "杭州 12.5 万。", "tool_calls": []},
        ]
        answer = agent_module.run_agent(
            _llm(responses), "杭州昨天", executor=executor)
        self.assertEqual("杭州 12.5 万。", answer)

    def test_error_and_empty_rows_no_block(self):
        executor = _RowsExecutor({"query_sql": {"error": "SQL 执行失败"}})
        responses = [_sql_call(), {"content": "没查到。", "tool_calls": []}]
        answer = agent_module.run_agent(
            _llm(responses), "q", executor=executor)
        self.assertEqual("没查到。", answer)

        executor2 = _RowsExecutor({"query_sql": {"total_rows": 0, "rows": []}})
        responses2 = [_sql_call(), {"content": "无数据。", "tool_calls": []}]
        answer2 = agent_module.run_agent(
            _llm(responses2), "q", executor=executor2)
        self.assertEqual("无数据。", answer2)

    def test_exhausted_rounds_append_block(self):
        executor = _RowsExecutor({"query_sql": {
            "total_rows": 2,
            "rows": [{"qty": 31, "amt": 804.0}, {"qty": 26, "amt": 808.0}]}})
        answer = agent_module.run_agent(
            _llm([_sql_call()]), "q", executor=executor, max_rounds=1)
        self.assertIn("最终答案", answer)
        self.assertIn("合计：销量 57 件、实付金额 1,612.00 元", answer)

    def test_prompt_declares_display_layer(self):
        self.assertIn("系统统一渲染", agent_module._SYSTEM_PROMPT)
        self.assertIn("不要手写 Markdown", agent_module._SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
