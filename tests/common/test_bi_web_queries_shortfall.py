"""test_bi_web_queries 的 shortfall 域测试；断言自原文件等价迁移。"""

from tests.common.bi_web_query_test_support import *


class ShortfallRegionGrainTests(ParameterizedSqlShapeTests):
    """region 汇总粒度（``grain="region"``）SQL 形状：``has_fact`` 列必须引用
    右表真实存在的列 ``d.responsible_person``，而非 people 版别名 ``d.person``
    （后者在 region 子查询中不存在，会导致 MySQL ``Unknown column`` 崩溃）。"""

    def test_region_grain_sql_uses_real_join_column_for_has_fact(self):
        connection = FakeConnection(
            rowsets={"fact_daily_report_offline": []}
        )

        shortfall_facts(
            connection, grain="region",
            first_day=date(2026, 9, 1), last_day=date(2026, 9, 30),
        )

        sql = self.sole_parameterized_sql(
            connection,
            (date(2026, 9, 1), date(2026, 9, 30),
             date(2026, 9, 1), date(2026, 9, 30)),
        )
        self.assertIn("FROM fact_daily_report_offline", sql)
        self.assertIn("GROUP BY region, responsible_person", sql)
        # 关键回归守门：has_fact 基于右表主键 responsible_person
        self.assertIn(
            "(MAX(d.responsible_person) IS NOT NULL) AS has_fact", sql
        )
        # P0-1（2026-09-28）：region 粒度缺省同排 qudao/vanke/电商。
        self.assertIn(_LINE_EXCLUSION, sql)
        self.assertNotIn("d.person", sql)


class ShortfallDefaultLineTests(ParameterizedSqlShapeTests):
    """P0-1（2026-09-28）缺口族缺省区域口径回归守门。

    L1 缺口/告警卡不带 region 参数：缺省必须落**线下线全域**（排除
    qudao 电商集合口径与 vanke 体验馆——修复前「空片段=全区域」把
    23 名电商人员、1.62 亿月目标整个混进缺口榜）；显式选 region
    （l2-ecom-people 选 qudao）则逐字透传、不注入排除名单。两条
    通道分别钉死，缺一不可。
    """

    def test_default_excludes_off_line_regions(self):
        connection = FakeConnection(
            rowsets={"fact_daily_report_offline": []}
        )

        shortfall_facts(
            connection,
            first_day=date(2026, 9, 1), last_day=date(2026, 9, 30),
        )

        sql = self.sole_parameterized_sql(
            connection,
            (date(2026, 9, 1), date(2026, 9, 30),
             date(2026, 9, 1), date(2026, 9, 30)),
        )
        self.assertIn("FROM fact_daily_report_offline", sql)
        self.assertIn(_LINE_EXCLUSION, sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, sql)
        self.assertNotIn("region = %s", sql)

    def test_explicit_region_passes_through_without_line_exclusion(self):
        connection = FakeConnection(
            rowsets={"fact_daily_report_offline": []}
        )

        shortfall_facts(
            connection, region="qudao",
            first_day=date(2026, 9, 1), last_day=date(2026, 9, 30),
        )

        sql = self.sole_parameterized_sql(
            connection,
            (date(2026, 9, 1), date(2026, 9, 30), "qudao",
             date(2026, 9, 1), date(2026, 9, 30), "qudao"),
        )
        self.assertIn("region = %s", sql)
        self.assertNotIn(_LINE_EXCLUSION, sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, sql)
