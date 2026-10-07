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
from common.public_data import channel_target, ops_control, report_roster
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


class JsapiConfigTests(unittest.TestCase):
    """ops-web 的 /auth/jsapi-config（免登 dd.config 签名原料，与 bi-web 同款）。"""

    class _JsapiClient:
        def exchange_auth_code(self, code):
            return ADMIN_USERID

        def jsapi_ticket(self):
            return "ticket-1"

    def _jsapi_app(self, *, client=None, corp_id="corp-1", agent_id="agent-1"):
        return create_app(
            settings=_fake_settings(), session_secret=SECRET,
            db_connector=_connector(),
            viewer_resolver=_StaticViewerResolver(_admin_viewer()),
            auth_client=client if client is not None else self._JsapiClient(),
            corp_id=corp_id, agent_id=agent_id,
        )

    def test_jsapi_config_returns_signature_material(self):
        client = TestClient(self._jsapi_app())
        url = "http://127.0.0.1:18100/pipelines"

        response = client.get("/auth/jsapi-config", params={"url": url})

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual("agent-1", body["agentId"])
        self.assertEqual("corp-1", body["corpId"])
        self.assertEqual(
            auth.build_jsapi_signature(
                "ticket-1", body["nonceStr"], body["timeStamp"], url
            ),
            body["signature"],
        )

    def test_jsapi_config_503_without_corp_or_agent(self):
        client = TestClient(self._jsapi_app(corp_id=None))
        response = client.get("/auth/jsapi-config", params={"url": "http://x/"})
        self.assertEqual(503, response.status_code)

    def test_jsapi_config_503_without_client(self):
        client = TestClient(self._jsapi_app(client=None))
        # client=None 显式构造无 auth_client 的应用
        app = create_app(
            settings=_fake_settings(), session_secret=SECRET,
            db_connector=_connector(),
            viewer_resolver=_StaticViewerResolver(_admin_viewer()),
            auth_client=None, corp_id="corp-1", agent_id="agent-1",
        )
        response = TestClient(app).get(
            "/auth/jsapi-config", params={"url": "http://x/"}
        )
        self.assertEqual(503, response.status_code)

    def test_jsapi_config_400_on_bad_url(self):
        client = TestClient(self._jsapi_app())
        for bad in ("notaurl", "javascript:alert(1)", "ftp://x/"):
            self.assertEqual(
                400,
                client.get("/auth/jsapi-config", params={"url": bad}).status_code,
                bad,
            )


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

        self.assertIn("robot-hangzhou", body)    # 原始 id 小字备查
        self.assertIn("杭州", body)              # 服务中文名（区域中文化）
        self.assertIn("0 2 * * *", body)         # 原表达式作小字备查
        self.assertIn("每天 02:00", body)        # cron 人性化
        self.assertIn("日报机器人", body)        # robot- 家族中文说明（动态生成）
        self.assertIn("运行一次", body)          # 有命令模板 → 可运行
        self.assertIn("成功", body)              # 最近运行（finished 中文化）
        self.assertIn("新增定时任务", body)
        self.assertIn("应用线", body)            # kind=apps 中文化
        self.assertIn("服务标识", body)          # 表头中文化
        self.assertNotIn(">finished<", body)     # 英文状态值不外显
        self.assertIn("2026-09-30 18:00:00", body)   # UTC 存储 → 北京时间
        self.assertNotIn("10:00:00", body)           # UTC 原文不外显

    def test_pipeline_audit_page_renders_entries(self):
        store = _PipelineStore()
        store.audit_rows = [("admin", "add", "robot-x", "0 2 * * *", "2026-09-30 10:00:00")]
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_audit_rows():
            body = client.get("/pipelines/audit").text

        self.assertIn("robot-x", body)
        self.assertIn("新增", body)  # action=add 中文化


class ZhLabelTests(unittest.TestCase):
    """中文化 helper：cron 人性化与服务中文说明（纯函数，无 IO）。"""

    def test_cron_zh_daily_multi_hours(self):
        from common.ops_web.app import _cron_zh
        self.assertEqual("每天 01:55、06:55、13:55、15:55",
                         _cron_zh("55 1,6,13,15 * * *"))
        self.assertEqual("每天 02:00", _cron_zh("0 2 * * *"))

    def test_cron_zh_weekly_and_monthly(self):
        from common.ops_web.app import _cron_zh
        self.assertEqual("每周一 09:30", _cron_zh("30 9 * * 1"))
        self.assertEqual("每月 1 日 10:00", _cron_zh("0 10 1 * *"))

    def test_cron_zh_minutely_hourly_and_fallback(self):
        from common.ops_web.app import _cron_zh
        self.assertEqual("每 5 分钟", _cron_zh("*/5 * * * *"))
        self.assertEqual("每小时第 30 分", _cron_zh("30 * * * *"))
        self.assertIsNone(_cron_zh("0 0 1 1 *"))     # 带月份字段 → 回退
        self.assertIsNone(_cron_zh("not a cron"))

    def test_schedule_cell_keeps_raw_as_hint(self):
        from common.ops_web.app import _schedule_cell
        cell = _schedule_cell("55 1,6,13,15 * * *")
        self.assertIn("每天 01:55", cell)
        self.assertIn("55 1,6,13,15 * * *", cell)
        self.assertIn("常驻", _schedule_cell(None))
        self.assertEqual("weird cron", _schedule_cell("weird cron"))

    def test_service_label_curated_family_and_fallback(self):
        from common.ops_web.app import _service_label
        self.assertIn("滚动源清单", _service_label("roll-manifest", "rolls..."))
        self.assertEqual("「杭州」日报机器人（报数汇总 → 群内播报/催办）",
                         _service_label("robot-hangzhou", "hangzhou daily..."))
        self.assertEqual("「万科&大莲花&团购」榜单页生成",
                         _service_label("pages-vanke", "vanke leaderboard..."))
        self.assertEqual("原文保留", _service_label("mystery", "原文保留"))

    def test_service_name_curated_family_and_fallback(self):
        from common.ops_web.app import _service_name, _service_name_cell
        self.assertEqual("滚动清单", _service_name("roll-manifest"))
        self.assertEqual("「杭州」日报机器人", _service_name("robot-hangzhou"))
        self.assertEqual("「绍兴」榜单页", _service_name("pages-shaoxing"))
        self.assertEqual("mystery", _service_name("mystery"))
        # 有中文名：中文为主、id 小字备查；无中文名：只显示 id
        cell = _service_name_cell("robot-hangzhou")
        self.assertIn("「杭州」日报机器人", cell)
        self.assertIn("（robot-hangzhou）", cell)
        self.assertEqual("mystery", _service_name_cell("mystery"))

    def test_fmt_time_converts_utc_to_beijing(self):
        from datetime import datetime, timezone
        from common.ops_web.app import _fmt_time
        # 字符串（naive 按 UTC 解读）
        self.assertEqual("2026-09-30 18:00:00",
                         _fmt_time("2026-09-30 10:00:00"))
        self.assertEqual("2026-10-06 19:13:03",
                         _fmt_time("2026-10-06 11:13:03.465847"))
        # datetime 对象（naive / 带 tz 都支持）
        self.assertEqual("2026-09-30 18:00:00",
                         _fmt_time(datetime(2026, 9, 30, 10, 0, 0)))
        self.assertEqual("2026-09-30 18:00:00",
                         _fmt_time(datetime(2026, 9, 30, 10, 0, 0,
                                            tzinfo=timezone.utc)))
        # None / 认不出的原样
        self.assertIsNone(_fmt_time(None))
        self.assertEqual("not-a-time", _fmt_time("not-a-time"))


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


class RosterPageTests(unittest.TestCase):
    def test_roster_page_groups_by_scope_with_chinese_labels(self):
        store = _RosterStore()
        store.rows = [_roster_row()]
        store.audit_rows = [{
            "actor": "admin", "action": "add", "scope": "qudao",
            "entity_type": "store", "entity_key": "JD购喝",
            "person_name": "饶佳君", "detail": "共管",
            "created_at": "2026-10-06 11:00:00",
        }]
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_fetch(), store.patch_audit():
            body = client.get("/roster").text

        self.assertIn("填报名册", body)
        self.assertIn("渠道门店", body)          # scope 中文化
        self.assertIn("门店", body)              # entity_type 中文化
        self.assertIn("JD购喝", body)
        self.assertIn("饶佳君", body)
        self.assertIn("小饶", body)              # aliases JSON 展开
        self.assertIn("停用", body)              # 操作按钮
        self.assertIn("新增名册记录", body)
        self.assertIn("暂无记录", body)          # 空 scope 分组提示
        self.assertIn("fail-open", body)
        self.assertIn("2026-10-06 19:00:00", body)  # 审计时间转北京时

    def test_roster_page_requires_admin(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        response = client.get("/roster", follow_redirects=False)
        self.assertEqual(302, response.status_code)

    def test_roster_page_marks_deputies_in_qudao_section(self):
        # v3 角色展示：渠道区段代填报人带【代填】标识，表头分列
        store = _RosterStore()
        store.rows = [
            _roster_row(id=1, entity_key="TM习酒旗舰店", person_name="卢雅玲",
                        aliases=None, note=""),
            _roster_row(id=2, entity_key="TM习酒旗舰店", person_name="卢雅莹",
                        aliases=None, note="", role="deputy"),
        ]
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_fetch(), store.patch_audit():
            body = client.get("/roster").text

        self.assertIn("【代填】", body)
        self.assertIn("负责人/代填报人", body)


class RosterApiTests(unittest.TestCase):
    def test_add_validates_and_upserts_with_actor(self):
        store = _RosterStore()
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_upsert():
            response = client.post("/api/roster/add", json={
                "scope": "qudao", "entity_type": "store",
                "entity_key": "JD购喝", "person_name": "饶佳君",
                "aliases": ["小饶"], "note": "共管",
            })

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(store.upserted))
        entry, actor = store.upserted[0]
        self.assertEqual("JD购喝", entry.entity_key)
        self.assertEqual(("小饶",), entry.aliases)
        self.assertEqual(ADMIN_USERID, actor)

    def test_add_rejects_invalid_fields(self):
        store = _RosterStore()
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        for payload in (
            {"scope": "nope", "entity_type": "store",
             "entity_key": "店", "person_name": "张三"},
            {"scope": "qudao", "entity_type": "alien",
             "entity_key": "店", "person_name": "张三"},
            {"scope": "qudao", "entity_type": "store",
             "entity_key": "店", "person_name": "  "},
            {"scope": "qudao", "entity_type": "store",
             "entity_key": "店", "person_name": "张三", "aliases": "not-a-list"},
            {"scope": "qudao", "entity_type": "store",
             "entity_key": "店", "person_name": "张三", "role": "boss"},
        ):
            self.assertEqual(
                400, client.post("/api/roster/add", json=payload).status_code,
                payload,
            )
        self.assertEqual([], store.upserted)

    def test_add_with_deputy_role(self):
        # v3 角色：代填报人经表单 role 字段入库（2026-10-07 运维裁决）
        store = _RosterStore()
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_upsert():
            response = client.post("/api/roster/add", json={
                "scope": "qudao", "entity_type": "store",
                "entity_key": "TM习酒旗舰店", "person_name": "卢雅莹",
                "role": "deputy",
            })

        self.assertEqual(200, response.status_code)
        entry, _actor = store.upserted[0]
        self.assertEqual("deputy", entry.role)

    def test_toggle_and_delete_with_hit_and_miss(self):
        store = _RosterStore()
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)

        with store.patch_toggle():
            self.assertEqual(200, client.post(
                "/api/roster/toggle", json={"id": 5, "enabled": False},
            ).status_code)
        self.assertEqual([(5, False, ADMIN_USERID)], store.toggled)

        with store.patch_delete():
            self.assertEqual(200, client.post(
                "/api/roster/delete", json={"id": 7},
            ).status_code)
        self.assertEqual([(7, ADMIN_USERID)], store.deleted)

        # 未命中 → 400；非法载荷 → 400
        store.hit = False
        with store.patch_toggle():
            self.assertEqual(400, client.post(
                "/api/roster/toggle", json={"id": 99, "enabled": True},
            ).status_code)
        self.assertEqual(400, client.post(
            "/api/roster/toggle", json={"id": "x", "enabled": True},
        ).status_code)
        self.assertEqual(400, client.post(
            "/api/roster/delete", json={"id": True},
        ).status_code)

    def test_apis_require_admin_session(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        self.assertEqual(401, client.post(
            "/api/roster/add", json={"scope": "qudao", "entity_type": "store",
                                     "entity_key": "店", "person_name": "张三"},
        ).status_code)


class RosterPublishTests(unittest.TestCase):
    """发布月度快照 API（S2）：校验 + 写 raw + 触发 extract run-request。"""

    def test_publish_writes_and_triggers_extract(self):
        published = []
        requests = []

        class _RawConn:
            def close(self):
                pass

        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with mock.patch.object(
                channel_target, "publish_snapshot",
                lambda raw_conn, rows, *, actor, roster_connection=None:
                published.append((rows, actor, roster_connection)) or (2, 3, ["新店"])), \
             mock.patch.object(
                ops_control, "insert_run_request",
                lambda conn, sid, by: requests.append((sid, by)) or 88), \
             mock.patch.object(
                report_roster, "fetch_store_numbers", lambda conn: {}), \
             mock.patch("common.ops_web.app.connect", lambda _ds: _RawConn()):
            response = client.post("/api/roster/publish-snapshot", json={
                "rows": [{"store_name": "JD购喝", "channel": "京东",
                          "monthly_target": 1500000}],
            })

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual((2, 3), (body["deleted"], body["inserted"]))
        self.assertEqual(["新店"], body["missing_owners"])
        self.assertEqual(88, body["extract_request_id"])
        self.assertEqual([("extract-mart", ADMIN_USERID)], requests)
        self.assertEqual(ADMIN_USERID, published[0][1])
        self.assertIsNotNone(published[0][2])  # roster_connection 走 mart

    def test_publish_rejects_bad_payload(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with mock.patch.object(report_roster, "fetch_store_numbers",
                               lambda conn: {}):
            self.assertEqual(400, client.post(
                "/api/roster/publish-snapshot", json={"rows": "x"}).status_code)
            self.assertEqual(400, client.post(
                "/api/roster/publish-snapshot",
                json={"rows": [{"store_name": "店", "channel": "商超",
                                "monthly_target": 1}]}).status_code)

    def test_publish_requires_admin_session(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        self.assertEqual(401, client.post(
            "/api/roster/publish-snapshot", json={"rows": []}).status_code)


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
