"""渠道月目标程序导入（2026-10-07 运维裁决：月目标真源从 AI 表切到程序维护）。

真源 = ``raw_dingtalk.channel_monthly_target``（快照表，无月份列）。导入 =
同事务全量替换快照（DELETE + INSERT），随后
``extract-mart --dataset channel_monthly_target`` 重建
``fact_channel_store_target``，下游（渠道榜单页目标、填报名册 owners、
extract_ecom_people）全部沿用现有链路，零代码改动。AI 表 sheet 已从
manifest 移除，raw 不再被每日 sync 覆盖。

写纪律同 ``report_roster``：SQL 只在本模块；CLI ``load-channel-target``
是唯一写入口。
"""

import json
from datetime import datetime, timezone

from common.public_data.db import transaction

#: 注册渠道词（= channel_intake.CHANNEL_WORDS 取值域，本层不依赖 gateway）。
CHANNELS = ("即时零售", "拼多多", "京东", "天猫", "直播", "私域", "猫超")

#: 程序导入行的 sync_run_id（与 AI 表 sync 的行区分，便于审计与排查）。
MANUAL_SYNC_RUN_ID = "manual:load-channel-target"


class ChannelTargetError(ValueError):
    """渠道月目标导入数据无效（消息只含字段名/行号，不含值）。"""


def load_target_rows(path):
    """解析并校验月目标 JSON 文件，返回规范化行列表。

    文件形态::

        {"rows": [{"channel": "京东", "store_name": "JD购喝",
                   "monthly_target": 1500000, "owners": ["娄灿斌"]}, ...]}

    monthly_target 单位为元、可为 null（无目标店）；owners 为姓名列表
    （可为空）。任何行非法抛 :class:`ChannelTargetError`。
    """
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        raise ChannelTargetError("file is not a readable JSON document")
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ChannelTargetError("rows must be a non-empty list")
    normalized = []
    seen_stores = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ChannelTargetError(f"rows[{index}] must be an object")
        channel = row.get("channel")
        if channel not in CHANNELS:
            raise ChannelTargetError(f"rows[{index}].channel is not registered")
        store = row.get("store_name")
        store = store.strip() if isinstance(store, str) else store
        if not store or not isinstance(store, str) or len(store) > 128:
            raise ChannelTargetError(f"rows[{index}].store_name is required")
        if store in seen_stores:
            raise ChannelTargetError(f"rows[{index}].store_name is duplicated")
        seen_stores.add(store)
        target = row.get("monthly_target")
        if target is not None:
            if isinstance(target, bool) or not isinstance(target, (int, float)):
                raise ChannelTargetError(
                    f"rows[{index}].monthly_target must be a number or null"
                )
            if target < 0:
                raise ChannelTargetError(
                    f"rows[{index}].monthly_target must be >= 0"
                )
        owners = row.get("owners")
        if not isinstance(owners, list):
            raise ChannelTargetError(f"rows[{index}].owners must be a list")
        normalized_owners = []
        for owner in owners:
            owner = owner.strip() if isinstance(owner, str) else owner
            if not owner or not isinstance(owner, str):
                raise ChannelTargetError(
                    f"rows[{index}].owners entries must be names"
                )
            if owner not in normalized_owners:
                normalized_owners.append(owner)
        normalized.append({
            "store_name": store,
            "channel": channel,
            "monthly_target": target,
            "owners": normalized_owners,
        })
    return normalized


def replace_snapshot(connection, rows, *, sync_run_id=MANUAL_SYNC_RUN_ID,
                     now=None):
    """全量替换 raw 快照（同事务 DELETE + INSERT）。返回 (删除数, 插入数)。

    responsible_person 与 AI 表 sync 同构：[{"name": ...}]（无 unionId，
    parse_owner_entries 容错）。
    """
    now = now or datetime.now(timezone.utc)
    owners_of = lambda owners: json.dumps(
        [{"name": name} for name in owners], ensure_ascii=False
    )
    cursor = connection.cursor()
    try:
        with transaction(connection):
            cursor.execute("DELETE FROM `channel_monthly_target`")
            deleted = getattr(cursor, "rowcount", 0)
            inserted = 0
            for row in rows:
                cursor.execute(
                    "INSERT INTO `channel_monthly_target` "
                    "(`store_name`, `channel`, `monthly_target`, "
                    "`responsible_person`, `dingtalk_record_id`, "
                    "`synced_at`, `sync_run_id`) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (row["store_name"], row["channel"], row["monthly_target"],
                     owners_of(row["owners"]), f"manual:{row['store_name']}",
                     now, sync_run_id),
                )
                inserted += 1
            return deleted, inserted
    finally:
        cursor.close()
