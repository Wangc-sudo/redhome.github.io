# -*- coding: utf-8 -*-
"""渠道门店到齐校验与催办（region=qudao，10:31 T+1 窗口，2026-09-28 方案）。

口径（运维裁决，不再讨论）：

* ``business_date`` = 今天 -1 天（北京）；T-1 该店**无行**或
  ``sales_amount IS NULL`` → 缺（催）；``sales_amount = 0`` → 已填写、
  不催，但单列「零销售」段播到群里（不 @）；金额非 0 → 到齐。
* 点名册 = ``fact_channel_store_target`` 全店 ∪ 近 30 天
  ``fact_channel_daily_sales`` 活跃店 − 区域配置 ``storeExclude``。
* 归属三级回退与 :func:`extract_ecom_people.build_fact_rows` **完全一致**
  （明细行 → 月目标表 owners_json → 渠道级兜底），禁止自造口径。
* 只发一条：outbox 去重键 ``qudao:channel_missing:{T-1}``（无时分后缀）；
  有缺口**或**有零销售才发，全齐静默。
* **fail-closed 同步门禁**：本日 dingtalk 渠道日销同步批次未 completed
  （10:31 窗口）→ ``skipped_sync_failed``，不发消息、记 ERROR——管道
  故障绝不能被误报成门店没填。

@ 映射三级：unionId → ``dim_robot_member.union_id`` → userId（映射随组织
投影物化，业务线不跨库读 raw；全空则整级跳过记 WARNING）→ 姓名精确匹配
（跨 region，歧义不 @）→ 文本点名。

纯逻辑（点名册/判定/归属/文案/映射）与 IO（ mart_ops 查询）分离；所有读
都在 mart_ops 内，不碰 raw_*、不持凭据。
"""

import contextlib
import json
import logging
from datetime import timedelta

from common.daily_robot.mart_tasks import TaskOutcome
from common.public_data.extract_ecom_people import channel_fallback_owners

logger = logging.getLogger(__name__)

MISSING = "missing"
ZERO = "zero"
OK = "ok"

_KIND = "channel_missing"

#: fail-closed 门禁：今日（北京）该时刻之后需有 completed 的 dingtalk
#: 渠道日销同步摘要（= 10:31 sync-channel-sales 窗口）。
_SYNC_GATE_DATASET_LIKE = "channel_daily_sales%"
_SYNC_GATE_BEIJING_HM = (10, 30)

#: 点名册的「近期活跃」回看天数。
_RECENT_ACTIVE_DAYS = 30

_STATE_RANK = {MISSING: 0, ZERO: 1, OK: 2}


# ---------------------------------------------------------------------------
# 纯逻辑：负责人集合 / 点名册 / 状态判定 / 归属 / 映射 / 文案（不触库）
# ---------------------------------------------------------------------------

def parse_owner_entries(value):
    """``responsible_person`` JSON → ``[{"name", "union_id"}]`` 去重保序。

    与 :func:`extract_ecom_people.parse_owners` 同容忍度（SQL NULL、
    JSON ``null``、空数组、逗号串兜底），但保留 ``unionId`` 供 @ 映射
    第一级使用。
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except ValueError:
            names = [n.strip() for n in text.split(",") if n.strip()]
            value = names
    if value is None:  # JSON "null"
        return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    entries = []
    seen = set()
    for item in value:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            union_id = item.get("unionId") or item.get("union_id") or None
            if union_id is not None:
                union_id = str(union_id).strip() or None
        else:
            name = str(item).strip() if item is not None else ""
            union_id = None
        if name and name not in seen:
            seen.add(name)
            entries.append({"name": name, "union_id": union_id})
    return entries


def expected_stores(rows_target, rows_recent, *, exclude=()):
    """点名册：月目标表全店 ∪ 近 30 天活跃店 − *exclude* → ``{store: channel}``。"""
    blocked = {str(s).strip() for s in exclude}
    roster = {}
    for row in rows_target:
        store = str(row.get("store_name") or "").strip()
        if store and store not in blocked:
            roster[store] = str(row.get("channel") or "").strip()
    for row in rows_recent:
        store = str(row.get("store_name") or "").strip()
        if store and store not in blocked and store not in roster:
            roster[store] = str(row.get("channel") or "").strip()
    return roster


def _state_of(sales):
    """``sales_amount`` → MISSING（NULL/不可解析）/ ZERO（=0）/ OK（非 0）。"""
    if sales is None:
        return MISSING
    try:
        value = float(sales)
    except (TypeError, ValueError):
        return MISSING
    return ZERO if value == 0 else OK


def store_states(rows_daily):
    """T-1 有店铺名的行 → ``{store: state}``；同店多行取最好状态。

    显式记录 ``sales_amount`` 为 NULL 的行（= ``MISSING``）：「填了但空」
    与「无行」同属缺报，但显式 NULL 行**不**再被渠道级留空行兜底救回。
    """
    states = {}
    for row in rows_daily:
        store = str(row.get("store_name") or "").strip()
        if not store:
            continue
        state = _state_of(row.get("sales_amount"))
        if store not in states or _STATE_RANK[state] > _STATE_RANK[states[store]]:
            states[store] = state
    return states


def channel_states(rows_daily):
    """T-1 店铺名留空的行（猫超式整渠道）→ ``{channel: state}``，取最好。"""
    states = {}
    for row in rows_daily:
        if str(row.get("store_name") or "").strip():
            continue
        channel = str(row.get("channel") or "").strip()
        if not channel:
            continue
        state = _state_of(row.get("sales_amount"))
        if channel not in states or _STATE_RANK[state] > _STATE_RANK[states[channel]]:
            states[channel] = state
    return states


def owners_for(store, channel, *, daily_rows, target_meta, channel_owners):
    """归属三级回退（与 ``build_fact_rows`` 优先级完全一致）→ entries。

    1. T-1 明细行自带 ``responsible_person``；
    2. ``fact_channel_store_target.owners_json``（月目标表当日集合）；
    3. 渠道级兜底（:func:`channel_fallback_owners`，姓名级、无 unionId）。
    """
    for row in daily_rows:
        if str(row.get("store_name") or "").strip() == store:
            entries = parse_owner_entries(row.get("responsible_person"))
            if entries:
                return entries
    meta = target_meta.get(store)
    if meta:
        entries = parse_owner_entries(meta.get("owners_json"))
        if entries:
            return entries
    return [
        {"name": name, "union_id": None}
        for name in (channel_owners or {}).get(channel) or []
    ]


def resolve_at_user_ids(entries, *, union_map, name_map):
    """三级 @ 映射 → ``(resolved, unmatched)``。

    *union_map* = ``{union_id: user_id}``；*name_map* = ``{name: [user_id]}``。
    返回 ``resolved = [(user_id, name)]``（去重保序）；姓名多个命中视为
    歧义——**不 @**，与全无命中一样进 ``unmatched``（文本点名）。
    """
    resolved = []
    seen_ids = set()
    unmatched = []
    for entry in entries:
        name = entry["name"]
        user_id = None
        union_id = entry.get("union_id")
        if union_id:
            user_id = union_map.get(union_id)
        if user_id is None:
            hits = name_map.get(name) or []
            if len(hits) == 1:
                user_id = hits[0]
        if user_id is None:
            if name not in unmatched:
                unmatched.append(name)
            continue
        if user_id not in seen_ids:
            seen_ids.add(user_id)
            resolved.append((user_id, name))
    return resolved, unmatched


def build_missing_message(*, display, business_date, missing_rows, zero_rows,
                          unmatched_names, url):
    """群播报文案 → ``(title, body_md)``。

    第一段「未上报」表格（渠道｜门店｜负责人，@ 由 atUserIds 承载）；
    第二段「上报为 0」只列表不 @；无法映射的姓名单列；尾部链接 +
    「已填报请忽略」。
    """
    lines = [
        f"### 📊 渠道日销到齐检查（{display} "
        f"{business_date.month}月{business_date.day}日）",
        "",
    ]
    if missing_rows:
        lines.append(
            f"以下 **{len(missing_rows)}** 家门店昨日未上报数据，请尽快补填："
        )
        lines.append("")
        lines.append("| 渠道 | 门店 | 负责人 |")
        lines.append("|---|---|---|")
        for row in missing_rows:
            owners = "、".join(row["owner_names"]) or "—"
            lines.append(
                f"| {row['channel'] or '—'} | {row['store']} | {owners} |"
            )
        lines.append("")
    if zero_rows:
        lines.append("以下门店昨日上报为 0（仅播报，不@）：")
        lines.append("")
        lines.append("| 渠道 | 门店 | 负责人 |")
        lines.append("|---|---|---|")
        for row in zero_rows:
            owners = "、".join(row["owner_names"]) or "—"
            lines.append(
                f"| {row['channel'] or '—'} | {row['store']} | {owners} |"
            )
        lines.append("")
    if unmatched_names:
        lines.append(
            f"（{'、'.join(unmatched_names)} 未匹配到钉钉账号，请手动提醒）"
        )
        lines.append("")
    lines.append(f"[点此填写]({url})")
    lines.append("")
    lines.append("已填报请忽略。")
    return "渠道日销到齐检查", "\n".join(lines)


# ---------------------------------------------------------------------------
# IO：mart_ops 只读查询
# ---------------------------------------------------------------------------

def _fetch_all(conn, sql, params=()):
    with contextlib.closing(conn.cursor()) as cursor:
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def fetch_sync_gate(conn, *, now, dataset_like=_SYNC_GATE_DATASET_LIKE,
                    beijing_hm=_SYNC_GATE_BEIJING_HM):
    """fail-closed 同步门禁：今日 10:31 窗口的渠道日销同步批次已 completed。

    *now* 按业务本地（北京）naive 解释；``completed_at`` 存 UTC，阈值按
    UTC+8 折算。容器时区无论是北京还是 UTC，「窗口时刻 − 8h」与 UTC 落库
    值自洽（同一日期内）。
    """
    hour, minute = beijing_hm
    threshold_utc = now.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    ) - timedelta(hours=8)
    rows = _fetch_all(
        conn,
        "SELECT 1 AS hit FROM `sync_dataset_summary` s "
        "JOIN `sync_runs` r ON r.`sync_run_id` = s.`sync_run_id` "
        "WHERE r.`status` = 'completed' "
        "AND s.`source_name` = %s "
        "AND s.`dataset_name` LIKE %s "
        "AND s.`completed_at` >= %s LIMIT 1",
        ("dingtalk", dataset_like, threshold_utc),
    )
    return bool(rows)


def fetch_roster_rows(conn, business_date):
    """点名册源行：``(月目标表行, 近 30 天活跃店行)``。"""
    target_rows = _fetch_all(
        conn,
        "SELECT `store_name`, `channel`, `owners_json` "
        "FROM `fact_channel_store_target`",
    )
    since = business_date - timedelta(days=_RECENT_ACTIVE_DAYS - 1)
    recent_rows = _fetch_all(
        conn,
        "SELECT DISTINCT `store_name`, `channel` "
        "FROM `fact_channel_daily_sales` "
        "WHERE `business_date` >= %s AND `business_date` < %s "
        "AND `store_name` IS NOT NULL AND `store_name` <> ''",
        (since, business_date),
    )
    return target_rows, recent_rows


def fetch_daily_rows(conn, business_date):
    """T-1 渠道日销事实行。"""
    return _fetch_all(
        conn,
        "SELECT `channel`, `store_name`, `sales_amount`, `responsible_person` "
        "FROM `fact_channel_daily_sales` WHERE `business_date` = %s",
        (business_date,),
    )


def fetch_union_map(conn):
    """``{union_id: user_id}``（dim_robot_member）。

    列不存在（迁移未应用）或全空时返回 ``{}``——unionId 级映射整级
    跳过（记 WARNING），落姓名匹配，**不**让整个催办因此失败。
    """
    try:
        rows = _fetch_all(
            conn,
            "SELECT `union_id`, `user_id` FROM `dim_robot_member` "
            "WHERE `union_id` IS NOT NULL AND `union_id` <> ''",
        )
    except Exception:
        logger.warning(
            "dim_robot_member.union_id 不可读（迁移未应用？），"
            "unionId 级映射整级跳过",
            exc_info=True,
        )
        return {}
    return {row["union_id"]: row["user_id"] for row in rows}


def fetch_name_map(conn):
    """``{name: [user_id, ...]}``（跨 region；多名命中即歧义）。"""
    rows = _fetch_all(conn, "SELECT `name`, `user_id` FROM `dim_robot_member`")
    name_map = {}
    for row in rows:
        bucket = name_map.setdefault(row["name"], [])
        if row["user_id"] not in bucket:
            bucket.append(row["user_id"])
    return name_map


# ---------------------------------------------------------------------------
# 任务入口
# ---------------------------------------------------------------------------

def run_channel_missing(conn, outbox, *, region, display, business_date, now,
                        table_url, cc_user_ids=(), store_exclude=(),
                        zero_sales_mention=True, at_limit=20, dry=False,
                        report=None, dedupe_suffix=None):
    """门店到齐校验：缺口 @ 负责人 + 零销售播报 → outbox（一条）。

    *dry* 只算不写（灰度核对）；*report* 传入 dict 时回填明细
    （roster/missing/zero/at/unmatched），供 CLI 打印与测试断言。
    ``dedupe_suffix``（run-once 手动触发 `--force` 传入）另起去重键
    强制重发；缺省维持当日幂等。
    """
    if not fetch_sync_gate(conn, now=now):
        logger.error(
            "channel_missing %s：本日渠道日销同步批次未完成（10:31 窗口），"
            "fail-closed 跳过催办（防管道故障误报为门店未填）",
            business_date,
        )
        return TaskOutcome("skipped_sync_failed", _KIND, business_date)

    target_rows, recent_rows = fetch_roster_rows(conn, business_date)
    roster = expected_stores(target_rows, recent_rows, exclude=store_exclude)
    daily_rows = fetch_daily_rows(conn, business_date)
    states = store_states(daily_rows)
    cstates = channel_states(daily_rows)
    target_meta = {
        str(r.get("store_name") or "").strip(): r for r in target_rows
    }
    channel_owners = channel_fallback_owners([
        {"channel": r.get("channel"), "responsible_person": r.get("owners_json")}
        for r in target_rows
    ])

    missing_rows, zero_rows, entries_pool = [], [], []
    for store, channel in roster.items():
        state = states.get(store) or cstates.get(channel) or MISSING
        entries = owners_for(
            store, channel,
            daily_rows=daily_rows, target_meta=target_meta,
            channel_owners=channel_owners,
        )
        item = {
            "store": store,
            "channel": channel,
            "owner_names": [e["name"] for e in entries],
        }
        if state == MISSING:
            missing_rows.append(item)
            entries_pool.extend(entries)
        elif state == ZERO and zero_sales_mention:
            zero_rows.append(item)

    if not missing_rows and not zero_rows:
        if report is not None:
            report.update({
                "roster": sorted(roster), "missing": [], "zero": [],
                "at_user_ids": [], "unmatched": [],
            })
        return TaskOutcome("all_filled", _KIND, business_date)

    union_map = fetch_union_map(conn)
    if not union_map and any(e.get("union_id") for e in entries_pool):
        logger.warning(
            "dim_robot_member.union_id 全空（payload_json 无 unionid 或"
            "组织投影未重跑？），unionId 级映射整级跳过，落姓名匹配"
        )
    name_map = fetch_name_map(conn)
    resolved, unmatched = resolve_at_user_ids(
        entries_pool, union_map=union_map, name_map=name_map
    )

    at_ids = [user_id for user_id, _ in resolved]
    cc = ()
    if len(at_ids) > at_limit:
        overflow_names = [name for _, name in resolved[at_limit:]]
        at_ids = at_ids[:at_limit]
        unmatched = unmatched + [
            n for n in overflow_names if n not in unmatched
        ]
        cc = tuple(cc_user_ids)

    title, body = build_missing_message(
        display=display, business_date=business_date,
        missing_rows=missing_rows, zero_rows=zero_rows,
        unmatched_names=unmatched, url=table_url,
    )

    if report is not None:
        report.update({
            "roster": sorted(roster),
            "missing": missing_rows,
            "zero": zero_rows,
            "at_user_ids": at_ids,
            "cc_user_ids": list(cc),
            "unmatched": unmatched,
            "title": title,
            "body_md": body,
        })

    if dry:
        return TaskOutcome(
            "dry", _KIND, business_date,
            unfilled=tuple(r["store"] for r in missing_rows),
        )

    enqueued = outbox.enqueue(
        region=region,
        kind=_KIND,
        business_date=business_date,
        title=title,
        body_md=body,
        at_user_ids=[*at_ids, *cc],
        created_at=now,
        dedupe_suffix=dedupe_suffix,
    )
    return TaskOutcome(
        "enqueued" if enqueued else "already_sent",
        _KIND,
        business_date,
        unfilled=tuple(r["store"] for r in missing_rows),
        enqueued=enqueued,
    )
