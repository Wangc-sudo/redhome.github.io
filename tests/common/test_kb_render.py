# -*- coding: utf-8 -*-
"""显示层（common/kb_qa/render.py + run_agent 接线）测试。

事故锚点（2026-11-02 用户截图）：
- 群通道表格列粘连「10/016」= 日期 + 订单行数；
- 合计行「155 14,907.39」不同量纲裸数字并排被连读。
"""

import json
import unittest
from datetime import date, datetime

from common.kb_qa import agent as agent_module
from common.kb_qa.focus import Period
from common.kb_qa.render import render_answer, render_table, summary_card
from common.kb_qa.result import from_payload


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


    def test_string_numbers_from_query_sql_normalized(self):
        # query_sql 给 LLM 的值已 str() 化（云上验收实测 "804.0000"），
        # 显示层必须归一，否则千分位与合计失效。
        rows = [{"d": "2026-10-01", "qty": "31.0000", "amt": "804.0000"},
                {"d": "2026-10-02", "qty": "26.0000", "amt": "808.0000"}]
        block = render_table(rows)
        self.assertIn("| 2026-10-01 | 31 | 804.00 |", block)
        self.assertIn("合计：销量 57 件、实付金额 1,612.00 元", block)


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


class ResultSetTests(unittest.TestCase):
    def test_payload_normalization(self):
        result = from_payload("query_sql", {
            "total_rows": 2, "rows": [{"qty": 1}, {"qty": 2}]})
        self.assertEqual(2, len(result.rows))
        self.assertTrue(result.complete)
        self.assertIsNone(from_payload("query_sql", {"error": "x"}))
        self.assertIsNone(from_payload("query_sql", {"rows": []}))
        self.assertIsNone(from_payload(  # 单格标量探索锚点
            "query_sql", {"rows": [{"max_time": "2026-11-02"}]}))

    def test_incomplete_result_set(self):
        result = from_payload("query_sql", {
            "total_rows": 60, "rows": [{"qty": 1}, {"qty": 2}]})
        self.assertFalse(result.complete)


class SummaryCardTests(unittest.TestCase):
    _ROWS = [
        {"d": "2026-10-01", "qty": 31, "amt": 804.0},
        {"d": "2026-10-02", "qty": 26, "amt": 808.0},
    ]

    def test_card_range_total_daily_extremes(self):
        card = summary_card(self._ROWS)
        self.assertIn("2026-10-01~2026-10-02", card)
        self.assertIn("合计：销量 57 件、实付金额 1,612.00 元", card)
        self.assertIn("日均 806.00 元", card)
        self.assertIn("峰值 10-02（808.00 元）", card)
        self.assertIn("谷值 10-01（804.00 元）", card)

    def test_card_skipped_for_single_row_or_incomplete(self):
        self.assertEqual("", summary_card(self._ROWS[:1]))
        self.assertEqual("", summary_card(self._ROWS, complete=False))

    def test_card_without_date_column(self):
        card = summary_card([{"goods_name": "A", "qty": 3},
                             {"goods_name": "B", "qty": 5}])
        self.assertIn("合计：销量 8 件", card)
        self.assertIn("峰值 B（5 件）", card)


class PeriodFilterTests(unittest.TestCase):
    _FOUR_DAYS = [
        {"d": "2026-10-30", "qty": 130, "amt": 21816.35},
        {"d": "2026-10-31", "qty": 18, "amt": 2933.12},
        {"d": "2026-11-01", "qty": 16, "amt": 2656.47},
        {"d": "2026-11-02", "qty": 1, "amt": 100.0},
    ]

    def test_yesterday_filters_rows_and_total(self):
        # 问「昨天」：4 天行只留 11-01，合计与表格同范围（不再跨 4 天）
        period = Period(date(2026, 11, 1), date(2026, 11, 1), "昨天")
        block = render_answer(self._FOUR_DAYS, period=period)
        self.assertIn("2026-11-01", block)
        self.assertNotIn("2026-10-30", block)
        self.assertNotIn("合计：销量 165 件", block)

    def test_no_date_column_passthrough(self):
        period = Period(date(2026, 11, 1), date(2026, 11, 1), "昨天")
        block = render_answer([{"goods_name": "A", "qty": 3},
                               {"goods_name": "B", "qty": 5}], period=period)
        self.assertIn("合计：销量 8 件", block)

    def test_empty_after_filter_returns_empty(self):
        period = Period(date(2025, 1, 1), date(2025, 1, 1), "昨天")
        self.assertEqual("", render_answer(self._FOUR_DAYS, period=period))

    def test_answer_block_card_and_table_single_total(self):
        block = render_answer(SummaryCardTests._ROWS)
        self.assertEqual(1, block.count("合计："))  # 合计只在结论卡出现一次
        self.assertIn("| 日期 | 销量(件) | 实付金额(元) |", block)


class _SeqExecutor:
    """逐次调用返回不同结果的替身（探索查询 + 最终查询场景）。"""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        return json.dumps(self._payloads.pop(0), ensure_ascii=False)


class AgentDisplayTests(unittest.TestCase):
    _EXPLORE = {"total_rows": 1, "rows": [{"max_time": "2026-11-02 05:30:28"}]}
    _FINAL = {"total_rows": 2,
              "rows": [{"qty": 31, "amt": 804.0}, {"qty": 26, "amt": 808.0}]}

    def test_single_scalar_anchor_not_a_candidate(self):
        # max_time/latest 等单格锚点是探索副产物，不进候选表
        executor = _RowsExecutor({"query_sql": self._EXPLORE})
        answer = agent_module.run_agent(
            _llm([_sql_call()], final="最新日期 11-01。"), "q",
            executor=executor, max_rounds=1)
        self.assertEqual("最新日期 11-01。", answer)

    _EXPLORE2 = {"total_rows": 1, "rows": [{"channel": "京东", "cnt": 321}]}

    def test_multiple_blocks_only_referenced_is_sent(self):
        executor = _SeqExecutor([self._EXPLORE2, self._FINAL])
        answer = agent_module.run_agent(
            _llm([_sql_call(), _sql_call()], final="结论。[[表2]]"),
            "q", executor=executor, max_rounds=2)
        self.assertIn("合计：销量 57 件", answer)
        self.assertNotIn("cnt", answer)
        self.assertNotIn("[[表", answer)

    def test_multiple_blocks_without_reference_not_sent(self):
        # 探索性多表 + 模型未引用 → 一张都不发（防中间过程噪音）
        executor = _SeqExecutor([self._EXPLORE2, self._FINAL])
        answer = agent_module.run_agent(
            _llm([_sql_call(), _sql_call()], final="结论。"),
            "q", executor=executor, max_rounds=2)
        self.assertEqual("结论。", answer)

    def test_invalid_placeholder_removed(self):
        executor = _RowsExecutor({"query_sql": self._FINAL})
        answer = agent_module.run_agent(
            _llm([_sql_call()], final="见[[表9]]"), "q",
            executor=executor, max_rounds=1)
        self.assertNotIn("[[表9]]", answer)
        self.assertIn("合计：销量 57 件", answer)  # 单表仍自动附

    def test_final_instruction_lists_block_numbers(self):
        seen = []

        class _Llm:
            def chat_with_tools(self, messages, *, tools, **kw):
                return _sql_call()

            def chat(self, messages, **kw):
                seen.append(messages[-1]["content"])
                return "结论。[[表1]]"

        executor = _RowsExecutor({"query_sql": self._FINAL})
        agent_module.run_agent(_Llm(), "q", executor=executor, max_rounds=1)
        self.assertIn("编号 表1", seen[0])
        self.assertIn("[[表N]]", seen[0])


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
