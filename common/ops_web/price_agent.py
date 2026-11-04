"""猫超-自动比价功能的人员管理读面与受控写操作。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from fastapi import Depends, HTTPException, Request

from common.price_agent.config import build_config_source
from common.price_agent.service import PriceAgentProtocolError, dispatch_job, utc_now

RETRYABLE_STATUSES = frozenset({"failed", "login_expired", "timed_out"})
JOB_STATUSES = frozenset({
    "queued", "running", "waiting_login", "completed", "failed",
    "timed_out", "login_expired", "cancelled",
})
MAX_PAGE_SIZE = 100
MAX_RECOLLECT = 100


def _page(value, default=1):
    try:
        result = int(value or default)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="page_invalid") from None
    if not 1 <= result <= 100000:
        raise HTTPException(status_code=400, detail="page_invalid")
    return result


def _page_size(value):
    result = _page(value, 20)
    if result > MAX_PAGE_SIZE:
        raise HTTPException(status_code=400, detail="page_size_invalid")
    return result


def _text(value, field, *, required=False, max_length=128):
    result = str(value or "").strip()
    if required and not result:
        raise HTTPException(status_code=400, detail=f"{field}_required")
    if len(result) > max_length or "\x00" in result:
        raise HTTPException(status_code=400, detail=f"{field}_invalid")
    return result


def _json_object(value):
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


def _json_list(value):
    if isinstance(value, list):
        return value
    try:
        return json.loads(value or "[]")
    except (TypeError, ValueError):
        return []


def _job_out(row):
    item = dict(row)
    config = _json_object(item.pop("config_snapshot", None))
    item["subject_summary"] = _json_list(item.get("subject_summary"))
    item["subjects"] = config.get("subjects") or []
    item["selected_skus"] = config.get("selected_skus") or []
    item.pop("lease_token_hash", None)
    item.pop("completion_hash", None)
    return item


def _fetch_job(connection, job_id):
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM `pd_price_agent_job` WHERE `job_id` = %s", (job_id,))
        row = cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    return dict(row)


def _artifacts(connection, job_id):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT `id`, `job_id`, `subject_key`, `artifact_type`, `bucket`, "
            "`object_key`, `sha256`, `size_bytes`, `upload_status`, `created_at` "
            "FROM `pd_price_agent_artifact` WHERE `job_id` = %s ORDER BY `id`",
            (job_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


def list_agents(connection, *, now=None):
    stamp = now or utc_now()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT `agent_id`, `last_seen_at`, `last_claim_at`, `last_job_id` "
            "FROM `pd_price_agent_status` ORDER BY `last_seen_at` DESC"
        )
        rows = [dict(row) for row in cursor.fetchall()]
    for row in rows:
        age = max(0, int((stamp - row["last_seen_at"]).total_seconds()))
        row["age_seconds"] = age
        row["status"] = "online" if age <= 60 else ("idle" if age <= 180 else "offline")
    return rows


def _digest(config):
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def retry_job(connection, job_id):
    job = _fetch_job(connection, job_id)
    if job["status"] not in RETRYABLE_STATUSES:
        raise HTTPException(status_code=400, detail="job_not_retryable")
    config = _json_object(job["config_snapshot"])
    return dispatch_job(
        connection, config, _digest(config), source="retry", parent_job_id=job_id,
    )


def recollect_job(connection, job_id, selected_skus):
    job = _fetch_job(connection, job_id)
    if not isinstance(selected_skus, list) or not 1 <= len(selected_skus) <= MAX_RECOLLECT:
        raise HTTPException(status_code=400, detail="selected_skus_invalid")
    selected = []
    for value in selected_skus:
        if not isinstance(value, dict):
            raise HTTPException(status_code=400, detail="selected_skus_invalid")
        selected.append({
            "subject_key": _text(value.get("subject_key"), "subject_key", required=True),
            "item_id": _text(value.get("item_id"), "item_id", required=True, max_length=64),
            "sku_id": _text(value.get("sku_id"), "sku_id", required=True, max_length=64),
        })
    unique = {(item["subject_key"], item["item_id"], item["sku_id"]) for item in selected}
    if len(unique) != len(selected):
        raise HTTPException(status_code=400, detail="selected_skus_duplicate")

    config = _json_object(job["config_snapshot"])
    subjects = set(config.get("subjects") or [])
    if any(item["subject_key"] not in subjects for item in selected):
        raise HTTPException(status_code=400, detail="subject_out_of_scope")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT `subject_key`, `item_id`, `sku_id` "
            "FROM `fact_maochao_price_snapshot` WHERE `job_id` = %s",
            (job_id,),
        )
        available = {(row["subject_key"], row["item_id"], row["sku_id"]) for row in cursor.fetchall()}
    if not unique.issubset(available):
        raise HTTPException(status_code=400, detail="selected_skus_not_in_job")

    config["subjects"] = sorted({item["subject_key"] for item in selected})
    config["selected_skus"] = selected
    return dispatch_job(
        connection, config, _digest(config), source="recollect", parent_job_id=job_id,
    )


def register_price_agent_console_routes(app, *, connector, require_capability, audit_writer):
    @app.get("/api/price-agent/overview", dependencies=[Depends(require_capability)])
    def overview():
        with connector() as conn:
            agents = list_agents(conn)
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT `status`, COUNT(*) AS `amount` FROM `pd_price_agent_job` "
                    "GROUP BY `status` ORDER BY `status`"
                )
                job_counts = {row["status"]: row["amount"] for row in cursor.fetchall()}
                cursor.execute("SELECT COUNT(*) AS `amount`, MAX(`captured_at`) AS `latest` FROM `fact_maochao_price_snapshot`")
                snapshots = dict(cursor.fetchone())
                cursor.execute("SELECT COUNT(*) AS `amount`, MAX(`created_at`) AS `latest` FROM `pd_price_agent_artifact`")
                artifacts = dict(cursor.fetchone())
                cursor.execute(
                    "SELECT * FROM `pd_price_agent_job` WHERE `status` IN "
                    "('queued','running','waiting_login') ORDER BY `created_at` DESC LIMIT 1"
                )
                active = cursor.fetchone()
        config, _ = build_config_source().get_config()
        return {
            "agents": agents, "job_counts": job_counts,
            "active_job": _job_out(active) if active else None,
            "snapshots": snapshots, "artifacts": artifacts,
            "notifications_enabled": bool((config.get("notify") or {}).get("enabled")),
        }

    @app.get("/api/price-agent/agents", dependencies=[Depends(require_capability)])
    def agents():
        with connector() as conn:
            return {"items": list_agents(conn)}

    @app.get("/api/price-agent/jobs", dependencies=[Depends(require_capability)])
    def jobs(status=None, page=1, page_size=20):
        if status and status not in JOB_STATUSES:
            raise HTTPException(status_code=400, detail="status_invalid")
        page, page_size = _page(page), _page_size(page_size)
        where = "WHERE `status` = %s" if status else ""
        params = (status,) if status else ()
        with connector() as conn:
            with conn.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) AS `amount` FROM `pd_price_agent_job` {where}", params)
                total = cursor.fetchone()["amount"]
                cursor.execute(
                    f"SELECT * FROM `pd_price_agent_job` {where} "
                    "ORDER BY `created_at` DESC LIMIT %s OFFSET %s",
                    (*params, page_size, (page - 1) * page_size),
                )
                items = [_job_out(row) for row in cursor.fetchall()]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    @app.get("/api/price-agent/jobs/{job_id}", dependencies=[Depends(require_capability)])
    def job_detail(job_id: str):
        with connector() as conn:
            return {"job": _job_out(_fetch_job(conn, job_id)), "artifacts": _artifacts(conn, job_id)}

    @app.get("/api/price-agent/results", dependencies=[Depends(require_capability)])
    def results(job_id=None, subject_key=None, item_id=None, sku_id=None, page=1, page_size=20):
        page, page_size = _page(page), _page_size(page_size)
        filters, params = [], []
        for column, value in (("job_id", job_id), ("subject_key", subject_key), ("item_id", item_id), ("sku_id", sku_id)):
            if value:
                filters.append(f"`{column}` = %s")
                params.append(_text(value, column, max_length=128))
        where = "WHERE " + " AND ".join(filters) if filters else ""
        with connector() as conn:
            with conn.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) AS `amount` FROM `fact_maochao_price_snapshot` {where}", tuple(params))
                total = cursor.fetchone()["amount"]
                cursor.execute(
                    "SELECT * FROM `fact_maochao_price_snapshot` " + where +
                    " ORDER BY `captured_at` DESC, `job_id`, `row_index` LIMIT %s OFFSET %s",
                    (*params, page_size, (page - 1) * page_size),
                )
                items = [dict(row) for row in cursor.fetchall()]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def _write(conn, viewer, action, job_id, result):
        if result.get("status") == "active":
            raise HTTPException(status_code=409, detail={"code": "active", "job": result.get("job")})
        actor = getattr(viewer, "userid", None) or getattr(viewer, "user_id", None) or "unknown"
        if audit_writer is not None:
            audit_writer(
                conn, actor, action, job_id,
                json.dumps({"new_job_id": result.get("job_id"), "parent_job_id": job_id}, ensure_ascii=False),
            )
        return result

    @app.post("/api/price-agent/jobs/{job_id}/retry")
    def retry(job_id: str, viewer=Depends(require_capability)):
        with connector() as conn:
            result = retry_job(conn, job_id)
            return _write(conn, viewer, "price_agent_retry", job_id, result)

    @app.post("/api/price-agent/jobs/{job_id}/recollect")
    async def recollect(job_id: str, request: Request, viewer=Depends(require_capability)):
        try:
            payload = await request.json()
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid_json") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid_json")
        with connector() as conn:
            try:
                result = recollect_job(conn, job_id, payload.get("selected_skus"))
            except PriceAgentProtocolError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.code) from None
            return _write(conn, viewer, "price_agent_recollect", job_id, result)
