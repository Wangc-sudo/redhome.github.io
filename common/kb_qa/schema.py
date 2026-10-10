# -*- coding: utf-8 -*-
"""知识库问答机器人 schema 定义（mart-ops-kb-qa-v1 的 DDL 源）。

被 live_migrations 引用登记，建表走「版本 + 校验和 + 跟踪表」正规通道；
本模块不得 import public_data（live_migrations 反向引用，防循环导入）。

表说明：
- kb_products      商品主表（AI 表「产品资料」同步落地；附件只存元信息不存 URL）
- kb_media_cache   钉钉 media_id 缓存（media_id 3 天有效，命中直发）
- kb_doc_chunks    手册切片 + embedding（P2 用；几百条暴力余弦，无需向量索引）
- kb_qa_audit      问答流水（append-only；msg_id 唯一键兼做投递去重）
"""

_KB_PRODUCTS_DDL = """CREATE TABLE IF NOT EXISTS `kb_products` (
  `record_id` varchar(64) NOT NULL COMMENT '钉钉 AI 表 recordId（幂等 upsert 键）',
  `code` varchar(64) NULL COMMENT '商品编码',
  `name` varchar(255) NOT NULL COMMENT '货品名称',
  `brand` varchar(64) NULL COMMENT '品牌',
  `category` varchar(64) NULL COMMENT '大类',
  `box_qty` int NULL COMMENT '箱规',
  `case_qty` int NULL COMMENT '盒规',
  `size_mm` varchar(64) NULL COMMENT '单盒尺寸(mm)',
  `weight_kg` decimal(10,3) NULL COMMENT '重量(kg)',
  `material` varchar(64) NULL,
  `gift_bag_spec` varchar(128) NULL COMMENT '礼袋规格',
  `barcode_single` varchar(64) NULL COMMENT '单品69码',
  `barcode_case` varchar(64) NULL COMMENT '原箱69码',
  `attachments` json NULL COMMENT '附件元信息 {字段名:[{name,size,mime}]}，URL 用时现取',
  `extra` json NULL COMMENT '其余未映射字段兜底',
  `synced_at` datetime NOT NULL,
  PRIMARY KEY (`record_id`),
  KEY `idx_kb_products_name` (`name`),
  KEY `idx_kb_products_brand` (`brand`, `category`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

_KB_MEDIA_CACHE_DDL = """CREATE TABLE IF NOT EXISTS `kb_media_cache` (
  `cache_key` varchar(128) NOT NULL COMMENT 'record_id:字段名:序号',
  `media_id` varchar(255) NOT NULL,
  `media_type` varchar(16) NOT NULL COMMENT 'image/file',
  `file_name` varchar(255) NULL,
  `expires_at` datetime NOT NULL COMMENT '钉钉 media_id 3 天有效，提前 1 小时作废',
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`cache_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

_KB_DOC_CHUNKS_DDL = """CREATE TABLE IF NOT EXISTS `kb_doc_chunks` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `doc_id` varchar(64) NOT NULL COMMENT '钉钉知识库 nodeId',
  `doc_title` varchar(255) NULL,
  `chunk_index` int NOT NULL,
  `content` text NOT NULL,
  `embedding` blob NULL COMMENT 'float32 小端字节序（全量读出内存余弦，<10ms）',
  `synced_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_kb_doc_chunks_doc` (`doc_id`, `chunk_index`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

_KB_QA_AUDIT_DDL = """CREATE TABLE IF NOT EXISTS `kb_qa_audit` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `msg_id` varchar(128) NULL COMMENT '钉钉回调 messageId（INSERT IGNORE 去重）',
  `conversation_id` varchar(128) NULL,
  `sender_uid` varchar(128) NULL,
  `sender_name` varchar(128) NULL,
  `question` text NULL,
  `intent` varchar(32) NULL,
  `matched_record_id` varchar(64) NULL,
  `reply_kind` varchar(16) NULL COMMENT 'text/link/none',
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_kb_qa_audit_msg` (`msg_id`),
  KEY `idx_kb_qa_audit_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def kb_ddl_statements():
    """mart-ops-kb-qa-v1 的建表语句（可重放，全部 IF NOT EXISTS）。"""
    return (
        _KB_PRODUCTS_DDL,
        _KB_MEDIA_CACHE_DDL,
        _KB_DOC_CHUNKS_DDL,
        _KB_QA_AUDIT_DDL,
    )
