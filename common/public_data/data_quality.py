"""Data-quality control plane and read-only cleaning runner.

Raw connections are read-only here.  The only writes in this module are to the
``dq_*`` control tables in mart_ops; publishing business facts remains the
extract layer's responsibility.  Rule evaluation itself lives in cleaning.py
and never receives a database connection.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone

from common.public_data.cleaning import (
    DEFAULT_RULES,
    ISSUE_STATUSES,
    CleaningIssue,
    evaluate_rows,
    issue_fingerprint,
    mask_value,
    rule_version as calculate_rule_version,
    validate_rule,
)
from common.public_data.db import LockUnavailable, named_lock, transaction
from common.public_data.extract_mart import _wdt_row_size, iterate_wdt_trades
from common.public_data.extract_order_line import (
    _EXCLUDED_TRADE_STATUSES,
    _TRADE_METHOD,
    _clean,
    _parse_datetime,
    _to_decimal,
)
from common.public_data.mart_extract_schema import dataset_by_name


SUPPORTED_DATASETS = frozenset({"channel_daily_sales", "wdt_order_line_fact"})
RUN_MODES = frozenset({"check", "preview", "rerun"})
RUN_STATUSES = frozenset({"pending", "running", "finished", "failed"})
RESOLUTION_STATUSES = frozenset({"confirmed", "ignored"})
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_DETAIL_FIELDS = frozenset({
    "line_no", "spec_no", "goods_name", "quantity", "paid_amount",
    "platform_subsidy", "shop_subsidy", "raw_json",
})
DATASET_FIELDS = {
    "channel_daily_sales": frozenset({
        "source_record_id", *dataset_by_name("channel_daily_sales").target_columns,
    }),
    "wdt_order_line_fact": frozenset({
        "source_record_id", "payload_json", "trade_no", "trade_status",
        "trade_time", "customer_id", *_DETAIL_FIELDS,
    }),
}


class DataQualityError(ValueError):
    pass


class DataQualityNotFound(DataQualityError):
    pass


class DataQualityConflict(DataQualityError):
    pass


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _new_id():
    return str(uuid.uuid4())


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                      separators=(",", ":"))


def _decode_params(value):
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def _rule_out(row):
    result = dict(row)
    if "params_json" in result:
        result["params"] = _decode_params(result.pop("params_json"))
    return result


def _validate_dataset(dataset):
    if dataset not in SUPPORTED_DATASETS:
        raise DataQualityError("unsupported dataset")
    return dataset


def _validate_window(window_start, window_end):
    def parse(value):
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            raise DataQualityError("invalid business window") from None
    start, end = parse(window_start), parse(window_end)
    if (start is None) != (end is None):
        raise DataQualityError("business window must have both boundaries")
    if start is not None and start > end:
        raise DataQualityError("invalid business window")
    return start, end


_DQ_RULE_DDL = """CREATE TABLE IF NOT EXISTS `dq_rule` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `dataset` VARCHAR(64) NOT NULL,
  `rule_code` VARCHAR(64) NOT NULL,
  `rule_type` VARCHAR(16) NOT NULL,
  `field_name` VARCHAR(128) DEFAULT NULL,
  `params_json` JSON NOT NULL,
  `severity` VARCHAR(16) NOT NULL,
  `enabled` TINYINT(1) NOT NULL DEFAULT 1,
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `created_by` VARCHAR(64) NOT NULL,
  `updated_by` VARCHAR(64) NOT NULL,
  `created_at` DATETIME(6) NOT NULL,
  `updated_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_dq_rule_code` (`dataset`, `rule_code`),
  KEY `idx_dq_rule_dataset_enabled` (`dataset`, `enabled`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

_DQ_RUN_DDL = """CREATE TABLE IF NOT EXISTS `dq_run` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `run_id` CHAR(36) NOT NULL,
  `dataset` VARCHAR(64) NOT NULL,
  `sync_run_id` CHAR(36) DEFAULT NULL,
  `rule_version` CHAR(64) NOT NULL,
  `mode` VARCHAR(16) NOT NULL,
  `dry_run` TINYINT(1) NOT NULL DEFAULT 1,
  `status` VARCHAR(16) NOT NULL,
  `window_start` DATE DEFAULT NULL,
  `window_end` DATE DEFAULT NULL,
  `records_read` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `records_passed` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `records_quarantined` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `records_written` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `duplicates` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `unmapped` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `issue_count` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `alert_status` VARCHAR(16) NOT NULL DEFAULT 'normal',
  `sql_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `db_ms` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `batch_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `max_batch_rows` INT UNSIGNED NOT NULL DEFAULT 0,
  `max_batch_bytes` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `requested_by` VARCHAR(64) NOT NULL,
  `reason` VARCHAR(500) NOT NULL DEFAULT '',
  `failure_code` VARCHAR(100) DEFAULT NULL,
  `diff_json` JSON DEFAULT NULL,
  `created_at` DATETIME(6) NOT NULL,
  `started_at` DATETIME(6) DEFAULT NULL,
  `finished_at` DATETIME(6) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_dq_run_id` (`run_id`),
  KEY `idx_dq_run_dataset_started` (`dataset`, `started_at`),
  KEY `idx_dq_run_status_id` (`status`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

_DQ_ISSUE_DDL = """CREATE TABLE IF NOT EXISTS `dq_issue` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `run_id` CHAR(36) NOT NULL,
  `fingerprint` CHAR(64) NOT NULL,
  `dataset` VARCHAR(64) NOT NULL,
  `source_record_id` VARCHAR(320) NOT NULL DEFAULT '',
  `business_key` VARCHAR(512) NOT NULL DEFAULT '',
  `rule_code` VARCHAR(64) NOT NULL,
  `rule_type` VARCHAR(16) NOT NULL,
  `field_name` VARCHAR(128) DEFAULT NULL,
  `original_value` TEXT,
  `cleaned_value` TEXT,
  `severity` VARCHAR(16) NOT NULL,
  `reason` VARCHAR(64) NOT NULL,
  `status` VARCHAR(16) NOT NULL DEFAULT 'open',
  `resolution_reason` VARCHAR(500) NOT NULL DEFAULT '',
  `resolved_by` VARCHAR(64) DEFAULT NULL,
  `resolved_at` DATETIME(6) DEFAULT NULL,
  `created_at` DATETIME(6) NOT NULL,
  `updated_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_dq_issue_run_fingerprint` (`run_id`, `fingerprint`),
  KEY `idx_dq_issue_dataset_status_id` (`dataset`, `status`, `id`),
  KEY `idx_dq_issue_rule` (`dataset`, `rule_code`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

_DQ_OVERRIDE_DDL = """CREATE TABLE IF NOT EXISTS `dq_value_override` (
  `override_id` CHAR(36) NOT NULL,
  `dataset` VARCHAR(64) NOT NULL,
  `source_record_id` VARCHAR(320) NOT NULL,
  `field_name` VARCHAR(128) NOT NULL,
  `override_value` TEXT,
  `reason` VARCHAR(500) NOT NULL,
  `active` TINYINT(1) NOT NULL DEFAULT 1,
  `audit_id` CHAR(36) NOT NULL,
  `created_by` VARCHAR(64) NOT NULL,
  `created_at` DATETIME(6) NOT NULL,
  `updated_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`override_id`),
  UNIQUE KEY `uq_dq_override_target` (`dataset`, `source_record_id`, `field_name`),
  KEY `idx_dq_override_source` (`dataset`, `source_record_id`, `field_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

_DQ_AUDIT_DDL = """CREATE TABLE IF NOT EXISTS `dq_audit` (
  `audit_id` CHAR(36) NOT NULL,
  `actor` VARCHAR(64) NOT NULL,
  `action` VARCHAR(32) NOT NULL,
  `dataset` VARCHAR(64) NOT NULL,
  `target` VARCHAR(320) NOT NULL DEFAULT '',
  `detail_json` JSON NOT NULL,
  `created_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`audit_id`),
  KEY `idx_dq_audit_dataset_created` (`dataset`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


_DQ_RUN_SNAPSHOT_DDL = """CREATE TABLE IF NOT EXISTS `dq_run_snapshot` (
  `run_id` CHAR(36) NOT NULL,
  `dataset` VARCHAR(64) NOT NULL,
  `rule_version` CHAR(64) NOT NULL,
  `rules_json` JSON NOT NULL,
  `overrides_json` JSON NOT NULL,
  `created_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`run_id`),
  KEY `idx_dq_snapshot_dataset_created` (`dataset`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def data_quality_ddl_statements() -> tuple[str, ...]:
    return (_DQ_RULE_DDL, _DQ_RUN_DDL, _DQ_ISSUE_DDL,
            _DQ_OVERRIDE_DDL, _DQ_AUDIT_DDL)


def data_quality_snapshot_ddl_statements() -> tuple[str, ...]:
    """Append-only rerun snapshots; kept out of the frozen v1 migration."""
    return (_DQ_RUN_SNAPSHOT_DDL,)


class DataQualityRepository:
    """Fixed-shape SQL for the quality control tables; no arbitrary SQL input."""

    def __init__(self, connection, *, now=_utc_now, new_id=_new_id):
        self.connection = connection
        self.now = now
        self.new_id = new_id
        self.sql_count = 0
        self.db_ms = 0

    def execute(self, sql, params=None):
        cursor = self.connection.cursor()
        started = time.perf_counter()
        try:
            cursor.execute(sql, params)
        except Exception:
            cursor.close()
            raise
        finally:
            self.sql_count += 1
            self.db_ms += int((time.perf_counter() - started) * 1000)
        return cursor

    def executemany(self, sql, params):
        cursor = self.connection.cursor()
        started = time.perf_counter()
        try:
            result = cursor.executemany(sql, params)
        except Exception:
            cursor.close()
            raise
        finally:
            self.sql_count += 1
            self.db_ms += int((time.perf_counter() - started) * 1000)
        cursor.close()
        return result

    def _audit(self, *, actor, action, dataset, target, detail, audit_id=None):
        audit_id = audit_id or self.new_id()
        with contextlib.closing(self.execute(
            "INSERT INTO `dq_audit` "
            "(`audit_id`, `actor`, `action`, `dataset`, `target`, `detail_json`, `created_at`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (audit_id, actor, action, dataset, str(target)[:320], _json(detail), self.now()),
        )):
            pass
        return audit_id

    def seed_default_rules(self, *, actor="system"):
        with contextlib.closing(self.execute(
            "SELECT `dataset`, `rule_code` FROM `dq_rule`"
        )) as cursor:
            existing = {(r["dataset"], r["rule_code"]) for r in cursor.fetchall()}
        missing = [dict(rule, created_by=actor, updated_by=actor)
                   for rule in DEFAULT_RULES
                   if (rule["dataset"], rule["rule_code"]) not in existing]
        if not missing:
            return 0
        now = self.now()
        with transaction(self.connection):
            self.executemany(
                "INSERT INTO `dq_rule` "
                "(`dataset`, `rule_code`, `rule_type`, `field_name`, `params_json`, "
                "`severity`, `enabled`, `version`, `created_by`, `updated_by`, `created_at`, `updated_at`) "
                "VALUES (%s, %s, %s, %s, %s, %s, 1, 1, %s, %s, %s, %s)",
                [(r["dataset"], r["rule_code"], r["rule_type"], r.get("field_name"),
                  _json(r.get("params") or {}), r["severity"], actor, actor, now, now)
                 for r in missing],
            )
            self._audit(actor=actor, action="seed", dataset="*",
                        target="default_rules", detail={"count": len(missing)})
        return len(missing)

    def list_rules(self, dataset=None, *, enabled_only=False):
        conditions, params = [], []
        if dataset is not None:
            conditions.append("`dataset` = %s")
            params.append(_validate_dataset(dataset))
        if enabled_only:
            conditions.append("`enabled` = 1")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with contextlib.closing(self.execute(
            "SELECT * FROM `dq_rule`" + where + " ORDER BY `dataset`, `id`",
            tuple(params) or None,
        )) as cursor:
            return [_rule_out(row) for row in cursor.fetchall()]

    def get_rule(self, rule_id, *, for_update=False):
        sql = "SELECT * FROM `dq_rule` WHERE `id` = %s"
        if for_update:
            sql += " FOR UPDATE"
        with contextlib.closing(self.execute(sql, (int(rule_id),))) as cursor:
            row = cursor.fetchone()
        if row is None:
            raise DataQualityNotFound("rule not found")
        return _rule_out(row)

    def create_rule(self, rule, *, actor):
        normalized = validate_rule(rule)
        _validate_dataset(normalized["dataset"])
        now = self.now()
        with transaction(self.connection):
            with contextlib.closing(self.execute(
                "INSERT INTO `dq_rule` "
                "(`dataset`, `rule_code`, `rule_type`, `field_name`, `params_json`, "
                "`severity`, `enabled`, `version`, `created_by`, `updated_by`, `created_at`, `updated_at`) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s)",
                (normalized["dataset"], normalized["rule_code"], normalized["rule_type"],
                 normalized.get("field_name"), _json(normalized["params"]),
                 normalized["severity"], normalized.get("enabled", 1), actor, actor, now, now),
            )) as cursor:
                rule_id = cursor.lastrowid
            audit_id = self._audit(actor=actor, action="create",
                                   dataset=normalized["dataset"],
                                   target=normalized["rule_code"], detail=normalized)
        return {"rule": self.get_rule(rule_id), "audit_id": audit_id}

    def update_rule(self, rule_id, changes, *, actor):
        allowed = {"rule_type", "field_name", "params", "params_json", "severity", "enabled"}
        if not isinstance(changes, dict) or not changes or set(changes) - allowed:
            raise DataQualityError("invalid rule changes")
        with transaction(self.connection):
            current = self.get_rule(rule_id, for_update=True)
            merged = {**current, **changes}
            normalized = validate_rule(merged)
            _validate_dataset(normalized["dataset"])
            version = int(current.get("version") or 1) + 1
            with contextlib.closing(self.execute(
                "UPDATE `dq_rule` SET `rule_type` = %s, `field_name` = %s, "
                "`params_json` = %s, `severity` = %s, `enabled` = %s, "
                "`version` = %s, `updated_by` = %s, `updated_at` = %s WHERE `id` = %s",
                (normalized["rule_type"], normalized.get("field_name"),
                 _json(normalized["params"]), normalized["severity"],
                 normalized.get("enabled", 1), version, actor, self.now(), int(rule_id)),
            )):
                pass
            audit_id = self._audit(
                actor=actor, action="update", dataset=current["dataset"],
                target=current["rule_code"],
                detail={"before": current, "after": normalized, "version": version},
            )
        return {"rule": self.get_rule(rule_id), "audit_id": audit_id}

    def list_overrides(self, dataset):
        _validate_dataset(dataset)
        with contextlib.closing(self.execute(
            "SELECT `source_record_id`, `field_name`, `override_value` "
            "FROM `dq_value_override` WHERE `dataset` = %s AND `active` = 1",
            (dataset,),
        )) as cursor:
            return {(r["source_record_id"], r["field_name"]): r["override_value"]
                    for r in cursor.fetchall()}

    def put_override(self, *, dataset, source_record_id, field_name, value,
                     reason, actor):
        _validate_dataset(dataset)
        if not source_record_id or len(source_record_id) > 320:
            raise DataQualityError("invalid source_record_id")
        if (not _FIELD_RE.fullmatch(field_name or "")
                or field_name not in DATASET_FIELDS[dataset]):
            raise DataQualityError("invalid field_name")
        if not str(reason or "").strip():
            raise DataQualityError("reason is required")
        if value is not None and len(str(value)) > 4096:
            raise DataQualityError("override value is too large")
        override_id, audit_id, now = self.new_id(), self.new_id(), self.now()
        with transaction(self.connection):
            with contextlib.closing(self.execute(
                "INSERT INTO `dq_value_override` "
                "(`override_id`, `dataset`, `source_record_id`, `field_name`, `override_value`, "
                "`reason`, `active`, `audit_id`, `created_by`, `created_at`, `updated_at`) "
                "VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE `override_value` = VALUES(`override_value`), "
                "`reason` = VALUES(`reason`), `active` = 1, `audit_id` = VALUES(`audit_id`), "
                "`updated_at` = VALUES(`updated_at`)",
                (override_id, dataset, source_record_id, field_name, value,
                 str(reason).strip()[:500], audit_id, actor, now, now),
            )) as cursor:
                pass
            with contextlib.closing(self.execute(
                "SELECT `override_id` FROM `dq_value_override` "
                "WHERE `dataset` = %s AND `source_record_id` = %s AND `field_name` = %s",
                (dataset, source_record_id, field_name),
            )) as cursor:
                override_id = cursor.fetchone()["override_id"]
            self._audit(actor=actor, action="override", dataset=dataset,
                        target=f"{source_record_id}:{field_name}",
                        detail={"override_id": override_id, "value": mask_value(field_name, value),
                                "reason": str(reason).strip()[:500]}, audit_id=audit_id)
        return {"override_id": override_id, "audit_id": audit_id,
                "dataset": dataset, "source_record_id": source_record_id,
                "field_name": field_name, "active": 1}

    def resolve_issue(self, issue_id, *, status, reason, actor):
        if status not in RESOLUTION_STATUSES:
            raise DataQualityError("invalid resolution status")
        if not str(reason or "").strip():
            raise DataQualityError("reason is required")
        with transaction(self.connection):
            with contextlib.closing(self.execute(
                "SELECT * FROM `dq_issue` WHERE `id` = %s FOR UPDATE", (int(issue_id),)
            )) as cursor:
                issue = cursor.fetchone()
            if issue is None:
                raise DataQualityNotFound("issue not found")
            with contextlib.closing(self.execute(
                "UPDATE `dq_issue` SET `status` = %s, `resolution_reason` = %s, "
                "`resolved_by` = %s, `resolved_at` = %s, `updated_at` = %s WHERE `id` = %s",
                (status, str(reason).strip()[:500], actor, self.now(), self.now(), int(issue_id)),
            )):
                pass
            audit_id = self._audit(actor=actor, action=status, dataset=issue["dataset"],
                                   target=str(issue_id), detail={"reason": str(reason).strip()[:500],
                                                                "rule_code": issue["rule_code"]})
        return {"issue_id": int(issue_id), "status": status, "audit_id": audit_id,
                "dataset": issue["dataset"]}

    def create_run(self, *, dataset, rule_version=None, mode, requested_by, reason="",
                   window_start=None, window_end=None, dry_run=True, status="running"):
        _validate_dataset(dataset)
        if mode not in RUN_MODES or status not in RUN_STATUSES:
            raise DataQualityError("invalid run")
        window_start, window_end = _validate_window(window_start, window_end)
        if mode == "rerun" and window_start is None:
            raise DataQualityError("rerun requires a bounded business window")
        run_id, now = self.new_id(), self.now()
        with transaction(self.connection):
            snapshot = None
            if mode == "rerun":
                rules = self.list_rules(dataset, enabled_only=True)
                overrides = self.list_overrides(dataset)
                snapshot = {
                    "rules": rules,
                    "overrides": [
                        {"source_record_id": source_id, "field_name": field_name,
                         "override_value": value}
                        for (source_id, field_name), value in sorted(overrides.items())
                    ],
                }
                rule_version = calculate_rule_version(rules)
            with contextlib.closing(self.execute(
                "INSERT INTO `dq_run` "
                "(`run_id`, `dataset`, `rule_version`, `mode`, `dry_run`, `status`, "
                "`window_start`, `window_end`, `requested_by`, `reason`, `created_at`, `started_at`) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (run_id, dataset, rule_version, mode, int(bool(dry_run)), status,
                 window_start, window_end, requested_by, str(reason or "")[:500], now,
                 now if status == "running" else None),
            )):
                pass
            if snapshot is not None:
                with contextlib.closing(self.execute(
                    "INSERT INTO `dq_run_snapshot` "
                    "(`run_id`, `dataset`, `rule_version`, `rules_json`, `overrides_json`, `created_at`) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (run_id, dataset, rule_version, _json(snapshot["rules"]),
                     _json(snapshot["overrides"]), now),
                )):
                    pass
                self._audit(actor=requested_by, action="rerun", dataset=dataset,
                            target=run_id, detail={"window_start": str(window_start or ""),
                                                  "window_end": str(window_end or ""),
                                                  "dry_run": bool(dry_run), "reason": str(reason or "")[:500]})
        return run_id

    def get_run_snapshot(self, run_id):
        with contextlib.closing(self.execute(
            "SELECT * FROM `dq_run_snapshot` WHERE `run_id` = %s", (run_id,)
        )) as cursor:
            row = cursor.fetchone()
        if row is None:
            raise DataQualityNotFound("rerun snapshot not found")
        rules = _decode_params(row["rules_json"])
        override_rows = _decode_params(row["overrides_json"])
        if not isinstance(rules, list) or not isinstance(override_rows, list):
            raise DataQualityError("invalid rerun snapshot")
        return {
            "run_id": run_id,
            "dataset": row["dataset"],
            "rule_version": row["rule_version"],
            "rules": rules,
            "overrides": {
                (str(item["source_record_id"]), str(item["field_name"])): item.get("override_value")
                for item in override_rows
            },
        }

    def insert_issues(self, run_id, issues):
        if not issues:
            return 0
        now = self.now()
        params = []
        for issue in issues:
            row = issue.as_row(run_id) if isinstance(issue, CleaningIssue) else dict(issue)
            row["fingerprint"] = issue_fingerprint(row)
            params.append((run_id, row["fingerprint"], row["dataset"], row["source_record_id"],
                           row["business_key"], row["rule_code"], row["rule_type"],
                           row.get("field_name"), row.get("original_value"), row.get("cleaned_value"),
                           row["severity"], row["reason"], now, now))
        self.executemany(
            "INSERT INTO `dq_issue` "
            "(`run_id`, `fingerprint`, `dataset`, `source_record_id`, `business_key`, "
            "`rule_code`, `rule_type`, `field_name`, `original_value`, `cleaned_value`, "
            "`severity`, `reason`, `created_at`, `updated_at`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            params,
        )
        return len(params)

    def delete_run_issues(self, run_id):
        """Replace one claimed run's precheck issues with its postcheck issues."""
        with contextlib.closing(self.execute(
            "DELETE FROM `dq_issue` WHERE `run_id` = %s", (run_id,),
        )) as cursor:
            return cursor.rowcount

    def previous_issue_baseline(self, run_id):
        """Return the latest earlier finished run for the same dataset/window."""
        with contextlib.closing(self.execute(
            "SELECT previous.`run_id`, previous.`issue_count` "
            "FROM `dq_run` current "
            "JOIN `dq_run` previous ON previous.`dataset` = current.`dataset` "
            "AND previous.`id` < current.`id` "
            "AND previous.`window_start` <=> current.`window_start` "
            "AND previous.`window_end` <=> current.`window_end` "
            "WHERE current.`run_id` = %s AND previous.`status` = 'finished' "
            "AND previous.`mode` IN ('check', 'rerun') "
            "ORDER BY previous.`id` DESC LIMIT 1",
            (run_id,),
        )) as cursor:
            row = cursor.fetchone()
        return dict(row) if row is not None else None

    def finish_run(self, run_id, *, status, counts, metrics, alert_status="normal",
                   failure_code=None, diff=None, sync_run_id=None):
        if status not in {"finished", "failed"}:
            raise DataQualityError("invalid terminal run status")
        with transaction(self.connection):
            with contextlib.closing(self.execute(
                "UPDATE `dq_run` SET `status` = %s, `records_read` = %s, `records_passed` = %s, "
                "`records_quarantined` = %s, `records_written` = %s, `duplicates` = %s, "
                "`unmapped` = %s, `issue_count` = %s, `alert_status` = %s, `sql_count` = %s, "
                "`db_ms` = %s, `batch_count` = %s, `max_batch_rows` = %s, `max_batch_bytes` = %s, "
                "`failure_code` = %s, `diff_json` = %s, `sync_run_id` = %s, `finished_at` = %s "
                "WHERE `run_id` = %s AND `status` = 'running'",
                (status, counts["records_read"], counts["records_passed"],
                 counts["records_quarantined"], counts.get("records_written", 0),
                 counts["duplicates"], counts["unmapped"], counts["issue_count"], alert_status,
                 metrics["sql_count"], metrics["db_ms"], metrics["batch_count"],
                 metrics["max_batch_rows"], metrics["max_batch_bytes"], failure_code,
                 _json(diff) if diff is not None else None, sync_run_id, self.now(), run_id),
            )):
                pass

    def get_run(self, run_id):
        with contextlib.closing(self.execute(
            "SELECT * FROM `dq_run` WHERE `run_id` = %s", (run_id,)
        )) as cursor:
            row = cursor.fetchone()
        if row is None:
            raise DataQualityNotFound("run not found")
        return row

    def list_runs(self, *, dataset=None, mode=None, cursor_id=None, limit=50):
        limit = max(1, min(int(limit), 200))
        conditions, params = [], []
        if dataset:
            conditions.append("`dataset` = %s")
            params.append(_validate_dataset(dataset))
        if mode:
            if mode not in RUN_MODES:
                raise DataQualityError("invalid run mode")
            conditions.append("`mode` = %s")
            params.append(mode)
        if cursor_id:
            conditions.append("`id` < %s")
            params.append(int(cursor_id))
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit + 1)
        with contextlib.closing(self.execute(
            "SELECT * FROM `dq_run`" + where + " ORDER BY `id` DESC LIMIT %s",
            tuple(params),
        )) as cursor:
            rows = cursor.fetchall()
        return {"items": rows[:limit],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}

    def list_issues(self, dataset, *, status=None, rule_code=None,
                    cursor_id=None, limit=50):
        _validate_dataset(dataset)
        if status is not None and status not in ISSUE_STATUSES:
            raise DataQualityError("invalid issue status")
        limit = max(1, min(int(limit), 200))
        conditions, params = ["`dataset` = %s"], [dataset]
        if status:
            conditions.append("`status` = %s")
            params.append(status)
        if rule_code:
            conditions.append("`rule_code` = %s")
            params.append(rule_code)
        if cursor_id:
            conditions.append("`id` < %s")
            params.append(int(cursor_id))
        params.append(limit + 1)
        with contextlib.closing(self.execute(
            "SELECT * FROM `dq_issue` WHERE " + " AND ".join(conditions) +
            " ORDER BY `id` DESC LIMIT %s", tuple(params),
        )) as cursor:
            rows = cursor.fetchall()
        return {"items": rows[:limit], "dataset": dataset,
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}

    def overview(self):
        with contextlib.closing(self.execute(
            "SELECT * FROM (SELECT r.*, ROW_NUMBER() OVER "
            "(PARTITION BY `dataset` ORDER BY `started_at` DESC, `id` DESC) AS `rn` "
            "FROM `dq_run` r WHERE `mode` IN ('check', 'rerun')) ranked WHERE `rn` = 1"
        )) as cursor:
            runs = cursor.fetchall()
        with contextlib.closing(self.execute(
            "SELECT `dataset`, `status`, COUNT(*) AS `n` FROM `dq_issue` "
            "GROUP BY `dataset`, `status`"
        )) as cursor:
            counts = cursor.fetchall()
        by_dataset = {}
        for row in counts:
            by_dataset.setdefault(row["dataset"], {})[row["status"]] = int(row["n"])
        items = []
        for run in runs:
            item = dict(run)
            item["issues"] = by_dataset.get(run["dataset"], {})
            items.append(item)
        return {"items": items, "datasets": sorted(SUPPORTED_DATASETS)}

    def claim_next_rerun(self):
        with transaction(self.connection):
            with contextlib.closing(self.execute(
                "SELECT * FROM `dq_run` WHERE `mode` = 'rerun' AND `status` = 'pending' "
                "ORDER BY `id` LIMIT 1 FOR UPDATE SKIP LOCKED"
            )) as cursor:
                row = cursor.fetchone()
            if row is None:
                return None
            with contextlib.closing(self.execute(
                "UPDATE `dq_run` SET `status` = 'running', `started_at` = %s WHERE `id` = %s",
                (self.now(), row["id"]),
            )):
                pass
        return row

    def close_resolved_issues(self, run_id, *, actor):
        """Close only earlier open issues absent from this exact run/window."""
        dataset = self.get_run(run_id)["dataset"]
        now = self.now()
        reason = f"rerun postcheck passed: {run_id}"[:500]
        with transaction(self.connection):
            with contextlib.closing(self.execute(
                "UPDATE `dq_issue` i "
                "JOIN `dq_run` previous ON previous.`run_id` = i.`run_id` "
                "JOIN `dq_run` current ON current.`run_id` = %s "
                "LEFT JOIN `dq_issue` current_issue "
                "ON current_issue.`run_id` = current.`run_id` "
                "AND current_issue.`fingerprint` = i.`fingerprint` "
                "SET i.`status` = 'resolved', i.`resolution_reason` = %s, "
                "i.`resolved_by` = %s, i.`resolved_at` = %s, i.`updated_at` = %s "
                "WHERE i.`dataset` = current.`dataset` "
                "AND previous.`dataset` = current.`dataset` "
                "AND previous.`id` < current.`id` "
                "AND previous.`window_start` <=> current.`window_start` "
                "AND previous.`window_end` <=> current.`window_end` "
                "AND i.`status` = 'open' AND current_issue.`id` IS NULL",
                (run_id, reason, actor, now, now),
            )) as cursor:
                closed = cursor.rowcount
            if closed:
                self._audit(actor=actor, action="auto_resolve", dataset=dataset,
                            target=run_id, detail={"closed": closed})
        return closed


@dataclass
class SourceBatch:
    rows: list
    source_record_ids: list
    byte_size: int


class _WDTWindowRawCache:
    """Bounded precheck batches reusable only by a WDT dry-run projection."""

    def __init__(self, max_bytes=128 * 1024 * 1024):
        self.max_bytes = int(max_bytes)
        self.byte_size = 0
        self.batches = []
        self.complete = True

    def append(self, batch):
        if not self.complete:
            return
        batch_size = sum(_wdt_row_size(row) for row in batch)
        if self.byte_size + batch_size > self.max_bytes:
            self.batches.clear()
            self.byte_size = 0
            self.complete = False
            return
        self.batches.append(list(batch))
        self.byte_size += batch_size


class DatabaseQualitySource:
    """Bounded raw readers.  SQL count is independent of business row count."""

    def __init__(self, raw_connection, wdt_connection):
        self.raw = raw_connection
        self.wdt = wdt_connection
        self.sql_count = 0
        self.db_ms = 0

    def _execute(self, connection, sql, params):
        cursor = connection.cursor()
        started = time.perf_counter()
        try:
            cursor.execute(sql, params)
        finally:
            self.sql_count += 1
            self.db_ms += int((time.perf_counter() - started) * 1000)
        return cursor

    def iter_batches(self, dataset, *, window_start=None, window_end=None,
                     batch_size=1000, max_batch_bytes=32 * 1024 * 1024,
                     raw_batch_cache=None):
        if raw_batch_cache is not None and dataset != "wdt_order_line_fact":
            raise DataQualityError("raw batch cache is only valid for WDT")
        if dataset == "channel_daily_sales":
            self.channel_business_dates = set()
            yield from self._channel_batches(window_start, window_end, batch_size,
                                             max_batch_bytes)
        elif dataset == "wdt_order_line_fact":
            yield from self._wdt_batches(window_start, window_end, batch_size,
                                         max_batch_bytes, raw_batch_cache)
        else:
            raise DataQualityError("unsupported dataset")

    def _channel_batches(self, window_start, window_end, batch_size, max_batch_bytes):
        dataset = dataset_by_name("channel_daily_sales")
        projection = ["`dingtalk_record_id` AS `source_record_id`"] + [
            f"`{source}` AS `{target}`" for source, target in dataset.columns
        ]
        cursor_id = ""
        while True:
            conditions = ["`dingtalk_record_id` > %s"]
            params = [cursor_id]
            if window_start is not None:
                conditions += ["`business_date` >= %s", "`business_date` <= %s"]
                params += [window_start, window_end]
            params.append(int(batch_size))
            with contextlib.closing(self._execute(
                self.raw,
                f"SELECT {', '.join(projection)} FROM `channel_daily_sales` WHERE " +
                " AND ".join(conditions) + " ORDER BY `dingtalk_record_id` LIMIT %s",
                tuple(params),
            )) as cursor:
                fetched = cursor.fetchall()
            rows, byte_size = [], 0
            for row in fetched:
                row = dict(row)
                size = len(_json(row).encode("utf-8"))
                if rows and byte_size + size > max_batch_bytes:
                    break
                rows.append(row)
                byte_size += size
                business_date = row.get("business_date")
                if isinstance(business_date, datetime):
                    business_date = business_date.date()
                elif business_date and not isinstance(business_date, date):
                    try:
                        business_date = date.fromisoformat(str(business_date)[:10])
                    except ValueError:
                        business_date = None
                if business_date is not None:
                    self.channel_business_dates.add(business_date)
            if not rows:
                return
            cursor_id = str(rows[-1]["source_record_id"])
            yield SourceBatch(rows, [str(r["source_record_id"]) for r in rows], byte_size)
            if len(rows) >= len(fetched) and len(fetched) < batch_size:
                return

    def _wdt_batches(self, window_start, window_end, batch_size, max_batch_bytes,
                     raw_batch_cache=None):
        for trades in iterate_wdt_trades(
            self.wdt, _TRADE_METHOD, batch_size=batch_size,
            max_batch_bytes=max_batch_bytes, metrics=self,
        ):
            rows, source_ids, byte_size = [], [], 0
            raw_batch = [] if raw_batch_cache is not None else None
            for trade in trades:
                payload = trade.get("payload_json")
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except ValueError:
                        pass
                if isinstance(payload, dict) and window_start is not None:
                    trade_date = _parse_datetime(payload.get("trade_time"))
                    if trade_date is not None:
                        trade_date = trade_date.date()
                        if trade_date < window_start or trade_date > window_end:
                            continue
                source_id = str(trade.get("source_record_id") or "")
                source_ids.append(source_id)
                byte_size += _wdt_row_size(trade)
                if raw_batch is not None:
                    raw_batch.append(trade)
                rows.extend(_wdt_quality_rows(source_id, trade.get("payload_json"), payload))
            if source_ids:
                if raw_batch:
                    raw_batch_cache.append(raw_batch)
                yield SourceBatch(rows, source_ids, byte_size)


def _wdt_quality_rows(source_id, original_payload, payload):
    """Return header/detail validation rows without publishing business facts."""
    trade_no = _clean(payload.get("trade_no")) if isinstance(payload, dict) else None
    header = {
        "source_record_id": source_id,
        "payload_json": original_payload,
        "trade_no": trade_no,
        "trade_status": _clean(payload.get("trade_status")) if isinstance(payload, dict) else None,
        "trade_time": _parse_datetime(payload.get("trade_time")) if isinstance(payload, dict) else None,
        "customer_id": _clean(payload.get("customer_id")) if isinstance(payload, dict) else None,
        "_dq_scope": "header",
    }
    rows = [header]
    if not isinstance(payload, dict):
        return rows
    if _clean(payload.get("trade_status")) in _EXCLUDED_TRADE_STATUSES:
        return rows
    details = payload.get("detail_list") or []
    if not isinstance(details, list):
        return rows
    for index, detail in enumerate(details, 1):
        detail = detail if isinstance(detail, dict) else {}
        rows.append({
            "source_record_id": f"{source_id}#{index}",
            "_dq_parent": source_id,
            "_dq_scope": "detail",
            "trade_no": trade_no,
            "line_no": index,
            "trade_time": _parse_datetime(payload.get("trade_time")),
            "customer_id": _clean(payload.get("customer_id")) or None,
            "spec_no": _clean(detail.get("spec_no")),
            "goods_name": _clean(detail.get("goods_name")) or None,
            "quantity": _to_decimal(detail.get("num")),
            "paid_amount": _to_decimal(detail.get("paid")),
        })
    return rows


def _rule_scope(rule):
    if rule.get("field_name") in _DETAIL_FIELDS:
        return "detail"
    fields = (rule.get("params") or {}).get("fields") or []
    if any(field in _DETAIL_FIELDS for field in fields):
        return "detail"
    return "header" if rule.get("dataset") == "wdt_order_line_fact" else "row"


def _empty_counts():
    return {"records_read": 0, "records_passed": 0, "records_quarantined": 0,
            "records_written": 0, "duplicates": 0, "unmapped": 0, "issue_count": 0}


def evaluate_dataset_rules(dataset, rules, counts, window_start, window_end,
                           channel_business_dates=()):
    """Evaluate dataset-level rules for both DQ checks and exact replay rows."""
    issues = []
    for rule in rules:
        rule_type = rule["rule_type"]
        if rule_type == "reconcile":
            raise DataQualityError("reconcile rule requires a registered dataset policy")
        if rule_type != "freshness":
            continue
        params = rule.get("params") or {}
        if dataset == "channel_daily_sales" and params.get("policy") == "t1_exists":
            business_today = datetime.now(timezone(timedelta(hours=8))).date()
            target = window_end or (business_today - timedelta(days=1))
            if target not in set(channel_business_dates):
                issues.append(CleaningIssue(
                    dataset=dataset, source_record_id="", business_key=str(target),
                    rule_code=rule["rule_code"], rule_type=rule_type,
                    field_name="business_date", original_value=str(target), cleaned_value=None,
                    severity=rule["severity"], reason="missing_business_date",
                ))
        elif dataset == "wdt_order_line_fact" and params.get("policy") == "not_empty":
            minimum = int(params.get("min_records", 1))
            if counts["records_read"] < minimum:
                issues.append(CleaningIssue(
                    dataset=dataset, source_record_id="", business_key="",
                    rule_code=rule["rule_code"], rule_type=rule_type,
                    field_name=None, original_value=counts["records_read"], cleaned_value=None,
                    severity=rule["severity"], reason="empty_source",
                ))
        else:
            raise DataQualityError("freshness rule requires a registered dataset policy")
    return issues


class DataQualityRunner:
    """Run deterministic checks over bounded batches and persist only DQ state."""

    def __init__(self, repository, source):
        self.repository = repository
        self.source = source

    def run(self, dataset, *, mode="check", window_start=None, window_end=None,
            rules=None, overrides=None, persist=True, requested_by="scheduler", reason="",
            batch_size=1000, max_batch_bytes=32 * 1024 * 1024, existing_run_id=None,
            finish=True, collect_fingerprints=False, raw_batch_cache=None):
        _validate_dataset(dataset)
        if raw_batch_cache is not None and dataset != "wdt_order_line_fact":
            raise DataQualityError("raw batch cache is only valid for WDT")
        if mode not in {"check", "preview", "rerun"}:
            raise DataQualityError("invalid runner mode")
        if mode == "rerun" and (
            not persist or not existing_run_id or rules is None or overrides is None
        ):
            raise DataQualityError("rerun requires a persisted run and frozen snapshot")
        if existing_run_id is not None and mode != "rerun":
            raise DataQualityError("existing_run_id is only valid for rerun")
        window_start, window_end = _validate_window(window_start, window_end)
        persist = bool(persist and mode in {"check", "rerun"})
        active_rules = [validate_rule(r) for r in (
            rules if rules is not None else self.repository.list_rules(dataset, enabled_only=True)
        ) if r.get("enabled", 1)]
        if any(rule.get("dataset") != dataset for rule in active_rules):
            raise DataQualityError("rule dataset mismatch")
        version = calculate_rule_version(active_rules)
        if overrides is None:
            overrides = self.repository.list_overrides(dataset)
        run_id = existing_run_id or (self.repository.create_run(
            dataset=dataset, rule_version=version, mode=mode, requested_by=requested_by,
            reason=reason, window_start=window_start, window_end=window_end,
            dry_run=True,
        ) if persist else self.repository.new_id())
        counts, state, samples = _empty_counts(), {}, []
        fingerprints, issue_reasons, issue_rule_types = set(), set(), set()
        metrics = {"batch_count": 0, "max_batch_rows": 0, "max_batch_bytes": 0}
        highest_severity = 0
        try:
            for batch in self.source.iter_batches(
                dataset, window_start=window_start, window_end=window_end,
                batch_size=batch_size, max_batch_bytes=max_batch_bytes,
                raw_batch_cache=raw_batch_cache,
            ):
                metrics["batch_count"] += 1
                metrics["max_batch_rows"] = max(metrics["max_batch_rows"], len(batch.source_record_ids))
                metrics["max_batch_bytes"] = max(metrics["max_batch_bytes"], batch.byte_size)
                counts["records_read"] += len(batch.source_record_ids)
                parent_by_source = {}
                scopes = {"row": [], "header": [], "detail": []}
                for input_row in batch.rows:
                    row = dict(input_row)
                    parent = str(row.pop("_dq_parent", row.get("source_record_id") or ""))
                    scope = row.pop("_dq_scope", "row")
                    parent_by_source[str(row.get("source_record_id") or "")] = parent
                    scopes[scope].append(row)
                batch_issues = []
                for scope, rows in scopes.items():
                    if not rows:
                        continue
                    selected = [r for r in active_rules if _rule_scope(r) == scope]
                    if scope == "row" and dataset != "wdt_order_line_fact":
                        selected = active_rules
                    result = evaluate_rows(dataset, rows, selected, overrides=overrides,
                                           state=state)
                    counts["duplicates"] += result.duplicates
                    counts["unmapped"] += result.unmapped
                    batch_issues.extend(result.issues)
                blocked = {parent_by_source.get(issue.source_record_id, issue.source_record_id)
                           for issue in batch_issues if issue.severity in {"critical", "error"}}
                counts["records_quarantined"] += len(blocked)
                counts["records_passed"] += max(0, len(batch.source_record_ids) - len(blocked))
                counts["issue_count"] += len(batch_issues)
                for issue in batch_issues:
                    highest_severity = max(highest_severity,
                                           {"warning": 1, "error": 2, "critical": 3}[issue.severity])
                    issue_reasons.add(issue.reason)
                    issue_rule_types.add(issue.rule_type)
                    row = issue.as_row(run_id)
                    fingerprints.add(issue_fingerprint(row))
                    if len(samples) < 20:
                        row["fingerprint"] = issue_fingerprint(row)
                        samples.append(row)
                if persist:
                    self.repository.insert_issues(run_id, batch_issues)
            dataset_issues = self._dataset_rules(
                dataset, active_rules, counts, window_start, window_end,
            )
            if dataset_issues:
                counts["issue_count"] += len(dataset_issues)
                if any(issue.severity in {"critical", "error"} for issue in dataset_issues):
                    counts["records_quarantined"] = counts["records_read"]
                    counts["records_passed"] = 0
                for issue in dataset_issues:
                    highest_severity = max(highest_severity,
                                           {"warning": 1, "error": 2, "critical": 3}[issue.severity])
                    issue_reasons.add(issue.reason)
                    issue_rule_types.add(issue.rule_type)
                    fingerprints.add(issue_fingerprint(issue.as_row(run_id)))
                if persist:
                    self.repository.insert_issues(run_id, dataset_issues)
                samples.extend(issue.as_row(run_id) for issue in dataset_issues[:max(0, 20 - len(samples))])
            alert_status = {0: "normal", 1: "warning", 2: "error", 3: "blocked"}[highest_severity]
            metrics.update(
                sql_count=self.repository.sql_count + self.source.sql_count,
                db_ms=self.repository.db_ms + self.source.db_ms,
            )
            if persist and finish:
                self.repository.finish_run(run_id, status="finished", counts=counts,
                                           metrics=metrics, alert_status=alert_status)
            output = {"run_id": run_id, "dataset": dataset, "rule_version": version,
                      "status": "finished", "alert_status": alert_status, **counts,
                      **metrics, "samples": samples}
            if collect_fingerprints:
                output["_issue_fingerprints"] = fingerprints
                output["_issue_reasons"] = issue_reasons
                output["_issue_rule_types"] = issue_rule_types
            return output
        except Exception as exc:
            metrics.update(
                sql_count=self.repository.sql_count + self.source.sql_count,
                db_ms=self.repository.db_ms + self.source.db_ms,
            )
            if persist and finish:
                self.repository.finish_run(run_id, status="failed", counts=counts,
                                           metrics=metrics, failure_code=type(exc).__name__)
            raise

    def _dataset_rules(self, dataset, rules, counts, window_start, window_end):
        return evaluate_dataset_rules(
            dataset, rules, counts, window_start, window_end,
            channel_business_dates=getattr(self.source, "channel_business_dates", set()),
        )


RERUN_WORKER_LOCK_NAME = "public-data:data-quality:rerun"
_ISSUE_SURGE_MIN_COUNT = 10
_ISSUE_SURGE_RATIO = 2
_METRIC_FIELDS = ("sql_count", "db_ms", "batch_count", "max_batch_rows", "max_batch_bytes")
_POLICY_RULE_ERRORS = {
    "freshness rule requires a registered dataset policy",
    "reconcile rule requires a registered dataset policy",
}


class _RerunFailure(Exception):
    def __init__(self, failure_code):
        super().__init__(failure_code)
        self.failure_code = failure_code


class DataQualityRerunWorker:
    """Claim and execute one frozen rerun while holding a dedicated MySQL lock."""

    def __init__(self, *, repository, runner, replay_service, outbox=None,
                 lock_connection=None, lock_factory=None, now=None,
                 actor="dq-rerun-worker"):
        self.repository = repository
        self.runner = runner
        self.replay_service = replay_service
        self.outbox = outbox
        self.lock_connection = (
            lock_connection if lock_connection is not None
            else getattr(repository, "connection", None)
        )
        self.lock_factory = lock_factory or named_lock
        self.now = now or _utc_now
        self.actor = actor

    def run_once(self):
        guard = self.lock_factory(
            self.lock_connection, RERUN_WORKER_LOCK_NAME, timeout_seconds=0,
        )
        try:
            guard.__enter__()
        except LockUnavailable:
            return {"status": "skipped", "reason": "lock_unavailable", "run_id": None}
        try:
            result = self._run_next()
        except BaseException as exc:
            guard.__exit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            guard.__exit__(None, None, None)
            return result

    def _run_next(self):
        run = self.repository.claim_next_rerun()
        if run is None:
            return {"status": "idle", "run_id": None}
        if run.get("mode") != "rerun" or run.get("status") != "pending":
            return {
                "status": "skipped", "reason": "not_pending_rerun",
                "run_id": run.get("run_id"),
            }
        return self._process(run)

    def _process(self, run):
        run_id = run["run_id"]
        dry_run = run.get("dry_run") in (1, True, "1")
        dataset = run.get("dataset")
        window_start = window_end = None
        snapshot = quality = None
        counts = _empty_counts()
        metrics = self._metrics()
        alert_status = "normal"
        diff = sync_run_id = None
        issues_closed = 0
        fingerprint_match = None
        gate_blocked = False
        extra_alert_codes = set()
        stage = "validation"
        try:
            try:
                dataset = _validate_dataset(dataset)
            except DataQualityError:
                raise _RerunFailure("unsupported_dataset") from None
            try:
                window_start, window_end = _validate_window(
                    run.get("window_start"), run.get("window_end"),
                )
                if window_start is None:
                    raise DataQualityError("rerun requires a bounded business window")
            except DataQualityError:
                raise _RerunFailure("invalid_business_window") from None

            stage = "snapshot"
            try:
                snapshot = self.repository.get_run_snapshot(run_id)
            except DataQualityNotFound:
                raise _RerunFailure("rerun_snapshot_missing") from None
            except Exception:
                raise _RerunFailure("rerun_snapshot_unavailable") from None
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("run_id") != run_id
                or snapshot.get("dataset") != dataset
                or not isinstance(snapshot.get("rules"), list)
                or not isinstance(snapshot.get("overrides"), dict)
            ):
                raise _RerunFailure("invalid_rerun_snapshot")
            rules, overrides = snapshot["rules"], snapshot["overrides"]

            stage = "precheck"
            raw_batch_cache = (
                _WDTWindowRawCache()
                if dry_run and dataset == "wdt_order_line_fact" else None
            )
            precheck = self._quality_run(
                run, rules, overrides, raw_batch_cache=raw_batch_cache,
            )
            quality = precheck
            counts = self._counts(precheck)
            metrics = self._metrics(precheck)
            alert_status = precheck.get("alert_status", "normal")
            if self._gate_blocked(precheck):
                gate_blocked = True
                outcome = {"status": "failed", "failure_code": "quality_gate_blocked"}
            elif dry_run:
                stage = "dry_run_replay"
                raw_batches = (
                    raw_batch_cache.batches
                    if raw_batch_cache is not None and raw_batch_cache.complete
                    else None
                )
                replay = self._replay(
                    run, rules, overrides, dry_run=True, raw_batches=raw_batches,
                )
                diff = replay.get("diff")
                counts["records_written"] = 0
                sync_run_id = None
                outcome = {"status": "finished", "failure_code": None}
            else:
                stage = "publish"
                published = self._replay(run, rules, overrides, dry_run=False)
                sync_run_id = published.get("sync_run_id")
                diff = published.get("diff")
                records_written = int(published.get("records_written") or 0)
                counts["records_written"] = records_written

                stage = "postcheck"
                self.repository.delete_run_issues(run_id)
                postcheck = self._quality_run(run, rules, overrides)
                quality = postcheck
                counts = self._counts(postcheck, records_written=records_written)
                metrics = self._metrics(postcheck)
                alert_status = postcheck.get("alert_status", "normal")
                precheck_fingerprints = precheck.get("_issue_fingerprints")
                postcheck_fingerprints = postcheck.get("_issue_fingerprints")
                fingerprint_match = (
                    precheck_fingerprints is not None
                    and postcheck_fingerprints is not None
                    and set(precheck_fingerprints) == set(postcheck_fingerprints)
                )
                if self._gate_blocked(postcheck):
                    gate_blocked = True
                    outcome = {
                        "status": "failed",
                        "failure_code": "postcheck_quality_gate_blocked",
                    }
                else:
                    if fingerprint_match:
                        stage = "postcheck_close"
                        issues_closed = self.repository.close_resolved_issues(
                            run_id, actor=self.actor,
                        )
                    outcome = {"status": "finished", "failure_code": None}
        except _RerunFailure as exc:
            outcome = {"status": "failed", "failure_code": exc.failure_code}
        except Exception as exc:
            failure_codes = {
                "precheck": "precheck_failed",
                "dry_run_replay": "dry_run_replay_failed",
                "publish": "publish_failed",
                "postcheck": "postcheck_failed",
                "postcheck_close": "postcheck_close_failed",
            }
            failure_code = failure_codes.get(stage, "rerun_failed")
            if stage == "publish" and self._is_quality_gate_error(exc):
                failure_code, gate_blocked = "quality_gate_blocked", True
            if (
                stage in {"precheck", "postcheck"}
                and dataset == "wdt_order_line_fact"
                and str(exc) in _POLICY_RULE_ERRORS
            ):
                extra_alert_codes.add("wdt_quality_policy_failed")
            outcome = {"status": "failed", "failure_code": failure_code}

        return self._terminal(
            run, dry_run=dry_run, outcome=outcome, quality=quality,
            counts=counts, metrics=metrics, alert_status=alert_status,
            diff=diff, sync_run_id=sync_run_id, window_end=window_end,
            gate_blocked=gate_blocked, extra_alert_codes=extra_alert_codes,
            issues_closed=issues_closed, fingerprint_match=fingerprint_match,
        )

    def _quality_run(self, run, rules, overrides, *, raw_batch_cache=None):
        kwargs = {}
        if raw_batch_cache is not None:
            kwargs["raw_batch_cache"] = raw_batch_cache
        return self.runner.run(
            run["dataset"], mode="rerun",
            window_start=run.get("window_start"), window_end=run.get("window_end"),
            rules=rules, overrides=overrides, persist=True,
            requested_by=run.get("requested_by") or self.actor,
            reason=run.get("reason") or "", existing_run_id=run["run_id"],
            finish=False, collect_fingerprints=True, **kwargs,
        )

    def _replay(self, run, rules, overrides, *, dry_run, raw_batches=None):
        kwargs = {}
        if raw_batches is not None:
            kwargs["raw_batches"] = raw_batches
        return self.replay_service.replay_dataset(
            run["dataset"], window_start=run.get("window_start"),
            window_end=run.get("window_end"), dry_run=dry_run,
            rules=rules, overrides=overrides, **kwargs,
        )

    @staticmethod
    def _counts(result, *, records_written=None):
        counts = {
            key: int(result.get(key) or 0) for key in _empty_counts()
            if key != "records_written"
        }
        counts["records_written"] = int(
            result.get("records_written") or 0
            if records_written is None else records_written
        )
        return counts

    def _metrics(self, result=None):
        metrics = {key: int((result or {}).get(key) or 0) for key in _METRIC_FIELDS}
        if result is None:
            source = getattr(self.runner, "source", None)
            metrics["sql_count"] = int(
                getattr(self.repository, "sql_count", 0)
                + getattr(source, "sql_count", 0)
            )
            metrics["db_ms"] = int(
                getattr(self.repository, "db_ms", 0) + getattr(source, "db_ms", 0)
            )
        return metrics

    @staticmethod
    def _gate_blocked(result):
        return result.get("alert_status") in {"error", "blocked"}

    @staticmethod
    def _is_quality_gate_error(exc):
        return str(exc) == "quality_gate_blocked"

    def _terminal(self, run, *, dry_run, outcome, quality, counts, metrics,
                  alert_status, diff, sync_run_id, window_end, gate_blocked,
                  extra_alert_codes, issues_closed, fingerprint_match):
        run_id = run["run_id"]
        if gate_blocked and alert_status not in {"blocked", "error"}:
            alert_status = "blocked"
        self.repository.finish_run(
            run_id, status=outcome["status"], counts=counts, metrics=metrics,
            alert_status=alert_status, failure_code=outcome["failure_code"],
            diff=diff, sync_run_id=sync_run_id,
        )
        result = {
            "run_id": run_id, "dataset": run.get("dataset"),
            "status": outcome["status"], "failure_code": outcome["failure_code"],
            "alert_status": alert_status, "diff": diff, "sync_run_id": sync_run_id,
            "issues_closed": issues_closed, **counts, **metrics,
        }
        if fingerprint_match is not None:
            result["postcheck_fingerprint_match"] = fingerprint_match
        result.update(self._emit_alerts(
            run, dry_run=dry_run, quality=quality, counts=counts,
            window_end=window_end, gate_blocked=gate_blocked,
            extra_alert_codes=extra_alert_codes,
        ))
        return result

    def _emit_alerts(self, run, *, dry_run, quality, counts, window_end,
                     gate_blocked, extra_alert_codes):
        if self.outbox is None:
            return {}
        codes = set(extra_alert_codes)
        if gate_blocked and not dry_run:
            codes.add("quality_gate_blocked")
        if run.get("dataset") == "wdt_order_line_fact" and quality is not None:
            reasons = set(quality.get("_issue_reasons") or ())
            rule_types = set(quality.get("_issue_rule_types") or ())
            if "empty_source" in reasons:
                codes.add("wdt_empty_window")
            if rule_types.intersection({"freshness", "reconcile"}):
                codes.add("wdt_quality_policy_failed")

        baseline = None
        if counts["issue_count"] >= _ISSUE_SURGE_MIN_COUNT:
            try:
                baseline = self.repository.previous_issue_baseline(run["run_id"])
            except Exception:
                baseline = None
            if baseline is not None and self._issue_surge(
                counts["issue_count"], int(baseline["issue_count"]),
            ):
                codes.add("issue_surge")
        if not codes:
            return {}

        lines = [
            "### 数据质量重跑告警",
            f"- 数据集：{run['dataset']}",
            f"- 窗口：{run.get('window_start')} 至 {run.get('window_end')}",
            f"- 运行：{run['run_id']}",
            f"- 异常数：{counts['issue_count']}",
            f"- 告警原因：{', '.join(sorted(codes))}",
        ]
        if "issue_surge" in codes:
            lines.append(
                f"- 异常激增：上一基线 {baseline['issue_count']}，"
                f"阈值不少于 {_ISSUE_SURGE_MIN_COUNT} 且超过 {_ISSUE_SURGE_RATIO} 倍"
            )
        try:
            with transaction(self.repository.connection):
                enqueued = self.outbox.enqueue(
                    region="ops", kind="alert", business_date=window_end,
                    title="数据质量重跑告警", body_md="\n".join(lines),
                    created_at=self.now(), dedupe_suffix=f"dq-rerun:{run['run_id']}",
                )
        except Exception:
            return {"alert_enqueued": False, "alert_enqueue_failed": True}
        return {"alert_enqueued": bool(enqueued), "alert_enqueue_failed": False}

    @staticmethod
    def _issue_surge(issue_count, baseline_count):
        return (
            baseline_count >= 0
            and issue_count >= _ISSUE_SURGE_MIN_COUNT
            and issue_count > _ISSUE_SURGE_RATIO * baseline_count
        )
