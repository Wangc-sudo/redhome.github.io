# 餐饮部门&体验部 日报表填写机器人

一个部门（餐饮部门&体验部）挂 **3 个填写群**，共用一张 AI 日报总表，按表内「项目部」分群：

| 群 | 项目部过滤 |
|----|-----------|
| 君品雅院日报表填写 | `君品雅院` |
| 习水雅院日报填写群 | `习水雅院` |
| 万科&大莲花&团购日报群（群号 193925005896） | `万科` / `大莲花` / `团购` |

所有脚本为薄包装，逻辑一律在 `common/daily_robot`（多群兼容层，杭州/绍兴零改动）。

## 脚本

| 脚本 | 用途 | 定时 |
|------|------|------|
| `daily_reminder.py --remind` | 18:30 逐群 @ 本群未填写人 | `30 18 * * *` |
| `daily_reminder.py --check` | 20:00 逐群 DING + 群 @ 知会人 | `0 20 * * *` |
| `daily_reminder.py --status` | 查看三群名单与当日填写状态（base 未配置时仅显示配置名单） | 手动 |
| `daily_listener.py` | 常驻监听 @提醒事项 报数（三群共用，白名单为全部门并集） | 常驻（`register_autostart.bat` 注册开机自启） |
| `org_sync.py --sync` | 拉一张部门快照，按项目部路由更新各群 members | `5 0 * * *` |
| `org_sync.py --status` | 查看部门档案与各群催报名单 | 手动 |
| `recalc_totals.py` | 逐群重算本群合计行与达成率 | `5 0 * * *`（org_sync 之后） |
| `check_data.py` | 逐群数据体检 | 手动 |
| `leaderboard_report.py --send` | 8:30 逐群播报本群完成率榜 | `30 8 * * *` |
| `leaderboard_report.py` | 生成每群 `leaderboard_<key>.html` | 手动 |

## 上线前需业务/钉钉提供的 4 项

1. **baseId / tableId**：钉钉 AI 表格「餐饮体验部日报表」的 baseId 与「各项目每日总销售」tableId（含 tableUrl）。表结构同杭州：`责任人` / `项目部` / `1日`…`31日` / `{month}月销量目标（万）` / `达成率`。
2. **三个群的 openConversationId**：把「提醒事项」机器人拉进群，群里发任意消息或 @机器人 一次，从 Stream/事件回调的 `conversationId` 字段取出（群号仅作人工核对）。
3. **「餐饮部门&体验部」部门 ID**：用于 `org.rootDeptId` / `org.depts.canyin`，再用 `dws contact` 拉快照生成 `org_input_canyin.json`。
4. **功能验证群 openConversationId**（`verify` 段）：TEST_MODE=1 时所有群消息统一重定向到该群，禁止发正式群。

以上在 `config.json` 中统一以 `<待填>` 标注，未填齐前 `--status` 仍可离线查看三群配置名单。

## 安全约束

- 所有验证必须 `TEST_MODE=1`（重定向功能验证群），严禁直接向正式群发消息。
- 换月时更新 `base.tableId` / `calendar.month` / `calendar.restDays`。
- `appSecret` 生产环境建议用 `DINGTALK_APP_SECRET` 环境变量注入。
