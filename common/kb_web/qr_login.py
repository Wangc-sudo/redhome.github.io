# -*- coding: utf-8 -*-
"""小易 Web 扫码登录：钉钉 OAuth2.0 授权码 → 本企业 userid（2026-10-26 方案）。

与 H5 免登（``bi_web.auth.DingTalkAuthClient.exchange_auth_code``）的区别：
免登 code 只在钉钉容器内由 JSAPI 取得、走 topapi/v2/user/getuserinfo 直换
userid；扫码登录 code 走 OAuth2 用户授权链：

1. ``POST /v1.0/oauth2/userAccessToken``  授权 code → 用户个人 token
2. ``GET  /v1.0/contact/users/me``        个人 token → unionId
3. ``POST /topapi/user/getbyunionid``     企业 token + unionId → 本企业 userid

两种 code 语义不同，独立客户端不复用免登链路。

安全纪律与 ``bi_web.auth`` 一致：3s 超时 + 1 次重试；失败一律抛泛化
``AuthError``（不含 URL、请求体、响应体、secret、userid）。
"""

import json
import logging
import secrets
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping

from common.bi_web.auth import AuthError
from common.dingtalk.client import _http_json

_log = logging.getLogger(__name__)

#: 钉钉扫码登录统一授权页（新版 OAuth2.0；旧版 qrconnect 已不推荐）。
LOGIN_AUTHORIZE_URL = "https://login.dingtalk.com/oauth2/auth"

_API_BASE = "https://api.dingtalk.com"
_OAPI_BASE = "https://oapi.dingtalk.com"
_USER_TOKEN_PATH = "/v1.0/oauth2/userAccessToken"
_USER_ME_PATH = "/v1.0/contact/users/me"
_GETBYUNIONID_PATH = "/topapi/user/getbyunionid"
_GETUSERINFO_PATH = "/topapi/v2/user/getuserinfo"
_ACCESS_TOKEN_PATH = "/v1.0/oauth2/accessToken"

#: 企业 access_token 缓存窗口（有效期 2h，与 bi_web.auth 同纪律）。
_TOKEN_CACHE_SECONDS = 100 * 60

#: 钉钉外呼纪律：3s 超时 + 1 次重试。
_REQUEST_TIMEOUT_SECONDS = 3
_MAX_ATTEMPTS = 2

#: state 一次性票据有效期（秒）：10 分钟。
STATE_TTL_SECONDS = 600

_KEY_STATE = "kbqa:web:oauth_state"


def new_state():
    """生成 192bit 随机 state（CSRF/重放防护）。"""
    return secrets.token_urlsafe(24)


def build_login_url(app_key, redirect_uri, state):
    """拼装钉钉扫码登录页地址。

    ``redirect_uri`` 必须由服务端固定配置提供——不接受前端传参，防回调
    域劫持。
    """
    query = urllib.parse.urlencode({
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "client_id": app_key,
        "scope": "openid",
        "state": state,
        "prompt": "consent",
    })
    return f"{LOGIN_AUTHORIZE_URL}?{query}"


class OAuthStateStore:
    """扫码登录 state 一次性票据：Redis 主、进程内兜底；消费即作废。"""

    def __init__(self, url=None, *, client=None,
                 ttl_seconds=STATE_TTL_SECONDS):
        self._url = url
        self._client = client
        self._ttl = int(ttl_seconds)

    def _kv(self):
        if self._client is None:
            if self._url:
                import redis
                self._client = redis.Redis.from_url(self._url)
            else:
                self._client = _MemoryKV()
        return self._client

    def issue(self):
        """签发 state 并入库；后端异常 fail-open 照样返回（消费端 fail-closed）。"""
        state = new_state()
        try:
            self._kv().setex(f"{_KEY_STATE}:{state}", self._ttl, "1")
        except Exception:
            pass
        return state

    def consume(self, state):
        """校验并作废 state：存在 → True（随即删除）；不存在/异常 → False。"""
        if not isinstance(state, str) or not state:
            return False
        key = f"{_KEY_STATE}:{state}"
        try:
            kv = self._kv()
            if not kv.get(key):
                return False
            kv.delete(key)
            return True
        except Exception:
            return False


class _MemoryKV:
    """进程内 KV 兜底（接口对齐 redis 子集 get/setex/delete）。"""

    def __init__(self):
        self._data = {}

    def get(self, key):
        entry = self._data.get(key)
        if entry is None:
            return None
        payload, expires_at = entry
        if expires_at is not None and time.time() >= expires_at:
            self._data.pop(key, None)
            return None
        return payload

    def setex(self, key, ttl, value):
        self._data[key] = (value, time.time() + ttl)

    def delete(self, key):
        self._data.pop(key, None)


class DingTalkQrLoginClient:
    """扫码登录的 HTTP 面：OAuth2 用户授权链 → 本企业 userid。

    注入约定同 ``DingTalkAuthClient``：``request_json`` 为 None 时走真实
    HTTP 与真实企业 token，否则走注入的 fake 与固定测试 token。
    """

    def __init__(self, app_key, app_secret, request_json=None):
        #: AppKey 非敏感（登录页 URL 明文携带），透出给路由拼装 login_url。
        self.app_key = app_key
        self._app_secret = app_secret
        self._request_json = (_http_json if request_json is None
                              else request_json)
        self._token_provider = (
            self._request_access_token
            if request_json is None
            else self._test_access_token
        )
        self._token = None
        self._token_timestamp = 0.0

    def exchange_qr_code(self, code):
        """扫码授权 code → ``(userid, union_id)``；任何失败抛泛化 AuthError。

        双通道：先按**企业内部应用免登码**语义直换（topapi/v2/user/
        getuserinfo，权限与 H5 免登同面、现成）；失败再走 OAuth2 用户授权链
        （2026-10-26 实测：users/me 需「通讯录个人信息读」权限点，未配时
        403 AccessTokenPermissionDenied——免登码通道可完全绕开它）。
        ``union_id`` 仅 OAuth 通道可得，免登码通道返回 None。
        """
        if not isinstance(code, str) or not code:
            raise AuthError("dingtalk qr login failed")
        try:
            return self._userid_by_auth_code(code), None
        except AuthError:
            _log.info("qr login: getuserinfo path failed, "
                      "fallback to oauth chain")
        user_token = self._user_access_token(code)
        union_id = self._user_union_id(user_token)
        userid = self._userid_by_union(union_id)
        return userid, union_id

    # ------------------------------------------------------------------
    # 通道一：企业内部应用免登码（与 H5 免登同接口同权限面）
    # ------------------------------------------------------------------
    def _userid_by_auth_code(self, code):
        url = (
            _OAPI_BASE + _GETUSERINFO_PATH
            + "?access_token=" + urllib.parse.quote(self._access_token(), safe="")
        )
        result = self._call(url, method="POST", body={"code": code},
                            errcode_gate=True, step="getuserinfo")
        userid = result.get("userid") if isinstance(result, Mapping) else None
        if not isinstance(userid, str) or not userid:
            _log.warning("qr login getuserinfo: userid missing")
            raise AuthError("dingtalk qr login failed")
        return userid

    # ------------------------------------------------------------------
    # 通道二：OAuth2 用户授权链（兜底）
    # ------------------------------------------------------------------
    def _user_access_token(self, code):
        body = {
            "clientId": self.app_key,
            "clientSecret": self._app_secret,
            "code": code,
            "grantType": "authorization_code",
        }
        result = self._call(_API_BASE + _USER_TOKEN_PATH,
                            method="POST", body=body, step="userAccessToken")
        token = result.get("accessToken") if isinstance(result, Mapping) else None
        if not isinstance(token, str) or not token:
            _log.warning("qr login userAccessToken: accessToken missing")
            raise AuthError("dingtalk qr login failed")
        return token

    def _user_union_id(self, user_token):
        headers = {"x-acs-dingtalk-access-token": user_token}
        result = self._call(_API_BASE + _USER_ME_PATH,
                            method="GET", headers=headers, step="usersMe")
        union_id = result.get("unionId") if isinstance(result, Mapping) else None
        if not isinstance(union_id, str) or not union_id:
            _log.warning("qr login usersMe: unionId missing")
            raise AuthError("dingtalk qr login failed")
        return union_id

    def _userid_by_union(self, union_id):
        url = (
            _OAPI_BASE + _GETBYUNIONID_PATH
            + "?access_token=" + urllib.parse.quote(self._access_token(), safe="")
        )
        result = self._call(url, method="POST", body={"unionid": union_id},
                            errcode_gate=True, step="getbyunionid")
        userid = result.get("userid") if isinstance(result, Mapping) else None
        if not isinstance(userid, str) or not userid:
            _log.warning("qr login getbyunionid: userid missing")
            raise AuthError("dingtalk qr login failed")
        return userid

    # ------------------------------------------------------------------
    # Transport（3s 超时 + 1 次重试；errcode_gate 用于 oapi 老接口）
    # ------------------------------------------------------------------
    @staticmethod
    def _diagnose(exc):
        """提取失败诊断码：异常类型 + 钉钉错误码（HTTPError body 的
        code/errcode 字段；不含 URL/请求体/敏感值）。"""
        detail = type(exc).__name__
        if isinstance(exc, urllib.error.HTTPError):
            try:
                payload = json.loads(exc.read().decode())
            except Exception:
                payload = None
            if isinstance(payload, Mapping):
                code = payload.get("code", payload.get("errcode"))
                if code is not None:
                    detail = f"{detail}({exc.code},{code})"
        return detail

    def _call(self, url, *, method, body=None, headers=None,
              errcode_gate=False, step="?"):
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self._request_json(
                    url, method=method, body=body, headers=headers,
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                if attempt + 1 >= _MAX_ATTEMPTS:
                    _log.warning("qr login %s failed: %s",
                                 step, self._diagnose(exc))
                    raise AuthError("dingtalk qr login failed") from None
                continue
            if not isinstance(response, Mapping):
                _log.warning("qr login %s failed: non-mapping response", step)
                raise AuthError("dingtalk qr login failed")
            if errcode_gate:
                if response.get("errcode") != 0:
                    _log.warning("qr login %s failed: errcode=%s",
                                 step, response.get("errcode"))
                    raise AuthError("dingtalk qr login failed")
                return response.get("result")
            return response
        raise AuthError("dingtalk qr login failed")  # pragma: no cover

    # ------------------------------------------------------------------
    # 企业 access_token（TTL 缓存，与 bi_web.auth 同纪律）
    # ------------------------------------------------------------------
    def _access_token(self):
        if (
            self._token is not None
            and time.time() - self._token_timestamp < _TOKEN_CACHE_SECONDS
        ):
            return self._token
        token = self._token_provider()
        if not isinstance(token, str) or not token:
            raise AuthError("dingtalk qr login failed")
        self._token = token
        self._token_timestamp = time.time()
        return token

    def _request_access_token(self):
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self._request_json(
                    _API_BASE + _ACCESS_TOKEN_PATH,
                    method="POST",
                    body={"appKey": self.app_key,
                          "appSecret": self._app_secret},
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                if attempt + 1 >= _MAX_ATTEMPTS:
                    _log.warning("qr login appAccessToken failed: %s",
                                 self._diagnose(exc))
                    raise AuthError("dingtalk qr login failed") from None
                continue
            if not isinstance(response, Mapping):
                raise AuthError("dingtalk qr login failed")
            return response.get("accessToken")
        raise AuthError("dingtalk qr login failed")  # pragma: no cover

    @staticmethod
    def _test_access_token():
        return "test-access-token"
