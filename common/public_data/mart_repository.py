"""Mart repository for sync-run state tracking and dataset summaries."""

import contextlib

_VALID_TRANSITIONS = {
    "started": {"raw_committed", "failed"},
    "raw_committed": {"completed", "projection_pending", "failed"},
    "projection_pending": {"completed", "failed"},
}


class MartRepository:
    """Tracks sync-run lifecycle state and dataset summaries."""

    def __init__(self, connection):
        self._connection = connection
        self._statuses: dict[str, str] = {}

    # -- lifecycle -----------------------------------------------------------

    def start_run(self, *, sync_run_id, manifest_sha256, started_at):
        self._check_transition(sync_run_id, None, "started")
        sql = (
            "INSERT INTO `sync_runs` "
            "(`sync_run_id`, `status`, `manifest_sha256`, `started_at`) "
            "VALUES (%s, %s, %s, %s)"
        )
        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(
                sql, (sync_run_id, "started", manifest_sha256, started_at),
            )
        self._statuses[sync_run_id] = "started"

    def mark_raw_committed(self, sync_run_id):
        self._update_status(sync_run_id, "raw_committed")

    def mark_completed(self, sync_run_id, *, finished_at):
        self._update_status(sync_run_id, "completed", finished_at=finished_at)

    def mark_failed(self, sync_run_id, *, failure_code, finished_at):
        self._update_status(
            sync_run_id, "failed",
            failure_code=failure_code, finished_at=finished_at,
        )

    def mark_projection_pending(self, sync_run_id, *, failure_code=None, finished_at=None):
        self._update_status(
            sync_run_id, "projection_pending",
            failure_code=failure_code, finished_at=finished_at,
        )

    # -- dataset summary -----------------------------------------------------

    def save_dataset_summary(
        self,
        *,
        sync_run_id,
        source_name,
        dataset_name,
        records_read,
        raw_records_written,
        record_id_digest,
        completed_at,
    ):
        sql = (
            "INSERT INTO `sync_dataset_summary` "
            "(`sync_run_id`, `source_name`, `dataset_name`, "
            "`records_read`, `raw_records_written`, "
            "`record_id_digest`, `completed_at`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)"
        )
        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(
                sql,
                (sync_run_id, source_name, dataset_name,
                 records_read, raw_records_written,
                 record_id_digest, completed_at),
            )

    # -- internal ------------------------------------------------------------

    def _update_status(self, sync_run_id, new_status, **extra):
        current = self._statuses.get(sync_run_id, "started")
        self._check_transition(sync_run_id, current, new_status)

        if new_status in ("failed",):
            sql = (
                "UPDATE `sync_runs` SET `status` = %s, "
                "`failure_code` = %s, `finished_at` = %s "
                "WHERE `sync_run_id` = %s"
            )
            params = (
                new_status, extra.get("failure_code"),
                extra.get("finished_at"), sync_run_id,
            )
        elif new_status == "completed":
            sql = (
                "UPDATE `sync_runs` SET `status` = %s, "
                "`finished_at` = %s "
                "WHERE `sync_run_id` = %s"
            )
            params = (new_status, extra.get("finished_at"), sync_run_id)
        elif new_status == "projection_pending":
            sql = (
                "UPDATE `sync_runs` SET `status` = %s, "
                "`failure_code` = %s, `finished_at` = %s "
                "WHERE `sync_run_id` = %s"
            )
            params = (
                new_status, extra.get("failure_code"),
                extra.get("finished_at"), sync_run_id,
            )
        else:
            sql = (
                "UPDATE `sync_runs` SET `status` = %s "
                "WHERE `sync_run_id` = %s"
            )
            params = (new_status, sync_run_id)

        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(sql, params)
        self._statuses[sync_run_id] = new_status

    @staticmethod
    def _check_transition(sync_run_id, current, target):
        if current is None:
            # Inserting a new run — always allowed.
            return
        allowed = _VALID_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise ValueError(
                f"Invalid sync-run transition for {sync_run_id}: "
                f"{current!r} -> {target!r}"
            )
