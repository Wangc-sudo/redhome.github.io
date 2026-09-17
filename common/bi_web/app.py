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

Routing (spec section 6; API-first per the 2026-09-14 separation spec):

* ``GET /``   -- redirect to the default dashboard (``l1-cockpit``);
* ``GET /d/{dashboard_id}`` -- the original-design static shell
  ``web/bi.html`` (no data in it; the client routes on
  ``location.pathname``; legacy ``web/`` remains the rollback shell),
  still behind the shared resolve chain so a missing/disabled dashboard
  404s and a corrupt one 503s -- bookmarkable URLs keep their old
  semantics;
* ``GET /api/v1/dashboards`` -- navigation list (enabled, by nav_order);
* ``GET /api/v1/dashboards/{dashboard_id}`` -- the definition the shell
  renders from: title, ``refresh_seconds``, filters, and each card's
  span/title/param whitelist/on_click;
* ``GET /api/v1/options/{source}`` -- one filter's option set (unknown
  source -> 404, a failed dimension query -> 503, mirroring the old
  page-local filter 503);
* ``GET /api/d/{dashboard_id}/cards/{card_id}`` and its versioned alias
  ``GET /api/v1/d/{dashboard_id}/cards/{card_id}`` -- card JSON with
  ``Cache-Control: no-store``; URL parameters are whitelisted against
  ``Card.params_schema`` and their values validated against the
  dimension option sets (TTL-cached, fail-open on a failed lookup -- a
  bad value can only ever surface as an empty card, never a 5xx); the
  SQL itself is fully static.  The ``run`` result is served through
  the stage-2 card cache (:mod:`common.bi_web.cache`): read-through
  with hot/cold TTLs, fail-open to a direct mart read, one compute per
  key under concurrency -- the ``no-store`` header stays, caching only
  ever lives server-side;
* ``GET /diagnostics/cache`` -- read-only cache counters (backend name,
  hit/miss/error, backend-call latency aggregates), at the same
  operational tier as ``/healthz``: a top-level route OUTSIDE the
  version layer, so the versioned surface keeps one uniform auth
  semantic; no auth, no gate, and no DSN, key samples, or payloads --
  aggregates only;
* ``GET /healthz`` -- liveness + mart connectivity (``SELECT 1``).

API versioning skeleton (P1): every versioned handler is registered
through :class:`_VersionedApi`, which mounts two channels for one
handler -- the URL-prefix channel ``/api/v1/...`` (behaviour identical
to the pre-skeleton routes, field by field) and the header channel
``/api/...`` where the ``API-Version`` header picks the version
(undeclared defaults to v1, an unregistered version answers 404
``not_found``).  The legacy ``/api/d/...`` card route IS the header
channel of the v1 card handler.

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
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from common.bi_web import queries
from common.bi_web.cache import build_card_cache, card_cache_key
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

#: The API-first static frontend (2026-09-14 separation spec; original-design
#: shell per the 2026-09-17 decision): ``bi.html`` is the data-free shell
#: served at ``/d/{id}``; ``bi.js`` builds the navigation, filters, and cards
#: entirely from ``/api/v1/``.  ``index.html`` (V0) stays on the tree as the
#: ``BI_WEB_SHELL=legacy`` rollback shell.  The Jinja2 rendering path is
#: retired and the dependency unpinned.
_WEB_DIR = Path(__file__).parent / "web"

#: The bi-react production build (V1, 2026-09-16): when ``dist/index.html``
#: exists it replaces the legacy ``web/`` shell at ``/d/{id}`` -- the React
#: shell is likewise data-free and resolves the dashboard from
#: ``location.pathname`` / ``#/d/{id}``, so bookmarkable URLs keep their
#: semantics.  The legacy shell is NOT deleted: ``BI_WEB_SHELL=legacy``
#: (or simply removing the dist directory) rolls straight back to V0.
_REACT_DIST_DIR = Path(__file__).parents[2] / "frontend" / "bi-react" / "dist"


def _shell_index_html() -> Path:
    """Pick the ``/d/{id}`` shell: bi.html first, dist as fallback, else legacy.

    The original-design shell ``web/bi.html`` is the first choice; the
    bi-react dist remains a second rollback layer until P3 removes it;
    ``web/index.html`` (V0) is the final fallback.  The decision is made
    per request so a rollback (env flip or file removal) takes effect
    without a process restart.  ``BI_WEB_SHELL`` is an ops switch, never
    a secret.
    """
    if os.environ.get("BI_WEB_SHELL", "").strip().lower() == "legacy":
        return _WEB_DIR / "index.html"
    bi_index = _WEB_DIR / "bi.html"
    if bi_index.is_file():
        return bi_index
    react_index = _REACT_DIST_DIR / "index.html"
    if react_index.is_file():
        return react_index
    return _WEB_DIR / "index.html"

#: Module logger -- the repo's first logging module, so the pattern is set
#: here: one module-level logger per module, 5xx paths log exactly one
#: WARNING with the exception class name only (never ``str(exc)`` or a
#: traceback, see the module docstring).
_LOGGER = logging.getLogger(__name__)


class ErrorDetail(str, Enum):
    """The generalized error vocabulary every error response draws from.

    Enum'ed so a new ad-hoc detail string is a diff against this class,
    not a grep across the module; members are ``str`` so Starlette's
    JSON encoder serializes the value verbatim (no payload, no SQL, no
    host names can ever ride along).
    """

    UNAUTHORIZED = "unauthorized"
    UNAVAILABLE = "unavailable"
    NOT_FOUND = "not_found"
    BAD_REQUEST = "bad_request"
    CARD_ERROR = "card_error"


#: The header channel's version header (P1); undeclared -> the default.
API_VERSION_HEADER = "API-Version"

#: The version an undeclared ``API-Version`` resolves to.
DEFAULT_API_VERSION = "v1"


def _version_guard(served_version: str) -> Callable[..., None]:
    """Header-channel guard: resolve ``API-Version`` against *served_version*.

    Undeclared (or blank) resolves to :data:`DEFAULT_API_VERSION`; a
    declared version this route does not serve answers 404 -- the
    requested version simply does not exist here.
    """

    def guard(
        api_version: Optional[str] = Header(default=None, alias=API_VERSION_HEADER),
    ) -> None:
        requested = (api_version or "").strip() or DEFAULT_API_VERSION
        if requested != served_version:
            raise HTTPException(status_code=404, detail=ErrorDetail.NOT_FOUND)

    return guard


class _VersionedApi:
    """Version-route registrar: one handler, two channels (P1 skeleton).

    ``registrar.route("v1", "/dashboards")`` mounts the handler at both
    ``/api/v1/dashboards`` (URL-prefix channel: bearer only, behaviour
    identical to the pre-skeleton route) and ``/api/dashboards`` (header
    channel: bearer + the version guard).  Adding a v2 later is a second
    registrar pass over new handlers -- existing mounts never move.
    """

    def __init__(self, app: FastAPI, bearer: Callable[..., None]) -> None:
        self._app = app
        self._bearer = bearer

    def route(self, version: str, path: str) -> Callable[[Callable], Callable]:
        def decorator(handler: Callable) -> Callable:
            self._app.get(
                f"/api/{version}{path}",
                dependencies=[Depends(self._bearer)],
            )(handler)
            self._app.get(
                f"/api{path}",
                dependencies=[
                    Depends(self._bearer),
                    Depends(_version_guard(version)),
                ],
            )(handler)
            return handler

        return decorator

#: Filter source name -> option query.  The key set is pinned to
#: ``config.KNOWN_FILTER_SOURCES`` by the test suite -- drift is a red
#: build, not a runtime KeyError.
_FILTER_SOURCE_QUERIES = {
    "regions": queries.region_options,
    "channels": queries.channel_options,
    "months": queries.month_options,
    "brands": queries.brand_options,
    "sku_channels": queries.sku_channel_options,
    "entities": queries.entity_options,
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

def validate_seed_file(seed_path: str, registry: Dict[str, Any]) -> None:
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

    def require_bearer(
        authorization: Optional[str] = Header(default=None),
    ) -> None:
        if expected is not None and authorization != expected:
            raise HTTPException(status_code=401, detail=ErrorDetail.UNAUTHORIZED)

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


def _resolve_dashboard(
    dashboard_id: str,
    dashboard_source,
    registry: Dict[str, Any],
    gate: Callable[[], bool],
):
    """The shared ``/d/`` and ``/api/`` prefix chain.

    Gate off -> 503.  A corrupt definition (source parse failure or a card
    id missing from the registry) -> 503, and only for that page.  A
    dashboard that is disabled -- or that resolved to the built-in minimal
    default with no placed cards, i.e. missing everywhere in the
    Nacos -> seed -> default chain -- is not a page -> 404.
    """
    if not gate():
        raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
    try:
        dashboard = dashboard_source.get_dashboard(dashboard_id)
    except DashboardConfigError as exc:
        _LOGGER.warning(
            "dashboard config failed to resolve: %s", type(exc).__name__
        )
        raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
    if not dashboard.enabled or not dashboard.cards:
        raise HTTPException(status_code=404, detail=ErrorDetail.NOT_FOUND)
    try:
        validate_dashboard_config(dashboard, registry)
    except CardConfigError as exc:
        _LOGGER.warning(
            "dashboard config failed validation: %s", type(exc).__name__
        )
        raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
    return dashboard


def _nav_entries(dashboard_source) -> Tuple[Tuple[str, str], ...]:
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
                (dashboard.nav_order, dashboard.dashboard_id, dashboard.title,
                 dashboard.icon, dashboard.group)
            )
    return tuple(
        (dashboard_id, title, icon, group)
        for _, dashboard_id, title, icon, group in sorted(entries)
    )


def _dashboard_definition(dashboard, registry: Dict[str, Any]) -> Dict[str, Any]:
    """The ``/api/v1/dashboards/{id}`` payload the static shell renders from.

    Each card entry carries its placement (span/title/on_click) plus the
    registry's param whitelist, so the client intersects page URL params
    per card exactly like the retired server-rendered shell did -- a
    page-level param never 400s a card that does not declare it.
    """
    return {
        "id": dashboard.dashboard_id,
        "title": dashboard.title,
        "refresh_seconds": dashboard.refresh_seconds,
        "filters": [
            {"param": spec.param, "source": spec.source, "label": spec.label}
            for spec in dashboard.filters
        ],
        "cards": [
            {
                "card": placement.card,
                "title": placement.title,
                "span": placement.span,
                "on_click": placement.on_click,
                "params": list(registry[placement.card].params_schema),
            }
            for placement in dashboard.cards
        ],
    }


# ---------------------------------------------------------------------------
# The application factory
# ---------------------------------------------------------------------------

def create_app(*, settings, dashboard_source, registry=REGISTRY,
               token=None, gate=None, db_connector=None,
               seed_path=None, card_cache=None) -> FastAPI:
    """Assemble the bi-web application with every dependency injected.

    ``settings`` feeds only the default ``db_connector`` (mart); the
    dashboard source, registry, token, gate, connector, and seed are
    explicit so tests -- and Task 6's compose wiring -- never need a real
    Nacos or RDS to exercise routing, auth, or gating.  ``card_cache``
    defaults to :func:`build_card_cache` (Redis when configured, else
    in-process); it lives exactly as long as the app instance.
    """
    if seed_path is not None:
        validate_seed_file(seed_path, registry)
    if db_connector is None:
        db_connector = _mart_connector(settings)
    if gate is None:
        gate = _TTLGate()
    if card_cache is None:
        card_cache = build_card_cache()
    option_sets = _TTLOptionSets(db_connector)
    require_bearer = _build_bearer_dependency(token)

    app = FastAPI(title="bi-web")
    app.mount("/web", StaticFiles(directory=_WEB_DIR, check_dir=False))
    # bi-react dist static assets (V1 shell); check_dir=False keeps the app
    # bootable when the dist has not been built -- the shell picker then
    # falls back to the legacy web/ index.html.
    app.mount(
        "/assets",
        StaticFiles(directory=_REACT_DIST_DIR / "assets", check_dir=False),
    )
    versioned = _VersionedApi(app, require_bearer)

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

    @app.get("/diagnostics/cache")
    def cache_diagnostics():
        # healthz 同级的运维端点：顶层路由、注册在版本路由层之外，
        # 版本化表面因此保持统一的认证语义（无鉴权特例）。无 auth、
        # 无 gate、只读聚合快照（后端名 + 计数，绝不含 DSN、键样本
        # 或载荷）。
        return JSONResponse(card_cache.metrics_snapshot())

    @app.get("/d/{dashboard_id}", dependencies=[Depends(require_bearer)])
    def dashboard_page(dashboard_id: str):
        # The shell carries no data -- the same resolve chain as the APIs
        # still guards the URL (404 missing/disabled, 503 corrupt), and
        # the client fetches everything else from /api/v1/.
        _resolve_dashboard(dashboard_id, dashboard_source, registry, gate)
        return FileResponse(_shell_index_html())

    @versioned.route("v1", "/dashboards")
    def dashboard_list():
        if not gate():
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        # Enumeration failures already degrade to "no navigation" inside
        # _nav_entries (fail-open, never a 5xx).
        return JSONResponse(
            {
                "dashboards": [
                    {"id": dashboard_id, "title": title, "icon": icon,
                     "group": group}
                    for dashboard_id, title, icon, group
                    in _nav_entries(dashboard_source)
                ]
            }
        )

    @versioned.route("v1", "/dashboards/{dashboard_id}")
    def dashboard_definition(dashboard_id: str):
        dashboard = _resolve_dashboard(dashboard_id, dashboard_source, registry, gate)
        return JSONResponse(_dashboard_definition(dashboard, registry))

    @versioned.route("v1", "/options/{source}")
    def filter_options(source: str):
        if not gate():
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if source not in _FILTER_SOURCE_QUERIES:
            raise HTTPException(status_code=404, detail=ErrorDetail.NOT_FOUND)
        options = option_sets.options(source)
        if options is None:
            # Same mapping the retired shell had: a failed dimension
            # query is a local 503, never a 5xx escape.
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"source": source, "options": list(options)})

    # The header channel of this registration IS the legacy
    # ``/api/d/{dashboard_id}/cards/{card_id}`` route.
    @versioned.route("v1", "/d/{dashboard_id}/cards/{card_id}")
    def card_data(dashboard_id: str, card_id: str, request: Request):
        dashboard = _resolve_dashboard(dashboard_id, dashboard_source, registry, gate)
        placement = None
        for candidate in dashboard.cards:
            if candidate.card == card_id:
                placement = candidate
                break
        if placement is None:
            raise HTTPException(status_code=404, detail=ErrorDetail.NOT_FOUND)
        card = registry[card_id]
        params = dict(request.query_params)
        if any(key not in card.params_schema for key in params):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        for key, value in params.items():
            if not option_sets.contains(card.params_schema[key], value):
                raise HTTPException(
                    status_code=400, detail=ErrorDetail.BAD_REQUEST
                )
        def compute():
            with db_connector() as connection:
                return card.run(connection, params)

        try:
            # Read-through (stage-2 cache): hot/cold TTL by period class,
            # one compute per key under concurrency, backend failures are
            # misses -- never 5xx.  Bad values (a raising run) are never
            # cached, and the key is only built past every gate above.
            key, ttl = card_cache_key(card_id, params)
            payload = card_cache.get_or_compute(key, ttl, compute)
            # Constructed inside the try: a payload Starlette cannot
            # JSON-encode must land in the designed 500 card_error mapping,
            # not escape as an unhandled TypeError (bare 500).
            return JSONResponse(payload, headers={"Cache-Control": "no-store"})
        except Exception as exc:
            _LOGGER.warning(
                "card run failed: %s", type(exc).__name__
            )
            raise HTTPException(status_code=500, detail=ErrorDetail.CARD_ERROR)

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
