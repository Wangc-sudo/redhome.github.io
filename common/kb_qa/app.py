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
import re
import time
from datetime import datetime

import dingtalk_stream
from dingtalk_stream import AckMessage

from common.kb_qa.assets import is_strong_asset_question, pick_asset
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
    Intent,
    PHONE_LOOKUP,
    PRODUCT_ASSET,
    PRODUCT_FILTER,
    PRODUCT_PARAM,
    SALES_QUERY,
    classify,
)

#: 手册无召回切片（未导入/问得偏）时的诚实兜底。
_DOC_FALLBACK = (
    "产品手册问答还在接入中。现在可以先问我商品参数"
    "（如「习酒窖藏1998 箱规」）或素材（如「发我摘要酒细节图」）。"
)

#: 业务数据问句（SALES_QUERY）在 agent 不可用/无答案时的诚实兜底——
#: 不能落 render_not_found（那句话术是按商品语境写的，会误导）。
_SALES_FALLBACK = (
    "业务数据查询暂时不可用，请稍后再试。商品参数、素材、手册问答不受影响。"
)

#: 号码台账问句（PHONE_LOOKUP）同上的诚实兜底。
_PHONE_FALLBACK = (
    "号码台账查询暂时不可用，请稍后再试；也可以直接到 ops-web 的"
    "「号码台账」页查询。"
)

#: clarify 选择形态：序号词映射与正则（「1」「第2个」「第一款」「3.」）。
_ORDINALS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8}
_CHOICE_RE = re.compile(r"^第?([1-9一二两三四五六七八])[个款条号]?[。.!！]?$")
#: 候选名片段的口语后缀（「2022那款」「雅致版这个」）。
_CHOICE_SUFFIXES = ("那款", "这款", "那个", "这个", "那瓶", "这瓶")


def _parse_choice(text, candidates):
    """clarify 反问后的选择解析：序号或候选名片段 → 命中候选 dict。

    非选择形态 / 序号越界 / 片段多义（命中多个候选）→ None（交给正常
    路由，不猜）。「第一个」之类此前会被当成新商品名直查必 miss
    （2026-10-23 云上验收撞见）。
    """
    normalized = (text or "").strip()
    if not normalized or not candidates:
        return None
    # 编码精确匹配（clarify 列表每行带编码，用户可直接回编码选中）。
    lowered = normalized.lower()
    for cand in candidates:
        code = (cand.get("code") or "").lower()
        if code and lowered == code:
            return cand
    match = _CHOICE_RE.match(normalized)
    if match:
        token = match.group(1)
        index = int(token) if token.isdigit() else _ORDINALS[token]
        if 1 <= index <= len(candidates):
            return candidates[index - 1]
        return None
    frag = normalized
    for suffix in _CHOICE_SUFFIXES:
        if frag.endswith(suffix):
            frag = frag[: -len(suffix)]
    frag = frag.strip()
    if len(frag) < 2:
        return None
    hits = [cand for cand in candidates if frag in (cand.get("name") or "")]
    return hits[0] if len(hits) == 1 else None


#: 负反馈修正（2026-10-23 对话层第 2 项）：「不是这个，是1988那款」
#: 「不对，是第二个」。前缀词 + 「是X」新指定（X 可带那款/这款后缀）。
_NEGATION_RE = re.compile(
    r"^(?:不是|不对|错了|搞错了?|弄错了?)(?:这个|这款|这瓶|这一个|了|的)?"
    r"[，,。.!！\s]*(?:是|要|想看|问)?(.+?)(?:那款|这款|那个|这个|那瓶|这瓶)?[。.!！]?$"
)


#: 语气词（「不是吧」「不是吗」是否认语气而非负反馈修正，不拦）。
_NEGATION_FALSE_FRIENDS = {"不是吧", "不是吗", "不会吧", "不是吧你"}


def _parse_correction(text):
    """负反馈修正解析。三态：False=非负反馈；None=负反馈但无新指定；
    str=新指定的关键词（「是1988那款」→「1988」）。

    只在 history 非空时由调用方启用（修正必有上文；无上文的「不是…」
    更可能是正常问句，不拦）。
    """
    normalized = (text or "").strip().rstrip("。.!！")
    if normalized in _NEGATION_FALSE_FRIENDS:
        return False
    # 纯负反馈（无新指定）：整串只剩否定词与指示词
    if normalized in ("不是", "不对", "错了", "搞错", "搞错了", "弄错",
                      "弄错了", "不是这个", "不是这款", "不是那个", "不是那款"):
        return None
    match = _NEGATION_RE.match(normalized)
    if not match:
        return False
    keyword = match.group(1).strip(" 的吗呢吧？?！!，,。.")
    # 捕获组回溯吃到指示词（「不是这个」的「这个」）= 无新指定
    if keyword in ("这个", "这款", "这瓶", "那个", "那款", "那瓶", "它"):
        return None
    return keyword if len(keyword) >= 2 else None

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


#: 合并后送给渲染层（LLM/摘录）的切片上限。
_DOC_MERGED_LIMIT = 5


def _merge_doc_hits(bm25_hits, vector_hits, *, limit=_DOC_MERGED_LIMIT):
    """BM25 与向量召回合并去重：BM25 在前（保持现有行为），向量补充。

    两条通路的切片来源不同（kb_doc_chunks 滑窗 vs kb_rag_v1 结构切片），
    id 体系不可比，按 (标题, 内容前 60 字) 去重——同一段话不重复喂 LLM。
    """
    merged = list(bm25_hits or [])
    seen = {_hit_key(chunk) for chunk in merged}
    for chunk in vector_hits or []:
        key = _hit_key(chunk)
        if key in seen:
            continue
        seen.add(key)
        merged.append(chunk)
        if len(merged) >= limit:
            break
    return merged


def _hit_key(chunk):
    return (chunk.get("doc_title") or "",
            (chunk.get("content") or "")[:60])


def _error_location(exc):
    """异常 → ``文件名:行号``（只取代码位置，不含异常原文/参数/数据）。"""
    try:
        import traceback as tb_module

        frames = tb_module.extract_tb(exc.__traceback__)
        if not frames:
            return "unknown"
        frame = frames[-1]
        filename = frame.filename.replace("\\", "/").rsplit("/", 1)[-1]
        return f"{filename}:{frame.lineno}"
    except Exception:
        return "unknown"


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
                 doc_search=None, vector_search=None, context_store=None,
                 agent_runner=None, asset_sender=None, image_search=None):
        super().__init__()
        self._connection_factory = connection_factory
        self._conversation_ids = conversation_ids
        self._now = now or datetime.now
        self._log = log
        self._reply_client = reply_client
        self._robot_code = robot_code
        self._llm = llm
        self._doc_search = doc_search or search_docs
        self._vector_search = vector_search
        self._context_store = context_store
        self._agent_runner = agent_runner
        self._asset_sender = asset_sender
        self._image_search = image_search

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

        # 对话层（2026-10-21）：同人同群的最近几轮历史，供指代/追问理解；
        # context_store 为 None 时行为与单轮完全一致。
        ctx_key = None
        history = None
        if self._context_store is not None:
            sender_id = (getattr(incoming, "sender_staff_id", None)
                         or getattr(incoming, "sender_nick", "") or "")
            ctx_key = self._context_store.key(conversation_id, sender_id)
            history = self._context_store.history(ctx_key)

        conn = self._connection_factory()
        started = time.monotonic()
        try:
            if not self._claim(conn, msg_id, incoming, text, conversation_id):
                self._log("kb qa: duplicate delivery")
                return AckMessage.STATUS_OK, "OK"
            meta = {}
            # clarify 待选择闭环（2026-10-23）：上一条若是多款反问，先取出
            # 候选（消费即作废），本轮是选择形态则 _answer 内直接命中。
            pending = None
            if ctx_key is not None:
                pending = self._context_store.pop_pending(ctx_key)
            # token 成本统计（2026-10-22）：LLM 用量前后快照差值归账到
            # 本条消息（遥测口径）。_RecordingLlm 类替身无 usage_snapshot，
            # getattr 守卫。
            usage_snap = getattr(self._llm, "usage_snapshot", None)
            usage_before = usage_snap() if usage_snap else None
            reply, intent, matched = self._answer(
                conn, text, history=history, conversation_id=conversation_id,
                meta=meta, pending=pending)
            if usage_before is not None:
                usage_after = usage_snap()
                meta["prompt_tokens"] = max(
                    0, usage_after["prompt_tokens"]
                    - usage_before["prompt_tokens"])
                meta["completion_tokens"] = max(
                    0, usage_after["completion_tokens"]
                    - usage_before["completion_tokens"])
            latency_ms = int((time.monotonic() - started) * 1000)
            self._finish_audit(conn, msg_id, intent, matched,
                               "text" if reply else "none",
                               meta=meta, reply=reply, latency_ms=latency_ms)
            # 噪声轮过滤（2026-10-22）：CHITCHAT 轮（HELP 帮助文案等）
            # 不进历史，避免稀释 6 轮窗口里的有效上文。subject=最近命中
            # 商品名，供历史透出「上文涉及商品」（指代补全锚点）。
            if ctx_key is not None and intent != CHITCHAT:
                self._context_store.record(
                    ctx_key, question=text, answer=reply,
                    category=intent, record_id=matched,
                    subject=meta.get("subject"))
            # 本轮发出的是多款反问 → 存候选，等下一条选择消息闭环。
            if ctx_key is not None and meta.get("pending_candidates"):
                self._context_store.set_pending(
                    ctx_key, meta["pending_candidates"])
            conn.commit()
        except Exception as exc:
            conn.rollback()
            # 非泄露约定：记异常类型与位置（文件:行号），不记异常原文/
            # 数据/堆栈详情——2026-10-21 AttributeError 复现不出，需要位置。
            location = _error_location(exc)
            self._log(f"kb qa: answer failed: {type(exc).__name__}"
                      f" at {location}")
            # 反馈闭环：占位审计行已随 rollback 回滚，用新连接补一行
            # failed=1 的失败流水（best-effort，再失败不影响兜底回复）。
            self._mark_failed(msg_id, incoming, text, conversation_id)
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

    def _finish_audit(self, conn, msg_id, intent, matched, reply_kind,
                      meta=None, reply=None, latency_ms=None):
        if not msg_id:
            return
        meta = meta or {}
        tools = meta.get("tools") or []
        cursor = conn.cursor()
        try:
            cursor.execute(
                "UPDATE `kb_qa_audit` SET `intent`=%s, `matched_record_id`=%s,"
                " `reply_kind`=%s, `recall_source`=%s, `tool_used`=%s,"
                " `agent_rounds`=%s, `latency_ms`=%s, `answer_excerpt`=%s,"
                " `prompt_tokens`=%s, `completion_tokens`=%s"
                " WHERE `msg_id`=%s",
                (
                    intent, matched, reply_kind,
                    meta.get("recall_source"),
                    ",".join(tools) or None,
                    meta.get("rounds"),
                    latency_ms,
                    (reply or "")[:500] or None,
                    meta.get("prompt_tokens"),
                    meta.get("completion_tokens"),
                    msg_id,
                ),
            )
        finally:
            cursor.close()

    def _mark_failed(self, msg_id, incoming, text, conversation_id):
        """处理异常后的失败流水补记（新连接，best-effort 不抛）。

        占位审计行已随主事务 rollback 消失；这里独立补一行 failed=1，
        让「answer failed」在 audit 表可统计、可聚类（反馈闭环）。
        """
        if not msg_id:
            return
        try:
            conn = self._connection_factory()
            try:
                cursor = conn.cursor()
                try:
                    cursor.execute(
                        "INSERT INTO `kb_qa_audit`"
                        " (`msg_id`, `conversation_id`, `sender_uid`,"
                        "  `sender_name`, `question`, `failed`, `created_at`)"
                        " VALUES (%s,%s,%s,%s,%s,1,%s)"
                        " ON DUPLICATE KEY UPDATE `failed`=1",
                        (
                            msg_id, conversation_id,
                            getattr(incoming, "sender_staff_id", None),
                            getattr(incoming, "sender_nick", None),
                            text, self._now().strftime("%Y-%m-%d %H:%M:%S"),
                        ),
                    )
                finally:
                    cursor.close()
                conn.commit()
            finally:
                conn.close()
        except Exception:
            self._log("kb qa: failure audit skipped")

    # ------------------------------------------------------------------
    # 问答主流程（纯函数式，便于测试）
    # ------------------------------------------------------------------

    def _answer(self, conn, text, history=None, conversation_id=None,
                meta=None, pending=None):
        """返回 (reply, intent, matched_record_id)。reply=None 表示静默。

        history（对话层）：同人同群最近几轮问答，透传给 LLM 二次解析与
        渲染；None 与单轮行为一致。
        conversation_id：附件直发需要目标群；None 时 ASSET 分支只列清单。
        meta（反馈闭环埋点）：传入 dict 时回填 recall_source/tools/rounds，
        供审计落列；不传零副作用。
        pending（对话层闭环）：上一轮 clarify 反问的候选列表（已被调用方
        消费——本轮说了别的就自然作废）。选择形态命中时直接渲染参数卡，
        不重新分类、不花 LLM。
        """
        if not text:
            return None, CHITCHAT, None
        if pending:
            choice = _parse_choice(text, pending)
            if choice is not None:
                rows = find_products(conn, choice["name"])
                if rows:
                    if meta is not None:
                        meta["recall_source"] = "products"
                        meta["subject"] = rows[0].get("name")
                        # 候选回填（2026-10-23 负反馈配套）：选中出卡后仍可
                        # 改选——「不对，是第二个」下一轮经负反馈分支里的
                        # pending 改选命中。
                        meta["pending_candidates"] = pending
                    reply = self._render_products(
                        text, rows,
                        Intent(PRODUCT_PARAM, choice["name"], "clarify 选择"),
                        history)
                    return reply, PRODUCT_PARAM, rows[0]["record_id"]
            # 非选择形态/解析失败/候选已删：继续往下（pending 已作废语义——
            # 但负反馈分支还会用到它做改选，故这里不 return）。
        correction = _parse_correction(text)
        if correction is not False and history:
            # 负反馈修正（「不是这个，是1988那款」「不对，是第二个」）。
            if correction:
                # 先看是不是对刚才候选的改选（选择命中后候选被回填）。
                if pending:
                    re_choice = _parse_choice(correction, pending)
                    if re_choice is not None:
                        correction = re_choice["name"]
                rows = find_products(conn, correction)
                if rows:
                    if len(rows) > 1:
                        if meta is not None:
                            meta["pending_candidates"] = [
                                {"record_id": row["record_id"],
                                 "name": row.get("name") or "",
                                 "code": row.get("code") or ""}
                                for row in rows
                            ]
                        return (render_clarify(correction, rows),
                                PRODUCT_PARAM, None)
                    if meta is not None:
                        meta["recall_source"] = "products"
                        meta["subject"] = rows[0].get("name")
                    reply = self._render_products(
                        text, rows,
                        Intent(PRODUCT_PARAM, correction, "负反馈修正"),
                        history)
                    return reply, PRODUCT_PARAM, rows[0]["record_id"]
                return (f"「{correction}」也没查到，发我完整品名或编码，"
                        "我精确查一下。"), PRODUCT_PARAM, None
            return ("好的，那你想问的是哪一款？发我品名或编码。"
                    ), PRODUCT_PARAM, None
        intent = classify(text)
        if intent.category in (SALES_QUERY, PHONE_LOOKUP):
            # 业务数据问句（销售额/完成率/排名…）与号码台账问句（尾号/验证
            # 码找谁）直接进工具调用 agent——不先走商品直查：商品表对它必然
            # 全 miss 或被模糊误伤（2026-10-26 实测纯尾号「5579」命中 7 款
            # 579ml 商品进 clarify），白烧 LLM 改写+检索才轮到 agent。
            agent_answer = self._try_agent(conn, text, history, meta=meta)
            if agent_answer:
                return agent_answer, "agent", None
            if intent.category == PHONE_LOOKUP:
                return _PHONE_FALLBACK, PHONE_LOOKUP, None
            return _SALES_FALLBACK, SALES_QUERY, None
        if intent.category in (PRODUCT_PARAM, PRODUCT_FILTER):
            rows = find_products(conn, intent.keyword)
            if not rows:
                # 规则层关键词整词/分词全 miss：有 LLM 时二次解析换关键词重查一次。
                rows = self._reparse_products(conn, text, history)
                if rows and meta is not None:
                    meta["recall_source"] = "products_reparse"
            if not rows:
                # 商品全 miss：可能是业务数据问句（「今天杭州出了多少货」
                # 会被兜底成疑似商品名直查）——给 agent 一次机会，它查
                # 不到再由 LLM 如实说，比干巴巴的「未找到」更有用。
                agent_answer = self._try_agent(conn, text, history, meta=meta)
                if agent_answer:
                    return agent_answer, "agent", None
                return render_not_found(intent.keyword), intent.category, None
            if len(rows) > 1 and intent.category == PRODUCT_PARAM:
                # 多款同名 → 规则反问确认（不猜，不受 LLM 影响）。
                # 候选列表透出给调用方存 pending（2026-10-23 闭环）：用户
                # 下一条回「第一个」「2022那款」可直接命中，不再被当成
                # 新商品名直查。
                if meta is not None:
                    meta["pending_candidates"] = [
                        {"record_id": row["record_id"],
                         "name": row.get("name") or "",
                         "code": row.get("code") or ""}
                        for row in rows
                    ]
                return render_clarify(intent.keyword, rows), intent.category, None
            if meta is not None:
                if "recall_source" not in meta:
                    meta["recall_source"] = "products"
                # 对话层（2026-10-22）：透出命中商品名，作指代追问锚点。
                meta["subject"] = rows[0].get("name")
            reply = self._render_products(text, rows, intent, history)
            return reply, intent.category, rows[0]["record_id"]
        if intent.category == PRODUCT_ASSET:
            rows = find_products(conn, intent.keyword)
            if not rows:
                return render_not_found(intent.keyword), intent.category, None
            if meta is not None:
                meta["recall_source"] = "products"
            # 精度（2026-10-21）：商品相关度与附件字段名联合选择——
            # 「习酒窖藏1988 质检报告」曾命中红习酱的「礼袋」。
            row, field_name, items = pick_asset(rows, text)
            if meta is not None:
                # 对话层（2026-10-22）：透出命中商品名，作指代追问锚点。
                meta["subject"] = (row or rows[0]).get("name")
                # Web 通道（2026-10-23）：附件清单结构化透出，前端据此
                # 渲染下载链接（/kb/api/attachments/... 现取签名 URL）。
                if row is not None and items:
                    meta["asset"] = {
                        "record_id": row["record_id"],
                        "field": field_name,
                        "names": [
                            (m.get("filename") or m.get("name") or "")
                            for m in items
                        ],
                    }
            if row is None:
                # 附件字段没匹配上（如问「包装什么样」而字段名不含包装）——
                # 向量图召回兜底：文字搜图跨字段跨商品找图直发。
                # 强类型词（质检报告/发票/证书）除外：图召回是跨商品的，
                # 发别家产品的证书比答「没有」更糟（2026-10-22 裁决）。
                recalled = None
                if not is_strong_asset_question(text):
                    recalled = self._try_send_recalled_images(
                        conn, text, conversation_id)
                if recalled:
                    return (
                        "给你找到这些商品的相关图片："
                        + "、".join(recalled),
                        intent.category, rows[0]["record_id"],
                    )
                available = [
                    field for field, its
                    in (rows[0].get("attachments") or {}).items() if its
                ]
                if available:
                    return (
                        f"「{rows[0]['name']}」的资料里没有这个附件，"
                        f"现有：{'、'.join(available)}。",
                        intent.category, rows[0]["record_id"],
                    )
                return (
                    f"「{rows[0]['name']}」的资料里还没有附件。",
                    intent.category, rows[0]["record_id"],
                )
            # 直发（2026-10-21）：下载 → 钉钉媒体库 → 群文件/图片消息；
            # 任一步失败降级为列清单，绝不甩异常给客服。
            if (self._asset_sender is not None and conversation_id
                    and self._robot_code):
                try:
                    sent = self._asset_sender.send(
                        conn, row=row, field_name=field_name,
                        conversation_id=conversation_id,
                        robot_code=self._robot_code)
                    if sent:
                        return (
                            f"「{row['name']}」的{field_name}"
                            f"已发送到群里，请查收。",
                            intent.category, row["record_id"],
                        )
                except Exception as exc:
                    # 非泄露约定同款：只记异常类型与代码位置——
                    # 2026-10-22 直发失败落兜底时无任何线索可查。
                    self._log(f"kb qa: asset deliver failed:"
                              f" {type(exc).__name__} at {_error_location(exc)}")
            lines = [f"「{row['name']}」的附件：", _ASSET_PENDING]
            lines.append(f"- {field_name}：{len(items)} 个文件")
            return "\n".join(lines), intent.category, row["record_id"]
        if intent.category == DOC_QA:
            return self._answer_docs(conn, text, history, meta=meta)
        # 规则未命中（含业务数据问句「今天杭州出了多少货」这类规则接不住
        # 的）→ 工具调用 agent：LLM 自主决定查商品/查文档/查业务数据。
        # 闲聊精确词（你好/在吗）直接 HELP，不花 agent 的 LLM 调用。
        if intent.category == CHITCHAT and intent.reason == "未命中任何规则":
            agent_answer = self._try_agent(conn, text, history, meta=meta)
            if agent_answer:
                return agent_answer, "agent", None
        return HELP_TEXT, CHITCHAT, None

    def _try_agent(self, conn, text, history, meta=None):
        """agent loop 入口（fail-open：异常/无答案返回 None 由调用方降级）。"""
        if self._agent_runner is None:
            return None
        try:
            answer = self._agent_runner(conn, text, history, meta=meta)
        except Exception:
            self._log("kb qa: agent failed")
            return None
        # 只有 agent 真正给出答案才记 recall_source=agent（降级走兜底
        # 话术的行不应虚标召回通路，否则 badcase 聚类口径失真）。
        if answer and meta is not None:
            meta.setdefault("recall_source", "agent")
        return answer

    def _try_send_recalled_images(self, conn, question, conversation_id):
        """向量图召回兜底直发（文字搜图）。返回发出的图片对应的商品名
        列表；不可用/无命中/发送失败返回 None（调用方走文本兜底）。

        全部 fail-open：缺配置、向量库异常、下载/上传/发送异常都只记
        日志不外抛——图召回是增强，绝不能拖垮文本兜底。
        """
        if (self._image_search is None or self._asset_sender is None
                or not conversation_id or not self._robot_code):
            return None
        from common.kb_qa.rag_docs import MIN_IMAGE_SCORE

        try:
            hits = self._image_search(question)
        except Exception:
            self._log("kb qa: image recall failed")
            return None
        refs = []
        names = []
        for hit in hits or []:
            if (hit.get("score") or 0) < MIN_IMAGE_SCORE:
                continue
            parts = (hit.get("image_ref") or "").split("/")
            if len(parts) != 3:
                continue
            try:
                index = int(parts[2])
            except ValueError:
                continue
            refs.append((parts[0], parts[1], index))
            names.append(hit.get("name") or "")
        if not refs:
            return None
        try:
            sent = self._asset_sender.send_image_refs(
                conn, refs=refs, conversation_id=conversation_id,
                robot_code=self._robot_code)
        except Exception as exc:
            self._log(f"kb qa: recalled image deliver failed:"
                      f" {type(exc).__name__} at {_error_location(exc)}")
            return None
        return names if sent else None

    # ------------------------------------------------------------------
    # P2：LLM 渲染与手册召回（全部 fail-open，异常只降级不外泄）
    # ------------------------------------------------------------------

    def _render_products(self, question, rows, intent, history=None):
        """命中行后的渲染：有 LLM 先试口语化答案，失败/无 LLM 降 P1 模板。

        ``_answer`` 已拦下「多款 PARAM 反问」，这里只可能是单款 PARAM 或
        FILTER 列表，故降级分支与 P1 完全一致（多行列表 / 单行参数卡）。
        """
        if self._llm is not None:
            try:
                answer = render_product_answer(self._llm, question, rows,
                                               history=history)
                # 推理版模型预算耗尽时返回空串（不抛异常）——空答案等同
                # 失败，降模板渲染，绝不让用户看到静默（2026-10-22 实测）。
                if answer:
                    return answer
                self._log("kb qa: llm product render empty")
            except LlmError:
                self._log("kb qa: llm product render failed")
        if len(rows) > 1:
            return render_filter_answer(rows, intent.keyword)
        return render_param_answer(rows[0])

    def _reparse_products(self, conn, question, history=None):
        """检索全 miss 时的 LLM 二次解析：换关键词重查一次商品。

        history 在场时 LLM 能把「价格呢/那款呢」按上文补全成完整商品名
        （对话层主战场：指代追问的规则 miss → 历史改写 → 命中）。
        """
        if self._llm is None:
            return []
        try:
            _category, keyword = extract_query(self._llm, question,
                                               history=history)
        except LlmError:
            self._log("kb qa: llm query extract failed")
            return []
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        return find_products(conn, keyword)

    def _answer_docs(self, conn, question, history=None, meta=None):
        """手册问答三态：有切片+有 LLM 带出处答案；有切片无 LLM 摘录；无切片兜底。

        召回两路并行：BM25（kb_doc_chunks）打底 + 向量（kb_rag_v1，语义
        召回，问法不同也能命中）合并去重。向量通路 fail-open——缺配/异常
        静默降级纯 BM25，绝不把异常甩给客服。
        meta 埋点：recall_source = mixed/bm25/vector/none（按实际贡献
        命中的通路；向量未配置或异常不计入）。
        """
        chunks = self._doc_search(conn, question, k=3)
        bm25_hit = bool(chunks)
        vector_hit = False
        if self._vector_search is not None:
            try:
                vector_hits = self._vector_search(question)
                vector_hit = bool(vector_hits)
                chunks = _merge_doc_hits(chunks, vector_hits)
            except Exception:
                self._log("kb qa: vector recall failed")
        if meta is not None:
            if bm25_hit and vector_hit:
                meta["recall_source"] = "mixed"
            elif vector_hit:
                meta["recall_source"] = "vector"
            elif bm25_hit:
                meta["recall_source"] = "bm25"
            else:
                meta["recall_source"] = "none"
        if not chunks:
            return _DOC_FALLBACK, DOC_QA, None
        if self._llm is not None:
            try:
                answer = render_doc_answer(self._llm, question, chunks,
                                           history=history)
                # 空串降摘录（同 _render_products 的推理版预算坑）。
                if answer:
                    return answer, DOC_QA, None
                self._log("kb qa: llm doc render empty")
            except LlmError:
                self._log("kb qa: llm doc render failed")
        return _render_doc_excerpt(chunks), DOC_QA, None

    def _safe_reply(self, incoming, text):
        conversation_id = getattr(incoming, "conversation_id", "") or ""
        # 单聊（conversationType=2）无群 openConversationId，
        # groupMessages/send 必失败——走 SDK reply_text（sessionWebhook）。
        # 2026-10-22 单聊验证发现：此前单聊回复必走群通道报错。
        is_single = getattr(incoming, "conversation_type", "") == "2"
        sender_uid = incoming.sender_staff_id or ""
        try:
            if (self._reply_client is not None and conversation_id
                    and not is_single):
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


def build_kb_handler(*, connection_factory, reply_client, robot_code,
                     conversation_ids, log=print):
    """KbStreamHandler 装配（2026-10-24 机器人合并抽取）。

    从 main 抽出供两处复用：独立 kb-gateway 进程（回退路径）与
    dingtalk-gateway 合并进程（RoutedStreamHandler 的 kb 侧）。
    llm/向量/图召回/上下文/agent/附件直发全部按 env 装配，fail-open
    降级语义与原 main 逐字一致——两份调用共享一份装配，杜绝漂移。
    """
    # P2：LLM 未配置（KB_LLM_* 缺失）或模型未开通时 from_environment 返回
    # None，handler 按「无 LLM」降级 P1 模板运行，不影响 P1 既有能力。
    llm = from_environment()
    if llm is None:
        log("kb qa: LLM not configured, running in P1 template mode")
    # 向量召回（2026-10-20，rag 第三期接线）：RAG_EMBED_* 未配或 provider
    # 不支持时 from_environment 返回 None，纯 BM25 运行；配好即生效。
    from common.kb_qa import rag_docs

    vector_search = rag_docs.from_environment()
    if vector_search is None:
        log("kb qa: vector recall not configured, running BM25 only")
    # 文字搜图（2026-10-22）：kind=image 图召回，附件字段 miss 时兜底直发；
    # 同一组 RAG_EMBED_* env，缺配返回 None 关闭。
    image_search = rag_docs.image_search_from_environment()
    if image_search is None:
        log("kb qa: image recall not configured")
    # 对话层（2026-10-21）：会话上下文（同人同群最近 6 轮 / TTL 10
    # 分钟）。2026-10-22 Redis 化：app.env 有 KB_REDIS_URL /
    # PUBLIC_DATA_REDIS_URL 时走 Redis（原生 TTL 自动过期、键不残留、
    # 重启不丢），否则进程内兜底；fail-open，Redis 不可达只当轮无历史。
    from common.kb_qa.context import build_conversation_store

    context_store = build_conversation_store()
    log(f"kb qa: context backend = {type(context_store).__name__}")

    # 工具调用 agent（2026-10-21）：规则 miss 的消息由 LLM 自主决定查
    # 商品/查文档/查业务数据（agg_offline_daily / fact_channel_daily_sales
    # 预定义只读查询，白名单参数）。LLM 未配置则不启用（规则兜底照旧）。
    agent_runner = None
    if llm is not None:
        from common.kb_qa.agent import ToolExecutor, run_agent

        def agent_runner(conn, text, history, meta=None):
            executor = ToolExecutor(
                conn, doc_search=search_docs, vector_search=vector_search)
            return run_agent(llm, text, executor=executor, history=history,
                             meta=meta)
    else:
        log("kb qa: agent disabled (no LLM)")

    # 附件直发（2026-10-21）：AI 表签名 URL → 下载 → 钉钉媒体库 → 群
    # 文件/图片消息；env 缺失则关闭（ASSET 分支降级列清单）。
    from common.kb_qa import assets

    asset_sender = assets.build_from_environment()
    if asset_sender is None:
        log("kb qa: asset delivery disabled (missing env)")

    return KbStreamHandler(
        connection_factory=connection_factory,
        conversation_ids=conversation_ids,
        reply_client=reply_client,
        robot_code=robot_code,
        llm=llm,
        vector_search=vector_search,
        context_store=context_store,
        agent_runner=agent_runner,
        asset_sender=asset_sender,
        image_search=image_search,
    )


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
    handler = build_kb_handler(
        connection_factory=lambda: db.connect(settings.mart_database),
        reply_client=reply_client,
        robot_code=app_key,
        conversation_ids=allowlist,
    )
    client = build_stream_client(app_key, app_secret, handler)
    client.start_forever()


if __name__ == "__main__":
    main()
