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
from datetime import date
from types import SimpleNamespace
from unittest import mock

warnings.filterwarnings(
    "ignore", message="Using `httpx` with `starlette.testclient` is deprecated"
)

from fastapi.testclient import TestClient

from common.bi_web import auth, authz
from common.ops_web.app import create_app
from common.public_data import (
    bi_authz, channel_target, ops_control, report_roster,
)
from common.public_data.calendar_import import CalendarImportError
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
        self.assertIn("填报提醒", body)          # robot- 家族中文说明（动态生成）
        self.assertIn("运行一次", body)          # 有命令模板 → 可运行
        self.assertIn("成功", body)              # 最近运行（finished 中文化）
        self.assertIn("新增定时任务", body)
        self.assertIn("应用线", body)            # kind=apps 中文化
        self.assertIn("服务标识", body)          # 表头中文化
        self.assertIn("管理群", body)            # 新列表头
        self.assertIn("功能", body)              # 新列表头
        self.assertIn("「杭州」群", body)        # 管理群（robot- 家族区域映射）
        self.assertIn("催办", body)              # 功能（robot- 家族归类）
        self.assertIn("操作…", body)             # 操作列改下拉
        self.assertIn('data-service="robot-hangzhou"', body)
        self.assertIn('id="confirm-mask"', body)     # 弹窗确认挂载点
        self.assertIn('id="confirm-text"', body)
        self.assertNotIn(">finished<", body)     # 英文状态值不外显
        self.assertIn("2026-09-30 18:00:00", body)   # UTC 存储 → 北京时间
        self.assertNotIn("10:00:00", body)           # UTC 原文不外显

    def test_pipelines_page_splits_three_class_tables(self):
        """分表：应用类/业务线/数据线三段标题与归类落位。"""
        fleet = (
            "scheduler",            # 平台 → 应用类
            "dingtalk-gateway",     # 钉钉 → 应用类
            "robot-hangzhou",       # 催办 → 业务线
            "leaderboard-qudao",    # 播报 → 业务线
            "sync-wdt",             # 同步 → 数据线
            "extract-mart",         # 加工 → 数据线
        )
        client = TestClient(_pipeline_app(viewer=_admin_viewer(), fleet=fleet))
        _login(client)
        body = client.get("/pipelines").text

        for title in ("<h2>应用类</h2>", "<h2>业务线</h2>", "<h2>数据线</h2>"):
            self.assertIn(title, body)
        app_i = body.index("<h2>应用类</h2>")
        biz_i = body.index("<h2>业务线</h2>")
        data_i = body.index("<h2>数据线</h2>")
        self.assertLess(app_i, biz_i)
        self.assertLess(biz_i, data_i)
        # 平台/钉钉线落在应用类段
        self.assertLess(app_i, body.index("scheduler"))
        self.assertLess(body.index("dingtalk-gateway"), biz_i)
        # 机器人/榜单播报落在业务线段
        self.assertLess(biz_i, body.index("robot-hangzhou"))
        self.assertLess(body.index("leaderboard-qudao"), data_i)
        # 同步/提取落在数据线段（表末无更多段）
        self.assertLess(data_i, body.index("sync-wdt"))
        self.assertLess(data_i, body.index("extract-mart"))

    def test_pipelines_page_group_and_function_classification(self):
        """管理群/功能列：家族后缀、显式区域映射、应用线回退。"""
        fleet = (
            "leaderboard-hangzhou",   # 家族：播报 + 「杭州」群
            "pages-qudao-t1",         # 显式区域：页面 + 「渠道」群（后缀非区域）
            "channel-missing-check",  # 显式：催办 + 「渠道」群
            "sync-wdt",               # 应用线：同步 + 无群
            "dingtalk-gateway",       # 平台线：钉钉 + 无群
        )
        client = TestClient(_pipeline_app(
            viewer=_admin_viewer(), fleet=fleet,
        ))
        _login(client)
        body = client.get("/pipelines").text

        self.assertIn("「杭州」群", body)
        self.assertEqual(2, body.count("「渠道」群"))  # 两条渠道线各自映射
        self.assertIn("播报", body)                    # leaderboard- 家族
        self.assertIn("页面", body)                    # pages- 家族
        self.assertIn("催办", body)                    # channel-missing-check
        self.assertIn("同步", body)                    # sync-wdt
        self.assertIn("钉钉", body)                    # dingtalk-gateway
        self.assertIn("榜单播报", body)                # leaderboard- 家族中文名

    def test_pipelines_page_reminder_targets(self):
        """收受影响人列：robot=名册在册人员（绑定）；渠道到齐=名册店铺负责人。"""
        connection = _Connection({
            "dim_robot_member": [
                ("u1", "张三", "hangzhou", "门店一部"),
                ("u2", "李四", "hangzhou", "门店二部"),
                ("u3", "王五", "shaoxing", "项目部"),
            ],
            "entity_type` = 'person'": [("张三",)],   # 名册：hangzhou 只登记张三
            "entity_type` = 'store'": [
                ("天猫-A店", "赵六"),
                ("京东-B店", "钱七"),
                ("京东-B店", "赵六"),
            ],
        })
        fleet = ("robot-hangzhou", "channel-missing-check", "sync-wdt")
        client = TestClient(_pipeline_app(
            viewer=_admin_viewer(), fleet=fleet, connection=connection))
        _login(client)
        body = client.get("/pipelines").text

        self.assertIn("收受影响人", body)     # 新列表头
        self.assertIn("张三", body)
        self.assertIn("（1 人 · 名册）", body)  # robot- 名册绑定：只列在册的张三
        self.assertNotIn("李四", body)         # 非在册成员不收影响
        self.assertIn("赵六、钱七", body)      # 渠道名册负责人（去重排序）
        self.assertIn("（2 人 · 2 店）", body)
        self.assertNotIn("王五", body)         # 无 robot-shaoxing 行不展示
        self.assertNotIn("读取失败", body)

    def test_pipelines_page_reminder_targets_fail_open_all_members(self):
        """名册无记录 → 区域在册填报人全员兜底（与机器人 fail-open 同语义）。"""
        connection = _Connection({
            "dim_robot_member": [
                ("u1", "张三", "hangzhou", "门店一部"),
                ("u2", "李四", "hangzhou", "门店二部"),
            ],
        })
        client = TestClient(_pipeline_app(
            viewer=_admin_viewer(), fleet=("robot-hangzhou",),
            connection=connection))
        _login(client)
        body = client.get("/pipelines").text

        self.assertIn("张三、李四", body)
        self.assertIn("（2 人 · 全员兜底）", body)

    def test_pipelines_page_reminder_targets_empty_roster_hint(self):
        """渠道名册未登记 → 如实标注机器人回退月目标表 owners_json。"""
        client = TestClient(_pipeline_app(
            viewer=_admin_viewer(), fleet=("channel-missing-check",)))
        _login(client)
        body = client.get("/pipelines").text
        self.assertIn("回退月目标表 owners_json", body)

    def test_pipelines_page_reminder_targets_fail_soft(self):
        """收受影响人读取故障 → 本列「读取失败」，整页不拖垮。"""
        def _boom(conn):
            raise RuntimeError("db down")
        client = TestClient(_pipeline_app(
            viewer=_admin_viewer(), fleet=("robot-hangzhou",)))
        _login(client)
        with mock.patch.object(bi_authz, "fetch_active_members", _boom):
            response = client.get("/pipelines")
        self.assertEqual(200, response.status_code)
        self.assertIn("读取失败", response.text)
        self.assertIn("robot-hangzhou", response.text)

    def test_pipelines_page_category_truth_overrides_derivation(self):
        """category 真源优先：注册表配置覆盖 service_id 推导（功能列+分表）。

        sync-wdt 推导=同步（数据线）；注册表配 category=催办 后，
        功能列显示催办、整行落在业务线段。
        """
        configs = {"sync-wdt": {"schedule": "0 2 * * *", "category": "催办"}}
        app = create_app(
            settings=_fake_settings(),
            session_secret=SECRET,
            db_connector=_connector(),
            viewer_resolver=_StaticViewerResolver(_admin_viewer()),
            fleet_source=lambda: ("sync-wdt",),
            config_source=StaticConfigSource(configs),
            config_publisher=None,
        )
        client = TestClient(app)
        _login(client)
        body = client.get("/pipelines").text

        biz_i = body.index("<h2>业务线</h2>")
        data_i = body.index("<h2>数据线</h2>")
        row_i = body.index("sync-wdt")
        self.assertLess(biz_i, row_i)          # 落业务线段（真源胜出）
        self.assertLess(row_i, data_i)
        row_html = body[row_i:data_i]
        # 功能单元格（管理群列之后）显示真源「催办」，推导值「同步」不占位
        self.assertIn("</td><td>催办</td>", row_html)
        self.assertNotIn("</td><td>同步</td>", row_html)

    def test_pipelines_page_add_form_offers_category_select(self):
        """新增表单带 category 下拉：空值=按标识推导，七值可选。"""
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        body = client.get("/pipelines").text

        self.assertIn('name="category"', body)
        self.assertIn("功能（留空按标识推导）", body)
        for category in ("同步", "加工", "播报", "催办", "页面", "钉钉", "平台"):
            self.assertIn(f'<option value="{category}">{category}</option>', body)

    def test_pipeline_audit_page_renders_entries(self):
        store = _PipelineStore()
        store.audit_rows = [("admin", "add", "robot-x", "0 2 * * *", "2026-09-30 10:00:00")]
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_audit_rows():
            body = client.get("/pipelines/audit").text

        self.assertIn("robot-x", body)
        self.assertIn("新增", body)  # action=add 中文化

    def test_pipeline_history_page_renders_entries(self):
        store = _PipelineStore()
        store.history_rows = [
            {"id": 2, "service_id": "robot-hangzhou", "trigger_type": "cron",
             "status": "failed", "exit_code": None,
             "note": "调度锁被占用或运行异常，未产生退出码",
             "started_at": "2026-10-07 02:00:01", "finished_at": "2026-10-07 02:00:02"},
            {"id": 1, "service_id": "sync-wdt", "trigger_type": "run_once",
             "status": "finished", "exit_code": 0, "note": None,
             "started_at": "2026-10-07 01:00:00", "finished_at": "2026-10-07 01:05:00"},
        ]
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_history_rows():
            body = client.get("/pipelines/history").text

        self.assertIn("robot-hangzhou", body)
        self.assertIn("sync-wdt", body)
        self.assertIn("定时", body)          # trigger=cron 中文化
        self.assertIn("手动", body)          # trigger=run_once 中文化
        self.assertIn("失败", body)
        self.assertIn("成功", body)

    def test_pipeline_history_page_filters_by_service_id(self):
        store = _PipelineStore()
        store.history_rows = [
            {"id": 1, "service_id": "sync-wdt", "trigger_type": "cron",
             "status": "finished", "exit_code": 0, "note": None,
             "started_at": "2026-10-07 01:00:00", "finished_at": "2026-10-07 01:05:00"},
            {"id": 2, "service_id": "robot-hangzhou", "trigger_type": "cron",
             "status": "finished", "exit_code": 0, "note": None,
             "started_at": "2026-10-07 02:00:00", "finished_at": "2026-10-07 02:01:00"},
        ]
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_history_rows():
            body = client.get(
                "/pipelines/history", params={"service_id": "sync-wdt"}
            ).text

        self.assertIn("sync-wdt", body)
        self.assertNotIn("robot-hangzhou<", body)


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
        self.assertEqual("「杭州」填报提醒（报数汇总 → 群内提醒）",
                         _service_label("robot-hangzhou", "hangzhou daily..."))
        self.assertEqual("「杭州」催办未填人 + DING（报数核对 → 群内 @ + DING）",
                         _service_label("robot-check-hangzhou", "hangzhou..."))
        self.assertEqual("「万科&大莲花&团购」榜单页生成",
                         _service_label("pages-vanke", "vanke leaderboard..."))
        self.assertEqual("原文保留", _service_label("mystery", "原文保留"))

    def test_service_name_curated_family_and_fallback(self):
        from common.ops_web.app import _service_name, _service_name_cell
        self.assertEqual("滚动清单", _service_name("roll-manifest"))
        self.assertEqual("「杭州」填报提醒", _service_name("robot-hangzhou"))
        self.assertEqual("「杭州」催办未填+DING",
                         _service_name("robot-check-hangzhou"))
        self.assertEqual("「绍兴」榜单页", _service_name("pages-shaoxing"))
        self.assertEqual("mystery", _service_name("mystery"))
        # 有中文名：中文为主、id 小字备查；无中文名：只显示 id
        cell = _service_name_cell("robot-hangzhou")
        self.assertIn("「杭州」填报提醒", cell)
        self.assertIn("（robot-hangzhou）", cell)
        self.assertEqual("mystery", _service_name_cell("mystery"))

    def test_service_region_robot_check_maps_region(self):
        """robot-check-<region> 管理群区域解析（家族长者优先于 robot-）。"""
        from common.ops_web.app import _group_cell
        self.assertIn("「杭州」群", _group_cell("robot-check-hangzhou"))
        self.assertIn("「杭州」群", _group_cell("robot-hangzhou"))

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

    def test_add_publishes_category_and_rejects_unknown(self):
        store = _PipelineStore()
        publisher = _RecordingPublisher()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(), publisher=publisher))
        _login(client)
        with store.patch_audit():
            ok = client.post(
                "/api/pipelines/add",
                json={"service_id": "robot-x", "kind": "business",
                      "category": "催办", "schedule": "0 2 * * *"},
            )
            bad = client.post(
                "/api/pipelines/add",
                json={"service_id": "robot-y", "kind": "business",
                      "category": "别的", "schedule": "0 2 * * *"},
            )

        self.assertEqual(200, ok.status_code)
        self.assertEqual("催办", publisher.published[0].category)
        self.assertEqual(400, bad.status_code)
        self.assertEqual(1, len(publisher.published))  # 非法 category 不落注册表

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

    def test_roster_page_splits_owner_deputy_columns_with_status_select(self):
        # 2026-10-07 运维裁决：负责人/代填报人分两列；启用/停用改下拉
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

        self.assertIn('href="?sort=owner">负责人', body)   # 两列可排序表头
        self.assertIn('href="?sort=deputy">代填报人', body)
        self.assertIn("卢雅玲", body)
        self.assertIn("卢雅莹", body)
        # 负责人链接：状态下拉（当前值即链接状态）+ 删除按钮
        self.assertIn('setRosterStatus(1, this.value)', body)
        self.assertIn('<option value="1" selected>启用</option>', body)
        # 代填报人店铺格只读（2026-10-07 裁决：授权不在此办理）——
        # 代填人（id=2）无状态下拉、无删除按钮
        self.assertNotIn('setRosterStatus(2, this.value)', body)
        self.assertNotIn("deleteRoster(2)", body)
        self.assertNotIn("toggleRoster", body)


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


class RosterManageGateTests(unittest.TestCase):
    """名册新增/删除/状态修改绑定「名册管理」授权（scope=roster，
    2026-10-07 运维裁决）；admin 隐式持有。"""

    def test_manager_can_view_and_write(self):
        viewer = _roster_manager_viewer()
        store = _RosterStore()
        client = TestClient(_app(viewer=viewer))
        _login(client, userid="u-manager")
        with store.patch_fetch(), store.patch_audit():
            self.assertEqual(200, client.get("/roster").status_code)
        with store.patch_upsert():
            self.assertEqual(200, client.post("/api/roster/add", json={
                "scope": "qudao", "entity_type": "store",
                "entity_key": "JD购喝", "person_name": "饶佳君",
            }).status_code)
        with store.patch_toggle():
            self.assertEqual(200, client.post(
                "/api/roster/toggle", json={"id": 1, "enabled": False},
            ).status_code)
        with store.patch_delete():
            self.assertEqual(200, client.post(
                "/api/roster/delete", json={"id": 1},
            ).status_code)
        entry, actor = store.upserted[0]
        self.assertEqual("u-manager", actor)

    def test_plain_member_forbidden(self):
        viewer = _plain_member_viewer()
        store = _RosterStore()
        client = TestClient(_app(viewer=viewer))
        _login(client, userid="u-plain")
        page = client.get("/roster", follow_redirects=False)
        self.assertEqual(302, page.status_code)
        self.assertEqual("/auth/entry?reason=forbidden",
                         page.headers["location"])
        for path, payload in (
            ("/api/roster/add", {"scope": "qudao", "entity_type": "store",
                                 "entity_key": "店", "person_name": "张三"}),
            ("/api/roster/toggle", {"id": 1, "enabled": False}),
            ("/api/roster/delete", {"id": 1}),
        ):
            self.assertEqual(
                403, client.post(path, json=payload).status_code, path,
            )
        self.assertEqual([], store.upserted)

    def test_admin_remains_implicit_manager(self):
        store = _RosterStore()
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_delete():
            self.assertEqual(200, client.post(
                "/api/roster/delete", json={"id": 1},
            ).status_code)


class RosterSortTests(unittest.TestCase):
    """渠道门店表排序与渠道聚合（2026-10-07 运维裁决）。"""

    def _rows(self):
        return [
            _roster_row(id=1, entity_key="JD购喝", person_name="娄灿斌",
                        aliases=None, note=""),
            _roster_row(id=2, entity_key="TM旗舰A", person_name="钟甜",
                        aliases=None, note=""),
            _roster_row(id=3, entity_key="TM旗舰B", person_name="周嘉炜",
                        aliases=None, note=""),
            _roster_row(id=4, entity_key="TM旗舰A", person_name="张瑾萱",
                        aliases=None, note="", role="deputy"),
        ]

    def _page(self, store, sort=None):
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        targets = {
            "JD购喝": ("京东", 1500000),
            "TM旗舰A": ("天猫", 3000000),
            "TM旗舰B": ("天猫", 2000000),
        }
        numbers = {
            "JD购喝": ("京东", 1),
            "TM旗舰A": ("天猫", 1),
            "TM旗舰B": ("天猫", 2),
        }
        url = "/roster" + (f"?sort={sort}" if sort else "")
        with store.patch_fetch(), store.patch_audit(), \
             mock.patch.object(channel_target, "fetch_store_targets",
                               lambda conn: targets), \
             mock.patch.object(report_roster, "fetch_store_numbers",
                               lambda conn: numbers):
            return client.get(url).text

    def test_default_channel_sort_aggregates_with_rowspan(self):
        store = _RosterStore()
        store.rows = self._rows()
        body = self._page(store)

        # 行 = 负责人链接（代填聚进店铺格）：天猫 2 店各 1 负责人 →
        # 渠道格 rowspan=2；单负责人店铺（TM旗舰A 虽有代填）店铺格不聚合
        self.assertIn('<td rowspan="2">天猫</td>', body)
        self.assertIn('<td>TM旗舰A</td>', body)
        # 代填张瑾萱在同行代填报人格内（只读：无状态下拉与删除）
        self.assertIn("张瑾萱", body)
        self.assertNotIn("setRosterStatus(4, this.value)", body)
        self.assertNotIn("deleteRoster(4)", body)
        self.assertLess(body.index("TM旗舰A"), body.index("TM旗舰B"))
        # 渠道序按 CHANNELS canonical：京东（idx 2）在天猫（idx 3）前
        self.assertLess(body.index("JD购喝"), body.index("TM旗舰A"))
        # 默认表头带当前排序标记
        self.assertIn('href="?sort=channel">渠道/地区 ▾', body)

    def test_sort_by_target_descending(self):
        store = _RosterStore()
        store.rows = self._rows()
        body = self._page(store, sort="target")

        # 目标降序：300万 TM旗舰A > 200万 TM旗舰B > 150万 JD购喝
        self.assertLess(body.index("TM旗舰A"), body.index("TM旗舰B"))
        self.assertLess(body.index("TM旗舰B"), body.index("JD购喝"))
        # 非渠道序渠道格不合并（天猫 2 行 = 两店各 1 负责人行）
        self.assertEqual(2, body.count("<td>天猫</td>"))

    def test_sort_by_owner_and_deputy(self):
        store = _RosterStore()
        store.rows = self._rows()
        body = self._page(store, sort="owner")
        # 负责人首名升序（Unicode）：周嘉炜 < 娄灿斌 < 钟甜
        self.assertLess(body.index("TM旗舰B"), body.index("JD购喝"))
        self.assertLess(body.index("JD购喝"), body.index("TM旗舰A"))

        body = self._page(store, sort="deputy")
        # 仅 TM旗舰A 有代填（张瑾萱）排最前
        self.assertLess(body.index("TM旗舰A"),
                        min(body.index("TM旗舰B"), body.index("JD购喝")))

    def test_invalid_sort_falls_back_to_channel(self):
        store = _RosterStore()
        store.rows = self._rows()
        body = self._page(store, sort="bogus")
        self.assertIn('<td rowspan="2">天猫</td>', body)

    def _assert_grid_balance(self, body, table_index=0, columns=9):
        """模拟 rowspan 布局，逐行校验有效列数（防聚合错位）。"""
        tables = re.findall(r"<table>(.*?)</table>", body, re.S)
        rows = re.findall(r"<tr>(.*?)</tr>", tables[table_index], re.S)
        pending = [0] * columns  # 每列被上行 rowspan 占用的剩余行数
        for row_html in rows[1:]:  # 跳过表头
            cells = re.findall(r'<td(?: rowspan="(\d+)")?[^>]*>', row_html)
            col = 0
            for span in cells:
                while col < columns and pending[col] > 0:
                    pending[col] -= 1
                    col += 1
                span_n = int(span) if span else 1
                if span_n > 1:
                    pending[col] = span_n - 1
                col += 1
            while col < columns and pending[col] > 0:
                pending[col] -= 1
                col += 1
            self.assertEqual(
                columns, col, f"行有效列数≠{columns}：{row_html[:100]}"
            )

    def test_grid_balance_all_sorts_and_region_table(self):
        # 渠道区数据：TM旗舰A 2 链接（1 负责人+1 代填）、TM旗舰B 1、JD购喝 1
        store = _RosterStore()
        store.rows = self._rows()
        for sort in (None, "store", "owner", "deputy", "target"):
            body = self._page(store, sort=sort)
            self._assert_grid_balance(body, table_index=0, columns=9)
        # 单负责人店铺（含代填）整行无聚合（2026-10-07 运维裁决）：
        # 店铺格/月目标格均无 rowspan，代填聚在同行的代填报人格
        body = self._page(store)
        self.assertIn('<td>TM旗舰A</td>', body)
        self.assertIn('<td><input class="roster-target" data-store="TM旗舰A"',
                      body)
        self.assertEqual(1, body.count('data-store="TM旗舰A"'))

    def test_aggregation_only_for_multi_owner_stores(self):
        # 多负责人才聚合：PDD共管店 2 负责人 → 店铺格/月目标格 rowspan=2
        store = _RosterStore()
        store.rows = self._rows() + [
            _roster_row(id=5, entity_key="PDD共管店", person_name="侯仙姚",
                        aliases=None, note=""),
            _roster_row(id=6, entity_key="PDD共管店", person_name="杨美聪",
                        aliases=None, note=""),
        ]
        body = self._page(store)
        self.assertIn('<td rowspan="2">PDD共管店</td>', body)
        self.assertIn(
            '<td rowspan="2"><input class="roster-target" data-store="PDD共管店"',
            body,
        )
        self._assert_grid_balance(body, table_index=0, columns=9)

    def test_data_cells_follow_header_column_order(self):
        # 单元格内容序 = 表头列序（2026-10-07 列序调整回归：
        # 月目标格必须出现在负责人格之前，而非仅表头换序）
        store = _RosterStore()
        store.rows = self._rows()
        body = self._page(store)
        first_row = re.search(
            r"<tr><td>京东</td>(.*?)</tr>", body, re.S
        ).group(1)
        self.assertLess(
            first_row.index('data-store="JD购喝"'),   # 月目标格
            first_row.index("娄灿斌"),                 # 负责人格
        )

        # 区域表（绍兴 3 链接）同样平衡
        store2 = _RosterStore()
        store2.rows = [
            _roster_row(id=11, scope="shaoxing", entity_type="person",
                        entity_key="绍兴项目部", person_name="潘良峰",
                        aliases=None, note=""),
            _roster_row(id=12, scope="shaoxing", entity_type="person",
                        entity_key="绍兴项目部", person_name="洪强",
                        aliases=None, note=""),
            _roster_row(id=13, scope="shaoxing", entity_type="person",
                        entity_key="诸暨项目部", person_name="周亚平",
                        aliases=None, note=""),
        ]
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with store2.patch_fetch(), store2.patch_audit():
            body2 = client.get("/roster").text
        self._assert_grid_balance(body2, table_index=1, columns=9)


class RosterRegionTemplateTests(unittest.TestCase):
    """区域区段统一链接行模板 + 个人月目标列（2026-10-07 运维裁决，
    目标真源 dim_report_target 入库）。"""

    def test_region_section_unified_template_with_personal_targets(self):
        store = _RosterStore()
        store.rows = [
            _roster_row(id=11, scope="shaoxing", entity_type="person",
                        entity_key="绍兴项目部", person_name="潘良峰",
                        aliases=None, note=""),
            _roster_row(id=12, scope="shaoxing", entity_type="person",
                        entity_key="绍兴项目部", person_name="洪强",
                        aliases=None, note="", enabled=0),
            _roster_row(id=13, scope="shaoxing", entity_type="person",
                        entity_key="诸暨项目部", person_name="周亚平",
                        aliases=None, note=""),
        ]
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_fetch(), store.patch_audit(), \
             mock.patch.object(
                 report_roster, "fetch_person_targets",
                 lambda conn, scope, ym: (
                     {"潘良峰": 383000.0, "洪强": 351000.0}
                     if scope == "shaoxing" else {})):
            body = client.get("/roster").text

        # 与渠道门店同构的统一表头（聚合列靠左：月目标在店铺之后）
        self.assertIn(
            "<th>渠道/地区</th><th>店铺/对象</th><th>月目标（元）</th>"
            "<th>负责人</th><th>代填报人</th>"
            "<th>状态</th><th>备注</th><th>更新</th><th>操作</th>",
            body,
        )
        # 地区整段合并（3 行）、对象按部门合并（绍兴项目部 2 行）
        self.assertIn('<td rowspan="3">绍兴</td>', body)
        self.assertIn('<td rowspan="2">绍兴项目部</td>', body)
        # 个人月目标为可编辑输入（入库真源），含保存按钮
        self.assertIn('class="person-target"', body)
        self.assertIn('data-name="潘良峰" value="383000"', body)
        # 保存按钮带目标月份（2026-10-09 月份切换：默认当月，可 ?month= 切下月）
        self.assertIn("saveRegionTargets('shaoxing', '", body)
        self.assertIn("区域个人月目标的目标月份：", body)
        # 日历选择器（任意月直达）+ 本月/下月快捷链接
        self.assertIn('<input type="month" name="month"', body)
        self.assertRegex(body, r"/roster\?month=20\d{2}-\d{2}")
        # 停用链接：下拉当前值=停用
        self.assertIn('setRosterStatus(12, this.value)', body)
        self.assertIn('<option value="0" selected>停用</option>', body)

    def test_region_section_splits_deputy_into_deputy_column(self):
        # 2026-10-08 修复：区域段此前忽略 role——代填人全显示在负责人
        # 列（宣卓萍/郑洁佩 诸暨门店实例）；应按 role 分列且负责人在前。
        store = _RosterStore()
        store.rows = [
            _roster_row(id=21, scope="shaoxing", entity_type="person",
                        entity_key="诸暨门店", person_name="郑洁佩",
                        aliases=None, note="", role="deputy"),
            _roster_row(id=22, scope="shaoxing", entity_type="person",
                        entity_key="诸暨门店", person_name="蒋丽兰",
                        aliases=None, note=""),
        ]
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_fetch(), store.patch_audit():
            body = client.get("/roster").text

        # 负责人在负责人列（代填报人列空），且排在代填人之前
        self.assertIn("<td>蒋丽兰</td><td></td>", body)
        # 代填人在代填报人列（负责人列空）
        self.assertIn("<td></td><td>郑洁佩</td>", body)
        self.assertLess(body.index("蒋丽兰"), body.index("郑洁佩"))


class RegionTargetsApiTests(unittest.TestCase):
    """区域月目标保存 API（dim_report_target 入库，2026-10-07）。"""

    def test_save_validates_and_upserts_with_actor(self):
        saved = []
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with mock.patch.object(
                report_roster, "upsert_report_target",
                lambda conn, entry, *, actor: saved.append((entry, actor))):
            response = client.post("/api/roster/region-targets", json={
                "scope": "shaoxing",
                "rows": [
                    {"person_name": "潘良峰", "monthly_target": 383000},
                    {"person_name": "周亚平", "monthly_target": None},
                ],
            })

        self.assertEqual(200, response.status_code)
        self.assertEqual(2, response.json()["saved"])
        self.assertEqual(2, len(saved))
        entry, actor = saved[0]
        self.assertEqual(("shaoxing", "潘良峰", 383000.0),
                         (entry.scope, entry.person_name,
                          entry.monthly_target))
        self.assertEqual(ADMIN_USERID, actor)
        self.assertIsNone(saved[1][0].monthly_target)  # 空白 = 清除

    def test_save_accepts_explicit_year_month(self):
        """2026-10-09 月份切换：可指定下月提前录入（结转管道不覆盖人工键）。"""
        saved = []
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with mock.patch.object(
                report_roster, "upsert_report_target",
                lambda conn, entry, *, actor: saved.append(entry)):
            response = client.post("/api/roster/region-targets", json={
                "scope": "hangzhou",
                "year_month": "2099-11",
                "rows": [{"person_name": "余发兴", "monthly_target": 822000}],
            })

        self.assertEqual(200, response.status_code)
        self.assertEqual("2099-11", saved[0].year_month)

    def test_save_rejects_invalid_payload_and_requires_manager(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        for payload in (
            {"scope": "qudao", "rows": []},                 # qudao 不走本表
            {"scope": "nope", "rows": []},
            {"scope": "shaoxing", "rows": "not-a-list"},
            {"scope": "shaoxing",
             "rows": [{"person_name": "潘良峰", "monthly_target": -1}]},
            {"scope": "shaoxing", "rows": [{"person_name": "  "}]},
        ):
            self.assertEqual(
                400, client.post("/api/roster/region-targets",
                                 json=payload).status_code, payload,
            )

        plain = TestClient(_app(viewer=_plain_member_viewer()))
        _login(plain, userid="u-plain")
        self.assertEqual(403, plain.post("/api/roster/region-targets", json={
            "scope": "shaoxing", "rows": [],
        }).status_code)


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


class CalendarPageTests(unittest.TestCase):
    """工作日历页（2026-10-10 DB 裁决层操作面：非技术同学用）。"""

    def _calendar_app(self, connection=None):
        settings = SimpleNamespace(
            **vars(_fake_settings()), calendar_seed_path="seed.json")
        return create_app(
            settings=settings, session_secret=SECRET,
            db_connector=_connector(connection),
            viewer_resolver=_StaticViewerResolver(_admin_viewer()),
        )

    def test_page_renders_grid_with_source_badges(self):
        connection = _Connection(rows_by_keyword={
            # 审计查询的表名包含 dim_calendar，必须先于月份查询命中
            "dim_calendar_override_audit": [
                {"actor": "admin", "action": "import", "business_date": None,
                 "year": 2027, "month": None, "detail": "d",
                 "created_at": "2026-10-10 08:00:00"},
            ],
            "dim_calendar": [
                {"business_date": date(2026, 10, 6), "is_workday": 0,
                 "source": "holiday_cn", "note": "国庆"},
                {"business_date": date(2026, 10, 7), "is_workday": 1,
                 "source": "manual", "note": "改上班"},
            ],
        })
        client = TestClient(self._calendar_app(connection))
        _login(client)

        page = client.get("/calendar?month=2026-10")

        self.assertEqual(200, page.status_code)
        self.assertIn("工作日历", page.text)
        self.assertIn("cal-src-holiday_cn", page.text)   # 法 徽标
        self.assertIn("cal-src-manual", page.text)       # 裁 徽标
        self.assertIn("calendarToggle('2026-10-07', true)", page.text)
        self.assertIn("法定导入", page.text)             # 审计动作中文标签
        self.assertIn("一键导入该年法定假日", page.text)

    def test_page_falls_back_to_current_month_on_bad_param(self):
        client = TestClient(self._calendar_app(_Connection()))
        _login(client)
        self.assertEqual(200, client.get("/calendar?month=not-a-month").status_code)

    def test_toggle_writes_override_and_rewrites_month(self):
        with mock.patch(
            "common.ops_web.app.calendar_store.upsert_override"
        ) as upsert, mock.patch(
            "common.ops_web.app.calendar_store.rewrite_month", return_value=31
        ) as rewrite:
            client = TestClient(self._calendar_app(_Connection()))
            _login(client)
            resp = client.post("/api/calendar/toggle", json={
                "date": "2026-10-07", "is_workday": True, "note": "改上班",
            })

        self.assertEqual(200, resp.status_code)
        _, day, is_workday = upsert.call_args.args
        self.assertEqual(date(2026, 10, 7), day)
        self.assertIs(is_workday, True)
        self.assertEqual(ADMIN_USERID, upsert.call_args.kwargs["actor"])
        self.assertEqual("改上班", upsert.call_args.kwargs["note"])
        _, seed_path, year, month = rewrite.call_args.args
        self.assertEqual("seed.json", seed_path)
        self.assertEqual((2026, 10), (year, month))

    def test_toggle_rejects_bad_payload(self):
        client = TestClient(self._calendar_app(_Connection()))
        _login(client)
        for payload in (
            {"date": "not-a-date", "is_workday": True},
            {"date": "2026-10-07", "is_workday": "yes"},
            {"date": "2026-10-07"},
        ):
            self.assertEqual(
                400,
                client.post("/api/calendar/toggle", json=payload).status_code,
                msg=repr(payload),
            )

    def test_import_year_applies_and_rewrites_twelve_months(self):
        with mock.patch(
            "common.ops_web.app.calendar_store.import_year_overrides",
            return_value={"deleted": 1, "inserted": 3, "holidays": 2,
                          "makeup": 1},
        ) as imp, mock.patch(
            "common.ops_web.app.calendar_store.rewrite_month", return_value=31
        ) as rewrite:
            client = TestClient(self._calendar_app(_Connection()))
            _login(client)
            resp = client.post("/api/calendar/import-year", json={"year": 2027})

        self.assertEqual(200, resp.status_code)
        body = resp.json()
        self.assertEqual(3, body["inserted"])
        self.assertEqual(31 * 12, body["rewritten"])
        self.assertEqual(ADMIN_USERID, imp.call_args.kwargs["actor"])
        self.assertEqual(12, rewrite.call_count)
        self.assertTrue(
            all(call.kwargs.get("strict") is False
                for call in rewrite.call_args_list)
        )

    def test_import_year_holiday_cn_failure_returns_502_with_reason(self):
        with mock.patch(
            "common.ops_web.app.calendar_store.import_year_overrides",
            side_effect=CalendarImportError("holiday-cn 2099 尚未发布"),
        ):
            client = TestClient(self._calendar_app(_Connection()))
            _login(client)
            resp = client.post("/api/calendar/import-year", json={"year": 2099})

        self.assertEqual(502, resp.status_code)
        self.assertIn("尚未发布", resp.json()["reason"])

    def test_reset_month_clears_manual_and_rewrites(self):
        with mock.patch(
            "common.ops_web.app.calendar_store.reset_month_overrides",
            return_value=2,
        ) as reset, mock.patch(
            "common.ops_web.app.calendar_store.rewrite_month", return_value=30
        ):
            client = TestClient(self._calendar_app(_Connection()))
            _login(client)
            resp = client.post(
                "/api/calendar/reset-month", json={"month": "2026-10"})

        self.assertEqual(200, resp.status_code)
        self.assertEqual(2, resp.json()["deleted"])
        _, year, month = reset.call_args.args
        self.assertEqual((2026, 10), (year, month))
        self.assertEqual(ADMIN_USERID, reset.call_args.kwargs["actor"])

    def test_reset_month_rejects_bad_month(self):
        client = TestClient(self._calendar_app(_Connection()))
        _login(client)
        self.assertEqual(
            400,
            client.post(
                "/api/calendar/reset-month", json={"month": "2026-13"}
            ).status_code,
        )


if __name__ == "__main__":
    unittest.main()
