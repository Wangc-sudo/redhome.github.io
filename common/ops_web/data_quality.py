"""Data-quality HTTP orchestration for ops-web.

The web layer only validates input and coordinates the existing quality
repository/runner.  It never publishes business facts and never accepts SQL or
executable rule code.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from common.public_data.cleaning import (
    RuleConfigurationError,
    rule_version as compute_rule_version,
)
from common.public_data.data_quality import (
    SUPPORTED_DATASETS,
    DataQualityConflict,
    DataQualityError,
    DataQualityNotFound,
    DataQualityRepository,
    DataQualityRunner,
    DatabaseQualitySource,
)


_COUNT_FIELDS = (
    "records_read", "records_passed", "records_quarantined", "records_written",
    "duplicates", "unmapped", "issue_count",
)


def _dataset(value):
    if value not in SUPPORTED_DATASETS:
        raise ValueError("unsupported dataset")
    return value


def _validate_window(start, end):
    if (start is None) != (end is None):
        raise ValueError("business window must have both boundaries")
    if start is not None and start > end:
        raise ValueError("invalid business window")


class RuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(min_length=1, max_length=64)
    rule_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    rule_type: str = Field(min_length=1, max_length=16)
    field_name: str | None = Field(
        default=None, max_length=128, pattern=r"^[a-z][a-z0-9_]*$",
    )
    params: dict = Field(default_factory=dict)
    severity: str = Field(default="error", max_length=16)
    enabled: bool = True

    _valid_dataset = field_validator("dataset")(_dataset)


class RuleDefinition(RuleCreate):
    id: int | None = Field(default=None, ge=1)
    version: int = Field(default=1, ge=1)


class RuleChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_type: str | None = Field(default=None, max_length=16)
    field_name: str | None = Field(
        default=None, max_length=128, pattern=r"^[a-z][a-z0-9_]*$",
    )
    params: dict | None = None
    severity: str | None = Field(default=None, max_length=16)
    enabled: bool | None = None

    @field_validator("rule_type", "severity", "enabled")
    @classmethod
    def change_cannot_be_null(cls, value):
        if value is None:
            raise ValueError("rule change cannot be null")
        return value

    @model_validator(mode="after")
    def at_least_one_change(self):
        if not self.model_fields_set:
            raise ValueError("at least one rule change is required")
        return self


class _WindowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(min_length=1, max_length=64)
    window_start: date | None = None
    window_end: date | None = None

    _valid_dataset = field_validator("dataset")(_dataset)

    @model_validator(mode="after")
    def valid_window(self):
        _validate_window(self.window_start, self.window_end)
        return self


class PreviewRequest(_WindowModel):
    rule: RuleDefinition | None = None


class ResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value not in {"confirmed", "ignored"}:
            raise ValueError("invalid resolution status")
        return value

    @field_validator("reason")
    @classmethod
    def non_blank_reason(cls, value):
        if not value.strip():
            raise ValueError("reason is required")
        return value.strip()


class OverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(min_length=1, max_length=64)
    source_record_id: str = Field(min_length=1, max_length=320)
    field_name: str = Field(max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    value: str | None = Field(default=None, max_length=4096)
    reason: str = Field(min_length=1, max_length=500)

    _valid_dataset = field_validator("dataset")(_dataset)

    @field_validator("source_record_id")
    @classmethod
    def non_blank_source(cls, value):
        if not value.strip():
            raise ValueError("source_record_id is required")
        return value

    @field_validator("reason")
    @classmethod
    def non_blank_reason(cls, value):
        if not value.strip():
            raise ValueError("reason is required")
        return value.strip()


class RerunRequest(_WindowModel):
    rule_version: str | None = Field(default=None, min_length=1, max_length=64)
    dry_run: bool = True
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def bounded_window(self):
        if self.window_start is None:
            raise ValueError("rerun requires a bounded business window")
        return self


def _default_source_connector(settings, connect):
    @contextmanager
    def connector():
        dingtalk = connect(settings.dingtalk_database)
        try:
            wdt = connect(settings.wdt_database)
        except Exception:
            dingtalk.close()
            raise
        try:
            yield DatabaseQualitySource(dingtalk, wdt)
        finally:
            try:
                wdt.close()
            finally:
                dingtalk.close()

    return connector


async def _model_body(request, model):
    try:
        payload = await request.json()
    except Exception:
        payload = None
    try:
        if not isinstance(payload, dict):
            raise DataQualityError("request body must be an object")
        return model.model_validate(payload)
    except (ValidationError, RuleConfigurationError, DataQualityError):
        raise HTTPException(status_code=400, detail="bad_request") from None


def _positive_int(value, name, *, maximum=None, default=None):
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad_request") from None
    if isinstance(value, bool) or parsed < 1 or (maximum is not None and parsed > maximum):
        raise HTTPException(status_code=400, detail="bad_request")
    return parsed


def _run_out(row):
    result = dict(row)
    diff = result.get("diff")
    raw_diff = result.pop("diff_json", None)
    if diff is None and raw_diff not in (None, ""):
        try:
            diff = json.loads(raw_diff) if isinstance(raw_diff, str) else raw_diff
        except (TypeError, ValueError):
            diff = {"raw": str(raw_diff)[:1024]}
    result["diff"] = diff
    result["dry_run"] = bool(result.get("dry_run"))
    result["counts"] = {
        key: int(result.get(key) or 0) for key in _COUNT_FIELDS
    }
    return result


def _preview_rules(repository, payload):
    rules = repository.list_rules(payload.dataset)
    if payload.rule is None:
        return rules
    candidate = payload.rule.model_dump()
    if candidate["dataset"] != payload.dataset:
        raise DataQualityError("rule dataset mismatch")
    replaced = False
    for index, current in enumerate(rules):
        same_id = candidate["id"] is not None and current.get("id") == candidate["id"]
        same_code = candidate["id"] is None and current.get("rule_code") == candidate["rule_code"]
        if same_id or same_code:
            rules[index] = candidate
            replaced = True
            break
    if candidate["id"] is not None and not replaced:
        raise DataQualityNotFound("rule not found")
    if not replaced:
        rules.append(candidate)
    return rules


def _current_rule_version(repository, dataset):
    return compute_rule_version(repository.list_rules(dataset, enabled_only=True))


def _resolve_rerun_rule_version(repository, dataset, requested_version):
    """Allow only a fresh snapshot of the current enabled rules.

    Stored snapshots are addressed by run_id; the repository has no historical
    dataset/rule-version lookup or snapshot-cloning contract.
    """
    current = _current_rule_version(repository, dataset)
    if requested_version is not None and requested_version != current:
        raise DataQualityConflict("rule_version is not the current enabled version")
    return current


def register_data_quality_routes(
    app, *, settings, db_connector, require_viewer, require_write,
    source_connector=None, connect, logger, error_detail,
):
    """Register the quality API before the SPA catch-all route."""
    source_connector = source_connector or _default_source_connector(settings, connect)

    def respond(action, callback):
        try:
            result = callback()
        except HTTPException:
            raise
        except DataQualityNotFound:
            raise HTTPException(status_code=404, detail=error_detail.NOT_FOUND) from None
        except DataQualityConflict:
            raise HTTPException(status_code=409, detail=error_detail.CONFLICT) from None
        except (ValidationError, RuleConfigurationError, DataQualityError):
            raise HTTPException(status_code=400, detail=error_detail.BAD_REQUEST) from None
        except Exception as exc:
            logger.warning("ops-web data-quality %s failed: %s", action, type(exc).__name__)
            raise HTTPException(status_code=503, detail=error_detail.UNAVAILABLE) from None
        return JSONResponse(jsonable_encoder(result))

    @app.get("/api/data-quality/overview")
    def quality_overview(request: Request):
        require_viewer(request)

        def read():
            with db_connector() as connection:
                result = DataQualityRepository(connection).overview()
            items = [_run_out(row) for row in result["items"]]
            return {**result, "items": items, "count": len(items), "status": "ok"}

        return respond("overview", read)

    @app.get("/api/data-quality/rules")
    def quality_rules(request: Request, dataset: str = ""):
        require_viewer(request)

        def read():
            selected = dataset or None
            with db_connector() as connection:
                repository = DataQualityRepository(connection)
                items = repository.list_rules(selected)
                version = compute_rule_version(
                    rule for rule in items if rule.get("enabled", 1)
                )
            return {
                "status": "ok", "dataset": selected or "*", "items": items,
                "count": len(items), "rule_version": version,
            }

        return respond("rule list", read)

    @app.post("/api/data-quality/rules")
    async def quality_rule_create(request: Request):
        viewer = require_write(request)
        payload = await _model_body(request, RuleCreate)

        def write():
            with db_connector() as connection:
                repository = DataQualityRepository(connection)
                result = repository.create_rule(payload.model_dump(), actor=viewer.userid)
                version = _current_rule_version(repository, payload.dataset)
            return {
                **result, "status": "ok", "dataset": payload.dataset,
                "rule_version": version, "count": 1,
            }

        return respond("rule create", write)

    @app.put("/api/data-quality/rules/{rule_id}")
    async def quality_rule_update(request: Request, rule_id: str):
        viewer = require_write(request)
        payload = await _model_body(request, RuleChanges)
        normalized_id = _positive_int(rule_id, "rule_id")

        def write():
            with db_connector() as connection:
                repository = DataQualityRepository(connection)
                result = repository.update_rule(
                    normalized_id, payload.model_dump(exclude_unset=True), actor=viewer.userid,
                )
                version = _current_rule_version(repository, result["rule"]["dataset"])
            return {
                **result, "status": "ok", "dataset": result["rule"]["dataset"],
                "rule_version": version, "count": 1,
            }

        return respond("rule update", write)

    @app.post("/api/data-quality/preview")
    async def quality_preview(request: Request):
        viewer = require_viewer(request)
        payload = await _model_body(request, PreviewRequest)

        def run_preview():
            with db_connector() as connection:
                repository = DataQualityRepository(connection)
                rules = _preview_rules(repository, payload)
                with source_connector() as source:
                    result = DataQualityRunner(repository, source).run(
                        payload.dataset, mode="preview", persist=False, rules=rules,
                        window_start=payload.window_start, window_end=payload.window_end,
                        requested_by=viewer.userid,
                    )
            result.pop("run_id", None)
            return {**result, "mode": "preview", "dry_run": True}

        return respond("preview", run_preview)

    @app.get("/api/data-quality/datasets/{dataset}/issues")
    def quality_issues(
        request: Request, dataset: str, status: str = "", rule_code: str = "",
        cursor: str = "", limit: str = "50",
    ):
        require_viewer(request)
        cursor_id = _positive_int(cursor, "cursor")
        page_size = _positive_int(limit, "limit", maximum=200, default=50)

        def read():
            with db_connector() as connection:
                result = DataQualityRepository(connection).list_issues(
                    dataset, status=status or None, rule_code=rule_code or None,
                    cursor_id=cursor_id, limit=page_size,
                )
            return {**result, "status": "ok", "count": len(result["items"])}

        return respond("issue list", read)

    @app.post("/api/data-quality/issues/{issue_id}/resolution")
    async def quality_issue_resolution(request: Request, issue_id: str):
        viewer = require_write(request)
        payload = await _model_body(request, ResolutionRequest)
        normalized_id = _positive_int(issue_id, "issue_id")

        def write():
            with db_connector() as connection:
                result = DataQualityRepository(connection).resolve_issue(
                    normalized_id, status=payload.status, reason=payload.reason,
                    actor=viewer.userid,
                )
            return {**result, "count": 1}

        return respond("issue resolution", write)

    @app.post("/api/data-quality/overrides")
    async def quality_override(request: Request):
        viewer = require_write(request)
        payload = await _model_body(request, OverrideRequest)

        def write():
            with db_connector() as connection:
                result = DataQualityRepository(connection).put_override(
                    **payload.model_dump(), actor=viewer.userid,
                )
            return {**result, "status": "ok", "count": 1}

        return respond("override", write)

    @app.post("/api/data-quality/reruns")
    async def quality_rerun_create(request: Request):
        viewer = require_write(request)
        payload = await _model_body(request, RerunRequest)

        def write():
            with db_connector() as connection:
                repository = DataQualityRepository(connection)
                version = _resolve_rerun_rule_version(
                    repository, payload.dataset, payload.rule_version,
                )
                run_id = repository.create_run(
                    dataset=payload.dataset, rule_version=version, mode="rerun",
                    requested_by=viewer.userid, reason=payload.reason,
                    window_start=payload.window_start, window_end=payload.window_end,
                    dry_run=payload.dry_run, status="pending",
                )
                # create_run freezes and versions the snapshot itself.  Return
                # that stored version rather than treating the input argument as
                # evidence of what was actually frozen.
                version = repository.get_run_snapshot(run_id)["rule_version"]
            return {
                "run_id": run_id, "dataset": payload.dataset, "rule_version": version,
                "mode": "rerun", "dry_run": payload.dry_run, "status": "pending",
                "window_start": payload.window_start, "window_end": payload.window_end,
                "counts": {key: 0 for key in _COUNT_FIELDS}, "diff": None,
            }

        return respond("rerun create", write)

    @app.get("/api/data-quality/reruns")
    def quality_reruns(request: Request, dataset: str = "", cursor: str = "", limit: str = "50"):
        require_viewer(request)
        cursor_id = _positive_int(cursor, "cursor")
        page_size = _positive_int(limit, "limit", maximum=200, default=50)

        def read():
            with db_connector() as connection:
                result = DataQualityRepository(connection).list_runs(
                    dataset=dataset or None, mode="rerun", cursor_id=cursor_id, limit=page_size,
                )
            items = [_run_out(row) for row in result["items"]]
            return {
                **result, "items": items, "count": len(items), "status": "ok",
                "dataset": dataset or "*", "mode": "rerun",
            }

        return respond("rerun list", read)

    @app.get("/api/data-quality/reruns/{run_id}")
    def quality_rerun(request: Request, run_id: str):
        require_viewer(request)
        if not run_id or len(run_id) > 64:
            raise HTTPException(status_code=400, detail=error_detail.BAD_REQUEST)

        def read():
            with db_connector() as connection:
                row = DataQualityRepository(connection).get_run(run_id)
            if row.get("mode") != "rerun":
                raise DataQualityNotFound("rerun not found")
            return _run_out(row)

        return respond("rerun detail", read)
