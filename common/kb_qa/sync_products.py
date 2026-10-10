# -*- coding: utf-8 -*-
"""kb-sync-products：AI 表「产品资料」→ mart_ops.kb_products 同步管道。

平台标准形态：
- 调度：scheduler 命令表 ``kb-sync-products``（seed 默认每小时 7 分）；
- 幂等：以钉钉 recordId 为唯一键 INSERT ... ON DUPLICATE KEY UPDATE，
  重跑/手动「运行一次」不产生脏数据（数据层幂等，无需 --force）；
- 门禁：require_live_run（外部 live 读 + 生产写双确认）；
- 字段白名单：FIELD_MAP 显式映射进列；ATTACHMENT_FIELDS 只存附件
  元信息（名称/大小/类型，不存签名 URL——URL 用时现取现下）；
  其余未映射标量字段兜底进 extra；公式字段（礼袋实拍/日期位置/
  细节实拍）跳过。

配置（env，凭据进 /opt/dops/live app.env 不入库）：
    KB_DINGTALK_APP_KEY / KB_DINGTALK_APP_SECRET / KB_DINGTALK_OPERATOR_ID
    KB_PRODUCTS_BASE_ID / KB_PRODUCTS_SHEET_ID
"""

import argparse
import json
import os
from datetime import datetime

#: AI 字段名 → (列名, 类型)。
FIELD_MAP = {
    "商品编码": ("code", "text"),
    "货品名称": ("name", "text"),
    "品牌": ("brand", "text"),
    "大类": ("category", "text"),
    "箱规": ("box_qty", "number"),
    "盒规": ("case_qty", "number"),
    "单盒尺寸(mm)": ("size_mm", "text"),
    # 2026-10-10 实测：表内是全角括号「重量（kg）」（半角键会静默漏列）。
    "重量（kg）": ("weight_kg", "number"),
    "材质": ("material", "text"),
    "礼袋规格": ("gift_bag_spec", "text"),
    "单品69码": ("barcode_single", "text"),
    "原箱69码": ("barcode_case", "text"),
}

#: 附件类字段：只留元信息，URL 不落地。
ATTACHMENT_FIELDS = (
    "尺寸重量", "细节图", "礼袋", "素材包", "随机发货", "质检报告", "进货发票",
)

#: 公式字段：派生值，不落地。
_FORMULA_FIELDS = ("礼袋实拍", "日期位置", "细节实拍")

_UPSERT_SQL = (
    "INSERT INTO `kb_products`"
    " (`record_id`, `code`, `name`, `brand`, `category`, `box_qty`,"
    "  `case_qty`, `size_mm`, `weight_kg`, `material`, `gift_bag_spec`,"
    "  `barcode_single`, `barcode_case`, `attachments`, `extra`, `synced_at`)"
    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) AS new"
    " ON DUPLICATE KEY UPDATE"
    " `code`=new.`code`, `name`=new.`name`, `brand`=new.`brand`,"
    " `category`=new.`category`, `box_qty`=new.`box_qty`,"
    " `case_qty`=new.`case_qty`, `size_mm`=new.`size_mm`,"
    " `weight_kg`=new.`weight_kg`, `material`=new.`material`,"
    " `gift_bag_spec`=new.`gift_bag_spec`,"
    " `barcode_single`=new.`barcode_single`,"
    " `barcode_case`=new.`barcode_case`,"
    " `attachments`=new.`attachments`, `extra`=new.`extra`,"
    " `synced_at`=new.`synced_at`"
)


def _scalar(value):
    """AI 表单元格值 → 标量。富文本段列表取 text 拼接；其余 str 兜底。"""
    if value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        joined = "".join(parts).strip()
        return joined or None
    if isinstance(value, dict):
        for key in ("text", "name", "value", "title"):
            if isinstance(value.get(key), str):
                return value[key].strip() or None
    return str(value)


def _attachment_meta(value):
    """附件字段值 → [{name, size, mime}]，URL 键一律丢弃。

    2026-10-10 实测附件结构：{resourceId, filename, size, type,
    resourceUrl(相对), url(OSS 绝对)}——名称键是 filename 不是 name。
    """
    if not isinstance(value, (list, tuple)):
        return None
    items = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        items.append({
            "name": entry.get("filename") or entry.get("name"),
            "size": entry.get("size"),
            "mime": entry.get("type") or entry.get("mimeType"),
            "resource_id": entry.get("resourceId"),
        })
    return items or None


def normalize_record(record):
    """一条 AI 表记录 → kb_products 行 dict；无货品名称返回 None。"""
    record_id = record.get("id") or record.get("recordId")
    fields = record.get("fields") or {}
    if not record_id or not isinstance(fields, dict):
        return None
    row = {column: None for column, _ in FIELD_MAP.values()}
    attachments = {}
    extra = {}
    for field_name, value in fields.items():
        if field_name in FIELD_MAP:
            column, kind = FIELD_MAP[field_name]
            scalar = _scalar(value)
            if kind == "number" and scalar is not None:
                try:
                    scalar = int(scalar) if float(scalar).is_integer() else float(scalar)
                except (TypeError, ValueError):
                    scalar = None
            row[column] = scalar
        elif field_name in ATTACHMENT_FIELDS:
            meta = _attachment_meta(value)
            if meta:
                attachments[field_name] = meta
        elif field_name in _FORMULA_FIELDS:
            continue
        else:
            scalar = _scalar(value)
            if scalar is not None:
                extra[field_name] = scalar
    if not row["name"]:
        return None
    row["record_id"] = str(record_id)
    row["attachments"] = attachments or None
    row["extra"] = extra or None
    return row


def upsert_products(conn, rows, *, synced_at=None):
    """幂等 upsert。返回写入行数（INSERT+UPDATE 合计）。"""
    synced_at = synced_at or datetime.now()
    stamp = synced_at.strftime("%Y-%m-%d %H:%M:%S")
    written = 0
    cursor = conn.cursor()
    try:
        for row in rows:
            cursor.execute(_UPSERT_SQL, (
                row["record_id"], row["code"], row["name"], row["brand"],
                row["category"], row["box_qty"], row["case_qty"],
                row["size_mm"], row["weight_kg"], row["material"],
                row["gift_bag_spec"], row["barcode_single"],
                row["barcode_case"],
                json.dumps(row["attachments"], ensure_ascii=False)
                if row["attachments"] else None,
                json.dumps(row["extra"], ensure_ascii=False)
                if row["extra"] else None,
                stamp,
            ))
            written += 1
    finally:
        cursor.close()
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kb_qa.sync_products")
    parser.add_argument("--live-read", action="store_true")
    parser.add_argument("--confirm-local-test-write", action="store_true")
    args = parser.parse_args(argv)

    from common.dingtalk.client import DingTalkClient
    from common.public_data import db
    from common.public_data.live_safety import require_live_run
    from common.public_data.settings import Settings

    settings = Settings.from_environment()
    require_live_run(
        settings,
        live_read=args.live_read,
        confirm_local_test_write=args.confirm_local_test_write,
    )
    client = DingTalkClient(
        os.environ["KB_DINGTALK_APP_KEY"],
        os.environ["KB_DINGTALK_APP_SECRET"],
        os.environ["KB_DINGTALK_OPERATOR_ID"],
    )
    base_id = os.environ["KB_PRODUCTS_BASE_ID"]
    sheet_id = os.environ["KB_PRODUCTS_SHEET_ID"]

    records = client.list_records(base_id, sheet_id)
    rows = [row for row in (normalize_record(r) for r in records) if row]

    conn = db.connect(settings.mart_database)
    try:
        written = upsert_products(conn, rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(
        f"service=kb-sync-products status=completed"
        f" records_read={len(records)} rows_written={written}"
        f" skipped={len(records) - len(rows)}"
    )


if __name__ == "__main__":
    main()
