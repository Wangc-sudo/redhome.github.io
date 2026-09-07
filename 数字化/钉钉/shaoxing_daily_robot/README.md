# 绍兴销售日报提醒机器人

## 机制

1. **@机器人报数，自动入表**：责任人在「线下绍兴日报群」发 `@提醒事项 12800`（报 0 也行），机器人自动写入其当日列并回复确认。
2. **18:30 仅提醒未填人**：检查表格后只 @ 当日未填写的人。
3. **20:00 催办**：对未填人发应用内 DING + 群里 @沈聪 知会。
4. **每晚 00:05 组织同步**：拉取绍兴区域通讯录快照，更新催报名单并发群通知。
5. **8:30 榜单播报**：发送部门维度完成率榜单。

## 手动命令

```
python shaoxing_reminder.py --status    # 查看填写状态
python shaoxing_reminder.py --remind    # 手动跑 18:30 提醒
python shaoxing_reminder.py --check     # 手动跑 20:00 催办
python org_sync.py --status             # 查看组织档案
python org_sync.py --sync               # 手动同步组织
python recalc_totals.py                 # 重算合计
python check_data.py                    # 数据体检
python leaderboard_report.py --send     # 发送榜单
```
