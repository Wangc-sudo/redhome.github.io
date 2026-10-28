# -*- coding: utf-8 -*-
"""商品向量索引的持久状态：增量判断、失败保留与失效条目对账。

表结构由 :func:`ddl_statements` 提供给统一迁移入口；本模块不自建表。
每轮索引只预载一次，状态写入与失效清理均使用批量 SQL。
"""

import contextlib

from common.public_data.db import transaction

TABLE = "rag_index_state"
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
_MAX_ERROR_LEN = 512
_DELETE_BATCH = 500

_DDL = (
    "CREATE TABLE IF NOT EXISTS `rag_index_state` (\n"
    "  `item_id` VARCHAR(128) NOT NULL,\n"
    "  `record_id` VARCHAR(128) NOT NULL,\n"
    "  `kind` VARCHAR(16) NOT NULL,\n"
    "  `content_hash` CHAR(64) NOT NULL,\n"
    "  `model` VARCHAR(255) NOT NULL,\n"
    "  `dim` INT NOT NULL,\n"
    "  `last_seen_run` VARCHAR(64) NOT NULL,\n"
    "  `last_success_at` DATETIME(6) DEFAULT NULL,\n"
    "  `status` VARCHAR(16) NOT NULL,\n"
    "  `attempts` INT NOT NULL DEFAULT 0,\n"
    "  `last_error` VARCHAR(512) DEFAULT NULL,\n"
    "  PRIMARY KEY (`item_id`),\n"
    "  KEY `idx_rag_index_state_record` (`record_id`),\n"
    "  KEY `idx_rag_index_state_seen` (`last_seen_run`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_FIELDS = (
    "item_id", "record_id", "kind", "content_hash", "model", "dim",
    "last_seen_run", "last_success_at", "status", "attempts", "last_error",
)
_PRODUCT_WHERE = "(`item_id` LIKE 'product:%' OR `item_id` LIKE 'product-img:%')"


def ddl_statements() -> tuple:
    """返回 mart 库迁移语句，由 public-data 统一迁移入口执行。"""
    return (_DDL,)


class RagIndexStateRepository:
    """``rag_index_state`` 的批量仓储；连接使用既有 PyMySQL DictCursor。"""

    def __init__(self, connection):
        self._connection = connection

    def preload(self):
        """一次读取全部商品状态，返回 ``item_id -> state``。"""
        columns = ", ".join(f"`{name}`" for name in _FIELDS)
        sql = f"SELECT {columns} FROM `{TABLE}` WHERE {_PRODUCT_WHERE}"
        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
        return {row["item_id"]: dict(row) for row in rows}

    def save_batch(self, rows, *, run_id):
        """批量 upsert 本轮成功、未变及 degraded 状态。

        degraded 行的 ``last_success_at=None`` 不会覆盖已有成功时间；调用方
        仍保留旧 Milvus 向量，并以 status 阻止下一轮误判 unchanged。
        """
        rows = list(rows)
        if not rows:
            return 0
        columns = ", ".join(f"`{name}`" for name in _FIELDS)
        placeholders = ", ".join(["%s"] * len(_FIELDS))
        updates = ", ".join(
            f"`{name}`=VALUES(`{name}`)"
            for name in _FIELDS
            if name not in ("item_id", "last_success_at")
        )
        sql = (
            f"INSERT INTO `{TABLE}` ({columns}) VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {updates}, "
            f"`last_success_at`=COALESCE(VALUES(`last_success_at`), "
            f"`last_success_at`)"
        )
        params = []
        for row in rows:
            values = dict(row)
            values["last_seen_run"] = run_id
            error = values.get("last_error")
            values["last_error"] = (
                str(error)[:_MAX_ERROR_LEN] if error is not None else None)
            values["attempts"] = int(values.get("attempts") or 0)
            params.append(tuple(values.get(name) for name in _FIELDS))
        with transaction(self._connection):
            with contextlib.closing(self._connection.cursor()) as cursor:
                cursor.executemany(sql, params)
        return len(params)

    def reconcile(self, run_id, *, complete):
        """返回本轮未见到的商品向量 ID；源不完整或失败时严禁对账删除。"""
        if not complete:
            return []
        sql = (
            f"SELECT `item_id` FROM `{TABLE}` WHERE {_PRODUCT_WHERE} "
            "AND `last_seen_run` <> %s ORDER BY `item_id`"
        )
        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(sql, (run_id,))
            rows = cursor.fetchall()
        return [row["item_id"] for row in rows]

    def delete_ids(self, item_ids):
        """在向量删除成功后批量删除状态，SQL 数量不随条目逐行增长。"""
        ids = list(dict.fromkeys(item_ids))
        if not ids:
            return 0
        deleted = 0
        with transaction(self._connection):
            with contextlib.closing(self._connection.cursor()) as cursor:
                for start in range(0, len(ids), _DELETE_BATCH):
                    part = ids[start:start + _DELETE_BATCH]
                    placeholders = ", ".join(["%s"] * len(part))
                    cursor.execute(
                        f"DELETE FROM `{TABLE}` WHERE `item_id` IN ({placeholders})",
                        part,
                    )
                    deleted += len(part)
        return deleted
