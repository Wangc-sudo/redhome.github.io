# -*- coding: utf-8 -*-
"""区域配置（阶段 4）：robot 与 gateway 共享的业务区域参数。

通道分工（沿用「种子兜底、Nacos 为准」）：

* **区域集合由版本受控的种子定义**——新增区域必须先进种子（代码评审），
  Nacos 只覆盖字段、不新增区域；
* **Nacos 覆盖**是运行时通道：dataId ``region-<region>.yaml``、group
  ``REGIONS``；缺失或不可达 → 种子值（fail-open，注册中心抖动不停摆）。

种子里 robotCode / openConversationId / tableUrl 一律是占位符——真实值
由运维发布到 Nacos，不入 git（与 config.json / test_groups.json 同约定）。

无 AI 表区域（报数直接 Stream 落库）的当月月目标由可选字段
``monthlyTargets``（表内用名 → 金额）承载：报数落库时快照进事实行，
口径等价于 AI 表行携带月目标。真实目标值同样只走 Nacos，不进种子。
"""

import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path


logger = logging.getLogger(__name__)


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
    #: 渠道到齐校验的门店排除清单（storeExclude，如已撤店/测试店）。
    store_exclude: tuple = ()
    #: 无 AI 表区域的当月月目标（表内用名 → 金额），报数落库时快照。
    monthly_targets: dict = field(default_factory=dict)


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
    store_exclude = _str_list("storeExclude")

    dept_label = raw.get("deptLabel", {})
    if not isinstance(dept_label, dict) or any(
        not isinstance(k, str) or not isinstance(v, str)
        for k, v in dept_label.items()
    ):
        raise RegionConfigError(f"{label}.deptLabel must be a string map")

    leaderboard_url = raw.get("leaderboardUrl", "")
    if not isinstance(leaderboard_url, str):
        raise RegionConfigError(f"{label}.leaderboardUrl must be a string")

    monthly_targets = raw.get("monthlyTargets") or {}
    if not isinstance(monthly_targets, dict) or any(
        not isinstance(k, str)
        or not k
        or isinstance(v, bool)
        or not isinstance(v, (int, float))
        or not math.isfinite(v)
        or v < 0
        for k, v in monthly_targets.items()
    ):
        raise RegionConfigError(
            f"{label}.monthlyTargets must map non-empty names "
            "to non-negative finite numbers"
        )

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
        store_exclude=store_exclude,
        monthly_targets=dict(monthly_targets),
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
        "storeExclude": list(cfg.store_exclude),
        "monthlyTargets": dict(cfg.monthly_targets),
    }


#: 自家榜单页 URL 基址 env（2026-10-26 用户裁决：自家系统 URL 不存 DB——
#: 基址是部署属性，随备案/端口/域名变，变化频率与业务数据完全不同，
#: 冻结进 DB 行就是「每次换域名都要批量修数」的根因）。配置后播报里的
#: 榜单链接恒为 ``{base}/{region}.html`` 派生；库存 leaderboard_url
#: 仅在 env 未配时兜底（兼容未迁移环境与将来的非标准路径覆盖）。
PAGES_BASE_ENV = "PUBLIC_PAGES_BASE"


def leaderboard_url_for(region, stored_url="", environ=None):
    """榜单页 URL 派生：env ``PUBLIC_PAGES_BASE`` 已配 → 模板现拼
    （``{base}/{region}.html``，基址变更 = 改一处 env 全局生效）；
    未配 → 库存 *stored_url* 原样兜底。两者皆空 → 空串（渲染层省略）。
    """
    env = os.environ if environ is None else environ
    base = (env.get(PAGES_BASE_ENV) or "").strip().rstrip("/")
    if base:
        return f"{base}/{region}.html"
    return stored_url or ""


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
        except Exception as exc:
            # fail-open 保留种子值是有意设计，但注册表故障必须可观测。
            logger.warning(
                "region overlay fetch failed for region-%s.yaml: %s -- using seed",
                region, type(exc).__name__,
            )
            remote = None
        if not remote:
            merged[region] = cfg
            continue
        merged[region] = _parse_region(
            region, {**_to_mapping(cfg), **remote}, f"overlay region-{region}"
        )
    return merged


#: DB 覆盖层（2026-10-26 权威源迁 DB）：dim_org_region 列 → RegionConfig
#: 字段的映射。JSON 文本列（dept_order 等）在合并前解析回 list/dict。
_DB_TEXT_FIELDS = ("display", "table_url", "robot_code",
                   "open_conversation_id", "remind_hour", "check_hour",
                   "leaderboard_url")
_DB_JSON_FIELDS = ("dept_order", "store_exclude", "aliases", "cc_user_ids",
                   "dept_label", "broadcast_exclude", "monthly_targets")
#: dim_org_region 列名 → seed/Nacos 键名（_parse_region 的入参形态）。
_DB_KEY_MAP = {
    "display": "display",
    "table_url": "tableUrl",
    "robot_code": "robotCode",
    "open_conversation_id": "openConversationId",
    "remind_hour": "remindHour",
    "check_hour": "checkHour",
    "leaderboard_url": "leaderboardUrl",
    "dept_order": "deptOrder",
    "store_exclude": "storeExclude",
    "aliases": "aliases",
    "cc_user_ids": "ccUserIds",
    "dept_label": "deptLabel",
    "broadcast_exclude": "broadcastExclude",
    "monthly_targets": "monthlyTargets",
}


def apply_db_overlay(configs, connection):
    """用 ``dim_org_region``（enabled=1）覆盖区域配置——DB 优先层。

    读序总链：seed 文件 → Nacos 覆盖 → **DB 覆盖（本层）**。非空字段
    覆盖；未登记的字段（NULL）保留前层值。configs 之外的区域键：核心
    字段（display/tableUrl/robotCode/openConversationId）齐全才新建，
    不齐跳过并告警。DB 不可读（迁移未跑/连接失败）→ fail-open 原样
    返回，打 WARNING（不静默）。
    """
    try:
        cols = ", ".join(f"`{c}`" for c in ("region_key",) + _DB_TEXT_FIELDS
                         + _DB_JSON_FIELDS)
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {cols} FROM `dim_org_region` WHERE `enabled`=1")
            rows = cursor.fetchall()
    except Exception as exc:
        logger.warning(
            "region db overlay fetch failed: %s -- keeping seed/nacos",
            type(exc).__name__)
        return dict(configs)

    merged = dict(configs)
    for row in rows:
        region = row.get("region_key")
        if not region:
            continue
        overlay = {}
        for col in _DB_TEXT_FIELDS:
            value = row.get(col)
            if value is not None:
                overlay[_DB_KEY_MAP[col]] = value
        for col in _DB_JSON_FIELDS:
            value = row.get(col)
            if value is None:
                continue
            try:
                overlay[_DB_KEY_MAP[col]] = json.loads(value)
            except (TypeError, ValueError):
                logger.warning(
                    "region db overlay: %s.%s is not valid JSON -- skipped",
                    region, col)
        if not overlay:
            continue
        if region in merged:
            base = _to_mapping(merged[region])
            merged[region] = _parse_region(
                region, {**base, **overlay}, f"db overlay region-{region}")
        else:
            # 纯 DB 注册的新区域：核心字段齐全才建（缺则跳过+告警）。
            required = ("display", "tableUrl", "robotCode",
                        "openConversationId")
            if all(overlay.get(k) for k in required):
                merged[region] = _parse_region(
                    region, overlay, f"db region-{region}")
            else:
                logger.warning(
                    "region db overlay: %s not in seed and core fields "
                    "incomplete -- skipped", region)
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
            from common.public_data.nacos_client import build_nacos_client
            client = build_nacos_client(
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

    from common.public_data.nacos_client import build_nacos_client
    client = build_nacos_client(
        server, namespace=namespace, username=username, password=password
    )
    return publish_region_configs(
        client, load_region_seed(source_path), if_missing=if_missing
    )
