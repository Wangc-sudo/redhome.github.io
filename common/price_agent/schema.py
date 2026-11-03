"""猫超价格 agent 的 MySQL schema（由 live_migrations 独立版本引用）。

活跃任务唯一性由 generated column + UNIQUE 在数据库层保证：queued/running/
waiting_login 映射为 pipeline，终态映射为 NULL（MySQL 唯一索引允许多个 NULL）。
这样定时派发、手动派发并发时不依赖 scheduler 的短生命周期锁。
"""

_JOB_DDL = (
    "CREATE TABLE IF NOT EXISTS `pd_price_agent_job` (\n"
    "  `job_id` VARCHAR(36) NOT NULL,\n"
    "  `pipeline` VARCHAR(32) NOT NULL,\n"
    "  `source` ENUM('cron','run_once') NOT NULL,\n"
    "  `status` ENUM('queued','running','waiting_login','completed','failed',"
    "'timed_out','login_expired','cancelled') NOT NULL DEFAULT 'queued',\n"
    "  `active_pipeline` VARCHAR(32) GENERATED ALWAYS AS (\n"
    "    CASE WHEN `status` IN ('queued','running','waiting_login')\n"
    "    THEN `pipeline` ELSE NULL END\n"
    "  ) STORED,\n"
    "  `priority` INT NOT NULL DEFAULT 100,\n"
    "  `attempt` INT NOT NULL DEFAULT 1,\n"
    "  `agent_id` VARCHAR(64) DEFAULT NULL,\n"
    "  `lease_token_hash` CHAR(64) DEFAULT NULL,\n"
    "  `lease_expires_at` DATETIME(6) DEFAULT NULL,\n"
    "  `heartbeat_at` DATETIME(6) DEFAULT NULL,\n"
    "  `config_hash` CHAR(64) NOT NULL,\n"
    "  `config_snapshot` MEDIUMTEXT NOT NULL,\n"
    "  `current_subject` VARCHAR(128) DEFAULT NULL,\n"
    "  `current_page` INT DEFAULT NULL,\n"
    "  `auth_state` ENUM('unknown','valid','login_required') "
    "NOT NULL DEFAULT 'unknown',\n"
    "  `auth_checked_at` DATETIME(6) DEFAULT NULL,\n"
    "  `login_required_since` DATETIME(6) DEFAULT NULL,\n"
    "  `session_version` INT NOT NULL DEFAULT 0,\n"
    "  `row_count` INT NOT NULL DEFAULT 0,\n"
    "  `subject_summary` MEDIUMTEXT DEFAULT NULL,\n"
    "  `result_fingerprint` CHAR(64) DEFAULT NULL,\n"
    "  `completion_hash` CHAR(64) DEFAULT NULL,\n"
    "  `error_code` VARCHAR(64) DEFAULT NULL,\n"
    "  `error_detail` VARCHAR(500) DEFAULT NULL,\n"
    "  `run_history_id` BIGINT DEFAULT NULL,\n"
    "  `created_at` DATETIME(6) NOT NULL,\n"
    "  `started_at` DATETIME(6) DEFAULT NULL,\n"
    "  `finished_at` DATETIME(6) DEFAULT NULL,\n"
    "  `updated_at` DATETIME(6) NOT NULL,\n"
    "  PRIMARY KEY (`job_id`),\n"
    "  UNIQUE KEY `uk_price_agent_active_pipeline` (`active_pipeline`),\n"
    "  KEY `idx_price_agent_queue` (`status`, `priority`, `created_at`),\n"
    "  KEY `idx_price_agent_lease` (`status`, `lease_expires_at`),\n"
    "  KEY `idx_price_agent_run_history` (`run_history_id`),\n"
    "  KEY `idx_price_agent_finished` (`status`, `finished_at`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_FACT_DDL = (
    "CREATE TABLE IF NOT EXISTS `fact_maochao_price_snapshot` (\n"
    "  `job_id` VARCHAR(36) NOT NULL,\n"
    "  `row_uid` CHAR(64) NOT NULL,\n"
    "  `business_date` DATE NOT NULL,\n"
    "  `subject_key` VARCHAR(128) NOT NULL,\n"
    "  `item_id` VARCHAR(64) NOT NULL DEFAULT '',\n"
    "  `sku_id` VARCHAR(64) NOT NULL DEFAULT '',\n"
    "  `product` VARCHAR(512) DEFAULT NULL,\n"
    "  `product_spec` VARCHAR(255) DEFAULT NULL,\n"
    "  `market_source` VARCHAR(255) DEFAULT NULL,\n"
    "  `market_product` VARCHAR(512) DEFAULT NULL,\n"
    "  `market_spec` VARCHAR(255) DEFAULT NULL,\n"
    "  `market_price` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `current_price` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `suggested_price` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `price_gap` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `page_status` VARCHAR(64) DEFAULT NULL,\n"
    "  `page_number` INT NOT NULL DEFAULT 0,\n"
    "  `row_index` INT NOT NULL DEFAULT 0,\n"
    "  `captured_at` DATETIME(6) NOT NULL,\n"
    "  `payload_hash` CHAR(64) NOT NULL,\n"
    "  PRIMARY KEY (`job_id`, `row_uid`),\n"
    "  KEY `idx_maochao_price_business` (`business_date`, `subject_key`),\n"
    "  KEY `idx_maochao_price_item` (`item_id`, `sku_id`),\n"
    "  KEY `idx_maochao_price_captured` (`captured_at`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_ARTIFACT_DDL = (
    "CREATE TABLE IF NOT EXISTS `pd_price_agent_artifact` (\n"
    "  `id` BIGINT NOT NULL AUTO_INCREMENT,\n"
    "  `job_id` VARCHAR(36) NOT NULL,\n"
    "  `subject_key` VARCHAR(128) NOT NULL DEFAULT '',\n"
    "  `artifact_type` VARCHAR(32) NOT NULL,\n"
    "  `bucket` VARCHAR(128) NOT NULL,\n"
    "  `object_key` VARCHAR(768) NOT NULL,\n"
    "  `sha256` CHAR(64) NOT NULL,\n"
    "  `size_bytes` BIGINT NOT NULL DEFAULT 0,\n"
    "  `upload_status` VARCHAR(32) NOT NULL,\n"
    "  `created_at` DATETIME(6) NOT NULL,\n"
    "  PRIMARY KEY (`id`),\n"
    "  UNIQUE KEY `uk_price_agent_artifact` (`job_id`, `object_key`(191)),\n"
    "  KEY `idx_price_agent_artifact_sha` (`sha256`),\n"
    "  KEY `idx_price_agent_artifact_job` (`job_id`, `artifact_type`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


def price_agent_ddl_statements() -> tuple[str, ...]:
    return (_JOB_DDL, _FACT_DDL, _ARTIFACT_DDL)


def price_agent_dept_level2_ddl_statements() -> tuple[str, ...]:
    return (
        "ALTER TABLE `fact_maochao_price_snapshot` "
        "ADD COLUMN `dept_level2` VARCHAR(128) NOT NULL DEFAULT '' AFTER `subject_key`",
    )
