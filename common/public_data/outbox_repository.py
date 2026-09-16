"""``robot_outbox`` 仓储：robot（业务线）入队，gateway（apps 线）轮询投递。

投递契约（spec §9，已定）：robot 只算「该发什么」并写入 outbox 行，
``dingtalk-gateway`` 轮询 ``pending`` 投递并回写状态。

* **幂等由唯一键承载**：``dedupe_key = region:kind:business_date[:suffix]``，
  重复入队返回 ``False``、不覆盖——机器人当日内重跑不产生重复消息，
  现行 ``stateFile`` 的去重职责由本表接管。
* **审计即行数据**：谁在何时该发什么、投递状态、重试次数，全部可查。
* ``last_error`` 只写**错误码**——调用方不得把异常原文（可能含 URL、
  响应体、凭据片段）传进来；仓储再做一次长度截断兜底。
"""

import contextlib
import json
from datetime import date


_KINDS = frozenset({"remind", "check", "ding", "leaderboard"})

#: 投递重试上限：达到后行转 ``failed``，留给控制面告警，不再自动重试。
DEFAULT_MAX_ATTEMPTS = 5

_MAX_ERROR_LEN = 255


class OutboxError(RuntimeError):
    """outbox 入参非法（消息不得包含载荷内容）。"""


class OutboxRepository:
    def __init__(self, connection):
        self._connection = connection

    # ------------------------------------------------------------------
    # robot 侧
    # ------------------------------------------------------------------

    def enqueue(
        self,
        *,
        region,
        kind,
        business_date,
        title,
        body_md,
        at_user_ids=None,
        created_at,
        dedupe_suffix=None,
    ):
        """幂等入队：同一 ``dedupe_key`` 已存在 → 返回 ``False``（不覆盖）。"""
        if kind not in _KINDS:
            raise OutboxError(f"unknown outbox kind: {kind!r}")
        if not isinstance(region, str) or not region:
            raise OutboxError("outbox region must be a non-empty string")
        if not isinstance(title, str) or not title:
            raise OutboxError("outbox title must be a non-empty string")
        if not isinstance(body_md, str) or not body_md:
            raise OutboxError("outbox body_md must be a non-empty string")
        if not isinstance(business_date, date):
            raise OutboxError("outbox business_date must be a date")

        dedupe_key = f"{region}:{kind}:{business_date.isoformat()}"
        if dedupe_suffix:
            dedupe_key = f"{dedupe_key}:{dedupe_suffix}"

        sql = (
            "INSERT IGNORE INTO `robot_outbox` "
            "(`dedupe_key`, `region`, `kind`, `business_date`, `title`, "
            "`body_md`, `at_user_ids`, `status`, `attempts`, `created_at`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', 0, %s)"
        )
        payload = json.dumps(list(at_user_ids or []), ensure_ascii=False)
        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(sql, (
                dedupe_key, region, kind, business_date, title, body_md,
                payload, created_at,
            ))
            return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # gateway 侧
    # ------------------------------------------------------------------

    def fetch_pending(
        self, *, limit=100, max_attempts=DEFAULT_MAX_ATTEMPTS, skip_locked=True
    ):
        """取待投递行（最老优先）；``at_user_ids`` 已解析为 list。

        *skip_locked*（默认开）追加 ``FOR UPDATE SKIP LOCKED``：并发 gateway
        实例在事务内各领各行，不重复投递；锁随调用方批次提交释放。目标库
        为 MySQL 8.0+（集成环境 8.4）；旧内核可显式传 ``False`` 退化。
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise OutboxError("fetch_pending limit must be 1..1000")
        sql = (
            "SELECT `dedupe_key`, `region`, `kind`, `business_date`, `title`, "
            "`body_md`, `at_user_ids`, `attempts` "
            "FROM `robot_outbox` "
            "WHERE `status` = 'pending' AND `attempts` < %s "
            "ORDER BY `created_at` LIMIT %s"
        )
        if skip_locked:
            sql += " FOR UPDATE SKIP LOCKED"
        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(sql, (max_attempts, limit))
            rows = [dict(row) for row in cursor.fetchall()]
        for row in rows:
            raw = row.get("at_user_ids")
            row["at_user_ids"] = json.loads(raw) if isinstance(raw, str) else []
        return rows

    def mark_delivered(self, dedupe_key, *, delivered_at):
        """pending → delivered。带状态守卫，不会覆盖已终态的行。"""
        sql = (
            "UPDATE `robot_outbox` "
            "SET `status` = 'delivered', `delivered_at` = %s "
            "WHERE `dedupe_key` = %s AND `status` = 'pending'"
        )
        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(sql, (delivered_at, dedupe_key))
            return cursor.rowcount == 1

    def register_failure(
        self, dedupe_key, *, error_code, max_attempts=DEFAULT_MAX_ATTEMPTS
    ):
        """投递失败登记：attempts+1 并记录**错误码**；达上限 → ``failed``。

        *error_code* 必须是非泄露的错误码（如 ``"dingtalk_send_failed"``），
        不是异常原文。返回登记后的行是否已转终态 ``failed``。
        """
        code = str(error_code)[:_MAX_ERROR_LEN]
        sql = (
            "UPDATE `robot_outbox` SET "
            "`attempts` = `attempts` + 1, "
            "`last_error` = %s, "
            "`status` = IF(`attempts` + 1 >= %s, 'failed', 'pending') "
            "WHERE `dedupe_key` = %s AND `status` = 'pending'"
        )
        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(sql, (code, max_attempts, dedupe_key))
            if cursor.rowcount != 1:
                return None
            cursor.execute(
                "SELECT `status` FROM `robot_outbox` WHERE `dedupe_key` = %s",
                (dedupe_key,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        status = row["status"] if isinstance(row, dict) else row[0]
        return status == "failed"

    # ------------------------------------------------------------------
    # 控制面（死信观测与重投）
    # ------------------------------------------------------------------

    def list_failed(self, *, limit=100):
        """死信（``failed``）行查询：dedupe_key、重试次数与**错误码**。

        只返回可观测字段，不含 ``body_md`` 等载荷，供控制面/CLI 安全输出。
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise OutboxError("list_failed limit must be 1..1000")
        sql = (
            "SELECT `dedupe_key`, `region`, `kind`, `business_date`, "
            "`attempts`, `last_error`, `created_at` "
            "FROM `robot_outbox` "
            "WHERE `status` = 'failed' "
            "ORDER BY `created_at` LIMIT %s"
        )
        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(sql, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def requeue(self, dedupe_key):
        """死信重投：``failed`` → ``pending``，attempts 清零、错误码清空。

        带状态守卫：只命中 ``failed`` 行，返回是否实际重置（幂等——重复
        重投同一 key 第二次返回 ``False``，不影响在途投递）。
        """
        sql = (
            "UPDATE `robot_outbox` "
            "SET `status` = 'pending', `attempts` = 0, `last_error` = NULL "
            "WHERE `dedupe_key` = %s AND `status` = 'failed'"
        )
        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(sql, (dedupe_key,))
            return cursor.rowcount == 1
