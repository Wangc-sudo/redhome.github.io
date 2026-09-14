"""FastAPI assembly for the bi-web cockpit (plan Task 4).

Output discipline follows :mod:`common.public_data.cli`: this module never
prints credentials, payloads, URLs, or tracebacks.  Every error body is
generalized ("unauthorized" / "unavailable" / "not_found" / "bad_request" /
"card_error") so a host name, SQL fragment, or exception text can never
reach an HTTP response; the only line ``main`` can ever print is a single
safe startup-failure message.  The 5xx exception paths each log one WARNING
carrying the exception class name only -- enough for an operator to tell
"SQL failed" from "config corrupt" without approaching the leak boundary
(``str(exc)`` and tracebacks are never logged).

Routing (spec section 6):

* ``GET /``   -- redirect to the default dashboard (``l1-cockpit``);
* ``GET /d/{dashboard_id}`` -- server-rendered shell, one placeholder per
  placed card, polling at ``refresh_seconds``; the shell context also
  carries the top navigation (every enabled dashboard by ``nav_order``),
  the page's filter dropdowns (options loaded from the mart, TTL-cached)
  and each card's whitelisted param names;
* ``GET /api/d/{dashboard_id}/cards/{card_id}`` -- card JSON with
  ``Cache-Control: no-store``; URL parameters are whitelisted against
  ``Card.params_schema`` and their values validated against the
  dimension option sets (TTL-cached, fail-open on a failed lookup -- a
  bad value can only ever surface as an empty card, never a 5xx); the
  SQL itself is fully static;
* ``GET /healthz`` -- liveness + mart connectivity (``SELECT 1``).

Two guards run per request on ``/d/`` and ``/api/`` only:

* Bearer auth (``BI_WEB_TOKEN``; unset or empty means fully open);
* the pipeline-registry gate -- Nacos turns the service off and every page
  answers 503 without a restart.  The gate check is TTL-cached (30s) and
  fail-open, the same semantics as ``cli._pipeline_enabled``.

The default database connector only ever passes ``settings.mart_database``
to :func:`connect`: bi-web is a read-only mart consumer and references the
raw dingtalk/wdt databases nowhere (the code-level half of the zero-raw
rule; the compose contract test is the other half).
"""

import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from common.bi_web import queries
from common.bi_web.cards import REGISTRY, CardConfigError, validate_dashboard_config
from common.bi_web.config import DashboardConfigError, load_seed, parse_dashboard_config

#: The dashboard ``GET /`` redirects to (spec section 6).
_DEFAULT_DASHBOARD_ID = "l1-cockpit"

#: How long a pipeline-registry gate answer stays cached (design section 13:
#: request-level gating without a registry read per request).
_GATE_TTL_SECONDS = 30.0

#: How long a filter option set stays cached (one dimension query per
#: source per window, mirroring the gate TTL).
_FILTER_TTL_SECONDS = 30.0

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

_TEMPLATES = Jinja2Templates(directory=_TEMPLATES_DIR)

#: Module logger -- the repo's first logging module, so the pattern is set
#: here: one module-level logger per module, 5xx paths log exactly one
#: WARNING with the exception class name only (never ``str(exc)`` or a
#: traceback, see the module docstring).
_LOGGER = logging.getLogger(__name__)

#: Filter source name -> option query.  The key set is pinned to
#: ``config.KNOWN_FILTER_SOURCES`` by the test suite -- drift is a red
#: build, not a runtime KeyError.
_FILTER_SOURCE_QUERIES = {
    "regions": queries.region_options,
    "channels": queries.channel_options,
    "months": queries.month_options,
}


# ---------------------------------------------------------------------------
# Lazy wrappers -- module-level so tests can patch them without importing
# heavy dependencies (pymysql, uvicorn) at module-load time.  Same pattern
# as ``common/public_data/cli.py``.
# ---------------------------------------------------------------------------

def load_settings():
    """Return ``Settings`` from the process environment."""
    from common.public_data.settings import Settings
    return Settings.from_environment()


def connect(database_settings):
    """Open one public-data connection."""
    from common.public_data.db import connect as _connect
    return _connect(database_settings)


def build_dashboard_config_source():
    """Return the configured dashboard-registry source."""
    from common.bi_web.config import build_dashboard_config_source as _build
    return _build()


def serve(application):
    """Run *application* under uvicorn on the fixed stage-A address."""
    import uvicorn
    uvicorn.run(application, host="0.0.0.0", port=8080)


# ---------------------------------------------------------------------------
# Startup seed validation
# ---------------------------------------------------------------------------

def validate_seed_file(seed_path, registry):
    """Validate the dashboard seed at *seed_path* against *registry*.

    ``load_seed`` + ``parse_dashboard_config`` +
    ``validate_dashboard_config``: a seed entry that is malformed or that
    references a card id missing from the registry fails at startup, so
    configuration drift can never reach a running server.
    """
    for dashboard_id, raw in load_seed(seed_path).items():
        validate_dashboard_config(parse_dashboard_config(dashboard_id, raw), registry)


# ---------------------------------------------------------------------------
# The request-level pipeline gate
# ---------------------------------------------------------------------------

def _default_gate_check() -> Callable[[], bool]:
    """Build the default gate check; the service id is resolved once, here.

    This is ``cli._pipeline_enabled`` for a long-lived process: the registry
    decides whether bi-web serves at all, and an unreachable registry never
    silently blanks the cockpit.
    """
    from common.public_data.pipeline_config import build_config_source, resolve_service_id

    service_id = resolve_service_id() or ""

    def check() -> bool:
        if not service_id:
            return True
        return bool(build_config_source().get_pipeline(service_id).enabled)

    return check


class _TTLGate:
    """Pipeline gate answer cached for a short TTL (thread-safe).

    The cached window trades up-to-``ttl_seconds`` staleness for one
    registry read per window instead of one per request.  A raising check
    counts as enabled -- fail-open, exactly like ``cli._pipeline_enabled``.
    """

    def __init__(self, check=None, ttl_seconds=_GATE_TTL_SECONDS):
        self._check = _default_gate_check() if check is None else check
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._checked_at = None
        self._enabled = True

    def __call__(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if self._checked_at is None or now - self._checked_at >= self._ttl_seconds:
                try:
                    self._enabled = bool(self._check())
                except Exception:
                    self._enabled = True
                self._checked_at = now
            return self._enabled


class _TTLOptionSets:
    """Filter option sets cached per source with a TTL (thread-safe).

    One dimension query per source per TTL window instead of one per
    request.  A failed query is never cached and answers ``None``: the
    value gate then fails open (a bad value only ever surfaces as an
    empty card, never a 5xx), while the page route turns it into a
    local 503.
    """

    def __init__(self, db_connector, source_queries=None,
                 ttl_seconds=_FILTER_TTL_SECONDS):
        self._db_connector = db_connector
        self._queries = (
            _FILTER_SOURCE_QUERIES if source_queries is None else source_queries
        )
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._options = {}
        self._options_at = {}

    def _fetch(self, source):
        query = self._queries.get(source)
        if query is None:
            return None
        try:
            with self._db_connector() as connection:
                return tuple(query(connection))
        except Exception as exc:
            _LOGGER.warning("filter options failed to load: %s", type(exc).__name__)
            return None

    def options(self, source):
        """Cached option tuple, or ``None`` (failed / unknown source)."""
        with self._lock:
            now = time.monotonic()
            cached_at = self._options_at.get(source)
            if cached_at is None or now - cached_at >= self._ttl_seconds:
                fetched = self._fetch(source)
                if fetched is None:
                    self._options.pop(source, None)
                    self._options_at.pop(source, None)
                    return None
                self._options[source] = fetched
                self._options_at[source] = now
            return self._options.get(source)

    def contains(self, source, value):
        """Fail-open membership: unknown source or failed lookup -> True."""
        options = self.options(source)
        return True if options is None else value in options


# ---------------------------------------------------------------------------
# Request guards
# ---------------------------------------------------------------------------

def _build_bearer_dependency(token):
    """Bearer dependency for the ``/d/`` and ``/api/`` routes (no-op if open)."""
    expected = f"Bearer {token}" if token else None

    def require_bearer(authorization: str | None = Header(default=None)) -> None:
        if expected is not None and authorization != expected:
            raise HTTPException(status_code=401, detail="unauthorized")

    return require_bearer


def _mart_connector(settings):
    """Default ``db_connector``: one mart connection per request, always closed.

    Only ``settings.mart_database`` is ever passed to :func:`connect`; the
    raw dingtalk/wdt databases are unreachable from this process by
    construction.
    """

    @contextmanager
    def connector():
        connection = connect(settings.mart_database)
        try:
            yield connection
        finally:
            connection.close()

    return connector


def _resolve_dashboard(dashboard_id, dashboard_source, registry, gate):
    """The shared ``/d/`` and ``/api/`` prefix chain.

    Gate off -> 503.  A corrupt definition (source parse failure or a card
    id missing from the registry) -> 503, and only for that page.  A
    dashboard that is disabled -- or that resolved to the built-in minimal
    default with no placed cards, i.e. missing everywhere in the
    Nacos -> seed -> default chain -- is not a page -> 404.
    """
    if not gate():
        raise HTTPException(status_code=503, detail="unavailable")
    try:
        dashboard = dashboard_source.get_dashboard(dashboard_id)
    except DashboardConfigError as exc:
        _LOGGER.warning(
            "dashboard config failed to resolve: %s", type(exc).__name__
        )
        raise HTTPException(status_code=503, detail="unavailable")
    if not dashboard.enabled or not dashboard.cards:
        raise HTTPException(status_code=404, detail="not_found")
    try:
        validate_dashboard_config(dashboard, registry)
    except CardConfigError as exc:
        _LOGGER.warning(
            "dashboard config failed validation: %s", type(exc).__name__
        )
        raise HTTPException(status_code=503, detail="unavailable")
    return dashboard


def _nav_entries(dashboard_source):
    """Top-navigation entries: every enabled dashboard, by nav_order.

    Enumeration failures degrade to "no navigation" (fail-open, never a
    5xx); a corrupt sibling entry is skipped with one safe WARNING --
    the broken page itself already answers 503 when visited directly.
    """
    try:
        ids = dashboard_source.dashboard_ids()
    except Exception as exc:
        _LOGGER.warning("dashboard enumeration failed: %s", type(exc).__name__)
        return ()
    entries = []
    for dashboard_id in ids:
        try:
            dashboard = dashboard_source.get_dashboard(dashboard_id)
        except DashboardConfigError as exc:
            _LOGGER.warning(
                "dashboard config failed to resolve: %s", type(exc).__name__
            )
            continue
        if dashboard.enabled and dashboard.cards:
            entries.append(
                (dashboard.nav_order, dashboard.dashboard_id, dashboard.title)
            )
    return tuple(
        (dashboard_id, title) for _, dashboard_id, title in sorted(entries)
    )


def _filter_views(dashboard, option_sets):
    """One render-ready view per page filter; 503 when options fail."""
    views = []
    for spec in dashboard.filters:
        options = option_sets.options(spec.source)
        if options is None:
            raise HTTPException(status_code=503, detail="unavailable")
        views.append(
            SimpleNamespace(param=spec.param, label=spec.label, options=options)
        )
    return tuple(views)


def _card_params(dashboard, registry):
    """card_id -> its whitelisted param names, for the page's JS wiring.

    The frontend intersects page URL params with this per-card list, so
    a page-level param never 400s a card that does not declare it.
    """
    return {
        placement.card: tuple(registry[placement.card].params_schema)
        for placement in dashboard.cards
    }


# ---------------------------------------------------------------------------
# The application factory
# ---------------------------------------------------------------------------

def create_app(*, settings, dashboard_source, registry=REGISTRY,
               token=None, gate=None, db_connector=None,
               seed_path=None) -> FastAPI:
    """Assemble the bi-web application with every dependency injected.

    ``settings`` feeds only the default ``db_connector`` (mart); the
    dashboard source, registry, token, gate, connector, and seed are
    explicit so tests -- and Task 6's compose wiring -- never need a real
    Nacos or RDS to exercise routing, auth, or gating.
    """
    if seed_path is not None:
        validate_seed_file(seed_path, registry)
    if db_connector is None:
        db_connector = _mart_connector(settings)
    if gate is None:
        gate = _TTLGate()
    option_sets = _TTLOptionSets(db_connector)
    require_bearer = _build_bearer_dependency(token)

    app = FastAPI(title="bi-web")
    app.mount("/static", StaticFiles(directory=_STATIC_DIR, check_dir=False))

    @app.get("/")
    def root():
        return RedirectResponse(f"/d/{_DEFAULT_DASHBOARD_ID}")

    @app.get("/healthz")
    def healthz():
        try:
            with db_connector() as connection:
                cursor = connection.cursor()
                try:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                finally:
                    cursor.close()
        except Exception as exc:
            _LOGGER.warning(
                "healthz mart probe failed: %s", type(exc).__name__
            )
            return JSONResponse({"status": "unhealthy"}, status_code=503)
        return JSONResponse({"status": "ok", "database": "ok"})

    @app.get("/d/{dashboard_id}", dependencies=[Depends(require_bearer)])
    def dashboard_page(dashboard_id: str, request: Request):
        dashboard = _resolve_dashboard(dashboard_id, dashboard_source, registry, gate)
        return _TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "dashboard": dashboard,
                "nav": _nav_entries(dashboard_source),
                "filters": _filter_views(dashboard, option_sets),
                "card_params": _card_params(dashboard, registry),
            },
        )

    @app.get(
        "/api/d/{dashboard_id}/cards/{card_id}",
        dependencies=[Depends(require_bearer)],
    )
    def card_data(dashboard_id: str, card_id: str, request: Request):
        dashboard = _resolve_dashboard(dashboard_id, dashboard_source, registry, gate)
        placement = None
        for candidate in dashboard.cards:
            if candidate.card == card_id:
                placement = candidate
                break
        if placement is None:
            raise HTTPException(status_code=404, detail="not_found")
        card = registry[card_id]
        params = dict(request.query_params)
        if any(key not in card.params_schema for key in params):
            raise HTTPException(status_code=400, detail="bad_request")
        for key, value in params.items():
            if not option_sets.contains(card.params_schema[key], value):
                raise HTTPException(status_code=400, detail="bad_request")
        try:
            with db_connector() as connection:
                payload = card.run(connection, params)
            # Constructed inside the try: a payload Starlette cannot
            # JSON-encode must land in the designed 500 card_error mapping,
            # not escape as an unhandled TypeError (bare 500).
            return JSONResponse(payload, headers={"Cache-Control": "no-store"})
        except Exception as exc:
            _LOGGER.warning(
                "card run failed: %s", type(exc).__name__
            )
            raise HTTPException(status_code=500, detail="card_error")

    return app


def main():
    """Build the app from the environment and serve it.

    Any startup failure (bad settings, unreadable or drifting seed) prints
    exactly one safe line -- the fixed message plus the exception class
    name, so an operator can tell "fix the env" (Settings ValueError) from
    "fix the seed" (CardConfigError) -- and exits 1.  Never a traceback,
    never a credential, never the seed path.
    """
    try:
        settings = load_settings()
        token = os.environ.get("BI_WEB_TOKEN") or None
        dashboard_source = build_dashboard_config_source()
        seed_path = os.environ.get("PUBLIC_DATA_BI_SEED") or None
        application = create_app(
            settings=settings,
            dashboard_source=dashboard_source,
            token=token,
            seed_path=seed_path,
        )
    except Exception as exc:
        print(f"bi-web startup failed: invalid configuration ({type(exc).__name__})")
        sys.exit(1)
    serve(application)


if __name__ == "__main__":  # pragma: no cover
    main()
