# -*- coding: utf-8 -*-
"""知识库问答机器人 Stream 入口（独立应用、独立连接、常驻进程）。

与报数网关的边界：本进程用**另一组应用凭据**建独立 Stream 连接，
只服务客服群问答；事件面与报数面物理隔离，互不争抢、可独立重启。

消息流（P1 纯规则，同步回复，目标 <5s）：
    Stream 回调 → msg_id 去重（kb_qa_audit INSERT IGNORE）
      → 会话白名单（可选 env）→ 规则路由
      → 商品参数/筛选/素材 → kb_products 查询 → 模板渲染
      → 手册类 → 兜底文案（P2 接 LLM+RAG）
      → reply_text（sessionWebhook）
      → 审计行回填 intent/matched/reply_kind

异常处理沿用平台非泄露约定：日志与回执都不含异常原文。
"""

import argparse
import os
from datetime import datetime

import dingtalk_stream
from dingtalk_stream import AckMessage

from common.kb_qa.products import (
    HELP_TEXT,
    find_products,
    render_clarify,
    render_filter_answer,
    render_not_found,
    render_param_answer,
)
from common.kb_qa.router import (
    CHITCHAT,
    DOC_QA,
    PRODUCT_ASSET,
    PRODUCT_FILTER,
    PRODUCT_PARAM,
    classify,
)

#: 手册问答未接入前的诚实兜底（P2 替换为 RAG 答案）。
_DOC_FALLBACK = (
    "产品手册问答还在接入中。现在可以先问我商品参数"
    "（如「习酒窖藏1998 箱规」）或素材（如「发我摘要酒细节图」）。"
)

#: 素材分支 P1 形态：列出附件清单 + 指引；原生图片/文件直发待
#: 媒体消息 msgKey 形态实证后替换（media.py 已备好下载/上传/缓存）。
_ASSET_PENDING = "文件直发功能接入中，请先到 AI 表「产品资料」对应行下载："


def parse_conversation_allowlist(raw):
    """env KB_QA_CONVERSATION_IDS：逗号分隔；空=不限制（应用只进客服群）。"""
    return tuple(item.strip() for item in (raw or "").split(",") if item.strip())


def callback_message_id(callback):
    """从回调提取 messageId（去重键）。

    dingtalk_stream 的 ``callback.headers`` 是 ``frames.Headers`` **对象**
    （蛇形属性 ``message_id``），不是 dict——2026-10-10 首跑事故：
    按 dict ``.get()`` 访问抛 AttributeError，消息到达但零回复。
    防御性兼容 dict 形态（测试桩/未来 SDK 变更）。
    """
    headers = getattr(callback, "headers", None)
    if headers is None:
        return None
    if isinstance(headers, dict):
        return headers.get("messageId") or headers.get("message_id")
    return getattr(headers, "message_id", None)


class KbStreamHandler(dingtalk_stream.ChatbotHandler):
    """客服群问答 handler。所有依赖注入，测试无需 Stream 连接。"""

    def __init__(self, *, connection_factory, conversation_ids=(), now=None,
                 log=print):
        super().__init__()
        self._connection_factory = connection_factory
        self._conversation_ids = conversation_ids
        self._now = now or datetime.now
        self._log = log

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        try:
            incoming = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
        except Exception:
            return AckMessage.STATUS_OK, "OK"

        text = (incoming.text.content or "").strip() if incoming.text else ""
        conversation_id = getattr(incoming, "conversation_id", "") or ""
        if self._conversation_ids and conversation_id not in self._conversation_ids:
            self._log("kb qa: unrouted conversation")
            return AckMessage.STATUS_OK, "OK"

        msg_id = callback_message_id(callback)

        conn = self._connection_factory()
        try:
            if not self._claim(conn, msg_id, incoming, text, conversation_id):
                self._log("kb qa: duplicate delivery")
                return AckMessage.STATUS_OK, "OK"
            reply, intent, matched = self._answer(conn, text)
            self._finish_audit(conn, msg_id, intent, matched,
                               "text" if reply else "none")
            conn.commit()
        except Exception:
            conn.rollback()
            self._log("kb qa: answer failed")
            self._safe_reply(incoming, "查询出了点问题，请稍后再试。")
            return AckMessage.STATUS_OK, "OK"

        if reply:
            self._safe_reply(incoming, reply)
        return AckMessage.STATUS_OK, "OK"

    # ------------------------------------------------------------------
    # 审计与去重
    # ------------------------------------------------------------------

    def _claim(self, conn, msg_id, incoming, text, conversation_id):
        """INSERT IGNORE 审计行占位；msg_id 重复=重复投递，返回 False。"""
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT IGNORE INTO `kb_qa_audit`"
                " (`msg_id`, `conversation_id`, `sender_uid`, `sender_name`,"
                "  `question`, `created_at`) VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    msg_id, conversation_id,
                    getattr(incoming, "sender_staff_id", None),
                    getattr(incoming, "sender_nick", None),
                    text, self._now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            return cursor.rowcount == 1
        finally:
            cursor.close()

    def _finish_audit(self, conn, msg_id, intent, matched, reply_kind):
        if not msg_id:
            return
        cursor = conn.cursor()
        try:
            cursor.execute(
                "UPDATE `kb_qa_audit` SET `intent`=%s, `matched_record_id`=%s,"
                " `reply_kind`=%s WHERE `msg_id`=%s",
                (intent, matched, reply_kind, msg_id),
            )
        finally:
            cursor.close()

    # ------------------------------------------------------------------
    # 问答主流程（纯函数式，便于测试）
    # ------------------------------------------------------------------

    def _answer(self, conn, text):
        """返回 (reply, intent, matched_record_id)。reply=None 表示静默。"""
        if not text:
            return None, CHITCHAT, None
        intent = classify(text)
        if intent.category in (PRODUCT_PARAM, PRODUCT_FILTER):
            rows = find_products(conn, intent.keyword)
            if not rows:
                if intent.category == PRODUCT_PARAM:
                    return render_not_found(intent.keyword), intent.category, None
                return render_not_found(intent.keyword), intent.category, None
            if len(rows) > 1 and intent.category == PRODUCT_PARAM:
                return render_clarify(intent.keyword, rows), intent.category, None
            if intent.category == PRODUCT_FILTER or len(rows) > 1:
                reply = (
                    render_filter_answer(rows, intent.keyword)
                    if len(rows) > 1
                    else render_param_answer(rows[0])
                )
                return reply, intent.category, rows[0]["record_id"]
            return render_param_answer(rows[0]), intent.category, rows[0]["record_id"]
        if intent.category == PRODUCT_ASSET:
            rows = find_products(conn, intent.keyword)
            if not rows:
                return render_not_found(intent.keyword), intent.category, None
            row = rows[0]
            attachments = row.get("attachments") or {}
            if not attachments:
                return (
                    f"「{row['name']}」的资料里还没有附件。",
                    intent.category, row["record_id"],
                )
            lines = [f"「{row['name']}」的附件：", _ASSET_PENDING]
            for field, items in attachments.items():
                lines.append(f"- {field}：{len(items)} 个文件")
            return "\n".join(lines), intent.category, row["record_id"]
        if intent.category == DOC_QA:
            return _DOC_FALLBACK, intent.category, None
        return HELP_TEXT, CHITCHAT, None

    def _safe_reply(self, incoming, text):
        try:
            self.reply_text(text, incoming)
        except Exception:
            self._log("kb qa: reply failed")


def build_client(app_key, app_secret, handler, *, client_factory=None):
    """构造独立 Stream client（与报数网关同一接法，另一组凭据）。"""
    credential = dingtalk_stream.Credential(app_key, app_secret)
    factory = client_factory or dingtalk_stream.DingTalkStreamClient
    client = factory(credential)
    client.register_callback_handler(
        dingtalk_stream.chatbot.ChatbotMessage.TOPIC, handler
    )
    return client


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kb_qa.app")
    parser.add_argument("--live-send", action="store_true")
    parser.add_argument("--confirm-local-test-write", action="store_true")
    args = parser.parse_args(argv)

    from common.gateway.stream_handler import build_stream_client
    from common.public_data import db
    from common.public_data.live_safety import require_gateway_run
    from common.public_data.settings import Settings

    settings = Settings.from_environment()
    require_gateway_run(
        settings,
        live_send=args.live_send,
        confirm_local_test_write=args.confirm_local_test_write,
    )
    app_key = os.environ["KB_DINGTALK_APP_KEY"]
    app_secret = os.environ["KB_DINGTALK_APP_SECRET"]
    allowlist = parse_conversation_allowlist(
        os.environ.get("KB_QA_CONVERSATION_IDS")
    )
    handler = KbStreamHandler(
        connection_factory=lambda: db.connect(settings.mart_database),
        conversation_ids=allowlist,
    )
    client = build_stream_client(app_key, app_secret, handler)
    client.start_forever()


if __name__ == "__main__":
    main()
