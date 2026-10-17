"""The bi-web card registry (stage A Task 3 / stage B Task 7).

The code-owned half of the ``SQL lives in code, layout lives in Nacos``
split: each card id binds a chart kind, the ``run`` function from
:mod:`common.bi_web.queries` and the URL-parameter whitelist.  Stage A
placed five parameter-less cards; stage B reuses two of them with
parameters (``trend_region_daily`` region+month, ``bar_channel_mtd``
month) and adds eleven cards; the SKU cockpit donut
(``pie_sku_mtd``, parameter-less) brings the total to seventeen; the
product-movement board (``l2-product``) adds four more (``kpi_sku_mtd``
plus the total / per-brand / per-channel hot-SKU tables) for
twenty-one in all; the derived-metric half of the CubeSchema brings two
more (``kpi_shortfall`` with the region/month whitelist and the
parameter-light ``anomaly_top``) for twenty-three; the manual-report
consumption pair (``table_manual_ecommerce_monthly`` /
``table_manual_restaurant_monthly``, dataset fixed per card id, no URL
parameters yet) brings the total to twenty-five; the fund-safety five
(``kpi_fin_receivables_overdue`` / ``table_fin_receivables_aging`` /
``table_fin_prepayment_uninvoiced`` / ``table_fin_deposit_status`` /
``trend_fin_store_funds``, derived via ``fin_derived``), the showroom
monthly clone (``table_manual_showroom_monthly``) and the five
placeholder structural cards (``table_inventory_aging`` /
``table_warehouse_ops`` / ``table_quarter_budget_actual`` /
``table_yoy_monthly`` / ``table_contract_writeoff``, static ``rows=[]``
with ``has_fact=false``) bring the total to thirty-six; the per-entity
view of ``trend_fin_store_funds`` (2026-09-17: 38 stores on one line chart
is unreadable, and the company entities change over time, so the four
hard-coded split cards were collapsed into one parameterized card
``trend_fin_store_funds_entity`` plus the ``entities`` filter source --
SQL aggregation pushed down, entity bound as ``%s``) brings it to
thirty-seven.  The day/week/month granularity batch (2026-09-18) keeps
the count at thirty-seven: three sample cards declare ``grans`` /
``default_gran`` (``trend_region_daily`` day+week+month default day;
``kpi_offline_mtd`` month only; ``trend_fin_store_funds_entity`` month
only) while ``kpi_offline_dod`` stays the undeclared control group --
cards without a declaration behave exactly as before.  The 2026-09-21
correction batch (订正方案 §4-A) re-opens ``kpi_offline_mtd`` to
day/week/month with ``gran`` entering the whitelist: its source table is
day-grained, so the month-only declaration was a fake control (the seg
rendered with no ``on`` state and no request ever carried ``gran``);
``default_gran="month"`` keeps the default path byte-identical.
The Hangzhou offline-sales board (2026-10-09, static leaderboard page to
BI behind the same URL) adds seven date-anchored cards
(``kpi_hz_mtd``/``kpi_hz_today``/``kpi_hz_progress``/``trend_hz_daily``/
``bar_hz_dept``/``table_hz_people_progress``/
``table_hz_unfilled_today``) for forty-four.  The offline-all board
(2026-10-10, ``8300/offline_all.html`` follows the same nginx reverse
proxy pattern) adds seven more (``kpi_oa_mtd``/``kpi_oa_today``/
``kpi_oa_progress``/``trend_oa_daily``/``bar_oa_dept``/
``table_oa_scopes``/``table_oa_people_progress``) for fifty-one.  The
Shaoxing offline-sales board (2026-10-10, ``8300/shaoxing.html`` same
pattern, mirroring the Hangzhou seven) adds
``kpi_sx_mtd``/``kpi_sx_today``/``kpi_sx_progress``/``trend_sx_daily``/
``bar_sx_dept``/``table_sx_people_progress``/
``table_sx_unfilled_today`` for fifty-eight.  The channel-sales board
(2026-10-13, ``8300/qudao.html`` same nginx-bridge pattern, sourcing the
channel fact table rather than the offline one) adds seven more
(``kpi_qd_mtd``/``kpi_qd_today``/``kpi_qd_progress``/``trend_qd_daily``/
``bar_qd_channel``/``table_qd_store_mtd``/``table_qd_unfilled_today``)
for sixty-five.  The same board's legacy pages-web panels
(2026-10-13, ``common/broadcast/qudao_panels.py``'s thirteen
channel-specific panels -- the rollback root cause) are migrated one
panel at a time into twenty date-anchored table cards
(``table_qd_monthly_kpi``/``table_qd_expense``/``table_qd_expense_rate``/
``table_qd_bill_category``/``table_qd_bill_platform``/
``table_qd_bill_items``/``table_qd_product_series``/
``table_qd_product_bands``/``table_qd_product_top``/
``table_qd_product_summary``/``table_qd_refund_reason``/
``table_qd_gmv_factors``/``table_qd_cohort``/
``table_qd_channel_daily``/``table_qd_hot_items``/
``table_qd_stock_urgent``/``table_qd_stock_oversold``/
``table_qd_purchase_inbound``/``table_qd_order_risk``/
``table_qd_notes``) for eighty-six.  Panels one through eight write no
new SQL -- each reuses ``common.broadcast.queries``' existing
``fetch_*`` outputs; panel nine reuses the channel daily-report
functions plus the store-granular monthly target source.

The whitelist maps param name -> filter source from
``config.KNOWN_FILTER_SOURCES``: the app layer resolves the source to a
dimension lookup for the value gate (invalid value -> 400).  The registry
is validated at import time -- an unknown chart kind or an unknown
filter source fails the import, so configuration drift can never reach a
running process silently.  :func:`validate_dashboard_config` closes the
loop with Task 2's :class:`~common.bi_web.config.DashboardConfig` -- a
dashboard may only place card ids that exist in the registry.
"""

from dataclasses import dataclass, replace
from typing import Callable

from common.bi_web import queries
from common.bi_web.config import KNOWN_FILTER_SOURCES

#: The chart kinds the front end can render (stage A uses scalar/line/bar).
KNOWN_CHARTS = ("scalar", "line", "bar", "table", "pie")


class CardConfigError(ValueError):
    """注册表/看板配置错误：未知 chart、未知筛选 source 或未知 card_id。"""


@dataclass(frozen=True)
class Card:
    """One registry entry.

    ``run`` is ``(connection, params) -> chart-ready dict`` (see
    :mod:`common.bi_web.queries`); ``params_schema`` maps param name ->
    filter source name (``regions``/``channels``/``months``) -- the
    URL-parameter whitelist and its value domain in one mapping.
    ``grans``/``default_gran`` declare the day/week/month granularity
    a card accepts (2026-09-18 batch); both default to empty, so every
    undeclared card keeps its pre-granularity behaviour untouched.

    ``row_scope`` declares whether the card's data carries a region
    dimension (``"region"``) or is explicitly exempt (``None``, the
    default).  ``"region"`` cards MUST consume the viewer's
    ``allowed_regions`` -- the app layer injects it into ``params`` (see
    :data:`common.bi_web.queries.ROW_SCOPE_PARAM`) and the per-card
    contract test pins that consumption; ``None`` cards must NOT emit a
    region whitelist predicate.  Design 2026-09-21 §4.1/§6: an admitted
    non-admin viewer with an empty grant set sees no region rows at all
    (fail-closed), and admin / the Bearer machine channel stay full-view.
    """

    card_id: str
    chart: str
    run: Callable
    params_schema: dict
    grans: tuple = ()
    default_gran: str = ""
    row_scope: str = ""


def _card(card_id, chart, run, params_schema=None, grans=(), default_gran=""):
    """Build one card with both halves import-time validated.

    An unknown chart kind or an unknown filter source raises here, at
    module load -- the same protection class for both.  A declared
    ``default_gran`` must be one of the declared ``grans``.
    """
    if chart not in KNOWN_CHARTS:
        raise CardConfigError(f"card '{card_id}' has unknown chart '{chart}'")
    schema = dict(params_schema or {})
    if any(source not in KNOWN_FILTER_SOURCES for source in schema.values()):
        raise CardConfigError(f"card '{card_id}' has an unknown filter source")
    grans = tuple(grans)
    if default_gran and default_gran not in grans:
        raise CardConfigError(f"card '{card_id}' default_gran not in grans")
    return Card(card_id=card_id, chart=chart, run=run, params_schema=schema,
                grans=grans, default_gran=default_gran)


_CARDS = (
    # 粒度批次 A（2026-09-21 订正）：kpi_offline_mtd 开通日/周/月三档——
    # 源表 fact_daily_report_offline 本就是日粒度，"仅月一档 + 无 gran
    # 白名单"渲染的是无选中态的假控件。default_gran=month：缺省请求
    # （app 层注入 gran=month）与开通前逐字节一致；日=水位日当日值、
    # 周=锚点所在自然周累计（语义见 run_kpi_offline_mtd）。
    # 批次 A5（同日）：补"年"档（年首→水位，YTD 窗口）——用户口径
    # 「数据维度改成年月日」，周档保留。
    _card(
        "kpi_offline_mtd", "scalar", queries.run_kpi_offline_mtd,
        {"month": "months", "gran": "granularity"},
        grans=("day", "week", "month", "year"), default_gran="month",
    ),
    _card("kpi_channel_mtd", "scalar", queries.run_kpi_channel_mtd),
    _card("kpi_annual_progress", "scalar", queries.run_kpi_annual_progress),
    # 粒度样板卡：trend_region_daily 三档、默认日（gran=day ≡ 不带参）。
    _card(
        "trend_region_daily", "line", queries.run_trend_region_daily,
        {"region": "regions", "month": "months", "gran": "granularity"},
        grans=("day", "week", "month"), default_gran="day",
    ),
    _card(
        "bar_channel_mtd", "bar", queries.run_bar_channel_mtd,
        {"month": "months"},
    ),
    _card("kpi_offline_dod", "scalar", queries.run_kpi_offline_dod),
    _card("kpi_channel_dod", "scalar", queries.run_kpi_channel_dod),
    _card(
        "kpi_region_mtd", "scalar", queries.run_kpi_region_mtd,
        {"region": "regions", "month": "months"},
    ),
    _card(
        "bar_department_mtd", "bar", queries.run_bar_department_mtd,
        {"region": "regions", "month": "months"},
    ),
    _card(
        "trend_channel_daily", "line", queries.run_trend_channel_daily,
        {"month": "months"},
    ),
    _card(
        "table_channel_mtd", "table", queries.run_table_channel_mtd,
        {"month": "months"},
    ),
    _card(
        "table_store_mtd", "table", queries.run_table_store_mtd,
        {"channel": "channels", "month": "months"},
    ),
    _card(
        "kpi_people_count", "scalar", queries.run_kpi_people_count,
        {"region": "regions", "month": "months"},
    ),
    _card(
        "kpi_people_completed", "scalar", queries.run_kpi_people_completed,
        {"region": "regions", "month": "months"},
    ),
    _card(
        "kpi_people_rate", "scalar", queries.run_kpi_people_rate,
        {"region": "regions", "month": "months"},
    ),
    _card(
        "table_people_leaderboard", "table",
        queries.run_table_people_leaderboard,
        {"region": "regions", "month": "months"},
    ),
    # 杭州线下销售BI看板（hz-offline-sales，2026-10-09 裁决）：区域钉死
    # 杭州、日期锚定 date（工作日日历点选，值域 dates）；静态榜单页
    # （8300/hangzhou.html）的 BI 替身，nginx 反代保留原 URL。
    _card("kpi_hz_mtd", "scalar", queries.run_kpi_hz_mtd,
          {"date": "dates"}),
    _card("kpi_hz_today", "scalar", queries.run_kpi_hz_today,
          {"date": "dates"}),
    _card("kpi_hz_progress", "scalar", queries.run_kpi_hz_progress,
          {"date": "dates"}),
    _card(
        "trend_hz_daily", "line", queries.run_trend_hz_daily,
        {"date": "dates", "gran": "granularity"},
        grans=("day", "week", "month"), default_gran="day",
    ),
    _card("bar_hz_dept", "bar", queries.run_bar_hz_dept,
          {"date": "dates"}),
    _card("table_hz_people_progress", "table",
          queries.run_table_hz_people_progress, {"date": "dates"}),
    # 当日未填报人（2026-10-10 运维反馈）：与 18:00 提醒/20:00 催办
    # 同真源（mart_tasks._unfilled，名册绑定 fail-open）。
    _card("table_hz_unfilled_today", "table",
          queries.run_table_hz_unfilled_today, {"date": "dates"}),
    # 部门进度表（2026-10-17 部门主视角裁决）：部门×人数×月累计/月目标/
    # 达成率/进度差；部门归属=通讯录归位（与 oa 部门卡同口径）。
    _card("table_hz_dept_progress", "table",
          queries.run_table_hz_dept_progress, {"date": "dates"}),
    # 线下整体销售BI看板（offline-all-sales，2026-10-10 裁决）：线下线
    # 全域（汇总卡）+ 三区合并人员/部门/板块（真源同播报/静态页函数）；
    # 静态榜单页（8300/offline_all.html）的 BI 替身，nginx 反代保留原 URL。
    _card("kpi_oa_mtd", "scalar", queries.run_kpi_oa_mtd,
          {"date": "dates"}),
    _card("kpi_oa_today", "scalar", queries.run_kpi_oa_today,
          {"date": "dates"}),
    _card("kpi_oa_progress", "scalar", queries.run_kpi_oa_progress,
          {"date": "dates"}),
    _card(
        "trend_oa_daily", "line", queries.run_trend_oa_daily,
        {"date": "dates", "gran": "granularity"},
        grans=("day", "week", "month"), default_gran="day",
    ),
    _card("bar_oa_dept", "bar", queries.run_bar_oa_dept,
          {"date": "dates"}),
    _card("table_oa_scopes", "table",
          queries.run_table_oa_scopes, {"date": "dates"}),
    _card("table_oa_people_progress", "table",
          queries.run_table_oa_people_progress, {"date": "dates"}),
    _card("table_oa_dept_progress", "table",
          queries.run_table_oa_dept_progress, {"date": "dates"}),
    # 绍兴线下销售BI看板（sx-offline-sales，2026-10-10 裁决）：与杭州
    # 看板同构（区域钉绍兴）；8300/shaoxing.html 的 BI 替身，nginx
    # 反代保留原 URL。
    _card("kpi_sx_mtd", "scalar", queries.run_kpi_sx_mtd,
          {"date": "dates"}),
    _card("kpi_sx_today", "scalar", queries.run_kpi_sx_today,
          {"date": "dates"}),
    _card("kpi_sx_progress", "scalar", queries.run_kpi_sx_progress,
          {"date": "dates"}),
    _card(
        "trend_sx_daily", "line", queries.run_trend_sx_daily,
        {"date": "dates", "gran": "granularity"},
        grans=("day", "week", "month"), default_gran="day",
    ),
    _card("bar_sx_dept", "bar", queries.run_bar_sx_dept,
          {"date": "dates"}),
    _card("table_sx_people_progress", "table",
          queries.run_table_sx_people_progress, {"date": "dates"}),
    _card("table_sx_unfilled_today", "table",
          queries.run_table_sx_unfilled_today, {"date": "dates"}),
    _card("table_sx_dept_progress", "table",
          queries.run_table_sx_dept_progress, {"date": "dates"}),
    # 渠道销售BI看板（qd-channel-sales，2026-10-13 裁决）：与三条线下
    # 看板同构（业务线为渠道），8300/qudao.html 的 BI 替身，nginx 反代
    # 保留原 URL。真源 = 渠道线事实表（汇总/趋势/排行/店铺）与渠道催办
    # channel_missing（未填报），trend 卡不开 gran（run_trend_channel_daily
    # 无 gran 支持，避免假控件）。
    _card("kpi_qd_mtd", "scalar", queries.run_kpi_qd_mtd,
          {"date": "dates"}),
    _card("kpi_qd_today", "scalar", queries.run_kpi_qd_today,
          {"date": "dates"}),
    _card("kpi_qd_progress", "scalar", queries.run_kpi_qd_progress,
          {"date": "dates"}),
    _card("trend_qd_daily", "line", queries.run_trend_qd_daily,
          {"date": "dates"}),
    _card("bar_qd_channel", "bar", queries.run_bar_qd_channel,
          {"date": "dates"}),
    _card("table_qd_store_mtd", "table", queries.run_table_qd_store_mtd,
          {"date": "dates"}),
    _card("table_qd_unfilled_today", "table",
          queries.run_table_qd_unfilled_today, {"date": "dates"}),
    # 渠道旧页板块补齐（2026-10-13 运维裁决）：旧 /qudao.html 由 pages-web
    # 追加 13 个渠道专属面板（common/broadcast/qudao_panels.py），首版切 BI
    # 只复刻了 7 张离线模板卡，13 个板块丢失故撤桥。本节把 13 个板块逐块
    # 迁成 21 张表卡（一板块可拆多卡），全部锚定 date；面板 1-8 不新写 SQL，
    # 全部复用 common.broadcast.queries 既有 fetch_* 的结构化输出，不做二次
    # 推导；面板 9 复用渠道日报三函数 + 店铺粒度月目标真源。迁移与复用纪律
    # 详见 queries.py 末尾同名段头注释块。
    _card("table_qd_monthly_kpi", "table",
          queries.run_table_qd_monthly_kpi, {"date": "dates"}),
    _card("table_qd_expense", "table",
          queries.run_table_qd_expense, {"date": "dates"}),
    _card("table_qd_expense_rate", "table",
          queries.run_table_qd_expense_rate, {"date": "dates"}),
    _card("table_qd_bill_category", "table",
          queries.run_table_qd_bill_category, {"date": "dates"}),
    _card("table_qd_bill_platform", "table",
          queries.run_table_qd_bill_platform, {"date": "dates"}),
    _card("table_qd_bill_items", "table",
          queries.run_table_qd_bill_items, {"date": "dates"}),
    _card("table_qd_product_series", "table",
          queries.run_table_qd_product_series, {"date": "dates"}),
    _card("table_qd_product_bands", "table",
          queries.run_table_qd_product_bands, {"date": "dates"}),
    _card("table_qd_product_top", "table",
          queries.run_table_qd_product_top, {"date": "dates"}),
    _card("table_qd_product_summary", "table",
          queries.run_table_qd_product_summary, {"date": "dates"}),
    _card("table_qd_refund_reason", "table",
          queries.run_table_qd_refund_reason, {"date": "dates"}),
    _card("table_qd_gmv_factors", "table",
          queries.run_table_qd_gmv_factors, {"date": "dates"}),
    _card("table_qd_cohort", "table",
          queries.run_table_qd_cohort, {"date": "dates"}),
    _card("table_qd_channel_daily", "table",
          queries.run_table_qd_channel_daily, {"date": "dates"}),
    _card("table_qd_hot_items", "table",
          queries.run_table_qd_hot_items, {"date": "dates"}),
    _card("table_qd_stock_urgent", "table",
          queries.run_table_qd_stock_urgent, {"date": "dates"}),
    _card("table_qd_stock_oversold", "table",
          queries.run_table_qd_stock_oversold, {"date": "dates"}),
    _card("table_qd_purchase_inbound", "table",
          queries.run_table_qd_purchase_inbound, {"date": "dates"}),
    _card("table_qd_order_risk", "table",
          queries.run_table_qd_order_risk, {"date": "dates"}),
    _card("table_qd_notes", "table",
          queries.run_table_qd_notes, {"date": "dates"}),
    _card("pie_sku_mtd", "pie", queries.run_pie_sku_mtd),
    _card(
        "kpi_sku_mtd", "scalar", queries.run_kpi_sku_mtd,
        {"month": "months"},
    ),
    _card(
        "table_sku_hot_total", "table", queries.run_table_sku_hot_total,
        {"month": "months"},
    ),
    _card(
        "table_sku_hot_brand", "table", queries.run_table_sku_hot_brand,
        {"brand": "brands", "month": "months"},
    ),
    _card(
        "table_sku_hot_channel", "table", queries.run_table_sku_hot_channel,
        {"channel": "sku_channels", "month": "months"},
    ),
    _card(
        "kpi_shortfall", "table", queries.run_kpi_shortfall,
        {"region": "regions", "month": "months"},
    ),
    _card(
        "anomaly_top", "table", queries.run_anomaly_top,
        {"region": "regions", "month": "months"},
    ),
    _card(
        "table_manual_ecommerce_monthly", "table",
        queries.run_table_manual_ecommerce_monthly,
    ),
    _card(
        "table_manual_restaurant_monthly", "table",
        queries.run_table_manual_restaurant_monthly,
    ),
    _card(
        "table_manual_showroom_monthly", "table",
        queries.run_table_manual_showroom_monthly,
    ),
    # 资金安全页（需求⑩）五卡：SQL 只读 mart fact_fin_*，派生走
    # fin_derived；params_schema 保守留空（同 ④⑤ 裁决）。
    _card("kpi_fin_receivables_overdue", "scalar",
          queries.run_kpi_fin_receivables_overdue),
    _card("table_fin_receivables_aging", "table",
          queries.run_table_fin_receivables_aging),
    _card("table_fin_prepayment_uninvoiced", "table",
          queries.run_table_fin_prepayment_uninvoiced),
    _card("table_fin_deposit_status", "table",
          queries.run_table_fin_deposit_status),
    _card("trend_fin_store_funds", "line",
          queries.run_trend_fin_store_funds),
    # 店铺资金余额趋势的主体参数化版（2026-09-17 P2）：38 家店铺一张图
    # 不可读，而公司主体会变，故 4 张硬编码分屏卡收敛为 1 张 +
    # ``entities`` 筛选源（entity 空 = 全主体按渠道汇总）。
    # 粒度样板卡：trend_fin_store_funds_entity 仅月一档（单档不切换）。
    _card(
        "trend_fin_store_funds_entity", "line",
        queries.run_trend_fin_store_funds_entity,
        {"entity": "entities", "gran": "granularity"},
        grans=("month",), default_gran="month",
    ),
    # 5 张 0 占位结构卡：run 直接返回静态结构（不查库），
    # has_fact=false 挂零语义（应接入未接入）。
    _card("table_inventory_aging", "table",
          queries.run_table_inventory_aging),
    _card("table_warehouse_ops", "table",
          queries.run_table_warehouse_ops),
    _card("table_quarter_budget_actual", "table",
          queries.run_table_quarter_budget_actual),
    _card("table_yoy_monthly", "table",
          queries.run_table_yoy_monthly),
    _card("table_contract_writeoff", "table",
          queries.run_table_contract_writeoff),
)

#: 行级 scope 卡片（``row_scope="region"``）：数据源带 region 维度
#: （``fact_daily_report_offline`` 的离线线聚合 / 区域下钻 / 人员榜 /
#: 缺口，以及 hz/sx/oa 三块区域大屏）。设计稿 §4.1/§6：**带区域语义
#: 的查询必须消费 allowed_regions**，本集合是唯一声明点，缺失/多余由
#: 逐卡合约测试（tests/common/test_bi_web_queries.py）双向锁定。
#:
#: 显式豁免（保持 ``row_scope=None``）及其事实依据：
#: * 渠道 / 商品（``fact_channel_daily_sales`` / ``fact_order_line`` 无
#:   ``region`` 列）——``kpi_channel_*`` / ``trend_channel_daily`` /
#:   ``table_channel_mtd`` / ``table_store_mtd`` / ``bar_channel_mtd`` /
#:   SKU 三卡 / 渠道大屏二十一卡；
#: * 资金（``fact_fin_*`` 无 region），人工月报、0 占位卡；
#: * 年度目标（设计稿点名豁免：全业务线目标无区域语义）；
#: * 时间进度（工作日日历不分区域，与杭州卡同函数）。
_REGION_SCOPED_CARD_IDS = frozenset({
    # 离线线聚合 + 区域下钻 + 日环比
    "kpi_offline_mtd", "kpi_offline_dod", "trend_region_daily",
    "kpi_region_mtd", "bar_department_mtd",
    # 人员榜（mart_collect 按 region 取榜）
    "kpi_people_count", "kpi_people_completed", "kpi_people_rate",
    "table_people_leaderboard",
    # 缺口 / 告警（shortfall_facts 带 region 口径）
    "kpi_shortfall", "anomaly_top",
    # 杭州大屏（钉 hangzhou）
    "kpi_hz_mtd", "kpi_hz_today", "trend_hz_daily", "bar_hz_dept",
    "table_hz_people_progress", "table_hz_unfilled_today",
    "table_hz_dept_progress",
    # 绍兴大屏（钉 shaoxing）
    "kpi_sx_mtd", "kpi_sx_today", "trend_sx_daily", "bar_sx_dept",
    "table_sx_people_progress", "table_sx_unfilled_today",
    "table_sx_dept_progress",
    # 线下整体大屏（三区合并，整表放行/关闭）
    "kpi_oa_mtd", "kpi_oa_today", "trend_oa_daily", "bar_oa_dept",
    "table_oa_scopes", "table_oa_people_progress",
    "table_oa_dept_progress",
})

_UNKNOWN_SCOPED = _REGION_SCOPED_CARD_IDS - {c.card_id for c in _CARDS}
if _UNKNOWN_SCOPED:
    raise CardConfigError(
        f"row_scope set names unknown card(s): {sorted(_UNKNOWN_SCOPED)}"
    )

#: card_id -> Card, built and import-time validated (chart + filter source).
#: ``row_scope`` is stamped from the single declaration above so the
#: region-scoped set has exactly one reviewable home.
REGISTRY = {
    card.card_id: (
        replace(card, row_scope="region")
        if card.card_id in _REGION_SCOPED_CARD_IDS else card
    )
    for card in _CARDS
}


def validate_dashboard_config(dashboard, registry) -> None:
    """Every card the dashboard places must exist in *registry*.

    ``dashboard`` is a :class:`common.bi_web.config.DashboardConfig`;
    each ``CardPlacement.card`` holds the registry id.  Raises
    :class:`CardConfigError` naming the first unknown id -- card ids are
    configuration, never secrets.
    """
    for placement in dashboard.cards:
        if placement.card not in registry:
            raise CardConfigError(
                f"dashboard '{dashboard.dashboard_id}' references "
                f"unknown card '{placement.card}'"
            )
