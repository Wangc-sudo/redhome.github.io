# -*- coding: utf-8 -*-
"""kb_web 环境装配：bi-web main 的依赖构建（fail-open，缺配=None）。

所需 env（全部已在 app.env 现成，bi-web 经 env_file 共享）：
- BI_WEB_SESSION_SECRET   session 签名密钥（kb 与看板同一 cookie 体系）
- KB_DINGTALK_APP_KEY/SECRET/OPERATOR_ID   小易应用凭据（免登交换+取记录）
- KB_PRODUCTS_BASE_ID/SHEET_ID             商品 AI 表定位（附件签名 URL）
- KB_LLM_* / RAG_EMBED_*                   LLM 与向量召回（缺则降级运行）
- KB_REDIS_URL / PUBLIC_DATA_REDIS_URL     会话存储（缺则进程内兜底）
- KB_WEB_PUBLIC_BASE_URL                   扫码登录回调的公网基址
                                           （缺省 https://dt.yyhlzj.com）
"""

import os

from common.bi_web import auth
from common.kb_web.acl import BusinessAcl
from common.kb_web.service import KbWebService
from common.kb_web.sessions import WebSessionStore


def _export_authorizer(member_checker):
    """将登录在册检查适配为导出授权的双参数契约。"""
    def authorize(userid, _conversation_id):
        return member_checker(userid)
    return authorize


def build_kb_web_deps(settings, environ=None):
    """返回 register_kb_routes 的 kwargs；缺关键配置返回 None。"""
    env = os.environ if environ is None else environ

    # kb 专用密钥优先：BI_WEB_SESSION_SECRET 一旦设置会翻转看板页面的
    # 认证重定向行为（redirect_pages），榜单页不能动——kb 用独立密钥
    # 隔离该副作用（同一 cookie 体系，仅 /kb 路由消费）。
    session_secret = ((env.get("KB_WEB_SESSION_SECRET") or "").strip()
                      or (env.get("BI_WEB_SESSION_SECRET") or "").strip())
    app_key = (env.get("KB_DINGTALK_APP_KEY") or "").strip()
    app_secret = (env.get("KB_DINGTALK_APP_SECRET") or "").strip()
    operator_id = (env.get("KB_DINGTALK_OPERATOR_ID") or "").strip()
    if not (session_secret and app_key and app_secret):
        return None

    from common.kb_qa import rag_docs
    from common.kb_qa.docs import search_docs
    from common.kb_qa.llm import from_environment as llm_from_env
    from common.public_data import db

    def connection_factory():
        return db.connect(settings.mart_database)

    redis_url = ((env.get("KB_REDIS_URL") or "").strip()
                 or (env.get("PUBLIC_DATA_REDIS_URL") or "").strip()
                 or None)

    record_fetcher = None
    base_id = (env.get("KB_PRODUCTS_BASE_ID") or "").strip()
    sheet_id = (env.get("KB_PRODUCTS_SHEET_ID") or "").strip()
    if operator_id and base_id and sheet_id:
        from common.dingtalk.client import DingTalkClient

        client = DingTalkClient(app_key, app_secret, operator_id)

        def record_fetcher(record_id):
            return client.get_record(base_id, sheet_id, record_id)

    def member_name_lookup(userid):
        conn = connection_factory()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT `name` FROM `dim_robot_member`"
                    " WHERE `user_id`=%s",
                    (userid,),
                )
                row = cursor.fetchone()
                return row["name"] if row else None
            finally:
                cursor.close()
        finally:
            conn.close()

    def member_checker(userid):
        """扫码登录在册校验（fail-closed：查询异常由路由层按拒绝处理）。"""
        conn = connection_factory()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT 1 FROM `dim_robot_member` WHERE `user_id`=%s"
                    " LIMIT 1",
                    (userid,),
                )
                return cursor.fetchone() is not None
            finally:
                cursor.close()
        finally:
            conn.close()

    from common.kb_web.qr_login import DingTalkQrLoginClient, OAuthStateStore

    public_base = ((env.get("KB_WEB_PUBLIC_BASE_URL") or "").strip()
                   or "https://dt.yyhlzj.com").rstrip("/")

    export_service = None
    try:
        from common.kb_qa.query_export import ExportService

        export_service = ExportService.from_env(connection_factory, environ=env)
        if not export_service.enabled:
            export_service = None
    except (ValueError, OSError):
        pass

    return {
        "service": KbWebService(
            connection_factory=connection_factory,
            llm=llm_from_env(),
            doc_search=search_docs,
            vector_search=rag_docs.from_environment(),
            acl=BusinessAcl(connection_factory),
        ),
        "store": WebSessionStore(redis_url),
        "session_secret": session_secret,
        "auth_client": auth.DingTalkAuthClient(app_key, app_secret),
        "record_fetcher": record_fetcher,
        "member_name_lookup": member_name_lookup,
        "session_secure": (env.get("BI_WEB_SESSION_SECURE", "")
                           .strip().lower() in ("1", "true", "yes")),
        # 免登前端 corpId（与 BI 应用同 Corp，两个变量名都认）。
        "corp_id": ((env.get("KB_DINGTALK_CORPID") or "").strip()
                    or (env.get("BI_DINGTALK_CORPID") or "").strip()
                    or None),
        # 扫码登录（2026-10-26）：与免登同凭据，回调路径已在 /kb/ 反代内。
        "qr_client": DingTalkQrLoginClient(app_key, app_secret),
        "state_store": OAuthStateStore(redis_url),
        "qr_redirect_uri": f"{public_base}/kb/api/auth/dingtalk/callback",
        "member_checker": member_checker,
        "export_service": export_service,
        "export_authorizer": _export_authorizer(member_checker),
    }
