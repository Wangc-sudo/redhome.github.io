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

from common.kb_qa.context import DEFAULT_MAX_TURNS

#: 业务数据工具名（白名单外用户不可见）。
_BUSINESS_TOOLS = frozenset(("daily_sales", "channel_sales", "query_sql"))


def web_history_from_messages(messages, *, max_turns=DEFAULT_MAX_TURNS):
    """Web 会话消息列表 → kb_qa 对话层 history 形态（最近 max_turns 轮）。

    配对规则：user 消息与其后第一条 assistant 消息成一轮；不成对的
    （如末尾刚发的 user）不进历史。
    """
    turns = []
    pending = None
    for msg in messages or []:
        role = msg.get("role")
        if role == "user":
            pending = msg.get("content")
        elif role == "assistant" and pending is not None:
            turns.append({"q": pending, "a": msg.get("content") or ""})
            pending = None
    return turns[-max_turns:]


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
        allow_business = bool(self._acl and self._acl.is_allowed(userid))

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

    def answer(self, userid, content, *, history_messages):
        """主入口：返回 {reply,intent,meta,attachments}；永不抛（降级话术）。"""
        started = self._now()
        meta = {}
        history = web_history_from_messages(history_messages)
        handler = self._build_handler(userid)
        conn = self._connection_factory()
        try:
            reply, intent, _matched = handler._answer(
                conn, content, history=history, conversation_id=None,
                meta=meta)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            self._log(f"kb web: answer failed: {type(exc).__name__}")
            reply, intent = "查询出了点问题，请稍后再试。", "error"
        finally:
            conn.close()
        latency_ms = int((self._now() - started) * 1000)

        attachments = []
        asset = meta.get("asset")
        if asset:
            for index, name in enumerate(asset.get("names") or []):
                attachments.append({
                    "name": name or f"附件{index + 1}",
                    "url": (f"/kb/api/attachments/{asset['record_id']}"
                            f"/{asset['field']}/{index}"),
                })
        return {
            "reply": reply or "",
            "intent": intent,
            "meta": {
                "tools": meta.get("tools") or [],
                "rounds": meta.get("rounds"),
                "latency_ms": latency_ms,
            },
            "attachments": attachments,
        }
