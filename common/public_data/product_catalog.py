"""WDT 商品目录同步 → dim_product 维度表
==========================================
从旺店通 goods_query 拉取全量商品档案，解析品牌/系列归属，
写入 dim_product 维度表（品牌→系列→SKU 三级结构）。

系列分类逻辑：
  - 优先用 WDT 返回的 brand_name 字段判定品牌
  - 在 goods_name + spec_name 中匹配系列关键词
  - 品牌字段缺失时，回退到品名关键词匹配

用法:
  python -m common.public_data.product_catalog
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.public_data.db import connect, transaction
from common.public_data.settings import Settings
from common.wdt.client import WdtClient

SERIES_RULES = {
    "习酒": [
        ("古韵", ["古韵"]),
        ("大坛", ["大坛"]),
        ("君品", ["君品"]),
        ("窖藏", ["窖藏"]),
        ("金习酒", ["金习"]),
        ("银习酒", ["银习"]),
    ],
}

BRAND_KEYWORDS_FALLBACK = [
    ("习酒", ["习酒"]),
    ("茅台", ["茅台"]),
    ("古越龙山", ["古越龙山"]),
    ("女儿红", ["女儿红"]),
    ("金沙", ["金沙"]),
    ("赖茅", ["赖茅"]),
    ("郎酒", ["郎酒"]),
    ("汾酒", ["汾酒"]),
    ("泸州老窖", ["泸州", "国窖"]),
    ("洋河", ["洋河", "梦之蓝", "天之蓝", "海之蓝"]),
    ("剑南春", ["剑南春"]),
    ("五粮液", ["五粮液"]),
    ("水井坊", ["水井坊"]),
    ("舍得", ["舍得"]),
    ("口子窖", ["口子窖"]),
]


def classify_series(brand_name, goods_name, spec_name):
    """根据品牌名和商品名判定系列归属。"""
    combined = f"{goods_name or ''} {spec_name or ''}"

    if brand_name:
        for brand_key, rules in SERIES_RULES.items():
            if brand_key in brand_name:
                for series_name, keywords in rules:
                    if any(kw in combined for kw in keywords):
                        return series_name
                return "其他"

    for brand_key, keywords in BRAND_KEYWORDS_FALLBACK:
        if any(kw in combined for kw in keywords):
            for series_name, series_kws in SERIES_RULES.get(brand_key, []):
                if any(kw in combined for kw in series_kws):
                    return series_name
            return "其他"

    return "未分类"


_DIM_PRODUCT_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_product` (\n"
    "  `spec_no` VARCHAR(100) NOT NULL,\n"
    "  `barcode` VARCHAR(100) DEFAULT NULL,\n"
    "  `goods_id` VARCHAR(50) DEFAULT NULL,\n"
    "  `goods_no` VARCHAR(100) DEFAULT NULL,\n"
    "  `goods_name` VARCHAR(500) DEFAULT NULL,\n"
    "  `spec_name` VARCHAR(500) DEFAULT NULL,\n"
    "  `brand_name` VARCHAR(200) DEFAULT NULL,\n"
    "  `series_name` VARCHAR(100) DEFAULT NULL,\n"
    "  `class_name` VARCHAR(200) DEFAULT NULL,\n"
    "  `retail_price` DECIMAL(12,2) DEFAULT NULL,\n"
    "  `wholesale_price` DECIMAL(12,2) DEFAULT NULL,\n"
    "  `is_deleted` TINYINT(1) NOT NULL DEFAULT 0,\n"
    "  `synced_at` DATETIME(6) NOT NULL,\n"
    "  `raw_json` JSON DEFAULT NULL,\n"
    "  PRIMARY KEY (`spec_no`),\n"
    "  KEY `idx_brand_series` (`brand_name`, `series_name`),\n"
    "  KEY `idx_goods_name` (`goods_name`(100))\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_UPSERT_SQL = (
    "INSERT INTO `dim_product` "
    "(`spec_no`, `barcode`, `goods_id`, `goods_no`, `goods_name`, `spec_name`, "
    "`brand_name`, `series_name`, `class_name`, `retail_price`, `wholesale_price`, "
    "`is_deleted`, `synced_at`, `raw_json`) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "`barcode`=VALUES(`barcode`), `goods_id`=VALUES(`goods_id`), "
    "`goods_no`=VALUES(`goods_no`), `goods_name`=VALUES(`goods_name`), "
    "`spec_name`=VALUES(`spec_name`), `brand_name`=VALUES(`brand_name`), "
    "`series_name`=VALUES(`series_name`), `class_name`=VALUES(`class_name`), "
    "`retail_price`=VALUES(`retail_price`), `wholesale_price`=VALUES(`wholesale_price`), "
    "`is_deleted`=0, `synced_at`=VALUES(`synced_at`), `raw_json`=VALUES(`raw_json`)"
)

_BATCH_SIZE = 200


def _fetch_all_goods(wdt_client):
    """分页拉取全量商品目录。"""
    return wdt_client.call_paged("goods_query", {}, page_size=100, max_pages=200)


def sync_product_catalog(wdt_client, db_settings):
    """从 WDT 拉取商品目录，写入 dim_product，返回统计摘要。"""
    conn = connect(db_settings)
    try:
        cursor = conn.cursor()
        cursor.execute(_DIM_PRODUCT_DDL)
        conn.commit()
        cursor.close()

        goods_list = _fetch_all_goods(wdt_client)
        now = datetime.now(timezone.utc)

        stats = {"total": 0, "new": 0, "updated": 0, "unmapped_series": 0}
        batch = []

        def flush():
            if not batch:
                return
            values = []
            for r in batch:
                values.append((
                    r["spec_no"], r.get("barcode"), r.get("goods_id"),
                    r.get("goods_no"), r.get("goods_name"), r.get("spec_name"),
                    r.get("brand_name"), r.get("series_name"), r.get("class_name"),
                    r.get("retail_price"), r.get("wholesale_price"),
                    now, json.dumps(r.get("_raw"), ensure_ascii=False),
                ))
            with transaction(conn):
                cursor = conn.cursor()
                cursor.executemany(_UPSERT_SQL, values)
                cursor.close()

        for goods in goods_list:
            brand = goods.get("brand_name") or ""
            goods_name = goods.get("goods_name") or ""
            goods_id = str(goods.get("goods_id") or "")
            goods_no = goods.get("goods_no") or ""
            class_name = goods.get("class_name") or ""

            for spec in goods.get("spec_list") or []:
                spec_no = spec.get("spec_no") or ""
                if not spec_no:
                    continue

                series = classify_series(brand, goods_name, spec.get("spec_name"))
                if series in ("其他", "未分类"):
                    stats["unmapped_series"] += 1

                batch.append({
                    "spec_no": spec_no,
                    "barcode": spec.get("barcode") or "",
                    "goods_id": goods_id,
                    "goods_no": goods_no,
                    "goods_name": goods_name,
                    "spec_name": spec.get("spec_name") or "",
                    "brand_name": brand,
                    "series_name": series,
                    "class_name": class_name,
                    "retail_price": spec.get("retail_price"),
                    "wholesale_price": spec.get("wholesale_price"),
                    "_raw": {**goods, "spec_list": [spec]},
                })
                stats["total"] += 1

                if len(batch) >= _BATCH_SIZE:
                    flush()
                    stats["new"] += len(batch)
                    batch.clear()

        if batch:
            flush()
            stats["new"] += len(batch)
            batch.clear()

        with transaction(conn):
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE `dim_product` SET `is_deleted` = 1, `synced_at` = %s "
                "WHERE `synced_at` < %s AND `is_deleted` = 0",
                (now, now),
            )
            deleted = cursor.rowcount
            cursor.close()
        stats["deleted"] = deleted

        return stats
    finally:
        conn.close()


def query_brand_series_summary(db_settings):
    """查询 dim_product 中品牌×系列的 SKU 分布，用于确认分类结果。"""
    conn = connect(db_settings)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT `brand_name`, `series_name`, COUNT(*) AS sku_count "
            "FROM `dim_product` WHERE `is_deleted` = 0 "
            "GROUP BY `brand_name`, `series_name` "
            "ORDER BY `brand_name`, `sku_count` DESC"
        )
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        conn.close()


if __name__ == "__main__":
    settings = Settings.from_environment()

    wdt_creds_path = REPO_ROOT / "数字化" / "钉钉" / "渠道日报机器人" / "wdt_credentials.json"
    with open(wdt_creds_path, encoding="utf-8") as f:
        creds = json.load(f)
    wdt = WdtClient(creds["sid"], creds["appkey"], creds["appsecret"])

    result = sync_product_catalog(wdt, settings.wdt_database)
    print(f"同步完成: 共 {result['total']} 个SKU, "
          f"新增/更新 {result['new']}, "
          f"标记删除 {result.get('deleted', 0)}, "
          f"系列未分类 {result['unmapped_series']}")

    summary = query_brand_series_summary(settings.wdt_database)
    print(f"\n品牌×系列分布:")
    for row in summary:
        print(f"  {row['brand_name'] or '(空)'} / {row['series_name'] or '(空)'}: {row['sku_count']} SKU")
