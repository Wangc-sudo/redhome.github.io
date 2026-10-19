"""按人登录口令（2026-10-19 过渡期裁决）：解掉「全局共享口令可冒用 admin」。

钉死三条：
* **已签发按人口令的人，全局共享口令对其失效**——这是解掉冒用的那一步；
* 口令只存哈希、明文只返回一次，台账永不含明文/哈希；
* 按人口令面不可用（表未迁移/DB 异常）时**不静默退回共享口令**，直接
  503——否则会退化成可冒用的老路径。

DB 面全部 patch，只钉 HTTP 形态与调用纪律。
"""

import unittest
import warnings
from unittest import mock

warnings.filterwarnings(
    "ignore", message="Using `httpx` with `starlette.testclient` is deprecated"
)

from fastapi.testclient import TestClient

from common.bi_web import auth
from common.ops_web.app import create_app
from common.public_data import ops_token
from tests.common.test_bi_web_app import _fake_settings

SECRET = "ops-test-secret"
SHARED = "shared-browser-secret"


def _app(*, token=SHARED):
    return create_app(
        settings=_fake_settings(),
        session_secret=SECRET,
        db_connector=mock.MagicMock(),
        browser_login_token=token,
    )


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


class TokenHashTests(unittest.TestCase):
    """高熵口令 → 单次 SHA-256（无需慢哈希）。"""

    def test_generated_token_is_high_entropy(self):
        for _ in range(5):
            self.assertGreaterEqual(len(ops_token.generate_token()), 40)
        self.assertNotEqual(ops_token.generate_token(),
                            ops_token.generate_token())

    def test_hash_is_deterministic_and_verifiable(self):
        token = ops_token.generate_token()
        digest = ops_token.hash_token(token)
        self.assertEqual(digest, ops_token.hash_token(token))
        self.assertTrue(ops_token.verify_token(token, digest))

    def test_verify_rejects_wrong_token_and_junk(self):
        digest = ops_token.hash_token("tok-1")
        self.assertFalse(ops_token.verify_token("tok-2", digest))
        self.assertFalse(ops_token.verify_token("", digest))
        self.assertFalse(ops_token.verify_token("tok-1", "deadbeef"))


# ---------------------------------------------------------------------------
# 登录：按人口令优先 + 冒用面关闭
# ---------------------------------------------------------------------------


class BrowserLoginWithPersonalTokenTests(unittest.TestCase):
    """``POST /auth/browser-login``：按人口令接管后共享口令必须失效。"""

    def _post(self, body, *, issued=True):
        """``check_token`` 模拟真实行为：只认该人自己的按人口令，其余一律
        不通过——含那枚全局共享口令（否则测不出「冒用面已关闭」）。"""
        with mock.patch.multiple(
                "common.ops_web.app.ops_token",
                is_issued=mock.MagicMock(return_value=issued),
                check_token=mock.MagicMock(
                    side_effect=lambda _c, _u, tok: tok == "personal-token"),
                touch_used=mock.MagicMock(),
        ):
            return TestClient(_app()).post("/auth/browser-login", json=body)

    def test_personal_token_login_succeeds(self):
        response = self._post({"user_id": "u-1", "token": "personal-token"})
        self.assertEqual(200, response.status_code)
        self.assertEqual("u-1", auth.resolve_session_userid(
            response.cookies.get(auth.SESSION_COOKIE), SECRET))

    def test_shared_token_is_rejected_once_personal_token_issued(self):
        """核心：冒用面关闭——已签发者不能再被全局共享口令登录。"""
        response = self._post({"user_id": "u-1", "token": SHARED})
        self.assertEqual(401, response.status_code)

    def test_wrong_personal_token_is_rejected(self):
        self.assertEqual(401, self._post(
            {"user_id": "u-1", "token": "nope"}).status_code)

    def test_shared_token_still_works_for_not_yet_issued_user(self):
        """未签发者走共享口令——换发期的逃生口。"""
        response = self._post(
            {"user_id": "u-2", "token": SHARED}, issued=False)
        self.assertEqual(200, response.status_code)

    def test_personal_token_outage_is_503_not_silent_fallback(self):
        """表未迁移/DB 异常时不能静默退回共享口令（那样等于没改）。"""
        with mock.patch(
                "common.ops_web.app.ops_token.is_issued",
                side_effect=RuntimeError("no table"),
        ):
            response = TestClient(_app()).post(
                "/auth/browser-login", json={"user_id": "u-1", "token": SHARED})
        self.assertEqual(503, response.status_code)

    def test_missing_fields_are_400(self):
        for body in ({}, {"user_id": "u-1"}, {"token": "x"},
                     {"user_id": "  ", "token": "x"}):
            self.assertEqual(400, self._post(body).status_code, body)


# ---------------------------------------------------------------------------
# admin 端点
# ---------------------------------------------------------------------------


class TokenAdminTests(unittest.TestCase):
    """``/api/tokens/issue|revoke`` + 台账：admin 闸 + 明文只回一次。"""

    def _client(self, admin=True):
        from tests.common.test_ops_web_app import (
            _StaticViewerResolver, _admin_viewer,
        )
        from common.bi_web import authz
        viewer = (_admin_viewer() if admin else authz.Viewer(
            userid="u-2", name="李四", scopes=frozenset({"roster"}),
            allowed_regions=frozenset(), is_admin=False, admitted=True))
        return TestClient(create_app(
            settings=_fake_settings(),
            session_secret=SECRET,
            db_connector=mock.MagicMock(),
            viewer_resolver=_StaticViewerResolver(viewer),
            browser_login_token=SHARED,
        ))

    def _login(self, client, admin=True):
        client.cookies.set(
            auth.SESSION_COOKIE,
            auth.issue_session("014341566058939427" if admin else "u-2", SECRET))

    def test_issue_returns_plaintext_once(self):
        client = self._client()
        self._login(client)
        with mock.patch(
                "common.ops_web.app.ops_token.issue_token",
                return_value="issued-plaintext-token",
        ) as issue:
            response = client.post("/api/tokens/issue", json={"user_id": "u-9"})
        self.assertEqual(200, response.status_code)
        self.assertEqual("issued-plaintext-token", response.json()["token"])
        issue.assert_called_once()

    def test_non_admin_cannot_issue(self):
        client = self._client(admin=False)
        self._login(client, admin=False)
        self.assertEqual(
            403, client.post("/api/tokens/issue",
                             json={"user_id": "u-9"}).status_code)

    def test_revoke_reports_count(self):
        client = self._client()
        self._login(client)
        with mock.patch(
                "common.ops_web.app.ops_token.revoke_tokens", return_value=2,
        ):
            response = client.post("/api/tokens/revoke", json={"user_id": "u-9"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(2, response.json()["revoked"])

    def test_ledger_contains_no_plaintext_or_hash(self):
        client = self._client()
        self._login(client)
        with mock.patch(
                "common.ops_web.app.ops_token.list_tokens",
                return_value=[{"user_id": "u-9", "created_at": "2026-10-19",
                               "revoked_at": None}],
        ):
            response = client.get("/api/tokens")
        self.assertEqual(200, response.status_code)
        self.assertNotIn("token", response.json()["items"][0])
        self.assertNotIn("token_hash", response.json()["items"][0])

    def test_issue_requires_user_id(self):
        client = self._client()
        self._login(client)
        self.assertEqual(
            400, client.post("/api/tokens/issue", json={}).status_code)