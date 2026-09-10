"""Raw repositories for persisting DingTalk and WDT gateway data."""

import contextlib
import json
import math
from datetime import date
from decimal import Decimal, InvalidOperation

from common.public_data import finance_schema


def _canonical_json(value):
    """Serialize *value* to compact canonical JSON."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _convert_value(value, column_def):
    """Convert a source *value* according to the column's registered type.

    Returns the Python object that should be passed as a parameterised SQL
    value for *column_def*.
    """
    source_type = column_def.source_type
    mysql_type = column_def.mysql_type

    # --- DECIMAL(20,4) / currency ---
    if source_type == "currency" or mysql_type.startswith("DECIMAL"):
        if value is None:
            return None
        d = Decimal(str(value))
        if not d.is_finite():
            raise ValueError(
                f"Non-finite value {value!r} rejected for DECIMAL column "
                f"{column_def.name!r}"
            )
        return d

    # --- DATE ---
    if source_type == "date" or mysql_type == "DATE":
        if value is None:
            return None
        return date.fromisoformat(value)

    # --- JSON (user, unidirectionalLink, or JSON mysql_type) ---
    if mysql_type == "JSON" or source_type in ("user", "unidirectionalLink"):
        return _canonical_json(value)

    # --- VARCHAR / TEXT / pass-through ---
    if value is None:
        return None
    return str(value)


class DingTalkRawRepository:
    """Persists DingTalk records into finance-schema target tables."""

    def __init__(self, connection):
        self._connection = connection

    def upsert_records(
        self,
        table_name,
        records,
        *,
        field_mapping,
        sync_run_id,
        synced_at,
    ):
        """Upsert *records* into the registered target table.

        Parameters
        ----------
        table_name : str
            A key known to :mod:`finance_schema`.
        records : list[dict]
            Each element has ``"id"`` (str) and ``"fields"`` (dict mapping
            source field names to raw values).
        field_mapping : dict[str, str]
            Maps source field names (e.g. Chinese labels) to MySQL column
            names defined in the table's business columns.
        sync_run_id : str
        synced_at : datetime
        """
        table_def = finance_schema.table_definition(table_name)

        # Build column lists -------------------------------------------------
        business_cols = []
        for col in table_def.business_columns:
            # Only include columns that are reachable via the field_mapping
            source_names = [
                src for src, dst in field_mapping.items() if dst == col.name
            ]
            if source_names:
                business_cols.append((col, source_names[0]))

        all_columns = (
            [col.name for col, _ in business_cols]
            + ["dingtalk_record_id", "synced_at", "sync_run_id"]
        )

        placeholders = ", ".join(["%s"] * len(all_columns))
        col_list = ", ".join(f"`{c}`" for c in all_columns)

        # ON DUPLICATE KEY UPDATE — everything except the PK (dingtalk_record_id)
        update_cols = (
            [col.name for col, _ in business_cols]
            + ["synced_at", "sync_run_id"]
        )
        update_clause = ", ".join(
            f"`{c}` = VALUES(`{c}`)" for c in update_cols
        )

        sql = (
            f"INSERT INTO `{table_name}` ({col_list}) "
            f"VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {update_clause}"
        )

        # Execute per record -------------------------------------------------
        with contextlib.closing(self._connection.cursor()) as cursor:
            for record in records:
                record_id = record["id"]
                fields = record["fields"]

                params = []
                for col, source_name in business_cols:
                    raw = fields.get(source_name)
                    params.append(_convert_value(raw, col))
                params.extend([record_id, synced_at, sync_run_id])

                cursor.execute(sql, tuple(params))


class WdtRawRepository:
    """Persists WDT gateway records into ``wdt_records``."""

    def __init__(self, connection):
        self._connection = connection

    def upsert_records(
        self,
        method,
        records,
        *,
        window_start,
        window_end,
        sync_run_id,
        synced_at,
    ):
        """Upsert WDT *records* into ``wdt_records``.

        Parameters
        ----------
        method : str
            The WDT API method name (e.g. ``sales.TradeQuery.queryWithDetail``).
        records : list[tuple[dict, str]]
            Each element is ``(record_dict, stable_id)``.
        window_start, window_end : datetime
        sync_run_id : str
        synced_at : datetime
        """
        sql = (
            "INSERT INTO `wdt_records` "
            "(`source_method`, `source_record_id`, "
            "`source_window_start`, `source_window_end`, "
            "`payload_json`, `synced_at`, `sync_run_id`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "`source_window_start` = VALUES(`source_window_start`), "
            "`source_window_end` = VALUES(`source_window_end`), "
            "`payload_json` = VALUES(`payload_json`), "
            "`synced_at` = VALUES(`synced_at`), "
            "`sync_run_id` = VALUES(`sync_run_id`)"
        )

        with contextlib.closing(self._connection.cursor()) as cursor:
            for record_dict, stable_id in records:
                payload = _canonical_json(record_dict)
                cursor.execute(
                    sql,
                    (method, stable_id, window_start, window_end,
                     payload, synced_at, sync_run_id),
                )

    def summary_for_run(self, sync_run_id):
        """Return row-level summary for *sync_run_id* without payload data."""
        sql = (
            "SELECT `source_method`, `source_record_id`, "
            "`source_window_start`, `source_window_end`, "
            "`synced_at`, `sync_run_id` "
            "FROM `wdt_records` "
            "WHERE `sync_run_id` = %s"
        )
        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(sql, (sync_run_id,))
