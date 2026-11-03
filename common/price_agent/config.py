"""PRICE_AGENT/maochao.yaml 非敏感配置及不可变任务快照。

Windows 不持有 Nacos 凭据：ops-web 的 claim 响应返回本模块生成的配置快照和
SHA-256。job 创建后配置固定，后续 Nacos 修改只影响新 job。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

GROUP = "PRICE_AGENT"
DATA_ID = "maochao.yaml"
DEFAULT_SEED = Path("docker/integration/price-agent-maochao.yaml")

_DEFAULTS = {
    "version": 1,
    "enabled": True,
    "page_size": 100,
    "agent_poll_seconds": 5,
    "page_ready_timeout_seconds": 60,
    "subject_attempts": 2,
    "job_lease_seconds": 900,
    "heartbeat_interval_seconds": 15,
    "login_wait_seconds": 1800,
    "auth_poll_seconds": 15,
    "subjects": [],
    "restore_subject": "",
    "notify": {
        "enabled": True,
        "region": "ops",
        "on_failure": True,
        "on_result_change": True,
        "full_rows_per_message": 10,
    },
    "artifacts": {"bucket": "", "prefix": "price-competition"},
}

_INT_RANGES = {
    "agent_poll_seconds": (1, 300),
    "page_ready_timeout_seconds": (5, 300),
    "subject_attempts": (1, 5),
    "job_lease_seconds": (60, 7200),
    "heartbeat_interval_seconds": (5, 120),
    "login_wait_seconds": (60, 86400),
    "auth_poll_seconds": (5, 120),
}


class PriceAgentConfigError(ValueError):
    """配置结构或取值不合法（错误不含原始配置内容）。"""


def _integer(data, key):
    value = data.get(key, _DEFAULTS[key])
    if type(value) is not int:  # bool 也是 int，必须排除
        raise PriceAgentConfigError(f"{key} must be an integer")
    low, high = _INT_RANGES[key]
    if not low <= value <= high:
        raise PriceAgentConfigError(f"{key} must be in {low}..{high}")
    return value


def normalize_config(data) -> dict:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise PriceAgentConfigError("config must be a mapping")
    if data.get("version", 1) != 1:
        raise PriceAgentConfigError("unsupported config version")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise PriceAgentConfigError("enabled must be a boolean")
    if data.get("page_size", 100) != 100:
        raise PriceAgentConfigError("page_size must be 100")

    subjects = data.get("subjects", [])
    if not isinstance(subjects, list) or not subjects:
        raise PriceAgentConfigError("subjects must be a non-empty list")
    normalized_subjects = []
    for value in subjects:
        subject = str(value).strip()
        if not subject.isdecimal() or len(subject) > 32:
            raise PriceAgentConfigError("subjects must contain numeric ids")
        if subject in normalized_subjects:
            raise PriceAgentConfigError("subjects must be unique")
        normalized_subjects.append(subject)
    restore = str(data.get("restore_subject", "")).strip()
    if restore not in normalized_subjects:
        raise PriceAgentConfigError("restore_subject must belong to subjects")

    notify_raw = data.get("notify") or {}
    artifacts_raw = data.get("artifacts") or {}
    if not isinstance(notify_raw, dict) or not isinstance(artifacts_raw, dict):
        raise PriceAgentConfigError("notify/artifacts must be mappings")
    notify = {}
    for key in ("enabled", "on_failure", "on_result_change"):
        value = notify_raw.get(key, _DEFAULTS["notify"][key])
        if not isinstance(value, bool):
            raise PriceAgentConfigError(f"notify.{key} must be a boolean")
        notify[key] = value
    region = str(notify_raw.get("region", "ops")).strip()
    if not region or len(region) > 64:
        raise PriceAgentConfigError("notify.region is invalid")
    rows_per_message = notify_raw.get("full_rows_per_message", 10)
    if type(rows_per_message) is not int or not 1 <= rows_per_message <= 50:
        raise PriceAgentConfigError("notify.full_rows_per_message must be 1..50")
    notify.update(region=region, full_rows_per_message=rows_per_message)

    bucket = str(artifacts_raw.get("bucket", "")).strip()
    prefix = str(artifacts_raw.get("prefix", "price-competition")).strip("/")
    if not bucket or len(bucket) > 128 or not prefix or len(prefix) > 255:
        raise PriceAgentConfigError("artifacts.bucket/prefix is invalid")
    if "\n" in bucket + prefix or "\r" in bucket + prefix:
        raise PriceAgentConfigError("artifacts.bucket/prefix is invalid")

    normalized = {
        "version": 1,
        "enabled": enabled,
        "page_size": 100,
        "subjects": normalized_subjects,
        "restore_subject": restore,
        "notify": notify,
        "artifacts": {"bucket": bucket, "prefix": prefix},
    }
    for key in _INT_RANGES:
        normalized[key] = _integer(data, key)
    return normalized


def config_hash(config: dict) -> str:
    payload = json.dumps(
        config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_config(text: str) -> tuple[dict, str]:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PriceAgentConfigError("config is not valid YAML") from exc
    config = normalize_config(raw)
    return config, config_hash(config)


class FilePriceAgentConfigSource:
    def __init__(self, path):
        self._path = Path(path)

    def get_config(self):
        return parse_config(self._path.read_text(encoding="utf-8"))


class NacosPriceAgentConfigSource:
    """Nacos 优先；不可用或条目缺失时回退版本受控 seed。"""

    def __init__(self, *, server, namespace="", username=None, password=None,
                 fallback=None, client=None):
        self._server = server
        self._namespace = namespace or ""
        self._username = username
        self._password = password
        self._fallback = fallback
        self._client = client

    def _nacos(self):
        if self._client is None:
            from common.public_data.nacos_client import build_nacos_client
            self._client = build_nacos_client(
                self._server,
                namespace=self._namespace,
                username=self._username,
                password=self._password,
            )
        return self._client

    def get_config(self):
        try:
            content = self._nacos().get_config(DATA_ID, GROUP)
        except Exception as exc:
            logger.warning(
                "price-agent nacos read failed: %s -- using seed fallback",
                type(exc).__name__,
            )
            content = None
        if content:
            return parse_config(content)
        if self._fallback is not None:
            return self._fallback.get_config()
        raise PriceAgentConfigError("PRICE_AGENT config is unavailable")


def build_config_source(environ=None):
    env = os.environ if environ is None else environ
    seed_value = (env.get("PUBLIC_DATA_PRICE_AGENT_SEED") or "").strip()
    seed_path = Path(seed_value) if seed_value else DEFAULT_SEED
    fallback = FilePriceAgentConfigSource(seed_path) if seed_path.is_file() else None
    server = (env.get("PUBLIC_DATA_NACOS_SERVER") or "").strip()
    if server:
        return NacosPriceAgentConfigSource(
            server=server,
            namespace=(env.get("PUBLIC_DATA_NACOS_NAMESPACE") or "").strip(),
            username=(env.get("PUBLIC_DATA_NACOS_USERNAME") or "").strip() or None,
            password=(env.get("PUBLIC_DATA_NACOS_PASSWORD") or "").strip() or None,
            fallback=fallback,
        )
    if fallback is not None:
        return fallback
    raise PriceAgentConfigError("no PRICE_AGENT config source is available")
