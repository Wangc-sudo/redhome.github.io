# -*- coding: utf-8 -*-
"""小易 Web 问答服务：复用 kb_qa 的 _answer 纯函数主链路。

Web 通道只是新调用方——规则路由/商品/手册/agent/token 统计原样生效；
差异点只有三个：
1. 历史来自 Web 会话存储（最近 6 轮，与群内同轮数不同生命周期）；
2. agent 工具按角色白名单收窄（acl.BusinessAcl，经 run_agent tools
   参数在 offering 层收窄——越权工具根本不出现在协议面）；
3. 附件不直发钉钉，走 meta.asset 透出 → 前端渲染 /kb/api/attachments 链接。

并发纪律：handler 每请求构造（仅对象分配），userid 经闭包进
agent_runner，不存共享可变状态。
"""

import time
import uuid

from common.kb_qa.context import DEFAULT_MAX_TURNS

#: 业务数据工具名（白名单外用户不可见）。
_BUSINESS_TOOLS = frozenset(
    ("daily_sales", "channel_sales", "channel_yoy", "query_sql"))


#: 历史卫生（2026-10-30 对话状态层）：进入 LLM prompt 的轮次时间窗。
#: 会话本身 7 天滑动（用户期待回来接着聊），但昨天的旧话题不该参与
#: 今天的指代/确认理解——此前「昨天下午的质检报告」污染「今天上午的
#: 销售排名确认」事故即因此。45 分钟覆盖一个 Web 工作话题的自然寿命。
HISTORY_TTL_SECONDS = 45 * 60

#: 噪声轮意图（与群内 record 的 CHITCHAT 过滤同口径）：不进历史，
#: 避免「你好/你能做什么」稀释有限轮次窗口、白白烧 token。
_NOISE_INTENTS = frozenset(("CHITCHAT", "HELP"))

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _parse_message_ts(value):
    """消息 ts（本地时间字符串）→ epoch；缺失/异常返回 None（不过滤）。"""
    if not value:
        return None
    try:
        return time.mktime(time.strptime(value, _TS_FORMAT))
    except (TypeError, ValueError, OverflowError):
        return None


def web_history_from_messages(messages, *, max_turns=DEFAULT_MAX_TURNS,
                              now=None, ttl_seconds=HISTORY_TTL_SECONDS):
    """Web 会话消息列表 → kb_qa 对话层 history 形态（最近 max_turns 轮）。

    配对规则：user 消息与其后第一条 assistant 消息成一轮；不成对的
    （如末尾刚发的 user）不进历史。
    卫生规则（2026-10-30）：噪声轮（CHITCHAT/HELP，经 assistant meta.intent
    识别，存量无 intent 的消息不受影响）与超出时间窗的旧轮次不进历史；
    ts 缺失/不可解析的轮次保留（兼容存量，不过滤）。
    """
    now = time.time() if now is None else now
    turns = []
    pending = None
    for msg in messages or []:
        role = msg.get("role")
        if role == "user":
            pending = msg.get("content")
        elif role == "assistant" and pending is not None:
            turns.append({
                "q": pending,
                "a": msg.get("content") or "",
                "intent": (msg.get("meta") or {}).get("intent"),
                "ts": _parse_message_ts(msg.get("ts")),
            })
            pending = None
    cleaned = []
    for turn in turns:
        if turn["intent"] in _NOISE_INTENTS:
            continue
        if (ttl_seconds and turn["ts"] is not None
                and now - turn["ts"] > ttl_seconds):
            continue
        cleaned.append({"q": turn["q"], "a": turn["a"]})
    return cleaned[-max_turns:]


def tools_for(allow_business):
    """工具 offering 子集：非白名单用户只给商品/文档两工具。"""
    from common.kb_qa.agent import TOOLS

    if allow_business:
        return TOOLS
    return [t for t in TOOLS
            if t.get("function", {}).get("name") not in _BUSINESS_TOOLS]


class KbWebService:
    """Web 问答：构造一次，逐请求 answer。依赖全注入，测试零 IO。"""

    def __init__(self, *, connection_factory, llm=None, doc_search=None,
                 vector_search=None, acl=None, log=print, now=time.monotonic):
        self._connection_factory = connection_factory
        self._llm = llm
        self._doc_search = doc_search
        self._vector_search = vector_search
        self._acl = acl
        self._log = log
        self._now = now

    def _build_handler(self, userid):
        from common.kb_qa.agent import ToolExecutor, run_agent
        from common.kb_qa.app import KbStreamHandler

        llm = self._llm
        # 2026-10-24 用户裁决「解除业务限制，客服也能查销售量」：
        # 业务数据工具不再按角色白名单收窄，Web 与群内同权。
        # acl 模块留仓备用（未来如需再收紧，恢复本行判定即可）：
        # allow_business = bool(self._acl and self._acl.is_allowed(userid))
        allow_business = True

        def agent_runner(conn, text, history, meta=None):
            executor = ToolExecutor(
                conn, doc_search=self._doc_search,
                vector_search=self._vector_search)
            return run_agent(
                llm, text, executor=executor, history=history, meta=meta,
                tools=tools_for(allow_business))

        return KbStreamHandler(
            connection_factory=self._connection_factory,
            log=self._log,
            llm=llm,
            doc_search=self._doc_search,
            vector_search=self._vector_search,
            context_store=None,       # Web 历史由会话存储供，不走群内上下文
            agent_runner=agent_runner if llm is not None else None,
        )

    def answer(self, userid, content, *, history_messages, pending=None,
               offer=None, audit=None):
        """主入口：返回 {reply,intent,meta,attachments,pending_candidates,
        offer}；永不抛（降级话术）。

        pending（2026-10-23 clarify Web 接线）：上一轮反问的候选列表，
        由调用方从会话存储 pop（消费即作废）传入；本轮若再发反问，
        返回值的 pending_candidates 非空，调用方负责存回。
        offer（2026-10-30 对话状态层）：上一轮助手提议（{question,
        action}，同 pending 消费语义传入）。本轮是纯确认词时，把提议
        改写成显式任务文本再进问答链路——确认词不再由 LLM 在历史里
        自由猜锚点。返回值的 offer 是本轮回复末尾的新提议（无则
        None），调用方负责存回；审计 question 始终记用户原文。
        audit（2026-10-26 审计补齐）：传 {"sid": ...} 时把本条问答写进
        kb_qa_audit（与群内同表同口径，badcase/token 运营统计两通道
        合一）；None 跳过。msg_id 合成 web:{sid}:{uuid}，天然无重复
        投递问题（发送幂等在路由层）。
        """
        from common.kb_qa.offers import (
            build_confirmed_text, detect_offer, parse_ack)

        started = self._now()
        meta = {}
        failed = False
        matched = None
        history = web_history_from_messages(history_messages)
        # 截断判定：全量配对数 > 实际带入轮数（上限 DEFAULT_MAX_TURNS）——
        # 前端据此提示「仅参考最近 N 轮」（2026-10-26 上下文可见性）。
        total_turns = len(web_history_from_messages(
            history_messages, max_turns=len(history_messages or []) or 1))
        # 提议确认短路：「可以」= 执行上一轮提议，而非新任务。
        answer_text = content
        if offer and parse_ack(content) == "confirm":
            answer_text = build_confirmed_text(
                offer, history[-1]["q"] if history else "")
        handler = self._build_handler(userid)
        conn = self._connection_factory()
        # token 归账（同 kb_qa.app 模式）：usage_snapshot 前后差值归本条
        # 消息（遥测口径非计费真源）；llm 缺省/替身无此方法则为 None。
        usage_snap = getattr(self._llm, "usage_snapshot", None)
        usage_before = usage_snap() if callable(usage_snap) else None
        try:
            reply, intent, matched = handler._answer(
                conn, answer_text, history=history, conversation_id=None,
                meta=meta, pending=pending)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            failed = True
            self._log(f"kb web: answer failed: {type(exc).__name__}")
            reply, intent = "查询出了点问题，请稍后再试。", "error"
        finally:
            conn.close()
        latency_ms = int((self._now() - started) * 1000)

        tokens = None
        if usage_before is not None:
            try:
                usage_after = usage_snap()
                tokens = {
                    "prompt": max(0, usage_after["prompt_tokens"]
                                  - usage_before["prompt_tokens"]),
                    "completion": max(0, usage_after["completion_tokens"]
                                      - usage_before["completion_tokens"]),
                }
            except Exception:
                tokens = None

        attachments = []
        asset = meta.get("asset")
        if asset:
            for index, name in enumerate(asset.get("names") or []):
                attachments.append({
                    "name": name or f"附件{index + 1}",
                    "url": (f"/kb/api/attachments/{asset['record_id']}"
                            f"/{asset['field']}/{index}"),
                })
        export_snapshot_id = None
        if audit:
            export_snapshot_id = self._write_audit(
                audit, userid=userid, question=content,
                reply=None if failed else reply,
                intent=intent, matched=matched, meta=meta,
                tokens=tokens, latency_ms=latency_ms, failed=failed,
            )
        return {
            "reply": reply or "",
            "intent": intent,
            "meta": {
                "tools": meta.get("tools") or [],
                "rounds": meta.get("rounds"),
                "latency_ms": latency_ms,
                "history_rounds": len(history),
                "history_truncated": total_turns > len(history),
                "tokens": tokens,
                "export_snapshot_id": export_snapshot_id,
            },
            "attachments": attachments,
            # clarify 闭环状态（路由层消费，不透给前端）。
            "pending_candidates": meta.get("pending_candidates"),
            # 助手提议状态（同上路由层消费）：本轮回复末尾的提议。
            "offer": detect_offer(reply or ""),
        }

    def _write_audit(self, audit, *, userid, question, reply, intent,
                     matched, meta, tokens, latency_ms, failed):
        """Web 问答审计落 kb_qa_audit（与群内同表同列语义）。

        新连接 + best-effort：写失败只记日志，绝不影响已产生的答复。
        失败行 answer_excerpt 置 NULL（同群内 _mark_failed 口径——兜底
        话术不算答案，badcase 聚类按 failed 列统计）。
        """
        sid = audit.get("sid") or ""
        msg_id = f"web:{sid}:{uuid.uuid4().hex[:12]}"
        snapshot_id = uuid.uuid4().hex if meta.get("query_snapshots") else None
        try:
            conn = self._connection_factory()
            try:
                cursor = conn.cursor()
                try:
                    cursor.execute(
                        "INSERT INTO `kb_qa_audit`"
                        " (`msg_id`, `conversation_id`, `sender_uid`,"
                        "  `question`, `intent`, `matched_record_id`,"
                        "  `reply_kind`, `recall_source`, `tool_used`,"
                        "  `agent_rounds`, `latency_ms`, `answer_excerpt`,"
                        "  `prompt_tokens`, `completion_tokens`, `failed`,"
                        "  `created_at`)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                        "        %s,%s)",
                        (
                            msg_id, f"web:{sid}", userid, question, intent,
                            matched, "text" if reply else "none",
                            meta.get("recall_source"),
                            ",".join(meta.get("tools") or []) or None,
                            meta.get("rounds"), latency_ms,
                            (reply or "")[:500] or None,
                            (tokens or {}).get("prompt"),
                            (tokens or {}).get("completion"),
                            1 if failed else 0,
                            time.strftime("%Y-%m-%d %H:%M:%S"),
                        ),
                    )
                finally:
                    cursor.close()
                if snapshot_id and not failed:
                    from common.kb_qa.query_export import QuerySnapshotRepository

                    QuerySnapshotRepository(conn).save(
                        snapshot_id=snapshot_id,
                        msg_id=msg_id,
                        sender_uid=userid,
                        conversation_id=f"web:{sid}",
                        payload={
                            "version": 1,
                            "items": meta["query_snapshots"],
                        },
                    )
                else:
                    snapshot_id = None
                conn.commit()
                return snapshot_id
            finally:
                conn.close()
        except Exception:
            self._log("kb web: audit skipped")
            return None
