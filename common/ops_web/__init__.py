"""ops-web：数字化运维系统（设计稿 2026-09-21 §3，权限管理为其第一个功能）。

独立 FastAPI 服务，网络层仅回环绑定 + 应用层 admin scope 校验双闸；
是 ``bi_authz_grant`` / ``bi_authz_grant_audit`` 的唯一写入方（bi-web
只读不破）。认证/session 代码全部复用 :mod:`common.bi_web.auth` 与
:mod:`common.bi_web.authz`，零复制。
"""
