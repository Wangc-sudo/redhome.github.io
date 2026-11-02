# -*- coding: utf-8 -*-
"""答复出口过滤器（reply_filter）单测：规则过滤 + 表示规范化 + 门禁。

方案 §4 B3。零 IO：LLM 用替身，env 用显式 dict。
"""

import unittest

from common.kb_qa import reply_filter as rf


class _Llm:
    """LLM 替身：按构造参数决定返回什么（短文本 / 长文本 / 抛异常）。"""

    def __init__(self, reply=None, raises=False):
        self._reply = reply
        self._raises = raises
        self.calls = []

    def chat(self, messages, max_tokens=None, temperature=None):
        self.calls.append(messages)
        if self._raises:
            raise RuntimeError("llm down")
        return self._reply


class EchoAndNoteTests(unittest.TestCase):
    def test_question_echo_removed(self):
        reply = "问题：杭州昨天出了多少货\n杭州昨天出货 12 万元。"
        self.assertEqual("杭州昨天出货 12 万元。",
                         rf.filter_reply("杭州昨天出了多少货", reply))

    def test_long_note_block_compressed_to_first_item(self):
        reply = (
            "杭州昨天出货 12 万元。\n"
            "⚠️ 两点特别说明：\n"
            "1. 口径为实付金额\n"
            "2. 部分退款不回扣"
        )
        filtered = rf.filter_reply("杭州昨天出货", reply)
        self.assertIn("12 万元", filtered)
        self.assertIn("口径为实付金额", filtered)     # 首条留下
        self.assertNotIn("部分退款不回扣", filtered)
        # 只剩一条时标题还写「两点」就不诚实 → 去条数
        self.assertNotIn("两点", filtered)
        self.assertIn("说明", filtered)

    def test_orphan_note_header_dropped(self):
        """说明段条目被判无关删光后，光杆标题「⚠️ 说明：」一并删掉
        （2026-11-02 云上验收暴露：答复末尾只剩一个空标题）。"""
        reply = ("9 月销售 120 万元。\n"
                 "⚠️ 说明：\n"
                 "其他渠道供参考：拼多多XX店 30 万元。")
        self.assertEqual("9 月销售 120 万元。",
                         rf.filter_reply("9月销售及同比", reply))

    def test_two_line_note_kept(self):
        reply = "出货 12 万元。\n注意：口径为实付金额"
        self.assertEqual(reply, rf.filter_reply("出货", reply))


class ReferenceTests(unittest.TestCase):
    def test_unrelated_reference_line_dropped(self):
        reply = (
            "京东 9 月销售 120 万元。\n"
            "其他店铺供参考：拼多多XX店 30 万元、抖音YY店 20 万元。"
        )
        filtered = rf.filter_reply("京东9月销售", reply)
        self.assertIn("京东 9 月销售 120 万元", filtered)
        self.assertNotIn("拼多多XX店", filtered)

    def test_related_reference_line_kept(self):
        reply = (
            "9 月销售 120 万元。\n"
            "其他店铺供参考：拼多多XX店 30 万元。"
        )
        filtered = rf.filter_reply("拼多多XX店9月销售", reply)
        self.assertIn("拼多多XX店", filtered)

    def test_data_lines_never_dropped(self):
        reply = "1. 京东 120 万元\n2. 猫超 80 万元"
        self.assertEqual(reply, rf.filter_reply("各渠道销售", reply))


class NormalizationTests(unittest.TestCase):
    def test_internal_field_name_masked(self):
        reply = "去年同期数据（has_previous_data=false）不可用。"
        filtered = rf.filter_reply("9月同比", reply)
        self.assertNotIn("has_previous_data", filtered)
        self.assertIn("去年同期数据", filtered)

    def test_unknown_snake_case_removed(self):
        reply = "结果 foo_bar 异常。"
        filtered = rf.filter_reply("x", reply)
        self.assertNotIn("foo_bar", filtered)

    def test_assignment_pair_removed(self):
        reply = "查询返回 channel_mapping=dim_channel_map。"
        filtered = rf.filter_reply("x", reply)
        self.assertNotIn("channel_mapping=dim_channel_map", filtered)
        self.assertIn("渠道口径", filtered)

    def test_zero_count_becomes_missing(self):
        reply = "去年同期：0 条，没有数据可对比。"
        filtered = rf.filter_reply("9月同比", reply)
        self.assertNotIn("0 条", filtered)
        self.assertIn("无数据", filtered)

    def test_zero_in_compare_context_becomes_missing(self):
        """「同比：0 条」是缺数不是零（2026-10-29 截图 badcase 一）：
        即使行内没有「没有数据」字样，同比语境里的 0 也要改口。"""
        filtered = rf.filter_reply("京东9月销售",
                                   "京东 9 月销售 120 万元，去年同期 0 条。")
        self.assertNotIn("0 条", filtered)
        self.assertIn("去年同期 无数据", filtered)

    def test_real_zero_amount_untouched(self):
        # 真实 0 元（无缺数语境）不能被改写成「无数据」
        reply = "该店今日退款 0 元。"
        self.assertEqual(reply, rf.filter_reply("退款", reply))

    def test_template_answer_untouched(self):
        # 规则模板通路（商品参数卡）不过滤是硬要求：误伤比不过滤更贵
        reply = ("📦 53°500ml*6 习酒窖藏1988\n箱规：6\n礼袋规格：2 只\n"
                 "【商品资料】")
        self.assertEqual(reply, rf.filter_reply("习酒窖藏1988 箱规", reply))


class LengthGateTests(unittest.TestCase):
    def test_llm_trim_used_when_over_limit(self):
        long_reply = "无关段落。" * 150
        llm = _Llm(reply="保留结论：120 万元。")
        filtered = rf.filter_reply("9月销售", long_reply, llm=llm)
        self.assertEqual("保留结论：120 万元。", filtered)
        self.assertEqual(1, len(llm.calls))

    def test_llm_failure_falls_back_to_rules(self):
        long_reply = "数据行。" * 100
        filtered = rf.filter_reply("9月销售", long_reply, llm=_Llm(raises=True))
        self.assertIn("数据行。", filtered)

    def test_llm_longer_output_ignored(self):
        long_reply = "数据行。" * 100
        llm = _Llm(reply="更长的一段" * 200)
        filtered = rf.filter_reply("9月销售", long_reply, llm=llm)
        self.assertIn("数据行。", filtered)

    def test_short_reply_never_calls_llm(self):
        llm = _Llm(reply="不该被调用")
        rf.filter_reply("9月销售", "120 万元。", llm=llm)
        self.assertEqual([], llm.calls)


class ApplyModeTests(unittest.TestCase):
    REPLY = "问题：杭州出货\n杭州出货 12 万元。"

    def test_off_returns_original(self):
        self.assertEqual(self.REPLY, rf.apply_reply_filter(
            "杭州出货", self.REPLY, mode=rf.MODE_OFF))

    def test_observe_keeps_original_and_logs(self):
        logs = []
        out = rf.apply_reply_filter("杭州出货", self.REPLY,
                                    mode=rf.MODE_OBSERVE, log=logs.append)
        self.assertEqual(self.REPLY, out)
        self.assertTrue(any("reply filter diff" in line for line in logs))

    def test_enforce_returns_filtered(self):
        out = rf.apply_reply_filter("杭州出货", self.REPLY,
                                    mode=rf.MODE_ENFORCE)
        self.assertEqual("杭州出货 12 万元。", out)

    def test_mode_from_env_defaults_observe(self):
        self.assertEqual(rf.MODE_OBSERVE, rf.env_mode({}))
        self.assertEqual(rf.MODE_OBSERVE, rf.env_mode({rf.MODE_ENV: "bogus"}))
        self.assertEqual(rf.MODE_ENFORCE,
                         rf.env_mode({rf.MODE_ENV: "enforce"}))
        self.assertEqual(rf.MODE_OFF, rf.env_mode({rf.MODE_ENV: "off"}))

    def test_filter_exception_fails_open(self):
        class _Boom(str):
            def splitlines(self):
                raise RuntimeError("boom")
        self.assertEqual("x", rf.apply_reply_filter("q", "x",
                                                    mode=rf.MODE_ENFORCE))
        self.assertIsInstance(_Boom("x"), str)

    def test_empty_reply_passthrough(self):
        self.assertEqual("", rf.apply_reply_filter("q", "",
                                                   mode=rf.MODE_ENFORCE))


if __name__ == "__main__":
    unittest.main()
