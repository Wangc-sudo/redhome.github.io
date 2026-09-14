"""Tests for the bi-web FastAPI assembly (plan Task 4).

Everything runs on ``TestClient`` with fully injected fakes -- no Nacos, no
RDS, no network.  The matrix locks:

* routing (API-first, 2026-09-14 separation spec): ``/`` redirects to the
  default dashboard, ``/d/{id}`` serves the data-free static shell
  (``web/index.html``) behind the shared resolve chain, the v1 API carries
  the navigation (``/api/v1/dashboards``), the dashboard definition
  (``/api/v1/dashboards/{id}``), and the filter option sets
  (``/api/v1/options/{source}``), while ``/api/d/{id}/cards/{card_id}``
  and its versioned alias ``/api/v1/d/...`` return the ``run`` payload
  with ``Cache-Control: no-store``, and ``/healthz`` probes the mart
  connection with ``SELECT 1``;
* error mapping: missing or disabled dashboard -> 404, corrupt definition
  (``DashboardConfigError`` / ``CardConfigError``) -> 503 and page-local,
  unknown query parameter -> 400, card ``run`` failure -> 500 ``card_error``
  -- and no response ever echoes a host name, SQL fragment, or exception
  text;
* auth: Bearer token guards ``/d/`` and ``/api/`` only (``/`` and
  ``/healthz`` never answer 401); unset or empty token means fully open;
* gate: request-level, TTL-cached (one registry read per 30s window),
  fail-open on a raising check;
* the default ``db_connector`` connects with ``settings.mart_database``
  only -- the raw dingtalk/wdt databases are never referenced;
* ``seed_path`` startup validation exposes seed/registry drift at boot;
* stage B mechanics: navigation fail-open (enumeration errors never 5xx
  a page), filter options TTL-cached with page-local 503 on failure, and
  the param double gate (unknown key -> 400, value outside its dimension
  table -> 400, dimension failure -> fail-open);
* integration smoke (``INTEGRATION_TEST_RUNNER=1`` only, plan Task 6): the
  compose wiring end to end -- environment settings, live migrations, the
  shipped seed file, the real registry, and the default mart-only
  connector behind ``TestClient``.
"""

import io
import os
import tempfile
import unittest
import warnings
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# The host pins httpx (Task 2); starlette merely warns about it.  Register
# the filter before importing TestClient so unittest output stays clean.
warnings.filterwarnings(
    "ignore", message="Using `httpx` with `starlette.testclient` is deprecated"
)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.bi_web import queries as bi_web_queries
from common.bi_web.app import (
    _FILTER_SOURCE_QUERIES,
    _TTLGate,
    _TTLOptionSets,
    _nav_entries,
    create_app,
    main,
)
from common.bi_web.cards import Card, CardConfigError, REGISTRY
from common.bi_web.config import (
    DashboardConfigError,
    FileDashboardSource,
    KNOWN_FILTER_SOURCES,
    StaticDashboardSource,
)
from common.public_data.db import transaction
from common.public_data.settings import DatabaseSettings, Settings


#: The first-batch card ids (the five placed on l1-cockpit).
_L1_CARD_IDS = (
    "kpi_offline_mtd",
    "kpi_channel_mtd",
    "kpi_annual_progress",
    "trend_region_daily",
    "bar_channel_mtd",
)


def _l1_mapping(enabled=True, cards=_L1_CARD_IDS, refresh_seconds=300):
    """A valid l1-cockpit seed entry as a mapping."""
    return {
        "title": "首页驾驶舱",
        "enabled": enabled,
        "refresh_seconds": refresh_seconds,
        "cards": [{"card": card, "title": f"t-{card}", "span": 4} for card in cards],
    }


def _l2_mapping(filters, cards=("kpi_offline_mtd",), nav_order=10):
    """A valid L2 seed entry carrying page filters."""
    return {
        "title": "分析页",
        "enabled": True,
        "refresh_seconds": 300,
        "nav_order": nav_order,
        "filters": [dict(spec) for spec in filters],
        "cards": [{"card": card, "title": f"t-{card}", "span": 4} for card in cards],
    }


#: The region+month filter pair shared by all three L2 pages.
_REGION_MONTH_FILTERS = (
    {"param": "region", "source": "regions", "label": "区域"},
    {"param": "month", "source": "months", "label": "月份"},
)


def _fake_settings():
    """Settings-shaped namespace carrying all three databases."""
    shared = {"host": "mart-secret-host.example.test", "port": 3306, "user": "bi", "password": "pw"}
    return SimpleNamespace(
        mart_database=DatabaseSettings(name="mart_ops_test", **shared),
        dingtalk_database=DatabaseSettings(name="dingtalk_ops_test", **shared),
        wdt_database=DatabaseSettings(name="wdt_ops_test", **shared),
    )


def _mock_connection():
    """Mock connection whose cursor serves one row for ``SELECT 1``."""
    connection = MagicMock(name="connection")
    connection.cursor.return_value.fetchone.return_value = {"1": 1}
    return connection


def _fake_db_connector(connection=None, error=None):
    """``db_connector`` fake yielding one connection (or raising *error*)."""
    if connection is None:
        connection = _mock_connection()

    @contextmanager
    def connector():
        if error is not None:
            raise error
        yield connection

    return connector


class _FakeRegistry(dict):
    """card_id -> Card; ``run`` records its calls and returns a fixed payload."""

    def __init__(self, error=None):
        super().__init__()
        self.run_calls = []
        self._error = error
        for card_id in _L1_CARD_IDS:
            self[card_id] = Card(
                card_id=card_id,
                chart="scalar",
                run=self._make_run(card_id),
                params_schema={},
            )

    def _make_run(self, card_id):
        def run(connection, params):
            self.run_calls.append((card_id, connection, params))
            if self._error is not None:
                raise self._error
            return {"chart": "scalar", "card": card_id}

        return run


class _CorruptDashboardSource:
    """Static source that raises ``DashboardConfigError`` for chosen ids.

    This mirrors the real Nacos failure mode: a stored entry that fails
    ``parse_dashboard_config`` raises from ``get_dashboard``.
    """

    def __init__(self, mapping, corrupt_ids):
        self._delegate = StaticDashboardSource(mapping)
        self._corrupt_ids = frozenset(corrupt_ids)

    def get_dashboard(self, dashboard_id):
        if dashboard_id in self._corrupt_ids:
            raise DashboardConfigError(f"dashboard '{dashboard_id}' is corrupt")
        return self._delegate.get_dashboard(dashboard_id)

    def dashboard_ids(self):
        return self._delegate.dashboard_ids()


class _CountingPipelineSource:
    """pipeline_config source fake counting ``get_pipeline`` calls."""

    def __init__(self, enabled=True, error=None):
        self.enabled = enabled
        self.error = error
        self.calls = 0

    def get_pipeline(self, service_id):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(enabled=self.enabled)


class _DimensionCursor:
    """Cursor recording SQL into its connection, serving rows by keyword."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, params=None):
        self._connection.dimension_sql.append(sql)

    def fetchall(self):
        sql = self._connection.dimension_sql[-1]
        for keyword, rows in self._connection.rows_by_keyword.items():
            if keyword in sql:
                return list(rows)
        return []

    def close(self):
        pass


class _DimensionConnection:
    """Connection whose cursors serve filter-option rows by SQL keyword."""

    def __init__(self, rows_by_keyword):
        self.rows_by_keyword = rows_by_keyword
        self.dimension_sql = []

    def cursor(self):
        return _DimensionCursor(self)

    def close(self):
        pass


#: keyword -> canned rows for the three filter sources.
_REGION_ROWS = {"DISTINCT region": [{"region": "杭州"}, {"region": "绍兴"}]}
_MONTH_ROWS = {"UNION": [{"month": "2026-09"}, {"month": "2026-08"}]}


def _dimension_connector(rows_by_keyword):
    """``db_connector`` fake yielding one shared dimension connection."""
    connection = _DimensionConnection(rows_by_keyword)

    @contextmanager
    def connector():
        yield connection

    connector.connection = connection
    return connector


def _counting_connector(connection):
    """``db_connector`` fake counting connections opened."""
    calls = []

    @contextmanager
    def connector():
        calls.append(1)
        yield connection

    connector.calls = calls
    return connector


def _failing_connector(error):
    """``db_connector`` fake raising *error* on entry, counting attempts."""
    calls = []

    @contextmanager
    def connector():
        calls.append(1)
        raise error
        yield  # pragma: no cover - unreachable

    connector.calls = calls
    return connector


def _build_app(*, dashboard_source=None, registry=None, token=None, gate=None,
               db_connector=None, settings=None):
    """create_app with fake defaults; an explicit None gate/token stays None."""
    if db_connector is None:
        db_connector = _fake_db_connector()
    return create_app(
        settings=settings if settings is not None else _fake_settings(),
        dashboard_source=(
            StaticDashboardSource({"l1-cockpit": _l1_mapping()})
            if dashboard_source is None
            else dashboard_source
        ),
        registry=registry if registry is not None else _FakeRegistry(),
        token=token,
        gate=gate,
        db_connector=db_connector,
    )


class _SeedFileTestCase(unittest.TestCase):
    """Shared helper: write a temp seed file that is unlinked on cleanup."""

    def _write_seed(self, text):
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        )
        tmp.write(text)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name


class HealthzTests(unittest.TestCase):
    def test_ok_reports_status_and_database(self):
        client = TestClient(_build_app())

        response = client.get("/healthz")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok", "database": "ok"}, response.json())

    def test_connection_error_answers_unhealthy_without_details(self):
        secret = RuntimeError(
            "connect to mart-secret-host.example.test failed with pw123"
        )
        client = TestClient(_build_app(db_connector=_fake_db_connector(error=secret)))

        response = client.get("/healthz")

        self.assertEqual(503, response.status_code)
        self.assertEqual({"status": "unhealthy"}, response.json())
        self.assertNotIn("mart-secret-host", response.text)
        self.assertNotIn("pw123", response.text)
        self.assertNotIn("connect", response.text)

    def test_healthz_never_requires_auth(self):
        client = TestClient(_build_app(token="t"))

        self.assertEqual(200, client.get("/healthz").status_code)

    def test_healthz_ignores_the_gate(self):
        client = TestClient(_build_app(gate=lambda: False))

        self.assertEqual(200, client.get("/healthz").status_code)


class RootRedirectTests(unittest.TestCase):
    def test_root_redirects_to_the_default_dashboard(self):
        client = TestClient(_build_app())

        response = client.get("/", follow_redirects=False)

        self.assertEqual(307, response.status_code)
        self.assertEqual("/d/l1-cockpit", response.headers["location"])

    def test_root_requires_no_auth(self):
        client = TestClient(_build_app(token="t"))

        response = client.get("/", follow_redirects=False)

        self.assertEqual(307, response.status_code)


class StaticFilesTests(unittest.TestCase):
    """The /web mount is created with ``check_dir=False``, so a forgotten
    or excluded web directory would boot silently and ship an unstyled,
    script-less cockpit.  These requests are the only guard -- the
    API-first shell's ``index.html`` / ``style.css`` / ``dashboard.js``
    must actually exist on the served tree.
    """

    def test_serves_style_css(self):
        client = TestClient(_build_app())

        response = client.get("/web/style.css")

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.content)

    def test_serves_dashboard_js(self):
        client = TestClient(_build_app())

        response = client.get("/web/dashboard.js")

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.content)

    def test_serves_index_html(self):
        client = TestClient(_build_app())

        response = client.get("/web/index.html")

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.content)


class DashboardPageTests(unittest.TestCase):
    """``/d/{id}`` serves the data-free static shell (2026-09-14 API-first).

    The page route only guards the URL through the shared resolve chain
    (404 missing/disabled, 503 corrupt) and returns ``web/index.html``;
    every data shape moved to the v1 API tests below.
    """

    def test_serves_the_static_shell_without_any_data(self):
        client = TestClient(_build_app())

        response = client.get("/d/l1-cockpit")

        self.assertEqual(200, response.status_code)
        body = response.text
        self.assertIn('href="/web/style.css"', body)
        self.assertIn('src="/web/dashboard.js"', body)
        # The shell is data-free: cards, params, filters, and options all
        # arrive via /api/v1/ from dashboard.js.
        self.assertNotIn("data-api=", body)
        self.assertNotIn("<select", body)

    def test_page_wires_local_assets_and_a_deferred_echarts_cdn_script(self):
        # index.html wiring is otherwise unpinned: a typo in an href/src or
        # a dropped ``defer`` would pass the whole suite and surface only in
        # manual smoke (unstyled/script-less cockpit, render-blocking CDN
        # fetch).  ``defer`` is safe: dashboard.js touches the echarts
        # global only from fetch callbacks, long after DOMContentLoaded.
        client = TestClient(_build_app())

        response = client.get("/d/l1-cockpit")

        self.assertEqual(200, response.status_code)
        body = response.text
        self.assertIn('href="/web/style.css"', body)
        self.assertIn('src="/web/dashboard.js"', body)
        self.assertIn(
            "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js", body
        )
        self.assertIn("defer", body)

    def test_missing_dashboard_is_not_found(self):
        client = TestClient(_build_app())

        self.assertEqual(404, client.get("/d/l2-region").status_code)

    def test_disabled_dashboard_is_not_found(self):
        source = StaticDashboardSource({"l1-cockpit": _l1_mapping(enabled=False)})
        client = TestClient(_build_app(dashboard_source=source))

        self.assertEqual(404, client.get("/d/l1-cockpit").status_code)

    def test_corrupt_definition_is_unavailable_and_page_local(self):
        source = _CorruptDashboardSource(
            {"l1-cockpit": _l1_mapping(), "l2-region": {"title": "区域"}},
            corrupt_ids=("l2-region",),
        )
        client = TestClient(_build_app(dashboard_source=source))

        broken = client.get("/d/l2-region")
        self.assertEqual(503, broken.status_code)
        self.assertEqual("unavailable", broken.json()["detail"])
        self.assertEqual(200, client.get("/d/l1-cockpit").status_code)

    def test_unknown_card_definition_is_unavailable(self):
        source = StaticDashboardSource(
            {"l1-cockpit": _l1_mapping(cards=("kpi_offline_mtd", "not_a_card"))}
        )
        client = TestClient(_build_app(dashboard_source=source))

        response = client.get("/d/l1-cockpit")

        self.assertEqual(503, response.status_code)
        self.assertEqual("unavailable", response.json()["detail"])

    def test_default_registry_is_the_real_registry(self):
        app = create_app(
            settings=_fake_settings(),
            dashboard_source=StaticDashboardSource({"l1-cockpit": _l1_mapping()}),
            db_connector=_fake_db_connector(),
        )
        client = TestClient(app)

        self.assertEqual(200, client.get("/d/l1-cockpit").status_code)
        self.assertTrue(set(_L1_CARD_IDS) <= set(REGISTRY))


class V1ApiTests(unittest.TestCase):
    """The API-first surface (2026-09-14 separation spec).

    ``/api/v1/dashboards`` carries the navigation,
    ``/api/v1/dashboards/{id}`` the definition the shell renders from,
    ``/api/v1/options/{source}`` one filter's option set, and
    ``/api/v1/d/...`` is the versioned alias of the legacy card route
    (same handler, same semantics).
    """

    def test_dashboards_lists_enabled_pages_by_nav_order(self):
        source = StaticDashboardSource({
            "l2-people": _l2_mapping(_REGION_MONTH_FILTERS, nav_order=30),
            "l1-cockpit": _l1_mapping(),
            "l2-region": _l2_mapping(_REGION_MONTH_FILTERS, nav_order=10),
        })
        client = TestClient(_build_app(dashboard_source=source))

        response = client.get("/api/v1/dashboards")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {
                "dashboards": [
                    {"id": "l1-cockpit", "title": "首页驾驶舱"},
                    {"id": "l2-region", "title": "分析页"},
                    {"id": "l2-people", "title": "分析页"},
                ]
            },
            response.json(),
        )

    def test_dashboards_degrades_to_empty_when_enumeration_fails(self):
        class _UnenumerableSource(StaticDashboardSource):
            def dashboard_ids(self):
                raise RuntimeError("nacos list failed")

        client = TestClient(
            _build_app(
                dashboard_source=_UnenumerableSource({"l1-cockpit": _l1_mapping()})
            )
        )

        response = client.get("/api/v1/dashboards")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"dashboards": []}, response.json())

    def test_dashboards_is_unavailable_when_the_gate_is_off(self):
        client = TestClient(_build_app(gate=lambda: False))

        response = client.get("/api/v1/dashboards")

        self.assertEqual(503, response.status_code)
        self.assertEqual("unavailable", response.json()["detail"])

    def test_definition_carries_title_refresh_filters_and_card_placements(self):
        registry = _FakeRegistry()
        registry["kpi_offline_mtd"] = replace(
            registry["kpi_offline_mtd"],
            params_schema={"region": "regions", "month": "months"},
        )
        source = StaticDashboardSource(
            {"l2-region": _l2_mapping(_REGION_MONTH_FILTERS)}
        )
        client = TestClient(_build_app(dashboard_source=source, registry=registry))

        response = client.get("/api/v1/dashboards/l2-region")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {
                "id": "l2-region",
                "title": "分析页",
                "refresh_seconds": 300,
                "filters": [dict(spec) for spec in _REGION_MONTH_FILTERS],
                "cards": [
                    {
                        "card": "kpi_offline_mtd",
                        "title": "t-kpi_offline_mtd",
                        "span": 4,
                        "on_click": None,
                        # The registry's param whitelist, so the client
                        # intersects page URL params per card exactly like
                        # the retired server-rendered shell did.
                        "params": ["region", "month"],
                    }
                ],
            },
            response.json(),
        )

    def test_definition_carries_on_click_for_drilldown_cards(self):
        source = StaticDashboardSource(
            {
                "l2-channel": {
                    "title": "渠道下钻",
                    "enabled": True,
                    "refresh_seconds": 300,
                    "nav_order": 20,
                    "filters": [dict(spec) for spec in _REGION_MONTH_FILTERS],
                    "cards": [
                        {
                            "card": "bar_channel_mtd",
                            "title": "t-bar",
                            "span": 6,
                            "on_click": {"param": "region"},
                        }
                    ],
                }
            }
        )
        client = TestClient(_build_app(dashboard_source=source))

        payload = client.get("/api/v1/dashboards/l2-channel").json()

        self.assertEqual("region", payload["cards"][0]["on_click"])
        self.assertEqual([], payload["cards"][0]["params"])

    def test_definition_404_for_missing_or_disabled_and_503_for_corrupt(self):
        source = _CorruptDashboardSource(
            {
                "l1-cockpit": _l1_mapping(),
                "l2-off": _l1_mapping(enabled=False),
                "l2-broken": {"title": "broken"},
            },
            corrupt_ids=("l2-broken",),
        )
        client = TestClient(_build_app(dashboard_source=source))

        self.assertEqual(404, client.get("/api/v1/dashboards/nope").status_code)
        self.assertEqual(404, client.get("/api/v1/dashboards/l2-off").status_code)
        broken = client.get("/api/v1/dashboards/l2-broken")
        self.assertEqual(503, broken.status_code)
        self.assertEqual("unavailable", broken.json()["detail"])

    def test_options_returns_the_option_list(self):
        client = TestClient(
            _build_app(db_connector=_dimension_connector(_REGION_ROWS))
        )

        response = client.get("/api/v1/options/regions")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {"source": "regions", "options": ["杭州", "绍兴"]}, response.json()
        )

    def test_options_unknown_source_is_not_found(self):
        client = TestClient(_build_app())

        response = client.get("/api/v1/options/no_such_source")

        self.assertEqual(404, response.status_code)
        self.assertEqual("not_found", response.json()["detail"])

    def test_options_is_unavailable_when_the_gate_is_off(self):
        client = TestClient(_build_app(gate=lambda: False))

        self.assertEqual(503, client.get("/api/v1/options/regions").status_code)

    def test_v1_card_alias_matches_the_legacy_route(self):
        client = TestClient(_build_app())

        legacy = client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd")
        alias = client.get("/api/v1/d/l1-cockpit/cards/kpi_offline_mtd")

        self.assertEqual(200, alias.status_code)
        self.assertEqual(legacy.json(), alias.json())
        self.assertEqual("no-store", alias.headers["cache-control"])

    def test_v1_card_alias_keeps_the_404_and_400_semantics(self):
        client = TestClient(_build_app())

        self.assertEqual(
            404, client.get("/api/v1/d/l1-cockpit/cards/not_placed").status_code
        )
        rejected = client.get(
            "/api/v1/d/l1-cockpit/cards/kpi_offline_mtd", params={"wat": "1"}
        )
        self.assertEqual(400, rejected.status_code)
        self.assertEqual("bad_request", rejected.json()["detail"])


class CardApiTests(unittest.TestCase):
    def test_returns_the_run_payload_with_no_store(self):
        connection = _mock_connection()
        registry = _FakeRegistry()
        client = TestClient(
            _build_app(registry=registry, db_connector=_fake_db_connector(connection))
        )

        response = client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"chart": "scalar", "card": "kpi_offline_mtd"}, response.json())
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual([("kpi_offline_mtd", connection, {})], registry.run_calls)

    def test_card_not_placed_on_the_dashboard_is_not_found(self):
        source = StaticDashboardSource(
            {"l1-cockpit": _l1_mapping(cards=("kpi_offline_mtd",))}
        )
        client = TestClient(_build_app(dashboard_source=source))

        response = client.get("/api/d/l1-cockpit/cards/kpi_channel_mtd")

        self.assertEqual(404, response.status_code)

    def test_unknown_query_parameter_is_rejected(self):
        client = TestClient(_build_app())

        response = client.get(
            "/api/d/l1-cockpit/cards/kpi_offline_mtd", params={"foo": "1"}
        )

        self.assertEqual(400, response.status_code)

    def test_whitelisted_query_parameter_reaches_run(self):
        registry = _FakeRegistry()
        registry["kpi_offline_mtd"] = replace(
            registry["kpi_offline_mtd"], params_schema={"region": "regions"}
        )
        client = TestClient(
            _build_app(
                registry=registry, db_connector=_dimension_connector(_REGION_ROWS)
            )
        )

        response = client.get(
            "/api/d/l1-cockpit/cards/kpi_offline_mtd", params={"region": "杭州"}
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(registry.run_calls))
        self.assertEqual("kpi_offline_mtd", registry.run_calls[0][0])
        self.assertEqual({"region": "杭州"}, registry.run_calls[0][2])

    def test_run_failure_answers_card_error_without_details(self):
        registry = _FakeRegistry(
            error=RuntimeError("SELECT ... WHERE token = 'secret-sql-value'")
        )
        client = TestClient(_build_app(registry=registry))

        response = client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd")

        self.assertEqual(500, response.status_code)
        self.assertEqual("card_error", response.json()["detail"])
        self.assertNotIn("secret-sql-value", response.text)

    def test_non_serializable_payload_answers_card_error(self):
        # A payload Starlette cannot JSON-encode (Decimal) must hit the same
        # designed 500 card_error mapping, not an unhandled TypeError that
        # escapes as a bare plain-text 500.
        registry = _FakeRegistry()
        registry["kpi_offline_mtd"] = replace(
            registry["kpi_offline_mtd"],
            run=lambda connection, params: {
                "chart": "scalar",
                "value": Decimal("1.5"),
            },
        )
        client = TestClient(
            _build_app(registry=registry), raise_server_exceptions=False
        )

        response = client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd")

        self.assertEqual(500, response.status_code)
        self.assertEqual("card_error", response.json()["detail"])

    def test_missing_dashboard_is_not_found(self):
        client = TestClient(_build_app())

        response = client.get("/api/d/l2-region/cards/kpi_offline_mtd")

        self.assertEqual(404, response.status_code)

    def test_disabled_dashboard_is_not_found(self):
        source = StaticDashboardSource({"l1-cockpit": _l1_mapping(enabled=False)})
        client = TestClient(_build_app(dashboard_source=source))

        response = client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd")

        self.assertEqual(404, response.status_code)

    def test_corrupt_definition_is_unavailable(self):
        source = _CorruptDashboardSource(
            {"l1-cockpit": _l1_mapping()}, corrupt_ids=("l1-cockpit",)
        )
        client = TestClient(_build_app(dashboard_source=source))

        response = client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd")

        self.assertEqual(503, response.status_code)


class SafeErrorLoggingTests(unittest.TestCase):
    """Every 5xx exception path emits exactly one WARNING carrying the
    exception class name -- an operator can tell "SQL failed" from "config
    corrupt" without the log ever approaching the leak boundary (SQL
    fragments, host names, credentials, payload values).
    """

    _LOGGER = "common.bi_web.app"

    def test_card_run_failure_logs_the_class_name_only(self):
        registry = _FakeRegistry(
            error=RuntimeError("SELECT ... WHERE token = 'secret-sql-value'")
        )
        client = TestClient(_build_app(registry=registry))

        with self.assertLogs(self._LOGGER, level="WARNING") as logs:
            response = client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd")

        self.assertEqual(500, response.status_code)
        self.assertEqual(1, len(logs.output))
        joined = "\n".join(logs.output)
        self.assertIn("RuntimeError", joined)
        self.assertNotIn("secret-sql-value", joined)
        self.assertNotIn("SELECT", joined)

    def test_healthz_connection_error_logs_the_class_name_only(self):
        secret = RuntimeError(
            "connect to mart-secret-host.example.test failed with pw123"
        )
        client = TestClient(_build_app(db_connector=_fake_db_connector(error=secret)))

        with self.assertLogs(self._LOGGER, level="WARNING") as logs:
            response = client.get("/healthz")

        self.assertEqual(503, response.status_code)
        self.assertEqual(1, len(logs.output))
        joined = "\n".join(logs.output)
        self.assertIn("RuntimeError", joined)
        self.assertNotIn("mart-secret-host", joined)
        self.assertNotIn("pw123", joined)
        self.assertNotIn("connect", joined)

    def test_corrupt_definition_logs_the_class_name_only(self):
        class _PoisonedSource:
            def get_dashboard(self, dashboard_id):
                raise DashboardConfigError(
                    "nacos entry broken at mart-secret-host.example.test (pw123)"
                )

        client = TestClient(_build_app(dashboard_source=_PoisonedSource()))

        with self.assertLogs(self._LOGGER, level="WARNING") as logs:
            response = client.get("/d/l1-cockpit")

        self.assertEqual(503, response.status_code)
        self.assertEqual(1, len(logs.output))
        joined = "\n".join(logs.output)
        self.assertIn("DashboardConfigError", joined)
        self.assertNotIn("mart-secret-host", joined)
        self.assertNotIn("pw123", joined)

    def test_unknown_card_placement_logs_the_class_name_only(self):
        source = StaticDashboardSource(
            {"l1-cockpit": _l1_mapping(cards=("kpi_offline_mtd", "not_a_card"))}
        )
        client = TestClient(_build_app(dashboard_source=source))

        with self.assertLogs(self._LOGGER, level="WARNING") as logs:
            response = client.get("/d/l1-cockpit")

        self.assertEqual(503, response.status_code)
        self.assertEqual(1, len(logs.output))
        joined = "\n".join(logs.output)
        self.assertIn("CardConfigError", joined)


class AuthTests(unittest.TestCase):
    #: The protected surface: pages plus legacy and v1 API routes alike.
    _PROTECTED_PATHS = (
        "/d/l1-cockpit",
        "/api/d/l1-cockpit/cards/kpi_offline_mtd",
        "/api/v1/dashboards",
        "/api/v1/dashboards/l1-cockpit",
        "/api/v1/options/regions",
        "/api/v1/d/l1-cockpit/cards/kpi_offline_mtd",
    )

    def test_missing_or_wrong_bearer_is_unauthorized_on_protected_routes(self):
        client = TestClient(_build_app(token="t"))

        for path in self._PROTECTED_PATHS:
            with self.subTest(path=path):
                self.assertEqual(401, client.get(path).status_code)
                wrong = client.get(path, headers={"Authorization": "Bearer wrong"})
                self.assertEqual(401, wrong.status_code)
                self.assertEqual("unauthorized", wrong.json()["detail"])

    def test_correct_bearer_is_accepted(self):
        client = TestClient(_build_app(token="t"))
        headers = {"Authorization": "Bearer t"}

        for path in self._PROTECTED_PATHS:
            with self.subTest(path=path):
                self.assertEqual(200, client.get(path, headers=headers).status_code)

    def test_missing_or_empty_token_leaves_everything_open(self):
        for token in (None, ""):
            with self.subTest(token=token):
                client = TestClient(_build_app(token=token))
                for path in self._PROTECTED_PATHS:
                    with self.subTest(path=path):
                        self.assertEqual(200, client.get(path).status_code)


class GateTests(unittest.TestCase):
    def test_disabled_gate_answers_unavailable_on_protected_routes(self):
        client = TestClient(_build_app(gate=lambda: False))

        page = client.get("/d/l1-cockpit")
        self.assertEqual(503, page.status_code)
        self.assertEqual("unavailable", page.json()["detail"])
        self.assertEqual(
            503, client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd").status_code
        )
        # The v1 surface is gated identically.
        self.assertEqual(503, client.get("/api/v1/dashboards").status_code)
        self.assertEqual(
            503, client.get("/api/v1/dashboards/l1-cockpit").status_code
        )
        self.assertEqual(503, client.get("/api/v1/options/regions").status_code)
        self.assertEqual(
            503,
            client.get("/api/v1/d/l1-cockpit/cards/kpi_offline_mtd").status_code,
        )
        self.assertEqual(200, client.get("/healthz").status_code)

    def _patched_pipeline(self, source):
        """Patch the pipeline registry behind the default gate (counting)."""
        resolve_calls = []

        def fake_resolve(*args, **kwargs):
            resolve_calls.append(1)
            return "bi-web"

        return (
            patch(
                "common.public_data.pipeline_config.build_config_source",
                return_value=source,
            ),
            patch(
                "common.public_data.pipeline_config.resolve_service_id",
                side_effect=fake_resolve,
            ),
            resolve_calls,
        )

    def test_default_gate_queries_the_pipeline_source_once_within_the_ttl(self):
        source = _CountingPipelineSource(enabled=True)
        build_patch, resolve_patch, resolve_calls = self._patched_pipeline(source)
        with build_patch, resolve_patch:
            client = TestClient(_build_app(gate=None))
            client.get("/d/l1-cockpit")
            client.get("/d/l1-cockpit")
            client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd")

        self.assertEqual(1, source.calls)
        self.assertEqual(1, len(resolve_calls))

    def test_default_gate_fails_open_when_the_source_raises(self):
        source = _CountingPipelineSource(
            error=RuntimeError("nacos unreachable at http://secret-nacos:8848")
        )
        build_patch, resolve_patch, _ = self._patched_pipeline(source)
        with build_patch, resolve_patch:
            client = TestClient(_build_app(gate=None))

        self.assertEqual(200, client.get("/d/l1-cockpit").status_code)

    def test_default_gate_serves_unavailable_when_the_pipeline_is_disabled(self):
        source = _CountingPipelineSource(enabled=False)
        build_patch, resolve_patch, _ = self._patched_pipeline(source)
        with build_patch, resolve_patch:
            client = TestClient(_build_app(gate=None))

        self.assertEqual(503, client.get("/d/l1-cockpit").status_code)
        self.assertEqual(200, client.get("/healthz").status_code)


class TTLGateUnitTests(unittest.TestCase):
    def test_caches_the_check_within_the_ttl(self):
        calls = []

        def check():
            calls.append(1)
            return True

        gate = _TTLGate(check=check)

        self.assertTrue(gate())
        self.assertTrue(gate())
        self.assertEqual(1, len(calls))

    def test_requeries_after_the_ttl_expires(self):
        calls = []

        def check():
            calls.append(1)
            return True

        gate = _TTLGate(check=check)
        with patch("common.bi_web.app.time") as fake_time:
            fake_time.monotonic.side_effect = [0.0, 0.0, 31.0]
            gate()
            gate()
            gate()

        self.assertEqual(2, len(calls))

    def test_fails_open_when_the_check_raises(self):
        def check():
            raise RuntimeError("registry exploded")

        gate = _TTLGate(check=check)

        self.assertTrue(gate())

    def test_reports_disabled_when_the_check_returns_false(self):
        gate = _TTLGate(check=lambda: False)

        self.assertFalse(gate())


class FilterSourceParityTests(unittest.TestCase):
    """``_FILTER_SOURCE_QUERIES`` must track ``KNOWN_FILTER_SOURCES``.

    config.py validates filter sources against its vocabulary at parse
    time and cards.py validates params_schema against the same vocabulary
    at import time -- this pins the app-side query map to it, so a new
    source turns the build red here instead of KeyErrors at runtime.
    """

    def test_filter_source_queries_match_known_filter_sources(self):
        self.assertEqual(set(KNOWN_FILTER_SOURCES), set(_FILTER_SOURCE_QUERIES))


class NavTests(unittest.TestCase):
    """Top navigation: enabled dashboards by nav_order, fail-open."""

    def test_lists_enabled_dashboards_by_nav_order(self):
        source = StaticDashboardSource({
            "l2-people": _l2_mapping(_REGION_MONTH_FILTERS, nav_order=30),
            "l1-cockpit": _l1_mapping(),
            "l2-region": _l2_mapping(_REGION_MONTH_FILTERS, nav_order=10),
        })

        nav = _nav_entries(source)

        self.assertEqual(
            (
                ("l1-cockpit", "首页驾驶舱"),
                ("l2-region", "分析页"),
                ("l2-people", "分析页"),
            ),
            nav,
        )

    def test_disabled_dashboards_are_excluded(self):
        source = StaticDashboardSource({"l1-cockpit": _l1_mapping(enabled=False)})

        self.assertEqual((), _nav_entries(source))

    def test_nav_order_ties_break_by_id_and_cardless_pages_are_excluded(self):
        source = StaticDashboardSource({
            "l2-region": _l2_mapping(_REGION_MONTH_FILTERS, nav_order=10),
            "cardless": {"title": "空页", "enabled": True},
            "l2-bbb": _l2_mapping(_REGION_MONTH_FILTERS, nav_order=10),
            "l2-aaa": _l2_mapping(_REGION_MONTH_FILTERS, cards=(), nav_order=10),
        })

        nav = _nav_entries(source)

        self.assertEqual(
            (("l2-bbb", "分析页"), ("l2-region", "分析页")),
            nav,
        )

    def test_corrupt_entries_are_skipped_and_logged_safely(self):
        source = _CorruptDashboardSource(
            {
                "l1-cockpit": _l1_mapping(),
                "l2-region": _l2_mapping(_REGION_MONTH_FILTERS),
            },
            corrupt_ids=("l2-region",),
        )

        with self.assertLogs("common.bi_web.app", level="WARNING") as logs:
            nav = _nav_entries(source)

        self.assertEqual((("l1-cockpit", "首页驾驶舱"),), nav)
        joined = "\n".join(logs.output)
        self.assertIn("DashboardConfigError", joined)
        self.assertNotIn("l2-region", joined)

    def test_enumeration_failure_answers_no_nav(self):
        class _UnenumerableSource(StaticDashboardSource):
            def dashboard_ids(self):
                raise RuntimeError("nacos list failed")

        self.assertEqual((), _nav_entries(_UnenumerableSource({"l1-cockpit": _l1_mapping()})))

    def test_page_renders_when_enumeration_fails(self):
        class _UnenumerableSource(StaticDashboardSource):
            def dashboard_ids(self):
                raise RuntimeError("nacos list failed")

        client = TestClient(
            _build_app(dashboard_source=_UnenumerableSource({"l1-cockpit": _l1_mapping()}))
        )

        self.assertEqual(200, client.get("/d/l1-cockpit").status_code)


class FilterRenderingTests(unittest.TestCase):
    """Filter options endpoint: ``/api/v1/options/{source}``, TTL-cached.

    The API-first shell never touches the mart for page loads; option sets
    are fetched by ``dashboard.js`` from this endpoint, still through the
    TTL cache that page rendering used before the separation.
    """

    def test_options_load_each_source_once_within_the_ttl(self):
        connector = _dimension_connector({**_REGION_ROWS, **_MONTH_ROWS})
        client = TestClient(_build_app(db_connector=connector))

        first = client.get("/api/v1/options/regions")
        second = client.get("/api/v1/options/regions")
        third = client.get("/api/v1/options/months")

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(200, third.status_code)
        self.assertEqual(2, len(connector.connection.dimension_sql))

    def test_shell_opens_no_connection(self):
        connector = _counting_connector(_DimensionConnection({}))
        client = TestClient(_build_app(db_connector=connector))

        response = client.get("/d/l1-cockpit")

        self.assertEqual(200, response.status_code)
        self.assertEqual([], connector.calls)

    def test_dimension_fetch_failure_is_unavailable_without_details(self):
        secret = RuntimeError("connect to mart-secret-host.example.test with pw123")
        client = TestClient(_build_app(db_connector=_fake_db_connector(error=secret)))

        broken = client.get("/api/v1/options/regions")
        healthy = client.get("/d/l1-cockpit")

        self.assertEqual(503, broken.status_code)
        self.assertEqual("unavailable", broken.json()["detail"])
        self.assertEqual(200, healthy.status_code)
        self.assertNotIn("mart-secret-host", broken.text)
        self.assertNotIn("pw123", broken.text)

    def test_dimension_fetch_failure_logs_the_class_name_only(self):
        secret = RuntimeError("connect to mart-secret-host.example.test with pw123")
        client = TestClient(_build_app(db_connector=_fake_db_connector(error=secret)))

        with self.assertLogs("common.bi_web.app", level="WARNING") as logs:
            response = client.get("/api/v1/options/regions")

        self.assertEqual(503, response.status_code)
        self.assertEqual(1, len(logs.output))
        joined = "\n".join(logs.output)
        self.assertIn("RuntimeError", joined)
        self.assertNotIn("mart-secret-host", joined)
        self.assertNotIn("pw123", joined)


class ValueGateTests(unittest.TestCase):
    """The second gate: param values must live in their dimension tables."""

    def _client(self, registry, db_connector):
        registry["kpi_offline_mtd"] = replace(
            registry["kpi_offline_mtd"], params_schema={"region": "regions"}
        )
        return TestClient(_build_app(registry=registry, db_connector=db_connector))

    def test_invalid_dimension_value_is_rejected(self):
        registry = _FakeRegistry()
        client = self._client(registry, _dimension_connector(_REGION_ROWS))

        response = client.get(
            "/api/d/l1-cockpit/cards/kpi_offline_mtd", params={"region": "不存在的区域"}
        )

        self.assertEqual(400, response.status_code)
        self.assertEqual("bad_request", response.json()["detail"])
        self.assertNotIn("不存在的区域", response.text)

    def test_value_gate_fails_open_when_the_dimension_query_raises(self):
        # Deviation from the plan (reported): the plan's fake raised at
        # connector entry, which also kills the card run (500 card_error)
        # and makes this test's assertions unreachable.  The failure is
        # injected at the dimension query itself, matching the test's
        # name and the fail-open contract: a failed lookup never blocks
        # the request, the param still reaches ``run``.
        registry = _FakeRegistry()
        connection = _mock_connection()
        connection.cursor.return_value.execute.side_effect = RuntimeError(
            "dimension query failed"
        )
        client = self._client(registry, _fake_db_connector(connection))

        response = client.get(
            "/api/d/l1-cockpit/cards/kpi_offline_mtd", params={"region": "任意值"}
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual({"region": "任意值"}, registry.run_calls[0][2])

    def test_value_gate_uses_the_ttl_cache(self):
        registry = _FakeRegistry()
        connector = _dimension_connector(_REGION_ROWS)
        client = self._client(registry, connector)

        client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd", params={"region": "杭州"})
        client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd", params={"region": "绍兴"})

        self.assertEqual(1, len(connector.connection.dimension_sql))


class OptionSetUnitTests(unittest.TestCase):
    """``_TTLOptionSets``: TTL caching, failure retry, fail-open contains."""

    def test_options_are_cached_within_the_ttl(self):
        connector = _counting_connector(_DimensionConnection(_REGION_ROWS))
        option_sets = _TTLOptionSets(connector, ttl_seconds=3600.0)

        first = option_sets.options("regions")
        second = option_sets.options("regions")

        self.assertEqual(("杭州", "绍兴"), first)
        self.assertEqual(first, second)
        self.assertEqual(1, len(connector.calls))

    def test_failed_fetch_is_not_cached_and_retries(self):
        connector = _failing_connector(RuntimeError("mart down"))
        option_sets = _TTLOptionSets(connector, ttl_seconds=3600.0)

        self.assertIsNone(option_sets.options("regions"))
        self.assertIsNone(option_sets.options("regions"))
        self.assertEqual(2, len(connector.calls))

    def test_contains_fails_open_on_error(self):
        option_sets = _TTLOptionSets(_failing_connector(RuntimeError("mart down")))

        self.assertTrue(option_sets.contains("regions", "任意值"))

    def test_contains_rejects_unknown_values(self):
        option_sets = _TTLOptionSets(_dimension_connector(_REGION_ROWS))

        self.assertTrue(option_sets.contains("regions", "杭州"))
        self.assertFalse(option_sets.contains("regions", "别处"))

    def test_unknown_source_answers_none_and_contains_open(self):
        option_sets = _TTLOptionSets(_dimension_connector(_REGION_ROWS))

        self.assertIsNone(option_sets.options("no_such_source"))
        self.assertTrue(option_sets.contains("no_such_source", "任意值"))


class DefaultDbConnectorTests(unittest.TestCase):
    @patch("common.bi_web.app.connect")
    def test_connects_only_to_the_mart_database(self, connect_mock):
        connection = _mock_connection()
        connect_mock.return_value = connection
        settings = _fake_settings()
        app = create_app(
            settings=settings,
            dashboard_source=StaticDashboardSource({"l1-cockpit": _l1_mapping()}),
            registry=_FakeRegistry(),
        )
        client = TestClient(app)

        self.assertEqual(200, client.get("/healthz").status_code)
        self.assertEqual(
            200, client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd").status_code
        )

        used = [call.args[0] for call in connect_mock.call_args_list]
        self.assertEqual([settings.mart_database, settings.mart_database], used)
        self.assertNotIn(settings.dingtalk_database, used)
        self.assertNotIn(settings.wdt_database, used)
        connection.close.assert_called()


class SeedValidationTests(_SeedFileTestCase):
    def test_seed_referencing_an_unknown_card_fails_startup(self):
        path = self._write_seed(
            "l1-cockpit:\n  cards:\n    - card: kpi_offline_mtd\n    - card: not_a_card\n"
        )

        with self.assertRaises(CardConfigError):
            create_app(
                settings=_fake_settings(),
                dashboard_source=StaticDashboardSource({"l1-cockpit": _l1_mapping()}),
                registry=_FakeRegistry(),
                db_connector=_fake_db_connector(),
                seed_path=path,
            )

    def test_valid_seed_starts_the_app(self):
        text = "l1-cockpit:\n  cards:\n" + "".join(
            f"    - card: {card_id}\n" for card_id in _L1_CARD_IDS
        )
        path = self._write_seed(text)
        app = create_app(
            settings=_fake_settings(),
            dashboard_source=StaticDashboardSource({"l1-cockpit": _l1_mapping()}),
            registry=_FakeRegistry(),
            db_connector=_fake_db_connector(),
            seed_path=path,
        )

        self.assertEqual(200, TestClient(app).get("/d/l1-cockpit").status_code)


class MainTests(_SeedFileTestCase):
    @patch("common.bi_web.app.serve")
    @patch("common.bi_web.app.build_dashboard_config_source")
    @patch("common.bi_web.app.load_settings")
    def test_main_serves_the_assembled_app_with_the_env_token(
        self, load_settings, build_source, serve
    ):
        load_settings.return_value = _fake_settings()
        build_source.return_value = StaticDashboardSource(
            {"l1-cockpit": _l1_mapping()}
        )
        env = {"BI_WEB_TOKEN": "t", "PUBLIC_DATA_BI_SEED": ""}

        with patch.dict(os.environ, env):
            main()

        serve.assert_called_once()
        served = serve.call_args.args[0]
        self.assertIsInstance(served, FastAPI)
        client = TestClient(served)
        self.assertEqual(401, client.get("/d/l1-cockpit").status_code)
        authorized = client.get("/d/l1-cockpit", headers={"Authorization": "Bearer t"})
        self.assertEqual(200, authorized.status_code)

    @patch("common.bi_web.app.serve")
    @patch("common.bi_web.app.build_dashboard_config_source")
    @patch("common.bi_web.app.load_settings")
    def test_main_exits_one_with_a_single_safe_line_when_validation_fails(
        self, load_settings, build_source, serve
    ):
        load_settings.return_value = _fake_settings()
        build_source.return_value = StaticDashboardSource({})
        path = self._write_seed("l1-cockpit:\n  cards:\n    - card: not_a_card\n")

        with patch.dict(os.environ, {"PUBLIC_DATA_BI_SEED": path}):
            with redirect_stdout(io.StringIO()) as captured:
                with self.assertRaises(SystemExit) as context:
                    main()

        self.assertEqual(1, context.exception.code)
        serve.assert_not_called()
        output = captured.getvalue()
        self.assertNotIn("Traceback", output)
        self.assertNotIn(path, output)
        # The class name tells the operator which side to fix -- here the
        # seed (CardConfigError), not the environment.
        self.assertIn("CardConfigError", output)
        lines = [line for line in output.splitlines() if line.strip()]
        self.assertEqual(1, len(lines))

    @patch("common.bi_web.app.serve")
    @patch("common.bi_web.app.build_dashboard_config_source")
    @patch("common.bi_web.app.load_settings")
    def test_main_failure_line_names_a_settings_error_class(
        self, load_settings, build_source, serve
    ):
        # Same one safe line, but the class name (ValueError) points the
        # operator at the environment rather than the seed.
        load_settings.side_effect = ValueError(
            "PUBLIC_DATA_MART_DATABASE__PASSWORD is required: pw123-secret"
        )
        build_source.return_value = StaticDashboardSource({})

        with patch.dict(
            os.environ, {"BI_WEB_TOKEN": "t", "PUBLIC_DATA_BI_SEED": ""}
        ):
            with redirect_stdout(io.StringIO()) as captured:
                with self.assertRaises(SystemExit) as context:
                    main()

        self.assertEqual(1, context.exception.code)
        serve.assert_not_called()
        output = captured.getvalue()
        self.assertIn("ValueError", output)
        self.assertNotIn("pw123-secret", output)
        self.assertNotIn("Traceback", output)
        lines = [line for line in output.splitlines() if line.strip()]
        self.assertEqual(1, len(lines))


#: The version-controlled dashboard seed shipped in the Docker image
#: (Task 5); compose points ``PUBLIC_DATA_BI_SEED`` at the same file.
_REPO_BI_SEED_PATH = (
    Path(__file__).resolve().parents[2] / "docker" / "integration" / "bi.seed.yaml"
)

#: The version-controlled target seed (Task 1); ``load-target`` replays
#: exactly this file, so replaying it in a test pins the two-line
#: denominator (760,210,000) without depending on suite order.
_REPO_TARGET_SEED_PATH = (
    Path(__file__).resolve().parents[2] / "docker" / "integration" / "target.seed.json"
)

# Controller-authorized addition C (口径对拍): independent recomputations
# of the two reconciled 口径, deliberately written here rather than imported
# from ``common.bi_web.queries`` -- a reconciliation only means something
# when the test does not reuse the code under test.  Same 合计 exclusion,
# same month/year truncation, same two-line target scope.
_RECON_OFFLINE_MTD_SQL = (
    "SELECT COALESCE(SUM(sales_amount), 0) "
    "FROM fact_daily_report_offline "
    "WHERE business_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01') "
    "AND business_date <= CURDATE() "
    "AND responsible_person NOT LIKE '%合计%'"
)
_RECON_OFFLINE_ANNUAL_SQL = (
    "SELECT COALESCE(SUM(sales_amount), 0) "
    "FROM fact_daily_report_offline "
    "WHERE business_date >= MAKEDATE(YEAR(CURDATE()), 1) "
    "AND business_date <= CURDATE() "
    "AND responsible_person NOT LIKE '%合计%'"
)
_RECON_CHANNEL_ANNUAL_SQL = (
    "SELECT COALESCE(SUM(sales_amount), 0) "
    "FROM fact_channel_daily_sales "
    "WHERE business_date >= MAKEDATE(YEAR(CURDATE()), 1) "
    "AND business_date <= CURDATE()"
)
_RECON_TWO_LINE_TARGET_SQL = (
    "SELECT COALESCE(SUM(annual_target), 0) "
    "FROM dim_target "
    "WHERE scope = 'line' "
    "AND scope_key IN ('offline', 'channel') "
    "AND year = YEAR(CURDATE())"
)

_STAGE_B_L1_CARD_IDS = (
    "kpi_offline_dod",
    "kpi_channel_dod",
    "kpi_offline_mtd",
    "kpi_channel_mtd",
    "kpi_annual_progress",
    "trend_region_daily",
    "bar_channel_mtd",
)

# L2 页面测试的卡片计数从本表推导（单一数据源）：两测试各自硬编码时，
# 加卡而只改一处会让另一处静默漏测。
_STAGE_B_L2_PLACEMENTS = {
    "l2-region": {
        "kpi_region_mtd": "scalar",
        "trend_region_daily": "line",
        "bar_department_mtd": "bar",
    },
    "l2-channel": {
        "bar_channel_mtd": "bar",
        "trend_channel_daily": "line",
        "table_channel_mtd": "table",
        "table_store_mtd": "table",
    },
    "l2-people": {
        "kpi_people_count": "scalar",
        "kpi_people_completed": "scalar",
        "kpi_people_rate": "scalar",
        "table_people_leaderboard": "table",
    },
}

_APP_FIXTURE_PREFIX = "biweb-app-test:"
# 仅作植入行溯源标记；清理按 source_record_id 前缀，不依赖此值。
_APP_SYNC_RUN_ID = "00000000-0000-0000-0000-000000000002"

_APP_OFFLINE_INSERT_SQL = (
    "INSERT INTO `fact_daily_report_offline` "
    "(`source_record_id`, `region`, `responsible_person`, `business_date`, "
    "`sales_amount`, `synced_at`, `sync_run_id`) "
    "VALUES (%s, %s, %s, %s, %s, NOW(6), %s)"
)

_APP_OFFLINE_CLEANUP_SQL = (
    "DELETE FROM `fact_daily_report_offline` "
    "WHERE `source_record_id` LIKE 'biweb-app-test:%'"
)


@unittest.skipUnless(
    os.environ.get("INTEGRATION_TEST_RUNNER") == "1",
    "Requires the Docker Compose MySQL integration environment.",
)
class BiWebAppIntegrationTests(unittest.TestCase):
    """Full-stack smoke (plan Task 6): exactly what compose starts.

    The assembly under test is the bi-web service wiring end to end --
    ``Settings.from_environment()``, live migrations applied to the real
    MySQL, the shipped dashboard seed, the real card registry, and the
    default mart-only ``db_connector`` -- behind ``TestClient``.  Numeric
    fields are asserted for structure and type only: the shared mart
    legitimately holds real (possibly zero) amounts.  The two 口径对拍
    tests (controller addition C) are the deliberate exception: they
    reconcile API values against independent SQL recomputations over the
    same rows, so they stay valid whatever the amounts are -- and only the
    annual-progress denominator replays the version-controlled target seed
    (the load-target post-condition) to pin 760,210,000 without depending
    on suite order.
    """

    @classmethod
    def setUpClass(cls):
        from common.public_data.db import connect
        from common.public_data.live_migrations import apply_live_migrations

        cls.settings = Settings.from_environment()
        cls.dingtalk_connection = connect(cls.settings.dingtalk_database)
        cls.wdt_connection = connect(cls.settings.wdt_database)
        cls.mart_connection = connect(cls.settings.mart_database)
        apply_live_migrations(
            cls.dingtalk_connection,
            cls.wdt_connection,
            cls.mart_connection,
        )
        cls.client = TestClient(
            create_app(
                settings=cls.settings,
                dashboard_source=FileDashboardSource(_REPO_BI_SEED_PATH),
                registry=REGISTRY,
            )
        )

    def setUp(self):
        bi_web_queries._PEOPLE_CACHE.clear()

    def tearDown(self):
        with transaction(self.mart_connection):
            with self.mart_connection.cursor() as cursor:
                cursor.execute(_APP_OFFLINE_CLEANUP_SQL)

    def _server_today(self):
        """CURDATE() as the server sees it (never trust the host clock/TZ)."""
        with self.mart_connection.cursor() as cursor:
            cursor.execute("SELECT CURDATE() AS today")
            return cursor.fetchone()["today"]

    def _fresh_client(self):
        """新 app 实例：_TTLOptionSets 按 app 实例持有，必读到插入后的维表。"""
        return TestClient(
            create_app(
                settings=self.settings,
                dashboard_source=FileDashboardSource(_REPO_BI_SEED_PATH),
                registry=REGISTRY,
            )
        )

    @classmethod
    def tearDownClass(cls):
        cls.mart_connection.close()
        cls.wdt_connection.close()
        cls.dingtalk_connection.close()

    def test_healthz_reports_database_ok(self):
        response = self.client.get("/healthz")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok", "database": "ok"}, response.json())

    def test_l1_cockpit_definition_places_the_seven_cards(self):
        response = self.client.get("/api/v1/dashboards/l1-cockpit")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual(7, len(payload["cards"]))
        self.assertEqual(
            set(_STAGE_B_L1_CARD_IDS),
            {card["card"] for card in payload["cards"]},
        )
        # The shell itself still serves the same guarded URL.
        self.assertEqual(200, self.client.get("/d/l1-cockpit").status_code)

    def test_static_style_css_ships_inside_the_image(self):
        # Controller-authorized addition A: the host StaticFilesTests run
        # from the repository tree, so a Docker-context exclusion of
        # web/ would pass them and surface only as an unstyled cockpit.
        # These requests run inside the image -- the real guard.
        for path in ("/web/style.css", "/web/dashboard.js", "/web/index.html"):
            with self.subTest(path=path):
                response = self.client.get(path)

                self.assertEqual(200, response.status_code)
                self.assertTrue(response.content)

    def test_every_card_answers_its_chart_payload_with_no_store(self):
        charts = {
            "kpi_offline_dod": "scalar",
            "kpi_channel_dod": "scalar",
            "kpi_offline_mtd": "scalar",
            "kpi_channel_mtd": "scalar",
            "kpi_annual_progress": "scalar",
            "trend_region_daily": "line",
            "bar_channel_mtd": "bar",
        }
        # 双源交叉校验：_STAGE_B_L1_CARD_IDS 与本字典各自枚举七卡，
        # 漂移（加卡只改一处）在此立刻红，而不是静默漏测。
        self.assertEqual(set(_STAGE_B_L1_CARD_IDS), set(charts))
        for card_id, chart in charts.items():
            with self.subTest(card=card_id):
                response = self.client.get(f"/api/d/l1-cockpit/cards/{card_id}")

                self.assertEqual(200, response.status_code)
                self.assertEqual("no-store", response.headers["cache-control"])
                payload = response.json()
                self.assertEqual(chart, payload["chart"])
                self._assert_payload_structure(card_id, payload)

    def test_kpi_offline_mtd_reconciles_with_direct_sql_sum(self):
        """口径对拍 (addition C): API JSON value == direct SQL SUM.

        The SUM is recomputed here with the same 合计 exclusion and
        month truncation, written independently of ``queries.py``; the
        printed pair is the reconciliation evidence for the report.
        """
        with self.mart_connection.cursor() as cursor:
            cursor.execute(_RECON_OFFLINE_MTD_SQL)
            sql_value = float(next(iter(cursor.fetchone().values())))

        # 新 app 实例：阶段 2 的卡片缓存按实例持有，口径对拍必须直查
        # mart（与 _TTLOptionSets 同理），绝不对拍一份缓存载荷。
        response = self._fresh_client().get(
            "/api/d/l1-cockpit/cards/kpi_offline_mtd"
        )
        payload = response.json()

        print(
            "reconciliation kpi_offline_mtd:",
            f"sql={sql_value} api={payload['value']}",
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("scalar", payload["chart"])
        self.assertAlmostEqual(sql_value, payload["value"], places=2)

    def test_kpi_annual_progress_rate_reconciles_with_two_line_target(self):
        """口径对拍 (addition C): rate == 两线年累计 ÷ 760,210,000.

        The denominator replays the version-controlled target seed first
        (the load-target post-condition), so the figure is pinned no
        matter where this test runs in the suite.
        """
        from common.public_data.target_seed import load_target_seed, replace_dim_target

        replace_dim_target(
            self.mart_connection, load_target_seed(_REPO_TARGET_SEED_PATH)
        )
        with self.mart_connection.cursor() as cursor:
            cursor.execute(_RECON_OFFLINE_ANNUAL_SQL)
            offline_annual = float(next(iter(cursor.fetchone().values())))
            cursor.execute(_RECON_CHANNEL_ANNUAL_SQL)
            channel_annual = float(next(iter(cursor.fetchone().values())))
            cursor.execute(_RECON_TWO_LINE_TARGET_SQL)
            target = float(next(iter(cursor.fetchone().values())))

        two_line_annual = offline_annual + channel_annual
        # 同上：replace_dim_target 之后必须直查，新实例 = 冷缓存。
        response = self._fresh_client().get(
            "/api/d/l1-cockpit/cards/kpi_annual_progress"
        )
        payload = response.json()

        print(
            "reconciliation kpi_annual_progress:",
            f"two_line_annual={two_line_annual} target={target}",
            f"api_value={payload['value']} api_rate={payload['rate']}",
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("scalar", payload["chart"])
        self.assertEqual(760210000.0, target)
        self.assertAlmostEqual(two_line_annual, payload["value"], places=2)
        self.assertAlmostEqual(target, payload["target"], places=2)
        self.assertAlmostEqual(two_line_annual / target, payload["rate"], places=9)

    def test_every_l2_card_answers_its_chart_payload_with_no_store(self):
        for dashboard_id, charts in _STAGE_B_L2_PLACEMENTS.items():
            for card_id, chart in charts.items():
                with self.subTest(dashboard=dashboard_id, card=card_id):
                    response = self.client.get(
                        f"/api/d/{dashboard_id}/cards/{card_id}"
                    )

                    self.assertEqual(200, response.status_code)
                    self.assertEqual("no-store", response.headers["cache-control"])
                    payload = response.json()
                    self.assertEqual(chart, payload["chart"])
                    self._assert_payload_structure(card_id, payload)

    def test_l2_definitions_carry_two_filters_and_full_navigation(self):
        # 计划原文对三页统一断言 region 首筛，但 Task 10 落地的 l2-channel
        # 筛选是 channel（渠道）+ month：第一筛选项按页面区分。
        # 卡片计数从 _STAGE_B_L2_PLACEMENTS 推导（单一数据源）；API-first
        # 之后，筛选/下钻/参数白名单都由 v1 定义载荷承载。
        card_counts = {
            dashboard_id: len(cards)
            for dashboard_id, cards in _STAGE_B_L2_PLACEMENTS.items()
        }
        first_params = {
            "l2-region": "region",
            "l2-channel": "channel",
            "l2-people": "region",
        }
        for dashboard_id, expected in card_counts.items():
            with self.subTest(dashboard=dashboard_id):
                response = self.client.get(f"/api/v1/dashboards/{dashboard_id}")

                self.assertEqual(200, response.status_code)
                payload = response.json()
                self.assertEqual(expected, len(payload["cards"]))
                self.assertEqual(2, len(payload["filters"]))
                self.assertEqual(
                    first_params[dashboard_id], payload["filters"][0]["param"]
                )
                self.assertEqual("month", payload["filters"][1]["param"])
                # The guarded shell URL still serves the page.
                self.assertEqual(200, self.client.get(f"/d/{dashboard_id}").status_code)

        channel_payload = self.client.get("/api/v1/dashboards/l2-channel").json()
        # 旧模板断言 data-onclick-param="channel" 与 data-params="channel month"
        # 同在页面出现——它们分属两张卡：下钻柱卡（bar_channel_mtd）自身
        # 只收 month（始终展示全渠道排行，点击设置 channel 筛选给其他卡）；
        # table_store_mtd 才同时收 channel + month。
        bar = next(
            card for card in channel_payload["cards"] if card["on_click"] is not None
        )
        self.assertEqual("channel", bar["on_click"])
        self.assertEqual(["month"], bar["params"])
        store_table = next(
            card for card in channel_payload["cards"] if card["card"] == "table_store_mtd"
        )
        self.assertEqual(["channel", "month"], store_table["params"])

        nav = self.client.get("/api/v1/dashboards").json()["dashboards"]
        self.assertEqual(
            ["l1-cockpit", "l2-region", "l2-channel", "l2-people"],
            [entry["id"] for entry in nav],
        )

    def test_l2_api_accepts_valid_params_and_rejects_invalid_values(self):
        today = self._server_today()
        month = today.strftime("%Y-%m")
        with transaction(self.mart_connection):
            with self.mart_connection.cursor() as cursor:
                cursor.executemany(
                    _APP_OFFLINE_INSERT_SQL,
                    [
                        (
                            _APP_FIXTURE_PREFIX + "gate-a",
                            "biweb甲",
                            "biweb甲人员",
                            today,
                            100,
                            _APP_SYNC_RUN_ID,
                        ),
                        (
                            _APP_FIXTURE_PREFIX + "gate-b",
                            "biweb乙",
                            "biweb乙人员",
                            today,
                            0,
                            _APP_SYNC_RUN_ID,
                        ),
                    ],
                )
        client = self._fresh_client()

        # 合法组合：值闸放行 fixture 区域（DISTINCT region 已含 biweb甲/乙）。
        ok = client.get(
            "/api/d/l2-region/cards/kpi_region_mtd",
            params={"region": "biweb甲", "month": month},
        )
        self.assertEqual(200, ok.status_code)
        payload = ok.json()
        self.assertEqual("scalar", payload["chart"])
        self.assertEqual(100.0, payload["value"])
        # fixture 未给 monthly_target → target=0；100/0 除零护栏 → rate=None
        # （kpi 卡的 None 路径在集成层同样成立）。
        self.assertEqual(0.0, payload["target"])
        self.assertIsNone(payload["rate"])

        # 无数据组合（spec §8）：零额区域 → 200 空载荷，绝不 400。
        empty = client.get(
            "/api/d/l2-region/cards/kpi_region_mtd",
            params={"region": "biweb乙", "month": month},
        )
        self.assertEqual(200, empty.status_code)
        self.assertEqual(0.0, empty.json()["value"])

        trend = client.get(
            "/api/d/l2-region/cards/trend_region_daily",
            params={"region": "biweb甲", "month": month},
        ).json()
        self.assertEqual("line", trend["chart"])
        self.assertEqual(["biweb甲"], [entry["name"] for entry in trend["series"]])
        self.assertEqual([today.strftime("%m-%d")], trend["dates"])
        self.assertEqual([100.0], trend["series"][0]["data"])

        # 值闸拒绝：不在维表里的值 → 400，安全文案且值绝不回显。
        bad = client.get(
            "/api/d/l2-region/cards/kpi_region_mtd",
            params={"region": "不存在区域"},
        )
        self.assertEqual(400, bad.status_code)
        self.assertEqual("bad_request", bad.json()["detail"])
        self.assertNotIn("不存在区域", bad.text)

        # 键白名单（阶段 A 既有行为）在集成层同样成立。
        unknown = client.get(
            "/api/d/l2-region/cards/kpi_region_mtd", params={"wat": "1"}
        )
        self.assertEqual(400, unknown.status_code)
        self.assertEqual("bad_request", unknown.json()["detail"])

    # -- helpers -------------------------------------------------------------

    def _assert_payload_structure(self, card_id, payload):
        """Structure and type only: real data may legitimately be zero."""
        if card_id in ("kpi_offline_mtd", "kpi_channel_mtd"):
            self.assertIsInstance(payload["value"], float)
            self.assertEqual("元", payload["unit"])
        elif card_id in ("kpi_offline_dod", "kpi_channel_dod"):
            # 真实 mart 当日预填行 sales_amount 全 NULL 时 SUM=None：value 与
            # prev/delta_pct/trend7 按可空处理（差值法对拍测试同约定）；date
            # 取自 latest 行必然有值，放宽仅为与家族一致的防御性写法。
            self.assertIsInstance(payload["value"], (float, type(None)))
            self.assertIsInstance(payload["date"], (str, type(None)))
            self.assertIsInstance(payload["prev"], (float, type(None)))
            self.assertIsInstance(payload["delta_pct"], (float, type(None)))
            self.assertIsInstance(payload["trend7"], list)
            for entry in payload["trend7"]:
                self.assertIsInstance(entry["date"], str)
                self.assertIsInstance(entry["value"], (float, type(None)))
            self.assertEqual("元", payload["unit"])
        elif card_id in (
            "kpi_annual_progress",
            "kpi_region_mtd",
            "kpi_people_rate",
        ):
            self.assertIsInstance(payload["value"], float)
            self.assertIsInstance(payload["target"], float)
            # ``None`` only while dim_target is empty (before load-target).
            self.assertIsInstance(payload["rate"], (float, type(None)))
            self.assertEqual("元", payload["unit"])
        elif card_id == "kpi_people_count":
            self.assertIsInstance(payload["value"], float)
            self.assertEqual("人", payload["unit"])
        elif card_id == "kpi_people_completed":
            self.assertIsInstance(payload["value"], float)
            self.assertEqual("元", payload["unit"])
        elif card_id in ("trend_region_daily", "trend_channel_daily"):
            self.assertIsInstance(payload["dates"], list)
            for entry in payload["series"]:
                self.assertIsInstance(entry["name"], str)
                self.assertEqual(len(payload["dates"]), len(entry["data"]))
                for point in entry["data"]:
                    self.assertIsInstance(point, float)
        elif card_id in ("bar_channel_mtd", "bar_department_mtd"):
            self.assertEqual(len(payload["categories"]), len(payload["values"]))
            for category in payload["categories"]:
                self.assertIsInstance(category, str)
            for value in payload["values"]:
                self.assertIsInstance(value, float)
            self.assertEqual("元", payload["unit"])
        elif card_id in (
            "table_channel_mtd",
            "table_store_mtd",
            "table_people_leaderboard",
        ):
            self.assertIsInstance(payload["columns"], list)
            for column in payload["columns"]:
                self.assertIn("key", column)
                self.assertIn("title", column)
            self.assertIsInstance(payload["rows"], list)
        else:
            self.fail(f"payload structure not asserted for {card_id}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
