"""统一新鲜度状态机：raw、mart、业务窗口、DQ 和源目标对账。"""

import contextlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from common.public_data.dataset_contract import DatasetContract

STATUS_FRESH = "fresh"
STATUS_DEGRADED = "degraded"
STATUS_STALE = "stale"
STATUS_BLOCKED = "blocked"
STATUS_UNKNOWN = "unknown"
ALL_STATUSES = (
    STATUS_FRESH, STATUS_DEGRADED, STATUS_STALE, STATUS_BLOCKED, STATUS_UNKNOWN,
)

_BJT = timezone(timedelta(hours=8))


@dataclass
class FreshnessResult:
    dataset_key: str
    status: str
    latest_business_date: Optional[date] = None
    last_collected_at: Optional[datetime] = None
    last_processed_at: Optional[datetime] = None
    last_published_at: Optional[datetime] = None
    lag_minutes: Optional[int] = None
    source_target_diff: Optional[dict] = None
    downstream_consumers: tuple = ()
    last_run_id: Optional[str] = None
    check_details: dict = field(default_factory=dict)
    error: Optional[str] = None


def _now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _expected_datetime(contract: DatasetContract, now: datetime):
    bj_now = now.replace(tzinfo=timezone.utc).astimezone(_BJT)
    expected_bj = bj_now.replace(
        hour=contract.expected_by.hour,
        minute=contract.expected_by.minute,
        second=0,
        microsecond=0,
    )
    if expected_bj > bj_now:
        expected_bj -= timedelta(days=1)
    return expected_bj.astimezone(timezone.utc).replace(tzinfo=None)


def evaluate_knowledge_sla(item, contract, now):
    """按当前时间重判知识索引，避免最后一次 fresh 永久残留。"""
    if not item:
        return STATUS_UNKNOWN, "not_checked"
    status = item.get("status") if item.get("status") in ALL_STATUSES else STATUS_UNKNOWN
    reference = _as_utc(item.get("checked_at") or item.get("last_success_at"))
    if reference is None:
        return STATUS_UNKNOWN, item.get("last_error") or "not_checked"
    lag_minutes = max(0, int((now - reference).total_seconds() // 60))
    hourly = contract.schedule.strip().split(" ", 1)[0].isdigit() and " " in contract.schedule
    hourly = hourly and contract.schedule.split()[1] == "*"
    if hourly:
        overdue = lag_minutes > contract.max_lag_minutes
    else:
        expected_utc = _expected_datetime(contract, now)
        deadline = expected_utc + timedelta(minutes=contract.max_lag_minutes)
        bj_expected = expected_utc.replace(tzinfo=timezone.utc).astimezone(_BJT)
        day_start = bj_expected.replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).astimezone(timezone.utc).replace(tzinfo=None)
        overdue = now > deadline and reference < day_start
    if overdue:
        return STATUS_STALE, "exceeded_max_lag"
    if status == STATUS_BLOCKED:
        return status, item.get("last_error") or "blocked"
    if status == STATUS_FRESH:
        return status, None
    return status, item.get("last_error") or status


def _previous_month(day):
    first = day.replace(day=1)
    return first - timedelta(days=1)


class FreshnessChecker:
    def __init__(self, *, connection, manual_connection=None, now=None):
        self._conn = connection
        self._manual_conn = manual_connection
        self._now = now or _now_utc

    def check(self, contract: DatasetContract) -> FreshnessResult:
        now = self._now()
        result = FreshnessResult(
            dataset_key=contract.dataset_key,
            status=STATUS_UNKNOWN,
            downstream_consumers=contract.downstream_consumers,
        )
        try:
            if contract.kind == "knowledge":
                result.error = "knowledge_contract_requires_index_checker"
            elif contract.kind == "manual":
                self._check_manual(contract, now, result)
            else:
                self._check_business_dataset(contract, now, result)
        except Exception as exc:
            result.status = STATUS_UNKNOWN
            result.error = type(exc).__name__
        return result

    def _check_business_dataset(self, contract, now, result):
        summary = self._latest_summary(contract.dataset_key, "extract")
        if summary is None:
            result.error = "no_extract_summary"
            return
        result.last_run_id = summary.get("sync_run_id")
        result.last_processed_at = _as_utc(summary.get("completed_at"))
        result.last_published_at = result.last_processed_at
        result.check_details["mart"] = {
            "run_status": summary.get("run_status"),
            "skipped": bool(summary.get("skipped")),
            "records_read": summary.get("records_read") or 0,
            "records_written": summary.get("raw_records_written") or 0,
        }
        if result.last_processed_at is None:
            result.error = "no_completion_time"
            return

        raw = self._latest_raw_summary(contract)
        if contract.raw_dataset and raw is None:
            result.error = "no_raw_summary"
            return
        if raw:
            result.last_collected_at = _as_utc(raw.get("completed_at"))
            result.check_details["raw"] = {
                "run_id": raw.get("sync_run_id"),
                "run_status": raw.get("run_status"),
                "records_read": raw.get("records_read") or 0,
                "records_written": raw.get("raw_records_written") or 0,
            }

        expected_utc = _expected_datetime(contract, now)
        deadline = expected_utc + timedelta(minutes=contract.max_lag_minutes)
        result.lag_minutes = max(
            0, int((now - result.last_processed_at).total_seconds() // 60)
        )
        result.latest_business_date = self._latest_business_date(contract, now)
        window = self._check_business_window(contract, now)
        quality = self._latest_quality(contract) if contract.dq_rules else None
        diff = self._source_target_diff(contract, summary, raw)
        result.source_target_diff = diff
        result.check_details.update({"window": window, "dq": quality})

        run_failed = (
            summary.get("run_status") in {"failed", "projection_pending"}
            or summary.get("failure_code")
        )
        quality_blocked = quality and (
            quality.get("status") == "failed"
            or quality.get("alert_status") in {"error", "blocked"}
        )
        if run_failed or quality_blocked:
            result.status = STATUS_BLOCKED
            result.error = (
                summary.get("failure_code")
                or (quality or {}).get("failure_code")
                or "quality_gate_blocked"
            )
            return
        if contract.dq_rules and quality is None:
            result.error = "no_quality_run"
            return
        if diff.get("null_business_keys"):
            result.status = STATUS_BLOCKED
            result.error = "null_business_key"
            return

        overdue = now > deadline
        missing_window = (
            overdue and window.get("required") and not window.get("complete")
        )
        mart_stale = overdue and result.last_processed_at < expected_utc
        raw_stale = bool(
            overdue
            and result.last_collected_at
            and result.last_collected_at < expected_utc
        )
        if missing_window or mart_stale or raw_stale:
            result.status = STATUS_STALE
            result.error = (
                window.get("reason") if missing_window
                else "exceeded_max_lag"
            )
            return

        if raw and raw.get("run_status") == "failed":
            result.status = STATUS_DEGRADED
            result.error = raw.get("failure_code") or "raw_run_failed"
            return
        if (
            raw
            and result.last_collected_at
            and result.last_collected_at > result.last_processed_at
        ):
            result.status = STATUS_DEGRADED
            result.error = "mart_behind_raw"
            return
        if diff.get("record_count_matches") is False:
            result.status = STATUS_DEGRADED
            result.error = "source_target_count_mismatch"
            return
        result.status = STATUS_FRESH

    def _check_manual(self, contract, now, result):
        if self._manual_conn is None:
            result.error = "manual_connection_unavailable"
            return
        run = self._latest_manual_run(contract.dataset_key)
        if run is None:
            result.error = "no_manual_import"
            return
        result.last_run_id = run.get("run_id")
        result.last_collected_at = _as_utc(run.get("imported_at"))
        result.last_processed_at = result.last_collected_at
        result.last_published_at = self._manual_published_at(contract.dataset_key)
        result.latest_business_date = self._latest_business_date(contract, now)
        result.lag_minutes = max(
            0, int((now - result.last_collected_at).total_seconds() // 60)
        )

        bj_today = now.replace(tzinfo=timezone.utc).astimezone(_BJT).date()
        expected_period = _previous_month(bj_today).replace(day=1)
        row_count = self._manual_period_count(contract.dataset_key, expected_period)
        deadline_bj = datetime(
            bj_today.year, bj_today.month, contract.expected_day,
            contract.expected_by.hour, contract.expected_by.minute,
            tzinfo=_BJT,
        )
        deadline = (
            deadline_bj.astimezone(timezone.utc).replace(tzinfo=None)
            + timedelta(minutes=contract.max_lag_minutes)
        )
        result.check_details.update({
            "raw": {"status": run.get("status"), "rows_ok": run.get("rows_ok")},
            "mart": {"period_start": str(expected_period), "rows": row_count},
            "window": {
                "required": True,
                "complete": row_count > 0,
                "target_period": str(expected_period),
            },
        })
        result.source_target_diff = {
            "source_rows": run.get("rows_ok") or 0,
            "target_rows": row_count,
        }
        if run.get("status") == "rejected":
            result.status = STATUS_BLOCKED
            result.error = "manual_import_rejected"
        elif row_count == 0:
            result.status = STATUS_STALE if now > deadline else STATUS_UNKNOWN
            result.error = "missing_previous_month"
        else:
            result.status = STATUS_FRESH

    def _latest_summary(self, dataset_key, source_name):
        sql = (
            "SELECT s.*, r.`status` AS `run_status`, r.`failure_code` "
            "FROM `sync_dataset_summary` s "
            "JOIN `sync_runs` r ON r.`sync_run_id` = s.`sync_run_id` "
            "WHERE s.`source_name` = %s AND s.`dataset_name` = %s "
            "ORDER BY s.`completed_at` DESC LIMIT 1"
        )
        return self._fetch_one(self._conn, sql, (source_name, dataset_key))

    def _latest_raw_summary(self, contract):
        if not contract.raw_dataset or contract.source not in {"dingtalk", "wdt"}:
            return None
        sql = (
            "SELECT s.*, r.`status` AS `run_status`, r.`failure_code` "
            "FROM `sync_dataset_summary` s "
            "JOIN `sync_runs` r ON r.`sync_run_id` = s.`sync_run_id` "
            "WHERE s.`source_name` = %s AND s.`dataset_name` LIKE %s "
            "ORDER BY s.`completed_at` DESC LIMIT 1"
        )
        return self._fetch_one(
            self._conn, sql, (contract.source, contract.raw_dataset)
        )

    def _latest_business_date(self, contract, now):
        if not contract.target_table or not contract.business_date_column:
            return None
        bj_today = now.replace(tzinfo=timezone.utc).astimezone(_BJT).date()
        if contract.kind == "manual":
            sql = (
                f"SELECT MAX(`{contract.business_date_column}`) AS `latest` "
                f"FROM `{contract.target_table}` WHERE `dataset` = %s "
                f"AND `{contract.business_date_column}` <= %s"
            )
            params = (contract.dataset_key, bj_today)
        else:
            sql = (
                f"SELECT MAX(`{contract.business_date_column}`) AS `latest` "
                f"FROM `{contract.target_table}` "
                f"WHERE `{contract.business_date_column}` <= %s"
            )
            params = (bj_today,)
        row = self._fetch_one(self._conn, sql, params)
        return _as_date((row or {}).get("latest"))

    def _check_business_window(self, contract, now):
        if contract.required_window != "t1":
            return {"required": False, "complete": True}
        target = now.replace(tzinfo=timezone.utc).astimezone(_BJT).date() - timedelta(days=1)
        details = {
            "required": True,
            "target_date": str(target),
            "complete": False,
            "reason": "missing_t1_business_date",
        }
        if not contract.target_table or not contract.business_date_column:
            details["reason"] = "window_contract_incomplete"
            return details
        sql = (
            f"SELECT 1 AS `hit` FROM `{contract.target_table}` "
            f"WHERE `{contract.business_date_column}` = %s LIMIT 1"
        )
        exists = self._fetch_one(self._conn, sql, (target,)) is not None
        missing_regions = []
        if exists and contract.expected_scope and contract.expected_scope.startswith("regions:"):
            expected = set(contract.expected_scope.split(":", 1)[1].split(","))
            sql = (
                f"SELECT DISTINCT `region` FROM `{contract.target_table}` "
                f"WHERE `{contract.business_date_column}` = %s"
            )
            with contextlib.closing(self._conn.cursor()) as cursor:
                cursor.execute(sql, (target,))
                actual = {str(row.get("region")) for row in cursor.fetchall()}
            missing_regions = sorted(expected - actual)
        details.update({
            "complete": exists and not missing_regions,
            "missing_regions": missing_regions,
        })
        if missing_regions:
            details["reason"] = "missing_expected_scope"
        return details

    def _latest_quality(self, contract):
        sql = (
            "SELECT `run_id`, `status`, `alert_status`, `failure_code`, "
            "`records_read`, `records_passed`, `records_quarantined`, "
            "`issue_count`, `finished_at` FROM `dq_run` "
            "WHERE `dataset` = %s AND `dry_run` = 0 "
            "ORDER BY `started_at` DESC LIMIT 1"
        )
        return self._fetch_one(self._conn, sql, (contract.dataset_key,))

    def _source_target_diff(self, contract, mart, raw):
        diff = {
            "source_records": (raw or {}).get("records_read"),
            "processed_records": mart.get("records_read") or 0,
            "published_records": mart.get("raw_records_written") or 0,
            "null_business_keys": 0,
        }
        if (
            raw
            and "record_count" in contract.reconcile_rules
            and "%" not in (contract.raw_dataset or "")
        ):
            diff["record_count_matches"] = (
                int(raw.get("records_read") or 0)
                == int(mart.get("records_read") or 0)
            )
            if diff["record_count_matches"] and int(mart.get("records_read") or 0) > 0:
                diff["record_count_matches"] = int(
                    mart.get("raw_records_written") or 0
                ) > 0
        if contract.target_table and contract.business_key_columns:
            null_checks = ", ".join(
                f"COALESCE(SUM(`{key}` IS NULL), 0) AS `{key}_nulls`"
                for key in contract.business_key_columns
            )
            row = self._fetch_one(
                self._conn,
                f"SELECT {null_checks} FROM `{contract.target_table}`",
                (),
            ) or {}
            diff["null_business_keys"] = sum(
                int(row.get(f"{key}_nulls") or 0)
                for key in contract.business_key_columns
            )
        return diff

    def _latest_manual_run(self, dataset_key):
        return self._fetch_one(
            self._manual_conn,
            "SELECT `run_id`, `status`, `rows_ok`, `rows_bad`, `period`, "
            "`imported_at` FROM `manual_import_runs` WHERE `dataset` = %s "
            "ORDER BY `imported_at` DESC LIMIT 1",
            (dataset_key,),
        )

    def _manual_published_at(self, dataset_key):
        row = self._fetch_one(
            self._conn,
            "SELECT MAX(`synced_at`) AS `published_at` FROM `fact_manual_report` "
            "WHERE `dataset` = %s",
            (dataset_key,),
        )
        return _as_utc((row or {}).get("published_at"))

    def _manual_period_count(self, dataset_key, period_start):
        row = self._fetch_one(
            self._conn,
            "SELECT COUNT(*) AS `n` FROM `fact_manual_report` "
            "WHERE `dataset` = %s AND `period_start` = %s",
            (dataset_key, period_start),
        )
        return int((row or {}).get("n") or 0)

    @staticmethod
    def _fetch_one(connection, sql, params):
        with contextlib.closing(connection.cursor()) as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
        return dict(row) if row else None
