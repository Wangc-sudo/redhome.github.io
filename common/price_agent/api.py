"""ops-web 内的 Windows 机器 API（与人员会话认证完全分离）。"""
from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from common.price_agent.service import (
    PriceAgentProtocolError,
    claim_job,
    complete_job,
    fail_job,
    heartbeat_job,
)

logger = logging.getLogger(__name__)

_PREFIX = "/price-agent/api"
_MAX_COMPLETE_BYTES = 2 * 1024 * 1024
_MAX_SMALL_BYTES = 64 * 1024


def register_price_agent_routes(app, *, db_connector, config_source, token_hash):
    """注册机器接口。所有方法（含 GET）都必须通过独立 Bearer token。"""

    def require_agent(request: Request):
        if not token_hash:
            raise HTTPException(status_code=503, detail="price_agent_auth_unconfigured")
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="price_agent_unauthorized")
        token = authorization[7:]
        if not token:
            raise HTTPException(status_code=401, detail="price_agent_unauthorized")
        actual = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual, token_hash.lower()):
            raise HTTPException(status_code=401, detail="price_agent_unauthorized")

    async def body(request: Request, limit=_MAX_SMALL_BYTES):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    raise HTTPException(status_code=413, detail="price_agent_body_too_large")
            except ValueError:
                raise HTTPException(status_code=400, detail="bad_request")
        raw = await request.body()
        if len(raw) > limit:
            raise HTTPException(status_code=413, detail="price_agent_body_too_large")
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="bad_request")
        return payload

    def failure(exc):
        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, PriceAgentProtocolError):
            return HTTPException(status_code=exc.status_code, detail=exc.code)
        logger.warning("price-agent API failed: %s", type(exc).__name__)
        return HTTPException(status_code=503, detail="price_agent_unavailable")

    @app.get(_PREFIX + "/config-version")
    def config_version(request: Request):
        require_agent(request)
        try:
            config, digest = config_source.get_config()
            return {"status": "ok", "config": config, "config_hash": digest}
        except Exception as exc:
            raise failure(exc)

    @app.post(_PREFIX + "/jobs/claim")
    async def jobs_claim(request: Request):
        require_agent(request)
        try:
            payload = await body(request)
            config, digest = config_source.get_config()
            with db_connector() as connection:
                result = claim_job(
                    connection, payload.get("agent_id"), config, digest,
                )
            return JSONResponse(result)
        except Exception as exc:
            raise failure(exc)

    @app.post(_PREFIX + "/jobs/{job_id}/heartbeat")
    async def jobs_heartbeat(job_id: str, request: Request):
        require_agent(request)
        try:
            payload = await body(request)
            with db_connector() as connection:
                result = heartbeat_job(connection, job_id, payload)
            return JSONResponse(result)
        except Exception as exc:
            raise failure(exc)

    @app.post(_PREFIX + "/jobs/{job_id}/complete")
    async def jobs_complete(job_id: str, request: Request):
        require_agent(request)
        try:
            payload = await body(request, _MAX_COMPLETE_BYTES)
            with db_connector() as connection:
                result = complete_job(connection, job_id, payload)
            return JSONResponse(result)
        except Exception as exc:
            raise failure(exc)

    @app.post(_PREFIX + "/jobs/{job_id}/fail")
    async def jobs_fail(job_id: str, request: Request):
        require_agent(request)
        try:
            payload = await body(request)
            with db_connector() as connection:
                result = fail_job(connection, job_id, payload)
            return JSONResponse(result)
        except Exception as exc:
            raise failure(exc)
