# -*- coding: utf-8 -*-
"""
合计自动维护：重算全表的 达成率 + 4条合计行日列
由每晚 00:05 组织同步任务在 org_sync 之后调用（或手动 python recalc_totals.py）

口径：
- 个人行：达成率 = 全部日列求和 / 9月销量目标（万）
- 合计行：各日列 = 同项目部成员该日之和；达成率 = 累计/目标合计
- 「达成合计」列已于 2026-09-04 删除（信息由分组视图小计/机器人回复提供）
- 只在有差异时写入，减少 API 调用
"""
import sys

sys.path.insert(0, r"C:\Users\9255589586e631a2\.qwenworkcn\workspace\mtmi1epasnu4mygi\outputs\hangzhou-report-robot")
from hangzhou_reminder import DingTalk, CONFIG

client = DingTalk(CONFIG["dingtalk"])
base = CONFIG["base"]

CAL = CONFIG["calendar"]
MONTH = CAL["month"]
WORKDAYS = [d for d in range(1, 31)
            if d not in CAL["restDays"]]  # 9月最多30日
DAY_COLS = [f"{d}日" for d in WORKDAYS]
TARGET_COL = f"{MONTH}月销量目标（万）"


def parse_num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def recalc():
    records = client.list_records(base["baseId"], base["tableId"])
    updates = []
    n_fixed = 0

    # --- 个人行 ---
    members_by_dept = {}
    for rec in records:
        f = rec.get("fields") or {}
        name = str(f.get("责任人") or "")
        if not name or "合计" in name:
            continue
        dept = str(f.get("项目部") or "?")
        members_by_dept.setdefault(dept, []).append(f)

        day_sum = sum(parse_num(f.get(c)) or 0 for c in DAY_COLS)
        target = parse_num(f.get(TARGET_COL))
        new_fields = {}
        # 「达成合计」列已删除（用户2026-09-04操作），只维护达成率
        if target:
            new_rate = day_sum / target
            old_rate = parse_num(f.get("达成率"))
            if old_rate is None or abs(old_rate - new_rate) > 0.0005:
                new_fields["达成率"] = repr(new_rate)
        if new_fields:
            updates.append({"id": rec["id"], "fields": new_fields})
            n_fixed += 1

    # --- 合计行 ---
    total_rows = {}
    for rec in records:
        f = rec.get("fields") or {}
        name = str(f.get("责任人") or "")
        if "合计" not in name or not name:
            continue
        dept = str(f.get("项目部") or "")
        for prefix in ["杭中", "滨萧", "余杭"]:
            if prefix in dept or prefix in name:
                total_rows[prefix] = (rec["id"], f)
                break
        else:
            if "杭州" in name or "杭州" in dept:
                total_rows["ALL"] = (rec["id"], f)

    def fix_total_row(rid, tf, members):
        nonlocal n_fixed
        new_fields = {}
        for c in DAY_COLS:
            s = sum(parse_num(mf.get(c)) or 0 for mf in members)
            old = parse_num(tf.get(c))
            if old is None or abs(old - s) > 0.5:
                new_fields[c] = str(int(s)) if s == int(s) else str(s)
        # 「达成合计」列已删除，只维护日列与达成率
        total_all = sum(parse_num(mf.get(c)) or 0 for mf in members for c in DAY_COLS)
        target_sum = sum(parse_num(mf.get(TARGET_COL)) or 0 for mf in members)
        if target_sum:
            new_rate = total_all / target_sum
            old_r = parse_num(tf.get("达成率"))
            if old_r is None or abs(old_r - new_rate) > 0.0005:
                new_fields["达成率"] = repr(new_rate)
        if new_fields:
            updates.append({"id": rid, "fields": new_fields})
            n_fixed += 1

    for dept in ["杭中", "滨萧", "余杭"]:
        if dept in total_rows and members_by_dept.get(dept):
            rid, tf = total_rows[dept]
            fix_total_row(rid, tf, members_by_dept[dept])

    if "ALL" in total_rows:
        all_members = []
        for d in ["杭中", "滨萧", "余杭", "杭州运营总监"]:
            all_members.extend(members_by_dept.get(d, []))
        rid, tf = total_rows["ALL"]
        fix_total_row(rid, tf, all_members)

    if updates:
        for i in range(0, len(updates), 100):
            client.update_records(base["baseId"], base["tableId"], updates[i:i + 100])
    print(f"RECALC_DONE: 修正 {n_fixed} 行" if n_fixed else "RECALC_DONE: 全部一致无需修正")
    return n_fixed


if __name__ == "__main__":
    recalc()
