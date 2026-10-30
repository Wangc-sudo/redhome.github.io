"""数据集级运行追踪，关联 sync_runs、sync_dataset_summary 和运行流水。"""

import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from common.public_data.dataset_contract import CONTRACT_BY_KEY


@dataclass
class DatasetRunStatus:
    dataset_key: str
    sync_run_id: Optional[str] = None
    status: str = "unknown"
    records_read: int = 0
    records_written: int = 0
    skipped: bool = False
    failure_code: Optional[str] = None
    completed_at: Optional[datetime] = None
    downstream_consumers: tuple = ()


@dataclass
class RunTrackingResult:
    run_id: str
    service_id: Optional[str] = None
    trigger_type: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    returncode: Optional[int] = None
    datasets: list = field(default_factory=list)

    @property
    def published(self):
        return [item for item in self.datasets if item.status == "published"]

    @property
    def old_version(self):
        return [item for item in self.datasets if item.status == "old_version"]

    @property
    def failed(self):
        return [item for item in self.datasets if item.status == "failed"]

    @property
    def blocked(self):
        return [item for item in self.datasets if item.status == "blocked"]

    @property
    def pending_replay(self):
        return [item for item in self.datasets if item.status == "pending_replay"]


class RunTrackingRepository:
    def __init__(self, connection):
        self._conn = connection

    def track_run(self, sync_run_id, *, expected_datasets=None) -> RunTrackingResult:
        result = RunTrackingResult(run_id=sync_run_id)
        run = self._load_sync_run(sync_run_id)
        if run:
            result.started_at = run.get("started_at")
            result.finished_at = run.get("finished_at")
        result.datasets = self._load_dataset_statuses(
            sync_run_id, run or {}, expected_datasets=expected_datasets,
        )
        return result

    def track_ops_run(self, history_id) -> RunTrackingResult:
        with contextlib.closing(self._conn.cursor()) as cursor:
            cursor.execute(
                "SELECT * FROM `pd_ops_run_history` WHERE `id` = %s",
                (history_id,),
            )
            row = cursor.fetchone()
        if not row:
            return RunTrackingResult(run_id=str(history_id))
        return RunTrackingResult(
            run_id=str(history_id),
            service_id=row.get("service_id"),
            trigger_type=row.get("trigger_type"),
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
            returncode=row.get("exit_code"),
        )

    def _load_sync_run(self, sync_run_id):
        with contextlib.closing(self._conn.cursor()) as cursor:
            cursor.execute(
                "SELECT * FROM `sync_runs` WHERE `sync_run_id` = %s",
                (sync_run_id,),
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def _load_dataset_statuses(self, sync_run_id, run, *, expected_datasets=None):
        sql = (
            "SELECT s.*, r.`status` AS `run_status`, "
            "r.`failure_code` AS `run_failure_code` "
            "FROM `sync_dataset_summary` s "
            "JOIN `sync_runs` r ON r.`sync_run_id` = s.`sync_run_id` "
            "WHERE s.`sync_run_id` = %s ORDER BY s.`dataset_name`"
        )
        with contextlib.closing(self._conn.cursor()) as cursor:
            cursor.execute(sql, (sync_run_id,))
            rows = cursor.fetchall()
        statuses = []
        seen = set()
        for row in rows:
            row = dict(row)
            dataset_key = row["dataset_name"]
            seen.add(dataset_key)
            contract = CONTRACT_BY_KEY.get(dataset_key)
            run_status = row.get("run_status")
            failure_code = row.get("run_failure_code")
            if run_status == "projection_pending":
                status = "pending_replay"
            elif run_status == "failed" or failure_code:
                status = "failed"
            elif row.get("skipped"):
                status = "old_version"
            else:
                status = "published"
            statuses.append(DatasetRunStatus(
                dataset_key=dataset_key,
                sync_run_id=sync_run_id,
                status=status,
                records_read=row.get("records_read") or 0,
                records_written=row.get("raw_records_written") or 0,
                skipped=bool(row.get("skipped")),
                failure_code=failure_code,
                completed_at=row.get("completed_at"),
                downstream_consumers=contract.downstream_consumers if contract else (),
            ))
        for dataset_key in expected_datasets or ():
            if dataset_key in seen:
                continue
            contract = CONTRACT_BY_KEY.get(dataset_key)
            run_status = run.get("status")
            status = {
                "failed": "failed",
                "projection_pending": "pending_replay",
            }.get(run_status, "old_version")
            statuses.append(DatasetRunStatus(
                dataset_key=dataset_key,
                sync_run_id=sync_run_id,
                status=status,
                failure_code=run.get("failure_code"),
                downstream_consumers=contract.downstream_consumers if contract else (),
            ))
        return statuses

    def recent_runs(self, *, dataset_key=None, limit=20):
        conditions = ["s.`source_name` = 'extract'"]
        params = []
        if dataset_key:
            conditions.append("s.`dataset_name` = %s")
            params.append(dataset_key)
        sql = (
            "SELECT r.`sync_run_id`, r.`started_at`, r.`finished_at`, r.`status`, "
            "r.`failure_code`, s.`dataset_name`, s.`records_read`, "
            "s.`raw_records_written`, s.`skipped`, s.`completed_at` "
            "FROM `sync_runs` r "
            "JOIN `sync_dataset_summary` s ON s.`sync_run_id` = r.`sync_run_id` "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY r.`started_at` DESC LIMIT %s"
        )
        params.append(max(1, min(int(limit), 100)))
        with contextlib.closing(self._conn.cursor()) as cursor:
            cursor.execute(sql, tuple(params))
            return [dict(row) for row in cursor.fetchall()]
