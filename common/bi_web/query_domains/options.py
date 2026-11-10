"""BI options 查询域；SQL 与返回契约自原 queries.py 等价迁移。"""

from common.bi_web.query_domains.common import *
from common.bi_web.query_domains.sku import _BRAND_OPTIONS_SQL, _SKU_CHANNEL_OPTIONS_SQL


_REGION_OPTIONS_SQL = (
    "SELECT DISTINCT region FROM fact_daily_report_offline ORDER BY region"
)


_CHANNEL_OPTIONS_SQL = (
    "SELECT DISTINCT channel FROM fact_channel_daily_sales ORDER BY channel"
)


_MONTH_OPTIONS_SQL = textwrap.dedent(
    """
    SELECT DISTINCT DATE_FORMAT(business_date, '%Y-%m') AS month
    FROM fact_daily_report_offline
    UNION
    SELECT DISTINCT DATE_FORMAT(business_date, '%Y-%m')
    FROM fact_channel_daily_sales
    UNION
    SELECT DATE_FORMAT(CURDATE(), '%Y-%m')
    ORDER BY 1 DESC
    """
).strip()


def region_options(connection) -> list:
    """筛选器「区域」下拉：DISTINCT region，升序（SQL 全静态）。"""
    return [row["region"] for row in _fetch_rows(connection, _REGION_OPTIONS_SQL)
            if row["region"]]


def channel_options(connection) -> list:
    """筛选器「渠道」下拉：DISTINCT channel，升序（SQL 全静态）。"""
    return [row["channel"] for row in _fetch_rows(connection, _CHANNEL_OPTIONS_SQL)
            if row["channel"]]


def month_options(connection) -> list:
    """筛选器「月份」下拉：两事实表 DISTINCT 月 ∪ 当前月，降序。

    只出现有数据的月份（数据积累后自动出新选项）；当前月恒在列，
    无数据也允许选择（空结果而非 400，spec §8）。

    business_date 为 NULL 的行（事实表允许 NULL）产出 None 月，须过滤，
    与 region_options/channel_options 的空值处理一致。
    """
    return [
        row["month"]
        for row in _fetch_rows(connection, _MONTH_OPTIONS_SQL)
        if row["month"]
    ]


def brand_options(connection) -> list:
    """筛选器「品牌」下拉：fact_order_line DISTINCT brand_name，升序。"""
    return [row["brand_name"] for row in _fetch_rows(connection, _BRAND_OPTIONS_SQL)
            if row["brand_name"]]


def sku_channel_options(connection) -> list:
    """筛选器「渠道」下拉（商品口径）：DISTINCT channel_name，升序。"""
    return [row["channel_name"] for row in _fetch_rows(connection, _SKU_CHANNEL_OPTIONS_SQL)
            if row["channel_name"]]

__all__ = ('_REGION_OPTIONS_SQL', '_CHANNEL_OPTIONS_SQL', '_MONTH_OPTIONS_SQL', 'region_options', 'channel_options', 'month_options', 'brand_options', 'sku_channel_options')
