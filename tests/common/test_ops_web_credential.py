"""运维后台登录凭据（2026-10-19 用户裁决「用户名=工号、密码=系统生成」）。

钉死三条纪律：
* 密码是**可轮换的独立 secret**——不是手机号/工号/钉钉号（组织属性，不可
  轮换、可枚举），故库里只存 PBKDF2 哈希，明文仅生成时返回一次；
* 防探测——工号不存在 / 密码错 / 已锁定**同一形态 401**（18100 对公网
  开放，不能让探测者区分账号存在性）；
* 签发会话 ≠ 获得权限——登录只证明「你是谁」，权限仍由 bi_authz 判定。
"""

import unittest
import warnings
from datetime import datetime, timedelta, timezone
from unittest import mock

warnings.filterwarnings(
    "ignore", message="Using `httpx` with `starlette.testclient` is deprecated"
)

from fastapi.testclient import TestClient

from common.bi_web import auth
from common.ops_web.app import create_app
from common.public_data import ops_credential
from tests.common.test_bi_web_app import _fake_settings

SECRET = "ops-test-secret"


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


class PasswordHashTests(unittest.TestCase):
    """PBKDF2 哈希：确定性、盐生效、错误密码必 False。"""

    def test_hash_is_deterministic_for_same_salt(self):
        salt = "00112233445566778899aabbccddeeff"
        self.assertEqual(
            ops_credential.hash_password("abc123XYZ", salt_hex=salt),
            ops_credential.hash_password("abc123XYZ", salt_hex=salt),
        )

    def test_different_salt_yields_different_hash(self):
        """盐确实参与派生——防止「盐与哈希用了两枚盐」这类错配静默通过。"""
        first = ops_credential.hash_password("abc123XYZ", salt_hex="aa" * 16)
        second = ops_credential.hash_password("abc123XYZ", salt_hex="bb" * 16)
        self.assertNotEqual(first, second)

    def test_verify_accepts_the_right_password(self):
        salt = ops_credential.new_salt_hex()
        digest = ops_credential.hash_password("S3cret-pass", salt_hex=salt)
        self.assertTrue(ops_credential.verify_password(
            "S3cret-pass", salt_hex=salt,
            iterations=ops_credential.ITERATIONS, expected_hash=digest))

    def test_verify_rejects_wrong_password_and_broken_fields(self):
        salt = ops_credential.new_salt_hex()
        digest = ops_credential.hash_password("S3cret-pass", salt_hex=salt)
        self.assertFalse(ops_credential.verify_password(
            "wrong-pass", salt_hex=salt,
            iterations=ops_credential.ITERATIONS, expected_hash=digest))
        self.assertFalse(ops_credential.verify_password(
            "S3cret-pass", salt_hex=salt,
            iterations=ops_credential.ITERATIONS, expected_hash="deadbeef"))
        self.assertFalse(ops_credential.verify_password(
            "", salt_hex=salt,
            iterations=ops_credential.ITERATIONS, expected_hash=digest))

    def test_generated_password_is_readable_and_strong(self):
        for _ in range(5):
            password = ops_credential.generate_password()
            self.assertEqual(ops_credential._INITIAL_PASSWORD_LENGTH,
                             len(password))
            self.assertTrue(
                set(password) <= set(ops_credential._PASSWORD_ALPHABET))
            ops_credential.validate_password(password)   # 必满足复杂度
        self.assertNotEqual(ops_credential.generate_password(),
                            ops_credential.generate_password())


class ValidationTests(unittest.TestCase):
    """工号与密码的形态校验（防超长串/畸形进查询）。"""

    def test_job_number_accepts_directory_shapes(self):
        for value in ("HY-006", "00123", "A.b_c-1", "hy006"):
            self.assertEqual(value, ops_credential.validate_job_number(value))

    def test_job_number_rejects_junk(self):
        for value in ("", "  ", "ab", None, 123, "x" * 33, "张 三", "a;b"):
            with self.assertRaises(ops_credential.CredentialError):
                ops_credential.validate_job_number(value)

    def test_password_length_bounds(self):
        ops_credential.validate_password("a" * ops_credential.MIN_PASSWORD_LENGTH)
        ops_credential.validate_password("a" * ops_credential.MAX_PASSWORD_LENGTH)
        for bad in ("short", "a" * 129, " spaced ", None):
            with self.assertRaises(ops_credential.CredentialError):
                ops_credential.validate_password(bad)


class LockTests(unittest.TestCase):
    """锁定窗口判定：无窗口/已过 = 可用，未来 = 锁定。"""

    def _now(self):
        return datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)

    def test_no_window_is_not_locked(self):
        self.assertFalse(ops_credential.is_locked(None, now=self._now()))

    def test_past_window_is_not_locked(self):
        past = (self._now() - timedelta(minutes=1)).strftime(
            "%Y-%m-%d %H:%M:%S.%f")
        self.assertFalse(ops_credential.is_locked(past, now=self._now()))

    def test_future_window_is_locked(self):
        future = (self._now() + timedelta(minutes=5)).strftime(
            "%Y-%m-%d %H:%M:%S.%f")
        self.assertTrue(ops_credential.is_locked(future, now=self._now()))


# ---------------------------------------------------------------------------
# 端点（DB 面全部 patch，只钉 HTTP 形态与调用纪律）
# ---------------------------------------------------------------------------


def _row(*, must_change=False, locked_until=None, failed=0):
    salt = ops_credential.new_salt_hex()
    return ops_credential.CredentialRow(
        user_id="u-1",
        algo=ops_credential.ALGO,
        iterations=ops_credential.ITERATIONS,
        salt_hex=salt,
        password_hash=ops_credential.hash_password("right-pass", salt_hex=salt),
        must_change=must_change,
        failed_count=failed,
        locked_until=locked_until,
    )


class PasswordLoginTests(unittest.TestCase):
    """``POST /auth/password-login``：工号+密码 → 同一枚 session cookie。"""

    def _client(self):
        return TestClient(create_app(
            settings=_fake_settings(),
            session_secret=SECRET,
            db_connector=mock.MagicMock(),
        ))

    def _patch(self, **overrides):
        defaults = {
            "resolve_user_by_job_number": mock.MagicMock(
                return_value=("u-1", "张三")),
            "fetch_credential": mock.MagicMock(return_value=_row()),
            "is_locked": mock.MagicMock(return_value=False),
            "verify_password": mock.MagicMock(return_value=True),
            "register_failure": mock.MagicMock(),
            "clear_failures": mock.MagicMock(),
        }
        defaults.update(overrides)
        return defaults

    def _post(self, patches, body):
        with mock.patch.multiple(
                "common.ops_web.app.ops_credential", **patches):
            return self._client().post("/auth/password-login", json=body)

    def test_success_issues_session_and_reports_must_change(self):
        response = self._post(
            self._patch(),
            {"job_number": "HY-006", "password": "right-pass"},
        )
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["must_change"] is False)
        cookie = response.cookies.get(auth.SESSION_COOKIE)
        self.assertEqual("u-1", auth.resolve_session_userid(cookie, SECRET))

    def test_initial_password_flags_must_change(self):
        response = self._post(
            self._patch(fetch_credential=mock.MagicMock(
                return_value=_row(must_change=True))),
            {"job_number": "HY-006", "password": "right-pass"},
        )
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["must_change"])

    def test_unknown_job_number_is_401_same_as_wrong_password(self):
        """防探测：工号不存在与密码错误必须无法区分。"""
        unknown = self._post(
            self._patch(resolve_user_by_job_number=mock.MagicMock(
                return_value=None)),
            {"job_number": "NOPE-1", "password": "right-pass"},
        )
        wrong = self._post(
            self._patch(verify_password=mock.MagicMock(return_value=False)),
            {"job_number": "HY-006", "password": "bad"},
        )
        self.assertEqual(401, unknown.status_code)
        self.assertEqual(401, wrong.status_code)
        self.assertEqual(unknown.json(), wrong.json())

    def test_wrong_password_registers_failure_and_clears_nothing(self):
        register = mock.MagicMock()
        self._post(
            self._patch(verify_password=mock.MagicMock(return_value=False),
                        register_failure=register),
            {"job_number": "HY-006", "password": "bad"},
        )
        register.assert_called_once()

    def test_locked_credential_skips_password_check(self):
        verify = mock.MagicMock(return_value=True)
        response = self._post(
            self._patch(is_locked=mock.MagicMock(return_value=True),
                        verify_password=verify),
            {"job_number": "HY-006", "password": "right-pass"},
        )
        self.assertEqual(401, response.status_code)
        verify.assert_not_called()

    def test_missing_fields_are_400(self):
        for body in ({}, {"job_number": "HY-006"},
                     {"job_number": "HY-006", "password": ""},
                     {"password": "x"}):
            response = self._post(self._patch(), body)
            self.assertEqual(400, response.status_code, body)

    def test_success_clears_failure_counter(self):
        clear = mock.MagicMock()
        self._post(self._patch(clear_failures=clear),
                   {"job_number": "HY-006", "password": "right-pass"})
        clear.assert_called_once()


class ChangePasswordTests(unittest.TestCase):
    """``POST /auth/change-password``：必须校验旧密码（防会话劫持后接管）。"""

    def _client(self, viewer):
        from tests.common.test_ops_web_app import _StaticViewerResolver
        return TestClient(create_app(
            settings=_fake_settings(),
            session_secret=SECRET,
            db_connector=mock.MagicMock(),
            viewer_resolver=_StaticViewerResolver(viewer),
        ))

    def _viewer(self):
        from common.bi_web import authz
        return authz.Viewer(
            userid="u-1", name="张三", scopes=frozenset(),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )

    def _post(self, body, *, old_ok=True):
        with mock.patch.multiple(
                "common.ops_web.app.ops_credential",
                fetch_credential=mock.MagicMock(return_value=_row()),
                verify_password=mock.MagicMock(return_value=old_ok),
                set_password=mock.MagicMock(return_value=1),
        ):
            client = self._client(self._viewer())
            client.cookies.set(
                auth.SESSION_COOKIE, auth.issue_session("u-1", SECRET))
            return client.post("/auth/change-password", json=body)

    def test_change_succeeds_with_correct_old_password(self):
        self.assertEqual(200, self._post(
            {"old_password": "right-pass",
             "new_password": "brand-new-pass-1"}).status_code)

    def test_wrong_old_password_is_rejected(self):
        self.assertEqual(401, self._post(
            {"old_password": "nope",
             "new_password": "brand-new-pass-1"}, old_ok=False).status_code)

    def test_weak_new_password_is_400(self):
        self.assertEqual(400, self._post(
            {"old_password": "right-pass", "new_password": "123"}).status_code)


class CredentialIssueTests(unittest.TestCase):
    """``/api/credentials/issue|reset``：admin 闸 + 明文只返回一次。"""

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
        ))

    def _post(self, path, body, *, admin=True, exists=True):
        with mock.patch.multiple(
                "common.ops_web.app.ops_credential",
                fetch_credential=mock.MagicMock(
                    return_value=_row() if exists else None),
                issue_initial_password=mock.MagicMock(
                    return_value="TempPass-12345"),
        ):
            client = self._client(admin)
            client.cookies.set(
                auth.SESSION_COOKIE, auth.issue_session(
                    "014341566058939427" if admin else "u-2", SECRET))
            return client.post(path, json=body)

    def test_issue_returns_plaintext_once_and_must_change(self):
        response = self._post("/api/credentials/issue", {"user_id": "u-9"})
        self.assertEqual(200, response.status_code)
        self.assertEqual("TempPass-12345", response.json()["password"])
        self.assertTrue(response.json()["must_change"])

    def test_non_admin_is_forbidden(self):
        self.assertEqual(403, self._post(
            "/api/credentials/issue", {"user_id": "u-9"}, admin=False
        ).status_code)

    def test_reset_requires_existing_credential(self):
        self.assertEqual(400, self._post(
            "/api/credentials/reset", {"user_id": "u-9"}, exists=False
        ).status_code)

    def test_issue_requires_user_id(self):
        self.assertEqual(400, self._post("/api/credentials/issue", {})
                         .status_code)