# -*- coding: utf-8 -*-
"""电商人员业绩 extract：raw 钉钉表 → ``fact_daily_report_offline``（raw→mart 投影）。

三层架构定位：源（钉钉 AI 表「电商渠道日报表」）→ raw
（``raw_dingtalk.channel_daily_sales`` 渠道日销明细 +
``raw_dingtalk.channel_monthly_target`` 店铺月目标，均由 sync-dingtalk
落库）→ mart。本模块与 :mod:`common.public_data.extract_mart` 同处提取层：
**只读本地 raw、绝不持源凭据、绝不直连钉钉**。重放 raw 即可重建事实，
与「raw 是唯一可重放层」的架构约定一致。

业务口径（与钉钉直连版完全一致）：把店铺负责人按"集合归属"物化进 mart
（``region='qudao'``，键口径，原显示名"电商"止于 raw 层）：店铺→负责人
是 user[] 集合（≤3 人，可随时变）；
**整店日销售额与整店月目标归到集合里每一位负责人名下**——多人共一店各
计整店，禁止均摊、禁止只取第一个。

* 数据源：``channel_daily_sales``
  （channel / store_name / business_date / sales_amount /
  responsible_person(JSON)）+ ``channel_monthly_target``
  （store_name / channel / monthly_target / responsible_person(JSON)）。
  ``responsible_person`` 的 raw 落库形态为 JSON 数组字符串
  ``[{"name": "张三", "unionId": "..."}]``（SQL NULL 或 JSON ``null``
  表示空集合）。
* 写入粒度：``(owner, day)`` 聚合——同一 owner 名下多店的整店日销求和、
  各店月目标求和（每店只计一次）；原因是事实表业务键为
  ``(region, responsible_person, business_date)``，一人多店若不聚合，
  多店行共享同一业务键会互相覆盖。
* 主键：``ecom:{负责人}:{日期}``（``ecom:*`` 命名空间，与 stream 的
  ``stream:*`` 隔离）；``sync_run_id`` 用固定非全零常量，与 stream 的全零
  占位 RUN_ID 区分。
* 月目标 melt：同一 owner 每一天的事实行都携带其月目标总额，读侧聚合用
  MAX，绝不 SUM。
* 归属优先级：明细行自带负责人集合非空则用之；为空则回退用该店铺在
  月目标表的负责人集合（方案口径「集合会变，以取数当日集合为准」，
  月目标表即当日集合的载体）；两者都空才跳过。归属与目标解耦——
  月目标表 target 为 NULL 但 owners 存在时仍归属（``monthly_target``
  落 NULL，读侧按无目标处理）。
* upsert 语义照 :mod:`common.gateway.report_intake` 的业务键优先写法：
  按 ``(region, responsible_person, business_date)`` 查既有行，存在则
  UPDATE，不存在才 INSERT；同键重放只更新不插重复行。

纯逻辑（负责人集合解析、行展开）与 IO（raw 读取、DB upsert）分离：
``parse_owners`` / ``build_fact_rows`` / ``store_meta_by_store`` 不触
库，``fetch_source_rows`` / ``upsert_rows`` / ``main`` 负责 IO。

运行::

    python -m common.public_data.extract_ecom_people \
        --confirm-local-test-write            # 真写（读本地 raw）
    python -m common.public_data.extract_ecom_people --dry   # 只读 raw，不写库
"""

import argparse
import contextlib
import json
import logging
import sys
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------

#: 默认月份（跨月时用 --month 覆盖；按 ``business_date`` 所在月份过滤）。
DEFAULT_MONTH = 9

#: 写入事实表与固定列值。region 列统一为 region 键（同 extract_mart 的
#: `_REGION_KEY_BY_DISPLAY` 口径：fact 表只存键，显示名止于 raw 层）。
FACT_TABLE = "fact_daily_report_offline"
REGION = "qudao"

#: ecom extract 的固定 run id（非全零）：与 stream 报数写入的全零占位
#: ``STREAM_RUN_ID`` 区分，便于审计与排查。本 extract 逐日重放同一批行，
#: 不走 sync_runs 状态机，故用常量而非每次新 UUID。
ECOM_RUN_ID = "3f6b2c10-7c2a-4f1e-9a5b-2e6d8c4a1b77"

#: 店铺粒度月目标投影表（2026-09-23 新增）：部门（=渠道）目标/完成的
#: 真值链——部门目标 = Σ 店铺目标（共管店只计一次），修复人员粒度
#: Σ 聚合对共管店的重复计数。
STORE_TARGET_TABLE = "fact_channel_store_target"


# ---------------------------------------------------------------------------
# 纯逻辑：负责人集合解析与行展开（不触网、不触库）
# ---------------------------------------------------------------------------

def parse_owners(value):
    """raw ``responsible_person`` JSON → 去重保序的姓名列表。

    raw 落库形态为 JSON 数组字符串 ``[{"name": "...", "unionId": "..."}]``
    （pymysql 对 JSON 列返回 ``str``；也容忍已反序列化的 list/dict）。
    SQL NULL、JSON ``null``、空数组、空白字符串均返回 ``[]``，由调用方
    跳过该行并记日志。历史脏数据若是逗号分隔字符串，按逗号拆分兜底。
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
            items = text.split(",")  # 逗号串兜底
            return _dedupe_names(items)
    if value is None:  # JSON "null"
        return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    names = []
    for item in value:
        if isinstance(item, dict):
            names.append(item.get("name"))
        else:
            names.append(item)
    return _dedupe_names(names)


def _dedupe_names(items):
    owners = []
    for item in items:
        name = str(item).strip() if item is not None else ""
        if name and name not in owners:
            owners.append(name)
    return owners


def store_meta_by_store(target_rows):
    """月目标 raw 行 → ``{店铺: {"target": float|None, "owners": [姓名]}}``。

    *target_rows* 为 ``channel_monthly_target`` 的行 dict（``store_name`` /
    ``monthly_target`` / ``responsible_person``）。月目标表是运营维护的
    当日归属集合载体：``owners`` 供明细行负责人为空时**回退归属**；
    ``target`` 与归属解耦——target 为 NULL 但 owners 存在时仍保留归属
    （事实行 ``monthly_target`` 落 NULL，读侧 MAX 得 None，derived 按
    无目标处理）。一行一店；重复店铺后者覆盖前者并记日志。
    """
    meta = {}
    for row in target_rows:
        store = str(row.get("store_name") or "").strip()
        if not store:
            continue
        if store in meta:
            logger.warning("月目标表店铺 %s 出现多行，后者覆盖前者", store)
        meta[store] = {
            "target": _to_float(row.get("monthly_target")),
            "owners": parse_owners(row.get("responsible_person")),
        }
    return meta


def _to_float(value):
    """raw 数值列（``Decimal``/float/int/字符串）→ float；失败返回 ``None``。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _day_str(value):
    """raw ``business_date``（``datetime.date``/字符串）→ ``"%Y-%m-%d"``。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text[:10] if text else None
    if isinstance(value, date):
        return value.isoformat()
    return None


def channel_fallback_owners(target_rows):
    """渠道级兜底归属：该渠道所有带负责人集合的店铺**集合相同**时才可用。

    猫超场景（2026-09-23 核查）：日报明细 29/30 行店铺名与负责人都留空，
    店铺级回退（按 store 匹配）因此失败、个人归属几乎全丢；而猫超渠道
    只有 "MC猫超" 一个店有负责人，归属无歧义，可按渠道兜底。即时零售
    三店同为 [黄贤宋, zzz] 也适用；直播/京东/拼多多/天猫各店集合不同
    （歧义）→ 不兜底，保持跳过。返回 ``{channel: [姓名]}``。
    """
    by_channel = {}
    ambiguous = set()
    for row in target_rows:
        channel = str(row.get("channel") or "").strip()
        owners = parse_owners(row.get("responsible_person"))
        if not channel or not owners:
            continue
        if channel in by_channel and by_channel[channel] != owners:
            ambiguous.add(channel)
        else:
            by_channel[channel] = owners
    return {c: o for c, o in by_channel.items() if c not in ambiguous}


def build_fact_rows(detail_rows, store_meta, channel_owners=None):
    """把渠道日销明细展开并聚合为事实行（每 ``(owner, 日期)`` 一行）。

    *detail_rows* 是 ``channel_daily_sales`` 的行 dict（``channel`` /
    ``store_name`` / ``business_date`` / ``sales_amount`` /
    ``responsible_person``）；*store_meta* 来自 :func:`store_meta_by_store`。

    归属优先级（方案口径「集合会变，以取数当日集合为准」，月目标表即
    当日集合的载体）：

    1. 明细行自带 ``responsible_person`` 非空 → 用它；
    2. 明细行为空 → 回退用该店铺在月目标表的负责人集合；
    3. 店铺名为空（猫超式整渠道留空）→ 回退 *channel_owners* 渠道级
       兜底（仅渠道内负责人集合无歧义时，见
       :func:`channel_fallback_owners`）；
    4. 三者都空 → 跳过并记日志。

    整店日销售额归到集合里每一位 owner 名下——多人共一店各计整店，
    绝不均摊；同一 owner 名下多店时：

    * ``sales_amount`` = 当日各店整店日销之和（每店完整计入，不切分）；
    * ``monthly_target`` = 名下各**不同店铺**月目标之和（每店只计一次，
      melt 到每日行，读侧 MAX 后即为该 owner 的月目标总额）；店铺在
      月目标表中 target 为 NULL 时该店不计入求和（归属与目标解耦，
      有归属无目标仍成行，``monthly_target`` 落 NULL）；
    * ``department`` = 涉及的渠道名去重保序后以 ``"/"`` 连接。

    聚合到 ``(owner, day)`` 的原因：事实表业务键为
    ``(region, responsible_person, business_date)``，一人多店若不聚合，
    多店行共享同一业务键会在 upsert 时互相覆盖。负责人集合（含回退）
    为空、缺日期/销售额的行跳过并记日志；店铺名为空的行放行到归属
    解析（渠道级兜底，见上文第 3 条），兜底不成才按无归属跳过。
    """
    # (owner, day) -> {"sales": float, "channels": [str]}，插入序即输出行序
    aggregated = {}
    owner_stores = {}  # owner -> {store}（跨天累计，月目标每店只计一次）
    for row in detail_rows:
        channel = str(row.get("channel") or "").strip()
        store = str(row.get("store_name") or "").strip()
        day = _day_str(row.get("business_date"))
        sales = _to_float(row.get("sales_amount"))
        if day is None or sales is None:
            logger.warning(
                "跳过不完整明细行: channel=%s store=%r day=%r sales=%r",
                channel, store, day, sales,
            )
            continue
        owners = parse_owners(row.get("responsible_person"))
        if not owners:
            owners = list(store_meta.get(store, {}).get("owners") or [])
        if not owners and not store:
            owners = list((channel_owners or {}).get(channel) or [])
        if not owners:
            logger.warning(
                "跳过负责人集合为空的明细行: channel=%s store=%s day=%s",
                channel, store, day,
            )
            continue
        for owner in owners:
            owner_stores.setdefault(owner, set()).add(store)
            bucket = aggregated.setdefault(
                (owner, day), {"sales": 0.0, "channels": []}
            )
            bucket["sales"] += sales
            if channel and channel not in bucket["channels"]:
                bucket["channels"].append(channel)

    rows = []
    for (owner, day), bucket in aggregated.items():
        targets = [
            store_meta[store]["target"]
            for store in owner_stores[owner]
            if store in store_meta and store_meta[store]["target"] is not None
        ]
        rows.append({
            "source_record_id": f"ecom:{owner}:{day}",
            "region": REGION,
            "responsible_person": owner,
            "department": "/".join(bucket["channels"]),
            "business_date": day,
            "sales_amount": bucket["sales"],
            "daily_target": None,
            "monthly_target": sum(targets) if targets else None,
            "note": None,
        })
    return rows


# ---------------------------------------------------------------------------
# IO：raw 层读取
# ---------------------------------------------------------------------------

#: 明细源表白名单（``fetch_source_rows`` 的 ``detail_table`` 形参防注入）。
_DETAIL_TABLES = ("channel_daily_sales", "fact_channel_daily_sales")


def fetch_source_rows(connection, month=DEFAULT_MONTH, day=None, *,
                      detail_table="channel_daily_sales"):
    """读取源行 → ``(detail_rows, target_rows)``。

    明细按 ``--day``（精确日）或 ``--month``（``business_date`` 所在
    月份）过滤；两者都缺省时取全量。``channel_monthly_target`` 是月
    维度全量表，不做日期过滤——目标属于哪个月由调用方
    （sync-dingtalk 当月补采）保证。

    ``detail_table``（2026-10-10 修复）：raw ``channel_daily_sales``
    或 mart ``fact_channel_daily_sales`` 二选一（白名单，防注入）。
    管线内必须取后者——渠道销售真源 = raw AI 表 ∪ 机器人 inbox 归并
    （2026-10-09 PR #29），归并产物在 mart 事实表；读 raw 会让机器
    人-only 的日期（2026-10 起全部）静默零产出，qudao 人员事实行断
    供（渠道播报部门排名空表即此事故）。事实表无负责人列，补
    ``NULL AS responsible_person``，归属全走 store_meta/渠道集合回退
    （月目标表名册真源，:func:`build_fact_rows` 既有回退链）。
    """
    if detail_table not in _DETAIL_TABLES:
        raise ValueError("detail_table must be a whitelisted source table")
    owner_col = (
        "`responsible_person`"
        if detail_table == "channel_daily_sales"
        else "NULL AS `responsible_person`"
    )
    where = ""
    params = ()
    if day:
        where = "WHERE `business_date` = %s"
        params = (day,)
    elif month is not None:
        where = "WHERE MONTH(`business_date`) = %s"
        params = (month,)
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(
            "SELECT `channel`, `store_name`, `business_date`, `sales_amount`, "
            f"{owner_col} FROM `{detail_table}` "
            f"{where} ORDER BY `business_date`, `channel`, `store_name`",
            params,
        )
        detail_rows = [dict(row) for row in cursor.fetchall()]
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(
            "SELECT `store_name`, `channel`, `monthly_target`, "
            "`responsible_person` FROM `channel_monthly_target`"
        )
        target_rows = [dict(row) for row in cursor.fetchall()]
    return detail_rows, target_rows


# ---------------------------------------------------------------------------
# IO：事实表 upsert（业务键优先，照 report_intake._write_report 语义）
# ---------------------------------------------------------------------------

def _fetch_one(connection, sql, params):
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    if row is None:
        return None
    return dict(row) if isinstance(row, dict) else row


def upsert_rows(connection, rows, now):
    """把 :func:`build_fact_rows` 的行幂等写入事实表。

    业务键 ``(region, responsible_person, business_date)`` 查既有行——存在
    则 UPDATE ``sales_amount``/``monthly_target``/``synced_at``（保留其
    PK），不存在才 INSERT 全列（``sync_run_id`` 用 :data:`ECOM_RUN_ID`）。
    不写 commit（调用方在 ``transaction()`` 里统一提交）。返回统计 dict。
    """
    inserted = updated = 0
    for row in rows:
        existing = _fetch_one(
            connection,
            "SELECT `source_record_id`, `sales_amount` "
            f"FROM `{FACT_TABLE}` "
            "WHERE `region` = %s AND `responsible_person` = %s "
            "AND `business_date` = %s "
            "ORDER BY (`monthly_target` IS NULL) LIMIT 1",
            (row["region"], row["responsible_person"], row["business_date"]),
        )
        with contextlib.closing(connection.cursor()) as cursor:
            if existing is not None:
                cursor.execute(
                    f"UPDATE `{FACT_TABLE}` "
                    "SET `sales_amount` = %s, `monthly_target` = %s, "
                    "`synced_at` = %s "
                    "WHERE `source_record_id` = %s",
                    (row["sales_amount"], row["monthly_target"], now,
                     existing["source_record_id"]),
                )
                updated += 1
            else:
                cursor.execute(
                    f"INSERT INTO `{FACT_TABLE}` "
                    "(`source_record_id`, `region`, `responsible_person`, "
                    "`department`, `business_date`, `sales_amount`, "
                    "`daily_target`, `monthly_target`, `note`, "
                    "`synced_at`, `sync_run_id`) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        row["source_record_id"],
                        row["region"],
                        row["responsible_person"],
                        row["department"],
                        row["business_date"],
                        row["sales_amount"],
                        row["daily_target"],
                        row["monthly_target"],
                        row["note"],
                        now,
                        ECOM_RUN_ID,
                    ),
                )
                inserted += 1
    return {"inserted": inserted, "updated": updated}


# ---------------------------------------------------------------------------
# IO：店铺粒度月目标投影（全量替换，与 raw 月维度全量表同语义）
# ---------------------------------------------------------------------------

def replace_store_targets(connection, target_rows, *, sync_run_id, synced_at):
    """把 ``channel_monthly_target`` 全量投影进 ``fact_channel_store_target``。

    DELETE + INSERT（源表是月维度全量表，店铺下线随之消失）；不写 commit
    （调用方在 ``transaction()`` 里统一提交）。``owners_json`` 保留 raw 的
    responsible_person JSON 原文（供读侧审计归属集合），返回写入行数。
    """
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(f"DELETE FROM `{STORE_TARGET_TABLE}`")
        written = 0
        for row in target_rows:
            store = str(row.get("store_name") or "").strip()
            if not store:
                continue
            owners = row.get("responsible_person")
            owners_json = (
                owners if isinstance(owners, str) else json.dumps(
                    owners, ensure_ascii=False, default=str
                )
            ) if owners is not None else None
            cursor.execute(
                f"INSERT INTO `{STORE_TARGET_TABLE}` "
                "(`store_name`, `channel`, `monthly_target`, `owners_json`, "
                "`synced_at`, `sync_run_id`) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    store,
                    str(row.get("channel") or "").strip() or None,
                    _to_float(row.get("monthly_target")),
                    owners_json,
                    synced_at,
                    sync_run_id,
                ),
            )
            written += 1
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="extract-ecom-people",
        description="电商人员业绩 extract：raw 钉钉表 -> fact_daily_report_offline",
    )
    parser.add_argument("--month", type=int, default=DEFAULT_MONTH,
                        help=f"按 business_date 所在月份过滤明细（默认 {DEFAULT_MONTH}）")
    parser.add_argument("--day", default=None,
                        help="只取这一天（YYYY-MM-DD），优先于 --month")
    parser.add_argument("--dry", action="store_true",
                        help="只读 raw 并打印展开统计，不写库")
    parser.add_argument("--confirm-local-test-write", action="store_true",
                        help="确认写入（非 --dry 时必需）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not args.dry and not args.confirm_local_test_write:
        print("非 --dry 运行必须显式携带 --confirm-local-test-write")
        return 1

    try:
        from common.public_data.db import connect, transaction
        from common.public_data.settings import Settings

        settings = Settings.from_environment()

        raw_connection = connect(settings.dingtalk_database)
        try:
            detail_rows, target_rows = fetch_source_rows(
                raw_connection, month=args.month, day=args.day
            )
        finally:
            raw_connection.close()

        store_meta = store_meta_by_store(target_rows)
        rows = build_fact_rows(
            detail_rows, store_meta, channel_fallback_owners(target_rows)
        )
        stores_with_target = sum(
            1 for meta in store_meta.values() if meta["target"] is not None
        )
        logger.info("展开事实行 %d 条（店铺归属 %d 个，其中有目标 %d 个）",
                    len(rows), len(store_meta), stores_with_target)

        if args.dry:
            print(f"dataset=ecom_people rows={len(rows)} "
                  f"stores_with_target={stores_with_target} status=dry")
            return 0

        from common.public_data.live_safety import require_extract_run
        from common.public_data.mart_routing import (
            MartSplitSwitches,
            facts_write_databases,
        )

        require_extract_run(
            settings, confirm_local_test_write=args.confirm_local_test_write
        )
        switches = MartSplitSwitches.from_environment()
        now = datetime.now(timezone.utc)
        for db_settings in facts_write_databases(settings, switches):
            connection = connect(db_settings)
            try:
                with transaction(connection):
                    stats = upsert_rows(connection, rows, now)
                    stores_written = replace_store_targets(
                        connection, target_rows,
                        sync_run_id=ECOM_RUN_ID, synced_at=now,
                    )
            finally:
                connection.close()
            print(f"dataset=ecom_people database={db_settings.name} "
                  f"inserted={stats['inserted']} updated={stats['updated']} "
                  f"store_targets={stores_written} status=completed")
        return 0
    except Exception:
        logger.error("extract_ecom_people 运行失败", exc_info=True)
        print("dataset=ecom_people status=failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
