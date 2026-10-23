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


# 反馈闭环（2026-10-21）：kb_qa_audit 扩观测列——此前只记 intent/matched/
# reply_kind，badcase 无法回溯（答了什么、走了哪条召回、调了什么工具、
# 多快、成没成全是黑盒）。独立版本而非改写 v1：v1 校验和已入跟踪表，
# 改动其文本会被 live_migrations 判漂移拒绝启动；存量库只跑本 ALTER。
_KB_QA_AUDIT_V2_DDL = """ALTER TABLE `kb_qa_audit`
  ADD COLUMN `recall_source` varchar(32) NULL COMMENT '召回通路：products/bm25/vector/mixed/agent/none' AFTER `reply_kind`,
  ADD COLUMN `tool_used` varchar(255) NULL COMMENT 'agent 工具调用序列（逗号分隔）',
  ADD COLUMN `agent_rounds` tinyint NULL COMMENT 'agent LLM 调用轮数',
  ADD COLUMN `latency_ms` int NULL COMMENT '问答主流程耗时（毫秒，不含网络发送）',
  ADD COLUMN `failed` tinyint NOT NULL DEFAULT 0 COMMENT '1=处理异常（走了兜底话术）',
  ADD COLUMN `answer_excerpt` varchar(500) NULL COMMENT '答复前 500 字（badcase 复盘用）'"""


def kb_audit_v2_ddl_statements():
    """mart-ops-kb-qa-audit-v2：kb_qa_audit 观测列扩列（反馈闭环）。"""
    return (_KB_QA_AUDIT_V2_DDL,)


# token 成本统计（2026-10-22）：LLM 用量随消息落审计（当条消息全部
# LLM 调用的前后快照差值合计；遥测口径非计费真源）。
_KB_QA_AUDIT_V3_DDL = """ALTER TABLE `kb_qa_audit`
  ADD COLUMN `prompt_tokens` int NULL COMMENT 'LLM 输入 token 合计（本条消息全部调用）',
  ADD COLUMN `completion_tokens` int NULL COMMENT 'LLM 输出 token 合计（本条消息全部调用）'"""


def kb_audit_v3_ddl_statements():
    """mart-ops-kb-qa-audit-v3：kb_qa_audit 加 token 用量列。"""
    return (_KB_QA_AUDIT_V3_DDL,)


# Web 通道角色白名单（2026-10-23 裁决：业务数据按角色白名单）——
# 小易 Web 前端用户默认只答商品/手册；行在册才开放日销/渠道/query_sql
# 等经营数据工具。独立于 bi_authz（那是看板授权，语义不混用）。
_KB_WEB_ACL_DDL = """CREATE TABLE IF NOT EXISTS `kb_web_acl` (
  `userid` varchar(128) NOT NULL COMMENT '钉钉 userid（免登带入）',
  `allow_business` tinyint NOT NULL DEFAULT 0 COMMENT '1=开放业务数据工具',
  `note` varchar(255) NULL,
  `created_at` datetime NOT NULL,
  PRIMARY KEY (`userid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def kb_web_acl_ddl_statements():
    """mart-ops-kb-web-acl-v1：Web 通道业务数据白名单表。"""
    return (_KB_WEB_ACL_DDL,)
