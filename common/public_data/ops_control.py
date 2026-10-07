"""ops-web 定时任务管理（运维控制台）的数据访问与写面校验。

三张表（均落 mart_ops）：

* ``pd_ops_run_request`` —— 「立即运行一次」触发通道：ops-web 写入
  pending 行，scheduler 每 tick 轮询认领（状态机：pending → launched
  → finished/failed；无命令映射 → rejected）。``requested_by`` 即触发
  审计，无需另表。（迁移 ``mart-ops-run-requests-v1``）
* ``pd_ops_pipeline_audit`` —— 管道注册表变更流水：Nacos 发布本身不
  带操作人，enable/disable/add 每次变更在此落一行。
  （迁移 ``mart-ops-run-requests-v1``）
* ``pd_ops_run_history`` —— 定时任务**执行**流水：scheduler 每次实际
  点火（cron 与 run-once 同路径）先落 running 行、结束回写
  finished/failed + 退出码；锁被占用/运行异常（无退出码）也落 failed。
  依赖未满足的暂缓与非法 cron 不是"运行"，只走日志告警、不落本表。
  （迁移 ``mart-ops-run-history-v1``）

写纪律同 ``bi_authz``：SQL 只存在于本模块，ops-web / scheduler 经函数
调用；ops-web 侧每次变更与同事务的一行 audit 同生共死。
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from common.public_data.db import transaction
from common.public_data.pipeline_config import SUPPORTED_KINDS, PipelineConfig

#: service_id 形态：小写字母开头，小写字母/数字/下划线/中划线（与既有
#: 注册表键一致，如 robot-offline_all、pages-qudao-t1）。
SERVICE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

_AUDIT_DETAIL_MAX = 200

# ---------------------------------------------------------------------------
# DDL（CREATE TABLE IF NOT EXISTS 使重放幂等）
# ---------------------------------------------------------------------------

_RUN_REQUEST_DDL = (
    "CREATE TABLE IF NOT EXISTS `pd_ops_run_request` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `service_id` VARCHAR(64) NOT NULL,\n"
    "  `requested_by` VARCHAR(64) NOT NULL,\n"
    "  `status` ENUM('pending', 'launched', 'finished', 'failed', 'rejected')"
    " NOT NULL DEFAULT 'pending',\n"
    "  `note` VARCHAR(255) DEFAULT NULL,\n"
    "  `exit_code` INT DEFAULT NULL,\n"
    "  `created_at` DATETIME(6) NOT NULL,\n"
    "  `claimed_at` DATETIME(6) DEFAULT NULL,\n"
    "  `finished_at` DATETIME(6) DEFAULT NULL,\n"
    "  KEY `idx_status_created` (`status`, `created_at`),\n"
    "  KEY `idx_service_status` (`service_id`, `status`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_PIPELINE_AUDIT_DDL = (
    "CREATE TABLE IF NOT EXISTS `pd_ops_pipeline_audit` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `actor` VARCHAR(64) NOT NULL,\n"
    "  `action` VARCHAR(16) NOT NULL,\n"
    "  `service_id` VARCHAR(64) NOT NULL,\n"
    "  `detail` VARCHAR(255) DEFAULT NULL,\n"
    "  `created_at` DATETIME(6) NOT NULL,\n"
    "  KEY `idx_created` (`created_at`),\n"
    "  KEY `idx_service` (`service_id`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


_RUN_HISTORY_DDL = (
    "CREATE TABLE IF NOT EXISTS `pd_ops_run_history` (\n"
    "  `id` BIGINT AUTO_INCREMENT PRIMARY KEY,\n"
    "  `service_id` VARCHAR(64) NOT NULL,\n"
    "  `trigger_type` ENUM('cron', 'run_once') NOT NULL,\n"
    "  `status` ENUM('running', 'finished', 'failed')"
    " NOT NULL DEFAULT 'running',\n"
    "  `exit_code` INT DEFAULT NULL,\n"
    "  `note` VARCHAR(255) DEFAULT NULL,\n"
    "  `started_at` DATETIME(6) NOT NULL,\n"
    "  `finished_at` DATETIME(6) DEFAULT NULL,\n"
    "  KEY `idx_started` (`started_at`),\n"
    "  KEY `idx_service_started` (`service_id`, `started_at`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


def pipeline_ops_ddl_statements() -> tuple:
    """返回 run_request + pipeline_audit 两表 DDL（建表顺序即返回顺序）。"""
    return (_RUN_REQUEST_DDL, _PIPELINE_AUDIT_DDL)


def run_history_ddl_statements() -> tuple:
    """返回 run_history 表 DDL（独立迁移版本，校验和红线不改旧版本）。"""
    return (_RUN_HISTORY_DDL,)


# ---------------------------------------------------------------------------
# 写面校验（ops-web 新增管道表单）
# ---------------------------------------------------------------------------

class OpsControlError(ValueError):
    """定时任务管理写面数据无效（消息只含字段名，不含值）。"""


def _is_service_id(value):
    return isinstance(value, str) and SERVICE_ID_RE.match(value) is not None


def validate_pipeline_fields(service_id, kind, schedule, enabled=True,
                             description="", depends_on=()):
    """校验「新增管道」表单字段，返回规范化的 :class:`PipelineConfig`。

    schedule 必填且必须是合法 cron（这是定时任务管理面）；depends_on
    逐条按 service_id 形态校验、不得自依赖。任何字段非法抛
    :class:`OpsControlError`（消息只含字段名，不含值）。
    """
    if not isinstance(service_id, str):
        raise OpsControlError("service_id must match ^[a-z][a-z0-9_-]{0,63}$")
    service_id = service_id.strip()
    if not _is_service_id(service_id):
        raise OpsControlError("service_id must match ^[a-z][a-z0-9_-]{0,63}$")
    if kind not in SUPPORTED_KINDS:
        raise OpsControlError("kind must be one of apps/business")
    if not isinstance(schedule, str) or not schedule.strip():
        raise OpsControlError("schedule is required (cron expression)")
    schedule = schedule.strip()
    try:
        from croniter import croniter
        croniter(schedule)
    except (ValueError, KeyError):
        raise OpsControlError("schedule is not a valid cron expression")
    if not isinstance(enabled, bool):
        raise OpsControlError("enabled must be a boolean")
    if description is None:
        description = ""
    if not isinstance(description, str) or len(description) > 200:
        raise OpsControlError("description must be a string of at most 200 chars")
    deps = []
    for dep in depends_on or ():
        dep = dep.strip() if isinstance(dep, str) else dep
        if not _is_service_id(dep):
            raise OpsControlError("depends_on entries must be service ids")
        if dep == service_id:
            raise OpsControlError("depends_on must not include the service itself")
        if dep not in deps:
            deps.append(dep)
    if len(deps) > 8:
        raise OpsControlError("depends_on supports at most 8 entries")
    return PipelineConfig(
        service_id=service_id,
        enabled=enabled,
        kind=kind,
        schedule=schedule,
        description=description,
        depends_on=tuple(deps),
    )


# ---------------------------------------------------------------------------
# 「立即运行一次」请求（pd_ops_run_request）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunRequest:
    """一条待触发的运行请求（scheduler 轮询到的最小信息）。"""

    request_id: int
    service_id: str
    requested_by: str


def _utc_now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def insert_run_request(connection, service_id, requested_by, note=""):
    """登记一条运行请求（pending）。返回请求 id。"""
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "INSERT INTO `pd_ops_run_request` "
                "(`service_id`, `requested_by`, `status`, `note`, `created_at`) "
                "VALUES (%s, %s, 'pending', %s, %s)",
                (service_id, requested_by, note or None, _utc_now_text()),
            )
            return getattr(cursor, "lastrowid", None)
    finally:
        cursor.close()


def has_pending_request(connection, service_id):
    """该线是否已有待触发请求（ops-web 写入前去重）。"""
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT 1 AS hit FROM `pd_ops_run_request` "
            "WHERE `service_id` = %s AND `status` = 'pending' LIMIT 1",
            (service_id,),
        )
        return cursor.fetchone() is not None
    finally:
        cursor.close()


def fetch_pending_requests(connection, limit=50):
    """scheduler 轮询：全部 pending 请求（按提交顺序）。返回 RunRequest 元组。"""
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT `id`, `service_id`, `requested_by` FROM `pd_ops_run_request` "
            "WHERE `status` = 'pending' ORDER BY `id` LIMIT %s",
            (limit,),
        )
        rows = cursor.fetchall() if hasattr(cursor, "fetchall") else ()
        return tuple(
            RunRequest(
                request_id=(row["id"] if isinstance(row, dict) else row[0]),
                service_id=(row["service_id"] if isinstance(row, dict) else row[1]),
                requested_by=(row["requested_by"] if isinstance(row, dict) else row[2]),
            )
            for row in rows
        )
    finally:
        cursor.close()


def claim_run_request(connection, request_id):
    """pending → launched（认领）。返回是否认领成功（多副本并发安全）。"""
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "UPDATE `pd_ops_run_request` "
                "SET `status` = 'launched', `claimed_at` = %s "
                "WHERE `id` = %s AND `status` = 'pending'",
                (_utc_now_text(), request_id),
            )
            return getattr(cursor, "rowcount", 0) == 1
    finally:
        cursor.close()


def finish_run_request(connection, request_id, returncode):
    """运行结束回写：0 → finished；非 0 / None（锁占用或异常）→ failed。"""
    if returncode == 0:
        status, note = "finished", None
    elif returncode is None:
        status, note = "failed", "调度锁被占用或运行异常，未产生退出码"
    else:
        status, note = "failed", None
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "UPDATE `pd_ops_run_request` "
                "SET `status` = %s, `exit_code` = %s, "
                "`note` = COALESCE(%s, `note`), `finished_at` = %s "
                "WHERE `id` = %s AND `status` = 'launched'",
                (status, returncode, note, _utc_now_text(), request_id),
            )
    finally:
        cursor.close()


def reject_run_request(connection, request_id, note):
    """无命令映射等不可触发原因：pending → rejected。"""
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "UPDATE `pd_ops_run_request` "
                "SET `status` = 'rejected', `note` = %s, `finished_at` = %s "
                "WHERE `id` = %s AND `status` = 'pending'",
                ((note or "")[:200] or None, _utc_now_text(), request_id),
            )
    finally:
        cursor.close()


def fetch_recent_requests(connection, limit=100):
    """ops-web 展示：全部状态的最近请求（最新在前）。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise OpsControlError("requests limit must be 1..500")
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT `id`, `service_id`, `requested_by`, `status`, `note`, "
            "`exit_code`, `created_at`, `finished_at` "
            "FROM `pd_ops_run_request` ORDER BY `id` DESC LIMIT %s",
            (limit,),
        )
        rows = cursor.fetchall() if hasattr(cursor, "fetchall") else ()
        return tuple(rows)
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# 注册表变更审计（pd_ops_pipeline_audit）
# ---------------------------------------------------------------------------

def insert_pipeline_audit(connection, actor, action, service_id, detail=""):
    """落一行注册表变更流水（与 Nacos 发布同一请求内调用）。"""
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "INSERT INTO `pd_ops_pipeline_audit` "
                "(`actor`, `action`, `service_id`, `detail`, `created_at`) "
                "VALUES (%s, %s, %s, %s, %s)",
                (actor, action, service_id,
                 (detail or "")[:_AUDIT_DETAIL_MAX] or None, _utc_now_text()),
            )
    finally:
        cursor.close()


def fetch_pipeline_audit(connection, limit=200):
    """audit 流水（ops-web 定时任务页），最新在前。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise OpsControlError("audit limit must be 1..1000")
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT `actor`, `action`, `service_id`, `detail`, `created_at` "
            "FROM `pd_ops_pipeline_audit` ORDER BY `id` DESC LIMIT %s",
            (limit,),
        )
        rows = cursor.fetchall() if hasattr(cursor, "fetchall") else ()
        return tuple(rows)
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# 定时任务执行流水（pd_ops_run_history，scheduler 唯一写方）
# ---------------------------------------------------------------------------

def insert_run_history(connection, service_id, trigger_type):
    """落一行运行开始流水（running）。返回流水 id。"""
    if trigger_type not in ("cron", "run_once"):
        raise OpsControlError("trigger_type must be cron or run_once")
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "INSERT INTO `pd_ops_run_history` "
                "(`service_id`, `trigger_type`, `status`, `started_at`) "
                "VALUES (%s, %s, 'running', %s)",
                (service_id, trigger_type, _utc_now_text()),
            )
            return getattr(cursor, "lastrowid", None)
    finally:
        cursor.close()


def finish_run_history(connection, history_id, returncode):
    """运行结束回写：0 → finished；非 0 / None（锁占用或异常）→ failed。"""
    if returncode == 0:
        status, note = "finished", None
    elif returncode is None:
        status, note = "failed", "调度锁被占用或运行异常，未产生退出码"
    else:
        status, note = "failed", None
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute(
                "UPDATE `pd_ops_run_history` "
                "SET `status` = %s, `exit_code` = %s, "
                "`note` = COALESCE(%s, `note`), `finished_at` = %s "
                "WHERE `id` = %s AND `status` = 'running'",
                (status, returncode, note, _utc_now_text(), history_id),
            )
    finally:
        cursor.close()


def fetch_run_history(connection, limit=200, service_id=None):
    """执行流水（ops-web 执行流水页），最新在前；可按 service_id 过滤。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise OpsControlError("history limit must be 1..1000")
    if service_id is not None and not _is_service_id(service_id):
        raise OpsControlError("service_id")
    sql = (
        "SELECT `id`, `service_id`, `trigger_type`, `status`, `exit_code`, "
        "`note`, `started_at`, `finished_at` FROM `pd_ops_run_history`"
    )
    params = []
    if service_id is not None:
        sql += " WHERE `service_id` = %s"
        params.append(service_id)
    sql += " ORDER BY `id` DESC LIMIT %s"
    params.append(limit)
    cursor = connection.cursor()
    try:
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall() if hasattr(cursor, "fetchall") else ()
        return tuple(rows)
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# scheduler 侧网关（长驻进程：每次调用一条短连接，规避 RDS wait_timeout）
# ---------------------------------------------------------------------------

class DbRunRequestGateway:
    """scheduler 轮询 run-request 的生产实现（mart_ops 短连接）。

    任何 DB 异常抛给调用方：scheduler 按 tick 降级（本 tick 跳过、下
    tick 重查），绝不让触发通道故障拖垮 cron 主循环。
    """

    def __init__(self, connect, database_settings):
        self._connect = connect
        self._settings = database_settings

    def _call(self, fn, *args):
        connection = self._connect(self._settings)
        try:
            return fn(connection, *args)
        finally:
            connection.close()

    def fetch_pending(self):
        return self._call(fetch_pending_requests)

    def claim(self, request_id):
        return self._call(claim_run_request, request_id)

    def mark_finished(self, request_id, returncode):
        return self._call(finish_run_request, request_id, returncode)

    def mark_rejected(self, request_id, note):
        return self._call(reject_run_request, request_id, note)


class DbRunHistoryGateway:
    """scheduler 落执行流水的生产实现（mart_ops 短连接，同上游网关）。

    异常抛给调用方：scheduler 捕获后仅告警、绝不因流水故障阻断管道
    运行（审计是旁路，不是门禁）。
    """

    def __init__(self, connect, database_settings):
        self._connect = connect
        self._settings = database_settings

    def _call(self, fn, *args):
        connection = self._connect(self._settings)
        try:
            return fn(connection, *args)
        finally:
            connection.close()

    def record_start(self, service_id, trigger_type):
        return self._call(insert_run_history, service_id, trigger_type)

    def record_finish(self, history_id, returncode):
        return self._call(finish_run_history, history_id, returncode)
