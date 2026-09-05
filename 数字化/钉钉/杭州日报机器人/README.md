# 杭州销售日报提醒机器人 v2

## 四大机制（2026-09-04）

1. **@机器人报数，自动入表**：责任人在「线下事业部杭州日报群」发 `@提醒事项 12800`（报 0 也行），机器人自动写入其当日列并回复确认（hangzhou_listener.py 常驻监听）
2. **18:30 仅提醒未填人**：检查表格后只 @ 当日未填写的人，已填的不打扰
3. **20:00 催办**：对未填人发**应用内 DING** + 群里 @沈聪 知会
4. **每晚 00:05 组织同步**（org_sync.py）：拉取线下运营中心通讯录快照，对比档案检测入离职；杭州区变动自动更新催报名单+群通知，绍兴区（16人）/省外等变动仅记档案（绍兴群建立后可接入）；cron 任务「组织同步-线下运营中心」

判定口径：当日列**空白 = 未填**，**0 = 已填**；非工作日自动跳过（休息日历见 config.json，依据钉钉考勤）。

## 组织档案（线下运营中心，deptId 1050135497）
- **杭州催报区 24人**：杭中项目部(1049728636)/滨萧项目部(1049886665)/余杭项目部(1050078449)+杭州销售部直属(1050062512)，与日报表一一对应
- **绍兴区 16人**：绍兴销售部(1050416052)+绍兴项目部(1049663672)+诸暨项目部(1050408252)，对接群非杭州群，暂只建档案
- **其他 3人**：省外销售部(1049860586)/浙中南销售部(1049857611，空)/运营中心直属(1050135497)
- 别名：通讯录「陆芳琳」= 表内「陆芳林」（config.org.aliases）
- 档案存于 config.json `org.archives`，查看：`python org_sync.py --status`

## 定时任务（千问办公 cron）
- `5 0 * * *`「组织同步-线下运营中心」→ agent 拉3区域快照 + `org_sync.py --sync`
- `30 18 * * *`「杭州日报-1830未填提醒」→ `--remind`
- `0 20 * * *`「杭州日报-2000催办DING」→ `--check`

## 手动命令
```
%USERPROFILE%\miniconda3\python.exe org_sync.py --status              # 查看组织档案
%USERPROFILE%\miniconda3\python.exe hangzhou_reminder.py --status    # 查看填写状态
%USERPROFILE%\miniconda3\python.exe hangzhou_reminder.py --remind    # 手动跑18:30提醒
%USERPROFILE%\miniconda3\python.exe hangzhou_reminder.py --check     # 手动跑20:00催办
```

## 常驻监听（报数机器人）
- 双击 `register_autostart.bat`：注册开机自启（当前用户启动文件夹的 vbs）并立即后台启动
- **门禁**：仅表格责任人（config.members）可使用报数；非责任人 @机器人 会实时收到拒绝回复，事件记入日志
- **白名单动态生效**：每 5 分钟自动重载 config.json，配合每晚组织同步——新入职次日自动放行、离职自动收回权限
- **重复报数**：以最后一次为准（直接覆盖），回复中注明「已覆盖上次填报的 X」
- **报数回复**：日期+销量+本月累计/目标/完成比例+祝您下班愉快；日志含新旧值可回溯
- 所有交互实时反馈（Stream 长连接，秒级回复）
- 日志：logs/reminder_YYYYMM.log

## 合计维护（recalc_totals.py，每晚 00:05 随组织同步执行）
- 自动重算：全员「达成率」+ 4条合计行（杭中/滨萧/余杭/杭州总）的日列和达成率，只写有差异的行
- 「达成合计」列已于 2026-09-04 删除（数据由分组视图小计和报数回复提供）
- 数据体检：`python check_data.py`（校验数值合理性、达成率一致性、合计行正确性）

## ⚠️ DING 说明（2026-09-04 已解决，无需开放平台操作）
- 直连 API（`topapi/ding/create`）需要的 `qyapi_create_ding` 权限在开放平台搜不到（旧版/受限权限）
- **替代方案**：`dws ding message send` 服务端通道已验证可用（不依赖该权限）
- 架构：20:00 脚本自发群消息（@未填人+沈聪），并输出 `DING_CMD_START...END` 包裹的 dws 命令，由 cron agent 原样执行完成 DING
- Stream 模式已验证可连接，无需额外操作

## 技术要点
- 凭据复用企业应用「提醒事项」（与渠道日报机器人同一应用）
- 群消息：`POST /v1.0/robot/groupMessages/send`，msgKey=`sampleMarkdownDX`（msgParam 含 atUserIds）
- 写表：`PUT /v1.0/notable/bases/{baseId}/sheets/{tableId}/records`，body `{"records":[{"id","fields"}]}`，fields 按字段名索引
- 组织同步数据源：cron agent 用 `dws contact dept list-members --depts <ids>` 拉3区域快照存 org_input_*.json（应用直连通讯录 API 需 qyapi_get_department_list 权限，暂未申请）
- 换月建新表时更新 config.json：baseId/tableId/tableUrl/month/restDays
