"""``raw_manual`` 仓储：导入审计头 + 原始行。

幂等分两层，别混为一谈：

* **同文件重跑** —— ``manual_import_runs`` 的
  ``uk_dataset_period_file`` 挡住；服务层先查 ``find_run``，命中即
  ``duplicate``，一行不写；
* **同期间换文件重导** —— 老行的 ``dataset + period`` 先删再插（
  ``replace_rows``），不是追加，也不是留着新旧混合。

raw 层永远保留**可重放**的原始 JSON：``source_json`` 是报表原样（复核用），
``fields_json`` 是标准化字段（投影用）。
"""

import contextlib
import json
from dataclasses import dataclass
from datetime import date, datetime

from common.public_data.manual_import.schema import (
    RAW_MANUAL_ROWS_TABLE,
    RAW_MANUAL_RUNS_TABLE,
)


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_safe(value):
    """把标准化字段转成 JSON 安全值（Decimal/date 落字符串）。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if value is None:
        return None
    return str(value)


@dataclass(frozen=True)
class ManualRow:
    """待落库的一行：标准化字段 + 报表原始单元格。"""

    row_no: int
    fields: dict
    source: dict


@dataclass(frozen=True)
class ManualRunRecord:
    """一次导入的审计头（查询结果）。"""

    run_id: str
    dataset: str
    period: str
    file_sha256: str
    status: str


class ManualImportRepository:
    """读写 ``raw_manual`` 的两张表。"""

    def __init__(self, connection):
        self._connection = connection

    def find_run(self, dataset: str, period: str, file_sha256: str):
        """同一 dataset + 期间 + 文件摘要是否已导入过。命中返回记录。"""
        sql = (
            "SELECT `run_id`, `dataset`, `period`, `file_sha256`, `status` "
            f"FROM `{RAW_MANUAL_RUNS_TABLE}` "
            "WHERE `dataset` = %s AND `period` = %s AND `file_sha256` = %s"
        )
        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(sql, (dataset, period, file_sha256))
            row = cursor.fetchone()
        if not row:
            return None
        return ManualRunRecord(
            run_id=row["run_id"],
            dataset=row["dataset"],
            period=row["period"],
            file_sha256=row["file_sha256"],
            status=row["status"],
        )

    def start_run(
        self,
        *,
        run_id: str,
        dataset: str,
        template_version: int,
        file_name: str,
        file_sha256: str,
        period: str,
        status: str,
        imported_by: str,
        imported_at,
    ) -> None:
        """写入审计头（先落 ``rejected``/``imported``，行数后补）。"""
        sql = (
            f"INSERT INTO `{RAW_MANUAL_RUNS_TABLE}` "
            "(`run_id`, `dataset`, `template_version`, `file_name`, "
            "`file_sha256`, `period`, `rows_ok`, `rows_bad`, `status`, "
            "`imported_by`, `imported_at`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(sql, (
                run_id, dataset, template_version, file_name, file_sha256,
                period, 0, 0, status, imported_by, imported_at,
            ))

    def finish_run(
        self, run_id: str, *, rows_ok: int, rows_bad: int, status: str
    ) -> None:
        sql = (
            f"UPDATE `{RAW_MANUAL_RUNS_TABLE}` "
            "SET `rows_ok` = %s, `rows_bad` = %s, `status` = %s "
            "WHERE `run_id` = %s"
        )
        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(sql, (rows_ok, rows_bad, status, run_id))

    def replace_rows(
        self,
        *,
        run_id: str,
        dataset: str,
        period: str,
        rows,
        imported_at,
    ) -> int:
        """先删同 dataset+period 的老行，再整体插入当前 run 的行。

        *rows* 是 :class:`ManualRow` 序列（标准化字段 + 报表原始单元格）。
        返回写入行数。
        """
        delete_sql = (
            f"DELETE FROM `{RAW_MANUAL_ROWS_TABLE}` "
            "WHERE `dataset` = %s AND `period` = %s"
        )
        insert_sql = (
            f"INSERT INTO `{RAW_MANUAL_ROWS_TABLE}` "
            "(`run_id`, `row_no`, `dataset`, `period`, "
            "`source_json`, `fields_json`, `imported_at`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)"
        )
        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(delete_sql, (dataset, period))
            for row in rows:
                cursor.execute(insert_sql, (
                    run_id,
                    row.row_no,
                    dataset,
                    period,
                    _canonical_json(row.source),
                    _canonical_json(
                        {k: _json_safe(v) for k, v in row.fields.items()}
                    ),
                    imported_at,
                ))
        return len(rows)
