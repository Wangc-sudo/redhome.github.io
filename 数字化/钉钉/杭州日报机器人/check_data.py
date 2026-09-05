# -*- coding: utf-8 -*-
"""表格数据体检：逐行检查数值合理性"""
import sys

sys.path.insert(0, r"C:\Users\9255589586e631a2\.qwenworkcn\workspace\mtmi1epasnu4mygi\outputs\hangzhou-report-robot")
from hangzhou_reminder import DingTalk, CONFIG

import re

client = DingTalk(CONFIG["dingtalk"])
base = CONFIG["base"]
records = client.list_records(base["baseId"], base["tableId"])

# 工作日列（现有24列）
workdays = [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 14, 15, 16, 17, 18, 20, 21, 22, 23, 24, 28, 29, 30]
problems = []

for rec in records:
    f = rec.get("fields") or {}
    name = f.get("责任人")
    if not name:
        problems.append(f"[空责任人行] id={rec.get('id')} keys={list(f.keys())[:5]}")
        continue
    if "合计" in str(name):
        continue  # 合计行单独核

    # 1) 检查残留的休息日列（6/13/19/25/26/27 应该已删除）
    for rest in [6, 13, 19, 25, 26, 27]:
        if f"{rest}日" in f:
            problems.append(f"[{name}] 残留休息日列 {rest}日 = {f[f'{rest}日']}")

    # 2) 汇总各日数据
    vals = {}
    for d in workdays:
        v = f.get(f"{d}日")
        if v is not None and str(v).strip() != "":
            try:
                vals[d] = float(str(v).replace(",", ""))
            except ValueError:
                problems.append(f"[{name}] {d}日 非数值: {v!r}")

    # 3) 已填列是否连续（今天4日，理应 1-4 都有值或空,5日及以后应该为空/0）
    filled_days = sorted(vals.keys())
    today = 4
    # 未来日期不该有大数（5日是周六上班,可以填）
    suspicious_future = {d: v for d, v in vals.items() if d > today and v > 0}
    if suspicious_future:
        problems.append(f"[{name}] 未来日期已有大额数据: {suspicious_future}")

    # 4) 达成率 vs 手算（「达成合计」列已删除，不再检查）
    day_sum = sum(vals.values())
    try:
        rate = float(str(f.get("达成率")))
        target = float(str(f.get("9月销量目标（万）")).replace(",", ""))
        if target > 0 and abs(rate - day_sum / target) > 0.005:
            problems.append(f"[{name}] 达成率({rate:.4f}) 与累计/目标({day_sum/target:.4f}) 不符")
    except (TypeError, ValueError):
        pass

    print(f"{name}: 已填{len(vals)}列 累计={int(day_sum)}")

print()
print("=" * 60)
if problems:
    print(f"发现 {len(problems)} 个问题:")
    for p in problems:
        print(" -", p)
else:
    print("数据体检通过，无问题")

# 合计行核对
print()
print("=" * 60)
by_dept = {}
for rec in records:
    f = rec.get("fields") or {}
    name = str(f.get("责任人") or "")
    if "合计" not in name:
        dept = str(f.get("项目部") or "")
        by_dept.setdefault(dept, []).append(f)

for dept in ["杭中", "滨萧", "余杭"]:
    members = by_dept.get(dept, [])
    if not members:
        continue
    for d in workdays[:6]:  # 检查前几天
        col = f"{d}日"
        s = 0.0
        cnt = 0
        for mf in members:
            v = mf.get(col)
            if v is not None and str(v).strip() != "":
                try:
                    s += float(str(v))
                    cnt += 1
                except ValueError:
                    pass
        # 找合计行的值
        tot = None
        for rec in records:
            f = rec.get("fields") or {}
            if str(f.get("项目部") or "").startswith(dept) and "合计" in str(f.get("责任人") or ""):
                tot = f.get(col)
                break
        if tot is not None:
            try:
                tot = float(str(tot))
                if abs(tot - s) > 1:
                    print(f"[{dept}合计行] {col}: 表值={int(tot)} 实际成员和={int(s)} (差{int(tot-s)})")
            except ValueError:
                pass
print("合计行核对完成")
