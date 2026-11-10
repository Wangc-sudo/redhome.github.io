"""Tests for the bi-web cockpit queries (plan Task 3).

Two layers:

* Unit tests (this file, always run): a fake DictCursor-shaped connection
  captures the executed SQL text.  Every query must be *fully static* --
  合计 rows excluded on the offline side, pre-filled future rows truncated
  everywhere, the annual-target scope filter, and never any ``%``
  formatting that splices a user value into the SQL.
* Integration tests (same file, ``INTEGRATION_TEST_RUNNER=1`` only): the
  real Docker Compose MySQL via the difference method, so assertions
  never couple to pre-existing data.
"""


import ast


import inspect


import os


import textwrap


import threading


import time


import unittest


from datetime import date, datetime, timedelta


from decimal import Decimal


from pathlib import Path


from common.bi_web import cards as bi_web_cards


from common.bi_web import queries as bi_web_queries


from common.bi_web.query_domains import (
    channel_dashboard as bi_web_channel_dashboard,
    offline_dashboards as bi_web_offline_dashboards,
)


from common.bi_web.queries import (
    annual_target_total,
    brand_options,
    channel_annual_total,
    channel_dod,
    channel_month_daily_series,
    channel_month_ranking,
    channel_mtd_comparison,
    channel_mtd_ranking,
    channel_mtd_total,
    channel_options,
    department_mtd_ranking,
    entity_options,
    granularity_options,
    manual_report_rows,
    month_bounds,
    month_options,
    offline_annual_total,
    offline_dod,
    offline_mtd_total,
    pivot_manual_rows,
    region_daily_series,
    region_month_daily_series,
    region_month_target,
    region_monthly_series,
    region_weekly_series,
    region_mtd_total,
    region_options,
    restaurant_annual_revenue,
    run_bar_channel_mtd,
    run_kpi_sku_mtd,
    run_table_sku_hot_brand,
    run_table_sku_hot_channel,
    run_table_sku_hot_total,
    run_bar_department_mtd,
    run_kpi_annual_progress,
    run_kpi_channel_dod,
    run_kpi_channel_mtd,
    run_kpi_offline_dod,
    run_kpi_offline_mtd,
    run_kpi_people_completed,
    run_kpi_people_count,
    run_kpi_people_rate,
    run_kpi_region_mtd,
    run_pie_sku_mtd,
    run_table_channel_mtd,
    run_table_manual_ecommerce_monthly,
    run_table_manual_restaurant_monthly,
    run_table_manual_showroom_monthly,
    run_kpi_fin_receivables_overdue,
    run_table_fin_receivables_aging,
    run_table_fin_prepayment_uninvoiced,
    run_table_fin_deposit_status,
    run_trend_fin_store_funds,
    run_trend_fin_store_funds_entity,
    run_table_inventory_aging,
    run_table_warehouse_ops,
    run_table_quarter_budget_actual,
    run_table_yoy_monthly,
    run_table_contract_writeoff,
    run_table_people_leaderboard,
    run_table_store_mtd,
    run_trend_channel_daily,
    run_trend_region_daily,
    shortfall_facts,
    sku_channel_options,
    sku_mtd_distribution,
    store_mtd_ranking,
)


from common.daily_robot.mart_leaderboard import mart_collect


from common.public_data.db import connect, transaction


from common.public_data.live_migrations import apply_live_migrations


from common.public_data.settings import Settings


from common.public_data.target_seed import load_target_seed, replace_dim_target


_MONTH_START = "business_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01')"


_YEAR_START = "business_date >= MAKEDATE(YEAR(CURDATE()), 1)"


_TRUNCATION = "business_date <= CURDATE()"


_SUMMARY_EXCLUSION = "responsible_person NOT LIKE '%合计%'"


_PARAM_SUMMARY_EXCLUSION = "responsible_person NOT LIKE '%%合计%%'"


_LINE_EXCLUSION = "region NOT IN ('电商', 'qudao', 'vanke')"


class FakeCursor:
    """Records executed SQL; serves scripted rows from its connection."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, query, parameters=None):
        self._connection.executed.append((query, parameters))

    def fetchone(self):
        return self._connection.scripted_fetchone(self._connection.executed[-1][0])

    def fetchall(self):
        return self._connection.scripted_fetchall(self._connection.executed[-1][0])

    def close(self):
        pass


class FakeConnection:
    """DictCursor-shaped fake whose rows are scripted per queried table.

    ``scalars`` maps table name -> the single summed value a total query
    should return; ``rowsets`` maps table name -> the list of rows a
    GROUP BY query should return.  Queries are recognised by the table
    they read, so scripting is independent of call order.
    """

    _TABLES = (
        "fact_daily_report_offline",
        "fact_channel_daily_sales",
        "fact_order_line",
        "dim_target",
        "months",  # 伪表键：month_options 的 UNION 查询横跨两张事实表
        "dim_calendar",  # l2-people: fetch_workdays 的日历查询
        "fact_manual_report",  # ④⑤⑪ 人工报表窄表
        "fact_fin_receivables_aging",  # ⑩ 应收账龄
        "fact_fin_prepayment_invoice",  # ⑩ 预付与未到票
        "fact_fin_store_funds",  # ⑩ 店铺资金余额
        "fact_fin_offline_deposit",  # ⑩ 保证金（线下+平台 UNION ALL）
        # 渠道旧页板块迁移二十一卡（2026-10-13）：broadcast fetch_* 命中的
        # 事实/维表（smoke 测试识别用，避免 _table_of 落到 None）。
        "fact_fin_store_expense",  # 平台账单（费用拆解）
        "fact_inventory_sku_daily",  # 库存快照（热卖/库存告急）
        "fact_sales_daily",  # 商品结构/GMV 归因
        "fact_purchase_inbound",  # 采购入库
        "dim_product",  # 商品系列维表
        "fact_refund_line",  # 退款原因
        "fact_order_cancel_line",  # 订单风险
        "fact_channel_store_target",  # 渠道店铺月目标真源
        # 归位覆盖并入部门排行（2026-10-19）：人级聚合后映射所需两表
        "dim_robot_member",
        "dim_org_assignment_override",
        # region 键→中文名（2026-10-19 英译中）：趋势图系列名映射
        "dim_org_region",
    )

    def __init__(self, scalars=None, rowsets=None):
        self.executed = []
        self._scalars = dict(scalars or {})
        self._rowsets = dict(rowsets or {})

    def cursor(self):
        return FakeCursor(self)

    def _table_of(self, sql):
        # 保证金合一查询是「线下 UNION ALL 平台」，先于通用 UNION→months
        # 规则归到线下键（该串不会出现在任何既有 SQL 中，行为零回归）。
        if "fact_fin_offline_deposit" in sql:
            return "fact_fin_offline_deposit"
        if "UNION" in sql:
            return "months"
        for table in self._TABLES:
            if table in sql:
                return table
        return None

    def scripted_fetchone(self, sql):
        value = self._scalars.get(self._table_of(sql))
        return {"total": Decimal("0") if value is None else value}

    def scripted_fetchall(self, sql):
        return [dict(row) for row in self._rowsets.get(self._table_of(sql), [])]


class _DodFakeConnection(FakeConnection):
    """DoD/水位卡两条 SQL 读同一张表：按 SQL 内容分流。

    MAX(business_date) 定锚查询走 ``latest``（未给时回落 scalars，
    保持 DoD 既有用法；None=空表，返回 None 行），窗口 GROUP BY
    查询照常走 rowsets，Σ 汇总走 scalars——B2 起 offline_mtd_total
    的定锚（date）与求和（Decimal）同表不同型，必须分开喂。
    """

    def __init__(self, scalars=None, rowsets=None, latest=None):
        super().__init__(scalars, rowsets)
        self._latest = None if latest is None else dict(latest)

    def scripted_fetchone(self, sql):
        if "MAX(business_date)" in sql:
            source = self._scalars if self._latest is None else self._latest
            value = source.get(self._table_of(sql))
            if value is None:
                return None
            return {"d": value}
        return super().scripted_fetchone(sql)


class SqlShapeTestCase(unittest.TestCase):
    """Shared helper: every query runs exactly one fully static statement."""

    def sole_static_sql(self, connection):
        self.assertEqual(1, len(connection.executed))
        sql, parameters = connection.executed[0]
        self.assertIsNone(parameters)
        return sql


class FakeSkuConnection(FakeConnection):
    """商品动销的脚本化连接：同一张事实表上的三次查询分开喂数。

    数据截至日（``MAX(DATE(trade_time))``）走 fetchone，月内日汇总与
    商品聚合走 fetchall——按 SQL 特征区分，与调用顺序无关。
    """

    def __init__(self, as_of=None, rollup_rows=None, trend_rows=None):
        super().__init__(rowsets={"fact_order_line": rollup_rows or []})
        self.as_of = as_of
        self.trend_rows = trend_rows or []

    def scripted_fetchone(self, sql):
        if "MAX(DATE(trade_time))" in sql:
            return {"d": self.as_of}
        return super().scripted_fetchone(sql)

    def scripted_fetchall(self, sql):
        if "GROUP BY DATE(trade_time)" in sql:
            return [dict(row) for row in self.trend_rows]
        return super().scripted_fetchall(sql)


class _TrendFakeConnection(_DodFakeConnection):
    """三档对拍的脚本化连接：日/周/月三条事实查询按分桶表达式分流。

    周档（WEEKDAY）与月档（DAYOFMONTH）各自喂 ``bucket_start`` 行，
    日档照常走 rowsets——与真实库中「同一批日行被 SQL 分桶」的
    产物一一对应，从而能对拍「同窗口三档合计相等」（B1 回归）。
    """

    def __init__(self, *args, week_rows=(), month_rows=(), **kwargs):
        super().__init__(*args, **kwargs)
        self._week_rows = [dict(row) for row in week_rows]
        self._month_rows = [dict(row) for row in month_rows]

    def scripted_fetchall(self, sql):
        if "WEEKDAY" in sql:
            return [dict(row) for row in self._week_rows]
        if "DAYOFMONTH" in sql:
            return [dict(row) for row in self._month_rows]
        return super().scripted_fetchall(sql)


class ParameterizedSqlShapeTests(unittest.TestCase):
    """阶段 B 参数化查询共用断言：恰好一条语句 + 参数元组逐位相等。

    另做两条通用 ``%`` 审计（FakeCursor 不做 pymysql 的 ``sql % params``
    格式化，漏写 ``%%`` 单测本不报错）：残留单 ``%`` 必须为 0；
    ``%s`` 占位符数量必须与参数元组长度一致。
    """

    def sole_parameterized_sql(self, connection, expected_params, *, table=None):
        # 三态改造（执行提示词 §4.2）：趋势序列多了 dim_calendar 营业日
        # 查询——传 table 时「恰好一条」收缩为该事实表语句，参数/%% 审计
        # 口径不变；不传 table 保持原语义（全部语句恰好一条）。
        statements = [
            (sql, parameters)
            for sql, parameters in connection.executed
            if table is None or table in sql
        ]
        self.assertEqual(1, len(statements))
        sql, parameters = statements[0]
        self.assertEqual(tuple(expected_params), parameters)
        self.assertEqual(0, sql.replace("%%", "").replace("%s", "").count("%"))
        self.assertEqual(sql.count("%s"), len(parameters))
        return sql


class _RegionFakeConnection(FakeConnection):
    """kpi_region_mtd 两条查询同表不同聚合：按 SQL 内容区分 value/target。"""

    def scripted_fetchone(self, sql):
        if "MAX(monthly_target)" in sql:
            value = self._scalars.get("region_target")
            return {"total": Decimal("0") if value is None else value}
        return super().scripted_fetchone(sql)


def _people_calendar_rows():
    """2026-08 工作日三日（供 mart 路径的 dim_calendar 查询）。"""
    return [
        {"business_date": date(2026, 8, 3)},
        {"business_date": date(2026, 8, 4)},
        {"business_date": date(2026, 8, 5)},
    ]


def _hangzhou_people_facts():
    """杭州三人形：张三（部分填写）、李四（无部门无目标）、合计行（须被排除）。"""
    return [
        {
            "region": "杭州",
            "responsible_person": "张三",
            "department": "零售一组",
            "business_date": date(2026, 8, 3),
            "sales_amount": Decimal("30"),
            "monthly_target": Decimal("100"),
        },
        {
            "region": "杭州",
            "responsible_person": "张三",
            "department": "零售一组",
            "business_date": date(2026, 8, 4),
            "sales_amount": Decimal("40"),
            "monthly_target": Decimal("100"),
        },
        {
            "region": "杭州",
            "responsible_person": "李四",
            "department": None,
            "business_date": date(2026, 8, 3),
            "sales_amount": Decimal("10"),
            "monthly_target": None,
        },
        {
            "region": "杭州",
            "responsible_person": "合计",
            "department": None,
            "business_date": date(2026, 8, 3),
            "sales_amount": Decimal("999"),
            "monthly_target": None,
        },
    ]


def _shaoxing_people_facts():
    """绍兴一人形：王五（rate 0.9，用于合并重排断言）。"""
    return [
        {
            "region": "绍兴",
            "responsible_person": "王五",
            "department": "绍兴一组",
            "business_date": date(2026, 8, 3),
            "sales_amount": Decimal("90"),
            "monthly_target": Decimal("100"),
        },
    ]


class _UnfilledFakeConnection(FakeConnection):
    """当日未填报人卡的脚本化连接：按 SQL 内容分流三张表。

    fetch_region_members（dim_robot_member，region+is_active 过滤、按
    姓名排序）/ fetch_scope_person_names（dim_report_roster，scope+
    enabled 过滤）/ fetch_filled_names（fact DISTINCT，region+当日过滤）。
    """

    def scripted_fetchall(self, sql):
        params = self.executed[-1][1] or ()
        if "dim_robot_member" in sql:
            rows = [
                dict(row)
                for row in self._rowsets.get("dim_robot_member", [])
                if row.get("region") == params[0] and row.get("is_active") == 1
            ]
            return sorted(rows, key=lambda row: row["name"])
        if "dim_report_roster" in sql:
            return [
                {"person_name": row["person_name"]}
                for row in self._rowsets.get("dim_report_roster", [])
                if row.get("scope") == params[0]
                and row.get("entity_type") == "person"
                and row.get("enabled") == 1
            ]
        if "DISTINCT" in sql:
            region, day = params
            return [
                {"responsible_person": row["responsible_person"]}
                for row in self._rowsets.get("fact_daily_report_offline", [])
                if row.get("region") == region
                and row.get("business_date") == day
            ]
        return super().scripted_fetchall(sql)


class _OaFakeConnection(FakeConnection):
    """线下整体看板 fake：事实行按最近一条语句的首个绑定参数（region）
    过滤；scope 查询按 SQL 形态分流——按日聚合 → ``{"d","s"}``、按人
    月目标 → ``{"p","t"}``（两路均镜像 NOT LIKE 合计 的排除语义）；其余
    （mart_collect 的 fetch_month_facts）回原始行。"""

    def _region_rows(self):
        sql, params = self.executed[-1]
        # 补集（「其他」桶，2026-10-16 三桶口径；2026-10-17 补排 qudao）：
        # region NOT IN (杭,绍,vanke,qudao)
        if "NOT IN" in sql:
            excludes = set(params[:4])
            return [
                row
                for row in self._rowsets.get("fact_daily_report_offline", [])
                if row.get("region") not in excludes
            ]
        region = params[0]
        return [
            row
            for row in self._rowsets.get("fact_daily_report_offline", [])
            if row.get("region") == region
        ]

    def scripted_fetchall(self, sql):
        if "fact_daily_report_offline" not in sql:
            return super().scripted_fetchall(sql)
        rows = self._region_rows()
        # 补集人员采集（SELECT ... AS rp/dept）：镜像别名
        if "`region` AS `region`" in sql:
            return [
                {"region": row.get("region"),
                 "rp": row.get("responsible_person"),
                 "dept": row.get("department"),
                 "business_date": row.get("business_date"),
                 "sales_amount": row.get("sales_amount"),
                 "monthly_target": row.get("monthly_target")}
                for row in rows
                if "合计" not in (row.get("responsible_person") or "")
            ]
        if "MAX(`monthly_target`)" in sql:
            by_person = {}
            for row in rows:
                if "合计" in row["responsible_person"]:
                    continue
                if row.get("monthly_target") is not None:
                    by_person[row["responsible_person"]] = row["monthly_target"]
            return [{"p": p, "t": t} for p, t in by_person.items()]
        if "GROUP BY `business_date`" in sql:
            by_day = {}
            for row in rows:
                if "合计" in row["responsible_person"]:
                    continue
                if row.get("sales_amount") is None:
                    continue
                day = row["business_date"]
                by_day[day] = by_day.get(day, Decimal("0")) + row["sales_amount"]
            return [{"d": d, "s": s} for d, s in sorted(by_day.items())]
        return [dict(row) for row in rows]


class _QdUnfilledFakeConnection(FakeConnection):
    """渠道未填报卡 fake：按 SQL 内容分流三路。

    ``fetch_roster_rows`` → ``fact_channel_store_target`` 全店（无参）+
    ``fact_channel_daily_sales`` DISTINCT 近窗（since, anchor）；
    ``fetch_daily_rows`` → ``fact_channel_daily_sales`` 锚点当日行。
    """

    def scripted_fetchall(self, sql):
        params = self.executed[-1][1] or ()
        if "fact_channel_store_target" in sql:
            return [
                dict(row)
                for row in self._rowsets.get("fact_channel_store_target", [])
            ]
        if "fact_channel_daily_sales" in sql:
            rows = self._rowsets.get("fact_channel_daily_sales", [])
            if "DISTINCT" in sql:
                since, anchor = params
                return [
                    {"store_name": row["store_name"], "channel": row["channel"]}
                    for row in rows
                    if row.get("business_date") is not None
                    and since <= row["business_date"] < anchor
                    and str(row.get("store_name") or "").strip()
                ]
            anchor = params[0]
            return [
                dict(row) for row in rows
                if row.get("business_date") == anchor
            ]
        return super().scripted_fetchall(sql)


class _PeopleFakeConnection(FakeConnection):
    """mart 路径 fake：事实行按最近一条语句的 region 参数过滤
    （模拟 fetch_month_facts 的 ``WHERE region = %s``）；日历行走 rowsets。"""

    def scripted_fetchall(self, sql):
        if "fact_daily_report_offline" in sql:
            region = self.executed[-1][1][0]
            rows = [
                row
                for row in self._rowsets.get("fact_daily_report_offline", [])
                if row.get("region") == region
            ]
            return [dict(row) for row in rows]
        return super().scripted_fetchall(sql)


class _GatedPeopleConnection(_PeopleFakeConnection):
    """采集 fetch 阻塞在门闩上：制造「采集进行中」的确定性窗口。

    压测 _people_snapshot 并发路径用——主线程等到 fetch_started 后再
    发同键请求，可精确复现两请求同时 miss 缓存的时序。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fetch_started = threading.Event()
        self._fetch_gate = threading.Event()

    def scripted_fetchall(self, sql):
        self.fetch_started.set()
        self._fetch_gate.wait(timeout=10)
        return super().scripted_fetchall(sql)

    def release_fetch(self):
        self._fetch_gate.set()


REPO_ROOT = Path(__file__).resolve().parents[2]


REPO_SEED_PATH = REPO_ROOT / "docker" / "integration" / "target.seed.json"


_FIXTURE_PREFIX = "biweb-test:"


_SYNC_RUN_ID = "00000000-0000-0000-0000-000000000001"


_OFFLINE_INSERT_SQL = (
    "INSERT INTO `fact_daily_report_offline` "
    "(`source_record_id`, `region`, `responsible_person`, `business_date`, "
    "`sales_amount`, `department`, `monthly_target`, `synced_at`, `sync_run_id`) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(6), %s)"
)


_CHANNEL_INSERT_SQL = (
    "INSERT INTO `fact_channel_daily_sales` "
    "(`source_record_id`, `channel`, `business_date`, `sales_amount`, "
    "`store_name`, `promotion_cost`, `synced_at`, `sync_run_id`) "
    "VALUES (%s, %s, %s, %s, %s, %s, NOW(6), %s)"
)


_OFFLINE_CLEANUP_SQL = (
    "DELETE FROM `fact_daily_report_offline` "
    "WHERE `source_record_id` LIKE 'biweb-test:%' OR `region` LIKE 'biweb%'"
)


_CHANNEL_CLEANUP_SQL = (
    "DELETE FROM `fact_channel_daily_sales` "
    "WHERE `source_record_id` LIKE 'biweb-test:%' "
    "OR `channel` = 'biweb测试渠道'"
)


_ORDER_LINE_INSERT_SQL = (
    "INSERT INTO `fact_order_line` "
    "(`trade_no`, `line_no`, `trade_time`, `trade_status`, `spec_no`, "
    "`goods_name`, `brand_name`, `quantity`, `paid_amount`, "
    "`platform_subsidy`, `shop_subsidy`, `raw_json`, `synced_at`, "
    "`sync_run_id`) "
    "VALUES (%s, 1, %s, '110', %s, %s, 'biweb测试品牌', 1, %s, 0, 0, "
    "'{}', NOW(6), %s)"
)


_ORDER_LINE_CLEANUP_SQL = (
    "DELETE FROM `fact_order_line` "
    "WHERE `trade_no` LIKE 'biweb-test:%' OR `brand_name` = 'biweb测试品牌'"
)

__all__ = tuple(name for name in globals() if not name.startswith("__"))
