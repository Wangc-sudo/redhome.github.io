"""钉钉免登认证：HMAC 签名 session 与 authCode→userid 交换客户端（设计稿 §5）。

* session 为**纯签名 cookie**（stdlib ``hmac``/``hashlib``/``secrets``），
  密钥由 ``BI_WEB_SESSION_SECRET`` 环境变量注入，无服务端存储、不引新依赖；
  8h 有效期、过半滑动续期。bi-web 与 ops-web 共用本模块（铁律 7：禁止
  复制一份改改用）。
* 交换客户端复用 :class:`OrgReadGateway` 的注入约定：``request_json``
  为 ``None`` 时走真实 HTTP 与真实 token，否则走注入的 fake 与固定测试
  token——生产代码路径与测试代码路径仅差这一个可替换点。3s 超时 +
  1 次重试，失败抛泛化 :class:`AuthError`。
* 错误纪律与 ``org_read`` 一致：异常消息不含 URL、请求体、响应体或
  AppSecret；userid 不进异常消息（日志脱敏由调用方保证）。
"""

import base64
import hashlib
import hmac
import secrets
import time
import urllib.parse
from collections.abc import Mapping
from pathlib import Path

from common.dingtalk.client import _http_json

#: session cookie 名与有效期（设计稿 §5.1：8h 滑动）。
SESSION_COOKIE = "bi_session"
SESSION_TTL_SECONDS = 8 * 3600

#: 未登录/未授权/已登出的统一提示页（bi-web 与 ops-web 共用，铁律 7：
#: 认证相关的每一寸都只存在一份）。静态、无数据、无鉴权。
AUTH_ENTRY_PAGE = Path(__file__).parent / "web" / "auth-entry.html"

#: 剩余有效期低于此阈值时滑动续期（TTL 的一半）。
_RENEW_THRESHOLD_SECONDS = SESSION_TTL_SECONDS // 2

#: access_token 缓存窗口（同 OrgReadGateway：有效期 2h，缓存 100min）。
_TOKEN_CACHE_SECONDS = 100 * 60

#: 钉钉外呼纪律（设计稿 §8）：3s 超时 + 1 次重试。
_REQUEST_TIMEOUT_SECONDS = 3
_MAX_ATTEMPTS = 2

_API_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
_OAPI_BASE = "https://oapi.dingtalk.com"
_GETUSERINFO_PATH = "/topapi/v2/user/getuserinfo"


# ---------------------------------------------------------------------------
# HMAC 签名 session（纯函数，无 IO，全部可离线单测）
# ---------------------------------------------------------------------------

class SessionError(ValueError):
    """session 校验失败的基类（消息泛化，不含 token 内容）。"""


class SessionFormatError(SessionError):
    """token 结构不合法（分段 / base64 / 字段数 / 过期时间戳）。"""


class SessionTamperedError(SessionError):
    """签名不匹配（篡改或密钥不一致）。"""


class SessionExpiredError(SessionError):
    """签名有效但已过期。"""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _signature(payload: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()


def issue_session(userid, secret, *, now=None, ttl_seconds=SESSION_TTL_SECONDS):
    """签发 ``base64url(userid|exp|nonce).hmac`` 形式的 session token。"""
    if not isinstance(userid, str) or not userid:
        raise SessionFormatError("session userid must be a non-empty string")
    if not secret:
        raise SessionFormatError("session secret must be configured")
    issued_at = time.time() if now is None else now
    exp = int(issued_at) + int(ttl_seconds)
    payload = _b64encode(f"{userid}|{exp}|{secrets.token_hex(8)}".encode("ascii"))
    return f"{payload}.{_signature(payload, secret)}"


def verify_session(token, secret, *, now=None):
    """校验 token，返回 ``(userid, exp_unix)``；各失败分支各自抛错。

    *SessionFormatError* / *SessionTamperedError* / *SessionExpiredError*
    都是 :class:`SessionError` 的子类——路由层一把抓，测试层分别钉死。
    """
    if not isinstance(token, str) or not secret:
        raise SessionFormatError("session token malformed")
    parts = token.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise SessionFormatError("session token malformed")
    payload, signature = parts
    if not hmac.compare_digest(signature, _signature(payload, secret)):
        raise SessionTamperedError("session signature mismatch")
    try:
        fields = _b64decode(payload).decode("ascii").split("|")
    except Exception:
        raise SessionFormatError("session token malformed") from None
    if len(fields) != 3 or not fields[0]:
        raise SessionFormatError("session token malformed")
    userid, exp_text, _nonce = fields
    try:
        exp = int(exp_text)
    except ValueError:
        raise SessionFormatError("session token malformed") from None
    current = time.time() if now is None else now
    if current >= exp:
        raise SessionExpiredError("session expired")
    return userid, exp


def slide_session(token, secret, *, now=None, ttl_seconds=SESSION_TTL_SECONDS):
    """滑动续期：剩余有效期过半即重签，否则返回 ``None``（无需新 cookie）。

    校验失败的分支异常原样上抛——调用方（认证依赖）按「无 session」处理。
    """
    userid, exp = verify_session(token, secret, now=now)
    current = time.time() if now is None else now
    if exp - current < _RENEW_THRESHOLD_SECONDS:
        return issue_session(userid, secret, now=now, ttl_seconds=ttl_seconds)
    return None


def resolve_session_userid(token, secret, *, now=None):
    """认证依赖用：有效 → userid；任何失败 → ``None``（不暴露分支）。"""
    try:
        userid, _exp = verify_session(token, secret, now=now)
        return userid
    except SessionError:
        return None


# ---------------------------------------------------------------------------
# 钉钉 authCode → userid 交换客户端
# ---------------------------------------------------------------------------

class AuthError(RuntimeError):
    """免登交换失败（消息不含 URL、请求体、响应体或 AppSecret）。"""


class DingTalkAuthClient:
    """钉钉免登的 HTTP 面：gettoken（TTL 缓存）→ getuserinfo → userid。

    与 :class:`OrgReadGateway` 同样的注入约定：``request_json`` 为
    ``None`` 时走真实 HTTP 与真实 token，否则走注入的 fake 与固定测试
    token。每次外呼 3s 超时、失败重试 1 次，最终失败抛泛化
    :class:`AuthError`。
    """

    def __init__(self, app_key, app_secret, request_json=None):
        self._app_key = app_key
        self._app_secret = app_secret
        self._request_json = _http_json if request_json is None else request_json
        self._token_provider = (
            self._request_access_token
            if request_json is None
            else self._test_access_token
        )
        self._token = None
        self._token_timestamp = 0.0

    def exchange_auth_code(self, auth_code):
        """用钉钉容器内的 authCode 换 ``userid``（字符串）。"""
        if not isinstance(auth_code, str) or not auth_code:
            raise AuthError("dingtalk auth exchange failed")
        result = self._call_with_retry(
            self._getuserinfo_url(), {"code": auth_code}
        )
        if not isinstance(result, Mapping):
            raise AuthError("dingtalk auth exchange failed")
        userid = result.get("userid")
        if not isinstance(userid, str) or not userid:
            raise AuthError("dingtalk auth exchange failed")
        return userid

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _getuserinfo_url(self):
        return (
            f"{_OAPI_BASE}{_GETUSERINFO_PATH}?access_token="
            + urllib.parse.quote(self._get_access_token(), safe="")
        )

    def _call_with_retry(self, url, body):
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self._request_json(
                    url, method="POST", body=body,
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except Exception:
                if attempt + 1 >= _MAX_ATTEMPTS:
                    raise AuthError("dingtalk auth exchange failed") from None
                continue
            if not isinstance(response, Mapping):
                raise AuthError("dingtalk auth exchange failed")
            if response.get("errcode") != 0:
                raise AuthError("dingtalk auth exchange failed")
            return response.get("result")
        raise AuthError("dingtalk auth exchange failed")  # pragma: no cover

    def _get_access_token(self):
        if (
            self._token is not None
            and time.time() - self._token_timestamp < _TOKEN_CACHE_SECONDS
        ):
            return self._token
        token = self._token_provider()
        if not isinstance(token, str) or not token:
            raise AuthError("dingtalk auth token failed")
        self._token = token
        self._token_timestamp = time.time()
        return token

    def _request_access_token(self):
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self._request_json(
                    _API_TOKEN_URL,
                    method="POST",
                    body={"appKey": self._app_key, "appSecret": self._app_secret},
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except Exception:
                if attempt + 1 >= _MAX_ATTEMPTS:
                    raise AuthError("dingtalk auth token failed") from None
                continue
            if not isinstance(response, Mapping):
                raise AuthError("dingtalk auth token failed")
            return response.get("accessToken")
        raise AuthError("dingtalk auth token failed")  # pragma: no cover

    @staticmethod
    def _test_access_token():
        return "test-access-token"


def mask_userid(userid):
    """日志脱敏：只留首尾各两位，中间打码（userid 不整体进日志）。"""
    if not isinstance(userid, str) or len(userid) <= 4:
        return "****"
    return f"{userid[:2]}{'*' * (len(userid) - 4)}{userid[-2:]}"
