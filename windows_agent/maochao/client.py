"""云端任务 API 的 stdlib 客户端；不携带浏览器 Cookie。"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class PriceAgentApiError(RuntimeError):
    def __init__(self, status, code):
        super().__init__(f"price-agent API HTTP {status}: {code}")
        self.status = status
        self.code = code


class PriceAgentClient:
    def __init__(self, base_url, token, *, timeout=30):
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _request(self, method, path, payload=None):
        data = None
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._base + path, data=data, headers=headers, method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        except (urllib.error.URLError, TimeoutError) as exc:
            raise PriceAgentApiError(0, "network_unavailable") from exc
        try:
            result = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PriceAgentApiError(status, "invalid_response") from exc
        if status < 200 or status >= 300:
            detail = result.get("detail", "request_failed") if isinstance(result, dict) else "request_failed"
            raise PriceAgentApiError(status, str(detail)[:128])
        return result

    def config_version(self):
        return self._request("GET", "/config-version")

    def claim(self, agent_id):
        return self._request("POST", "/jobs/claim", {"agent_id": agent_id})

    def heartbeat(self, job_id, payload):
        return self._request("POST", f"/jobs/{urllib.parse.quote(job_id, safe='')}/heartbeat", payload)

    def complete(self, job_id, payload):
        return self._request("POST", f"/jobs/{urllib.parse.quote(job_id, safe='')}/complete", payload)

    def fail(self, job_id, payload):
        return self._request("POST", f"/jobs/{urllib.parse.quote(job_id, safe='')}/fail", payload)
