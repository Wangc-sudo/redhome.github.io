#!/usr/bin/env python3
"""
表结构发现工具
==============
部署到 ECS 后先跑这个：列出 Base 下全部数据表与指定表的字段，
把结果填回 config.json 的 base.dailySalesTable 和 fields。

用法:
  python discover_tables.py                # 列出全部表名
  python discover_tables.py -t 表名         # 列出某表的字段
  python discover_tables.py -t 表名 --sample 5   # 额外看 5 条样例记录
"""
import argparse
import json
import sys
from pathlib import Path

from dingtalk_client import DingTalkClient, DingTalkError


def load_config():
    cfg_path = Path(__file__).parent / "config.json"
    if not cfg_path.exists():
        print("未找到 config.json，请先复制 config.example.json 为 config.json 并填写凭据")
        sys.exit(1)
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def flat_value(v, depth=0):
    """notable 记录值可能是 {text:...}/{number:...}/{date:...} 等结构，取展示值"""
    if depth > 3 or v is None:
        return v
    if isinstance(v, dict):
        for k in ("text", "value", "number", "date", "name", "title"):
            if k in v:
                return flat_value(v[k], depth + 1)
        if "valueList" in v or "values" in v:
            return flat_value(v.get("valueList") or v.get("values"), depth + 1)
        return json.dumps(v, ensure_ascii=False)[:40]
    if isinstance(v, list):
        return ", ".join(str(flat_value(x, depth + 1)) for x in v[:3])
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-t", "--table", help="表名（可选），指定则列出该表字段")
    ap.add_argument("--sample", type=int, default=0, help="附带查看 N 条样例记录")
    args = ap.parse_args()

    cfg = load_config()
    dt = cfg["dingtalk"]
    base_id = cfg["base"]["baseId"]
    client = DingTalkClient(dt["appKey"], dt["appSecret"], dt["operatorId"])

    try:
        sheets = client.list_sheets(base_id)
    except DingTalkError as e:
        print(f"获取表清单失败: {e}")
        sys.exit(2)

    print(f"Base {base_id} 共 {len(sheets)} 张表：\n")
    sheet_id = None
    for i, s in enumerate(sheets, 1):
        name = s.get("name") or s.get("title") or "?"
        sid = s.get("id") or s.get("sheetId") or ""
        print(f"{i:3d}. {name}  (id={sid})")
        if args.table and name == args.table:
            sheet_id = sid or name

    if not args.table:
        print("\n把目标表名填入 config.json 的 base.dailySalesTable 即可。")
        return

    if sheet_id is None:
        print(f"\n未找到表「{args.table}」，请核对表名。")
        sys.exit(3)

    print(f"\n=== 表「{args.table}」字段 ===")
    try:
        fields = client.list_fields(base_id, sheet_id)
        for f in fields:
            print(f"  - {f.get('name')}  (type={f.get('type')})")
    except DingTalkError as e:
        print(f"获取字段失败: {e}")

    if args.sample > 0:
        print(f"\n=== 样例记录（前 {args.sample} 条）===")
        try:
            records = client.list_records(base_id, sheet_id)[: args.sample]
            for r in records:
                fields_map = r.get("fields") or r.get("values") or {}
                if isinstance(fields_map, list):  # values 数组形态
                    print("  ", [flat_value(x) for x in fields_map])
                else:
                    print("  ", {k: flat_value(v) for k, v in list(fields_map.items())[:12]})
        except DingTalkError as e:
            print(f"获取记录失败: {e}")


if __name__ == "__main__":
    main()
