# -*- coding: utf-8 -*-
"""渠道日销机器人填报收件箱（2026-09-29 方案，region=qudao）。

手工 AI 表台账的机器人替代通道：群内一句话填报 → 本表（append-only）
→ extract 的 ``channel_daily_sales`` 分支与 raw AI 表行归并后 upsert
进 ``fact_channel_daily_sales``。

归并规则（纯逻辑 :func:`merge_channel_rows`，全部可单测）：

* 同业务键 ``(business_date, channel, store_name)`` robot 优先：窗口内
  raw 行就地改值；窗口外有 AI 行则**复用其 ``source_record_id``** 生成
  覆盖行（fact 每业务键恒一行，报表/催办/页面零改动）；
* 无对应 AI 行 → 新行 ``source_record_id = robot:{inbox_id}``；
* robot 来源行打行级 ``_sync_run_id = ROBOT_RUN_ID``（全零），
  :func:`extract_mart` 的 ``upsert_fact`` 行级 override 落库，审计可辨；
* AI 表事后补填同业务键 → raw 行变化进 extract 窗口 → AI 值覆盖回
  fact（运维裁决「AI 表数据是权威的」，后写赢语义自洽）。
"""

import contextlib
import logging

logger = logging.getLogger(__name__)

INBOX_TABLE = "channel_sales_robot_inbox"

#: robot 来源行的占位 run id（与 ``report_intake.STREAM_RUN_ID`` 同值全零：
#: stream/机器人落库不属于任何同步 run，用它与 sync/extract 行区分）。
ROBOT_RUN_ID = "00000000-0000-0000-0000-000000000000"

_RAW_TABLE = "channel_daily_sales"


def _store_key(value):
    """store_name 归一键：None/空白 → ""（猫超式渠道级行）。"""
    return str(value or "").strip()


def business_key(business_date, channel, store_name):
    return (business_date, str(channel or "").strip(), _store_key(store_name))


# ---------------------------------------------------------------------------
# 写入（gateway 侧）
# ---------------------------------------------------------------------------

def insert_inbox(conn, *, conversation_id, sender_userid, sender_name,
                 raw_text, channel, store_name, business_date, sales_amount,
                 status, reject_reason, now):
    """追加一条收件箱行，返回自增 id。"""
    with contextlib.closing(conn.cursor()) as cursor:
        cursor.execute(
            f"INSERT INTO `{INBOX_TABLE}` "
            "(`conversation_id`, `sender_userid`, `sender_name`, `raw_text`, "
            "`channel`, `store_name`, `business_date`, `sales_amount`, "
            "`status`, `reject_reason`, `created_at`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                conversation_id, sender_userid, sender_name, raw_text,
                channel, store_name, business_date, sales_amount,
                status, reject_reason, now,
            ),
        )
        return cursor.lastrowid


def fetch_latest_inbox(conn):
    """``status='parsed'`` 行按业务键归并取最新 → ``{key: row}``。

    表不存在（迁移未应用）等读取失败 → ``{}``（fail-open：extract 不因
    inbox 缺表而中断，记 WARNING）。
    """
    try:
        with contextlib.closing(conn.cursor()) as cursor:
            cursor.execute(
                f"SELECT `id`, `channel`, `store_name`, `business_date`, "
                f"`sales_amount`, `created_at` FROM `{INBOX_TABLE}` "
                "WHERE `status` = 'parsed'"
            )
            rows = [dict(row) for row in cursor.fetchall()]
    except Exception:
        logger.warning(
            "%s 不可读（迁移未应用？），robot 合并整级跳过",
            INBOX_TABLE, exc_info=True,
        )
        return {}
    latest = {}
    for row in rows:
        key = business_key(
            row.get("business_date"), row.get("channel"), row.get("store_name")
        )
        current = latest.get(key)
        if current is None or (row["created_at"], row["id"]) > (
            current["created_at"], current["id"]
        ):
            latest[key] = row
    return latest


def fetch_raw_business_keys(raw_conn):
    """raw ``channel_daily_sales`` 全量业务键 → ``source_record_id``。

    小表（数百行）全量读；同键多行（AI 表历史重复）取后出现者，归并只
    需要一个可覆盖的 recordId。
    """
    with contextlib.closing(raw_conn.cursor()) as cursor:
        cursor.execute(
            f"SELECT `dingtalk_record_id` AS `source_record_id`, `channel`, "
            f"`store_name`, `business_date` FROM `{_RAW_TABLE}`"
        )
        rows = [dict(row) for row in cursor.fetchall()]
    index = {}
    for row in rows:
        key = business_key(
            row.get("business_date"), row.get("channel"), row.get("store_name")
        )
        index[key] = row["source_record_id"]
    return index


# ---------------------------------------------------------------------------
# 归并（纯逻辑）
# ---------------------------------------------------------------------------

def merge_channel_rows(raw_rows, inbox_latest, raw_key_index):
    """raw 窗口行 + inbox 最新行 → 归并后的投影行（新列表）。

    *raw_rows*：``read_dataset`` 的输出（键为目标列名，含
    ``source_record_id``）；*inbox_latest* / *raw_key_index* 见上方两个
    fetch。返回行里 robot 来源行带 ``_sync_run_id = ROBOT_RUN_ID``。
    """
    if not inbox_latest:
        return raw_rows
    rows = [dict(row) for row in raw_rows]
    window_index = {}
    for pos, row in enumerate(rows):
        key = business_key(
            row.get("business_date"), row.get("channel"), row.get("store_name")
        )
        window_index.setdefault(key, pos)
    for key, entry in inbox_latest.items():
        record_id = raw_key_index.get(key)
        pos = window_index.get(key)
        if pos is not None:
            row = rows[pos]
            row["sales_amount"] = entry["sales_amount"]
            row["_sync_run_id"] = ROBOT_RUN_ID
            continue
        rows.append({
            "source_record_id": record_id or f"robot:{entry['id']}",
            "channel": entry["channel"],
            "store_name": entry["store_name"],
            "business_date": key[0],
            "sales_amount": entry["sales_amount"],
            "promotion_cost": None,
            "roi": None,
            "responsible_person": None,
            "_sync_run_id": ROBOT_RUN_ID,
        })
    return rows
