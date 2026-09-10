"""测试 WDT 商品目录拉取 + 品牌/系列分类（不写数据库）"""

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.wdt.client import WdtClient
from common.public_data.product_catalog import classify_series

WDT_CREDS = REPO_ROOT / "数字化" / "钉钉" / "渠道日报机器人" / "wdt_credentials.json"


def main():
    with open(WDT_CREDS, encoding="utf-8") as f:
        creds = json.load(f)
    wdt = WdtClient(creds["sid"], creds["appkey"], creds["appsecret"])

    print("正在拉取商品目录...")
    goods_list = wdt.call_paged("goods_query", {}, page_size=100, max_pages=200)
    print(f"共 {len(goods_list)} 个商品（货品）")

    specs = []
    for g in goods_list:
        brand = g.get("brand_name") or ""
        goods_name = g.get("goods_name") or ""
        for s in g.get("spec_list") or []:
            spec_no = s.get("spec_no") or ""
            if not spec_no:
                continue
            series = classify_series(brand, goods_name, s.get("spec_name"))
            specs.append({
                "spec_no": spec_no,
                "goods_name": goods_name,
                "spec_name": s.get("spec_name") or "",
                "brand_name": brand,
                "series_name": series,
            })

    print(f"共 {len(specs)} 个 SKU（规格）\n")

    # 品牌×系列汇总
    from collections import Counter, defaultdict
    brand_series = Counter()
    brand_series_skus = defaultdict(list)
    for s in specs:
        key = (s["brand_name"] or "(空)", s["series_name"])
        brand_series[key] += 1
        if len(brand_series_skus[key]) < 5:
            brand_series_skus[key].append(s)

    print("=" * 70)
    print("品牌 × 系列 分布")
    print("=" * 70)
    for (brand, series), count in sorted(brand_series.items(), key=lambda x: (-x[1], x[0])):
        print(f"\n  {brand} / {series}  ({count} SKU)")
        for s in brand_series_skus[(brand, series)]:
            print(f"    {s['spec_no']:20s}  {s['goods_name'][:30]:30s}  {s['spec_name'][:20]}")

    # 习酒专项
    print("\n" + "=" * 70)
    print("习酒 SKU 明细（全部）")
    print("=" * 70)
    xijiu = [s for s in specs if "习酒" in (s["brand_name"] or "") or "习酒" in s["goods_name"]]
    by_series = defaultdict(list)
    for s in xijiu:
        by_series[s["series_name"]].append(s)
    for series, items in sorted(by_series.items()):
        print(f"\n  [{series}] {len(items)} SKU")
        for s in items:
            print(f"    {s['spec_no']:20s}  {s['goods_name'][:35]:35s}  {s['spec_name'][:25]}")

    # 古韵/大坛专项
    print("\n" + "=" * 70)
    print("古韵 + 大坛 匹配结果")
    print("=" * 70)
    targets = [s for s in specs if s["series_name"] in ("古韵", "大坛")]
    if not targets:
        print("  ⚠️ 未匹配到任何古韵/大坛 SKU！请检查关键词")
    for s in targets:
        print(f"  {s['series_name']:4s}  {s['spec_no']:20s}  {s['goods_name'][:40]:40s}  {s['spec_name'][:25]}")
    print(f"\n  合计: 古韵 {sum(1 for s in targets if s['series_name']=='古韵')} SKU, "
          f"大坛 {sum(1 for s in targets if s['series_name']=='大坛')} SKU")


if __name__ == "__main__":
    main()
