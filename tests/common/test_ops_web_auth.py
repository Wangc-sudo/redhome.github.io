"""test_ops_web_app 的 auth 域测试；断言自原文件等价迁移。"""

from tests.common.ops_web_test_support import *


class AuthDisabledTests(unittest.TestCase):
    """认证总开关·读写分离（2026-10-26 用户裁决，同日从「全直通」升级为
    「读开放、写要登录」——SG 全放行现实下写面不能裸奔）：
    auth_disabled=True 时读方法匿名直通，写方法仍走会话+授权闸。"""

    def _open_app(self, viewer=None):
        return create_app(
            settings=_fake_settings(),
            session_secret=SECRET,
            db_connector=_connector(),
            viewer_resolver=_StaticViewerResolver(viewer),
            auth_disabled=True,
        )

    def test_no_session_passes_api_without_login(self):
        client = TestClient(self._open_app())
        response = client.get("/api/me")
        self.assertEqual(200, response.status_code)
        self.assertEqual("anon-ops", response.json()["userid"])

    def test_write_without_session_is_401(self):
        # 读写分离核心：写面（POST /api/**）无会话不再直通——401。
        client = TestClient(self._open_app())
        response = client.post(
            "/api/org/region/broadcast",
            json={"region_key": "hangzhou", "fields": {}},
        )
        self.assertEqual(401, response.status_code)

    def test_write_with_admin_session_passes_gate(self):
        # 登录 admin 写：过闸到达参数校验（400 bad_request），不是 401/403。
        client = TestClient(self._open_app(viewer=_admin_viewer()))
        _login(client)
        response = client.post(
            "/api/org/region/broadcast",
            json={"region_key": "hangzhou", "fields": {}},
        )
        self.assertEqual(400, response.status_code)

    def test_write_with_plain_session_is_403(self):
        # 登录但无能力授权：写面 fail-closed 403（授权判定照常生效）。
        viewer = authz.Viewer(
            userid="u-plain", name="路人", scopes=frozenset(),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        client = TestClient(self._open_app(viewer=viewer))
        _login(client, "u-plain")
        response = client.post(
            "/api/org/region/broadcast",
            json={"region_key": "hangzhou", "fields": {}},
        )
        self.assertEqual(403, response.status_code)

    def test_login_endpoints_stay_anonymous(self):
        # 登录端点不调闸：未配 browser_login_token → 503 业务码
        #（若被写面闸拦会是 401）。
        client = TestClient(self._open_app())
        response = client.post(
            "/auth/browser-login", json={"user_id": "x", "token": "y"},
        )
        self.assertEqual(503, response.status_code)

    def test_default_keeps_gates(self):
        # 缺省（auth_disabled=False）行为不变：未登录 401。
        client = TestClient(_app())
        response = client.get("/api/me")
        self.assertEqual(401, response.status_code)


class AdminGateTests(unittest.TestCase):
    """应用层闸（铁律 8 的第二闸）：session + admin 缺一不可。"""

    def test_no_session_serves_spa_shell(self):
        # 2026-10-14 全 SPA 迁移：壳路由不设应用层闸（未登录由前端启动
        # /api/me → 401 跳登录）；数据 API 仍 fail-closed（见下用例）。
        client = TestClient(_app(viewer=_admin_viewer()))

        assert_spa_shell(client.get("/", follow_redirects=False))

        self.assertEqual(401, client.get("/api/me").status_code)

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

        # 壳路由放行（权限由前端 Router 守卫）；数据面仍 403 fail-closed。
        assert_spa_shell(client.get("/", follow_redirects=False))
        self.assertEqual(
            403, client.post("/api/grants", json={"user_id": "u"}).status_code
        )

    def test_resolver_failure_is_fail_closed(self):
        # 铁律 2：Viewer 解析异常在数据面 = 403，绝不放行。
        client = TestClient(_app(resolver_error=RuntimeError("mart down")))
        _login(client)

        # 壳路由不解析 Viewer，照常返回壳；解析异常只影响数据面。
        assert_spa_shell(client.get("/", follow_redirects=False))
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


class BrowserLoginTests(unittest.TestCase):
    """浏览器登录（2026-10-14）：运维口令 + 账号 → 同一枚 shared session。

    与钉钉免登的区别只在「怎么证明身份」；签发出来的会话与准入门完全同款
    （签发 ≠ 有权限，权限仍由 ViewerResolver 对 bi_authz 逐请求判定）。
    """

    TOKEN = "ops-browser-secret"

    def _app_with_channel(self, token=TOKEN, viewer=None):
        return create_app(
            settings=_fake_settings(), session_secret=SECRET,
            db_connector=_connector(),
            viewer_resolver=_StaticViewerResolver(
                viewer() if callable(viewer) else (viewer or _admin_viewer())
            ),
            browser_login_token=token,
        )

    def test_channel_closed_without_token(self):
        client = TestClient(_app(viewer=_admin_viewer()))

        self.assertEqual(
            503,
            client.post(
                "/auth/browser-login",
                json={"user_id": ADMIN_USERID, "token": "anything"},
            ).status_code,
        )
        self.assertFalse(client.get("/auth/login-options").json()["browser"])

    def test_login_options_reports_the_channel(self):
        client = TestClient(self._app_with_channel())

        self.assertTrue(client.get("/auth/login-options").json()["browser"])

    def test_issues_the_shared_session_cookie(self):
        client = TestClient(self._app_with_channel())

        response = client.post(
            "/auth/browser-login",
            json={"user_id": ADMIN_USERID, "token": self.TOKEN},
        )

        self.assertEqual(200, response.status_code)
        cookie = response.cookies.get(auth.SESSION_COOKIE)
        self.assertEqual(ADMIN_USERID, auth.resolve_session_userid(cookie, SECRET))
        self.assertEqual(200, client.get("/api/me").status_code)

    def test_rejects_bad_token_or_malformed_payload(self):
        client = TestClient(self._app_with_channel())

        self.assertEqual(
            401,
            client.post(
                "/auth/browser-login",
                json={"user_id": ADMIN_USERID, "token": "wrong"},
            ).status_code,
        )
        self.assertEqual(
            400,
            client.post(
                "/auth/browser-login",
                json={"user_id": "  ", "token": self.TOKEN},
            ).status_code,
        )
        self.assertEqual(
            400,
            client.post(
                "/auth/browser-login", json={"user_id": ADMIN_USERID}
            ).status_code,
        )

    def test_session_grants_no_permission_of_its_own(self):
        # 准入门仍以 bi_authz 为准：拿到会话但未被授权 → 403（fail-closed）。
        stranger = authz.Viewer(
            userid="u-nobody", name="查无此人", scopes=frozenset(),
            allowed_regions=frozenset(), is_admin=False, admitted=False,
        )
        client = TestClient(self._app_with_channel(viewer=stranger))

        response = client.post(
            "/auth/browser-login",
            json={"user_id": "u-nobody", "token": self.TOKEN},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(403, client.get("/api/recipients").status_code)


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
