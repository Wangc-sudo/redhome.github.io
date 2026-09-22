# -*- coding: utf-8 -*-
"""互动卡片菜单（PoC，2026-09-23 运维批准）：/菜单 发按钮卡片，按钮动作
经 **Stream 通道** 回调执行辅助指令——零入站架构不变（不开公网回调）。

链路：`/菜单` → ``MarkdownButtonCardInstance``（SDK 内置公共模板，免搭建）
→ 用户点按钮 → 钉钉经 Stream 推送 ``/v1.0/card/instances/callback`` →
本模块路由到既有 ``/辅助指令``（report_intake 原样复用，门禁不变）→
结果**就地更新卡片**（不刷屏，按钮保留可连查）。

设计约束：
* 按钮可执行的指令白名单 = 查询类（未填/我的/门店/帮助）；``/补签`` 需要
  键入参数且属写操作，只走文字指令，不上卡片；
* 回调报文结构宽容解析（content 兼容 JSON 字符串/已解析字典），首个真实
  回调会打印原始键名（安全字段）用于校准；
* 发送/更新卡片失败只记日志降级为文字提示，不影响报数主链路。
"""

import json
import re
from datetime import datetime

import dingtalk_stream
from dingtalk_stream import AckMessage

from common.gateway.report_intake import (
    handle_report,
    region_for_conversation,
)

#: 触发菜单卡片的文字指令。
_MENU_WORDS = ("/菜单", "/menu")

#: 卡片按钮可路由的查询类指令白名单（补签等写操作不上卡片）。
_CARD_AUX_COMMANDS = ("未填", "我的", "门店", "帮助")

MENU_MARKDOWN = (
    "**点按钮直接查**（结果就地更新本卡片，不刷屏）：\n\n"
    "- 📋 **今日未填**：与 18:30 提醒同口径\n"
    "- 📊 **我的进度**：你的本月累计/目标/完成率\n"
    "- 🏬 **各店进度**：多门店区域各店一览\n\n"
    "也可以 @我 发 `/帮助` 查看全部指令（含管理人 /补签）。"
)

MENU_TITLE = "提醒事项 辅助菜单"
MENU_TIPS = "报数助手"


def build_menu_buttons():
    """菜单按钮。``action.privateData.aux`` 是回调路由键（与 /指令 同名）。

    ``actionType=request`` 表示点击后回调开发者（Stream 通道），
    ``privateData`` 原样回传。
    """

    def _btn(text, aux, color="blue"):
        return {
            "text": text,
            "color": color,
            "action": {
                "actionType": "request",
                "privateData": {"aux": aux},
            },
        }

    return [
        _btn("📋 今日未填", "未填"),
        _btn("📊 我的进度", "我的"),
        _btn("🏬 各店进度", "门店"),
        _btn("🤖 帮助", "帮助", color="gray"),
    ]


def is_menu_request(text):
    """``/菜单`` 判定（兼容 @机器人 前缀与零宽字符）。"""
    body = (text or "").strip()
    body = re.sub("[​‌‍⁠﻿]", "", body)  # U+200B/200C/200D/2060/FEFF
    body = re.sub(r"^@[^\s/]+\s*", "", body)
    return body.lower() in _MENU_WORDS


def send_menu_card(handler, incoming):
    """经 ChatbotHandler 便捷方法发送按钮卡片。"""
    handler.reply_markdown_button(
        incoming, MENU_MARKDOWN, build_menu_buttons(), tips=MENU_TIPS
    )


def _as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return {}


def extract_aux_action(data):
    """从卡片回调报文提取 ``(aux, user_id, conversation_id, card_instance_id)``。

    官方结构：``content.cardPrivateData.action.privateData`` 原样回传按钮
    私有数据。取不到 aux 时首元素为 ``None``（调用方应静默 ACK）。
    """
    data = data or {}
    content = _as_dict(data.get("content"))
    private = _as_dict(
        _as_dict(_as_dict(content.get("cardPrivateData")).get("action"))
        .get("privateData")
    )
    conversation_id = (
        content.get("openConversationId")
        or _as_dict(data.get("extension")).get("openConversationId")
        or data.get("spaceId")
        or ""
    )
    return (
        private.get("aux"),
        data.get("userId") or "",
        conversation_id,
        data.get("outTrackId") or content.get("cardInstanceId") or "",
    )


class MenuCardCallbackHandler(dingtalk_stream.CallbackHandler):
    """卡片按钮回调：白名单指令路由 + 结果就地更新卡片。

    与报数 handler 共用**同一条 Stream 连接**（多 topic 注册，不另建连接）。
    依赖全部注入，测试无需 Stream。
    """

    def __init__(self, *, region_configs, connection_factory,
                 report_handler=handle_report, now=None, log=print,
                 card_factory=None):
        super().__init__()
        self._region_configs = region_configs
        self._connection_factory = connection_factory
        self._report_handler = report_handler
        self._now = now or datetime.now
        self._log = log
        self._card_factory = card_factory

    async def process(self, callback):
        data = callback.data or {}
        aux, user_id, conversation_id, card_instance_id = extract_aux_action(data)
        # 首个真实回调打印原始键名（安全字段）用于校准宽容解析。
        self._log(
            "card callback keys=" + ",".join(sorted(data.keys()))
            + f" aux={aux}"
        )
        if aux not in _CARD_AUX_COMMANDS:
            return AckMessage.STATUS_OK, "OK"
        region_cfg = region_for_conversation(
            self._region_configs, conversation_id
        )
        if region_cfg is None:
            self._log("card callback unrouted")
            return AckMessage.STATUS_OK, "OK"

        conn = self._connection_factory()
        try:
            outcome = self._report_handler(
                conn,
                region_cfg=region_cfg,
                text=f"/{aux}",
                sender_uid=user_id,
                now=self._now(),
                all_region_cfgs=tuple(self._region_configs.values()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            self._log("card aux failed")
            self._safe_update_card(
                card_instance_id, "⚠️ 处理出错，请稍后再试，或直接在群里 @我 报数。"
            )
            return AckMessage.STATUS_OK, "处理出错"

        self._safe_update_card(card_instance_id, outcome.reply)
        return AckMessage.STATUS_OK, "已更新"

    def _safe_update_card(self, card_instance_id, markdown):
        if not card_instance_id:
            return
        try:
            factory = (
                self._card_factory
                or dingtalk_stream.MarkdownButtonCardInstance
            )
            card = factory(self.dingtalk_client, None)
            card.card_instance_id = card_instance_id
            card.set_title_and_logo(MENU_TITLE, "")
            card.update(markdown, build_menu_buttons(), tips=MENU_TIPS)
        except Exception:
            self._log("card update failed")
