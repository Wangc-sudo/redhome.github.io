"""导入编排：校验 → raw_manual → mart_ops。

服务只依赖两个仓储接口（``repository`` / ``projector``），不认识
pymysql，也不认识 CLI——单测用内存 fake 就能跑完整条链路。

四种终态（都写进 ``manual_import_runs.status``）：

===========  ===================================================
``imported`` 校验通过并已落库
``dry_run``  只校验，未落库（默认路径，``run_id`` 仅为本次会话标识）
``duplicate`` 同文件重复导入，一行未写
``rejected`` 校验有 error，整批拒绝（不半截入库）
===========  ===================================================
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from common.public_data.manual_import.projector import build_fact_rows
from common.public_data.manual_import.repository import ManualRow
from common.public_data.manual_import.validate import ValidationReport, validate_table

STATUS_DRY_RUN = "dry_run"
STATUS_IMPORTED = "imported"
STATUS_DUPLICATE = "duplicate"
STATUS_REJECTED = "rejected"


@dataclass(frozen=True)
class ManualImportResult:
    """一次导入的结果摘要（CLI 只打印这些字段）。"""

    run_id: str
    dataset: str
    period: str
    rows_ok: int
    rows_bad: int
    status: str
    report: ValidationReport | None = None


class ManualImportService:
    """人工报表导入编排。"""

    def __init__(self, repository=None, projector=None, *, now=None, new_run_id=None):
        self._repository = repository
        self._projector = projector
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._new_run_id = new_run_id or (lambda: str(uuid.uuid4()))

    # -- dry-run ------------------------------------------------------------

    def dry_run(self, template, table, period) -> ManualImportResult:
        """只校验不落库；``run_id`` 为 ``-``（未产生任何 run）。"""
        report = validate_table(table, template, period)
        return ManualImportResult(
            run_id="-",
            dataset=template.dataset,
            period=report.period,
            rows_ok=report.rows_ok,
            rows_bad=report.rows_bad,
            status=STATUS_DRY_RUN if report.ok else STATUS_REJECTED,
            report=report,
        )

    # -- apply --------------------------------------------------------------

    def apply(
        self,
        template,
        table,
        period,
        *,
        imported_by: str = "cli",
    ) -> ManualImportResult:
        if self._repository is None or self._projector is None:
            raise RuntimeError("manual import service has no repository/projector")

        report = validate_table(table, template, period)
        run_id = self._new_run_id()
        now = self._now()

        if not report.ok:
            self._repository.start_run(
                run_id=run_id,
                dataset=template.dataset,
                template_version=template.version,
                file_name=table.file_name,
                file_sha256=table.sha256,
                period=report.period,
                status=STATUS_REJECTED,
                imported_by=imported_by,
                imported_at=now,
            )
            self._repository.finish_run(
                run_id,
                rows_ok=0,
                rows_bad=report.rows_bad,
                status=STATUS_REJECTED,
            )
            return ManualImportResult(
                run_id=run_id,
                dataset=template.dataset,
                period=report.period,
                rows_ok=0,
                rows_bad=report.rows_bad,
                status=STATUS_REJECTED,
                report=report,
            )

        # 同文件重复导入：一行不写，返回上一次的 run_id。
        existing = self._repository.find_run(
            template.dataset, report.period, table.sha256
        )
        if existing is not None:
            return ManualImportResult(
                run_id=existing.run_id,
                dataset=template.dataset,
                period=report.period,
                rows_ok=report.rows_ok,
                rows_bad=report.rows_bad,
                status=STATUS_DUPLICATE,
                report=report,
            )

        source_by_row = {row.row_no: row.values for row in table.rows}
        payload = tuple(
            ManualRow(
                row_no=row.row_no,
                fields=dict(row.fields),
                source=dict(source_by_row.get(row.row_no, {})),
            )
            for row in report.rows
        )

        self._repository.start_run(
            run_id=run_id,
            dataset=template.dataset,
            template_version=template.version,
            file_name=table.file_name,
            file_sha256=table.sha256,
            period=report.period,
            status=STATUS_IMPORTED,
            imported_by=imported_by,
            imported_at=now,
        )
        self._repository.replace_rows(
            run_id=run_id,
            dataset=template.dataset,
            period=report.period,
            rows=payload,
            imported_at=now,
        )
        facts = build_fact_rows(
            report.rows,
            template,
            period_start=report.period_start,
            period_end=report.period_end,
            run_id=run_id,
        )
        self._projector.replace_facts(facts, synced_at=now)
        self._repository.finish_run(
            run_id,
            rows_ok=report.rows_ok,
            rows_bad=report.rows_bad,
            status=STATUS_IMPORTED,
        )
        return ManualImportResult(
            run_id=run_id,
            dataset=template.dataset,
            period=report.period,
            rows_ok=report.rows_ok,
            rows_bad=report.rows_bad,
            status=STATUS_IMPORTED,
            report=report,
        )
