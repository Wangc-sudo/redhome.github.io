"""ops-web（M2b 权限管理）离线单测。

钉死铁律 8（双闸之应用层闸）与铁律 3 的写面归属：
* 无 session / 非 admin / 解析异常 —— 页面 302 提示页、API 401/403，
  权限故障一律 fail-closed；
* grant/revoke/批量区域开通全部经 bi_authz 写路径，actor 来自 session，
  逐行落 audit；
* 页面插值全部转义（管理页无反射面）。

全部 TestClient + FakeConnection：不联网、不碰凭据。
"""


import re


import unittest


import warnings


from contextlib import contextmanager


from datetime import date, datetime


from types import SimpleNamespace


from unittest import mock


warnings.filterwarnings(
    "ignore", message="Using `httpx` with `starlette.testclient` is deprecated"
)


from fastapi.testclient import TestClient


from common.bi_web import auth, authz


from common.ops_web.app import (
    ALL_CAPABILITIES, capability_allows, capability_has_scoped, create_app,
)


from common.public_data import (
    bi_authz, calendar_store, channel_target, fleet_audit, ops_control,
    org_catalog, phone_registry, recipient_override, report_roster,
)


from common.public_data.pipeline_config import StaticConfigSource


from tests.common.test_bi_web_app import _fake_settings


SECRET = "ops-test-secret"


ADMIN_USERID = "014341566058939427"


def _admin_viewer():
    return authz.Viewer(
        userid=ADMIN_USERID, name="王城", scopes=frozenset({"fin"}),
        allowed_regions=frozenset(), is_admin=True, admitted=True,
    )


class _StaticViewerResolver:
    def __init__(self, viewer=None, error=None):
        self._viewer = viewer
        self._error = error

    def resolve(self, userid):
        if self._error is not None:
            raise self._error
        if self._viewer is not None and self._viewer.userid == userid:
            return self._viewer
        return authz.denied_viewer(userid)


class _FakeAuthClient:
    def __init__(self, userid=ADMIN_USERID):
        self._userid = userid

    def exchange_auth_code(self, auth_code):
        return self._userid


class _Cursor:
    """按最近一条 SQL 的关键字服务行，同时记录全部执行。"""

    def __init__(self, rows_by_keyword):
        self._rows_by_keyword = rows_by_keyword
        self.executed = []
        self._last = ""
        self.rowcount = 1

    # 真实 pymysql cursor 支持 with；fake 必须同样支持，否则生产代码里
    # 的 with connection.cursor() 在测试中会 TypeError。
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last = sql

    def _rows(self):
        for keyword, rows in self._rows_by_keyword.items():
            if keyword in self._last:
                return list(rows)
        return []

    def fetchall(self):
        return self._rows()

    def fetchone(self):
        rows = self._rows()
        return rows[0] if rows else None

    def close(self):
        pass


class _Connection:
    def __init__(self, rows_by_keyword=None):
        self.cursor_instance = _Cursor(rows_by_keyword or {})
        self.commits = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def _connector(connection=None, error=None):
    connection = connection if connection is not None else _Connection()

    @contextmanager
    def connector():
        if error is not None:
            raise error
        yield connection

    connector.connection = connection
    return connector


_MEMBERS = [
    ("u-zhang", "张三", "hangzhou", "门店一部"),
    ("u-li", "李四", "hq", "总经办"),
    ("u-wang", "王五", "hangzhou", "门店二部"),
]


def _app(*, viewer=None, resolver_error=None, connection=None,
         connector_error=None, auth_client=None):
    return create_app(
        settings=_fake_settings(),
        session_secret=SECRET,
        db_connector=_connector(connection, connector_error),
        auth_client=auth_client,
        viewer_resolver=_StaticViewerResolver(viewer, resolver_error),
    )


def _login(client, userid=ADMIN_USERID):
    client.cookies.set(
        auth.SESSION_COOKIE, auth.issue_session(userid, SECRET)
    )


def assert_spa_shell(resp):
    """断言响应是 SPA 单壳（dist/index.html）。"""
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert '<div id="app">' in body
    assert "/static/dist/assets/index-" in body


class _RecordingPublisher:
    """config_publisher 替身：记录每条发布；可注入异常模拟 Nacos 故障。"""

    def __init__(self, error=None):
        self.published = []
        self._error = error

    def __call__(self, config):
        if self._error is not None:
            raise self._error
        self.published.append(config)


class _PipelineStore:
    """ops_control 读写函数的插桩替身集合（记录调用、回灌既定值）。"""

    def __init__(self):
        self.audit = []          # (actor, action, service_id, detail)
        self.inserted = []       # (service_id, requested_by)
        self.pending = False     # has_pending_request 的应答
        self.recent = []         # fetch_recent_requests 的应答
        self.audit_rows = []     # fetch_pipeline_audit 的应答
        self.history_rows = []   # fetch_run_history 的应答

    def patch_audit(self):
        return mock.patch.object(
            ops_control, "insert_pipeline_audit",
            lambda conn, actor, action, sid, detail="":
            self.audit.append((actor, action, sid, detail)))

    def patch_pending(self):
        return mock.patch.object(
            ops_control, "has_pending_request", lambda conn, sid: self.pending)

    def patch_insert(self):
        return mock.patch.object(
            ops_control, "insert_run_request",
            lambda conn, sid, by, note="":
            self.inserted.append((sid, by)) or 42)

    def patch_recent(self):
        return mock.patch.object(
            ops_control, "fetch_recent_requests",
            lambda conn, limit=100: tuple(self.recent))

    def patch_audit_rows(self):
        return mock.patch.object(
            ops_control, "fetch_pipeline_audit",
            lambda conn, limit=200: tuple(self.audit_rows))

    def patch_history_rows(self):
        return mock.patch.object(
            ops_control, "fetch_run_history",
            lambda conn, limit=200, service_id=None:
            tuple(r for r in self.history_rows
                  if service_id is None or r["service_id"] == service_id))


class _RecordingDeleter:
    """config_deleter 替身：记录每次删除；可注入异常模拟 Nacos 故障。"""

    def __init__(self, error=None):
        self.deleted = []
        self._error = error

    def __call__(self, service_id):
        if self._error is not None:
            raise self._error
        self.deleted.append(service_id)


def _pipeline_app(*, viewer=None, fleet=("robot-hangzhou",), publisher=None,
                  deleter=None, configs=None,
                  connection=None, connector_error=None):
    if configs is None:
        configs = {"robot-hangzhou": {"schedule": "0 2 * * *", "enabled": True}}
    return create_app(
        settings=_fake_settings(),
        session_secret=SECRET,
        db_connector=_connector(connection, connector_error),
        viewer_resolver=_StaticViewerResolver(viewer),
        fleet_source=lambda: fleet,
        config_source=StaticConfigSource(configs),
        config_publisher=publisher,
        config_deleter=deleter,
    )


def _fleet_app(*, viewer=None, fleet=("robot-hangzhou",), configs=None,
               connection=None):
    """带舰队清单的 ops-web（名册页反向索引 P3 测试用）。"""
    return create_app(
        settings=_fake_settings(),
        session_secret=SECRET,
        db_connector=_connector(connection),
        viewer_resolver=_StaticViewerResolver(viewer),
        fleet_source=lambda: fleet,
        config_source=StaticConfigSource(configs or {}),
    )


class _BrokenConfigSource:
    """config_source 替身：读注册表即抛（模拟 Nacos 读取故障）。"""

    def get_pipeline(self, service_id):
        raise RuntimeError("registry down")


class _RosterStore:
    """report_roster 读写函数的插桩替身集合（填报名册页/API 离线测）。"""

    def __init__(self):
        self.rows = []
        self.audit_rows = []
        self.upserted = []       # (entry, actor)
        self.toggled = []        # (id, enabled, actor)
        self.deleted = []        # (id, actor)
        self.hit = True          # toggle/delete 是否命中

    def patch_fetch(self):
        return mock.patch.object(
            report_roster, "fetch_roster", lambda conn, scope=None:
            tuple(self.rows))

    def patch_audit(self):
        return mock.patch.object(
            report_roster, "fetch_roster_audit", lambda conn, limit=200:
            tuple(self.audit_rows))

    def patch_upsert(self):
        return mock.patch.object(
            report_roster, "upsert_roster_entry",
            lambda conn, entry, *, actor: self.upserted.append((entry, actor)))

    def patch_toggle(self):
        return mock.patch.object(
            report_roster, "set_roster_enabled",
            lambda conn, rid, enabled, *, actor:
            self.toggled.append((rid, enabled, actor)) or self.hit)

    def patch_delete(self):
        return mock.patch.object(
            report_roster, "delete_roster_entry",
            lambda conn, rid, *, actor:
            self.deleted.append((rid, actor)) or self.hit)


def _roster_row(**kw):
    row = {
        "id": 1, "scope": "qudao", "entity_type": "store",
        "entity_key": "JD购喝", "person_name": "饶佳君",
        "aliases": '["小饶"]', "enabled": 1, "note": "共管",
        "updated_by": "admin", "updated_at": "2026-10-06 11:00:00",
    }
    row.update(kw)
    return row


def _roster_manager_viewer():
    """持 scope='roster' 授权的非 admin 成员（名册管理员）。"""
    return authz.Viewer(
        userid="u-manager", name="卢雅玲", scopes=frozenset({"roster"}),
        allowed_regions=frozenset(), is_admin=False, admitted=True,
    )


def _plain_member_viewer():
    return authz.Viewer(
        userid="u-plain", name="路人甲", scopes=frozenset({"fin"}),
        allowed_regions=frozenset(), is_admin=False, admitted=True,
    )


class _OrgCursor:
    """组织编排端点假游标：按 SQL 关键字路由行，可设 rowcount 与故障关键字。"""

    def __init__(self, conn):
        self._conn = conn
        self._last = ""
        self._rows = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        self._last = sql
        for keyword in self._conn.fail:
            if keyword in sql:
                raise RuntimeError("db down")
        self._rows = self._conn.route(sql, params)
        if sql.lstrip().upper().startswith(("UPDATE", "DELETE")):
            self.rowcount = self._conn.rowcount
        else:
            self.rowcount = len(self._rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _OrgConn:
    def __init__(self, rows_by_keyword=None, *, rowcount=1, fail=()):
        self.rows = rows_by_keyword or {}
        self.rowcount = rowcount
        self.fail = set(fail)
        self.executed = []
        self.commits = 0

    def route(self, sql, params=None):
        for keyword, rows in self.rows.items():
            if keyword in sql:
                if callable(rows):
                    return rows(sql, params)
                return list(rows)
        return []

    def cursor(self):
        return _OrgCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


_ORG_OVERRIDE_ROW = {
    "id": 1, "user_id": "u-wang", "name": "王五", "region": "hq",
    "dept_id": None, "dept_name": "总经办", "enabled": 1, "note": None,
    "updated_by": "ops", "updated_at": None,
}

__all__ = tuple(name for name in globals() if not name.startswith("__"))
