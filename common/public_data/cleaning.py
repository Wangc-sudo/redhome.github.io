"""Deterministic, database-independent cleaning rules for data-quality runs.

Rules are data, not executable code.  The engine never imports or evaluates
rule parameters and never opens a database connection.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Iterable


RULE_TYPES = frozenset({
    "required", "enum", "range", "regex", "unique", "mapping",
    "freshness", "reconcile", "schema",
})
SEVERITIES = frozenset({"critical", "error", "warning"})
ISSUE_STATUSES = frozenset({"open", "confirmed", "ignored", "resolved"})
_DATASET_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_RULE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_SENSITIVE_FIELD_RE = re.compile(
    r"phone|mobile|buyer|customer|identity|id_card|bank_card", re.I
)


class RuleConfigurationError(ValueError):
    """A rule is outside the whitelist or has invalid parameters."""


@dataclass(frozen=True)
class CleaningIssue:
    dataset: str
    source_record_id: str
    business_key: str
    rule_code: str
    rule_type: str
    field_name: str | None
    original_value: object
    cleaned_value: object
    severity: str
    reason: str

    def as_row(self, run_id: str) -> dict:
        return {
            "run_id": run_id,
            "dataset": self.dataset,
            "source_record_id": self.source_record_id,
            "business_key": self.business_key,
            "rule_code": self.rule_code,
            "rule_type": self.rule_type,
            "field_name": self.field_name,
            "original_value": mask_value(self.field_name, self.original_value),
            "cleaned_value": mask_value(self.field_name, self.cleaned_value),
            "severity": self.severity,
            "reason": self.reason,
        }


@dataclass
class CleaningResult:
    rows: list[dict] = field(default_factory=list)
    issues: list[CleaningIssue] = field(default_factory=list)
    records_read: int = 0
    records_passed: int = 0
    records_quarantined: int = 0
    duplicates: int = 0
    unmapped: int = 0

    @property
    def counts(self) -> dict:
        return {
            "records_read": self.records_read,
            "records_passed": self.records_passed,
            "records_quarantined": self.records_quarantined,
            "duplicates": self.duplicates,
            "unmapped": self.unmapped,
        }


def normalize_text(value):
    """Normalize matching text without conflating empty strings with zero."""
    if not isinstance(value, str):
        return value
    return _INVISIBLE_RE.sub("", value).strip()


def mask_value(field_name, value):
    if value is None:
        return None
    text = str(value)
    if field_name and _SENSITIVE_FIELD_RE.search(field_name):
        if len(text) <= 4:
            return "*" * len(text)
        return f"{text[:2]}{'*' * min(len(text) - 4, 12)}{text[-2:]}"
    # Never persist a whole payload through an issue value.
    return text[:1024]


def _params(rule) -> dict:
    params = rule.get("params", rule.get("params_json", {}))
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (TypeError, ValueError):
            raise RuleConfigurationError("params must be a JSON object") from None
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise RuleConfigurationError("params must be a JSON object")
    return params


def validate_rule(rule, *, partial=False):
    """Validate a white-listed rule; return a normalized independent copy."""
    if not isinstance(rule, dict):
        raise RuleConfigurationError("rule must be an object")
    normalized = dict(rule)
    if not partial or "dataset" in rule:
        dataset = str(rule.get("dataset") or "")
        if not _DATASET_RE.fullmatch(dataset):
            raise RuleConfigurationError("invalid dataset")
        normalized["dataset"] = dataset
    if not partial or "rule_code" in rule:
        code = str(rule.get("rule_code") or "")
        if not _RULE_CODE_RE.fullmatch(code):
            raise RuleConfigurationError("invalid rule_code")
        normalized["rule_code"] = code
    if not partial or "rule_type" in rule:
        rule_type = str(rule.get("rule_type") or "")
        if rule_type not in RULE_TYPES:
            raise RuleConfigurationError("unsupported rule_type")
        normalized["rule_type"] = rule_type
    if not partial or "severity" in rule:
        severity = str(rule.get("severity") or "error")
        if severity not in SEVERITIES:
            raise RuleConfigurationError("invalid severity")
        normalized["severity"] = severity
    if "enabled" in rule:
        normalized["enabled"] = 1 if rule["enabled"] in (1, True, "1") else 0
    params = _params(rule) if ("params" in rule or "params_json" in rule or not partial) else {}
    if "params" in rule or "params_json" in rule or not partial:
        normalized["params"] = params
        normalized.pop("params_json", None)

    rule_type = normalized.get("rule_type")
    field_name = normalized.get("field_name")
    if rule_type in {"required", "enum", "range", "regex", "mapping", "schema"} and not field_name:
        raise RuleConfigurationError("field_name is required")
    if rule_type == "unique":
        fields = params.get("fields")
        if (not isinstance(fields, list) or not fields
                or any(not isinstance(name, str) or not name for name in fields)):
            raise RuleConfigurationError("unique fields are required")
    if rule_type == "enum" and not isinstance(params.get("values"), list):
        raise RuleConfigurationError("enum values must be a list")
    if rule_type == "regex":
        pattern = params.get("pattern")
        if not isinstance(pattern, str) or not pattern or len(pattern) > 512:
            raise RuleConfigurationError("invalid regex pattern")
        try:
            re.compile(pattern)
        except re.error:
            raise RuleConfigurationError("invalid regex pattern") from None
    if rule_type == "range":
        for key in ("min", "max"):
            if key in params and params[key] is not None:
                _decimal(params[key])
    if rule_type == "mapping" and not isinstance(params.get("values"), dict):
        raise RuleConfigurationError("mapping values must be an object")
    return normalized


def _decimal(value) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise InvalidOperation
    if isinstance(value, Decimal):
        return value
    text = normalize_text(str(value))
    if not text:
        raise InvalidOperation
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        raise InvalidOperation from None
    if not number.is_finite():
        raise InvalidOperation
    return number


def _is_missing(value) -> bool:
    return value is None or (isinstance(value, str) and not normalize_text(value))


def _key_part(value) -> str:
    return "" if value is None else str(value)


def _business_key(row, fields=None) -> str:
    if fields:
        return "|".join(_key_part(row.get(name)) for name in fields)
    for name in ("trade_no", "order_no", "business_date", "source_record_id"):
        if row.get(name) not in (None, ""):
            return str(row[name])
    return ""


def _issue(dataset, row, rule, reason, *, field_name=None, original=None,
           cleaned=None, severity=None, business_key=None) -> CleaningIssue:
    return CleaningIssue(
        dataset=dataset,
        source_record_id=str(row.get("source_record_id") or ""),
        business_key=business_key if business_key is not None else _business_key(row),
        rule_code=str(rule.get("rule_code") or rule.get("rule_type", "UNKNOWN")),
        rule_type=str(rule.get("rule_type") or "unknown"),
        field_name=field_name if field_name is not None else rule.get("field_name"),
        original_value=original,
        cleaned_value=cleaned,
        severity=severity or rule.get("severity") or "error",
        reason=reason,
    )


def evaluate_rows(dataset, rows: Iterable[dict], rules: Iterable[dict], *,
                  overrides=None, keep_quarantined=True, state=None) -> CleaningResult:
    """Clean and validate a batch using only in-memory rules and overrides.

    ``state`` may be shared by successive batches of one run.  Only compact
    unique-key digests are retained, never raw payloads or database resources.
    """
    active_rules = [validate_rule(rule) for rule in rules if rule.get("enabled", 1)]
    overrides = overrides or {}
    result = CleaningResult()
    cleaned_rows = []
    row_blocking = []
    unique_rules = [rule for rule in active_rules if rule["rule_type"] == "unique"]
    row_rules = [rule for rule in active_rules if rule["rule_type"] not in
                 {"unique", "freshness", "reconcile"}]
    unique_state = state.setdefault("unique", {}) if state is not None else {}

    for input_row in rows:
        if not isinstance(input_row, dict):
            raise RuleConfigurationError("rows must contain objects")
        result.records_read += 1
        row = {key: normalize_text(value) for key, value in input_row.items()}
        source_id = str(row.get("source_record_id") or "")
        for (override_source, field_name), value in overrides.items():
            if override_source == source_id:
                row[field_name] = normalize_text(value)
        blocking = False
        for rule in row_rules:
            rule_type = rule["rule_type"]
            field_name = rule.get("field_name")
            value = row.get(field_name) if field_name else None
            params = rule.get("params") or {}
            issue = None
            if rule_type == "required" and _is_missing(value):
                issue = _issue(dataset, row, rule, "required", original=value, cleaned=value)
            elif rule_type == "enum" and not _is_missing(value):
                allowed = {normalize_text(str(item)) for item in params["values"]}
                if normalize_text(str(value)) not in allowed:
                    issue = _issue(dataset, row, rule, "enum", original=value, cleaned=value)
            elif rule_type == "range" and not _is_missing(value):
                try:
                    number = _decimal(value)
                    if params.get("min") is not None and number < _decimal(params["min"]):
                        issue = _issue(dataset, row, rule, "below_min", original=value, cleaned=value)
                    elif params.get("max") is not None and number > _decimal(params["max"]):
                        issue = _issue(dataset, row, rule, "above_max", original=value, cleaned=value)
                except InvalidOperation:
                    issue = _issue(dataset, row, rule, "invalid_decimal", original=value, cleaned=value)
            elif rule_type == "regex" and not _is_missing(value):
                if re.search(params["pattern"], str(value)) is None:
                    issue = _issue(dataset, row, rule, "regex", original=value, cleaned=value)
            elif rule_type == "mapping" and not _is_missing(value):
                mapping = params["values"]
                key = normalize_text(str(value))
                if key in mapping:
                    target = params.get("target_field") or field_name
                    row[target] = mapping[key]
                else:
                    result.unmapped += 1
                    issue = _issue(dataset, row, rule, "unmapped", original=value, cleaned=value)
            elif rule_type == "schema" and params.get("format") == "json_object":
                candidate = value
                if isinstance(candidate, str):
                    try:
                        candidate = json.loads(candidate)
                    except (TypeError, ValueError):
                        candidate = None
                if not isinstance(candidate, dict):
                    issue = _issue(dataset, row, rule, "invalid_json_object",
                                   original="<redacted>", cleaned=None)
            if issue is not None:
                result.issues.append(issue)
                blocking = blocking or issue.severity in {"critical", "error"}

        for rule in unique_rules:
            fields = tuple(rule["params"]["fields"])
            key = tuple(normalize_text(_key_part(row.get(name))) for name in fields)
            rule_key = str(rule.get("id") or rule["rule_code"])
            seen = unique_state.setdefault(rule_key, {})
            digest = hashlib.sha256(json.dumps(
                row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                default=str,
            ).encode("utf-8")).hexdigest()
            if key in seen:
                result.duplicates += 1
                exact = seen[key] == digest
                issue = _issue(
                    dataset, row, rule,
                    "duplicate_exact" if exact else "duplicate_conflict",
                    severity="warning" if exact else rule["severity"],
                    business_key="|".join(key),
                )
                result.issues.append(issue)
                blocking = blocking or issue.severity in {"critical", "error"}
            else:
                seen[key] = digest
        cleaned_rows.append(row)
        row_blocking.append(blocking)

    for row, blocking in zip(cleaned_rows, row_blocking):
        if blocking:
            result.records_quarantined += 1
            if keep_quarantined:
                result.rows.append(row)
        else:
            result.records_passed += 1
            result.rows.append(row)
    return result


def rule_version(rules: Iterable[dict]) -> str:
    payload = []
    for rule in rules:
        if not rule.get("enabled", 1):
            continue
        params = _params(rule)
        payload.append({
            "id": rule.get("id"),
            "rule_code": rule.get("rule_code"),
            "version": rule.get("version", 1),
            "rule_type": rule.get("rule_type"),
            "field_name": rule.get("field_name"),
            "severity": rule.get("severity"),
            "params": params,
        })
    payload.sort(key=lambda item: (str(item.get("rule_code") or ""),
                                   str(item.get("id") or "")))
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def issue_fingerprint(issue: dict) -> str:
    parts = [
        issue.get("dataset"), issue.get("source_record_id"),
        issue.get("business_key"), issue.get("rule_code"),
        issue.get("field_name"), issue.get("reason"),
    ]
    return hashlib.sha256(
        "\x1f".join(str(part or "") for part in parts).encode("utf-8")
    ).hexdigest()


DEFAULT_RULES = (
    {
        "dataset": "channel_daily_sales", "rule_code": "CHANNEL_SOURCE_REQUIRED",
        "rule_type": "required", "field_name": "source_record_id",
        "params": {}, "severity": "error",
    },
    {
        "dataset": "channel_daily_sales", "rule_code": "CHANNEL_KEY_REQUIRED",
        "rule_type": "required", "field_name": "channel",
        "params": {}, "severity": "error",
    },
    {
        "dataset": "channel_daily_sales", "rule_code": "CHANNEL_STORE_REQUIRED",
        "rule_type": "required", "field_name": "store_name",
        "params": {}, "severity": "error",
    },
    {
        "dataset": "channel_daily_sales", "rule_code": "CHANNEL_DATE_REQUIRED",
        "rule_type": "required", "field_name": "business_date",
        "params": {}, "severity": "error",
    },
    {
        "dataset": "channel_daily_sales", "rule_code": "CHANNEL_BUSINESS_KEY_UNIQUE",
        "rule_type": "unique", "field_name": None,
        "params": {"fields": ["channel", "store_name", "business_date"]},
        "severity": "critical",
    },
    {
        "dataset": "channel_daily_sales", "rule_code": "CHANNEL_T1_FRESHNESS",
        "rule_type": "freshness", "field_name": "business_date",
        "params": {"policy": "t1_exists"}, "severity": "critical",
    },
    {
        "dataset": "wdt_order_line_fact", "rule_code": "WDT_PAYLOAD_SCHEMA",
        "rule_type": "schema", "field_name": "payload_json",
        "params": {"format": "json_object"}, "severity": "error",
    },
    {
        "dataset": "wdt_order_line_fact", "rule_code": "WDT_TRADE_NO_REQUIRED",
        "rule_type": "required", "field_name": "trade_no",
        "params": {}, "severity": "error",
    },
    {
        "dataset": "wdt_order_line_fact", "rule_code": "WDT_SPEC_NO_REQUIRED",
        "rule_type": "required", "field_name": "spec_no",
        "params": {}, "severity": "error",
    },
    {
        "dataset": "wdt_order_line_fact", "rule_code": "WDT_ORDER_LINE_UNIQUE",
        "rule_type": "unique", "field_name": None,
        "params": {"fields": ["trade_no", "line_no"]}, "severity": "critical",
    },
    {
        "dataset": "wdt_order_line_fact", "rule_code": "WDT_SOURCE_NOT_EMPTY",
        "rule_type": "freshness", "field_name": None,
        "params": {"policy": "not_empty", "min_records": 1}, "severity": "critical",
    },
)
