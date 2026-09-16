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
parameter-light ``anomaly_top``) for twenty-three.

The whitelist maps param name -> filter source from
``config.KNOWN_FILTER_SOURCES``: the app layer resolves the source to a
dimension lookup for the value gate (invalid value -> 400).  The registry
is validated at import time -- an unknown chart kind or an unknown
filter source fails the import, so configuration drift can never reach a
running process silently.  :func:`validate_dashboard_config` closes the
loop with Task 2's :class:`~common.bi_web.config.DashboardConfig` -- a
dashboard may only place card ids that exist in the registry.
"""

from dataclasses import dataclass
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
    """

    card_id: str
    chart: str
    run: Callable
    params_schema: dict


def _card(card_id, chart, run, params_schema=None):
    """Build one card with both halves import-time validated.

    An unknown chart kind or an unknown filter source raises here, at
    module load -- the same protection class for both.
    """
    if chart not in KNOWN_CHARTS:
        raise CardConfigError(f"card '{card_id}' has unknown chart '{chart}'")
    schema = dict(params_schema or {})
    if any(source not in KNOWN_FILTER_SOURCES for source in schema.values()):
        raise CardConfigError(f"card '{card_id}' has an unknown filter source")
    return Card(card_id=card_id, chart=chart, run=run, params_schema=schema)


_CARDS = (
    _card("kpi_offline_mtd", "scalar", queries.run_kpi_offline_mtd),
    _card("kpi_channel_mtd", "scalar", queries.run_kpi_channel_mtd),
    _card("kpi_annual_progress", "scalar", queries.run_kpi_annual_progress),
    _card(
        "trend_region_daily", "line", queries.run_trend_region_daily,
        {"region": "regions", "month": "months"},
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
        {"month": "months"},
    ),
)

#: card_id -> Card, built and import-time validated (chart + filter source).
REGISTRY = {card.card_id: card for card in _CARDS}


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
