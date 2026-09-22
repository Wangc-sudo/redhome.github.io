"""Shared Nacos client factory with Nacos 2.x auth support.

``nacos-sdk-python==0.1.12`` predates Nacos 2.x token auth: when the server
runs with ``NACOS_AUTH_ENABLE=true`` the SDK's requests are rejected with
403 ("Insufficient privilege").  ``RestNacosClient`` implements the two
operations this repository needs (``get_config`` / ``publish_config``)
against the Nacos open API directly, with a cached ``accessToken`` login.

``build_nacos_client`` keeps the old SDK path for unauthenticated servers
(local integration Nacos) and returns the REST client whenever a username
is configured -- call sites stay one-liners either way.

The REST surface is intentionally tiny; HTTP is injectable (``http_get`` /
``http_post``) so tests never touch a real server.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class NacosAuthError(RuntimeError):
    pass


def _default_http_get(url, timeout):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")


def _default_http_post(url, form, timeout):
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(form).encode("utf-8"), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")


class RestNacosClient:
    """Duck-typed drop-in for the SDK client (auth-enabled Nacos 2.x)."""

    #: Re-login this many seconds before the server-side token expiry.
    _TOKEN_EXPIRY_MARGIN = 60

    def __init__(self, server, *, namespace="", username=None, password=None,
                 timeout=5, http_get=None, http_post=None, monotonic=None):
        self._base = server if "://" in server else f"http://{server}"
        self._base = self._base.rstrip("/")
        self._namespace = namespace or ""
        self._username = username
        self._password = password
        self._timeout = timeout
        self._http_get = http_get or _default_http_get
        self._http_post = http_post or _default_http_post
        self._monotonic = monotonic or time.monotonic
        self._token = None
        self._token_expires_at = 0.0
        self._lock = threading.Lock()

    # -- auth ---------------------------------------------------------------
    def _access_token(self):
        if not self._username:
            return None
        with self._lock:
            now = self._monotonic()
            if self._token is not None and now < self._token_expires_at:
                return self._token
            status, body = self._http_post(
                f"{self._base}/nacos/v1/auth/login",
                {"username": self._username, "password": self._password or ""},
                self._timeout,
            )
            if status != 200:
                raise NacosAuthError(f"nacos login failed: HTTP {status}")
            try:
                payload = json.loads(body)
                token = payload["accessToken"]
            except (ValueError, KeyError) as error:
                raise NacosAuthError("nacos login returned no accessToken") from error
            ttl = payload.get("tokenTtl", 18000)
            self._token = token
            self._token_expires_at = now + max(0, ttl - self._TOKEN_EXPIRY_MARGIN)
            return token

    def _query(self, params):
        if self._namespace:
            params["tenant"] = self._namespace
        token = self._access_token()
        if token:
            params["accessToken"] = token
        return params

    # -- SDK-compatible surface ----------------------------------------------
    def get_config(self, data_id, group, timeout=None):
        params = self._query({"dataId": data_id, "group": group})
        url = f"{self._base}/nacos/v1/cs/configs?{urllib.parse.urlencode(params)}"
        status, body = self._http_get(url, timeout or self._timeout)
        if status == 404:
            return None
        if status != 200:
            raise NacosAuthError(f"nacos get_config failed: HTTP {status}")
        return body

    def publish_config(self, data_id, group, content, config_type="yaml",
                       timeout=None):
        form = self._query(
            {
                "dataId": data_id,
                "group": group,
                "content": content,
                "type": config_type,
            }
        )
        status, body = self._http_post(
            f"{self._base}/nacos/v1/cs/configs", form, timeout or self._timeout
        )
        if status != 200 or body.strip().lower() != "true":
            raise NacosAuthError(
                f"nacos publish_config failed: HTTP {status} {body.strip()!r}"
            )
        return True


def build_nacos_client(server, *, namespace="", username=None, password=None,
                       timeout=5, **rest_kwargs):
    """Return a client for *server*.

    Authenticated servers (``username`` set) get the REST client with Nacos
    2.x token login; anything else keeps the legacy SDK client so the local
    integration environment behaves exactly as before.
    """
    if username:
        return RestNacosClient(
            server,
            namespace=namespace,
            username=username,
            password=password,
            timeout=timeout,
            **rest_kwargs,
        )
    from nacos import NacosClient

    return NacosClient(server, namespace=namespace)
