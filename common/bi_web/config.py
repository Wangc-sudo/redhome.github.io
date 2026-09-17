"""Dashboard registry configuration -- the Nacos-backed control plane for bi-web.

Every dashboard (such as ``l1-cockpit``) carries a small configuration entry
stored in Nacos:

    namespace = environment (e.g. ``test``, or "" for the public namespace)
    group     = ``BI``
    data-id   = ``<dashboard_id>.yaml``

The group is a fixed constant, never an environment knob: compose pins
``PUBLIC_DATA_NACOS_GROUP`` to ``PIPELINES`` for the sync services, so this
module deliberately never reads it (spec section 5.2).

Parsing is strict -- unknown keys are rejected, so a typo such as ``car:``
cannot silently turn a full cockpit into a dashboard without cards.  The
``_说明`` documentation key is allowed and ignored at every mapping level
(dashboard, card, filter, on_click -- seed-file convention).  Error
messages contain only ids, field names and indices -- never file contents.

Consumers depend only on the :class:`DashboardConfigSource` contract -- never
on Nacos directly -- so the backend stays swappable without touching
consumers.
"""

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path


BI_GROUP = "BI"

#: How long a resolved dashboard config stays cached before the next
#: Nacos read (2026-09-14 cache spec; the same staleness budget as the
#: app's 30 s pipeline gate).
_DEFAULT_CACHE_TTL_SECONDS = 30.0

DEFAULT_REFRESH_SECONDS = 300
MIN_REFRESH_SECONDS = 60
MAX_REFRESH_SECONDS = 86400
DEFAULT_SPAN = 4
MIN_SPAN = 1
MAX_SPAN = 12
DEFAULT_NAV_ORDER = 0

_ALLOWED_DASHBOARD_KEYS = frozenset(
    {"title", "enabled", "refresh_seconds", "cards", "nav_order", "filters",
     "icon", "group", "_说明"}
)
_ALLOWED_CARD_KEYS = frozenset({"card", "title", "span", "on_click", "_说明"})
_ALLOWED_FILTER_KEYS = frozenset({"param", "source", "label", "_说明"})
_ALLOWED_ON_CLICK_KEYS = frozenset({"param", "_说明"})

#: 筛选器 ``source`` 可指向的维表查询名；与 ``app._FILTER_SOURCE_QUERIES``
#: 的键集合由测试对拍保持一致（漂移=红构建，而非运行期 KeyError）。
#: 商品口径的品牌/渠道来自 fact_order_line（店铺渠道），与
#: ``channels``（fact_channel_daily_sales 的业务渠道）是两套维度，故分开。
#: ``entities``（fact_fin_store_funds 的公司主体）供资金安全页主体筛选。
KNOWN_FILTER_SOURCES = frozenset(
    {"regions", "channels", "months", "brands", "sku_channels", "entities"}
)


class DashboardConfigError(ValueError):
    """Raised for malformed dashboard configuration (safe messages only)."""


def _load_yaml(text, label):
    import yaml
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        raise DashboardConfigError(f"{label} is not valid JSON or YAML")


def _dump_yaml(mapping):
    import yaml
    return yaml.safe_dump(mapping, sort_keys=False, allow_unicode=True)


@dataclass(frozen=True)
class FilterSpec:
    """One dropdown filter: *param* is the URL query key, *source* names the
    dimension query that fills the dropdown."""

    param: str
    source: str
    label: str = ""


@dataclass(frozen=True)
class CardPlacement:
    """One card placed on a dashboard grid (``span`` is 1..12 columns)."""

    card: str
    title: str = ""
    span: int = DEFAULT_SPAN
    on_click: str | None = None


@dataclass(frozen=True)
class DashboardConfig:
    """Resolved configuration for a single dashboard.

    There is deliberately no ``to_mapping``: the version-controlled seed is
    the source of truth and is published verbatim (see
    :func:`publish_dashboards`).
    """

    dashboard_id: str
    title: str = ""
    enabled: bool = True
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS
    nav_order: int = DEFAULT_NAV_ORDER
    cards: tuple[CardPlacement, ...] = ()
    filters: tuple[FilterSpec, ...] = ()
    #: 导航图标：emoji 短文本或 ``/static/`` 静态资源路径（FTP 资源仓，
    #: 2026-09-17）；空串 = 前端回退默认图标。展示逻辑归属后端配置层，
    #: 前端只渲染（「前端逻辑后端化」裁决）。
    icon: str = ""
    #: 导航分组名（侧栏 nav-group 标题，如 经营驾驶舱/专项分析）；空串 =
    #: 前端防御性回退。分组归属同 icon 裁决。
    group: str = ""


def parse_dashboard_config(dashboard_id, data):
    """Validate *data* (a mapping) into a :class:`DashboardConfig`.

    ``None`` yields the built-in minimal default (enabled, no cards), so a
    missing entry never silently disables a dashboard.  Unknown keys are
    rejected: orchestration lives in Nacos and a loose parse would let a
    ``car:`` typo silently drop cards from the cockpit.
    """
    if data is None:
        return DashboardConfig(dashboard_id=dashboard_id)
    if not isinstance(data, dict):
        raise DashboardConfigError(f"dashboard '{dashboard_id}' must be a mapping")

    if any(key not in _ALLOWED_DASHBOARD_KEYS for key in data):
        raise DashboardConfigError(f"dashboard '{dashboard_id}' has an unknown key")

    title = data.get("title", "")
    if not isinstance(title, str):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' field 'title' must be a string"
        )

    icon = data.get("icon", "")
    if not isinstance(icon, str):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' field 'icon' must be a string"
        )

    group = data.get("group", "")
    if not isinstance(group, str):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' field 'group' must be a string"
        )

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' field 'enabled' must be a boolean"
        )

    refresh_seconds = data.get("refresh_seconds", DEFAULT_REFRESH_SECONDS)
    if isinstance(refresh_seconds, bool) or not isinstance(refresh_seconds, int):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' field 'refresh_seconds' must be an integer"
        )
    if not MIN_REFRESH_SECONDS <= refresh_seconds <= MAX_REFRESH_SECONDS:
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' field 'refresh_seconds' must be "
            f"{MIN_REFRESH_SECONDS}..{MAX_REFRESH_SECONDS} seconds"
        )

    nav_order = data.get("nav_order", DEFAULT_NAV_ORDER)
    if isinstance(nav_order, bool) or not isinstance(nav_order, int):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' field 'nav_order' must be an integer"
        )
    if nav_order < 0:
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' field 'nav_order' must be >= 0"
        )

    cards = data.get("cards", [])
    if not isinstance(cards, list):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' field 'cards' must be a list"
        )
    parsed_cards = tuple(
        _parse_card(dashboard_id, index, raw) for index, raw in enumerate(cards)
    )

    filters = data.get("filters", [])
    if not isinstance(filters, list):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' field 'filters' must be a list"
        )
    parsed_filters = tuple(
        _parse_filter(dashboard_id, index, raw)
        for index, raw in enumerate(filters)
    )
    _reject_duplicate_filter_params(dashboard_id, parsed_filters)
    _reject_unknown_on_click_params(dashboard_id, parsed_cards, parsed_filters)

    return DashboardConfig(
        dashboard_id=dashboard_id,
        title=title,
        enabled=enabled,
        refresh_seconds=refresh_seconds,
        nav_order=nav_order,
        cards=parsed_cards,
        filters=parsed_filters,
        icon=icon,
        group=group,
    )


def _parse_card(dashboard_id, index, raw):
    if not isinstance(raw, dict):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' cards[{index}] must be a mapping"
        )

    if any(key not in _ALLOWED_CARD_KEYS for key in raw):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' cards[{index}] has an unknown key"
        )

    card = raw.get("card")
    if not isinstance(card, str) or not card:
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' cards[{index}] needs a non-empty 'card'"
        )

    title = raw.get("title", "")
    if not isinstance(title, str):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' cards[{index}] field 'title' must be a string"
        )

    span = raw.get("span", DEFAULT_SPAN)
    if isinstance(span, bool) or not isinstance(span, int):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' cards[{index}] field 'span' must be an integer"
        )
    if not MIN_SPAN <= span <= MAX_SPAN:
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' cards[{index}] field 'span' must be "
            f"{MIN_SPAN}..{MAX_SPAN}"
        )

    on_click = raw.get("on_click")
    on_click_param = None
    if on_click is not None:
        if not isinstance(on_click, dict):
            raise DashboardConfigError(
                f"dashboard '{dashboard_id}' cards[{index}] field 'on_click' "
                "must be a mapping"
            )
        if any(key not in _ALLOWED_ON_CLICK_KEYS for key in on_click):
            raise DashboardConfigError(
                f"dashboard '{dashboard_id}' cards[{index}] field 'on_click' "
                "has an unknown key"
            )
        on_click_param = on_click.get("param")
        if not isinstance(on_click_param, str) or not on_click_param:
            raise DashboardConfigError(
                f"dashboard '{dashboard_id}' cards[{index}] field 'on_click' "
                "needs a non-empty 'param'"
            )

    return CardPlacement(card=card, title=title, span=span, on_click=on_click_param)


def _parse_filter(dashboard_id, index, raw):
    if not isinstance(raw, dict):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' filters[{index}] must be a mapping"
        )
    if any(key not in _ALLOWED_FILTER_KEYS for key in raw):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' filters[{index}] has an unknown key"
        )
    param = raw.get("param")
    if not isinstance(param, str) or not param:
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' filters[{index}] needs a non-empty 'param'"
        )
    source = raw.get("source")
    if not isinstance(source, str) or not source:
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' filters[{index}] needs a non-empty 'source'"
        )
    if source not in KNOWN_FILTER_SOURCES:
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' filters[{index}] has an unknown source"
        )
    label = raw.get("label", "")
    if not isinstance(label, str):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' filters[{index}] field 'label' "
            "must be a string"
        )
    return FilterSpec(param=param, source=source, label=label)


def _reject_duplicate_filter_params(dashboard_id, filters):
    seen = set()
    for spec in filters:
        if spec.param in seen:
            raise DashboardConfigError(
                f"dashboard '{dashboard_id}' declares a filter param twice"
            )
        seen.add(spec.param)


def _reject_unknown_on_click_params(dashboard_id, cards, filters):
    """on_click 的参数必须出现在同页 filters 中（spec §5，启动即报错）。"""
    filter_params = {spec.param for spec in filters}
    for placement in cards:
        if placement.on_click is not None and placement.on_click not in filter_params:
            raise DashboardConfigError(
                f"dashboard '{dashboard_id}' cards on_click param "
                "is not a page filter"
            )


# ---------------------------------------------------------------------------
# Config sources (the contract + its backends)
# ---------------------------------------------------------------------------

class DashboardConfigSource:
    """Contract: resolve a dashboard's config by id."""

    def get_dashboard(self, dashboard_id):  # pragma: no cover - interface
        raise NotImplementedError

    def dashboard_ids(self):
        """Ids this source can enumerate for the top navigation.

        基类返回空（不可枚举）；具体后端覆写。app 层把枚举异常当作
        「无导航」处理（fail-open），而不是让整页 5xx。
        """
        return ()


class StaticDashboardSource(DashboardConfigSource):
    """In-memory source (defaults and tests)."""

    def __init__(self, mapping=None):
        self._by_id = {}
        for dashboard_id, raw in (mapping or {}).items():
            if isinstance(raw, DashboardConfig):
                self._by_id[dashboard_id] = raw
            else:
                self._by_id[dashboard_id] = parse_dashboard_config(dashboard_id, raw)

    def get_dashboard(self, dashboard_id):
        return self._by_id.get(
            dashboard_id, DashboardConfig(dashboard_id=dashboard_id)
        )

    def dashboard_ids(self):
        return tuple(sorted(self._by_id))


class FileDashboardSource(DashboardConfigSource):
    """Reads the version-controlled seed file (YAML or JSON)."""

    def __init__(self, path):
        mapping = _load_yaml(
            Path(path).read_text(encoding="utf-8"), f"dashboard seed {path}"
        )
        if mapping is None:
            mapping = {}
        if not isinstance(mapping, dict):
            raise DashboardConfigError("dashboard seed must be a mapping")
        self._delegate = StaticDashboardSource(mapping)

    def get_dashboard(self, dashboard_id):
        return self._delegate.get_dashboard(dashboard_id)

    def dashboard_ids(self):
        return self._delegate.dashboard_ids()


class NacosDashboardSource(DashboardConfigSource):
    """Resolves dashboard config from a Nacos config server.

    Falls back to *fallback* (typically the seed file), and finally to the
    built-in minimal default, when the entry is missing or Nacos is
    unreachable -- the cockpit should not blank out just because the
    registry is momentarily down.

    Resolved configs are cached for ``ttl_seconds`` (2026-09-14 cache spec):
    without it, every page render pays one Nacos read for the page itself
    plus one per navigation entry (the ``_nav_entries`` N+1), which is a
    ~4 s penalty per read when the registry is unreachable.  The TTL is
    the same staleness budget as the app's pipeline gate.  A resolution
    reached through the fallback (unreachable client or empty content)
    IS cached -- that dead-registry penalty is exactly what the cache
    exists to bound; only a corrupt entry (a parse error, answered 503
    per request) is never cached, so the next call retries the read.
    """

    def __init__(self, *, server, namespace="", group=BI_GROUP,
                 username=None, password=None, fallback=None, client=None,
                 ttl_seconds=_DEFAULT_CACHE_TTL_SECONDS, monotonic=None):
        self._server = server
        self._namespace = namespace or ""
        self._group = group or BI_GROUP
        self._username = username
        self._password = password
        self._fallback = fallback
        self._client = client  # injectable for tests
        self._ttl_seconds = ttl_seconds
        self._monotonic = time.monotonic if monotonic is None else monotonic
        self._lock = threading.Lock()
        self._cache = {}
        self._ids_cache = None

    def _nacos(self):
        if self._client is None:
            from nacos import NacosClient
            self._client = NacosClient(
                self._server,
                namespace=self._namespace,
                username=self._username,
                password=self._password,
            )
        return self._client

    def _fetch(self, dashboard_id):
        data_id = f"{dashboard_id}.yaml"
        try:
            content = self._nacos().get_config(data_id, self._group)
        except Exception:
            content = None
        if not content:
            if self._fallback is not None:
                return self._fallback.get_dashboard(dashboard_id)
            return DashboardConfig(dashboard_id=dashboard_id)
        return parse_dashboard_config(
            dashboard_id, _load_yaml(content, f"nacos config {data_id}")
        )

    def get_dashboard(self, dashboard_id):
        # The lock covers the fetch (same semantics as app._TTLGate):
        # concurrent first reads of one key collapse to a single fetch.
        with self._lock:
            now = self._monotonic()
            cached = self._cache.get(dashboard_id)
            if cached is not None and now - cached[0] < self._ttl_seconds:
                return cached[1]
            dashboard = self._fetch(dashboard_id)
            self._cache[dashboard_id] = (now, dashboard)
            return dashboard

    def dashboard_ids(self):
        with self._lock:
            now = self._monotonic()
            if (self._ids_cache is not None
                    and now - self._ids_cache[0] < self._ttl_seconds):
                return self._ids_cache[1]
            ids = (self._fallback.dashboard_ids()
                   if self._fallback is not None else ())
            self._ids_cache = (now, ids)
            return ids


def build_dashboard_config_source(environ=None):
    """Pick a backend: Nacos if configured, else the seed file, else default.

    ``PUBLIC_DATA_BI_SEED`` is read here (from the environment, not
    ``Settings``) following the ``PUBLIC_DATA_PIPELINE_SEED`` precedent.
    The group is always ``BI``: ``PUBLIC_DATA_NACOS_GROUP`` is the pipeline
    services' knob and is deliberately not read.
    """
    env = os.environ if environ is None else environ
    server = (env.get("PUBLIC_DATA_NACOS_SERVER") or "").strip()
    namespace = (env.get("PUBLIC_DATA_NACOS_NAMESPACE") or "").strip()
    username = (env.get("PUBLIC_DATA_NACOS_USERNAME") or "").strip() or None
    password = (env.get("PUBLIC_DATA_NACOS_PASSWORD") or "").strip() or None
    seed_path = (env.get("PUBLIC_DATA_BI_SEED") or "").strip()

    fallback = None
    if seed_path and Path(seed_path).exists():
        fallback = FileDashboardSource(seed_path)

    if server:
        return NacosDashboardSource(
            server=server, namespace=namespace, group=BI_GROUP,
            username=username, password=password, fallback=fallback,
        )
    if fallback is not None:
        return fallback
    return StaticDashboardSource({})


# ---------------------------------------------------------------------------
# Publishing the seed (first-boot import into Nacos)
# ---------------------------------------------------------------------------

def load_seed(path):
    mapping = _load_yaml(
        Path(path).read_text(encoding="utf-8"), f"dashboard seed {path}"
    )
    if not isinstance(mapping, dict):
        raise DashboardConfigError("dashboard seed must be a mapping")
    return mapping


def publish_dashboards(client, mapping, group=BI_GROUP, if_missing=False):
    """Publish each seed entry to Nacos.  Returns the number written.

    The seed dict is published verbatim (``DashboardConfig`` has no
    ``to_mapping``): the file is the source of truth.  Every entry is still
    validated first, so a malformed dashboard never reaches the registry.
    """
    published = 0
    for dashboard_id, raw in mapping.items():
        parse_dashboard_config(dashboard_id, raw)  # validate only
        data_id = f"{dashboard_id}.yaml"
        if if_missing and client.get_config(data_id, group):
            continue
        client.publish_config(data_id, group, _dump_yaml(raw), config_type="yaml")
        published += 1
    return published


def publish_bi_seed_from_env(seed_path, if_missing=False, environ=None):
    """Build a Nacos client from the environment and publish *seed_path*.

    The group is the fixed ``BI`` group: ``PUBLIC_DATA_NACOS_GROUP`` is the
    pipeline services' knob and is deliberately not read.
    """
    env = os.environ if environ is None else environ
    server = (env.get("PUBLIC_DATA_NACOS_SERVER") or "").strip()
    if not server:
        raise DashboardConfigError("PUBLIC_DATA_NACOS_SERVER is required to publish")
    namespace = (env.get("PUBLIC_DATA_NACOS_NAMESPACE") or "").strip()
    username = (env.get("PUBLIC_DATA_NACOS_USERNAME") or "").strip() or None
    password = (env.get("PUBLIC_DATA_NACOS_PASSWORD") or "").strip() or None

    # Same namespace as the pipeline registry; reuse its (tested) helper.
    from common.public_data.pipeline_config import ensure_namespace
    ensure_namespace(server, namespace, username=username, password=password)

    from nacos import NacosClient
    client = NacosClient(server, namespace=namespace, username=username, password=password)
    return publish_dashboards(
        client, load_seed(seed_path), group=BI_GROUP, if_missing=if_missing
    )
