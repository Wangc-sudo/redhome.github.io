# -*- coding: utf-8 -*-
"""区域配置（阶段 4）：robot 与 gateway 共享的业务区域参数。

通道分工（沿用「种子兜底、Nacos 为准」）：

* **区域集合由版本受控的种子定义**——新增区域必须先进种子（代码评审），
  Nacos 只覆盖字段、不新增区域；
* **Nacos 覆盖**是运行时通道：dataId ``region-<region>.yaml``、group
  ``REGIONS``；缺失或不可达 → 种子值（fail-open，注册中心抖动不停摆）。

种子里 robotCode / openConversationId / tableUrl 一律是占位符——真实值
由运维发布到 Nacos，不入 git（与 config.json / test_groups.json 同约定）。
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path


#: 区域覆盖所在的 Nacos group（与管道注册表的 PIPELINES 分离）。
REGION_GROUP = "REGIONS"

_SEED_VERSION = 1


class RegionConfigError(ValueError):
    """区域配置非法（消息不得包含配置值）。"""


@dataclass(frozen=True)
class RegionConfig:
    """一个业务区域的全部运行参数。"""

    region: str
    display: str
    table_url: str
    robot_code: str
    open_conversation_id: str
    aliases: dict
    cc_user_ids: tuple
    remind_hour: int = 18
    check_hour: int = 20


def _require_str(raw, key, label):
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise RegionConfigError(f"{label}.{key} must be a non-empty string")
    return value


def _parse_region(region, raw, label):
    if not isinstance(raw, dict):
        raise RegionConfigError(f"{label} must be an object")

    aliases = raw.get("aliases", {})
    if not isinstance(aliases, dict) or any(
        not isinstance(k, str) or not isinstance(v, str)
        for k, v in aliases.items()
    ):
        raise RegionConfigError(f"{label}.aliases must be a string map")

    cc = raw.get("ccUserIds", [])
    if not isinstance(cc, list) or any(
        not isinstance(v, str) or not v for v in cc
    ):
        raise RegionConfigError(f"{label}.ccUserIds must be a list of strings")

    remind_hour = raw.get("remindHour", 18)
    check_hour = raw.get("checkHour", 20)
    for name, hour in (("remindHour", remind_hour), ("checkHour", check_hour)):
        if not isinstance(hour, int) or isinstance(hour, bool) or not 0 <= hour <= 23:
            raise RegionConfigError(f"{label}.{name} must be 0..23")

    return RegionConfig(
        region=region,
        display=_require_str(raw, "display", label),
        table_url=_require_str(raw, "tableUrl", label),
        robot_code=_require_str(raw, "robotCode", label),
        open_conversation_id=_require_str(raw, "openConversationId", label),
        aliases=dict(aliases),
        cc_user_ids=tuple(cc),
        remind_hour=remind_hour,
        check_hour=check_hour,
    )


def load_region_seed(path):
    """读取版本受控的区域种子，返回 ``{region: RegionConfig}``。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegionConfigError("region seed is not a readable JSON file") from exc
    if not isinstance(raw, dict):
        raise RegionConfigError("region seed must be a JSON object")
    if raw.get("version") != _SEED_VERSION:
        raise RegionConfigError(f"region seed version must be {_SEED_VERSION}")

    regions = raw.get("regions")
    if not isinstance(regions, dict) or not regions:
        raise RegionConfigError("region seed regions must be a non-empty object")

    result = {}
    for region, cfg in regions.items():
        if not isinstance(region, str) or not region:
            raise RegionConfigError("region names must be non-empty strings")
        result[region] = _parse_region(region, cfg, f"regions.{region}")
    return result


def _to_mapping(cfg):
    return {
        "display": cfg.display,
        "tableUrl": cfg.table_url,
        "robotCode": cfg.robot_code,
        "openConversationId": cfg.open_conversation_id,
        "aliases": dict(cfg.aliases),
        "ccUserIds": list(cfg.cc_user_ids),
        "remindHour": cfg.remind_hour,
        "checkHour": cfg.check_hour,
    }


def apply_region_overlay(configs, overlay):
    """用 *overlay*（``dataId -> mapping | None``）逐区域覆盖字段。

    overlay 抛错或返回 None → 保留种子值（fail-open）；覆盖内容仍按完整
    schema 校验（合并后），拼错的键不会静默生效。
    """
    if overlay is None:
        return dict(configs)
    merged = {}
    for region, cfg in configs.items():
        try:
            remote = overlay(f"region-{region}.yaml")
        except Exception:
            remote = None
        if not remote:
            merged[region] = cfg
            continue
        merged[region] = _parse_region(
            region, {**_to_mapping(cfg), **remote}, f"overlay region-{region}"
        )
    return merged


def build_nacos_region_overlay(environ=None):
    """构造 Nacos 覆盖（group=``REGIONS``）。未配置服务器时返回 ``None``。"""
    env = os.environ if environ is None else environ
    server = (env.get("PUBLIC_DATA_NACOS_SERVER") or "").strip()
    if not server:
        return None
    namespace = (env.get("PUBLIC_DATA_NACOS_NAMESPACE") or "").strip()
    username = (env.get("PUBLIC_DATA_NACOS_USERNAME") or "").strip() or None
    password = (env.get("PUBLIC_DATA_NACOS_PASSWORD") or "").strip() or None

    client = None

    def _overlay(data_id):
        nonlocal client
        import yaml
        if client is None:
            from nacos import NacosClient
            client = NacosClient(
                server, namespace=namespace,
                username=username, password=password,
            )
        content = client.get_config(data_id, REGION_GROUP)
        if not content:
            return None
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            raise RegionConfigError(f"nacos config {data_id} must be a mapping")
        return data

    return _overlay
