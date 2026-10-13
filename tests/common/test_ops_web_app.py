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
from common.ops_web.app import create_app
from common.public_data import (
    bi_authz, calendar_store, channel_target, ops_control,
    recipient_override, report_roster,
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
            # 群视角是唯一视角（2026-10-13 收敛）：不带 view 即按管理群分组。
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
        self.assertIn("按管理群", body)          # 唯一视角（无功能类别 tab）
        self.assertIn("「杭州」群", body)        # 管理群（robot- 家族区域映射）
        self.assertIn("<details", body)          # 群分组原生折叠
        self.assertIn("<summary", body)
        self.assertIn("操作…", body)             # 操作列改下拉
        self.assertIn('data-service="robot-hangzhou"', body)
        self.assertIn('data-schedule="0 2 * * *"', body)   # 行内带原 cron 供改播报时间
        self.assertIn("改播报时间", body)        # 操作列「改播报时间」
        self.assertIn("<th>播报时间</th>", body)     # 列头改名
        self.assertIn('id="confirm-mask"', body)     # 弹窗确认挂载点
        self.assertIn('id="confirm-text"', body)
        self.assertIn('id="schedule-mask"', body)     # 改播报时间弹窗挂载点
        self.assertIn('id="schedule-edit-times"', body)      # 时点槽容器
        self.assertIn('id="schedule-edit-add"', body)        # ＋时点
        self.assertIn('id="schedule-edit-ok"', body)         # 确定
        self.assertIn('id="schedule-edit-unsupported"', body)  # 非时刻型提示
        self.assertNotIn('id="schedule-edit-hour"', body)     # 旧单时点控件已删
        self.assertNotIn('id="schedule-edit-minute"', body)
        # 弹窗内一并显示「当前播报时间」并实时预览改后结果
        self.assertIn("当前播报时间", body)
        self.assertIn('id="schedule-edit-rule"', body)
        self.assertIn('id="schedule-edit-preview"', body)
        self.assertIn("function parseBroadcastTimes", body)
        self.assertIn("function buildBroadcastCron", body)
        self.assertNotIn("function rewriteCronTime", body)  # 旧实现已删
        self.assertIn("function updateScheduleEditPreview", body)
        self.assertNotIn(">finished<", body)     # 英文状态值不外显
        self.assertIn("2026-09-30 18:00:00", body)   # UTC 存储 → 北京时间
        self.assertNotIn("10:00:00", body)           # UTC 原文不外显

    def test_pipelines_page_group_and_function_classification(self):
        """群视角分组：家族后缀/显式区域映射、跨区域线与平台线归组。"""
        fleet = (
            "leaderboard-hangzhou",   # 家族：播报 + 「杭州」群
            "pages-qudao-t1",         # 显式区域：页面 + 「渠道」群（后缀非区域）
            "channel-missing-check",  # 显式：催办 + 「渠道」群
            "sync-wdt",               # 应用线：同步 + 无群
            "dingtalk-gateway",       # 平台线：钉钉 + 无群
        )
        client = TestClient(_pipeline_app(viewer=_admin_viewer(), fleet=fleet))
        _login(client)
        body = client.get("/pipelines").text

        self.assertIn("「杭州」群（1 项）", body)   # leaderboard- 家族区域映射
        self.assertIn("「渠道」群（2 项）", body)   # 两条渠道线同群聚为一组
        self.assertIn("平台 / 无群（2 项）", body)  # sync-wdt + dingtalk-gateway
        self.assertIn("榜单播报", body)            # leaderboard- 家族中文名

    def test_pipelines_page_view_tabs_default_group(self):
        """群视角唯一（2026-10-13 收敛）：无「按功能类别」tab、无 ?view=class。"""
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        body = client.get("/pipelines").text

        self.assertIn("按管理群", body)
        self.assertNotIn("按功能类别", body)
        self.assertNotIn('href="/pipelines?view=group"', body)
        self.assertNotIn('href="/pipelines?view=class"', body)
        self.assertNotIn('class="tabs"', body)
        # 群视角：分组头 + 项数；不再是功能分表
        self.assertIn("「杭州」群（1 项）", body)
        self.assertNotIn("<h2>催办</h2>", body)

    def test_pipelines_page_group_view_places_platform_last_keeps_rows(self):
        """群视角：按区域分组、平台/无群恒置末位、任一行不丢。"""
        fleet = (
            "robot-hangzhou",        # 「杭州」群
            "channel-missing-check",  # 「渠道」群（跨区域线单独成组）
            "sync-wdt",               # 平台 / 无群
            "dingtalk-gateway",       # 平台 / 无群
        )
        client = TestClient(_pipeline_app(viewer=_admin_viewer(), fleet=fleet))
        _login(client)
        body = client.get("/pipelines").text

        self.assertIn("「杭州」群（1 项）", body)
        self.assertIn("「渠道」群（1 项）", body)
        self.assertIn("平台 / 无群（2 项）", body)
        # 顺序：区域组在前，平台/无群收尾
        self.assertLess(body.index("「杭州」群（1 项）"),
                        body.index("「渠道」群（1 项）"))
        self.assertLess(body.index("「渠道」群（1 项）"),
                        body.index("平台 / 无群（2 项）"))
        # 每行都在（不得因分组丢行）
        for sid in fleet:
            self.assertIn(sid, body)
        # 群视角行内不再重复「管理群」列
        self.assertNotIn("<th>管理群</th>", body)

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

    def test_pipelines_page_reminder_targets_with_overrides(self):
        """任务级覆盖（方案 B）：有效名单 = 名册 ± include/exclude，带徽标。"""
        connection = _Connection({
            "dim_robot_member": [
                ("u1", "张三", "hangzhou", "门店一部"),
                ("u2", "李四", "hangzhou", "门店二部"),
            ],
            "entity_type` = 'person'": [("张三",), ("李四",)],
            "dim_recipient_override": [
                {"id": 1, "service_id": "robot-hangzhou", "mode": "exclude",
                 "target_type": "name", "target_value": "李四",
                 "resolved_user_id": None, "enabled": 1, "note": None,
                 "updated_by": "ops", "updated_at": None},
                {"id": 2, "service_id": "robot-hangzhou", "mode": "include",
                 "target_type": "user_id", "target_value": "u9",
                 "resolved_user_id": "u9", "enabled": 1, "note": None,
                 "updated_by": "ops", "updated_at": None},
            ],
        })
        client = TestClient(_pipeline_app(
            viewer=_admin_viewer(), fleet=("robot-hangzhou",),
            connection=connection))
        _login(client)
        body = client.get("/pipelines").text

        self.assertIn("张三", body)
        self.assertNotIn("李四", body)      # exclude 按姓名移除
        self.assertIn("u9", body)           # include 追加（未知 id 回显）
        self.assertIn("（2 人 · 名册）", body)
        self.assertIn("+1 覆盖", body)
        self.assertIn("−1 移除", body)

    def test_pipelines_page_reminder_override_read_fail_soft(self):
        """覆盖表未迁移/读失败 → 回退名册口径，本列不整体「读取失败」。"""
        connection = _Connection({
            "dim_robot_member": [("u1", "张三", "hangzhou", "门店一部")],
        })
        client = TestClient(_pipeline_app(
            viewer=_admin_viewer(), fleet=("robot-hangzhou",),
            connection=connection))
        _login(client)
        with mock.patch.object(recipient_override, "fetch_overrides",
                               side_effect=RuntimeError("no table")):
            body = client.get("/pipelines").text
        self.assertIn("张三", body)
        self.assertNotIn("读取失败", body)

    def test_pipelines_page_reminder_targets_fail_open_all_members(self):
        """名册无记录 → 区域在册填报人全员兜底（与机器人 fail-open 同语义）；
        跨多部门时按部门归组展示（2026-10-13 联动组织编排）。"""
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

        self.assertIn("门店一部：张三", body)   # 按部门归组（非旧的「、」连接）
        self.assertIn("门店二部：李四", body)
        self.assertIn("（2 人 · 全员兜底）", body)

    def test_pipelines_page_reminder_targets_empty_roster_hint(self):
        """渠道名册未登记 → 如实标注机器人自动回退读门店月目标表。"""
        client = TestClient(_pipeline_app(
            viewer=_admin_viewer(), fleet=("channel-missing-check",)))
        _login(client)
        body = client.get("/pipelines").text
        self.assertIn("自动回退读门店月目标表", body)

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

    def test_pipelines_page_recipient_drawer_shows_base_block(self):
        """P1（2026-10-12）：抽屉脚本随页下发「本线基名单」区块 + 去名册深链，
        并区分「本任务覆盖」与「整片区域名册」两层。"""
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        body = client.get("/pipelines").text

        self.assertIn("function renderRecipientBase", body)
        self.assertIn("本线基名单", body)
        self.assertIn("function renderRecipientExplain", body)  # P2 归因
        self.assertIn("口径说明：为什么有人不在名单", body)
        self.assertIn('href="/roster">填报名册', body)   # 名册层深链
        self.assertIn('href="/member-grants">成员与授权', body)  # 在职层深链

    def test_pipelines_page_service_deep_link_opens_drawer(self):
        """P3（2026-10-12）：?service=<id> 深链随页下发自动开抽屉脚本；
        未知/非法标识不注入。"""
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        body = client.get("/pipelines",
                          params={"service": "robot-hangzhou"}).text
        self.assertIn("openRecipientOverride('robot-hangzhou')", body)

        # 不在舰队清单里的标识：不注入（避免无意义开抽屉）。
        other = client.get("/pipelines", params={"service": "nope"}).text
        self.assertNotIn("openRecipientOverride('nope')", other)
        # 非法字符（防注入）：不注入。
        bad = client.get("/pipelines",
                         params={"service": "x');alert(1)//"}).text
        self.assertNotIn("alert(1)", bad)

    def test_pipelines_page_category_truth_overrides_derivation(self):
        """category 真源优先：注册表配 category=催办 时，即便 service_id 推导为
        「同步」，该行仍按催办装载（收受影响人列走催办取值域）。"""
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
        seen = []

        def _spy(conn, service_id):
            seen.append(service_id)
            return ()

        with mock.patch.object(recipient_override, "fetch_overrides", _spy):
            body = client.get("/pipelines").text

        self.assertEqual(["sync-wdt"], seen)   # 真源 = 催办 → 催办装载被触发
        self.assertIn("收受影响人", body)     # 催办行入口（列）仍在
        self.assertIn("sync-wdt", body)       # 群视角仍在（平台 / 无群）

    def test_pipelines_page_add_form_offers_category_select(self):
        """新增表单带 category 下拉：空值=按标识推导，七值可选。"""
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        body = client.get("/pipelines").text

        self.assertIn('name="category"', body)
        self.assertIn("功能（留空按标识推导）", body)
        for category in ("同步", "加工", "播报", "催办", "页面", "钉钉", "平台"):
            self.assertIn(f'<option value="{category}">{category}</option>', body)

    def test_pipelines_page_schedule_builder_form(self):
        """定时构建器（2026-10-10 裁决）：预设+时点选择+自定义 cron+预览，
        非技术同学不手写 cron。"""
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        body = client.get("/pipelines").text

        self.assertIn('id="pipeline-add-form"', body)
        self.assertIn('name="schedule_preset"', body)
        for preset in ('value="daily"', 'value="weekday"', 'value="weekly"',
                       'value="monthly"', 'value="hourly"', 'value="custom"'):
            self.assertIn(preset, body)
        # 时点选择拆为「时(0-23)」「分(0-59)」两维下拉（2026-10-13 裁决：
        # 分维开放全量 0..59，非仅 00/30）。
        self.assertIn('name="schedule_hour"', body)
        self.assertIn('name="schedule_minute"', body)
        self.assertNotIn('name="schedule_time"', body)
        self.assertNotIn('type="time"', body)
        for hour in range(24):
            self.assertIn(f'<option value="{hour}"', body)
        self.assertIn('<option value="0" selected>00</option>', body)  # 分默认 0
        self.assertIn('<option value="30">30</option>', body)
        self.assertIn('<option value="59">59</option>', body)          # 分上限
        self.assertIn('name="schedule_dow"', body)     # 每周（默认隐藏）
        self.assertIn('name="schedule_dom"', body)     # 每月（默认隐藏）
        self.assertIn('name="schedule"', body)         # 自定义 cron（默认隐藏）
        self.assertIn('id="schedule-preview"', body)
        # cron 输入不再 required（隐藏态不参与提交校验，由 JS 合成）
        self.assertNotIn(
            'name="schedule" placeholder="cron（分 时 日 月 周）" required',
            body,
        )

    def test_pipelines_page_weekday_cron_is_humanized(self):
        """工作日区间 cron（1-5）人性化：与构建器「工作日」预设同形。"""
        configs = {"sync-wdt": {"schedule": "0 18 * * 1-5"}}
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

        self.assertIn("工作日（周一至周五）18:00", body)
        self.assertIn("0 18 * * 1-5", body)   # 原表达式小字备查

    def test_pipeline_audit_page_renders_entries(self):
        store = _PipelineStore()
        store.audit_rows = [
            ("admin", "add", "robot-x", "0 2 * * *", "2026-09-30 10:00:00"),
            ("admin", "schedule", "robot-hangzhou", "30 20 * * 1-5",
             "2026-10-12 10:00:00"),
        ]
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_audit_rows():
            body = client.get("/pipelines/audit").text

        self.assertIn("robot-x", body)
        self.assertIn("新增", body)  # action=add 中文化
        self.assertIn("改定时", body)  # action=schedule 中文化
        self.assertIn("30 20 * * 1-5", body)

    def test_pipeline_audit_page_has_cross_links(self):
        """G2（2026-10-12 断点报告）：审计分散四页，任务审计页给交叉索引。"""
        store = _PipelineStore()
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_audit_rows():
            body = client.get("/pipelines/audit").text

        self.assertIn('href="/roster"', body)     # 名册变更流水
        self.assertIn('href="/audit"', body)      # 授权审计
        self.assertIn('href="/calendar"', body)   # 日历改判流水

    def test_action_label_covers_target(self):
        """G1：名册月目标保存动作中文化（原回退英文原文 target）。"""
        from common.ops_web.app import _ACTION_LABEL, _label
        self.assertEqual("月目标", _label(_ACTION_LABEL, "target"))

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
        # 多段（播报时间多时点，2026-10-13）：逐段中文、以「；」连接
        self.assertEqual("每天 08:00；每天 12:30",
                         _cron_zh("0 8 * * *;30 12 * * *"))
        self.assertEqual("每天 02:00", _cron_zh("0 2 * * *;"))   # 空段忽略
        self.assertIsNone(_cron_zh("0 8 * * *;0 0 1 1 *"))  # 任一段不可识别
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
        from common.ops_web.app import _service_region, _pipeline_group_title
        self.assertEqual("hangzhou", _service_region("robot-check-hangzhou"))
        self.assertEqual("hangzhou", _service_region("robot-hangzhou"))
        self.assertIn("「杭州」群", _pipeline_group_title("hangzhou"))

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


class _BrokenConfigSource:
    """config_source 替身：读注册表即抛（模拟 Nacos 读取故障）。"""

    def get_pipeline(self, service_id):
        raise RuntimeError("registry down")


class PipelineScheduleUpdateTests(unittest.TestCase):
    """改播报时间（2026-10-13）：read-modify-write，只换 cron 分/时多段。

    请求体 ``{service_id, times:[{hour,minute}, ...]}``（1..3 个时点、任意
    分 0-59、去重升序）；日/月/周形态（工作日、每月 N 日）取自现有首段保留，
    且发布用原配置 replace（不得丢 sources/reads）。
    """

    def _client(self, *, configs=None, publisher=None, config_source=None,
                fleet=("robot-hangzhou",), login=True):
        app = create_app(
            settings=_fake_settings(),
            session_secret=SECRET,
            db_connector=_connector(),
            viewer_resolver=_StaticViewerResolver(_admin_viewer()),
            fleet_source=lambda: fleet,
            config_source=(config_source if config_source is not None
                           else StaticConfigSource(
                               configs or {"robot-hangzhou":
                                           {"schedule": "0 18 * * 1-5"}})),
            config_publisher=publisher,
        )
        client = TestClient(app)
        if login:
            _login(client)
        return client

    def test_update_rewrites_time_and_preserves_weekday(self):
        store = _PipelineStore()
        publisher = _RecordingPublisher()
        client = self._client(publisher=publisher)
        with store.patch_audit():
            response = client.post(
                "/api/pipelines/update",
                json={"service_id": "robot-hangzhou",
                      "times": [{"hour": 20, "minute": 30}]},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual("30 20 * * 1-5", response.json()["schedule"])
        self.assertEqual("30 20 * * 1-5", publisher.published[0].schedule)
        self.assertEqual(True, publisher.published[0].enabled)
        self.assertEqual(
            [(ADMIN_USERID, "schedule", "robot-hangzhou", "30 20 * * 1-5")],
            store.audit,
        )

    def test_update_accepts_multiple_times_sorted_and_deduped(self):
        """多时点：去重、按 (hour,minute) 升序，';' 连接多段 cron。"""
        store = _PipelineStore()
        publisher = _RecordingPublisher()
        client = self._client(
            configs={"robot-hangzhou": {"schedule": "0 2 * * *", "enabled": True}},
            publisher=publisher)
        with store.patch_audit():
            response = client.post(
                "/api/pipelines/update",
                json={"service_id": "robot-hangzhou",
                      "times": [{"hour": 18, "minute": 0},
                                {"hour": 8, "minute": 0},
                                {"hour": 12, "minute": 30}]},
            )

        self.assertEqual(200, response.status_code)
        expected = "0 8 * * *;30 12 * * *;0 18 * * *"
        self.assertEqual(expected, response.json()["schedule"])
        self.assertEqual(expected, publisher.published[0].schedule)
        self.assertEqual(
            [(ADMIN_USERID, "schedule", "robot-hangzhou", expected)],
            store.audit,
        )

    def test_update_accepts_any_minute(self):
        """分 0-59 全量合法（旧实现仅 00/30；minute=15 现应成功）。"""
        publisher = _RecordingPublisher()
        client = self._client(publisher=publisher)
        response = client.post(
            "/api/pipelines/update",
            json={"service_id": "robot-hangzhou",
                  "times": [{"hour": 1, "minute": 15}]},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("15 1 * * 1-5", response.json()["schedule"])

    def test_update_falls_back_to_daily_for_non_cron_schedule(self):
        publisher = _RecordingPublisher()
        client = self._client(
            configs={"robot-hangzhou": {"schedule": None, "enabled": True}},
            publisher=publisher,
        )
        response = client.post(
            "/api/pipelines/update",
            json={"service_id": "robot-hangzhou",
                  "times": [{"hour": 7, "minute": 0}]},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("0 7 * * *", response.json()["schedule"])
        self.assertEqual("0 7 * * *", publisher.published[0].schedule)

    def test_update_preserves_sources_and_reads(self):
        publisher = _RecordingPublisher()
        client = self._client(
            configs={"robot-hangzhou": {"schedule": "0 2 * * *", "enabled": True,
                                        "sources": ["dingtalk"],
                                        "reads": ["dim_x"]}},
            publisher=publisher,
        )
        response = client.post(
            "/api/pipelines/update",
            json={"service_id": "robot-hangzhou",
                  "times": [{"hour": 9, "minute": 30}]},
        )

        self.assertEqual(200, response.status_code)
        published = publisher.published[0]
        self.assertEqual(("dingtalk",), published.sources)
        self.assertEqual(("dim_x",), published.reads)
        self.assertEqual("30 9 * * *", published.schedule)

    def test_update_rejects_bad_payload_and_unknown_service(self):
        client = self._client(publisher=_RecordingPublisher())
        three = [{"hour": 1, "minute": 0}, {"hour": 2, "minute": 0},
                 {"hour": 3, "minute": 0}]
        for payload in (
            {"service_id": "BAD ID", "times": three},             # 非法 id
            {"service_id": "nope", "times": three},               # 不在舰队
            {"service_id": "robot-hangzhou"},                     # 缺 times
            {"service_id": "robot-hangzhou", "times": None},      # 非 list
            {"service_id": "robot-hangzhou", "times": []},        # 空
            {"service_id": "robot-hangzhou",                      # 超 3 时点
             "times": [{"hour": h, "minute": 0} for h in (1, 2, 3, 4)]},
            {"service_id": "robot-hangzhou", "times": [1]},       # 项非 dict
            {"service_id": "robot-hangzhou",                      # 时越界
             "times": [{"hour": 24, "minute": 0}]},
            {"service_id": "robot-hangzhou",
             "times": [{"hour": -1, "minute": 0}]},
            {"service_id": "robot-hangzhou",                      # 分越界
             "times": [{"hour": 1, "minute": 60}]},
            {"service_id": "robot-hangzhou",                      # 非 int
             "times": [{"hour": "1", "minute": 0}]},
            {"service_id": "robot-hangzhou",                      # bool
             "times": [{"hour": True, "minute": 0}]},
            {"service_id": "robot-hangzhou",                      # bool 分
             "times": [{"hour": 1, "minute": True}]},
        ):
            self.assertEqual(
                400, client.post("/api/pipelines/update", json=payload).status_code,
                payload,
            )

    def test_update_requires_admin(self):
        client = self._client(publisher=_RecordingPublisher(), login=False)
        self.assertEqual(
            401, client.post("/api/pipelines/update",
                             json={"service_id": "robot-hangzhou",
                                   "times": [{"hour": 1, "minute": 0}]}
                             ).status_code
        )

    def test_update_503_when_no_publisher(self):
        client = self._client(publisher=None)
        self.assertEqual(
            503, client.post("/api/pipelines/update",
                             json={"service_id": "robot-hangzhou",
                                   "times": [{"hour": 1, "minute": 0}]}
                             ).status_code
        )

    def test_update_503_when_read_or_publish_fails(self):
        # 读注册表失败
        read_fail = self._client(config_source=_BrokenConfigSource(),
                                 publisher=_RecordingPublisher())
        self.assertEqual(
            503, read_fail.post("/api/pipelines/update",
                                json={"service_id": "robot-hangzhou",
                                      "times": [{"hour": 1, "minute": 0}]}
                                ).status_code
        )
        # 发布失败
        publish_fail = self._client(
            publisher=_RecordingPublisher(error=RuntimeError("publish down")))
        self.assertEqual(
            503, publish_fail.post("/api/pipelines/update",
                                   json={"service_id": "robot-hangzhou",
                                         "times": [{"hour": 1, "minute": 0}]}
                                   ).status_code
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
        self.assertIn("不拦截", body)            # 名册空范围提示（原 fail-open 术语）
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

    def test_roster_page_scope_backlinks_to_pipelines_drawer(self):
        """P3（2026-10-12）：名册 scope 区块显示「被这些任务催办」，并把
        任务名深链到定时任务页对应行的抽屉（service_id → scope 反查）。"""
        store = _RosterStore()
        configs = {
            "robot-hangzhou": {}, "robot-check-hangzhou": {},
            "channel-missing-check": {}, "sync-wdt": {},
        }
        client = TestClient(_fleet_app(
            viewer=_admin_viewer(), fleet=tuple(configs), configs=configs))
        _login(client)
        with store.patch_fetch(), store.patch_audit():
            body = client.get("/roster").text

        self.assertIn("被这些任务催办", body)
        # 杭州 scope：两条区域催办任务，深链到 /pipelines?service=...
        self.assertIn('href="/pipelines?service=robot-hangzhou"', body)
        self.assertIn('href="/pipelines?service=robot-check-hangzhou"', body)
        # 渠道 scope：channel-missing-check（_service_region == qudao）
        self.assertIn('href="/pipelines?service=channel-missing-check"', body)
        # 非催办任务（同步类）不进反向索引
        self.assertNotIn('href="/pipelines?service=sync-wdt"', body)

    def test_roster_page_backlinks_fail_soft_without_fleet(self):
        """无舰队清单（fleet_source=None）时名册页照常渲染，仅省略反向索引行。"""
        store = _RosterStore()
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_fetch(), store.patch_audit():
            body = client.get("/roster").text
        self.assertIn("填报名册", body)
        self.assertIn("新增名册记录", body)
        self.assertNotIn("被这些任务催办（改本范围名册会波及）", body)


class RecipientsOverviewTests(unittest.TestCase):
    """收件人总览页（P4，2026-10-12）：只读、按区域/scope 一屏展示
    「在职 ∩ 名册 ∪/∖ 覆盖」的层层结果，按区块 fail-soft（不整页 503）。"""

    _FLEET = ("robot-hangzhou", "robot-check-hangzhou",
              "channel-missing-check", "sync-wdt")

    def _client(self, *, fleet=None, configs=None):
        fleet = self._FLEET if fleet is None else fleet
        configs = configs if configs is not None else {
            service_id: {} for service_id in fleet}
        client = TestClient(_fleet_app(
            viewer=_admin_viewer(), fleet=tuple(fleet), configs=configs))
        _login(client)
        return client

    def test_overview_groups_by_scope_base_and_effective_with_badge(self):
        def _roster_names(conn, scope=None):
            return frozenset({"张三", "王五"}) if scope == "hangzhou" else None

        def _overrides(conn, sid):
            if sid != "robot-hangzhou":
                return []
            return [{
                "id": 1, "service_id": "robot-hangzhou", "mode": "include",
                "target_type": "user_id", "target_value": "u9",
                "resolved_user_id": "u9", "enabled": 1, "note": None,
                "updated_at": None,
            }]

        owners = {"天猫-A店": {"张三"}, "京东-B店": {"李四"}}
        with mock.patch.object(bi_authz, "fetch_active_members",
                               lambda conn: list(_MEMBERS)), \
                mock.patch.object(report_roster, "fetch_scope_person_names",
                                  _roster_names), \
                mock.patch.object(report_roster, "fetch_store_owner_map",
                                  lambda conn, scope, roles=None: owners), \
                mock.patch.object(recipient_override, "fetch_overrides",
                                  _overrides):
            response = self._client().get("/recipients")

        self.assertEqual(200, response.status_code)
        body = response.text
        self.assertIn("收件人总览", body)
        # 杭州：基名单 = 在职(张三/王五) ∩ 名册(张三/王五)，来源人话。
        self.assertIn("<h2>杭州", body)
        self.assertIn("名册（hangzhou）∩ 在职成员", body)
        self.assertIn("张三、王五", body)
        self.assertIn("（2 条催办任务）", body)
        # 有效名单叠加覆盖（+1 覆盖），任务深链回定时任务页抽屉。
        self.assertIn("+1 覆盖", body)
        self.assertIn("u9", body)
        self.assertIn('href="/pipelines?service=robot-hangzhou"', body)
        self.assertIn('href="/pipelines?service=robot-check-hangzhou"', body)
        # 渠道：基名单 = 名册 qudao·owner，带门店数徽标。
        self.assertIn("店铺负责人（名册 qudao·owner）", body)
        self.assertIn("张三、李四", body)
        self.assertIn("2 店", body)
        self.assertIn('href="/pipelines?service=channel-missing-check"', body)
        # 非催办任务不进总览（与 P3 反查同一取值域）。
        self.assertNotIn('href="/pipelines?service=sync-wdt"', body)
        # 空 scope 给一句人话，不留空。
        self.assertIn("本范围暂无绑定的催办任务。", body)

    def test_overview_fail_soft_when_context_load_fails(self):
        """只读上下文装载故障：各区块给提示，页面仍 200（不整页 503）。"""
        def _boom(conn):
            raise RuntimeError("members down")

        with mock.patch.object(bi_authz, "fetch_active_members", _boom):
            response = self._client().get("/recipients")

        self.assertEqual(200, response.status_code)
        body = response.text
        self.assertIn("收件人总览", body)
        self.assertIn("名单读取失败，请稍后重试。", body)

    def test_overview_renders_without_fleet(self):
        """无舰队清单：仍按 scope 展示基名单，只是无催办任务行。"""
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        response = client.get("/recipients")
        self.assertEqual(200, response.status_code)
        body = response.text
        self.assertIn("收件人总览", body)
        self.assertIn("本范围暂无绑定的催办任务。", body)
        self.assertNotIn('href="/pipelines?service=', body)

    def test_overview_requires_admin(self):
        client = TestClient(_fleet_app(viewer=_admin_viewer()))
        response = client.get("/recipients", follow_redirects=False)
        self.assertEqual(302, response.status_code)


class RecipientRulesTests(unittest.TestCase):
    """口径收口（P5，2026-10-12，设计稿 §3）：名册 / 覆盖 / 授权三条边界
    只在「收件人总览」完整渲染一处；成员与授权 / 填报名册 / 授权现状三页
    复用逐字一致的口径指针，避免同一套话散落多页各说各话。"""

    _POINTER = "名册定资格、覆盖定单条例外、授权定系统访问（与催办无关）"

    def _admin_client(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        return client

    def test_overview_renders_canonical_boundary_block(self):
        body = self._admin_client().get("/recipients").text
        self.assertIn("口径说明：名册 / 覆盖 / 授权各管什么", body)
        self.assertIn("（在职成员 ∩ 名册）∪ 任务级覆盖 − 移除", body)
        # 三条边界的关键裁决点都在权威区里（与设计稿 §3 逐条对应）
        self.assertIn("业务资格", body)                 # 名册 = 资格
        self.assertIn("scope（区域/渠道）", body)
        self.assertIn("service_id（单条线）", body)      # 覆盖 = 单条任务
        self.assertIn("不改正文、不改资格", body)
        self.assertIn("与「谁能被催办」<b>无关</b>", body)  # 授权与催办无关
        self.assertIn("名册定「资格」", body)
        # 三个入口深链
        self.assertIn('href="/roster"', body)
        self.assertIn('href="/pipelines"', body)
        self.assertIn('href="/grants"', body)

    def test_roster_page_shares_pointer(self):
        """「填报名册」仍共享口径指针（成员与授权 / 授权现状已停用）。"""
        store = _RosterStore()
        client = self._admin_client()
        with store.patch_fetch(), store.patch_audit():
            roster = client.get("/roster").text
        self.assertIn(self._POINTER, roster)
        self.assertIn('href="/recipients"', roster)

    def test_decommissioned_pages_do_not_carry_pointer(self):
        """「成员与授权」「授权现状」已停用（2026-10-13），不再渲染口径指针。"""
        members = self._admin_client().get("/member-grants").text
        grants = self._admin_client().get("/grants").text
        for page, body in (("成员与授权", members), ("授权现状", grants)):
            with self.subTest(page=page):
                self.assertNotIn(self._POINTER, body)


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
    def test_members_page_shows_decommission_notice(self):
        """「成员与授权」页已停用（2026-10-13），引导跳转组织编排。"""
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        body = client.get("/member-grants").text
        self.assertIn("已合并至", body)
        self.assertIn('href="/members"', body)

    def test_members_page_no_longer_reads_db(self):
        """停用后页面不读成员表，DB 故障不再 503。"""
        client = TestClient(
            _app(viewer=_admin_viewer(), connector_error=RuntimeError("down"))
        )
        _login(client)
        response = client.get("/member-grants")
        self.assertEqual(200, response.status_code)

    def test_nav_flat_links_are_grouped_by_purpose(self):
        """顶栏分组：日常 / 权限 / 系统 / 记录。
        「成员与授权」「授权现状」已停用（2026-10-13），权限组仅留组织编排。"""
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        body = client.get("/member-grants").text

        self.assertEqual(4, body.count('class="nav-group"'))
        for label in ("日常", "权限", "系统", "记录"):
            self.assertIn(f'nav-label">{label}<', body)

        def group(label):
            tail = body.split(f'nav-label">{label}<', 1)[1]
            return tail.split("</span>", 1)[0]

        self.assertIn('href="/roster"', group("日常"))
        self.assertIn('href="/calendar"', group("日常"))
        # 权限组仅保留组织编排
        self.assertIn('href="/members"', group("权限"))
        self.assertNotIn('href="/member-grants"', group("权限"))
        self.assertNotIn('href="/grants"', group("权限"))
        self.assertIn('href="/pipelines"', group("系统"))
        self.assertIn('href="/records"', group("记录"))
        self.assertIn("<nav>", body)


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
    def test_grants_page_shows_decommission_notice(self):
        """「授权现状」页已停用（2026-10-13），引导联系管理员。"""
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        body = client.get("/grants").text
        self.assertIn("授权管理功能已停用", body)
        self.assertIn('href="/members"', body)

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

        self.assertIn("授权", body)       # 动作中文化（原 "grant"）
        self.assertIn("看板页面", body)   # 类型中文化（原 "scope"）
        self.assertIn("u-zhang", body)


class RecordsPageTests(unittest.TestCase):
    """统一「操作记录」页（任务3 2026-10-12：4 处审计收口 + 运行流水）。"""

    _GRANT = [{
        "actor": ADMIN_USERID, "action": "grant", "user_id": "u-zhang",
        "grant_type": "scope", "grant_key": "fin", "note": "",
        "created_at": "2026-09-21 10:00:00.000000",
    }]
    _PIPELINE = [{
        "actor": ADMIN_USERID, "action": "toggle",
        "service_id": "robot-hangzhou", "detail": "停用",
        "created_at": "2026-09-21 11:00:00.000000",
    }]
    _ROSTER = [{
        "actor": ADMIN_USERID, "action": "add", "scope": "hangzhou",
        "entity_key": "A店", "person_name": "张三", "detail": "",
        "created_at": "2026-09-21 12:00:00.000000",
    }]
    _CALENDAR = [{
        "actor": ADMIN_USERID, "action": "upsert",
        "business_date": date(2026, 9, 21), "year": 2026, "month": 9,
        "detail": "改上班", "created_at": "2026-09-21 13:00:00.000000",
    }]
    _HISTORY = [{
        "id": 1, "service_id": "robot-hangzhou", "trigger_type": "cron",
        "status": "success", "exit_code": 0, "note": "",
        "started_at": "2026-09-21 14:00:00.000000",
        "finished_at": "2026-09-21 14:05:00.000000",
    }]
    _RECIPIENT = [{
        "actor": ADMIN_USERID, "action": "add", "service_id": "robot-hangzhou",
        "mode": "include", "target_type": "user_id", "target_value": "u9",
        "detail": "", "created_at": "2026-09-21 15:00:00.000000",
    }]

    @contextmanager
    def _patched(self):
        with mock.patch.object(bi_authz, "fetch_audit_rows",
                               lambda conn: list(self._GRANT)), \
             mock.patch.object(ops_control, "fetch_pipeline_audit",
                               lambda conn, limit=200: list(self._PIPELINE)), \
             mock.patch.object(report_roster, "fetch_roster_audit",
                               lambda conn, limit=200: list(self._ROSTER)), \
             mock.patch.object(calendar_store, "fetch_recent_audits",
                               lambda conn, limit=200: list(self._CALENDAR)), \
             mock.patch.object(ops_control, "fetch_run_history",
                               lambda conn, limit=200, service_id=None:
                               list(self._HISTORY)):
            yield

    def _client(self):
        client = TestClient(_app(viewer=_admin_viewer(),
                                 connection=_Connection({})))
        _login(client)
        return client

    def test_records_merges_all_five_sources(self):
        with self._patched():
            body = self._client().get("/records").text
        for source in ("授权", "定时任务", "填报名册", "工作日历", "定时运行"):
            self.assertIn(source, body)
        self.assertIn("fin", body)       # 授权对象
        self.assertIn("张三", body)      # 名册对象
        self.assertIn("系统", body)      # 运行流水操作人

    def test_records_filters_by_source(self):
        with self._patched():
            body = self._client().get(
                "/records", params={"source": "填报名册"}).text
        self.assertIn("张三", body)
        self.assertNotIn("fin", body)    # 授权行已被来源过滤

    def test_write_feedback_banner_present(self):
        """任务3：写入后有成功/失败中文提示（替换静默 reload）。"""
        with self._patched():
            body = self._client().get("/records").text
        self.assertIn('id="flash"', body)
        self.assertIn("showFlash", body)
        self.assertIn("操作成功", body)

    def test_records_includes_recipient_override_source(self):
        """方案 B：收件人覆盖审计并入操作记录（来源/动作/对象中文化）。"""
        with self._patched(), mock.patch.object(
                recipient_override, "fetch_recent_audits",
                lambda conn, limit=200: list(self._RECIPIENT)):
            body = self._client().get("/records").text
        self.assertIn("收件人覆盖", body)
        self.assertIn("robot-hangzhou", body)
        self.assertIn("追加", body)     # mode=include 中文化
        self.assertIn("账号", body)     # target_type=user_id 中文化
        self.assertIn("u9", body)

    def test_records_fails_soft_when_override_audit_read_fails(self):
        """缺表（迁移未跑）或读异常：只跳过该来源，不整页 503。

        #62 上线后曾实测踩到：deploy 不跑迁移，dim_recipient_override_audit
        缺失时 /records 整页 503。此用例钉住 fail-soft 口径——其余来源照常
        展示，来源筛选项不因该来源读失败而出现「读取失败」。
        """
        def _boom(*_args, **_kwargs):
            raise RuntimeError("table missing")

        with self._patched(), mock.patch.object(
                recipient_override, "fetch_recent_audits", _boom):
            body = self._client().get("/records").text
        self.assertIn("授权", body)       # 其余来源仍正常展示
        self.assertIn("定时运行", body)
        self.assertNotIn("读取失败", body)
        self.assertNotIn("Traceback", body)


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


class RecipientOverrideApiTests(unittest.TestCase):
    """任务级收件人覆盖写 API（方案 B，2026-10-12）。"""

    def _client(self):
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        return client

    def test_get_returns_overrides_and_rejects_bad_service(self):
        rows = [{
            "id": 1, "service_id": "robot-hangzhou", "mode": "include",
            "target_type": "user_id", "target_value": "u9",
            "resolved_user_id": "u9", "enabled": 1, "note": None,
            "updated_by": "ops", "updated_at": None,
        }]
        with mock.patch.object(recipient_override, "fetch_service_overrides",
                               lambda conn, sid: list(rows)):
            response = self._client().get(
                "/api/pipelines/recipient-override",
                params={"service_id": "robot-hangzhou"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(rows, response.json()["overrides"])
        self.assertEqual(400, self._client().get(
            "/api/pipelines/recipient-override").status_code)

    def test_get_serializes_datetime_updated_at(self):
        """线上实测：updated_at 为 datetime 时 JSONResponse 序列化失败 → 500，
        抽屉显示「读取异常，请稍后重试」。出口须归一为字符串（2026-10-12
        hotfix，方案 A P0）。"""
        rows = [{
            "id": 7, "service_id": "robot-hangzhou", "mode": "exclude",
            "target_type": "name", "target_value": "李四",
            "resolved_user_id": None, "enabled": 1, "note": None,
            "updated_by": "ops",
            "updated_at": datetime(2026, 10, 12, 8, 30, 0),
        }]
        with mock.patch.object(recipient_override, "fetch_service_overrides",
                               lambda conn, sid: list(rows)):
            response = self._client().get(
                "/api/pipelines/recipient-override",
                params={"service_id": "robot-hangzhou"})
        self.assertEqual(200, response.status_code)
        overrides = response.json()["overrides"]
        self.assertEqual(1, len(overrides))
        self.assertIsInstance(overrides[0]["updated_at"], str)
        self.assertEqual("李四", overrides[0]["target_value"])

    def test_get_includes_base_and_source_from_roster(self):
        """P1（2026-10-12）：抽屉 API 附带「本线基名单」——robot 线取该区域
        在职成员 ∩ 名册启用在册人员，来源标注人话（与列表列同源口径）。"""
        with mock.patch.object(recipient_override, "fetch_service_overrides",
                               lambda conn, sid: []), \
                mock.patch.object(bi_authz, "fetch_active_members",
                                  lambda conn: list(_MEMBERS)), \
                mock.patch.object(report_roster, "fetch_scope_person_names",
                                  lambda conn, scope=None:
                                  frozenset({"张三", "王五"})):
            response = self._client().get(
                "/api/pipelines/recipient-override",
                params={"service_id": "robot-hangzhou"})
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(
            [{"user_id": "u-zhang", "name": "张三"},
             {"user_id": "u-wang", "name": "王五"}], data["base"])
        self.assertEqual("名册（hangzhou）∩ 在职成员", data["base_source"])

    def test_get_returns_qudao_base(self):
        """渠道线基名单 = 名册 qudao·owner 去重人名；无 uid 映射时以 ?名 占位。"""
        owners = {"天猫-A店": {"张三"}, "京东-B店": {"李四", "张三"}}
        with mock.patch.object(recipient_override, "fetch_service_overrides",
                               lambda conn, sid: []), \
                mock.patch.object(report_roster, "fetch_store_owner_map",
                                  lambda conn, scope, roles=None: owners):
            response = self._client().get(
                "/api/pipelines/recipient-override",
                params={"service_id": "channel-missing-check"})
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(
            [{"user_id": "?张三", "name": "张三"},
             {"user_id": "?李四", "name": "李四"}], data["base"])
        self.assertEqual("店铺负责人（名册 qudao·owner）", data["base_source"])

    def test_get_base_fail_soft_keeps_overrides(self):
        """基名单是附属信息：装载故障只让抽屉少「来源」区块，不给整页 503。"""
        def _boom(conn, scope=None):
            raise RuntimeError("roster down")

        rows = [{"id": 1, "service_id": "robot-hangzhou", "mode": "include",
                 "target_type": "user_id", "target_value": "u9",
                 "resolved_user_id": "u9", "enabled": 1, "note": None,
                 "updated_by": "ops", "updated_at": None}]
        with mock.patch.object(recipient_override, "fetch_service_overrides",
                               lambda conn, sid: list(rows)), \
                mock.patch.object(bi_authz, "fetch_active_members",
                                  lambda conn: list(_MEMBERS)), \
                mock.patch.object(report_roster, "fetch_scope_person_names",
                                  _boom):
            response = self._client().get(
                "/api/pipelines/recipient-override",
                params={"service_id": "robot-hangzhou"})
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual([], data["base"])
        self.assertEqual("", data["base_source"])
        self.assertEqual([], data["explain"])
        self.assertEqual(rows, data["overrides"])

    def test_get_includes_explain_for_robot(self):
        """P2（2026-10-12）：抽屉附带「为什么不在名单」三组归因——名册有但
        不在职、在职但不在名册、被本任务 exclude 移除（展示口径，不新增写面）。"""
        rows = [{"id": 3, "service_id": "robot-hangzhou", "mode": "exclude",
                 "target_type": "user_id", "target_value": "u-zhang",
                 "resolved_user_id": "u-zhang", "enabled": 1, "note": None,
                 "updated_by": "ops", "updated_at": None}]
        with mock.patch.object(recipient_override, "fetch_service_overrides",
                               lambda conn, sid: list(rows)), \
                mock.patch.object(bi_authz, "fetch_active_members",
                                  lambda conn: list(_MEMBERS)), \
                mock.patch.object(report_roster, "fetch_scope_person_names",
                                  lambda conn, scope=None:
                                  frozenset({"张三", "赵六"})):
            response = self._client().get(
                "/api/pipelines/recipient-override",
                params={"service_id": "robot-hangzhou"})
        self.assertEqual(200, response.status_code)
        data = response.json()
        # 基名单 = 在职(hangzhou: 张三/王五) ∩ 名册(张三/赵六) = 张三
        self.assertEqual([{"user_id": "u-zhang", "name": "张三"}], data["base"])
        groups = {g["key"]: g for g in data["explain"]}
        self.assertEqual(["赵六"],
                         [i["name"] for i in groups["roster_only"]["items"]])
        self.assertEqual(["王五"],
                         [i["name"] for i in groups["member_only"]["items"]])
        self.assertEqual(["张三"],
                         [i["name"] for i in groups["excluded"]["items"]])
        self.assertIn("成员与授权", groups["roster_only"]["reason"])
        self.assertIn("填报名册", groups["member_only"]["reason"])

    def test_get_qudao_explain_has_no_member_groups(self):
        """渠道线基名单直接来自名册 owner、无区域在职交集：explain 不产出
        roster_only/member_only（否则会把全部 owner 误报为「不在职」），
        只随覆盖产出 excluded。"""
        owners = {"天猫-A店": {"张三"}, "京东-B店": {"李四"}}
        rows = [{"id": 5, "service_id": "channel-missing-check",
                 "mode": "exclude", "target_type": "name",
                 "target_value": "张三", "resolved_user_id": None,
                 "enabled": 1, "note": None, "updated_by": "ops",
                 "updated_at": None}]
        with mock.patch.object(recipient_override, "fetch_service_overrides",
                               lambda conn, sid: list(rows)), \
                mock.patch.object(report_roster, "fetch_store_owner_map",
                                  lambda conn, scope, roles=None: owners):
            response = self._client().get(
                "/api/pipelines/recipient-override",
                params={"service_id": "channel-missing-check"})
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(["excluded"], [g["key"] for g in data["explain"]])
        self.assertEqual(["张三"],
                         [i["name"] for i in data["explain"][0]["items"]])

    def test_add_resolves_snapshot_and_reports_unresolved(self):
        seen = {}

        def fake_upsert(conn, service_id, mode, target_type, target_value,
                        *, actor, note="", resolved_user_id=None):
            seen.update(service_id=service_id, mode=mode,
                        target_type=target_type, target_value=target_value,
                        actor=actor, note=note, resolved=resolved_user_id)

        with mock.patch.object(
                recipient_override, "resolve_targets",
                lambda conn, rows: (
                    [{"target_type": "name", "target_value": "张三",
                      "resolved_user_id": "u-zhang"}], ["李四"])), \
                mock.patch.object(recipient_override, "upsert_override",
                                  fake_upsert):
            response = self._client().post(
                "/api/pipelines/recipient-override/add",
                json={"service_id": "robot-hangzhou", "mode": "include",
                      "target_type": "name", "target_value": "张三",
                      "note": "补人"})

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual("u-zhang", data["resolved_user_id"])
        self.assertEqual(["李四"], data["unresolved"])
        self.assertEqual(ADMIN_USERID, seen["actor"])
        self.assertEqual("u-zhang", seen["resolved"])
        self.assertEqual("补人", seen["note"])

    def test_add_rejects_invalid_payload(self):
        client = self._client()
        for payload in (
            {"service_id": "robot-hangzhou", "mode": "nope",
             "target_type": "name", "target_value": "张三"},
            {"service_id": "robot-hangzhou", "mode": "include",
             "target_type": "alien", "target_value": "张三"},
            {"service_id": "robot-hangzhou", "mode": "include",
             "target_type": "name", "target_value": "  "},
            {"service_id": "bad id!", "mode": "include",
             "target_type": "name", "target_value": "张三"},
        ):
            self.assertEqual(400, client.post(
                "/api/pipelines/recipient-override/add",
                json=payload).status_code, payload)

    def test_toggle_delete_reset(self):
        client = self._client()
        with mock.patch.object(recipient_override, "set_override_enabled",
                               lambda conn, oid, enabled, *, actor: True):
            self.assertEqual(200, client.post(
                "/api/pipelines/recipient-override/toggle",
                json={"id": 3, "enabled": False}).status_code)
        with mock.patch.object(recipient_override, "set_override_enabled",
                               lambda conn, oid, enabled, *, actor: False):
            self.assertEqual(400, client.post(
                "/api/pipelines/recipient-override/toggle",
                json={"id": 3, "enabled": True}).status_code)
        with mock.patch.object(recipient_override, "delete_override",
                               lambda conn, oid, *, actor: True):
            self.assertEqual(200, client.post(
                "/api/pipelines/recipient-override/delete",
                json={"id": 4}).status_code)
        with mock.patch.object(recipient_override, "reset_service_overrides",
                               lambda conn, sid, *, actor: 5):
            response = client.post(
                "/api/pipelines/recipient-override/reset",
                json={"service_id": "robot-hangzhou"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(5, response.json()["deleted"])
        # 非法载荷 → 400（bool 不算 int；service_id 非法）
        self.assertEqual(400, client.post(
            "/api/pipelines/recipient-override/toggle",
            json={"id": True, "enabled": True}).status_code)
        self.assertEqual(400, client.post(
            "/api/pipelines/recipient-override/reset",
            json={"service_id": "bad id!"}).status_code)

    def test_add_db_error_returns_503(self):
        def boom(*args, **kwargs):
            raise RuntimeError("db down")

        with mock.patch.object(recipient_override, "resolve_targets", boom):
            response = self._client().post(
                "/api/pipelines/recipient-override/add",
                json={"service_id": "robot-hangzhou", "mode": "include",
                      "target_type": "user_id", "target_value": "u9"})
        self.assertEqual(503, response.status_code)


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
        self._rows = self._conn.route(sql)
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

    def route(self, sql):
        for keyword, rows in self.rows.items():
            if keyword in sql:
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


class OrgAssignmentApiTests(unittest.TestCase):
    """组织编排 SPA 后端（2026-10-13 P6）：/members 壳 + /api/org/*。

    覆盖：admin 闸、overview 结构（区域/部门/池）、覆盖读 fail-soft、
    成员读 503、assign/toggle/delete/reset 落库与审计、参数校验。
    """

    def _admin_client(self, connection):
        client = TestClient(_app(viewer=_admin_viewer(), connection=connection))
        _login(client)
        return client

    def _members_conn(self, rows=None, **kwargs):
        base = {"dim_robot_member": _MEMBERS}
        base.update(rows or {})
        return _OrgConn(base, **kwargs)

    # -- / 与 /members（SPA 壳）---------------------------------------------

    def test_root_serves_org_spa_shell(self):
        """B3（2026-10-13）：登录后首页 / 即「组织编排」SPA 壳（登录即见拖拽树）。"""
        import tempfile
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".html", delete=False, encoding="utf-8")
        handle.write("<!DOCTYPE html><div id=\"app\"></div>")
        handle.close()
        try:
            with mock.patch("common.ops_web.app._ORG_SPA_INDEX", handle.name):
                client = self._admin_client(_OrgConn({}))
                response = client.get("/")
            self.assertEqual(200, response.status_code)
            self.assertIn("text/html", response.headers["content-type"])
            self.assertIn("id=\"app\"", response.text)
        finally:
            import os
            os.unlink(handle.name)

    def test_members_spa_requires_session(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        response = client.get("/members", follow_redirects=False)
        self.assertEqual(302, response.status_code)
        self.assertIn("reason=login", response.headers["location"])

    def test_members_spa_forbidden_for_non_admin(self):
        viewer = authz.Viewer(
            userid="u1", name="张三", scopes=frozenset(),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        client = TestClient(_app(viewer=viewer))
        _login(client, "u1")
        response = client.get("/members", follow_redirects=False)
        self.assertEqual(302, response.status_code)
        self.assertIn("reason=forbidden", response.headers["location"])

    def test_members_spa_serves_shell_when_built(self):
        import tempfile
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".html", delete=False, encoding="utf-8")
        handle.write("<!DOCTYPE html><div id=\"app\"></div>")
        handle.close()
        try:
            with mock.patch("common.ops_web.app._ORG_SPA_INDEX", handle.name):
                client = self._admin_client(_OrgConn({}))
                response = client.get("/members")
            self.assertEqual(200, response.status_code)
            self.assertIn("text/html", response.headers["content-type"])
            self.assertIn("id=\"app\"", response.text)
        finally:
            import os
            os.unlink(handle.name)

    def test_members_spa_503_when_build_missing(self):
        with mock.patch("common.ops_web.app._ORG_SPA_INDEX",
                        "Z:/nonexistent/ops-web/dist/index.html"):
            client = self._admin_client(_OrgConn({}))
            response = client.get("/members")
        self.assertEqual(503, response.status_code)

    # -- /api/org/overview --------------------------------------------------

    def test_overview_gate(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        self.assertEqual(401, client.get("/api/org/overview").status_code)

    def test_overview_forbidden_for_non_admin(self):
        viewer = authz.Viewer(
            userid="u1", name="张三", scopes=frozenset(),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        client = TestClient(_app(viewer=viewer))
        _login(client, "u1")
        self.assertEqual(403, client.get("/api/org/overview").status_code)

    def test_overview_groups_base_members(self):
        client = self._admin_client(self._members_conn())
        data = client.get("/api/org/overview").json()

        self.assertEqual("ok", data["status"])
        regions = {region["key"]: region for region in data["regions"]}
        hangzhou = {
            member["name"]
            for group in regions["hangzhou"]["depts"]
            for member in group["members"]
        }
        self.assertEqual({"张三", "王五"}, hangzhou)
        hq = {
            member["name"]
            for group in regions["hq"]["depts"]
            for member in group["members"]
        }
        self.assertEqual({"李四"}, hq)
        self.assertEqual("杭州", regions["hangzhou"]["label"])
        self.assertEqual("总部", regions["hq"]["label"])
        self.assertEqual([], data["pool"])
        self.assertEqual([], data["overrides"])

    def test_overview_applies_override_move_region_and_dept(self):
        client = self._admin_client(self._members_conn(
            {"dim_org_assignment_override": [_ORG_OVERRIDE_ROW]}))
        data = client.get("/api/org/overview").json()

        regions = {region["key"]: region for region in data["regions"]}
        hangzhou = {
            member["user_id"]
            for group in regions["hangzhou"]["depts"]
            for member in group["members"]
        }
        self.assertEqual({"u-zhang"}, hangzhou)          # 王五已移出杭州
        hq_groups = {group["dept_name"]: group["members"]
                     for group in regions["hq"]["depts"]}
        self.assertEqual({"u-li", "u-wang"},              # 王五移入总部
                         {m["user_id"] for m in hq_groups["总经办"]})
        self.assertEqual(1, len(data["overrides"]))
        self.assertEqual("总部", data["overrides"][0]["region_label"])

    def test_overview_override_region_null_goes_to_pool(self):
        row = dict(_ORG_OVERRIDE_ROW, user_id="u-li", name="李四",
                   region=None, dept_name=None)
        client = self._admin_client(self._members_conn(
            {"dim_org_assignment_override": [row]}))
        data = client.get("/api/org/overview").json()

        self.assertEqual(["u-li"], [m["user_id"] for m in data["pool"]])
        self.assertNotIn("hq", {region["key"] for region in data["regions"]})

    def test_overview_override_read_fails_soft(self):
        conn = self._members_conn(fail={"dim_org_assignment_override"})
        client = self._admin_client(conn)
        response = client.get("/api/org/overview")

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual([], data["overrides"])           # 覆盖读失败 = 空
        self.assertIn("hangzhou",
                      {region["key"] for region in data["regions"]})

    def test_overview_members_read_503(self):
        conn = self._members_conn(fail={"dim_robot_member"})
        client = self._admin_client(conn)
        self.assertEqual(503, client.get("/api/org/overview").status_code)

    # -- 写面 ---------------------------------------------------------------

    def test_assign_requires_admin(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        self.assertEqual(
            401, client.post("/api/org/assign",
                             json={"user_id": "u-zhang"}).status_code)

    def test_assign_writes_override_and_audit(self):
        conn = self._members_conn()
        client = self._admin_client(conn)
        response = client.post("/api/org/assign", json={
            "user_id": "u-zhang", "region": "hq", "dept_id": "d9",
            "dept_name": "总经办", "name": "张三"})

        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok"}, response.json())
        sqls = [sql for sql, _ in conn.executed]
        self.assertTrue(any(
            "INSERT INTO `dim_org_assignment_override`" in sql
            and "ON DUPLICATE KEY UPDATE" in sql for sql in sqls))
        audit = next(p for sql, p in conn.executed
                     if "dim_org_assignment_override_audit" in sql)
        self.assertEqual(ADMIN_USERID, audit[0])
        self.assertEqual("assign", audit[1])
        self.assertEqual(1, conn.commits)                 # 审计同事务

    def test_assign_null_region_moves_out(self):
        conn = self._members_conn()
        client = self._admin_client(conn)
        response = client.post(
            "/api/org/assign", json={"user_id": "u-li", "region": None})
        self.assertEqual(200, response.status_code)
        audit = next(p for sql, p in conn.executed
                     if "dim_org_assignment_override_audit" in sql)
        self.assertEqual("移出全部区域", audit[7])

    def test_assign_rejects_missing_user_id(self):
        client = self._admin_client(self._members_conn())
        response = client.post("/api/org/assign", json={"region": "hq"})
        self.assertEqual(400, response.status_code)

    def test_assign_rejects_bad_region_type(self):
        client = self._admin_client(self._members_conn())
        response = client.post(
            "/api/org/assign", json={"user_id": "u-zhang", "region": 5})
        self.assertEqual(400, response.status_code)

    def test_assign_rejects_illegal_region_key(self):
        conn = self._members_conn()
        client = self._admin_client(conn)
        response = client.post(
            "/api/org/assign", json={"user_id": "u-zhang", "region": "Bad Key"})
        self.assertEqual(400, response.status_code)
        self.assertEqual(0, conn.commits)

    def test_toggle_hit_and_miss(self):
        rows = {"dim_org_assignment_override": [_ORG_OVERRIDE_ROW]}
        hit = self._admin_client(self._members_conn(rows, rowcount=1))
        self.assertEqual(
            200, hit.post("/api/org/toggle",
                          json={"user_id": "u-wang", "enabled": False}
                          ).status_code)
        miss = self._admin_client(self._members_conn(rows, rowcount=0))
        self.assertEqual(
            400, miss.post("/api/org/toggle",
                           json={"user_id": "u-wang", "enabled": False}
                           ).status_code)

    def test_toggle_rejects_bad_body(self):
        client = self._admin_client(self._members_conn())
        self.assertEqual(
            400, client.post("/api/org/toggle",
                             json={"user_id": "u-wang", "enabled": "yes"}
                             ).status_code)

    def test_delete_hit_and_miss(self):
        hit = self._admin_client(self._members_conn(
            {"dim_org_assignment_override": [_ORG_OVERRIDE_ROW]}))
        self.assertEqual(
            200, hit.post("/api/org/delete",
                          json={"user_id": "u-wang"}).status_code)
        miss = self._admin_client(self._members_conn())
        self.assertEqual(
            400, miss.post("/api/org/delete",
                           json={"user_id": "u-wang"}).status_code)

    def test_reset_clears_all(self):
        conn = self._members_conn(rowcount=3)
        client = self._admin_client(conn)
        response = client.post("/api/org/reset", json={})
        self.assertEqual(200, response.status_code)
        self.assertEqual(3, response.json()["deleted"])
        audit = next(p for sql, p in conn.executed
                     if "dim_org_assignment_override_audit" in sql)
        self.assertEqual("reset", audit[1])

    # -- 组织目录（新增区域 / 部门，2026-10-13 P7）-------------------------

    def test_overview_renders_catalog_region_and_dept_without_members(self):
        conn = self._members_conn({
            "dim_org_region": [
                {"region_key": "chengdu", "label": "成都", "sort_order": 0},
            ],
            "dim_org_dept": [
                {"region_key": "hangzhou", "dept_name": "杭州销售部",
                 "dept_id": None, "sort_order": 0},
                {"region_key": "chengdu", "dept_name": "成都销售部",
                 "dept_id": None, "sort_order": 0},
            ],
        })
        data = self._admin_client(conn).get("/api/org/overview").json()
        regions = {region["key"]: region for region in data["regions"]}

        # 目录登记的新区域（无成员）也出现，label 取目录值。
        self.assertIn("chengdu", regions)
        self.assertEqual("成都", regions["chengdu"]["label"])
        chengdu = {d["dept_name"]: d for d in regions["chengdu"]["depts"]}
        self.assertEqual([], chengdu["成都销售部"]["members"])
        # 目录登记的部门（无成员）并入既有区域。
        hangzhou = {d["dept_name"]: d for d in regions["hangzhou"]["depts"]}
        self.assertIn("杭州销售部", hangzhou)
        self.assertEqual([], hangzhou["杭州销售部"]["members"])

    def test_overview_catalog_read_fails_soft(self):
        conn = self._members_conn(fail={"dim_org_region"})
        response = self._admin_client(conn).get("/api/org/overview")
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertIn("hangzhou",
                      {region["key"] for region in data["regions"]})

    def test_region_add_requires_admin(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        self.assertEqual(
            401, client.post("/api/org/region/add",
                             json={"region_key": "chengdu", "label": "成都"}
                             ).status_code)

    def test_region_add_writes_catalog_and_audit(self):
        conn = self._members_conn()
        client = self._admin_client(conn)
        response = client.post("/api/org/region/add",
                               json={"region_key": "chengdu", "label": "成都"})
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok"}, response.json())
        sqls = [sql for sql, _ in conn.executed]
        self.assertTrue(any("INSERT INTO `dim_org_region`" in sql
                            and "ON DUPLICATE KEY UPDATE" in sql
                            for sql in sqls))
        audit = next(p for sql, p in conn.executed
                     if "dim_org_catalog_audit" in sql)
        self.assertEqual(ADMIN_USERID, audit[0])
        self.assertEqual("add_region", audit[1])
        self.assertEqual("chengdu", audit[3])
        self.assertEqual(1, conn.commits)

    def test_region_add_rejects_bad_body(self):
        conn = self._members_conn()
        client = self._admin_client(conn)
        self.assertEqual(
            400, client.post("/api/org/region/add",
                             json={"region_key": "Bad Key", "label": "成都"}
                             ).status_code)
        self.assertEqual(
            400, client.post("/api/org/region/add",
                             json={"region_key": "chengdu", "label": ""}
                             ).status_code)
        self.assertEqual(0, conn.commits)

    def test_dept_add_writes_catalog_and_audit(self):
        conn = self._members_conn()
        client = self._admin_client(conn)
        response = client.post(
            "/api/org/dept/add",
            json={"region_key": "hangzhou", "dept_name": "杭州销售部",
                  "dept_id": "1050062512"})
        self.assertEqual(200, response.status_code)
        sqls = [sql for sql, _ in conn.executed]
        self.assertTrue(any("INSERT INTO `dim_org_dept`" in sql
                            and "ON DUPLICATE KEY UPDATE" in sql
                            for sql in sqls))
        audit = next(p for sql, p in conn.executed
                     if "dim_org_catalog_audit" in sql)
        self.assertEqual("add_dept", audit[1])
        self.assertEqual("hangzhou", audit[3])
        self.assertEqual("1050062512", audit[4])
        self.assertEqual("杭州销售部", audit[5])

    def test_dept_add_rejects_missing_name(self):
        client = self._admin_client(self._members_conn())
        self.assertEqual(
            400, client.post("/api/org/dept/add",
                             json={"region_key": "hangzhou",
                                   "dept_name": "  "}).status_code)


if __name__ == "__main__":
    unittest.main()
