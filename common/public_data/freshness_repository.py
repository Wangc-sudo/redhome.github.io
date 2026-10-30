"""数据新鲜度与知识索引新鲜度的持久化仓储。"""

import contextlib
import json
from datetime import date, datetime
from decimal import Decimal

from common.public_data.db import transaction


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value):
    return json.dumps(value, ensure_ascii=False, default=_json_default)


_FRESHNESS_DDL = """CREATE TABLE IF NOT EXISTS `pd_dataset_freshness` (
  `dataset_key` VARCHAR(64) NOT NULL,
  `status` VARCHAR(16) NOT NULL,
  `latest_business_date` DATE DEFAULT NULL,
  `last_collected_at` DATETIME(6) DEFAULT NULL,
  `last_processed_at` DATETIME(6) DEFAULT NULL,
  `last_published_at` DATETIME(6) DEFAULT NULL,
  `lag_minutes` INT DEFAULT NULL,
  `source_target_diff_json` JSON DEFAULT NULL,
  `downstream_consumers_json` JSON DEFAULT NULL,
  `last_run_id` VARCHAR(64) DEFAULT NULL,
  `check_details_json` JSON DEFAULT NULL,
  `error` VARCHAR(255) DEFAULT NULL,
  `checked_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`dataset_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

_KNOWLEDGE_FRESHNESS_DDL = """CREATE TABLE IF NOT EXISTS `pd_knowledge_index_freshness` (
  `index_key` VARCHAR(64) NOT NULL,
  `source_type` VARCHAR(32) NOT NULL,
  `source_id` VARCHAR(128) NOT NULL DEFAULT '',
  `source_version` VARCHAR(64) DEFAULT NULL,
  `content_hash` CHAR(64) DEFAULT NULL,
  `chunker_version` VARCHAR(32) DEFAULT NULL,
  `embedding_model` VARCHAR(128) DEFAULT NULL,
  `dim` INT DEFAULT NULL,
  `index_version` VARCHAR(64) DEFAULT NULL,
  `source_count` INT DEFAULT 0,
  `indexed_count` INT DEFAULT 0,
  `pending_count` INT DEFAULT 0,
  `failed_count` INT DEFAULT 0,
  `last_source_snapshot_at` DATETIME(6) DEFAULT NULL,
  `last_success_at` DATETIME(6) DEFAULT NULL,
  `status` VARCHAR(16) NOT NULL,
  `attempts` INT NOT NULL DEFAULT 0,
  `last_error` VARCHAR(512) DEFAULT NULL,
  `checked_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`index_key`),
  KEY `idx_knowledge_freshness_source` (`source_type`, `source_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def ddl_statements():
    return (_FRESHNESS_DDL, _KNOWLEDGE_FRESHNESS_DDL)


class FreshnessRepository:
    def __init__(self, connection):
        self._conn = connection

    def upsert(self, result, *, checked_at=None):
        checked_at = checked_at or datetime.utcnow()
        sql = """INSERT INTO `pd_dataset_freshness`
          (`dataset_key`, `status`, `latest_business_date`,
           `last_collected_at`, `last_processed_at`, `last_published_at`,
           `lag_minutes`, `source_target_diff_json`, `downstream_consumers_json`,
           `last_run_id`, `check_details_json`, `error`, `checked_at`)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON DUPLICATE KEY UPDATE
           `status`=VALUES(`status`),
           `latest_business_date`=VALUES(`latest_business_date`),
           `last_collected_at`=VALUES(`last_collected_at`),
           `last_processed_at`=VALUES(`last_processed_at`),
           `last_published_at`=VALUES(`last_published_at`),
           `lag_minutes`=VALUES(`lag_minutes`),
           `source_target_diff_json`=VALUES(`source_target_diff_json`),
           `downstream_consumers_json`=VALUES(`downstream_consumers_json`),
           `last_run_id`=VALUES(`last_run_id`),
           `check_details_json`=VALUES(`check_details_json`),
           `error`=VALUES(`error`),
           `checked_at`=VALUES(`checked_at`)"""
        with transaction(self._conn):
            with contextlib.closing(self._conn.cursor()) as cursor:
                cursor.execute(sql, (
                    result.dataset_key, result.status, result.latest_business_date,
                    result.last_collected_at, result.last_processed_at,
                    result.last_published_at, result.lag_minutes,
                    _json_dumps(result.source_target_diff)
                    if result.source_target_diff is not None else None,
                    _json_dumps(result.downstream_consumers),
                    result.last_run_id,
                    _json_dumps(result.check_details),
                    result.error, checked_at,
                ))

    def list_all(self, *, status=None, source=None, owner=None):
        conditions, params = [], []
        if status:
            conditions.append("`status` = %s")
            params.append(status)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM `pd_dataset_freshness`{where} ORDER BY `dataset_key`"
        with contextlib.closing(self._conn.cursor()) as cursor:
            cursor.execute(sql, tuple(params) or None)
            rows = cursor.fetchall()
        return [self._decode(row) for row in rows]

    def get(self, dataset_key):
        sql = "SELECT * FROM `pd_dataset_freshness` WHERE `dataset_key` = %s"
        with contextlib.closing(self._conn.cursor()) as cursor:
            cursor.execute(sql, (dataset_key,))
            row = cursor.fetchone()
        return self._decode(row) if row else None

    @staticmethod
    def _decode(row):
        item = dict(row)
        for key in ("source_target_diff_json", "downstream_consumers_json",
                    "check_details_json"):
            if key in item:
                target = key[:-5]
                value = item.pop(key)
                item[target] = json.loads(value) if value else None
        return item


class KnowledgeIndexFreshnessRepository:
    _STATUSES = {"fresh", "degraded", "stale", "blocked", "unknown"}

    def __init__(self, connection):
        self._conn = connection

    def record(self, item, *, checked_at=None):
        checked_at = checked_at or datetime.utcnow()
        status = item.get("status")
        if status not in self._STATUSES:
            raise ValueError(f"invalid knowledge index status: {status}")
        previous = self.get(item["index_key"]) or {}
        values = dict(previous)
        values.update(item)
        values["status"] = status
        values["attempts"] = (
            0 if status == "fresh"
            else int(previous.get("attempts") or 0) + 1
        )
        values["last_success_at"] = (
            checked_at if status == "fresh" else previous.get("last_success_at")
        )
        values["last_error"] = (
            None if status == "fresh" else item.get("last_error")
        )
        self.upsert(values, checked_at=checked_at)
        return values

    def check_sla(self, contract, *, checked_at=None):
        """持久化当前时刻的 SLA 判定，不刷新 last_success_at。"""
        from common.public_data.freshness import evaluate_knowledge_sla

        checked_at = checked_at or datetime.utcnow()
        previous = self.get(contract.dataset_key) or {
            "index_key": contract.dataset_key,
            "source_type": contract.source,
            "source_id": "",
            "source_count": 0,
            "indexed_count": 0,
            "pending_count": 0,
            "failed_count": 0,
            "attempts": 0,
        }
        status, error = evaluate_knowledge_sla(previous, contract, checked_at)
        values = dict(previous)
        values.update({
            "index_key": contract.dataset_key,
            "source_type": previous.get("source_type") or contract.source,
            "status": status,
            "last_error": error,
            "attempts": 0 if status == "fresh" else int(previous.get("attempts") or 0) + 1,
        })
        self.upsert(values, checked_at=checked_at)
        values["checked_at"] = checked_at
        return values

    def upsert(self, item, *, checked_at=None):
        checked_at = checked_at or datetime.utcnow()
        columns = (
            "index_key", "source_type", "source_id", "source_version",
            "content_hash", "chunker_version", "embedding_model", "dim",
            "index_version", "source_count", "indexed_count", "pending_count",
            "failed_count", "last_source_snapshot_at", "last_success_at",
            "status", "attempts", "last_error", "checked_at",
        )
        placeholders = ", ".join(["%s"] * len(columns))
        updates = ", ".join(
            f"`{c}`=VALUES(`{c}`)" for c in columns
            if c not in ("index_key", "last_success_at")
        )
        sql = (
            f"INSERT INTO `pd_knowledge_index_freshness` "
            f"({', '.join(f'`{c}`' for c in columns)}) VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {updates}, "
            "`last_success_at`=COALESCE(VALUES(`last_success_at`), "
            "`last_success_at`)"
        )
        values = dict(item)
        values["checked_at"] = checked_at
        with transaction(self._conn):
            with contextlib.closing(self._conn.cursor()) as cursor:
                cursor.execute(sql, tuple(values.get(c) for c in columns))

    def list_all(self, *, source_type=None, status=None):
        conditions, params = [], []
        if source_type:
            conditions.append("`source_type` = %s")
            params.append(source_type)
        if status:
            conditions.append("`status` = %s")
            params.append(status)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM `pd_knowledge_index_freshness`{where} ORDER BY `index_key`"
        with contextlib.closing(self._conn.cursor()) as cursor:
            cursor.execute(sql, tuple(params) or None)
            return [dict(row) for row in cursor.fetchall()]

    def get(self, index_key):
        sql = "SELECT * FROM `pd_knowledge_index_freshness` WHERE `index_key` = %s"
        with contextlib.closing(self._conn.cursor()) as cursor:
            cursor.execute(sql, (index_key,))
            row = cursor.fetchone()
        return dict(row) if row else None
