# -*- coding: utf-8 -*-
"""知识库问答机器人 Stream 入口（独立应用、独立连接、常驻进程）。

与报数网关的边界：本进程用**另一组应用凭据**建独立 Stream 连接，
只服务客服群问答；事件面与报数面物理隔离，互不争抢、可独立重启。

消息流（P1 规则打底、P2 接天翼 DeepSeek，同步回复，目标 <5s）：
    Stream 回调 → msg_id 去重（kb_qa_audit INSERT IGNORE）
      → 会话白名单（可选 env）→ 规则路由
      → 商品参数/筛选/素材 → kb_products 查询 → LLM 口语化渲染（降模板）
      → 手册类 → BM25 召回切片 → LLM 带出处答案（降切片摘录/兜底）
      → reply_text（sessionWebhook）
      → 审计行回填 intent/matched/reply_kind

P2 fail-open 铁律：LLM 未配置/不可达/超时/返回畸形时全部降级 P1 模板行为，
绝不把异常甩给客服（``from_environment()`` 缺配返回 None 即「无 LLM」）。

异常处理沿用平台非泄露约定：日志与回执都不含异常原文。
"""

import argparse
import os
from datetime import datetime

import dingtalk_stream
from dingtalk_stream import AckMessage

from common.kb_qa.docs import search_docs
from common.kb_qa.llm import (
    LlmError,
    extract_query,
    from_environment,
    render_doc_answer,
    render_product_answer,
)
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

#: 手册无召回切片（未导入/问得偏）时的诚实兜底。
_DOC_FALLBACK = (
    "产品手册问答还在接入中。现在可以先问我商品参数"
    "（如「习酒窖藏1998 箱规」）或素材（如「发我摘要酒细节图」）。"
)

#: 素材分支 P1 形态：列出附件清单 + 指引；原生图片/文件直发待
#: 媒体消息 msgKey 形态实证后替换（media.py 已备好下载/上传/缓存）。
_ASSET_PENDING = "文件直发功能接入中，请先到 AI 表「产品资料」对应行下载："

#: 群回复 markdown 卡片标题。
_REPLY_TITLE = "产品资料助手"

#: 手册切片摘录兜底：无 LLM 时直接回切片标题 + 前 200 字（带出处）。
_DOC_EXCERPT_CHARS = 200


def _render_doc_excerpt(chunks):
    """BM25 召回切片 → 「标题 + 前 200 字」摘录（无 LLM 时的降级答案）。"""
    lines = ["根据产品手册："]
    for chunk in chunks:
        title = chunk.get("doc_title") or "产品手册"
        excerpt = (chunk.get("content") or "").strip()[:_DOC_EXCERPT_CHARS]
        lines.append(f"【{title}】{excerpt}")
    return "\n".join(lines)


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
    """客服群问答 handler。所有依赖注入，测试无需 Stream 连接。

    回复通道（2026-10-10 二次事故教训）：**不用 SDK reply_text 走
    sessionWebhook**——该路径对应用级 errcode 静默（SDK 只检查 HTTP
    状态码，body 里的 errcode 不抛不记），曾出现「处理完成、群内零回复、
    日志无痕」。改走 ``DingTalkClient.send_group_markdown``
    （groupMessages/send，errcode 显式可查，已实测可达），*reply_client*
    为空才回退 reply_text。群聊场景限定（客服群）；单聊回退 reply_text。
    """

    def __init__(self, *, connection_factory, conversation_ids=(), now=None,
                 log=print, reply_client=None, robot_code=None, llm=None,
                 doc_search=None):
        super().__init__()
        self._connection_factory = connection_factory
        self._conversation_ids = conversation_ids
        self._now = now or datetime.now
        self._log = log
        self._reply_client = reply_client
        self._robot_code = robot_code
        self._llm = llm
        self._doc_search = doc_search or search_docs

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
                # 规则层关键词整词/分词全 miss：有 LLM 时二次解析换关键词重查一次。
                rows = self._reparse_products(conn, text)
            if not rows:
                return render_not_found(intent.keyword), intent.category, None
            if len(rows) > 1 and intent.category == PRODUCT_PARAM:
                # 多款同名 → 规则反问确认（不猜，不受 LLM 影响）。
                return render_clarify(intent.keyword, rows), intent.category, None
            reply = self._render_products(text, rows, intent)
            return reply, intent.category, rows[0]["record_id"]
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
            return self._answer_docs(conn, text)
        return HELP_TEXT, CHITCHAT, None

    # ------------------------------------------------------------------
    # P2：LLM 渲染与手册召回（全部 fail-open，异常只降级不外泄）
    # ------------------------------------------------------------------

    def _render_products(self, question, rows, intent):
        """命中行后的渲染：有 LLM 先试口语化答案，失败/无 LLM 降 P1 模板。

        ``_answer`` 已拦下「多款 PARAM 反问」，这里只可能是单款 PARAM 或
        FILTER 列表，故降级分支与 P1 完全一致（多行列表 / 单行参数卡）。
        """
        if self._llm is not None:
            try:
                return render_product_answer(self._llm, question, rows)
            except LlmError:
                self._log("kb qa: llm product render failed")
        if len(rows) > 1:
            return render_filter_answer(rows, intent.keyword)
        return render_param_answer(rows[0])

    def _reparse_products(self, conn, question):
        """检索全 miss 时的 LLM 二次解析：换关键词重查一次商品。"""
        if self._llm is None:
            return []
        try:
            _category, keyword = extract_query(self._llm, question)
        except LlmError:
            self._log("kb qa: llm query extract failed")
            return []
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        return find_products(conn, keyword)

    def _answer_docs(self, conn, question):
        """手册问答三态：有切片+有 LLM 带出处答案；有切片无 LLM 摘录；无切片兜底。"""
        chunks = self._doc_search(conn, question, k=3)
        if not chunks:
            return _DOC_FALLBACK, DOC_QA, None
        if self._llm is not None:
            try:
                return render_doc_answer(self._llm, question, chunks), DOC_QA, None
            except LlmError:
                self._log("kb qa: llm doc render failed")
        return _render_doc_excerpt(chunks), DOC_QA, None

    def _safe_reply(self, incoming, text):
        conversation_id = getattr(incoming, "conversation_id", "") or ""
        sender_uid = incoming.sender_staff_id or ""
        try:
            if self._reply_client is not None and conversation_id:
                self._reply_client.send_group_markdown(
                    self._robot_code, conversation_id,
                    _REPLY_TITLE, text,
                    at_user_ids=[sender_uid] if sender_uid else None,
                )
            else:
                self.reply_text(text, incoming)
            self._log("kb qa: reply sent")
        except Exception:
            # 非泄露约定：不记异常原文；errcode 类失败由
            # DingTalkClient 抛出走到这里（sessionWebhook 路径则无感）。
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
    from common.dingtalk.client import DingTalkClient

    reply_client = DingTalkClient(
        app_key, app_secret, os.environ["KB_DINGTALK_OPERATOR_ID"]
    )
    # P2：LLM 未配置（KB_LLM_* 缺失）或模型未开通时 from_environment 返回
    # None，handler 按「无 LLM」降级 P1 模板运行，不影响 P1 既有能力。
    llm = from_environment()
    if llm is None:
        print("kb qa: LLM not configured, running in P1 template mode")
    handler = KbStreamHandler(
        connection_factory=lambda: db.connect(settings.mart_database),
        conversation_ids=allowlist,
        reply_client=reply_client,
        robot_code=app_key,
        llm=llm,
    )
    client = build_stream_client(app_key, app_secret, handler)
    client.start_forever()


if __name__ == "__main__":
    main()
