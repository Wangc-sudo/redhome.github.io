"""投影层：人工报表原始行 → ``mart_ops.fact_manual_report`` 窄表。

人工报表是**宽表**（一行一个门店/渠道，收入毛利费用各一列），BI 卡片要的
是**窄表**（一行一个指标）：模板每演进一次加一列指标，卡片 SQL 不该跟着
改。所以这里做一次 melt——指标列展开成 ``metric`` / ``value`` 行。

覆盖语义：**同一 dataset + 同一 period_type + 同一 period_start 整段先删
后插**。月报重导（改一版数）是覆盖，不是追加；历史不会被新旧两版混着
统计。
"""

import contextlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from common.public_data.manual_import.schema import MART_FACT_TABLE


@dataclass(frozen=True)
class FactRow:
    """``fact_manual_report`` 的一行。"""

    dataset: str
    period_type: str
    period_start: date
    period_end: date
    dim_scope: str
    dimension_value: str
    metric: str
    value: Decimal | None
    unit: str
    source_run_id: str


def build_fact_rows(rows, template, *, period_start, period_end, run_id):
    """把标准化行熔成窄表行（每个指标一行）。

    * 维度：模板里所有 ``role=dimension`` 的列，多维度时
      ``dim_scope`` 用 ``+`` 连接、``dimension_value`` 用 ``|`` 连接；
    * 指标：模板里所有 ``role=metric`` 的列，缺值不出行（留空比写 0 诚实）。
    """
    dimension_columns = template.dimension_columns
    metric_columns = template.metric_columns
    dim_scope = "+".join(column.field for column in dimension_columns)

    facts = []
    for row in rows:
        dimension_value = "|".join(
            str(row.fields.get(column.field, "")) for column in dimension_columns
        )
        for column in metric_columns:
            if column.field not in row.fields:
                continue  # 选填指标留空：不写 0，避免把「没填」当成「0」
            facts.append(FactRow(
                dataset=template.dataset,
                period_type=template.period_type,
                period_start=period_start,
                period_end=period_end,
                dim_scope=dim_scope,
                dimension_value=dimension_value,
                metric=column.field,
                value=_as_decimal(row.fields[column.field]),
                unit=column.unit,
                source_run_id=run_id,
            ))
    return tuple(facts)


class ManualProjector:
    """把 :class:`FactRow` 写入 ``mart_ops.fact_manual_report``。"""

    def __init__(self, connection):
        self._connection = connection

    def replace_facts(self, facts, *, synced_at) -> int:
        """整段覆盖 *facts* 所属的 dataset + 期间，返回写入行数。"""
        if not facts:
            return 0
        first = facts[0]
        delete_sql = (
            f"DELETE FROM `{MART_FACT_TABLE}` "
            "WHERE `dataset` = %s AND `period_type` = %s AND `period_start` = %s"
        )
        insert_sql = (
            f"INSERT INTO `{MART_FACT_TABLE}` "
            "(`dataset`, `period_type`, `period_start`, `period_end`, "
            "`dim_scope`, `dimension_value`, `metric`, `value`, `unit`, "
            "`source_run_id`, `synced_at`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "`period_end` = VALUES(`period_end`), "
            "`value` = VALUES(`value`), "
            "`unit` = VALUES(`unit`), "
            "`source_run_id` = VALUES(`source_run_id`), "
            "`synced_at` = VALUES(`synced_at`)"
        )
        with contextlib.closing(self._connection.cursor()) as cursor:
            cursor.execute(
                delete_sql, (first.dataset, first.period_type, first.period_start)
            )
            for fact in facts:
                cursor.execute(insert_sql, (
                    fact.dataset,
                    fact.period_type,
                    fact.period_start,
                    fact.period_end,
                    fact.dim_scope,
                    fact.dimension_value,
                    fact.metric,
                    fact.value,
                    fact.unit,
                    fact.source_run_id,
                    synced_at,
                ))
        return len(facts)


def _as_decimal(value):
    if value is None or isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
