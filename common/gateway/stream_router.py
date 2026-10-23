# -*- coding: utf-8 -*-
"""Stream 总路由 handler（2026-10-24 机器人合并）：单连接下按
``conversation_id`` 把消息委托给报数或问答 handler。

背景（《钉钉机器人合并方案-20261023》§二）：
1. 同一 AppKey 多条 Stream 连接会被钉钉**负载均衡**推送（消息随机落
   其一、另一半丢失）——合并后只能保留 gateway 这一条连接；
2. SDK 的 ``callback_handler_map`` 按 topic 单注册，``ChatbotMessage.
   TOPIC`` 只能挂一个 handler——故注册本类作总入口，进程内再分发；
3. SDK 对每条消息 ``asyncio.create_task`` 并发执行，问答的慢 LLM 调用
   不阻塞报数，无需额外隔离。

分发规则：**报数群优先**（核心业务优先保障），再问答群，都不命中静默
ACK。委托子 handler 的 ``process``（其内部自行再解析一次消息体，幂等、
子 handler 零改动）。
"""

import dingtalk_stream
from dingtalk_stream import AckMessage

from common.gateway.report_intake import region_for_conversation


class RoutedStreamHandler(dingtalk_stream.ChatbotHandler):
    """单连接总入口：报数群 → report handler；问答群 → kb handler。"""

    def __init__(self, *, report_handler, kb_handler, region_configs,
                 kb_conversation_ids, log=print):
        super().__init__()
        self._report = report_handler
        self._kb = kb_handler
        self._region_configs = region_configs
        self._kb_ids = frozenset(kb_conversation_ids or ())
        self._log = log

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        try:
            incoming = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
        except Exception:
            return AckMessage.STATUS_OK, "OK"
        conv_id = getattr(incoming, "conversation_id", "") or ""
        if region_for_conversation(self._region_configs, conv_id) is not None:
            return await self._report.process(callback)   # 报数群优先
        if conv_id in self._kb_ids:
            return await self._kb.process(callback)        # 问答群
        self._log("routed stream: unrouted conversation")
        return AckMessage.STATUS_OK, "OK"


def assert_no_group_overlap(region_configs, kb_conversation_ids):
    """启动断言：报数群与问答群集合不得相交（同群两边登记=路由歧义）。

    冲突抛 ``RuntimeError``——启动失败优于运行期错路由。
    """
    report_convs = {
        cfg.open_conversation_id
        for cfg in region_configs.values()
        if getattr(cfg, "open_conversation_id", None)
    }
    overlap = report_convs & set(kb_conversation_ids or ())
    if overlap:
        raise RuntimeError(
            f"report/kb conversation overlap: {len(overlap)} group(s)"
        )
