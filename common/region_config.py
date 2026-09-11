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
from dataclasses import dataclass, field
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
    #: 榜单相关（复刻现行 config.json 的 region 段）。
    dept_order: tuple = ()
    dept_label: dict = field(default_factory=dict)
    broadcast_exclude: tuple = ()
    leaderboard_url: str = ""


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

    def _str_list(key):
        value = raw.get(key, [])
        if not isinstance(value, list) or any(
            not isinstance(v, str) or not v for v in value
        ):
            raise RegionConfigError(f"{label}.{key} must be a list of strings")
        return tuple(value)

    dept_order = _str_list("deptOrder")
    broadcast_exclude = _str_list("broadcastExclude")

    dept_label = raw.get("deptLabel", {})
    if not isinstance(dept_label, dict) or any(
        not isinstance(k, str) or not isinstance(v, str)
        for k, v in dept_label.items()
    ):
        raise RegionConfigError(f"{label}.deptLabel must be a string map")

    leaderboard_url = raw.get("leaderboardUrl", "")
    if not isinstance(leaderboard_url, str):
        raise RegionConfigError(f"{label}.leaderboardUrl must be a string")

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
        dept_order=dept_order,
        dept_label=dict(dept_label),
        broadcast_exclude=broadcast_exclude,
        leaderboard_url=leaderboard_url,
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
        "deptOrder": list(cfg.dept_order),
        "deptLabel": dict(cfg.dept_label),
        "broadcastExclude": list(cfg.broadcast_exclude),
        "leaderboardUrl": cfg.leaderboard_url,
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


# ---------------------------------------------------------------------------
# 发布（真实 region 配置 → Nacos，替换种子占位符）
# ---------------------------------------------------------------------------

def publish_region_configs(client, configs, *, if_missing=False, group=REGION_GROUP):
    """把区域配置逐条发布到 Nacos（dataId ``region-<region>.yaml``）。

    返回发布条数。*configs* 为 ``{region: RegionConfig}``（运维持真值文件，
    经 :func:`load_region_seed` 解析——与种子同 schema、同校验）。
    """
    import yaml

    published = 0
    for region, cfg in configs.items():
        data_id = f"region-{region}.yaml"
        if if_missing and client.get_config(data_id, group):
            continue
        client.publish_config(
            data_id,
            group,
            yaml.safe_dump(_to_mapping(cfg), sort_keys=False, allow_unicode=True),
            config_type="yaml",
        )
        published += 1
    return published


def publish_regions_from_env(source_path, *, if_missing=False, environ=None):
    """从环境构造 Nacos client，把 *source_path* 的真值配置发布出去。

    真值文件与种子同 schema（``regions.seed.json`` 形态），由运维保管在
    仓库之外——真实 robotCode / conversationId 永不入 git。
    """
    env = os.environ if environ is None else environ
    server = (env.get("PUBLIC_DATA_NACOS_SERVER") or "").strip()
    if not server:
        raise RegionConfigError("PUBLIC_DATA_NACOS_SERVER is required to publish")
    namespace = (env.get("PUBLIC_DATA_NACOS_NAMESPACE") or "").strip()
    username = (env.get("PUBLIC_DATA_NACOS_USERNAME") or "").strip() or None
    password = (env.get("PUBLIC_DATA_NACOS_PASSWORD") or "").strip() or None

    from common.public_data.pipeline_config import ensure_namespace
    ensure_namespace(server, namespace, username=username, password=password)

    from nacos import NacosClient
    client = NacosClient(server, namespace=namespace,
                         username=username, password=password)
    return publish_region_configs(
        client, load_region_seed(source_path), if_missing=if_missing
    )
