"""Orchestration service for restricted live source sync."""

import hashlib
from dataclasses import dataclass, field

from common.public_data.db import named_lock, transaction
from common.public_data.dingtalk_read import DingTalkReadError
from common.public_data.org_read import OrgReadError, collect_members
from common.public_data.transforms import apply_transform
from common.public_data.wdt_read import PaginationLimitExceeded, WdtReadError


def _transform_field_mapping(transform):
    """Return field mapping entries for transform-generated fields.

    These are synthetic source names (not real DingTalk fields) that the
    transform injects into each record.  They map to themselves because
    the post-transform field keys already match the target column names.
    """
    kind = transform["type"]
    if kind == "melt":
        mapping = {"business_date": "business_date",
                   transform["value_column"]: transform["value_column"]}
        for key in transform.get("inject", {}):
            mapping[key] = key
        return mapping
    if kind == "inject":
        return {key: key for key in transform["values"]}
    return {}


class LiveSyncError(RuntimeError):
    """Raised when the live sync encounters an unrecoverable error."""


class _ProjectionFailure(Exception):
    """Internal marker: raw was committed but the mart summary step failed.

    This is NOT part of the public API.  It exists solely so that the
    outer ``sync`` method can distinguish a post-commit projection
    failure from a source-read or raw-write failure and choose the
    correct run-state transition.
    """


@dataclass(frozen=True)
class SyncResult:
    """Summary returned after a sync run completes or is recovered."""

    run_id: str
    datasets: list = field(default_factory=list)


class LiveSyncService:
    """Coordinates the full restricted sync flow.

    All dependencies are injected so that the service can be exercised in
    tests without real MySQL connections, HTTP clients, or credentials.
    """

    def __init__(
        self,
        *,
        dingtalk_gateway,
        wdt_gateway,
        dingtalk_repository,
        wdt_repository,
        mart_repository,
        connections,
        now,
        new_run_id,
        org_gateway=None,
        org_repository=None,
        org_regions=(),
    ):
        self._dingtalk_gateway = dingtalk_gateway
        self._wdt_gateway = wdt_gateway
        self._dingtalk_repo = dingtalk_repository
        self._wdt_repo = wdt_repository
        self._mart_repo = mart_repository
        self._connections = connections
        self._now = now
        self._new_run_id = new_run_id
        self._org_gateway = org_gateway
        self._org_repo = org_repository
        self._org_regions = tuple(org_regions)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync(self, manifest, source=None) -> SyncResult:
        """Execute a full sync run described by *manifest*.

        *source* may be ``"dingtalk"`` or ``"wdt"`` to restrict the run to a
        single source line (used by the per-line ``sync-dingtalk`` /
        ``sync-wdt`` runners); ``None`` syncs every dataset in the manifest.
        Each invocation is its own run with its own ``sync_run_id``, so the
        two lines fail independently.

        Steps for every selected dataset:

        1. (DingTalk only) validate sheet schema against remote.
        2. Acquire a MySQL named lock for the dataset.
        3. Read records from the source gateway.
        4. Persist records to the raw database inside a transaction.
        5. Mark the run state as ``raw_committed``.
        6. Save a dataset summary in the mart.

        If all selected datasets succeed the run is marked *completed*.  On
        any exception the run is marked *failed* with a mapped failure code,
        or *projection_pending* if only the mart summary step failed.
        """
        if source not in (None, "dingtalk", "wdt"):
            raise LiveSyncError(f"unknown source: {source!r}")
        if source in (None, "dingtalk") and self._dingtalk_gateway is None:
            raise LiveSyncError("dingtalk gateway is not configured")
        if source in (None, "wdt") and self._wdt_gateway is None:
            raise LiveSyncError("wdt gateway is not configured")

        run_id = self._new_run_id()
        started_at = self._now()

        self._mart_repo.start_run(
            sync_run_id=run_id,
            manifest_sha256=manifest.sha256,
            started_at=started_at,
        )
        mart_conn = self._connections.mart
        mart_conn.commit()

        datasets_summary = []

        try:
            if source in (None, "dingtalk"):
                for sheet in manifest.dingtalk_sheets:
                    summary = self._sync_dingtalk_sheet(sheet, run_id)
                    datasets_summary.append(summary)
                if manifest.dingtalk_org is not None:
                    summary = self._sync_org_dataset(manifest.dingtalk_org, run_id)
                    datasets_summary.append(summary)

            if source in (None, "wdt"):
                for dataset in manifest.wdt_datasets:
                    summary = self._sync_wdt_dataset(dataset, run_id)
                    datasets_summary.append(summary)

            self._mart_repo.mark_completed(
                sync_run_id=run_id,
                finished_at=self._now(),
            )
            mart_conn.commit()

        except _ProjectionFailure as exc:
            self._mart_repo.mark_projection_pending(
                sync_run_id=run_id,
                failure_code="projection_failed",
                finished_at=self._now(),
            )
            mart_conn.commit()
            raise exc.__cause__ from None

        except Exception as exc:
            mart_conn.rollback()
            self._mart_repo.mark_failed(
                sync_run_id=run_id,
                failure_code=self._failure_code(exc),
                finished_at=self._now(),
            )
            mart_conn.commit()
            raise

        return SyncResult(run_id=run_id, datasets=datasets_summary)

    def rebuild_projection(self, sync_run_id, manifest, source=None) -> SyncResult:
        """Recover a ``projection_pending`` run without re-reading sources.

        Queries the raw repositories for the records already persisted,
        recomputes per-dataset digests, writes mart summaries, and marks
        the run *completed*.  *source* restricts the rebuild to a single
        source line, matching how the run was produced.

        No gateway method is invoked — this is a pure raw-to-mart repair.
        """
        if source not in (None, "dingtalk", "wdt"):
            raise LiveSyncError(f"unknown source: {source!r}")

        self._mart_repo.load_run_status(sync_run_id)
        datasets_summary = []

        if source in (None, "dingtalk"):
            for sheet in manifest.dingtalk_sheets:
                records = self._dingtalk_repo.summary_for_run(
                    sheet.target_table, sync_run_id,
                )
                record_ids = [r["source_record_id"] for r in records]
                digest = self._compute_digest(record_ids)

                self._mart_repo.save_dataset_summary(
                    sync_run_id=sync_run_id,
                    source_name="dingtalk",
                    dataset_name=sheet.dataset,
                    records_read=len(records),
                    raw_records_written=len(records),
                    record_id_digest=digest,
                    completed_at=self._now(),
                )
                datasets_summary.append({
                    "source": "dingtalk",
                    "dataset": sheet.dataset,
                    "records_read": len(records),
                })

            if manifest.dingtalk_org is not None:
                rows = self._org_repo.summary_for_run(sync_run_id)
                record_ids = [r["source_record_id"] for r in rows]
                digest = self._compute_digest(record_ids)

                self._mart_repo.save_dataset_summary(
                    sync_run_id=sync_run_id,
                    source_name="dingtalk",
                    dataset_name=manifest.dingtalk_org.dataset,
                    records_read=len(rows),
                    raw_records_written=len(rows),
                    record_id_digest=digest,
                    completed_at=self._now(),
                )
                datasets_summary.append({
                    "source": "dingtalk",
                    "dataset": manifest.dingtalk_org.dataset,
                    "records_read": len(rows),
                })

        if source in (None, "wdt"):
            for dataset in manifest.wdt_datasets:
                rows = self._wdt_repo.summary_for_run(sync_run_id)
                stable_ids = [r["source_record_id"] for r in rows]
                digest = self._compute_digest(stable_ids)

                self._mart_repo.save_dataset_summary(
                    sync_run_id=sync_run_id,
                    source_name="wdt",
                    dataset_name=dataset.dataset,
                    records_read=len(rows),
                    raw_records_written=len(rows),
                    record_id_digest=digest,
                    completed_at=self._now(),
                )
                datasets_summary.append({
                    "source": "wdt",
                    "dataset": dataset.dataset,
                    "records_read": len(rows),
                })

        self._mart_repo.mark_completed(
            sync_run_id=sync_run_id,
            finished_at=self._now(),
        )
        self._connections.mart.commit()

        return SyncResult(run_id=sync_run_id, datasets=datasets_summary)

    # ------------------------------------------------------------------
    # Per-source sync helpers
    # ------------------------------------------------------------------

    def _sync_dingtalk_sheet(self, sheet, run_id):
        # 1. Validate schema against remote (may raise DingTalkReadError).
        self._dingtalk_gateway.validate_sheet(sheet)

        # 2. Acquire lock, then read.
        conn = self._connections.dingtalk
        lock_name = f"public-data:dingtalk:{sheet.dataset}"

        with named_lock(conn, lock_name):
            records = self._dingtalk_gateway.read_records(sheet)

        # 3. Apply transform if configured (melt / inject).
        if sheet.transform:
            records = apply_transform(records, sheet.transform)

        # 4. Build field mapping and persist raw records.
        field_mapping = {f.source_name: f.column for f in sheet.fields}
        if sheet.transform:
            field_mapping.update(
                _transform_field_mapping(sheet.transform)
            )

        with transaction(conn):
            self._dingtalk_repo.upsert_records(
                sheet.target_table,
                records,
                field_mapping=field_mapping,
                sync_run_id=run_id,
                synced_at=self._now(),
            )

        # 5. Advance state.
        self._mart_repo.mark_raw_committed(run_id)

        # 6. Save summary — if this fails, wrap as _ProjectionFailure so
        #    the outer handler knows raw was already committed.
        try:
            return self._save_dingtalk_summary(sheet, records, run_id)
        except Exception as exc:
            raise _ProjectionFailure() from exc

    def _sync_wdt_dataset(self, dataset, run_id):
        # 1. Acquire lock, then read (no schema validation for WDT).
        conn = self._connections.wdt
        lock_name = f"public-data:wdt:{dataset.dataset}"

        with named_lock(conn, lock_name):
            records = self._wdt_gateway.read_dataset(dataset)

        # 2. Persist raw records inside a transaction.
        with transaction(conn):
            self._wdt_repo.upsert_records(
                dataset.method,
                records,
                window_start=dataset.window_start,
                window_end=dataset.window_end,
                sync_run_id=run_id,
                synced_at=self._now(),
            )

        # 3. Advance state.
        self._mart_repo.mark_raw_committed(run_id)

        # 4. Save summary — wrap failures as _ProjectionFailure.
        try:
            return self._save_wdt_summary(dataset, records, run_id)
        except Exception as exc:
            raise _ProjectionFailure() from exc

    def _sync_org_dataset(self, org_dataset, run_id):
        # The manifest declared the directory sync, so a missing gateway or
        # seed is a misconfiguration — fail loudly, never silently skip.
        if self._org_gateway is None:
            raise LiveSyncError("org gateway is not configured")
        if not self._org_regions:
            raise LiveSyncError("org regions are not configured")

        conn = self._connections.dingtalk
        lock_name = f"public-data:dingtalk:{org_dataset.dataset}"

        with named_lock(conn, lock_name):
            members = collect_members(self._org_gateway, self._org_regions)

        with transaction(conn):
            self._org_repo.upsert_members(
                members,
                sync_run_id=run_id,
                synced_at=self._now(),
            )

        self._mart_repo.mark_raw_committed(run_id)

        try:
            return self._save_org_summary(org_dataset, members, run_id)
        except Exception as exc:
            raise _ProjectionFailure() from exc

    # ------------------------------------------------------------------
    # Summary helpers
    # ------------------------------------------------------------------

    def _save_dingtalk_summary(self, sheet, records, run_id):
        record_ids = [r["id"] for r in records]
        digest = self._compute_digest(record_ids)

        self._mart_repo.save_dataset_summary(
            sync_run_id=run_id,
            source_name="dingtalk",
            dataset_name=sheet.dataset,
            records_read=len(records),
            raw_records_written=len(records),
            record_id_digest=digest,
            completed_at=self._now(),
        )

        return {
            "source": "dingtalk",
            "dataset": sheet.dataset,
            "records_read": len(records),
            "raw_records_written": len(records),
        }

    def _save_org_summary(self, org_dataset, members, run_id):
        record_ids = [member.user_id for member in members]
        digest = self._compute_digest(record_ids)

        self._mart_repo.save_dataset_summary(
            sync_run_id=run_id,
            source_name="dingtalk",
            dataset_name=org_dataset.dataset,
            records_read=len(members),
            raw_records_written=len(members),
            record_id_digest=digest,
            completed_at=self._now(),
        )

        return {
            "source": "dingtalk",
            "dataset": org_dataset.dataset,
            "records_read": len(members),
            "raw_records_written": len(members),
        }

    def _save_wdt_summary(self, dataset, records, run_id):
        stable_ids = [stable_id for _, stable_id in records]
        digest = self._compute_digest(stable_ids)

        self._mart_repo.save_dataset_summary(
            sync_run_id=run_id,
            source_name="wdt",
            dataset_name=dataset.dataset,
            records_read=len(records),
            raw_records_written=len(records),
            record_id_digest=digest,
            completed_at=self._now(),
        )

        return {
            "source": "wdt",
            "dataset": dataset.dataset,
            "records_read": len(records),
            "raw_records_written": len(records),
        }

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_digest(ids) -> str:
        return hashlib.sha256(
            "\n".join(sorted(ids)).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _failure_code(exc) -> str:
        if isinstance(exc, DingTalkReadError):
            return "schema_drift"
        if isinstance(exc, PaginationLimitExceeded):
            return "pagination_incomplete"
        if isinstance(exc, (WdtReadError, OrgReadError)):
            return "source_read_failed"
        if isinstance(exc, LiveSyncError):
            return str(exc)
        return "source_read_failed"
