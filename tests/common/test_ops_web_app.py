"""ops-web（M2b 权限管理）离线单测。

钉死铁律 8（双闸之应用层闸）与铁律 3 的写面归属：
* 无 session / 非 admin / 解析异常 —— 页面 302 提示页、API 401/403，
  权限故障一律 fail-closed；
* grant/revoke/批量区域开通全部经 bi_authz 写路径，actor 来自 session，
  逐行落 audit；
* 页面插值全部转义（管理页无反射面）。

全部 TestClient + FakeConnection：不联网、不碰凭据。
"""

import unittest
import warnings
from contextlib import contextmanager
from unittest import mock

warnings.filterwarnings(
    "ignore", message="Using `httpx` with `starlette.testclient` is deprecated"
)

from fastapi.testclient import TestClient

from common.bi_web import auth, authz
from common.ops_web.app import create_app
from common.public_data import ops_control
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


class FactoryTests(unittest.TestCase):
    def test_session_secret_is_mandatory(self):
        with self.assertRaises(ValueError):
            create_app(settings=_fake_settings(), session_secret=None,
                       viewer_resolver=_StaticViewerResolver())


class AdminGateTests(unittest.TestCase):
    """应用层闸（铁律 8 的第二闸）：session + admin 缺一不可。"""

    def test_no_session_page_redirects_to_login(self):
        client = TestClient(_app(viewer=_admin_viewer()))

        response = client.get("/", follow_redirects=False)

        self.assertEqual(302, response.status_code)
        self.assertEqual("/auth/entry?reason=login", response.headers["location"])

    def test_no_session_api_answers_401(self):
        client = TestClient(_app(viewer=_admin_viewer()))

        response = client.post("/api/grants", json={"user_id": "u"})

        self.assertEqual(401, response.status_code)
        self.assertEqual({"detail": "unauthorized"}, response.json())

    def test_non_admin_is_forbidden(self):
        viewer = authz.Viewer(
            userid="u1", name="张三", scopes=frozenset({"fin"}),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        client = TestClient(_app(viewer=viewer))
        _login(client, "u1")

        page = client.get("/", follow_redirects=False)
        self.assertEqual(302, page.status_code)
        self.assertIn("reason=forbidden", page.headers["location"])
        self.assertEqual(
            403, client.post("/api/grants", json={"user_id": "u"}).status_code
        )

    def test_resolver_failure_is_fail_closed(self):
        # 铁律 2：Viewer 解析异常 = 403，绝不放行。
        client = TestClient(_app(resolver_error=RuntimeError("mart down")))
        _login(client)

        self.assertEqual(403, client.get("/").status_code)
        self.assertEqual(
            403, client.post("/api/grants", json={"user_id": "u"}).status_code
        )

    def test_admin_passes_every_surface(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)

        self.assertEqual(200, client.get("/").status_code)
        self.assertEqual(200, client.get("/grants").status_code)
        self.assertEqual(200, client.get("/audit").status_code)

    def test_auth_routes_stay_public(self):
        client = TestClient(_app(viewer=_admin_viewer()))

        self.assertEqual(200, client.get("/auth/entry").status_code)
        response = client.get("/auth/logout", follow_redirects=False)
        self.assertEqual(307, response.status_code)


class AuthExchangeTests(unittest.TestCase):
    def test_exchange_disabled_without_client(self):
        client = TestClient(_app(viewer=_admin_viewer(), auth_client=None))

        self.assertEqual(
            503, client.post("/auth/dingtalk", json={"authCode": "c"}).status_code
        )

    def test_exchange_issues_the_shared_session_cookie(self):
        client = TestClient(
            _app(viewer=_admin_viewer(), auth_client=_FakeAuthClient())
        )

        response = client.post("/auth/dingtalk", json={"authCode": "c"})

        self.assertEqual(200, response.status_code)
        cookie = response.cookies.get(auth.SESSION_COOKIE)
        self.assertEqual(ADMIN_USERID, auth.resolve_session_userid(cookie, SECRET))


class HealthzTests(unittest.TestCase):
    def test_healthz_reports_ok_with_db(self):
        client = TestClient(_app(viewer=_admin_viewer()))

        response = client.get("/healthz")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok", "database": "ok"}, response.json())

    def test_healthz_reports_unhealthy_without_db(self):
        client = TestClient(
            _app(viewer=_admin_viewer(), connector_error=RuntimeError("mart down"))
        )

        response = client.get("/healthz")

        self.assertEqual(503, response.status_code)
        self.assertEqual({"status": "unhealthy"}, response.json())


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


def _pipeline_app(*, viewer=None, fleet=("robot-hangzhou",), publisher=None,
                  connection=None, connector_error=None):
    configs = {"robot-hangzhou": {"schedule": "0 2 * * *", "enabled": True}}
    return create_app(
        settings=_fake_settings(),
        session_secret=SECRET,
        db_connector=_connector(connection, connector_error),
        viewer_resolver=_StaticViewerResolver(viewer),
        fleet_source=lambda: fleet,
        config_source=StaticConfigSource(configs),
        config_publisher=publisher,
    )


class PipelinePageTests(unittest.TestCase):
    def test_pipelines_page_lists_fleet_with_template_and_run_button(self):
        store = _PipelineStore()
        store.recent = [{
            "service_id": "robot-hangzhou", "requested_by": "王城",
            "status": "finished", "note": None, "exit_code": 0,
            "created_at": "2026-09-30 10:00:00", "finished_at": "2026-09-30 10:01:00",
        }]
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_recent():
            body = client.get("/pipelines").text

        self.assertIn("robot-hangzhou", body)
        self.assertIn("0 2 * * *", body)
        self.assertIn("运行一次", body)          # 有命令模板 → 可运行
        self.assertIn("finished", body)          # 最近运行请求
        self.assertIn("新增管道", body)

    def test_pipeline_audit_page_renders_entries(self):
        store = _PipelineStore()
        store.audit_rows = [("admin", "add", "robot-x", "0 2 * * *", "2026-09-30 10:00:00")]
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_audit_rows():
            body = client.get("/pipelines/audit").text

        self.assertIn("robot-x", body)
        self.assertIn("add", body)


class PipelineToggleTests(unittest.TestCase):
    def test_toggle_publishes_flipped_enabled_and_audits(self):
        store = _PipelineStore()
        publisher = _RecordingPublisher()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(), publisher=publisher))
        _login(client)
        with store.patch_audit():
            response = client.post(
                "/api/pipelines/toggle",
                json={"service_id": "robot-hangzhou", "enabled": False},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(publisher.published))
        self.assertEqual("robot-hangzhou", publisher.published[0].service_id)
        self.assertEqual(False, publisher.published[0].enabled)
        self.assertEqual(
            [(ADMIN_USERID, "disable", "robot-hangzhou", "0 2 * * *")],
            store.audit,
        )

    def test_toggle_requires_admin(self):
        store = _PipelineStore()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(),
                                          publisher=_RecordingPublisher()))
        self.assertEqual(
            401, client.post("/api/pipelines/toggle",
                             json={"service_id": "robot-hangzhou",
                                   "enabled": True}).status_code
        )

    def test_toggle_503_when_no_publisher(self):
        store = _PipelineStore()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(), publisher=None))
        _login(client)
        self.assertEqual(
            503, client.post("/api/pipelines/toggle",
                             json={"service_id": "robot-hangzhou",
                                   "enabled": True}).status_code
        )

    def test_toggle_rejects_bad_payload_and_unknown_service(self):
        store = _PipelineStore()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(),
                                          publisher=_RecordingPublisher()))
        _login(client)
        for payload in (
            {"service_id": "BAD ID", "enabled": True},   # 非法 id
            {"service_id": "robot-hangzhou", "enabled": "yes"},  # enabled 非 bool
            {"service_id": "nope", "enabled": True},      # 不在舰队
        ):
            self.assertEqual(
                400,
                client.post("/api/pipelines/toggle", json=payload).status_code,
                payload,
            )


class PipelineAddTests(unittest.TestCase):
    def test_add_validates_publishes_and_audits(self):
        store = _PipelineStore()
        publisher = _RecordingPublisher()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(), publisher=publisher))
        _login(client)
        with store.patch_audit():
            response = client.post(
                "/api/pipelines/add",
                json={"service_id": "robot-x", "kind": "apps",
                      "schedule": "0 2 * * *", "enabled": True,
                      "depends_on": [], "description": "demo"},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual("robot-x", publisher.published[0].service_id)
        self.assertEqual(
            [(ADMIN_USERID, "add", "robot-x", "0 2 * * *")], store.audit
        )

    def test_add_rejects_invalid_fields(self):
        store = _PipelineStore()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(),
                                          publisher=_RecordingPublisher()))
        _login(client)
        for payload in (
            {"service_id": "BAD ID", "kind": "apps", "schedule": "0 2 * * *"},
            {"service_id": "robot-x", "kind": "nope", "schedule": "0 2 * * *"},
            {"service_id": "robot-x", "kind": "apps", "schedule": "not a cron"},
            {"service_id": "robot-x", "kind": "apps",
             "schedule": "0 2 * * *", "depends_on": ["robot-x"]},
        ):
            self.assertEqual(
                400, client.post("/api/pipelines/add", json=payload).status_code,
                payload,
            )

    def test_add_rejects_duplicate_service(self):
        store = _PipelineStore()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(),
                                          publisher=_RecordingPublisher()))
        _login(client)
        self.assertEqual(
            400, client.post("/api/pipelines/add",
                             json={"service_id": "robot-hangzhou", "kind": "apps",
                                   "schedule": "0 2 * * *"}).status_code
        )


class PipelineRunOnceTests(unittest.TestCase):
    def test_run_once_inserts_request_with_actor(self):
        store = _PipelineStore()
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_pending(), store.patch_insert():
            response = client.post(
                "/api/pipelines/run-once", json={"service_id": "robot-hangzhou"}
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual(42, response.json()["request_id"])
        self.assertEqual([("robot-hangzhou", ADMIN_USERID)], store.inserted)

    def test_run_once_rejects_without_command_template(self):
        store = _PipelineStore()
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        # bi-web 无调度命令模板（C 方案：允许注册但不允许空跑）
        self.assertEqual(
            400, client.post("/api/pipelines/run-once",
                             json={"service_id": "bi-web"}).status_code
        )

    def test_run_once_rejects_bad_id_and_duplicate_pending(self):
        store = _PipelineStore()
        store.pending = True
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        self.assertEqual(
            400, client.post("/api/pipelines/run-once",
                             json={"service_id": "BAD ID"}).status_code
        )
        with store.patch_pending():  # has_pending_request -> True
            self.assertEqual(
                400, client.post("/api/pipelines/run-once",
                                 json={"service_id": "robot-hangzhou"}).status_code
            )


class MembersPageTests(unittest.TestCase):
    def test_members_render_grouped_data_escaped(self):
        connection = _Connection({
            "dim_robot_member": _MEMBERS + [("u-x", "<script>alert(1)</script>", "hq", None)],
        })
        client = TestClient(_app(viewer=_admin_viewer(), connection=connection))
        _login(client)

        body = client.get("/").text

        self.assertIn("u-zhang", body)
        self.assertIn("hangzhou", body)
        # XSS 插值以转义形态出现，原始 <script> 绝不进 HTML。
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_store_failure_is_a_generalized_503(self):
        client = TestClient(
            _app(viewer=_admin_viewer(), connector_error=RuntimeError("down"))
        )
        _login(client)

        response = client.get("/")

        self.assertEqual(503, response.status_code)
        self.assertNotIn("down", response.text)


class GrantWriteApiTests(unittest.TestCase):
    def test_grant_writes_record_and_audit_with_session_actor(self):
        connection = _Connection()
        client = TestClient(_app(viewer=_admin_viewer(), connection=connection))
        _login(client)

        response = client.post("/api/grants", json={
            "user_id": "u-zhang", "grant_type": "scope",
            "grant_key": "fin", "note": "财务页",
        })

        self.assertEqual(200, response.status_code)
        executed = connection.cursor_instance.executed
        sql = "\n".join(q for q, _ in executed)
        self.assertIn("INSERT INTO `bi_authz_grant`", sql)
        self.assertIn("INSERT INTO `bi_authz_grant_audit`", sql)
        audit_params = [p for q, p in executed if "audit" in q][0]
        self.assertEqual(ADMIN_USERID, audit_params[0])
        self.assertEqual(1, connection.commits)

    def test_grant_rejects_bad_fields(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)

        for payload in (
            {"user_id": "", "grant_type": "scope", "grant_key": "fin"},
            {"user_id": "u", "grant_type": "superuser", "grant_key": "x"},
            {"user_id": "u", "grant_type": "admin", "grant_key": "all"},
            {"user_id": "u", "grant_type": "scope"},
        ):
            self.assertEqual(
                400, client.post("/api/grants", json=payload).status_code,
                msg=repr(payload),
            )

    def test_delete_removes_and_audits(self):
        connection = _Connection()
        client = TestClient(_app(viewer=_admin_viewer(), connection=connection))
        _login(client)

        response = client.post("/api/grants/delete", json={
            "user_id": "u-zhang", "grant_type": "scope", "grant_key": "fin",
        })

        self.assertEqual(200, response.status_code)
        executed = connection.cursor_instance.executed
        sql = "\n".join(q for q, _ in executed)
        self.assertIn("DELETE FROM `bi_authz_grant`", sql)
        self.assertIn("revoke", [p[1] for q, p in executed if "audit" in q])

    def test_write_failure_is_a_generalized_503(self):
        client = TestClient(
            _app(viewer=_admin_viewer(), connector_error=RuntimeError("down"))
        )
        _login(client)

        response = client.post("/api/grants", json={
            "user_id": "u", "grant_type": "scope", "grant_key": "fin",
        })

        self.assertEqual(503, response.status_code)
        self.assertNotIn("down", response.text)


class RegionBatchTests(unittest.TestCase):
    def test_only_matching_region_members_are_granted_each_with_audit(self):
        connection = _Connection({"dim_robot_member": _MEMBERS})
        client = TestClient(_app(viewer=_admin_viewer(), connection=connection))
        _login(client)

        response = client.post("/api/grants/region", json={"region": "hangzhou"})

        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok", "written": 2}, response.json())
        executed = connection.cursor_instance.executed
        grant_params = [
            p for q, p in executed
            if q.startswith("INSERT INTO `bi_authz_grant` ")
        ]
        self.assertEqual(["u-zhang", "u-wang"], [p[0] for p in grant_params])
        # 批量也必须逐行落 audit（铁律：每次变更一行流水）。
        audit_params = [p for q, p in executed if "audit" in q]
        self.assertEqual(2, len(audit_params))
        self.assertEqual(
            ["u-zhang", "u-wang"], [p[2] for p in audit_params]
        )

    def test_region_requires_a_value(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)

        self.assertEqual(
            400, client.post("/api/grants/region", json={"region": " "}).status_code
        )


class GrantsAndAuditPageTests(unittest.TestCase):
    def test_grants_page_lists_current_rows(self):
        connection = _Connection({
            "bi_authz_grant` ": [
                ("u-zhang", "scope", "fin", "财务页", ADMIN_USERID,
                 "2026-09-21 10:00:00.000000"),
            ],
        })
        client = TestClient(_app(viewer=_admin_viewer(), connection=connection))
        _login(client)

        body = client.get("/grants").text

        self.assertIn("u-zhang", body)
        self.assertIn("fin", body)
        self.assertIn("收回", body)

    def test_audit_page_lists_entries_newest_first(self):
        connection = _Connection({
            "bi_authz_grant_audit`": [
                (ADMIN_USERID, "grant", "u-zhang", "scope", "fin", "",
                 "2026-09-21 10:00:00.000000"),
            ],
        })
        client = TestClient(_app(viewer=_admin_viewer(), connection=connection))
        _login(client)

        body = client.get("/audit").text

        self.assertIn("grant", body)
        self.assertIn("u-zhang", body)


if __name__ == "__main__":
    unittest.main()
