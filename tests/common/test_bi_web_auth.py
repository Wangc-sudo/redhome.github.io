"""钉钉免登（M1）与授权骨架（M2a）的离线单测。

覆盖铁律 1/2 的独立断言：

* deny-by-default：无 grant 记录的已认证用户只见提示页 / 403；
* fail-closed：grant 表读失败、Viewer 解析异常、required_scope 声明损坏
  ——一律未授权提示页 / 403，绝不放大可见面；
* Bearer 兜底通道行为与 T0 基准一致（机器通道不进准入门）。

全部 TestClient + 注入 fake：不联网、不碰凭据、不依赖真实库。
"""

import unittest
import warnings
from contextlib import contextmanager
from dataclasses import replace

warnings.filterwarnings(
    "ignore", message="Using `httpx` with `starlette.testclient` is deprecated"
)

from fastapi.testclient import TestClient

from common.bi_web import auth, authz
from common.bi_web import queries as BI_WEB_QUERIES
from common.bi_web.app import create_app
from common.bi_web.cache import card_cache_key
from common.bi_web.config import StaticDashboardSource

from tests.common.test_bi_web_app import (
    _FakeRegistry,
    _fake_db_connector,
    _fake_settings,
    _l1_mapping,
)

SECRET = "test-session-secret"
ADMIN = authz.Viewer(
    userid="u-admin", name="王城", scopes=frozenset({"fin"}),
    allowed_regions=frozenset(), is_admin=True, admitted=True,
)
DENIED = authz.denied_viewer("u-nobody")


def _viewer(userid="u1", scopes=(), regions=(), admitted=True):
    return authz.Viewer(
        userid=userid, name="测试", scopes=frozenset(scopes),
        allowed_regions=frozenset(regions), is_admin=False, admitted=admitted,
    )


class _StaticViewerResolver:
    """按 userid 查表回答 Viewer 的 fake。"""

    def __init__(self, viewers=None, error=None):
        self._viewers = viewers or {}
        self._error = error
        self.calls = []

    def resolve(self, userid):
        self.calls.append(userid)
        if self._error is not None:
            raise self._error
        return self._viewers.get(userid, authz.denied_viewer(userid))


class _FakeAuthClient:
    def __init__(self, userid="u1", error=None):
        self._userid = userid
        self._error = error
        self.codes = []

    def exchange_auth_code(self, auth_code):
        self.codes.append(auth_code)
        if self._error is not None:
            raise self._error
        return self._userid


def _session_app(*, viewer_resolver=None, auth_client=None, token=None,
                 dashboard_source=None):
    return create_app(
        settings=_fake_settings(),
        dashboard_source=(
            StaticDashboardSource({"l1-cockpit": _l1_mapping()})
            if dashboard_source is None else dashboard_source
        ),
        registry=_FakeRegistry(),
        token=token,
        gate=lambda: True,
        db_connector=_fake_db_connector(),
        session_secret=SECRET,
        auth_client=auth_client,
        viewer_resolver=viewer_resolver,
    )


def _login(client, userid="u1"):
    token = auth.issue_session(userid, SECRET)
    client.cookies.set(auth.SESSION_COOKIE, token)
    return token


# ---------------------------------------------------------------------------
# HMAC session 纯函数
# ---------------------------------------------------------------------------

class SessionPureFunctionTests(unittest.TestCase):
    def test_issue_verify_roundtrip(self):
        token = auth.issue_session("u1", SECRET, now=1000.0)

        userid, exp = auth.verify_session(token, SECRET, now=1000.0)

        self.assertEqual("u1", userid)
        self.assertEqual(1000 + auth.SESSION_TTL_SECONDS, exp)

    def test_expired_session_has_its_own_branch(self):
        token = auth.issue_session("u1", SECRET, now=1000.0)

        with self.assertRaises(auth.SessionExpiredError):
            auth.verify_session(token, SECRET, now=1000.0 + auth.SESSION_TTL_SECONDS + 1)

    def test_tampered_payload_has_its_own_branch(self):
        token = auth.issue_session("u1", SECRET, now=1000.0)
        _payload, sig = token.split(".")
        forged = auth._b64encode(b"u2|9999999999|deadbeef")

        with self.assertRaises(auth.SessionTamperedError):
            auth.verify_session(f"{forged}.{sig}", SECRET, now=1000.0)

    def test_wrong_secret_is_tampered(self):
        token = auth.issue_session("u1", SECRET, now=1000.0)
        with self.assertRaises(auth.SessionTamperedError):
            auth.verify_session(token, "other-secret", now=1000.0)

    def test_format_errors_have_their_own_branch(self):
        for bad in ("", "no-dot", "a.b.c", ".sig", "payload.", None, 123):
            with self.assertRaises(auth.SessionFormatError, msg=repr(bad)):
                auth.verify_session(bad, SECRET, now=1000.0)

    def test_malformed_payload_fields_are_format_errors(self):
        sig_payload = auth._b64encode(b"only-one-field")
        sig = auth._signature(sig_payload, SECRET)
        with self.assertRaises(auth.SessionFormatError):
            auth.verify_session(f"{sig_payload}.{sig}", SECRET, now=1000.0)

        bad_exp = auth._b64encode(b"u1|not-a-number|nonce")
        sig = auth._signature(bad_exp, SECRET)
        with self.assertRaises(auth.SessionFormatError):
            auth.verify_session(f"{bad_exp}.{sig}", SECRET, now=1000.0)

    def test_slide_renews_only_in_the_second_half_of_the_ttl(self):
        token = auth.issue_session("u1", SECRET, now=1000.0)

        # 前半程：不续期。
        self.assertIsNone(auth.slide_session(token, SECRET, now=1000.0 + 100))
        # 后半程：续期且新旧 token 都校验同一 userid。
        renewed = auth.slide_session(
            token, SECRET, now=1000.0 + auth.SESSION_TTL_SECONDS * 0.75
        )
        self.assertIsNotNone(renewed)
        userid, _exp = auth.verify_session(
            renewed, SECRET, now=1000.0 + auth.SESSION_TTL_SECONDS * 0.75
        )
        self.assertEqual("u1", userid)

    def test_resolve_session_userid_swallows_every_failure_branch(self):
        token = auth.issue_session("u1", SECRET, now=1000.0)
        self.assertEqual("u1", auth.resolve_session_userid(token, SECRET, now=1000.0))
        self.assertIsNone(auth.resolve_session_userid("garbage", SECRET))
        self.assertIsNone(auth.resolve_session_userid(token, "wrong"))
        self.assertIsNone(
            auth.resolve_session_userid(
                token, SECRET, now=1000.0 + auth.SESSION_TTL_SECONDS + 1
            )
        )


# ---------------------------------------------------------------------------
# 钉钉交换客户端（注入 fake，不联网）
# ---------------------------------------------------------------------------

class DingTalkAuthClientTests(unittest.TestCase):
    def _client(self, responses):
        calls = []

        def request_json(url, method="GET", body=None, timeout=None):
            calls.append((url, body, timeout))
            outcome = responses[len(calls) - 1] if len(calls) <= len(responses) else responses[-1]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return auth.DingTalkAuthClient("key", "secret", request_json=request_json), calls

    def test_exchange_happy_path_returns_userid(self):
        client, calls = self._client(
            [{"errcode": 0, "result": {"userid": "014341566058939427"}}]
        )

        self.assertEqual(
            "014341566058939427", client.exchange_auth_code("auth-code-1")
        )
        url, body, timeout = calls[0]
        self.assertIn("access_token=test-access-token", url)
        self.assertEqual({"code": "auth-code-1"}, body)
        self.assertEqual(3, timeout)

    def test_http_failure_is_retried_once_then_generalized(self):
        client, calls = self._client([RuntimeError("boom"), RuntimeError("boom")])

        with self.assertRaises(auth.AuthError) as caught:
            client.exchange_auth_code("c")
        self.assertEqual(2, len(calls))
        self.assertNotIn("boom", str(caught.exception))

    def test_one_failure_then_success_uses_the_retry(self):
        client, calls = self._client(
            [RuntimeError("boom"), {"errcode": 0, "result": {"userid": "u9"}}]
        )

        self.assertEqual("u9", client.exchange_auth_code("c"))
        self.assertEqual(2, len(calls))

    def test_nonzero_errcode_is_generalized(self):
        client, _calls = self._client([{"errcode": 40078, "errmsg": "bad code"}])
        with self.assertRaises(auth.AuthError) as caught:
            client.exchange_auth_code("c")
        self.assertNotIn("40078", str(caught.exception))

    def test_missing_userid_is_generalized(self):
        client, _calls = self._client([{"errcode": 0, "result": {"name": "x"}}])
        with self.assertRaises(auth.AuthError):
            client.exchange_auth_code("c")

    def test_mask_userid_keeps_only_the_edges(self):
        self.assertEqual("01**************27", auth.mask_userid("014341566058939427"))
        self.assertEqual("****", auth.mask_userid("ab"))


# ---------------------------------------------------------------------------
# Viewer 解析（纯函数 + 解析器）
# ---------------------------------------------------------------------------

class BuildViewerTests(unittest.TestCase):
    def test_deny_by_default_no_grants_no_admission(self):
        viewer = authz.build_viewer("u1", [], ("测试", True))

        self.assertFalse(viewer.admitted)
        self.assertEqual(frozenset(), viewer.scopes)

    def test_admin_grant_admits_without_member_record(self):
        # Q3 自举语义：admin 不以 dim_robot_member 在册为前提。
        viewer = authz.build_viewer("u-admin", [("admin", "-")], None)

        self.assertTrue(viewer.admitted)
        self.assertTrue(viewer.is_admin)
        self.assertIn("fin", viewer.scopes)

    def test_inactive_member_is_denied_even_with_grants(self):
        viewer = authz.build_viewer(
            "u1", [("scope", "fin")], ("测试", False)
        )
        self.assertFalse(viewer.admitted)

    def test_member_not_on_record_is_denied(self):
        viewer = authz.build_viewer("u1", [("scope", "fin")], None)
        self.assertFalse(viewer.admitted)

    def test_active_member_gets_scopes_and_regions(self):
        viewer = authz.build_viewer(
            "u1",
            [("scope", "fin"), ("region", "hangzhou"), ("region", "hq")],
            ("测试", True),
        )

        self.assertTrue(viewer.admitted)
        self.assertEqual(frozenset({"fin"}), viewer.scopes)
        self.assertEqual(
            frozenset({"hangzhou", "hq"}), viewer.allowed_regions
        )

    def test_corrupt_rows_are_fail_closed(self):
        for bad_rows in (
            None,
            "not-rows",
            [("scope",)],  # 形态损坏的行
            [("superuser", "x"), ("scope", "")],  # 非法类型/空 key 被过滤后为空
        ):
            viewer = authz.build_viewer("u1", bad_rows, ("测试", True))
            self.assertFalse(viewer.admitted, msg=repr(bad_rows))

    def test_corrupt_userid_is_denied(self):
        self.assertFalse(authz.build_viewer("", [("admin", "-")], None).admitted)
        self.assertFalse(authz.build_viewer(None, [("admin", "-")], None).admitted)


class _GrantConnection:
    """按 SQL 关键字服务 grant / member 行的 connection fake。"""

    def __init__(self, grant_rows=(), member_row=None):
        self._grant_rows = grant_rows
        self._member_row = member_row
        self.queries = 0

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.queries += 1
        self._last = sql

    def fetchall(self):
        return list(self._grant_rows) if "bi_authz_grant`" in self._last else []

    def fetchone(self):
        return self._member_row if "dim_robot_member" in self._last else None

    def close(self):
        pass


class ViewerResolverTests(unittest.TestCase):
    def _connector(self, connection):
        calls = []

        @contextmanager
        def connector():
            calls.append(1)
            yield connection

        connector.calls = calls
        return connector

    def test_resolve_caches_within_the_ttl(self):
        connection = _GrantConnection(
            grant_rows=[("scope", "fin")], member_row=("测试", True)
        )
        connector = self._connector(connection)
        resolver = authz.ViewerResolver(connector, ttl_seconds=60.0)

        first = resolver.resolve("u1")
        second = resolver.resolve("u1")

        self.assertTrue(first.admitted)
        self.assertIs(first, second)
        self.assertEqual(1, len(connector.calls))

    def test_storage_failure_is_fail_closed_and_never_cached(self):
        @contextmanager
        def failing():
            raise RuntimeError("mart down")
            yield

        resolver = authz.ViewerResolver(failing)

        viewer = resolver.resolve("u1")

        self.assertFalse(viewer.admitted)
        self.assertEqual(1, resolver.failures)

    def test_denied_results_are_not_cached(self):
        connection = _GrantConnection(grant_rows=[], member_row=None)
        connector = self._connector(connection)
        resolver = authz.ViewerResolver(connector, ttl_seconds=60.0)

        resolver.resolve("u1")
        resolver.resolve("u1")

        self.assertEqual(2, len(connector.calls))


class ScopeAllowsTests(unittest.TestCase):
    def test_no_declaration_means_any_admitted_viewer(self):
        self.assertTrue(authz.scope_allows(_viewer(), None))

    def test_matching_scope_allows(self):
        self.assertTrue(authz.scope_allows(_viewer(scopes={"fin"}), "fin"))

    def test_mismatching_scope_denies(self):
        self.assertFalse(authz.scope_allows(_viewer(scopes={"ecom"}), "fin"))

    def test_admin_always_passes(self):
        self.assertTrue(authz.scope_allows(ADMIN, "fin"))
        self.assertTrue(authz.scope_allows(ADMIN, "anything"))

    def test_not_admitted_never_passes(self):
        self.assertFalse(authz.scope_allows(DENIED, None))

    def test_corrupt_declaration_is_fail_closed_for_non_admins(self):
        self.assertFalse(authz.scope_allows(_viewer(scopes={"fin"}), 123))
        self.assertFalse(authz.scope_allows(_viewer(scopes={"fin"}), ""))
        # admin 不受声明损坏影响（fail-closed 的出口是「仅 admin 可见」）。
        self.assertTrue(authz.scope_allows(ADMIN, 123))


# ---------------------------------------------------------------------------
# 应用装配：免登路由 + 身份闸 + 准入门 + 页面级
# ---------------------------------------------------------------------------

class AuthRouteTests(unittest.TestCase):
    def test_exchange_endpoint_disabled_without_client(self):
        client = TestClient(_session_app(auth_client=None))

        response = client.post("/auth/dingtalk", json={"authCode": "c"})

        self.assertEqual(503, response.status_code)
        self.assertEqual({"detail": "unavailable"}, response.json())

    def test_exchange_rejects_a_missing_auth_code(self):
        client = TestClient(_session_app(auth_client=_FakeAuthClient()))

        self.assertEqual(400, client.post("/auth/dingtalk", json={}).status_code)
        self.assertEqual(
            400, client.post("/auth/dingtalk", json={"authCode": ""}).status_code
        )
        self.assertEqual(
            400, client.post("/auth/dingtalk", content=b"not-json").status_code
        )

    def test_exchange_issues_the_session_cookie(self):
        client = TestClient(_session_app(auth_client=_FakeAuthClient("u1")))

        response = client.post("/auth/dingtalk", json={"authCode": "c1"})

        self.assertEqual(200, response.status_code)
        cookie = response.cookies.get(auth.SESSION_COOKIE)
        self.assertIsNotNone(cookie)
        self.assertEqual("u1", auth.resolve_session_userid(cookie, SECRET))

    def test_exchange_failure_is_a_generalized_503(self):
        client = TestClient(
            _session_app(auth_client=_FakeAuthClient(error=auth.AuthError("x")))
        )

        response = client.post("/auth/dingtalk", json={"authCode": "c1"})

        self.assertEqual(503, response.status_code)
        self.assertNotIn("x", response.text)

    def test_entry_page_is_public_and_data_free(self):
        client = TestClient(_session_app())

        response = client.get("/auth/entry?reason=unauthorized")

        self.assertEqual(200, response.status_code)
        self.assertIn("text/html", response.headers["content-type"])

    def test_logout_clears_the_cookie_and_redirects(self):
        client = TestClient(_session_app())

        response = client.get("/auth/logout", follow_redirects=False)

        self.assertEqual(307, response.status_code)
        self.assertIn("/auth/entry", response.headers["location"])


class IdentityGateTests(unittest.TestCase):
    """session 通道启用后的身份闸三态：未登录 / 已登录已授权 / Bearer 兜底。"""

    def test_no_credentials_page_redirects_to_entry(self):
        client = TestClient(_session_app())

        response = client.get("/d/l1-cockpit", follow_redirects=False)

        self.assertEqual(302, response.status_code)
        # M4 免登：401 跳转携带回跳地址（提示页 JS 校验单斜杠后 location.href）
        self.assertEqual(
            "/auth/entry?reason=login&next=%2Fd%2Fl1-cockpit",
            response.headers["location"],
        )

    def test_no_credentials_api_answers_401_json(self):
        client = TestClient(_session_app())

        response = client.get("/api/v1/dashboards")

        self.assertEqual(401, response.status_code)
        self.assertEqual({"detail": "unauthorized"}, response.json())

    def test_valid_session_with_admitted_viewer_passes(self):
        client = TestClient(
            _session_app(viewer_resolver=_StaticViewerResolver({"u1": _viewer()}))
        )
        _login(client, "u1")

        self.assertEqual(200, client.get("/d/l1-cockpit").status_code)
        self.assertEqual(200, client.get("/api/v1/dashboards").status_code)

    def test_deny_by_default_session_without_grants_sees_the_hint_page(self):
        # 铁律 1：认证通过但 grant 表无记录——页面 302 提示页，API 403。
        client = TestClient(
            _session_app(viewer_resolver=_StaticViewerResolver())
        )
        _login(client, "u-nobody")

        page = client.get("/d/l1-cockpit", follow_redirects=False)
        self.assertEqual(302, page.status_code)
        self.assertEqual(
            "/auth/entry?reason=unauthorized", page.headers["location"]
        )
        api = client.get("/api/v1/dashboards")
        self.assertEqual(403, api.status_code)
        self.assertEqual({"detail": "forbidden"}, api.json())

    def test_fail_closed_when_the_viewer_store_is_unreadable(self):
        # 铁律 2：grant 表读失败——fail-closed，绝不退化为全员可见。
        # （ViewerResolver 自身吞异常回 denied；这里钉的是闸对 denied 的映射。）
        resolver = _StaticViewerResolver()
        client = TestClient(_session_app(viewer_resolver=resolver))
        _login(client, "u1")

        self.assertEqual(403, client.get("/api/v1/dashboards").status_code)

    def test_bearer_fallback_bypasses_the_admission_gate(self):
        # 机器通道（大屏/审计）与 T0 基准一致：不进准入门、不解析 Viewer。
        resolver = _StaticViewerResolver()
        client = TestClient(
            _session_app(viewer_resolver=resolver, token="t")
        )

        response = client.get("/d/l1-cockpit", headers={"Authorization": "Bearer t"})

        self.assertEqual(200, response.status_code)
        self.assertEqual([], resolver.calls)

    def test_wrong_bearer_with_session_enabled_redirects_pages(self):
        client = TestClient(_session_app(token="t"))

        response = client.get("/d/l1-cockpit", headers={"Authorization": "Bearer x"},
                              follow_redirects=False)

        self.assertEqual(302, response.status_code)

    def test_garbage_session_cookie_falls_back_to_denied(self):
        client = TestClient(_session_app())
        client.cookies.set(auth.SESSION_COOKIE, "garbage")

        response = client.get("/api/v1/dashboards")

        self.assertEqual(401, response.status_code)


class PageLevelScopeTests(unittest.TestCase):
    """required_scope：导航过滤 + 页面 403/提示页 + 旧格式零影响。"""

    def _source(self, **extra):
        mapping = _l1_mapping()
        mapping.update(extra)
        return StaticDashboardSource({"l1-cockpit": mapping})

    def _client_with(self, viewer, **extra):
        resolver = _StaticViewerResolver({"u1": viewer})
        client = TestClient(
            _session_app(
                viewer_resolver=resolver,
                dashboard_source=self._source(**extra),
            )
        )
        _login(client, "u1")
        return client

    def test_legacy_dashboard_without_the_field_is_unaffected(self):
        client = self._client_with(_viewer())

        self.assertEqual(200, client.get("/d/l1-cockpit").status_code)
        self.assertEqual(
            200, client.get("/api/v1/dashboards/l1-cockpit").status_code
        )

    def test_matching_scope_sees_the_page(self):
        client = self._client_with(_viewer(scopes={"fin"}), required_scope="fin")

        self.assertEqual(200, client.get("/d/l1-cockpit").status_code)
        self.assertEqual(
            200, client.get("/api/v1/dashboards/l1-cockpit").status_code
        )

    def test_mismatching_scope_gets_hint_page_and_403(self):
        client = self._client_with(_viewer(scopes={"ecom"}), required_scope="fin")

        page = client.get("/d/l1-cockpit", follow_redirects=False)
        self.assertEqual(307, page.status_code)
        self.assertEqual(
            "/auth/entry?reason=forbidden", page.headers["location"]
        )
        self.assertEqual(
            403, client.get("/api/v1/dashboards/l1-cockpit").status_code
        )
        # 数据面同步关闭：卡片 API 同样 403。
        self.assertEqual(
            403,
            client.get("/api/v1/d/l1-cockpit/cards/kpi_offline_mtd").status_code,
        )

    def test_admin_sees_scoped_pages_without_the_scope(self):
        resolver = _StaticViewerResolver({"u-admin": ADMIN})
        client = TestClient(
            _session_app(
                viewer_resolver=resolver,
                dashboard_source=self._source(required_scope="fin"),
            )
        )
        _login(client, "u-admin")

        self.assertEqual(200, client.get("/d/l1-cockpit").status_code)

    def test_navigation_hides_scoped_pages_from_mismatched_viewers(self):
        mapping = _l1_mapping()
        mapping["required_scope"] = "fin"
        source = StaticDashboardSource({
            "l1-cockpit": mapping,
            "open-page": _l1_mapping(),
        })
        resolver = _StaticViewerResolver({"u1": _viewer(scopes={"ecom"})})
        client = TestClient(
            _session_app(viewer_resolver=resolver, dashboard_source=source)
        )
        _login(client, "u1")

        payload = client.get("/api/v1/dashboards").json()

        ids = [entry["id"] for entry in payload["dashboards"]]
        self.assertEqual(["open-page"], ids)


class PublicDashboardTests(unittest.TestCase):
    """公开看板免登录（2026-10-26 用户裁决）：杭州/绍兴/线下整体的公网
    榜单页面向群内散客直开。页面壳/定义/卡片/日期筛选放行；其余看板
    与导航列表仍走会话+准入门。"""

    def _client(self):
        source = StaticDashboardSource({
            "hz-offline-sales": _l1_mapping(),
            "l1-cockpit": _l1_mapping(),
        })
        return TestClient(_session_app(dashboard_source=source))

    def test_public_page_definition_and_card_open(self):
        client = self._client()
        # 页面壳 200（不 302 跳登录）
        page = client.get("/d/hz-offline-sales", follow_redirects=False)
        self.assertEqual(200, page.status_code)
        # 看板定义与日期筛选值域放行
        self.assertEqual(
            200, client.get("/api/v1/dashboards/hz-offline-sales").status_code)
        self.assertEqual(200, client.get("/api/v1/options/dates").status_code)

    def test_non_public_dashboard_still_gated(self):
        client = self._client()
        page = client.get("/d/l1-cockpit", follow_redirects=False)
        self.assertEqual(302, page.status_code)
        api = client.get("/api/v1/dashboards/l1-cockpit")
        self.assertEqual(401, api.status_code)
        # 导航列表不放行（公网页无顶栏，设计内）
        self.assertEqual(401, client.get("/api/v1/dashboards").status_code)

    def test_public_prefix_never_matches_unknown_ids(self):
        client = self._client()
        # 未知看板 id 不在公开集合 → 不放行：未登录先被认证拦（302），
        # 公开前缀不构成旁路。
        response = client.get("/d/not-a-dashboard", follow_redirects=False)
        self.assertEqual(302, response.status_code)

    def test_db_granted_dashboard_opens_without_login(self):
        """DB 增量通道（2026-10-26 权威源迁 DB）：bi_authz_grant 里
        grant_type='public_dashboard' 的看板免登录。"""
        from common.bi_web.app import _public_dashboard_resolver

        class _GrantCursor:
            def execute(self, sql, params=None):
                pass

            def fetchall(self):
                return [{"grant_key": "ops-health"}]

            def close(self):
                pass

        class _GrantConn:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def cursor(self):
                return _GrantCursor()

        resolver = _public_dashboard_resolver(lambda: _GrantConn())
        ids = resolver()
        # 常量保底 ∪ DB 增量
        self.assertIn("hz-offline-sales", ids)
        self.assertIn("ops-health", ids)

    def test_resolver_db_failure_falls_back_to_baseline(self):
        from common.bi_web.app import _public_dashboard_resolver

        class _Broken:
            def __enter__(self):
                raise RuntimeError("db down")

            def __exit__(self, *args):
                return False

        resolver = _public_dashboard_resolver(lambda: _Broken())
        self.assertEqual({"hz-offline-sales", "sx-offline-sales",
                          "offline-all-sales"}, set(resolver()))


class LegacyModeTests(unittest.TestCase):
    """session 未启用 = 旧行为逐字保留（T0 基准自证的补充）。"""

    def test_open_mode_stays_open(self):
        client = TestClient(create_app(
            settings=_fake_settings(),
            dashboard_source=StaticDashboardSource({"l1-cockpit": _l1_mapping()}),
            registry=_FakeRegistry(),
            gate=lambda: True,
            db_connector=_fake_db_connector(),
        ))

        self.assertEqual(200, client.get("/d/l1-cockpit").status_code)

    def test_bearer_only_mode_pages_stay_401_not_redirects(self):
        client = TestClient(create_app(
            settings=_fake_settings(),
            dashboard_source=StaticDashboardSource({"l1-cockpit": _l1_mapping()}),
            registry=_FakeRegistry(),
            token="t",
            gate=lambda: True,
            db_connector=_fake_db_connector(),
        ))

        response = client.get("/d/l1-cockpit", follow_redirects=False)

        self.assertEqual(401, response.status_code)


# ---------------------------------------------------------------------------
# 行级 scope（M3，设计稿 §4.1/§6）：allowed_regions 注入卡片的 params
#
# 分界很清楚：``row_scope="region"`` 的卡片必须收到 viewer 的区域集合
# （空集也发，fail-closed 在查询层兜底）；豁免卡与两种「全量身份」
# （admin / Bearer 机读通道）一律**不写保留键**，行为与 M3 前逐字节一致。
# ---------------------------------------------------------------------------


class RegionRowScopeTests(unittest.TestCase):
    """``authz.region_row_scope`` 的三条出口（纯函数）。"""

    def test_no_viewer_keeps_the_legacy_full_view(self):
        # Bearer 机读通道 / 未配置 session：'None' 即"不启用行级过滤"。
        self.assertIsNone(authz.region_row_scope(None))

    def test_admin_is_always_full_view(self):
        self.assertIsNone(authz.region_row_scope(ADMIN))

    def test_admitted_non_admin_gets_its_exact_grant_set(self):
        self.assertEqual(
            frozenset({"hangzhou", "hq"}),
            authz.region_row_scope(_viewer(regions={"hangzhou", "hq"})),
        )

    def test_empty_grant_set_stays_the_empty_set(self):
        # 铁律 2：显式授予、无自动默认——空集是空集，绝不等价于 None(全量)。
        self.assertEqual(frozenset(), authz.region_row_scope(_viewer()))


class RegionScopeInjectionTests(unittest.TestCase):
    """app 层：保留键只对 ``row_scope="region"`` 的卡片写入。"""

    def _app(self, viewer):
        registry = _FakeRegistry()
        registry["kpi_offline_mtd"] = replace(
            registry["kpi_offline_mtd"], row_scope="region"
        )
        client = TestClient(create_app(
            settings=_fake_settings(),
            dashboard_source=StaticDashboardSource({"l1-cockpit": _l1_mapping()}),
            registry=registry,
            token="machine-token",
            gate=lambda: True,
            db_connector=_fake_db_connector(),
            session_secret=SECRET,
            viewer_resolver=_StaticViewerResolver({"u1": viewer}),
        ))
        return client, registry

    def _last_params(self, registry):
        _card_id, _connection, params = registry.run_calls[-1]
        return params

    def test_scoped_viewer_gets_an_allow_list_fingerprint(self):
        client, registry = self._app(_viewer(regions={"hangzhou"}))
        _login(client, "u1")

        response = client.get("/api/v1/d/l1-cockpit/cards/kpi_offline_mtd")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "hangzhou", self._last_params(registry)[BI_WEB_QUERIES.ROW_SCOPE_PARAM]
        )

    def test_empty_grant_set_injects_the_empty_fingerprint(self):
        client, registry = self._app(_viewer())
        _login(client, "u1")

        self.assertEqual(
            200,
            client.get("/api/v1/d/l1-cockpit/cards/kpi_offline_mtd").status_code,
        )
        # 空串（而非缺键）：查询层据此拼 AND 1 = 0，而不是退回全量。
        self.assertEqual(
            "", self._last_params(registry)[BI_WEB_QUERIES.ROW_SCOPE_PARAM]
        )

    def test_admin_viewer_writes_no_reserved_key(self):
        client, registry = self._app(ADMIN)
        _login(client, "u1")

        self.assertEqual(
            200,
            client.get("/api/v1/d/l1-cockpit/cards/kpi_offline_mtd").status_code,
        )
        self.assertNotIn(
            BI_WEB_QUERIES.ROW_SCOPE_PARAM, self._last_params(registry)
        )

    def test_bearer_channel_writes_no_reserved_key(self):
        client, registry = self._app(_viewer(regions={"hangzhou"}))

        response = client.get(
            "/api/v1/d/l1-cockpit/cards/kpi_offline_mtd",
            headers={"Authorization": "Bearer machine-token"},
        )

        self.assertEqual(200, response.status_code)
        self.assertNotIn(
            BI_WEB_QUERIES.ROW_SCOPE_PARAM, self._last_params(registry)
        )

    def test_exempt_card_never_receives_the_reserved_key(self):
        client, registry = self._app(_viewer(regions={"hangzhou"}))
        _login(client, "u1")

        self.assertEqual(
            200,
            client.get("/api/v1/d/l1-cockpit/cards/kpi_channel_mtd").status_code,
        )
        self.assertNotIn(
            BI_WEB_QUERIES.ROW_SCOPE_PARAM, self._last_params(registry)
        )

    def test_scope_separates_the_card_cache_key(self):
        _full, _full_class = card_cache_key("kpi_offline_mtd", {})
        hz_key, _hz_class = card_cache_key(
            "kpi_offline_mtd", {BI_WEB_QUERIES.ROW_SCOPE_PARAM: "hangzhou"}
        )
        empty_key, _empty_class = card_cache_key(
            "kpi_offline_mtd", {BI_WEB_QUERIES.ROW_SCOPE_PARAM: ""}
        )

        # 可见范围不同的载荷绝不能共用缓存键（否则就是越权缓存命中）。
        self.assertEqual(3, len({_full, hz_key, empty_key}))

    def test_fingerprint_is_order_independent(self):
        one, _ = card_cache_key(
            "kpi_offline_mtd", {BI_WEB_QUERIES.ROW_SCOPE_PARAM: "hangzhou,hq"}
        )
        other, _ = card_cache_key(
            "kpi_offline_mtd",
            {BI_WEB_QUERIES.ROW_SCOPE_PARAM: BI_WEB_QUERIES.scope_fingerprint(
                frozenset({"hq", "hangzhou"})
            )},
        )
        self.assertEqual(one, other)


if __name__ == "__main__":
    unittest.main()
