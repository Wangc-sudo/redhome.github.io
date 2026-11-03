"""猫超价格 agent 任务状态机、幂等入库与 outbox 后处理。

所有多写操作与 outbox 入队在同一 MySQL 事务中完成。租约明文只返回给 claim
调用方，数据库保存 SHA-256；超时后的迟到回传不能覆盖新任务。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import pymysql

from common.public_data.db import transaction
from common.public_data.outbox_repository import OutboxRepository

PIPELINE = "maochao"
ACTIVE_STATUSES = ("queued", "running", "waiting_login")
TERMINAL_STATUSES = ("completed", "failed", "timed_out", "login_expired", "cancelled")
ARTIFACT_TYPES = frozenset({"page_screenshot", "row_evidence", "json", "csv"})
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_ROWS = 5000
_MAX_ARTIFACTS = 1000


class PriceAgentProtocolError(ValueError):
    def __init__(self, code, *, status_code=400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash_token(token):
    if not isinstance(token, str) or not token:
        raise PriceAgentProtocolError("lease_token_required", status_code=401)
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _sha256_value(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value, field, *, max_length, required=False, default=""):
    if value is None:
        value = default
    if not isinstance(value, (str, int)):
        raise PriceAgentProtocolError(f"{field}_invalid")
    result = str(value).strip()
    if required and not result:
        raise PriceAgentProtocolError(f"{field}_required")
    if len(result) > max_length or "\x00" in result:
        raise PriceAgentProtocolError(f"{field}_invalid")
    return result


def _integer(value, field, *, minimum=0, maximum=1000000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise PriceAgentProtocolError(f"{field}_invalid")
    return value


def _price(value, field):
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PriceAgentProtocolError(f"{field}_invalid") from exc
    if not result.is_finite() or abs(result) >= Decimal("10000000000000000"):
        raise PriceAgentProtocolError(f"{field}_invalid")
    try:
        return format(result.quantize(Decimal("0.0001")), "f")
    except InvalidOperation as exc:
        raise PriceAgentProtocolError(f"{field}_invalid") from exc


def _date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise PriceAgentProtocolError("business_date_invalid") from exc


def _captured_at(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise PriceAgentProtocolError("captured_at_invalid") from exc
    if parsed.tzinfo is None:
        raise PriceAgentProtocolError("captured_at_timezone_required")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def normalize_snapshot_row(row, config):
    if not isinstance(row, dict):
        raise PriceAgentProtocolError("row_invalid")
    subject = _text(row.get("subject_key"), "subject_key", max_length=128, required=True)
    if subject not in config["subjects"]:
        raise PriceAgentProtocolError("subject_out_of_scope")
    page_number = _integer(row.get("page_number"), "page_number", minimum=1, maximum=10000)
    row_index = _integer(row.get("row_index"), "row_index", minimum=1, maximum=1000)
    normalized = {
        "business_date": _date(row.get("business_date")),
        "subject_key": subject,
        "dept_level2": _text(row.get("dept_level2"), "dept_level2", max_length=128),
        "item_id": _text(row.get("item_id"), "item_id", max_length=64),
        "sku_id": _text(row.get("sku_id", row.get("sku")), "sku_id", max_length=64),
        "product": _text(row.get("product"), "product", max_length=512),
        "product_spec": _text(row.get("product_spec"), "product_spec", max_length=255),
        "market_source": _text(row.get("market_source"), "market_source", max_length=255),
        "market_product": _text(row.get("market_product"), "market_product", max_length=512),
        "market_spec": _text(row.get("market_spec"), "market_spec", max_length=255),
        "market_price": _price(row.get("market_price"), "market_price"),
        "current_price": _price(row.get("current_price"), "current_price"),
        "suggested_price": _price(row.get("suggested_price"), "suggested_price"),
        "price_gap": _price(row.get("price_gap"), "price_gap"),
        "page_status": _text(row.get("page_status", row.get("status")), "page_status", max_length=64),
        "page_number": page_number,
        "row_index": row_index,
        "captured_at": _captured_at(row.get("captured_at")),
    }
    identity = {key: normalized[key] for key in (
        "subject_key", "item_id", "sku_id", "page_number", "row_index"
    )}
    normalized["row_uid"] = _sha256_value(identity)
    normalized["payload_hash"] = _sha256_value(normalized)
    return normalized


def normalize_artifact(artifact, config, job_id):
    if not isinstance(artifact, dict):
        raise PriceAgentProtocolError("artifact_invalid")
    artifact_type = _text(
        artifact.get("artifact_type"), "artifact_type", max_length=32, required=True
    )
    if artifact_type not in ARTIFACT_TYPES:
        raise PriceAgentProtocolError("artifact_type_invalid")
    bucket = _text(artifact.get("bucket"), "bucket", max_length=128, required=True)
    object_key = _text(
        artifact.get("object_key"), "object_key", max_length=768, required=True
    )
    prefix = config["artifacts"]["prefix"]
    if bucket != config["artifacts"]["bucket"] or not object_key.startswith(prefix + "/"):
        raise PriceAgentProtocolError("artifact_scope_invalid")
    if f"/{job_id}/" not in object_key or ".." in object_key.split("/"):
        raise PriceAgentProtocolError("artifact_job_scope_invalid")
    sha256 = _text(artifact.get("sha256"), "sha256", max_length=64, required=True).lower()
    if not _SHA256_RE.match(sha256):
        raise PriceAgentProtocolError("artifact_sha256_invalid")
    if artifact.get("upload_status") != "uploaded":
        raise PriceAgentProtocolError("artifact_not_uploaded")
    return {
        "subject_key": _text(artifact.get("subject_key"), "subject_key", max_length=128),
        "artifact_type": artifact_type,
        "bucket": bucket,
        "object_key": object_key,
        "sha256": sha256,
        "size_bytes": _integer(
            artifact.get("size_bytes"), "size_bytes", minimum=0, maximum=1024 ** 4
        ),
        "upload_status": "uploaded",
    }


def validate_completion(payload, config, job_id):
    if payload.get("complete") is not True:
        raise PriceAgentProtocolError("completion_not_complete")
    if payload.get("restored_subject") != config["restore_subject"]:
        raise PriceAgentProtocolError("subject_not_restored")
    summaries = payload.get("subjects")
    if not isinstance(summaries, list):
        raise PriceAgentProtocolError("subjects_invalid")
    by_subject = {}
    for summary in summaries:
        if not isinstance(summary, dict):
            raise PriceAgentProtocolError("subject_summary_invalid")
        key = _text(summary.get("subject_key"), "subject_key", max_length=128, required=True)
        if key in by_subject:
            raise PriceAgentProtocolError("subject_summary_duplicate")
        row_count = _integer(summary.get("row_count"), "row_count", maximum=_MAX_ROWS)
        page_size_100 = summary.get("page_size_100")
        empty_without_selector = (
            row_count == 0 and summary.get("empty") is True and page_size_100 is False
        )
        if summary.get("status") != "completed" or (
            page_size_100 is not True and not empty_without_selector
        ):
            raise PriceAgentProtocolError("subject_incomplete")
        by_subject[key] = {
            "subject_key": key,
            "dept_level2": _text(summary.get("dept_level2"), "dept_level2", max_length=128),
            "status": "completed",
            "row_count": row_count,
            "pages_captured": _integer(
                summary.get("pages_captured"), "pages_captured", maximum=10000
            ),
            "page_size_100": page_size_100,
            "empty": not row_count,
        }
    if set(by_subject) != set(config["subjects"]):
        raise PriceAgentProtocolError("subject_set_mismatch")

    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) > _MAX_ROWS:
        raise PriceAgentProtocolError("rows_invalid")
    rows = [normalize_snapshot_row(row, config) for row in raw_rows]
    row_keys = {(row["subject_key"], row["row_uid"]) for row in rows}
    if len(row_keys) != len(rows):
        raise PriceAgentProtocolError("row_uid_duplicate")
    counts = {key: 0 for key in config["subjects"]}
    for row in rows:
        counts[row["subject_key"]] += 1
    for key, summary in by_subject.items():
        if summary["row_count"] != counts[key]:
            raise PriceAgentProtocolError("subject_row_count_mismatch")

    raw_artifacts = payload.get("artifacts")
    if not isinstance(raw_artifacts, list) or len(raw_artifacts) > _MAX_ARTIFACTS:
        raise PriceAgentProtocolError("artifacts_invalid")
    artifacts = [normalize_artifact(item, config, job_id) for item in raw_artifacts]
    keys = {item["object_key"] for item in artifacts}
    if len(keys) != len(artifacts):
        raise PriceAgentProtocolError("artifact_duplicate")
    return rows, artifacts, [by_subject[key] for key in config["subjects"]]


def result_fingerprint(rows):
    """只比较业务值；采集时间、业务日期、页码和文件信息不触发播报。"""
    fields = (
        "subject_key", "item_id", "sku_id", "product", "product_spec",
        "market_source", "market_product", "market_spec", "market_price",
        "current_price", "suggested_price", "price_gap", "page_status",
    )
    values = sorted(
        _canonical({field: row[field] for field in fields})
        for row in rows
    )
    return _sha256_value(values)


def dispatch_job(connection, config, digest, *, source, run_history_id=None, now=None):
    if source not in ("cron", "run_once"):
        raise PriceAgentProtocolError("source_invalid")
    if not config["enabled"]:
        return {"status": "disabled"}
    import uuid
    job_id = str(uuid.uuid4())
    stamp = now or utc_now()
    snapshot = _canonical(config)
    try:
        with transaction(connection):
            with connection.cursor() as cursor:
                queue_expires = stamp + timedelta(seconds=config["job_lease_seconds"])
                cursor.execute(
                    "INSERT INTO `pd_price_agent_job` (`job_id`, `pipeline`, `source`, "
                    "`status`, `lease_expires_at`, `config_hash`, `config_snapshot`, "
                    "`run_history_id`, `created_at`, `updated_at`) VALUES "
                    "(%s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s)",
                    (job_id, PIPELINE, source, queue_expires, digest, snapshot,
                     run_history_id, stamp, stamp),
                )
    except pymysql.err.IntegrityError as exc:
        if not exc.args or exc.args[0] != 1062:
            raise
        return {"status": "active", "job": fetch_active_job(connection)}
    return {"status": "queued", "job_id": job_id, "config_hash": digest}


def fetch_active_job(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT `job_id`, `status`, `agent_id`, `created_at`, `heartbeat_at` "
            "FROM `pd_price_agent_job` WHERE `active_pipeline` = %s",
            (PIPELINE,),
        )
        row = cursor.fetchone()
    return dict(row) if row else None


def _job_for_update(connection, job_id):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM `pd_price_agent_job` WHERE `job_id` = %s FOR UPDATE",
            (job_id,),
        )
        row = cursor.fetchone()
    return dict(row) if row else None


def _job_config(job):
    try:
        return json.loads(job["config_snapshot"])
    except (TypeError, ValueError) as exc:
        raise PriceAgentProtocolError("job_config_invalid", status_code=500) from exc


def _verify_lease(job, payload):
    expected = job.get("lease_token_hash")
    actual = _hash_token(payload.get("lease_token"))
    if not expected or not hmac.compare_digest(expected, actual):
        raise PriceAgentProtocolError("lease_invalid", status_code=409)
    agent_id = payload.get("agent_id")
    if not agent_id or agent_id != job.get("agent_id"):
        raise PriceAgentProtocolError("agent_mismatch", status_code=409)


def claim_job(connection, agent_id, config, digest, *, now=None):
    agent_id = _text(agent_id, "agent_id", max_length=64, required=True)
    if not config["enabled"]:
        return {"status": "disabled"}
    stamp = now or utc_now()
    with transaction(connection):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT `job_id` FROM `pd_price_agent_job` WHERE `status` = 'queued' "
                "AND `pipeline` = %s AND (`lease_expires_at` IS NULL OR `lease_expires_at` >= %s) "
                "ORDER BY `priority`, `created_at` LIMIT 1 FOR UPDATE SKIP LOCKED",
                (PIPELINE, stamp),
            )
            row = cursor.fetchone()
        if row is None:
            return {"status": "empty"}
        job = _job_for_update(connection, row["job_id"])
        job_config = _job_config(job)
        lease_token = secrets.token_urlsafe(32)
        lease_expires = stamp + timedelta(seconds=job_config["job_lease_seconds"])
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE `pd_price_agent_job` SET `status` = 'running', `agent_id` = %s, "
                "`lease_token_hash` = %s, `lease_expires_at` = %s, `heartbeat_at` = %s, "
                "`auth_state` = 'unknown', `auth_checked_at` = %s, `started_at` = %s, "
                "`updated_at` = %s WHERE `job_id` = %s AND `status` = 'queued'",
                (agent_id, _hash_token(lease_token), lease_expires, stamp, stamp,
                 stamp, stamp, job["job_id"]),
            )
            if cursor.rowcount != 1:
                raise PriceAgentProtocolError("job_claim_conflict", status_code=409)
        return {
            "status": "claimed",
            "job_id": job["job_id"],
            "lease_token": lease_token,
            "lease_expires_at": lease_expires.isoformat() + "Z",
            "config": job_config,
            "config_hash": job["config_hash"],
        }


def _enqueue_alert(connection, config, job_id, title, body, stamp, suffix):
    notify = config.get("notify") or {}
    if not notify.get("enabled") or not notify.get("on_failure"):
        return False
    return OutboxRepository(connection).enqueue(
        region=notify.get("region", "ops"),
        kind="price_alert",
        business_date=(stamp + timedelta(hours=8)).date(),
        title=title,
        body_md=body,
        created_at=stamp,
        dedupe_suffix=suffix,
    )


def heartbeat_job(connection, job_id, payload, *, now=None):
    stamp = now or utc_now()
    with transaction(connection):
        job = _job_for_update(connection, job_id)
        if job is None:
            raise PriceAgentProtocolError("job_not_found", status_code=404)
        if job["status"] in TERMINAL_STATUSES:
            raise PriceAgentProtocolError("job_terminal", status_code=409)
        _verify_lease(job, payload)
        config = _job_config(job)
        auth_state = payload.get("auth_state", "unknown")
        if auth_state not in ("unknown", "valid", "login_required"):
            raise PriceAgentProtocolError("auth_state_invalid")
        session_version = _integer(
            payload.get("session_version", job["session_version"]),
            "session_version", maximum=1000000,
        )
        current_subject = _text(
            payload.get("current_subject"), "current_subject", max_length=128
        ) or None
        current_page = payload.get("current_page")
        if current_page is not None:
            current_page = _integer(current_page, "current_page", minimum=1, maximum=10000)
        action = "continue"
        status = job["status"]
        login_since = job.get("login_required_since")
        if auth_state == "login_required":
            status = "waiting_login"
            login_since = login_since or stamp
            lease_expires = login_since + timedelta(seconds=config["login_wait_seconds"])
            action = "wait_login"
        else:
            if job["status"] == "waiting_login" and auth_state == "valid":
                action = "restart_job"
            status = "running"
            login_since = None
            lease_expires = stamp + timedelta(seconds=config["job_lease_seconds"])
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE `pd_price_agent_job` SET `status` = %s, `lease_expires_at` = %s, "
                "`heartbeat_at` = %s, `current_subject` = %s, `current_page` = %s, "
                "`auth_state` = %s, `auth_checked_at` = %s, `login_required_since` = %s, "
                "`session_version` = %s, `updated_at` = %s WHERE `job_id` = %s",
                (status, lease_expires, stamp, current_subject, current_page, auth_state,
                 stamp, login_since, session_version, stamp, job_id),
            )
        if status == "waiting_login" and job["status"] != "waiting_login":
            body = (
                "### 猫超采集需要人工登录\n\n"
                f"- agent：`{job['agent_id']}`\n- job：`{job_id}`\n"
                "请到 Windows 可见 Chrome 完成登录和滑块；agent 会自动恢复。\n"
                "原始 Cookie 不会上传或写入告警。"
            )
            _enqueue_alert(
                connection, config, job_id, "猫超登录态失效", body, stamp,
                f"{job_id}:login_required:{session_version}",
            )
        return {"status": status, "action": action, "lease_expires_at": lease_expires.isoformat() + "Z"}


def _result_message(job_id, rows, limit):
    lines = [
        "### 猫超价格竞争力结果变化", "", f"- job：`{job_id}`", f"- 本轮：{len(rows)} 行", "",
        "| 商品 | 当前价 | 建议价 | 差额 | 状态 | 二级部门 |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in rows[:limit]:
        product = (row["product"] or row["item_id"] or row["row_uid"][:8])
        product = " ".join(product.replace("|", "/").split())
        dept = " ".join((row.get("dept_level2") or "—").replace("|", "/").split())
        lines.append(
            f"| {product[:50]} | {row['current_price'] or '—'} | "
            f"{row['suggested_price'] or '—'} | {row['price_gap'] or '—'} | "
            f"{row['page_status'] or '—'} | {dept} |"
        )
    if len(rows) > limit:
        lines.append(f"\n其余 {len(rows) - limit} 行按 job_id 查询。")
    return "\n".join(lines)


def _enqueue_result_change(connection, config, job, rows, fingerprint, stamp):
    notify = config.get("notify") or {}
    if not notify.get("enabled") or not notify.get("on_result_change"):
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT `result_fingerprint` FROM `pd_price_agent_job` "
            "WHERE `pipeline` = %s AND `status` = 'completed' AND `job_id` <> %s "
            "ORDER BY `finished_at` DESC LIMIT 1",
            (PIPELINE, job["job_id"]),
        )
        previous = cursor.fetchone()
    # 首次只建立基线，避免把全量商品当成变化播报。
    if previous is None or previous.get("result_fingerprint") == fingerprint:
        return False
    return OutboxRepository(connection).enqueue(
        region=notify.get("region", "ops"),
        kind="price_change",
        business_date=(stamp + timedelta(hours=8)).date(),
        title="猫超价格竞争力变化",
        body_md=_result_message(job["job_id"], rows, notify.get("full_rows_per_message", 10)),
        created_at=stamp,
        dedupe_suffix=f"{job['job_id']}:result",
    )


def complete_job(connection, job_id, payload, *, now=None):
    stamp = now or utc_now()
    completion_hash = _sha256_value(payload)
    with transaction(connection):
        job = _job_for_update(connection, job_id)
        if job is None:
            raise PriceAgentProtocolError("job_not_found", status_code=404)
        if job["status"] == "completed":
            if job.get("completion_hash") != completion_hash:
                raise PriceAgentProtocolError("completion_payload_mismatch", status_code=409)
            return {
                "status": "completed", "job_id": job_id,
                "row_count": job["row_count"], "idempotent": True,
            }
        if job["status"] != "running":
            raise PriceAgentProtocolError("job_not_completable", status_code=409)
        _verify_lease(job, payload)
        config = _job_config(job)
        rows, artifacts, summaries = validate_completion(payload, config, job_id)
        fingerprint = result_fingerprint(rows)
        notified = _enqueue_result_change(connection, config, job, rows, fingerprint, stamp)
        with connection.cursor() as cursor:
            for row in rows:
                cursor.execute(
                    "INSERT INTO `fact_maochao_price_snapshot` (`job_id`, `row_uid`, "
                    "`business_date`, `subject_key`, `dept_level2`, `item_id`, `sku_id`, `product`, "
                    "`product_spec`, `market_source`, `market_product`, `market_spec`, "
                    "`market_price`, `current_price`, `suggested_price`, `price_gap`, "
                    "`page_status`, `page_number`, `row_index`, `captured_at`, `payload_hash`) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s, %s, %s, %s, %s)",
                    (job_id, row["row_uid"], row["business_date"], row["subject_key"],
                     row["dept_level2"], row["item_id"], row["sku_id"], row["product"], row["product_spec"],
                     row["market_source"], row["market_product"], row["market_spec"],
                     row["market_price"], row["current_price"], row["suggested_price"],
                     row["price_gap"], row["page_status"], row["page_number"],
                     row["row_index"], row["captured_at"], row["payload_hash"]),
                )
            for item in artifacts:
                cursor.execute(
                    "INSERT INTO `pd_price_agent_artifact` (`job_id`, `subject_key`, "
                    "`artifact_type`, `bucket`, `object_key`, `sha256`, `size_bytes`, "
                    "`upload_status`, `created_at`) VALUES (%s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s)",
                    (job_id, item["subject_key"], item["artifact_type"], item["bucket"],
                     item["object_key"], item["sha256"], item["size_bytes"],
                     item["upload_status"], stamp),
                )
            cursor.execute(
                "UPDATE `pd_price_agent_job` SET `status` = 'completed', "
                "`lease_token_hash` = NULL, `lease_expires_at` = NULL, `row_count` = %s, "
                "`subject_summary` = %s, `result_fingerprint` = %s, `completion_hash` = %s, "
                "`auth_state` = 'valid', `auth_checked_at` = %s, `finished_at` = %s, "
                "`updated_at` = %s WHERE `job_id` = %s AND `status` = 'running'",
                (len(rows), _canonical(summaries), fingerprint, completion_hash,
                 stamp, stamp, stamp, job_id),
            )
            if cursor.rowcount != 1:
                raise PriceAgentProtocolError("job_complete_conflict", status_code=409)
        return {
            "status": "completed", "job_id": job_id, "row_count": len(rows),
            "artifact_count": len(artifacts), "result_fingerprint": fingerprint,
            "notified": notified, "idempotent": False,
        }


def fail_job(connection, job_id, payload, *, now=None):
    stamp = now or utc_now()
    with transaction(connection):
        job = _job_for_update(connection, job_id)
        if job is None:
            raise PriceAgentProtocolError("job_not_found", status_code=404)
        if job["status"] in TERMINAL_STATUSES:
            return {"status": job["status"], "idempotent": True}
        _verify_lease(job, payload)
        config = _job_config(job)
        error_code = _text(payload.get("error_code"), "error_code", max_length=64, required=True)
        if not _ERROR_CODE_RE.match(error_code):
            raise PriceAgentProtocolError("error_code_invalid")
        detail = _text(payload.get("error_detail"), "error_detail", max_length=500)
        if detail and not re.fullmatch(r"[A-Za-z0-9_.:/ \-]{1,500}", detail):
            detail = ""
        status = "login_expired" if error_code == "login_expired" else "failed"
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE `pd_price_agent_job` SET `status` = %s, `lease_token_hash` = NULL, "
                "`lease_expires_at` = NULL, `error_code` = %s, `error_detail` = %s, "
                "`finished_at` = %s, `updated_at` = %s WHERE `job_id` = %s",
                (status, error_code, detail or None, stamp, stamp, job_id),
            )
        body = (
            "### 猫超价格采集失败\n\n"
            f"- agent：`{job['agent_id']}`\n- job：`{job_id}`\n"
            f"- 阶段：`{error_code}`\n\n原始 Cookie 与凭据未回传。"
        )
        notified = _enqueue_alert(
            connection, config, job_id, "猫超价格采集失败", body, stamp,
            f"{job_id}:{error_code}",
        )
        return {"status": status, "notified": notified}


def expire_jobs(connection, *, now=None):
    stamp = now or utc_now()
    expired = {"timed_out": 0, "login_expired": 0, "notified": 0}
    with transaction(connection):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT `job_id`, `status`, `config_snapshot`, `agent_id` "
                "FROM `pd_price_agent_job` WHERE `status` IN ('queued','running','waiting_login') "
                "AND `lease_expires_at` IS NOT NULL AND `lease_expires_at` < %s FOR UPDATE",
                (stamp,),
            )
            jobs = [dict(row) for row in cursor.fetchall()]
        for job in jobs:
            status = "login_expired" if job["status"] == "waiting_login" else "timed_out"
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE `pd_price_agent_job` SET `status` = %s, `error_code` = %s, "
                    "`lease_token_hash` = NULL, `lease_expires_at` = NULL, "
                    "`finished_at` = %s, `updated_at` = %s WHERE `job_id` = %s "
                    "AND `status` = %s",
                    (status, status, stamp, stamp, job["job_id"], job["status"]),
                )
                if cursor.rowcount != 1:
                    continue
            expired[status] += 1
            config = json.loads(job["config_snapshot"])
            body = (
                "### 猫超采集任务超时\n\n"
                f"- agent：`{job.get('agent_id') or '未分配'}`\n- job：`{job['job_id']}`\n"
                f"- 终态：`{status}`\n\n后续周期会创建新任务，旧任务迟到回传将被拒绝。"
            )
            if _enqueue_alert(
                connection, config, job["job_id"], "猫超采集任务超时", body, stamp,
                f"{job['job_id']}:{status}",
            ):
                expired["notified"] += 1
    return expired
