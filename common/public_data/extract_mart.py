"""提取层：``raw_dingtalk`` -> ``mart_ops``（spec §8 阶段 3）。

提取层是业务 mart 表的**唯一生产者**：业务线容器（robot、pages-leaderboard）
只读 ``mart_ops``，既不持源凭据、也不接触 ``raw_*``（spec §7）。

每次运行是一个独立的 ``sync_run_id``（spec §6「run_id 按线独立」），并复用
``sync_runs`` / ``sync_dataset_summary``，使控制面仍只有**一个**「线状态」
入口（spec §4）；摘要以 ``source_name='extract'`` 落库，与
``sync-dingtalk`` / ``sync-wdt`` 的摘要区分开。

投影是**全量**的：raw 是唯一可重放层（spec §6），所以提取层从本地 raw 重建
整张 mart 表、绝不重读源；``ON DUPLICATE KEY UPDATE`` 保证幂等。
"""

import contextlib
import hashlib
from dataclasses import dataclass, field

from common.calendar_utils import month_days
from common.public_data.db import named_lock, transaction
from common.public_data.mart_extract_schema import DIM_CALENDAR, EXTRACT_DATASETS


EXTRACT_SOURCE_NAME = "extract"

#: ``sync_dataset_summary`` 里工作日历那一条的数据集名。
CALENDAR_DATASET = "dim_calendar"


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

    def __init__(self, raw_connection, mart_connection):
        self._raw = raw_connection
        self._mart = mart_connection

    def read_dataset(self, dataset):
        """按列白名单读取 ``dataset.source_table`` 的全部行。

        返回字典的键即**目标列名**，所以投影只搬运被显式登记的列——
        作废列与钉钉技术列在 SQL 层就已排除，而非读出来再丢弃。
        """
        projections = ["`dingtalk_record_id` AS `source_record_id`"]
        projections += [
            f"`{source}` AS `{target}`" for source, target in dataset.columns
        ]
        sql = f"SELECT {', '.join(projections)} FROM `{dataset.source_table}`"
        with contextlib.closing(self._raw.cursor()) as cursor:
            cursor.execute(sql)
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
        with contextlib.closing(self._mart.cursor()) as cursor:
            for row in rows:
                params = [row.get("source_record_id")]
                params += [row.get(name) for name in dataset.target_columns]
                params += [synced_at, sync_run_id]
                cursor.execute(sql, tuple(params))

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
    ):
        self._repository = repository
        self._mart_repository = mart_repository
        self._mart_connection = mart_connection
        self._now = now
        self._new_run_id = new_run_id
        self._datasets = tuple(datasets)
        self._calendar_months = tuple(calendar_months)

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
        with named_lock(
            self._mart_connection, f"public-data:extract:{dataset.dataset}"
        ):
            rows = self._repository.read_dataset(dataset)

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
                record_ids=[row.get("source_record_id") for row in rows],
                records_read=len(rows),
                run_id=run_id,
            )
        except Exception as exc:
            raise _ProjectionFailure() from exc

    def _extract_calendar(self, run_id, synced_at):
        rows = []
        for year, month, rest_days, source in self._calendar_months:
            rest = set(rest_days)
            rows.extend(
                (day, 0 if day.day in rest else 1, source, None)
                for day in month_days(year, month)
            )

        with transaction(self._mart_connection):
            self._repository.replace_dim_calendar(
                rows, sync_run_id=run_id, synced_at=synced_at
            )

        self._mart_repository.mark_raw_committed(run_id)

        try:
            return self._save_summary(
                dataset=CALENDAR_DATASET,
                record_ids=[business_date.isoformat() for business_date, *_ in rows],
                records_read=len(rows),
                run_id=run_id,
            )
        except Exception as exc:
            raise _ProjectionFailure() from exc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _save_summary(self, *, dataset, record_ids, records_read, run_id):
        digest = self._compute_digest(record_ids)
        self._mart_repository.save_dataset_summary(
            sync_run_id=run_id,
            source_name=EXTRACT_SOURCE_NAME,
            dataset_name=dataset,
            records_read=records_read,
            raw_records_written=records_read,
            record_id_digest=digest,
            completed_at=self._now(),
        )
        return {
            "source": EXTRACT_SOURCE_NAME,
            "dataset": dataset,
            "records_read": records_read,
            "raw_records_written": records_read,
            "record_id_digest": digest,
        }

    def _plan_digest(self) -> str:
        parts = [
            f"{dataset.dataset}:{dataset.source_table}->{dataset.target_table}"
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
    def _failure_code(exc) -> str:
        if isinstance(exc, MartExtractError):
            return str(exc)
        return "extract_failed"
