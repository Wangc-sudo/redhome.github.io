import hashlib
import re
from datetime import datetime, timezone

from common.public_data.finance_schema import all_table_definitions
from common.public_data.db import transaction

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class LiveMigrationError(RuntimeError):
    pass


def _quote(name: str) -> str:
    if not _IDENTIFIER_RE.match(name):
        raise LiveMigrationError(f"invalid identifier: {name!r}")
    return f"`{name}`"


def _build_finance_table_ddl(table) -> str:
    column_defs = []
    for col in table.business_columns:
        null = "NULL" if col.nullable else "NOT NULL"
        column_defs.append(f"  {_quote(col.name)} {col.mysql_type} {null}")
    for col in table.technical_columns:
        null = "NULL" if col.nullable else "NOT NULL"
        column_defs.append(f"  {_quote(col.name)} {col.mysql_type} {null}")

    column_defs.append("  PRIMARY KEY (`dingtalk_record_id`)")
    column_defs.append("  KEY `idx_synced_at` (`synced_at`)")
    for index in table.indexes:
        cols = ", ".join(_quote(c) for c in index.columns)
        column_defs.append(f"  KEY `{index.name}` ({cols})")

    body = ",\n".join(column_defs)
    return (
        f"CREATE TABLE IF NOT EXISTS {_quote(table.name)} (\n"
        f"{body}\n"
        f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )


_SCHEMA_SNAPSHOTS_DDL = (
    "CREATE TABLE IF NOT EXISTS `dingtalk_schema_snapshots` (\n"
    "  `base_id` VARCHAR(255) NOT NULL,\n"
    "  `sheet_id` VARCHAR(255) NOT NULL,\n"
    "  `fields_sha256` CHAR(64) NOT NULL,\n"
    "  `fields_json` JSON NOT NULL,\n"
    "  `observed_at` DATETIME(6) NOT NULL,\n"
    "  PRIMARY KEY (`base_id`, `sheet_id`, `fields_sha256`),\n"
    "  KEY `idx_observed_at` (`observed_at`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_WDT_RECORDS_DDL = (
    "CREATE TABLE IF NOT EXISTS `wdt_records` (\n"
    "  `source_method` VARCHAR(255) NOT NULL,\n"
    "  `source_record_id` VARCHAR(255) NOT NULL,\n"
    "  `source_window_start` DATETIME(6) NOT NULL,\n"
    "  `source_window_end` DATETIME(6) NOT NULL,\n"
    "  `payload_json` JSON NOT NULL,\n"
    "  `synced_at` DATETIME(6) NOT NULL,\n"
    "  `sync_run_id` CHAR(36) NOT NULL,\n"
    "  PRIMARY KEY (`source_method`, `source_record_id`),\n"
    "  KEY `idx_sync_run_id` (`sync_run_id`),\n"
    "  KEY `idx_source_window` (`source_method`, `source_window_start`, `source_window_end`),\n"
    "  KEY `idx_synced_at` (`synced_at`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_SYNC_RUNS_DDL = (
    "CREATE TABLE IF NOT EXISTS `sync_runs` (\n"
    "  `sync_run_id` CHAR(36) NOT NULL,\n"
    "  `status` ENUM('started', 'raw_committed', 'projection_pending', 'completed', 'failed') NOT NULL,\n"
    "  `manifest_sha256` CHAR(64) NOT NULL,\n"
    "  `started_at` DATETIME(6) NOT NULL,\n"
    "  `finished_at` DATETIME(6) NULL,\n"
    "  `failure_code` VARCHAR(100) NULL,\n"
    "  PRIMARY KEY (`sync_run_id`),\n"
    "  KEY `idx_status_started_at` (`status`, `started_at`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_SYNC_DATASET_SUMMARY_DDL = (
    "CREATE TABLE IF NOT EXISTS `sync_dataset_summary` (\n"
    "  `sync_run_id` CHAR(36) NOT NULL,\n"
    "  `source_name` ENUM('dingtalk', 'wdt') NOT NULL,\n"
    "  `dataset_name` VARCHAR(255) NOT NULL,\n"
    "  `records_read` BIGINT UNSIGNED NOT NULL,\n"
    "  `raw_records_written` BIGINT UNSIGNED NOT NULL,\n"
    "  `record_id_digest` CHAR(64) NOT NULL,\n"
    "  `completed_at` DATETIME(6) NOT NULL,\n"
    "  PRIMARY KEY (`sync_run_id`, `source_name`, `dataset_name`),\n"
    "  KEY `idx_dataset_completed_at` (`source_name`, `dataset_name`, `completed_at`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_MIGRATION_TRACKING_DDL = (
    "CREATE TABLE IF NOT EXISTS `pd_live_schema_migration` (\n"
    "  `version` VARCHAR(64) NOT NULL,\n"
    "  `checksum` CHAR(64) NOT NULL,\n"
    "  `applied_at` DATETIME(6) NOT NULL,\n"
    "  PRIMARY KEY (`version`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


def _build_dingtalk_ddl() -> tuple[str, ...]:
    ddls = []
    for table in all_table_definitions():
        ddls.append(_build_finance_table_ddl(table))
    ddls.append(_SCHEMA_SNAPSHOTS_DDL)
    return tuple(ddls)


def _build_wdt_ddl() -> tuple[str, ...]:
    return (_WDT_RECORDS_DDL,)


def _build_mart_ddl() -> tuple[str, ...]:
    return (_SYNC_RUNS_DDL, _SYNC_DATASET_SUMMARY_DDL)


_MIGRATIONS = (
    ("raw-dingtalk-v1", "dingtalk", _build_dingtalk_ddl()),
    ("raw-wdt-v1", "wdt", _build_wdt_ddl()),
    ("mart-ops-v1", "mart", _build_mart_ddl()),
)


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _combined_checksum(statements: tuple[str, ...]) -> str:
    return hashlib.sha256(
        "\n".join(statements).encode("utf-8")
    ).hexdigest()


def _apply_to_connection(connection, version_statements, applied_checksums):
    cursor = connection.cursor()
    try:
        cursor.execute(_MIGRATION_TRACKING_DDL)

        cursor.execute("SELECT `version`, `checksum` FROM `pd_live_schema_migration`")
        rows = cursor.fetchall() if hasattr(cursor, "fetchall") else []
        if not rows and hasattr(cursor, "fetchone"):
            row = cursor.fetchone()
            rows = [row] if row else []

        applied = {}
        for row in rows:
            if isinstance(row, dict):
                applied[row["version"]] = row["checksum"]
            elif isinstance(row, (list, tuple)):
                applied[row[0]] = row[1]

        if applied_checksums is not None:
            applied = dict(applied_checksums)

        for version, statements in version_statements:
            current_checksum = _combined_checksum(statements)
            if version in applied:
                if applied[version] != current_checksum:
                    raise LiveMigrationError(
                        f"checksum mismatch for {version}: "
                        f"applied={applied[version]}, current={current_checksum}"
                    )
                continue

            with transaction(connection):
                for statement in statements:
                    cursor.execute(statement)
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
                cursor.execute(
                    "INSERT INTO `pd_live_schema_migration` "
                    "(`version`, `checksum`, `applied_at`) VALUES (%s, %s, %s)",
                    (version, current_checksum, now),
                )
    finally:
        cursor.close()


def apply_live_migrations(
    dingtalk_connection,
    wdt_connection,
    mart_connection,
    applied_checksums=None,
):
    dingtalk_versions = [
        (v, stmts) for v, t, stmts in _MIGRATIONS if t == "dingtalk"
    ]
    wdt_versions = [(v, stmts) for v, t, stmts in _MIGRATIONS if t == "wdt"]
    mart_versions = [(v, stmts) for v, t, stmts in _MIGRATIONS if t == "mart"]

    _apply_to_connection(dingtalk_connection, dingtalk_versions, applied_checksums)
    _apply_to_connection(wdt_connection, wdt_versions, applied_checksums)
    _apply_to_connection(mart_connection, mart_versions, applied_checksums)
