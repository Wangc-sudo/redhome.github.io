"""提取层：``raw_dingtalk`` -> ``mart_ops``（spec §8 阶段 3）。

提取层是业务 mart 表的**唯一生产者**：业务线容器（robot、pages-leaderboard）
只读 ``mart_ops``，既不持源凭据、也不接触 ``raw_*``（spec §7）。

每次运行是一个独立的 ``sync_run_id``（spec §6「run_id 按线独立」），并复用
``sync_runs`` / ``sync_dataset_summary``，使控制面仍只有**一个**「线状态」
入口（spec §4）；摘要以 ``source_name='extract'`` 落库，与
``sync-dingtalk`` / ``sync-wdt`` 的摘要区分开。

投影只从本地 raw 重建、绝不重读源（spec §6：raw 是唯一可重放层）。默认
**增量**：有水位列（``synced_at``）的源表按上次成功运行的水位窗口过滤，
内容 digest 未变化的数据集整体跳过写入；``ON DUPLICATE KEY UPDATE``
保证任意窗口都可安全重放。构造开关 ``incremental=False`` /
``full_rebuild=True`` 可一键回退到全量替换语义。
"""

import contextlib
import hashlib
import logging
import re
import textwrap
from dataclasses import dataclass, field

from common.calendar_utils import month_days
from common.public_data.db import named_lock, transaction
from common.public_data.mart_extract_schema import (
    DIM_CALENDAR,
    DIM_ROBOT_MEMBER,
    EXTRACT_DATASETS,
)


#: fact 表 region 列统一为 region 键（组织架构口径）：AI 表原值是显示名，
#: raw 层保持源值不动，归一在投影落 mart 前完成（2026-09-22 双跑实测发现
#: 键/显示名撞车：事实行存"杭州"、查询端用 'hangzhou' → 榜单/未填全空）。
#: 未登记的显示名原样保留（新区域上线时先进本表，代码评审）。
_REGION_KEY_BY_DISPLAY = {
    "杭州": "hangzhou",
    "绍兴": "shaoxing",
}


def _normalize_region_keys(dataset, rows):
    """daily_report_offline 投影行的 region 显示名 → region 键（就地修改）。"""
    if dataset.dataset != "daily_report_offline":
        return rows
    for row in rows:
        display = str(row.get("region") or "").strip()
        if display in _REGION_KEY_BY_DISPLAY:
            row["region"] = _REGION_KEY_BY_DISPLAY[display]
    return rows


EXTRACT_SOURCE_NAME = "extract"

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: ``sync_dataset_summary`` 里工作日历那一条的数据集名。
CALENDAR_DATASET = "dim_calendar"

logger = logging.getLogger(__name__)

#: 窗口过滤使用的源侧水位列。raw 钉钉表统一带有 ``synced_at`` 技术列
#: （DATETIME(6)，含索引）；业务列 ``updated_at`` 多为 DATE 粒度，作水位
#: 会整天重放，故不采用。
WINDOW_WATERMARK_COLUMN = "synced_at"

_WINDOWED_TABLES_CACHE = None


def _windowed_source_tables():
    """返回拥有水位列、可窗口过滤的 raw 源表集合（惰性加载并缓存）。"""

    global _WINDOWED_TABLES_CACHE
    if _WINDOWED_TABLES_CACHE is None:
        from common.public_data.finance_schema import all_table_definitions

        _WINDOWED_TABLES_CACHE = frozenset(
            table.name for table in all_table_definitions()
        )
    return _WINDOWED_TABLES_CACHE


class MartExtractError(RuntimeError):
    """提取层无法完成一次运行（消息不得包含凭据或载荷）。"""


class _ProjectionFailure(Exception):
    """raw 侧已产出、但 mart 摘要步骤失败的内部标记。

    它不是公开 API，存在的唯一理由是让外层 ``extract`` 把「写 mart 失败」
    与「摘要失败」区分开，从而选择正确的 run 状态迁移（与 ``live_sync`` 同构）。
    """


@dataclass(frozen=True)
class ExtractResult:
    """一次提取运行（或恢复）后的摘要。"""

    run_id: str
    datasets: list = field(default_factory=list)


class MartExtractRepository:
    """读取 raw 表，并把事实投影与维度写入 mart。"""

    def __init__(self, raw_connection, mart_connection, wdt_connection=None):
        self._raw = raw_connection
        self._mart = mart_connection
        self._wdt = wdt_connection
        self._has_skipped_column = None

    @property
    def wdt_connection(self):
        if self._wdt is None:
            raise MartExtractError("wdt source connection is not configured")
        return self._wdt

    def read_table(self, table):
        if table not in {d.source_table for d in EXTRACT_DATASETS if d.source == "dingtalk"}:
            raise MartExtractError("unregistered source table")
        with contextlib.closing(self._raw.cursor()) as cursor:
            cursor.execute(f"SELECT * FROM `{table}`")
            return [dict(row) for row in cursor.fetchall()]

    def read_wdt_table(self, table):
        """读取 raw_wdt 侧已注册的维表（当前仅 ``dim_product``）。"""

        if table not in {
            d.source_table
            for d in EXTRACT_DATASETS
            if d.source == "wdt" and d.kind == "dim_mirror"
        }:
            raise MartExtractError("unregistered wdt source table")
        with contextlib.closing(self.wdt_connection.cursor()) as cursor:
            cursor.execute(f"SELECT * FROM `{table}`")
            return [dict(row) for row in cursor.fetchall()]

    def read_wdt_trades(self, source_method):
        """按接口方法读取 raw_wdt.wdt_records 的全部 payload。"""

        if not source_method:
            raise MartExtractError("missing wdt source method")
        sql = (
            "SELECT `source_record_id`, `payload_json` FROM `wdt_records` "
            "WHERE `source_method` = %s"
        )
        with contextlib.closing(self.wdt_connection.cursor()) as cursor:
            cursor.execute(sql, (source_method,))
            return [dict(row) for row in cursor.fetchall()]

    def read_mart_table(self, table, columns=None):
        """读取 mart 侧已注册的镜像目标表（当前仅 dim_product，供品牌反查）。

        *columns* 非空时只投影指定列（排查报告 2026-09-17 §2.3：品牌反查
        只需要 spec_no / brand_name，全列 ``SELECT *`` 随商品量线性膨胀）。
        """

        if table not in {
            d.target_table for d in EXTRACT_DATASETS if d.kind == "dim_mirror"
        }:
            raise MartExtractError("unregistered mart table")
        if columns:
            if any(not _IDENTIFIER_RE.match(c) for c in columns):
                raise MartExtractError("invalid projection column")
            projection = ", ".join(f"`{c}`" for c in columns)
        else:
            projection = "*"
        with contextlib.closing(self._mart.cursor()) as cursor:
            cursor.execute(f"SELECT {projection} FROM `{table}`")
            return [dict(row) for row in cursor.fetchall()]

    def replace_table(self, target_table, columns, rows):
        dataset = next((d for d in EXTRACT_DATASETS if d.target_table == target_table), None)
        replace_kinds = (
            "snapshot", "melt_store_funds", "dim_mirror", "order_line_expand",
            "stockout_line_expand", "refund_line_expand",
        )
        if dataset is None or dataset.kind not in replace_kinds:
            raise MartExtractError("unregistered snapshot table")
        allowed = set(dataset.target_columns) | {"synced_at", "sync_run_id"}
        if dataset.kind == "melt_store_funds":
            allowed |= {"store_name", "channel", "company_entity", "month", "balance"}
        if target_table == "fact_fin_prepayment_invoice":
            allowed.add("statement_date")
        if dataset.kind == "dim_mirror":
            from common.public_data.extract_order_line import _DIM_PRODUCT_MIRROR_COLUMNS
            allowed |= set(_DIM_PRODUCT_MIRROR_COLUMNS)
        if dataset.kind == "order_line_expand":
            from common.public_data.extract_order_line import _ORDER_LINE_COLUMNS
            allowed |= set(_ORDER_LINE_COLUMNS)
        if dataset.kind in ("stockout_line_expand", "refund_line_expand"):
            from common.public_data.extract_stock_flow import dataset_columns
            allowed |= set(dataset_columns(dataset))
        if not columns or len(set(columns)) != len(columns) or set(columns) != allowed:
            raise MartExtractError("invalid snapshot columns")
        params = [tuple(row.get(name) for name in columns) for row in rows]
        with transaction(self._mart):
            with contextlib.closing(self._mart.cursor()) as cursor:
                cursor.execute(f"DELETE FROM `{target_table}`")
                if params:
                    col_sql = ", ".join(f"`{name}`" for name in columns)
                    placeholders = ", ".join(["%s"] * len(columns))
                    cursor.executemany(
                        f"INSERT INTO `{target_table}` ({col_sql}) VALUES ({placeholders})", params,
                    )
        return len(rows)

    def read_dataset(self, dataset, since=None):
        """按列白名单读取 ``dataset.source_table`` 的行。

        返回字典的键即**目标列名**，所以投影只搬运被显式登记的列——
        作废列与钉钉技术列在 SQL 层就已排除，而非读出来再丢弃。

        *since* 非空时只读 ``synced_at`` 在该水位之后（含）的行：raw 侧
        upsert 会刷新 ``synced_at``，窗口内的行集恰好覆盖「上次提取以来
        变化过」的记录；闭区间 + mart 侧幂等 upsert，窗口可安全重放。
        """
        projections = ["`dingtalk_record_id` AS `source_record_id`"]
        projections += [
            f"`{source}` AS `{target}`" for source, target in dataset.columns
        ]
        sql = f"SELECT {', '.join(projections)} FROM `{dataset.source_table}`"
        params = None
        if since is not None:
            sql += f" WHERE `{WINDOW_WATERMARK_COLUMN}` >= %s"
            params = (since,)
        with contextlib.closing(self._raw.cursor()) as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def upsert_fact(self, dataset, rows, *, sync_run_id, synced_at):
        """把 *rows* 幂等写入 ``dataset.target_table``。"""
        columns = ["source_record_id", *dataset.target_columns]
        all_columns = [*columns, "synced_at", "sync_run_id"]
        placeholders = ", ".join(["%s"] * len(all_columns))
        column_list = ", ".join(f"`{name}`" for name in all_columns)
        update_clause = ", ".join(
            f"`{name}` = VALUES(`{name}`)"
            for name in all_columns
            if name != "source_record_id"
        )
        sql = (
            f"INSERT INTO `{dataset.target_table}` ({column_list}) "
            f"VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {update_clause}"
        )
        # executemany 批量写入（排查报告 2026-09-17 §2.1 P1）：一次
        # round-trip 替代逐行 N 次，语义与逐行 execute 完全等价。
        params = [
            tuple(
                [row.get("source_record_id")]
                + [row.get(name) for name in dataset.target_columns]
                + [synced_at, sync_run_id]
            )
            for row in rows
        ]
        if params:
            with contextlib.closing(self._mart.cursor()) as cursor:
                cursor.executemany(sql, params)

    def replace_dim_calendar(self, rows, *, sync_run_id, synced_at):
        """用 *rows* **整体替换** ``dim_calendar``。

        日历是提取层完全拥有的派生维度，所以种子里被移除的月份必须随之消失
        ——用替换而非 upsert。
        """
        with contextlib.closing(self._mart.cursor()) as cursor:
            cursor.execute(f"DELETE FROM `{DIM_CALENDAR}`")
            if rows:
                cursor.executemany(
                    "INSERT INTO `dim_calendar` "
                    "(`business_date`, `is_workday`, `source`, `note`, "
                    "`synced_at`, `sync_run_id`) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    [
                        (business_date, is_workday, source, note,
                         synced_at, sync_run_id)
                        for business_date, is_workday, source, note in rows
                    ],
                )

    def read_org_members(self):
        """读取 raw 通讯录快照中**最近一次 run** 的全员。

        raw 按 user_id upsert，离职成员的旧行仍在表里；「当前全集」由最新
        ``synced_at`` 的 ``sync_run_id`` 圈定。从未同步过时返回空列表——
        调用方据此**跳过**投影，而不是把维度清成空表。
        """
        sql = textwrap.dedent(
            """\
            SELECT `user_id`, `name`, `region`, `dept_id`, `dept_name`
            FROM `dingtalk_org_member`
            WHERE `sync_run_id` = (
              SELECT `sync_run_id` FROM `dingtalk_org_member`
              ORDER BY `synced_at` DESC LIMIT 1
            )"""
        )
        with contextlib.closing(self._raw.cursor()) as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def replace_dim_robot_member(self, rows, *, sync_run_id, synced_at):
        """用 *rows* **整体替换** ``dim_robot_member``。

        与 ``dim_calendar`` 同理：成员维度由提取层完全拥有，离职成员必须
        随之消失——替换而非 upsert。
        """
        with contextlib.closing(self._mart.cursor()) as cursor:
            cursor.execute(f"DELETE FROM `{DIM_ROBOT_MEMBER}`")
            if rows:
                cursor.executemany(
                    "INSERT INTO `dim_robot_member` "
                    "(`user_id`, `name`, `region`, `dept_id`, `dept_name`, "
                    "`is_active`, `synced_at`, `sync_run_id`) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            row["user_id"],
                            row["name"],
                            row["region"],
                            row.get("dept_id"),
                            row.get("dept_name"),
                            1,
                            synced_at,
                            sync_run_id,
                        )
                        for row in rows
                    ],
                )


    # ------------------------------------------------------------------
    # Incremental extraction
    # ------------------------------------------------------------------

    def upsert_dim_calendar(self, rows, *, sync_run_id, synced_at):
        """以 upsert 语义写入 ``dim_calendar``（增量默认路径）。

        效果与 ``replace_dim_calendar`` 等价（种子中消失的日期随之删除），
        但只写变化的行：命中主键原地更新，不在本次集合内的日期按行删除。
        整体在调用方事务内执行，重跑安全。
        """
        with contextlib.closing(self._mart.cursor()) as cursor:
            if not rows:
                cursor.execute(f"DELETE FROM `{DIM_CALENDAR}`")
                return
            cursor.executemany(
                textwrap.dedent(
                    """\
                    INSERT INTO `dim_calendar`
                    (`business_date`, `is_workday`, `source`, `note`,
                     `synced_at`, `sync_run_id`)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      `is_workday` = VALUES(`is_workday`),
                      `source` = VALUES(`source`),
                      `note` = VALUES(`note`),
                      `synced_at` = VALUES(`synced_at`),
                      `sync_run_id` = VALUES(`sync_run_id`)"""
                ),
                [
                    (business_date, is_workday, source, note,
                     synced_at, sync_run_id)
                    for business_date, is_workday, source, note in rows
                ],
            )
            placeholders = ", ".join(["%s"] * len(rows))
            cursor.execute(
                f"DELETE FROM `{DIM_CALENDAR}` "
                f"WHERE `business_date` NOT IN ({placeholders})",
                tuple(business_date for business_date, *_ in rows),
            )

    def upsert_dim_robot_member(self, rows, *, sync_run_id, synced_at):
        """以 upsert 语义写入 ``dim_robot_member``（增量默认路径）。

        与 ``replace_dim_robot_member`` 等价：离职成员（不在本次集合内）
        随之删除，在职成员原地更新。
        """
        with contextlib.closing(self._mart.cursor()) as cursor:
            if not rows:
                cursor.execute(f"DELETE FROM `{DIM_ROBOT_MEMBER}`")
                return
            cursor.executemany(
                textwrap.dedent(
                    """\
                    INSERT INTO `dim_robot_member`
                    (`user_id`, `name`, `region`, `dept_id`, `dept_name`,
                     `is_active`, `synced_at`, `sync_run_id`)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      `name` = VALUES(`name`),
                      `region` = VALUES(`region`),
                      `dept_id` = VALUES(`dept_id`),
                      `dept_name` = VALUES(`dept_name`),
                      `is_active` = VALUES(`is_active`),
                      `synced_at` = VALUES(`synced_at`),
                      `sync_run_id` = VALUES(`sync_run_id`)"""
                ),
                [
                    (
                        row["user_id"],
                        row["name"],
                        row["region"],
                        row.get("dept_id"),
                        row.get("dept_name"),
                        1,
                        synced_at,
                        sync_run_id,
                    )
                    for row in rows
                ],
            )
            placeholders = ", ".join(["%s"] * len(rows))
            cursor.execute(
                f"DELETE FROM `{DIM_ROBOT_MEMBER}` "
                f"WHERE `user_id` NOT IN ({placeholders})",
                tuple(row["user_id"] for row in rows),
            )

    def last_extract_started_at(self, dataset_name):
        """最近一次为该数据集写过摘要的 extract run 的 ``started_at``。

        作为窗口水位：摘要在该数据集数据提交之后才落库，所以该水位不会
        越过「已入库」边界；用 ``started_at`` 而非 ``completed_at``，避免
        漏掉「提取运行期间 raw 侧新写入」的行。从未写过摘要时返回
        ``None``（首次运行，调用方走全量）。
        """
        sql = textwrap.dedent(
            """\
            SELECT r.`started_at`
            FROM `sync_runs` r
            JOIN `sync_dataset_summary` s ON s.`sync_run_id` = r.`sync_run_id`
            WHERE s.`source_name` = %s AND s.`dataset_name` = %s
            ORDER BY r.`started_at` DESC
            LIMIT 1"""
        )
        with contextlib.closing(self._mart.cursor()) as cursor:
            cursor.execute(sql, (EXTRACT_SOURCE_NAME, dataset_name))
            rows = cursor.fetchall()
        return rows[0]["started_at"] if rows else None

    def last_summary_digest(self, dataset_name):
        """该数据集最近一次摘要的 ``record_id_digest``；无历史返回 ``None``。"""

        sql = textwrap.dedent(
            """\
            SELECT `record_id_digest`
            FROM `sync_dataset_summary`
            WHERE `source_name` = %s AND `dataset_name` = %s
            ORDER BY `completed_at` DESC
            LIMIT 1"""
        )
        with contextlib.closing(self._mart.cursor()) as cursor:
            cursor.execute(sql, (EXTRACT_SOURCE_NAME, dataset_name))
            rows = cursor.fetchall()
        return rows[0]["record_id_digest"] if rows else None

    def has_skipped_column(self):
        """探测 ``sync_dataset_summary.skipped`` 列是否已迁移（fail-open）。

        迁移未应用（或探测失败）时返回 ``False``，调用方降级为普通摘要
        并记录日志——绝不因缺列而中断提取。
        """
        if self._has_skipped_column is None:
            try:
                sql = textwrap.dedent(
                    """\
                    SELECT COUNT(*) AS `n`
                    FROM `information_schema`.`COLUMNS`
                    WHERE `TABLE_SCHEMA` = DATABASE()
                      AND `TABLE_NAME` = 'sync_dataset_summary'
                      AND `COLUMN_NAME` = 'skipped'"""
                )
                with contextlib.closing(self._mart.cursor()) as cursor:
                    cursor.execute(sql)
                    rows = cursor.fetchall()
                self._has_skipped_column = bool(
                    rows and next(iter(rows[0].values()))
                )
            except Exception:
                logger.warning(
                    "检测 sync_dataset_summary.skipped 列失败，按未迁移处理",
                    exc_info=True,
                )
                self._has_skipped_column = False
        return self._has_skipped_column

    def save_skipped_summary(
        self, *, sync_run_id, dataset_name, records_read,
        record_id_digest, completed_at,
    ):
        """写入「跳过」摘要：``raw_records_written=0``、``skipped=1``。"""

        sql = textwrap.dedent(
            """\
            INSERT INTO `sync_dataset_summary`
            (`sync_run_id`, `source_name`, `dataset_name`,
             `records_read`, `raw_records_written`,
             `record_id_digest`, `completed_at`, `skipped`)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"""
        )
        with contextlib.closing(self._mart.cursor()) as cursor:
            cursor.execute(
                sql,
                (sync_run_id, EXTRACT_SOURCE_NAME, dataset_name,
                 records_read, 0, record_id_digest, completed_at, 1),
            )


def _projector_for_kind(kind):
    """按 kind 惰性解析投影器，避免 extract_mart 与投影模块的硬依赖。"""

    if kind in ("snapshot", "melt_store_funds"):
        from common.public_data.extract_finance import (
            project_snapshot,
            project_store_funds_melt,
        )
        return {
            "snapshot": project_snapshot,
            "melt_store_funds": project_store_funds_melt,
        }[kind]
    if kind in ("dim_mirror", "order_line_expand"):
        from common.public_data.extract_order_line import (
            project_dim_product_mirror,
            project_order_lines,
        )
        return {
            "dim_mirror": project_dim_product_mirror,
            "order_line_expand": project_order_lines,
        }[kind]
    if kind in ("stockout_line_expand", "refund_line_expand"):
        from common.public_data.extract_stock_flow import (
            project_refund_lines,
            project_stockout_lines,
        )
        return {
            "stockout_line_expand": project_stockout_lines,
            "refund_line_expand": project_refund_lines,
        }[kind]
    return None


class MartExtractService:
    """把 raw 钉钉表投影到 ``mart_ops``，并物化工作日历维度。

    所有依赖均注入，因此测试不需要真实 MySQL、HTTP 或凭据。
    """

    def __init__(
        self,
        *,
        repository,
        mart_repository,
        mart_connection,
        now,
        new_run_id,
        datasets=EXTRACT_DATASETS,
        calendar_months=(),
        incremental=True,
        full_rebuild=False,
    ):
        self._repository = repository
        self._mart_repository = mart_repository
        self._mart_connection = mart_connection
        self._now = now
        self._new_run_id = new_run_id
        self._datasets = tuple(datasets)
        self._calendar_months = tuple(calendar_months)
        # 一键回退开关：incremental=False 关闭窗口过滤与 digest 跳过；
        # full_rebuild=True 进一步把 dim 写回「整表替换」旧语义。
        self._incremental = incremental
        self._full_rebuild = full_rebuild

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(self) -> ExtractResult:
        """执行一次提取运行。

        步骤：

        1. 起 run。``manifest_sha256`` 用**本次投影计划的摘要**填充——提取层
           没有 manifest，但计划本身就是「什么配置产出了这批数据」。
        2. 逐数据集：加锁 -> 读 raw -> 事务内写 mart -> 落摘要。
        3. 物化 ``dim_calendar``（未配置日历种子则跳过）。
        4. 标记 ``completed``。

        任何异常都会把 run 标记为 ``failed``；只有「mart 已写、摘要失败」
        才标记 ``projection_pending``。
        """
        run_id = self._new_run_id()
        synced_at = self._now()

        self._mart_repository.start_run(
            sync_run_id=run_id,
            manifest_sha256=self._plan_digest(),
            started_at=synced_at,
        )
        self._mart_connection.commit()

        datasets_summary = []
        try:
            for dataset in self._datasets:
                datasets_summary.append(
                    self._extract_dataset(dataset, run_id, synced_at)
                )

            if self._calendar_months:
                datasets_summary.append(
                    self._extract_calendar(run_id, synced_at)
                )

            org_summary = self._extract_org_members(run_id, synced_at)
            if org_summary is not None:
                datasets_summary.append(org_summary)

            self._mart_repository.mark_completed(
                sync_run_id=run_id,
                finished_at=self._now(),
            )
            self._mart_connection.commit()

        except _ProjectionFailure as exc:
            self._mart_repository.mark_projection_pending(
                sync_run_id=run_id,
                failure_code="projection_failed",
                finished_at=self._now(),
            )
            self._mart_connection.commit()
            raise exc.__cause__ from None

        except Exception as exc:
            self._mart_connection.rollback()
            self._mart_repository.mark_failed(
                sync_run_id=run_id,
                failure_code=self._failure_code(exc),
                finished_at=self._now(),
            )
            self._mart_connection.commit()
            raise

        return ExtractResult(run_id=run_id, datasets=datasets_summary)

    # ------------------------------------------------------------------
    # Per-output helpers
    # ------------------------------------------------------------------

    def _extract_dataset(self, dataset, run_id, synced_at):
        if dataset.kind != "fact":
            return self._extract_projection(dataset, run_id, synced_at)
        if dataset.source != "dingtalk":
            raise MartExtractError("unsupported extract source")
        with named_lock(
            self._mart_connection, f"public-data:extract:{dataset.dataset}"
        ):
            since, degrade_reason = self._read_window(dataset)
            if degrade_reason:
                logger.warning("dataset=%s %s", dataset.dataset, degrade_reason)
            rows = self._repository.read_dataset(dataset, since=since)
            rows = _normalize_region_keys(dataset, rows)
            record_ids = [row.get("source_record_id") for row in rows]
            digest = self._compute_digest(record_ids)
            skipped = self._should_skip_dataset(
                dataset.dataset, rows, digest, since
            )
            if skipped:
                logger.info(
                    "dataset=%s 无变化，跳过写入（records_read=%d）",
                    dataset.dataset, len(rows),
                )
            else:
                with transaction(self._mart_connection):
                    self._repository.upsert_fact(
                        dataset, rows, sync_run_id=run_id, synced_at=synced_at
                    )

        # raw 侧在提取层之前就已物化，所以这一步的含义是「首个产出已提交」；
        # 复用 sync_runs 既有状态机（started -> raw_committed -> completed）。
        self._mart_repository.mark_raw_committed(run_id)

        try:
            return self._save_summary(
                dataset=dataset.dataset,
                record_ids=record_ids,
                records_read=len(rows),
                records_written=0 if skipped else len(rows),
                run_id=run_id,
                skipped=skipped,
                digest=digest,
            )
        except Exception as exc:
            raise _ProjectionFailure() from exc

    def _read_window(self, dataset):
        """返回 ``(since, degrade_reason)``；``since=None`` 表示全量读取。

        无水位列的数据集**显式降级**为全量并给出原因（调用方记 WARNING），
        绝不静默；首次运行（无历史水位）记 INFO 后同样全量。
        """
        if not self._incremental or self._full_rebuild:
            return None, None
        if dataset.source_table not in _windowed_source_tables():
            return None, (
                f"源表 {dataset.source_table} 未登记 "
                f"{WINDOW_WATERMARK_COLUMN} 水位列，显式降级为全量扫描"
            )
        since = self._repository.last_extract_started_at(dataset.dataset)
        if since is None:
            logger.info(
                "dataset=%s 首次运行，无历史水位，全量扫描", dataset.dataset
            )
        return since, None

    def _should_skip_dataset(self, dataset_name, rows, digest, since):
        """窗口为空或（全量读取时）digest 命中历史摘要 → 跳过写入。"""
        if not rows:
            return True
        if not self._incremental or self._full_rebuild:
            return False
        if since is not None:
            # 窗口内是增量子集，与历史全量 digest 口径不同，不做比较。
            return False
        previous = self._repository.last_summary_digest(dataset_name)
        return previous is not None and previous == digest

    def _extract_projection(self, dataset, run_id, synced_at):
        projector = _projector_for_kind(dataset.kind)
        if projector is None:
            raise MartExtractError("unknown extract kind")
        with named_lock(self._mart_connection, f"public-data:extract:{dataset.dataset}"):
            result = projector(self._repository, dataset, run_id, synced_at)
        try:
            self._mart_repository.mark_raw_committed(run_id)
            summary = self._save_summary(
                dataset=dataset.dataset, record_ids=result.pop("_record_ids"),
                records_read=result["records_read"], records_written=result["records_new"],
                run_id=run_id,
            )
        except Exception as exc:
            raise _ProjectionFailure() from exc
        return {**summary, **result, "target_table": dataset.target_table}

    def _extract_calendar(self, run_id, synced_at):
        rows = []
        for year, month, rest_days, source in self._calendar_months:
            rest = set(rest_days)
            rows.extend(
                (day, 0 if day.day in rest else 1, source, None)
                for day in month_days(year, month)
            )

        # 跳过判定的 digest 覆盖整行内容（含 is_workday/source），而非只
        # 覆盖日期——休息日规则调整也必须触发重写。
        digest = self._content_digest(
            f"{business_date.isoformat()}|{is_workday}|{source}|{note or ''}"
            for business_date, is_workday, source, note in rows
        )
        skipped = (
            self._incremental
            and not self._full_rebuild
            and self._repository.last_summary_digest(CALENDAR_DATASET) == digest
        )
        if skipped:
            logger.info("dataset=%s 内容 digest 未变化，跳过写入", CALENDAR_DATASET)
        else:
            with transaction(self._mart_connection):
                if self._full_rebuild:
                    self._repository.replace_dim_calendar(
                        rows, sync_run_id=run_id, synced_at=synced_at
                    )
                else:
                    self._repository.upsert_dim_calendar(
                        rows, sync_run_id=run_id, synced_at=synced_at
                    )

        self._mart_repository.mark_raw_committed(run_id)

        try:
            return self._save_summary(
                dataset=CALENDAR_DATASET,
                record_ids=[business_date.isoformat() for business_date, *_ in rows],
                records_read=len(rows),
                records_written=0 if skipped else len(rows),
                run_id=run_id,
                skipped=skipped,
                digest=digest,
            )
        except Exception as exc:
            raise _ProjectionFailure() from exc

    def _extract_org_members(self, run_id, synced_at):
        """把最新通讯录快照投影为 ``dim_robot_member``。

        raw 为空（``sync-dingtalk`` 尚未同步过通讯录）时**跳过**并返回
        ``None``——不把维度清成空表；这对应「未配置组织同步」的环境，
        提取层的事实表与日历仍然有效。
        """
        rows = self._repository.read_org_members()
        if not rows:
            return None

        digest = self._content_digest(
            "{}|{}|{}|{}|{}".format(
                row["user_id"], row["name"], row["region"],
                row.get("dept_id") or "", row.get("dept_name") or "",
            )
            for row in rows
        )
        skipped = (
            self._incremental
            and not self._full_rebuild
            and self._repository.last_summary_digest(DIM_ROBOT_MEMBER) == digest
        )
        if skipped:
            logger.info("dataset=%s 内容 digest 未变化，跳过写入", DIM_ROBOT_MEMBER)
        else:
            with transaction(self._mart_connection):
                if self._full_rebuild:
                    self._repository.replace_dim_robot_member(
                        rows, sync_run_id=run_id, synced_at=synced_at
                    )
                else:
                    self._repository.upsert_dim_robot_member(
                        rows, sync_run_id=run_id, synced_at=synced_at
                    )

        self._mart_repository.mark_raw_committed(run_id)

        try:
            return self._save_summary(
                dataset=DIM_ROBOT_MEMBER,
                record_ids=[row["user_id"] for row in rows],
                records_read=len(rows),
                records_written=0 if skipped else len(rows),
                run_id=run_id,
                skipped=skipped,
                digest=digest,
            )
        except Exception as exc:
            raise _ProjectionFailure() from exc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _save_summary(
        self, *, dataset, record_ids, records_read, run_id,
        records_written=None, skipped=False, digest=None,
    ):
        if records_written is None:
            records_written = records_read
        if digest is None:
            digest = self._compute_digest(record_ids)
        if skipped:
            self._save_skipped_summary(
                dataset=dataset, records_read=records_read,
                digest=digest, run_id=run_id,
            )
        else:
            self._mart_repository.save_dataset_summary(
                sync_run_id=run_id,
                source_name=EXTRACT_SOURCE_NAME,
                dataset_name=dataset,
                records_read=records_read,
                raw_records_written=records_written,
                record_id_digest=digest,
                completed_at=self._now(),
            )
        return {
            "source": EXTRACT_SOURCE_NAME,
            "dataset": dataset,
            "records_read": records_read,
            "raw_records_written": records_written,
            "record_id_digest": digest,
            "skipped": skipped,
        }

    def _save_skipped_summary(self, *, dataset, records_read, digest, run_id):
        """写「跳过」摘要；skipped 列未迁移时降级为普通摘要 + WARNING。"""

        if self._repository.has_skipped_column():
            self._repository.save_skipped_summary(
                sync_run_id=run_id,
                dataset_name=dataset,
                records_read=records_read,
                record_id_digest=digest,
                completed_at=self._now(),
            )
            return
        logger.warning(
            "sync_dataset_summary 缺少 skipped 列（迁移未应用），"
            "dataset=%s 的跳过标记降级为普通摘要（raw_records_written=0）",
            dataset,
        )
        self._mart_repository.save_dataset_summary(
            sync_run_id=run_id,
            source_name=EXTRACT_SOURCE_NAME,
            dataset_name=dataset,
            records_read=records_read,
            raw_records_written=0,
            record_id_digest=digest,
            completed_at=self._now(),
        )

    def _plan_digest(self) -> str:
        parts = [
            f"{dataset.dataset}:{dataset.source}:{dataset.kind}:{dataset.source_table}->{dataset.target_table}:{dataset.columns!r}"
            for dataset in self._datasets
        ]
        parts += [
            "calendar:{}:{:02d}:{}:{}".format(
                year, month, ",".join(str(day) for day in rest_days), source
            )
            for year, month, rest_days, source in self._calendar_months
        ]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    @staticmethod
    def _compute_digest(ids) -> str:
        return hashlib.sha256(
            "\n".join(str(value) for value in sorted(ids)).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _content_digest(parts) -> str:
        """对任意字符串片段集合求稳定 digest（用于维度内容跳过判定）。"""

        return hashlib.sha256(
            "\n".join(sorted(parts)).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _failure_code(exc) -> str:
        if isinstance(exc, MartExtractError):
            return str(exc)
        return "extract_failed"
