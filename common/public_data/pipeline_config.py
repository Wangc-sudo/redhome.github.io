"""Pipeline registry configuration -- the Nacos-backed control plane.

Every pipeline service (an apps-line sync such as ``sync-wdt`` or a future
business-line robot) carries a small configuration entry stored in Nacos:

    namespace = environment (e.g. ``test``, or "" for the public namespace)
    group     = ``PIPELINES``
    data-id   = ``<service_id>.yaml``

The entry answers the questions a consumer cannot know statically: *is this
line enabled?* and *which source line does it run?*  It deliberately never
holds secrets or raw data (spec section 3).

Consumers depend only on the :class:`ConfigSource` contract -- never on Nacos
directly -- so the backend stays swappable without touching consumers.
"""

import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


DEFAULT_GROUP = "PIPELINES"
SUPPORTED_KINDS = ("apps", "business")
SUPPORTED_SOURCES = ("dingtalk", "wdt")


class PipelineConfigError(ValueError):
    """Raised for malformed pipeline configuration (safe messages only)."""


def _load_yaml(text, label):
    import yaml
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        raise PipelineConfigError(f"{label} is not valid JSON or YAML")


def _dump_yaml(mapping):
    import yaml
    return yaml.safe_dump(mapping, sort_keys=False, allow_unicode=True)


@dataclass(frozen=True)
class PipelineConfig:
    """Resolved configuration for a single pipeline service."""

    service_id: str
    enabled: bool = True
    kind: str = "apps"
    sources: tuple = ()
    schedule: str | None = None
    reads: tuple = ()
    description: str = ""

    def to_mapping(self):
        mapping = {"enabled": self.enabled, "kind": self.kind}
        if self.sources:
            mapping["sources"] = list(self.sources)
        if self.schedule is not None:
            mapping["schedule"] = self.schedule
        if self.reads:
            mapping["reads"] = list(self.reads)
        if self.description:
            mapping["description"] = self.description
        return mapping


def parse_pipeline_config(service_id, data):
    """Validate *data* (a mapping) into a :class:`PipelineConfig`.

    ``None`` yields the built-in default (enabled), so a missing entry never
    silently disables a line.
    """
    if data is None:
        return PipelineConfig(service_id=service_id)
    if not isinstance(data, dict):
        raise PipelineConfigError(f"pipeline '{service_id}' must be a mapping")

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise PipelineConfigError(
            f"pipeline '{service_id}' field 'enabled' must be a boolean"
        )

    kind = data.get("kind", "apps")
    if kind not in SUPPORTED_KINDS:
        raise PipelineConfigError(f"pipeline '{service_id}' has an invalid 'kind'")

    sources = data.get("sources", [])
    if not isinstance(sources, list) or any(s not in SUPPORTED_SOURCES for s in sources):
        raise PipelineConfigError(f"pipeline '{service_id}' has invalid 'sources'")

    reads = data.get("reads", [])
    if not isinstance(reads, list) or any(not isinstance(r, str) for r in reads):
        raise PipelineConfigError(f"pipeline '{service_id}' has invalid 'reads'")

    schedule = data.get("schedule")
    if schedule is not None and not isinstance(schedule, str):
        raise PipelineConfigError(f"pipeline '{service_id}' has an invalid 'schedule'")

    description = data.get("description", "")
    if not isinstance(description, str):
        raise PipelineConfigError(
            f"pipeline '{service_id}' has an invalid 'description'"
        )

    return PipelineConfig(
        service_id=service_id,
        enabled=enabled,
        kind=kind,
        sources=tuple(sources),
        schedule=schedule,
        reads=tuple(reads),
        description=description,
    )


# ---------------------------------------------------------------------------
# Config sources (the contract + its backends)
# ---------------------------------------------------------------------------

class ConfigSource:
    """Contract: resolve a service's pipeline config by id."""

    def get_pipeline(self, service_id):  # pragma: no cover - interface
        raise NotImplementedError


class StaticConfigSource(ConfigSource):
    """In-memory source (defaults and tests)."""

    def __init__(self, mapping=None):
        self._by_id = {}
        for service_id, raw in (mapping or {}).items():
            if isinstance(raw, PipelineConfig):
                self._by_id[service_id] = raw
            else:
                self._by_id[service_id] = parse_pipeline_config(service_id, raw)

    def get_pipeline(self, service_id):
        return self._by_id.get(service_id, PipelineConfig(service_id=service_id))


class FileConfigSource(ConfigSource):
    """Reads the version-controlled seed file (YAML or JSON)."""

    def __init__(self, path):
        mapping = _load_yaml(Path(path).read_text(encoding="utf-8"), f"pipeline seed {path}")
        if mapping is None:
            mapping = {}
        if not isinstance(mapping, dict):
            raise PipelineConfigError("pipeline seed must be a mapping")
        self._delegate = StaticConfigSource(mapping)

    def get_pipeline(self, service_id):
        return self._delegate.get_pipeline(service_id)


class NacosConfigSource(ConfigSource):
    """Resolves pipeline config from a Nacos config server.

    Falls back to *fallback* (typically the seed file), and finally to the
    built-in default, when the entry is missing or Nacos is unreachable --
    reports should not stop just because the registry is momentarily down.
    """

    def __init__(self, *, server, namespace="", group=DEFAULT_GROUP,
                 username=None, password=None, fallback=None, client=None):
        self._server = server
        self._namespace = namespace or ""
        self._group = group or DEFAULT_GROUP
        self._username = username
        self._password = password
        self._fallback = fallback
        self._client = client  # injectable for tests

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

    def get_pipeline(self, service_id):
        data_id = f"{service_id}.yaml"
        try:
            content = self._nacos().get_config(data_id, self._group)
        except Exception:
            content = None
        if not content:
            if self._fallback is not None:
                return self._fallback.get_pipeline(service_id)
            return PipelineConfig(service_id=service_id)
        return parse_pipeline_config(service_id, _load_yaml(content, f"nacos config {data_id}"))


def build_config_source(environ=None):
    """Pick a backend: Nacos if configured, else the seed file, else default."""
    env = os.environ if environ is None else environ
    server = (env.get("PUBLIC_DATA_NACOS_SERVER") or "").strip()
    namespace = (env.get("PUBLIC_DATA_NACOS_NAMESPACE") or "").strip()
    group = (env.get("PUBLIC_DATA_NACOS_GROUP") or "").strip() or DEFAULT_GROUP
    username = (env.get("PUBLIC_DATA_NACOS_USERNAME") or "").strip() or None
    password = (env.get("PUBLIC_DATA_NACOS_PASSWORD") or "").strip() or None
    seed_path = (env.get("PUBLIC_DATA_PIPELINE_SEED") or "").strip()

    fallback = None
    if seed_path and Path(seed_path).exists():
        fallback = FileConfigSource(seed_path)

    if server:
        return NacosConfigSource(
            server=server, namespace=namespace, group=group,
            username=username, password=password, fallback=fallback,
        )
    if fallback is not None:
        return fallback
    return StaticConfigSource({})


def resolve_service_id(environ=None, override=None):
    """Return the pipeline service id: explicit *override*, else env, else None."""
    if override:
        return override
    env = os.environ if environ is None else environ
    value = (env.get("PUBLIC_DATA_SERVICE_ID") or "").strip()
    return value or None


# ---------------------------------------------------------------------------
# Publishing the seed (first-boot import into Nacos)
# ---------------------------------------------------------------------------

def ensure_namespace(server, namespace, username=None, password=None, timeout=5):
    """Create the Nacos namespace if it does not exist (no-op for "")."""
    if not namespace:
        return
    base = server if "://" in server else f"http://{server}"
    params = urllib.parse.urlencode(
        {"customNamespaceId": namespace, "namespaceName": namespace}
    )
    if username:
        params += "&" + urllib.parse.urlencode(
            {"username": username, "password": password or ""}
        )
    request = urllib.request.Request(
        f"{base}/nacos/v1/console/namespaces",
        data=params.encode("utf-8"),
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=timeout)
    except Exception:
        # Already exists (or the server refuses duplicates) -- not fatal.
        return


def load_seed(path):
    mapping = _load_yaml(Path(path).read_text(encoding="utf-8"), f"pipeline seed {path}")
    if not isinstance(mapping, dict):
        raise PipelineConfigError("pipeline seed must be a mapping")
    return mapping


def publish_pipelines(client, mapping, group=DEFAULT_GROUP, if_missing=False):
    """Publish each seed entry to Nacos.  Returns the number written."""
    published = 0
    for service_id, raw in mapping.items():
        config = parse_pipeline_config(service_id, raw)
        data_id = f"{service_id}.yaml"
        if if_missing and client.get_config(data_id, group):
            continue
        client.publish_config(
            data_id, group, _dump_yaml(config.to_mapping()), config_type="yaml"
        )
        published += 1
    return published


def publish_seed_from_env(seed_path, if_missing=False, environ=None):
    """Build a Nacos client from the environment and publish *seed_path*."""
    env = os.environ if environ is None else environ
    server = (env.get("PUBLIC_DATA_NACOS_SERVER") or "").strip()
    if not server:
        raise PipelineConfigError("PUBLIC_DATA_NACOS_SERVER is required to publish")
    namespace = (env.get("PUBLIC_DATA_NACOS_NAMESPACE") or "").strip()
    group = (env.get("PUBLIC_DATA_NACOS_GROUP") or "").strip() or DEFAULT_GROUP
    username = (env.get("PUBLIC_DATA_NACOS_USERNAME") or "").strip() or None
    password = (env.get("PUBLIC_DATA_NACOS_PASSWORD") or "").strip() or None

    ensure_namespace(server, namespace, username=username, password=password)

    from nacos import NacosClient
    client = NacosClient(server, namespace=namespace, username=username, password=password)
    return publish_pipelines(client, load_seed(seed_path), group=group, if_missing=if_missing)
