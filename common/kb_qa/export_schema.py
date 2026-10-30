# -*- coding: utf-8 -*-
"""小易查询快照与 XLSX 导出任务的独立迁移 DDL。"""

_QUERY_SNAPSHOT_DDL = """CREATE TABLE IF NOT EXISTS `kb_qa_query_snapshot` (
  `snapshot_id` char(32) NOT NULL,
  `msg_id` varchar(128) NOT NULL,
  `sender_uid` varchar(128) NOT NULL,
  `conversation_id` varchar(128) NOT NULL,
  `payload_json` LONGTEXT NOT NULL COMMENT '冻结的工具参数与结果，不生成时重查',
  `created_at` datetime(6) NOT NULL,
  PRIMARY KEY (`snapshot_id`),
  UNIQUE KEY `uk_kb_qa_query_snapshot_msg` (`msg_id`),
  KEY `idx_kb_qa_query_snapshot_owner_time` (`sender_uid`, `conversation_id`, `created_at`),
  KEY `idx_kb_qa_query_snapshot_conversation_time` (`conversation_id`, `created_at`),
  KEY `idx_kb_qa_query_snapshot_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

_QUERY_EXPORT_DDL = """CREATE TABLE IF NOT EXISTS `kb_qa_export` (
  `export_id` char(32) NOT NULL,
  `snapshot_id` char(32) NOT NULL,
  `requester_uid` varchar(128) NOT NULL,
  `conversation_id` varchar(128) NOT NULL,
  `status` varchar(16) NOT NULL COMMENT 'pending/running/completed/failed/expired',
  `file_name` varchar(255) NOT NULL COMMENT '展示用中文文件名',
  `stored_name` varchar(255) NOT NULL COMMENT '服务端随机存储名',
  `row_count` bigint unsigned NULL,
  `file_size` bigint unsigned NULL,
  `file_sha256` char(64) NULL,
  `freshness_status` varchar(32) NULL,
  `query_summary_json` LONGTEXT NULL COMMENT '条件与口径摘要，不含消息内容或完整查询语句',
  `error` text NULL,
  `expires_at` datetime(6) NOT NULL,
  `created_at` datetime(6) NOT NULL,
  `finished_at` datetime(6) NULL,
  `downloaded_at` datetime(6) NULL,
  `sent_at` datetime(6) NULL,
  PRIMARY KEY (`export_id`),
  KEY `idx_kb_qa_export_snapshot` (`snapshot_id`),
  KEY `idx_kb_qa_export_requester_time` (`requester_uid`, `conversation_id`, `created_at`),
  KEY `idx_kb_qa_export_conversation_time` (`conversation_id`, `created_at`),
  KEY `idx_kb_qa_export_status_expires` (`status`, `expires_at`),
  KEY `idx_kb_qa_export_expires` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

_QUERY_EXPORT_V2_DDL = """ALTER TABLE `kb_qa_export`
  ADD COLUMN `started_at` datetime(6) NULL AFTER `created_at`"""


def query_snapshot_ddl_statements():
    """返回 ``kb_qa_query_snapshot`` 的可重放建表语句。"""
    return (_QUERY_SNAPSHOT_DDL,)


def query_export_ddl_statements():
    """返回 ``kb_qa_export`` 的可重放建表语句。"""
    return (_QUERY_EXPORT_DDL,)


def query_export_v2_ddl_statements():
    """v2 补运行租约列；不修改已应用 v1 的校验和。"""
    return (_QUERY_EXPORT_V2_DDL,)
