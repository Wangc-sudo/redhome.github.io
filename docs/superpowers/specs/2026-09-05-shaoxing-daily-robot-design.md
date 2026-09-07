# 绍兴日报机器人设计文档

## 背景

「线下事业部杭州日报群」已运行杭州销售日报机器人（以下简称「杭州机器人」），具备 18:30 提醒、20:00 催办、@机器人报数、组织同步、合计重算、数据体检、8:30 榜单播报等功能。现需为「线下绍兴日报群」建设能力完全一致的机器人，但使用独立的 AI 表格与绍兴组织人员。

## 目标

1. 为「线下绍兴日报群」提供与杭州机器人一致的功能：
   - 工作日 18:30 仅 @ 未填写人提醒
   - 工作日 20:00 应用内 DING + 群 @ 知会
   - @机器人报数自动写入当日列
   - 每晚 00:05 组织同步 + 合计重算
   - 表格数据体检
   - 工作日 8:30 完成率榜单播报
2. 通过公共核心模块复用代码，避免杭州/绍兴两份逻辑重复维护。
3. 不破坏杭州机器人现有行为与 cron 路径。

## 非目标

- 不改杭州机器人目录名（避免现有 cron、快捷方式、自启脚本失效）。
- 不新增通用报表平台、不以网页形式提供榜单托管。
- 不修改钉钉应用「提醒事项」本身。

## 架构

```
common/daily_robot/
  __init__.py
  core.py          # 提醒、催办、组织同步、合计重算、数据体检
  leaderboard.py   # 榜单生成与播报
  listener.py      # Stream 报数监听 Handler

digital-ops/数字化/钉钉/
  杭州日报机器人/          # 现有目录，脚本改为薄包装
    hangzhou_reminder.py
    hangzhou_listener.py
    org_sync.py
    recalc_totals.py
    check_data.py
    leaderboard_report.py
    config.json
    config.example.json
  shaoxing_daily_robot/    # 新建目录
    shaoxing_reminder.py
    shaoxing_listener.py
    org_sync.py
    recalc_totals.py
    check_data.py
    leaderboard_report.py
    config.json
    config.example.json
    README.md
    register_autostart.bat
    logs/
```

## 配置改造

两区域 `config.json` 新增 `region` 段，将原本硬编码在杭州脚本中的区域常量收归配置：

```json
{
  "region": {
    "name": "shaoxing",
    "displayName": "绍兴",
    "activeOrgRegion": "shaoxing",
    "deptOrder": ["绍兴", "诸暨", "绍兴运营总监"],
    "deptLabel": {"绍兴运营总监": "运营总监"},
    "broadcastExclude": [],
    "totalPrefixes": ["绍兴", "诸暨"],
    "allDeptNames": ["绍兴", "诸暨", "绍兴运营总监"]
  }
}
```

杭州 `region` 段对应设置为：

```json
{
  "region": {
    "name": "hangzhou",
    "displayName": "杭州",
    "activeOrgRegion": "hangzhou",
    "deptOrder": ["杭中", "滨萧", "余杭"],
    "deptLabel": {"杭州运营总监": "运营总监"},
    "broadcastExclude": ["余发兴"],
    "totalPrefixes": ["杭中", "滨萧", "余杭"],
    "allDeptNames": ["杭中", "滨萧", "余杭", "杭州运营总监"]
  }
}
```

`robot` 配置统一使用 `common.test_group.resolve_target` 解析，支持 `TEST_MODE=1` 切换到共享测试群。注意杭州现有配置需显式加 `"mode": "groupSend"`（原配置无 mode 字段，会被默认当成 webhook）：

- 杭州生产：`mode=groupSend`，`robotCode` + `openConversationId`
- 绍兴生产：`mode=groupSend`，复用同一 `robotCode`，`openConversationId=cidO7oMaE/xLi8uyyHj4t2tgw==`

## 公共核心模块

### `common/daily_robot/core.py`

| 函数 | 说明 |
|------|------|
| `today_info(calendar)` | 返回 `(now, day, error)`；非工作日/月份不符返回空 day |
| `fetch_status(config)` | 读表统计已填/未填 |
| `do_remind(config, state)` | 18:30 群提醒 |
| `do_check(config, state)` | 20:00 催办 + DING 命令输出 |
| `org_sync(config, inputs, active_region)` | 对比快照更新档案与 members |
| `recalc_totals(config)` | 重算达成率与合计行 |
| `check_data(config)` | 数据体检 |
| `send_group(config, title, text, at_ids=None)` | 统一群发送入口 |

### `common/daily_robot/leaderboard.py`

| 函数 | 说明 |
|------|------|
| `collect(config, include_today=False)` | 汇总已过工作日数据 |
| `build_bc_markdown(config, url=None)` | 群播报 Markdown（部门维度） |
| `build_html(config, ...)` | 完整 HTML 榜单 |

### `common/daily_robot/listener.py`

- 提供 `ReportHandler(config)`，供杭州/绍兴 listener 实例化。
- 负责 Stream 连接、白名单缓存、数字解析、写表、回复累计完成率。

## 数据流

### 日常提醒催办

```
cron (18:30 / 20:00)
  → shaoxing_reminder.py --remind/--check
    → 读 config.json
    → common.daily_robot.core.do_remind / do_check
      → DingTalkClient → groupMessages/send
```

### 组织同步与合计重算

```
cron 00:05
  → org_sync.py --sync
    → 读 org_input_shaoxing.json
    → common.daily_robot.core.org_sync
      → 更新 config.org.archives.shaoxing / config.members
      → 群通知绍兴变动
  → recalc_totals.py
    → common.daily_robot.core.recalc_totals
      → 重算本表达成率与合计行
```

### 榜单播报

```
cron 08:30
  → leaderboard_report.py --send
    → common.daily_robot.leaderboard.build_bc_markdown
    → common.daily_robot.core.send_group
```

### @机器人报数

```
用户 @提醒事项 12345
  → shaoxing_listener.py (Stream)
    → common.daily_robot.listener.ReportHandler
      → 校验白名单 → 写表 → 回复确认
```

## 定时任务（绍兴）

```
5 0 * * *   cd shaoxing_daily_robot && python org_sync.py --sync && python recalc_totals.py
30 8 * * *  cd shaoxing_daily_robot && python leaderboard_report.py --send
30 18 * * * cd shaoxing_daily_robot && python shaoxing_reminder.py --remind
0 20 * * *  cd shaoxing_daily_robot && python shaoxing_reminder.py --check
```

杭州 cron 任务不变，脚本内部改调公共模块。

## 错误处理

- 脚本顶层捕获异常并写日志，避免 cron 失败时静默退出。
- `listener.py` 单条消息异常不影响后续消息。
- `org_sync` 缺少快照或 API 失败时不覆盖档案。
- 月份不匹配、休息日自动跳过并记录。

## 测试与验证

1. 为 `common/daily_robot/core.py` 编写单元测试（mock DingTalkClient、假配置）。
2. `python -m compileall` 通过：
   - `common/daily_robot/`
   - `数字化/钉钉/杭州日报机器人/`
   - `数字化/钉钉/shaoxing_daily_robot/`
   - `tests/`
3. 杭州 `--status` 干跑，行为与重构前一致。
4. 绍兴 `--status` 干跑，确认 16 人名单与表结构正确。
5. `TEST_MODE=1` 下绍兴 `--remind` / `--check` 重定向到共享测试群（功能验证群）。
6. 组织同步 `--status` 查看档案，再用 `--sync` 测试一次。

## 迁移影响

- 杭州脚本改为调用公共模块，功能不变。
- 杭州 `config.json` 需补 `region` 段，并把 `robot` 改为显式 `"mode": "groupSend"`。
- `org_input_shaoxing.json` 从杭州目录复制/迁移到 `shaoxing_daily_robot/` 目录，绍兴 cron 在该目录下拉取快照。
- 绍兴使用新建的 `shaoxing_daily_robot` 目录与配置，不影响杭州。
