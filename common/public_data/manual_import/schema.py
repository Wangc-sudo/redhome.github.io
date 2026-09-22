"""人工报表通道的 DDL（幂等，注册进 ``live_migrations``）。

两张 raw 表 + 一张 mart 事实表，**与模板数量无关**：

* ``manual_import_runs`` —— 一次导入的审计头（谁、什么时候、哪个文件、
  多少行）；
* ``manual_import_row`` —— 原始行（源列 JSON + 标准化字段 JSON）；
* ``mart_ops.fact_manual_report`` —— 投影出的窄表，BI 只读这一张。

刻意**不为每个模板建一张表**：模板演进（加一列、改枚举）属于契约变更，
走模板版本评审；建表 DDL 一旦随模板漂移，``live_migrations`` 的校验和
就会把整个服务拒之门外。
"""

#: raw_manual.manual_import_runs —— 导入审计头。
#: ``uk_dataset_period_file`` 承载「同文件重复导入幂等」：同一 dataset +
#: 同一期间 + 同一文件摘要 = 同一次导入，重跑无副作用。
_MANUAL_IMPORT_RUNS_DDL = (
    "CREATE TABLE IF NOT EXISTS `manual_import_runs` (\n"
    "  `run_id` CHAR(36) NOT NULL,\n"
    "  `dataset` VARCHAR(64) NOT NULL,\n"
    "  `template_version` INT UNSIGNED NOT NULL,\n"
    "  `file_name` VARCHAR(255) NOT NULL,\n"
    "  `file_sha256` CHAR(64) NOT NULL,\n"
    "  `period` VARCHAR(16) NOT NULL,\n"
    "  `rows_ok` INT UNSIGNED NOT NULL DEFAULT 0,\n"
    "  `rows_bad` INT UNSIGNED NOT NULL DEFAULT 0,\n"
    "  `status` ENUM('dry_run', 'imported', 'duplicate', 'rejected') NOT NULL,\n"
    "  `imported_by` VARCHAR(64) NOT NULL DEFAULT 'cli',\n"
    "  `imported_at` DATETIME(6) NOT NULL,\n"
    "  PRIMARY KEY (`run_id`),\n"
    "  UNIQUE KEY `uk_dataset_period_file` (`dataset`, `period`, `file_sha256`),\n"
    "  KEY `idx_manual_runs_dataset_period` (`dataset`, `period`),\n"
    "  KEY `idx_manual_runs_imported_at` (`imported_at`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

#: raw_manual.manual_import_row —— 原始行。``source_json`` 留报表原样（可
#: 复核、可重放），``fields_json`` 是标准化后的字段（投影的输入）。
_MANUAL_IMPORT_ROW_DDL = (
    "CREATE TABLE IF NOT EXISTS `manual_import_row` (\n"
    "  `run_id` CHAR(36) NOT NULL,\n"
    "  `row_no` INT UNSIGNED NOT NULL,\n"
    "  `dataset` VARCHAR(64) NOT NULL,\n"
    "  `period` VARCHAR(16) NOT NULL,\n"
    "  `source_json` JSON NOT NULL,\n"
    "  `fields_json` JSON NOT NULL,\n"
    "  `imported_at` DATETIME(6) NOT NULL,\n"
    "  PRIMARY KEY (`run_id`, `row_no`),\n"
    "  KEY `idx_manual_row_dataset_period` (`dataset`, `period`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

#: mart_ops.fact_manual_report —— 人工报表的统一窄表。
#: 唯一键即「同一指标同一期间同一维度只有一行」，配合投影前按
#: dataset + period 整段删除，实现「重导覆盖而非追加」。
_FACT_MANUAL_REPORT_DDL = (
    "CREATE TABLE IF NOT EXISTS `fact_manual_report` (\n"
    "  `record_id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n"
    "  `dataset` VARCHAR(64) NOT NULL,\n"
    "  `period_type` ENUM('month', 'quarter', 'year') NOT NULL,\n"
    "  `period_start` DATE NOT NULL,\n"
    "  `period_end` DATE NOT NULL,\n"
    "  `dim_scope` VARCHAR(64) NOT NULL,\n"
    "  `dimension_value` VARCHAR(255) NOT NULL,\n"
    "  `metric` VARCHAR(64) NOT NULL,\n"
    "  `value` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `unit` VARCHAR(16) DEFAULT NULL,\n"
    "  `source_run_id` CHAR(36) NOT NULL,\n"
    "  `synced_at` DATETIME(6) NOT NULL,\n"
    "  PRIMARY KEY (`record_id`),\n"
    "  UNIQUE KEY `uk_manual_report` "
    "(`dataset`, `period_type`, `period_start`, `dim_scope`, `dimension_value`, `metric`),\n"
    "  KEY `idx_manual_report_dataset_period` (`dataset`, `period_start`),\n"
    "  KEY `idx_manual_report_run` (`source_run_id`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

RAW_MANUAL_RUNS_TABLE = "manual_import_runs"
RAW_MANUAL_ROWS_TABLE = "manual_import_row"
MART_FACT_TABLE = "fact_manual_report"


def raw_manual_ddl_statements() -> tuple[str, ...]:
    """``raw_manual`` 库的建表语句（按依赖顺序）。"""
    return (_MANUAL_IMPORT_RUNS_DDL, _MANUAL_IMPORT_ROW_DDL)


def mart_manual_ddl_statements() -> tuple[str, ...]:
    """``mart_ops`` 库的建表语句（BI 只读 ``fact_manual_report``）。"""
    return (_FACT_MANUAL_REPORT_DDL,)
