# 餐饮部门&体验部 日报表填写（渠道日报机器人）设计文档

> 渠道/部门：餐饮部门 & 体验部（统一一张部门下）
> 填写群（3 个）：君品雅院日报表填写、习水雅院日报填写群、万科&大莲花&团购日报群

## 背景

「线下事业部杭州日报群」「线下绍兴日报群」已分别运行销售日报提醒机器人（基于 `common/daily_robot` 公共引擎），具备 18:30 提醒、20:00 催办、@机器人报数、组织同步、合计重算、数据体检、8:30 榜单播报等能力。

现需为 **餐饮部门 & 体验部** 新增一个日报填写渠道，该渠道下挂 **3 个**钉钉填写群：

- **君品雅院日报表填写**（项目部：君品雅院）
- **习水雅院日报填写群**（项目部：习水雅院）
- **万科&大莲花&团购日报群**（项目部：万科、大莲花、团购；群号 `193925005896`）

三个群的人员均归属同一张部门「餐饮部门 & 体验部」，因此组织同步拉取的是**一张部门**的通讯录快照。与杭州/绍兴「单群单表」不同，本渠道是「**一个部门、三张子群、共用一张总表**」：同一张 AI 日报表里不同「项目部」行的数据，要分别提醒、分别催办、分别推送到对应群。这是本次设计的核心扩展点（多群分发 + 单部门组织同步）。

## 目标

1. 为餐饮部门&体验部提供日报填写能力，功能与杭州/绍兴机器人一致：
   - 工作日 18:30 仅 @ 本群未填写人提醒
   - 工作日 20:00 应用内 DING + 群 @ 知会
   - @机器人报数自动写入当日列
   - 每晚 00:05 组织同步 + 合计重算
   - 表格数据体检
   - 工作日 8:30 完成率榜单播报（按群分别播报）
2. 支持 **一个部门对应多个群**：每个群只负责自己项目的人，互不串扰（提醒/催办/榜单仅推本群）。
3. 组织同步基于**一张部门**快照：按表内「项目部」自动把人员路由到对应群。
4. 公共引擎做 **多群扩展**，同时保持杭州/绍兴「单群」路径完全向后兼容。
5. 不破坏杭州/绍兴机器人现有行为与 cron 路径。

## 非目标

- 不改杭州/绍兴目录名与脚本（避免现有 cron、自启脚本失效）。
- 不新建通用报表平台、不以网页托管榜单作为唯一出口（榜单 HTML 仍可生成本地文件）。
- 不修改钉钉应用「提醒事项」本身（复用同一 appKey/appSecret）。
- 本期不接入用友 T+ 排班（休息日历仍用 `restDays` + `rule` 兜底）。

## 架构

```
common/daily_robot/                 # 公共引擎（本次扩展为多群）
  __init__.py
  core.py          # 提醒/催办/组织同步/合计重算/数据体检/send_group（增加 groups 维度）
  leaderboard.py   # 榜单生成与播报（增加按 group 过滤）
  listener.py      # Stream 报数监听 Handler（白名单全部门并集）

digital-ops/数字化/钉钉/
  杭州日报机器人/          # 现有，不变
  shaoxing_daily_robot/   # 现有，不变
  餐饮体验部日报表填写/      # 新建目录（渠道/部门）
    daily_reminder.py     # 薄包装：循环 groups 调 do_remind/do_check
    daily_listener.py     # 薄包装：实例化 ReportHandler(config)
    org_sync.py           # 薄包装：拉一张部门快照 → 按项目部路由到各 group
    recalc_totals.py      # 薄包装：逐 group 重算合计
    check_data.py         # 薄包装：数据体检
    leaderboard_report.py # 薄包装：逐 group 播报榜单
    config.json           # 新建（含 groups 段）
    config.example.json   # 新建（模板）
    README.md             # 新建
    register_autostart.bat# 新建（监听器自启）
    logs/
```

> 目录名 `餐饮体验部日报表填写/` 对应「餐饮部门&体验部」渠道；如更想保留原词「君品雅院」，可改名，不影响其他设计。

### 数据模型（一张总表，按「项目部」分群）

新增一个钉钉 AI 表格（base），结构与杭州一致：

| 列 | 说明 |
|----|------|
| `责任人` | 姓名（主键，区分合计行） |
| `项目部` | 项目/渠道，取值 ∈ {君品雅院, 习水雅院, 万科, 大莲花, 团购, …} |
| `1日`…`31日` | 每日销量列 |
| `{month}月销量目标（万）` | 月度目标 |
| `达成率` | 自动重算 |

三个群按 `项目部` 取数：

| 群 | `项目部` 过滤 |
|----|--------------|
| 君品雅院日报表填写 | `== 君品雅院` |
| 习水雅院日报填写群 | `== 习水雅院` |
| 万科&大莲花&团购日报群 | `∈ {万科, 大莲花, 团购}` |

> 备选（非首选）：若三群数据完全独立、不愿共用一张表，可按群拆成三张 base/table，每组独立 `base` 配置。设计以「共用一张总表 + `项目部` 过滤」为主，扩展点均在引擎 `projects` 过滤参数，切换成本低。

## 配置改造

### 新建 `餐饮体验部日报表填写/config.example.json`

```json
{
  "_说明": "餐饮部门&体验部 日报表填写渠道配置。一个部门挂三个填写群，共用一张总表，按项目部(project)分群。组织同步拉一张部门快照，按项目部路由到各群。",
  "dingtalk": {
    "appKey": "dingg85lqvifeny3ywfi",
    "appSecret": "<同提醒事项应用，生产建议用 DINGTALK_APP_SECRET 环境变量>",
    "operatorId": "x6sfcPCiiJqIDWuQva8GfFQiEiE"
  },
  "base": {
    "baseId": "<待填：餐饮体验部日报表 baseId>",
    "tableId": "<待填：各项目每日总销售 tableId>",
    "tableUrl": "<待填：表格访问地址>"
  },
  "calendar": {
    "month": 9,
    "source": "local",
    "restDays": [6, 13, 19, 25, 26, 27],
    "rule": { "year": 2026, "bigRestSaturdays": [19], "holidays": [25,26,27], "makeupWorkdays": [20] },
    "_说明": "与杭州同口径，接 T+ 前用 restDays 兜底"
  },
  "org": {
    "_说明": "一张部门（餐饮部门&体验部）快照，按表内项目部路由到 groups",
    "rootDeptId": "<待填：餐饮部门&体验部 部门ID>",
    "aliases": { "通讯录实名": "表内用名" },
    "depts": { "canyin": ["<餐饮部门&体验部 部门ID>"] },
    "archives": { "canyin": { "<姓名>": "<userId>" } },
    "lastSync": ""
  },
  "groups": [
    {
      "key": "junpin",
      "name": "君品雅院日报表填写",
      "projects": ["君品雅院"],
      "robot": { "mode": "groupSend", "robotCode": "dingg85lqvifeny3ywfi", "openConversationId": "<待填>", "groupName": "君品雅院日报表填写" },
      "groupNumber": "<待填>",
      "members": { "<姓名>": "<userId>" },
      "ccUsers": { "<知会人>": "<userId>" },
      "broadcastExclude": [],
      "totalPrefixes": ["君品雅院"]
    },
    {
      "key": "xishui",
      "name": "习水雅院日报填写群",
      "projects": ["习水雅院"],
      "robot": { "mode": "groupSend", "robotCode": "dingg85lqvifeny3ywfi", "openConversationId": "<待填>", "groupName": "习水雅院日报填写群" },
      "groupNumber": "<待填>",
      "members": { "<姓名>": "<userId>" },
      "ccUsers": { "<知会人>": "<userId>" },
      "broadcastExclude": [],
      "totalPrefixes": ["习水雅院"]
    },
    {
      "key": "wanke",
      "name": "万科&大莲花&团购日报群",
      "projects": ["万科", "大莲花", "团购"],
      "robot": { "mode": "groupSend", "robotCode": "dingg85lqvifeny3ywfi", "openConversationId": "<待填：机器人入群后从 Stream/事件回调取 conversationId>", "groupName": "万科&大莲花&团购日报群" },
      "groupNumber": "193925005896",
      "members": { "<姓名>": "<userId>" },
      "ccUsers": { "<知会人>": "<userId>" },
      "broadcastExclude": [],
      "totalPrefixes": ["万科", "大莲花", "团购"]
    }
  ],
  "verify": {
    "openConversationId": "<待填：功能验证群>",
    "groupName": "功能验证群",
    "_说明": "TEST_MODE=1 时所有群消息重定向到本群"
  }
}
```

### 兼容性说明（重要）

`common/daily_robot/core.py` 现有函数均从 `config["robot"]` / `config["members"]` / `config["ccUsers"]` / `config["region"]` 读取单群信息。多群扩展采用 **「兼容层」** 策略：

- 若 config 含 `groups` 数组 → 按多群模式逐 group 执行（本渠道）。
- 若 config 不含 `groups` → 自动把顶层 `robot`/`members`/`ccUsers`/`region` 包装成「一个隐式 group」，行为与杭州/绍兴完全一致。

实现上在 `core.py` 新增辅助 `iter_groups(config)`，统一产出 `group` 字典（含 `robot`/`members`/`ccUsers`/`projects`/`totalPrefixes`/`broadcastExclude`）。杭州/绍兴代码零改动。

## 公共核心模块改造（`common/daily_robot`）

### `core.py`

| 函数 | 改造点 |
|------|--------|
| `iter_groups(config)` | **新增**：返回 group 列表（多群取 `groups`，单群包装顶层字段） |
| `fetch_status(config, projects=None)` | 新增 `projects` 过滤：仅统计 `项目部 ∈ projects` 的责任人行 |
| `send_group(config, robot_target, title, text, at_ids=None)` | 第二参数改为**具体 robot 目标**（`resolve_target(group["robot"])`），不再硬编码 `config["robot"]`；TEST_MODE 下用 `verify` 群 |
| `do_remind(config, state, now, day, group)` | 增加 `group` 参数：状态/成员/推送均限定本群 |
| `do_check(config, state, now, day, group)` | 同上；DING 仅发本群 `members` |
| `org_sync(config, inputs, department_key)` | 拉**一张部门**快照，按表内 `项目部` 将人员路由到 `groups[].projects` 匹配的群，更新各群 `members` |
| `recalc_totals(config, group=None)` | 增加 `group`：仅重算 `group.projects` 行与 `group.totalPrefixes` 合计行；无参时全表（兼容） |
| `check_data(config, projects=None)` | 增加 `projects` 过滤 |

### `listener.py`（报数监听）

- `ReportHandler(config)` 加载时构建 **全部门白名单**（`所有 groups 的 members 并集`）→ `uid2name` / `name2rid`。
- 收到 `@提醒事项 数字`：按 sender 找到姓名 → 定位其所属 group（由 `members` 反查）→ 写表、回复累计完成率。
- 同一张总表，不同群的人写不同 `项目部` 行，互不干扰。
- 每 5 分钟重载 config（配合组织同步动态生效）。

### `leaderboard.py`

- `collect(config, include_today=False, projects=None)`、`build_bc_markdown(config, url=None, projects=None)`、`build_html(...)` 增加 `projects` 过滤，使每个群只播报自己项目的完成率榜。

## 数据流

### 日常提醒/催办（多群分发）

```
cron (18:30 / 20:00)
  → daily_reminder.py --remind / --check
    → 读 config.json
    → 循环 config.groups:
        君品雅院日报表填写   → do_remind/do_check(projects=["君品雅院"]) → 仅推本群
        习水雅院日报填写群   → do_remind/do_check(projects=["习水雅院"]) → 仅推本群
        万科&大莲花&团购日报群 → do_remind/do_check(projects=["万科","大莲花","团购"]) → 仅推本群
```

### 组织同步与合计重算（一张部门 → 多群路由）

```
cron 00:05
  → org_sync.py --sync
    → 用 dws contact 拉「餐饮部门&体验部」一张部门快照 → org_input_canyin.json
    → common.daily_robot.core.org_sync(config, inputs, department_key="canyin")
      → 读总表 责任人+项目部
      → 按项目部把人员路由到匹配 projects 的群，更新各群 members
      → 群通知各群人员变动
  → recalc_totals.py
    → 循环 config.groups:
        common.daily_robot.core.recalc_totals(config, group=g)   # 仅本群合计行
```

### 榜单播报（按群）

```
cron 08:30
  → leaderboard_report.py --send
    → 循环 config.groups:
        build_bc_markdown(config, url, projects=g.projects)
        send_group(config, g.robot, "销售完成率榜", md)
```

### @机器人报数（跨群同一张表）

```
用户 @提醒事项 12345（任一填写群）
  → daily_listener.py (Stream)
    → ReportHandler(config)
      → 全部门白名单校验 → 定位所属 group/行 → 写表 → 回复确认
```

## 定时任务（餐饮部门&体验部渠道）

```
5 0 * * *   cd 餐饮体验部日报表填写 && python org_sync.py --sync && python recalc_totals.py
30 8 * * *  cd 餐饮体验部日报表填写 && python leaderboard_report.py --send
30 18 * * * cd 餐饮体验部日报表填写 && python daily_reminder.py --remind
0 20 * * *  cd 餐饮体验部日报表填写 && python daily_reminder.py --check
```

杭州/绍兴 cron 任务不变。监听器 `daily_listener.py` 需常驻（参考 `register_autostart.bat` 注册开机自启）。

## 错误处理

- 脚本顶层捕获异常并写 `logs/reminder_YYYYMM.log`，避免 cron 静默退出。
- `listener.py` 单条消息异常不影响后续消息。
- `org_sync` 缺少快照或 API 失败时不覆盖档案。
- 月份不符 / 休息日自动跳过并记录。
- `send_group` 在 TEST_MODE=1 下统一重定向到 `verify` 群，禁止发正式群（与杭州 `test_group.resolve_target` 机制一致）。

## 测试与验证

1. 为 `common/daily_robot/core.py` 多群函数编写单元测试（mock `DingTalkClient`、构造多 group 假配置）：
   - `iter_groups` 兼容单群/多群；
   - `fetch_status(projects=...)` 过滤正确；
   - `do_remind/do_check` 仅 @ 本群成员、仅推本群 robot；
   - `org_sync` 一张部门快照按项目部正确路由到三群。
2. `python -m compileall` 通过：`common/daily_robot/`、`数字化/钉钉/餐饮体验部日报表填写/`、`tests/`。
3. `TEST_MODE=1` 下 `--status` / `--remind` / `--check` 重定向到功能验证群，确认三群各自名单与表结构正确、互不串扰。
4. 组织同步 `--status` 查看部门档案，再用 `--sync` 测试一次（验证路由）。
5. 榜单 `--send`（TEST_MODE）确认三群分别播报各自项目完成率。

## 实施清单（落地步骤）

- [ ] 在钉钉新建 AI 表格（base + 各项目每日总销售表），建 `责任人`/`项目部`/日列/目标/达成率列
- [ ] 取得三个群的 `openConversationId`（**获取方式**：把「提醒事项」机器人拉进群，群里发任意一条消息/触发一次 `@提醒事项`，从 Stream/事件回调的 `conversationId` 字段取出；群号仅作人工核对——万科&大莲花&团购日报群群号 `193925005896`；其余两群群号待填）
- [ ] 取得「餐饮部门&体验部」部门 ID，用 `dws contact dept list-members` 拉快照 → `org_input_canyin.json`
- [ ] 新建 `餐饮体验部日报表填写/` 目录与全部薄包装脚本 + `config.example.json`
- [ ] 改造 `common/daily_robot` 增加多群维度（`iter_groups`/projects 过滤/`send_group` 接收 robot 目标/单部门多群路由 `org_sync`）
- [ ] 补充单元测试
- [ ] `TEST_MODE=1` 全流程验证
- [ ] 配置生产 cron + 注册监听器自启
- [ ] 换月时更新 `base.tableId` / `calendar.month` / `restDays`

## 迁移影响

- 杭州/绍兴脚本与目录完全不变（兼容层保证）。
- 新增的 `餐饮体验部日报表填写` 目录与配置独立，互不影响。
- `common/daily_robot` 仅做向后兼容的签名扩展（新增可选参数），不破坏既有调用。
