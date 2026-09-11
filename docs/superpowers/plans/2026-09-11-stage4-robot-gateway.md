# 阶段 4：钉钉网关 + 业务线容器化 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 业务线机器人停止直连钉钉 AI 表、停止持有钉钉凭据：`robot` 只读 `mart_ops`（+ Nacos 配置）算「该发什么」，`dingtalk-gateway` 独占钉钉交互（投递 + Stream 报数落库）。

**Architecture:** 线间唯一边界是 `mart_ops`。robot 的产出（提醒/催办/榜单消息）写入 `robot_outbox`，gateway 轮询投递——robot 的世界里没有 HTTP、没有钉钉 SDK、没有 Stream。报数反向链路（Stream 收消息 → 落 `mart_ops`）同容器承载。

**投递契约（spec §9 开放点 → 已定）**：`mart_ops.robot_outbox` **表轮询**，不选 gateway 内部 API。理由：
1. robot 保持「世界 = mart_ops + Nacos」，不引入新的协议/鉴权/可用性耦合；
2. outbox 行即审计（谁在何时该发什么、投递状态、重试次数），控制面可直接读；
3. **幂等天然由唯一键承载**：`dedupe_key = region:kind:business_date`，`INSERT IGNORE` 语义——现行 `stateFile` 的去重职责整体退役，机器人变为无状态容器；
4. gateway 崩溃/重启不丢消息（pending 行仍在），重试上限在 DB 层可见。

**Tech Stack:** Python 3.12、标准库 `unittest`、PyMySQL、MySQL 8.4、Docker Compose。

**Safety constraints:**
- `robot` 容器不得挂钉钉凭据、不得 import 钉钉 client、不得访问 `raw_*`（Compose 合约测试把守，同 extract-mart 先例）。
- `dingtalk-gateway` 是唯一持钉钉凭据的业务相关容器；outbox 的 `last_error` 只写错误码，不写响应体。
- 消息文案与现行机器人**逐字一致**（含 emoji 与@人语义），切换不得改变群内观感。

---

## Task 1: 投递契约与 outbox 表

- [x] **Step 1: 决策记录。** spec §9 投递契约：已定 = `robot_outbox` 表轮询（见上）。
- [x] **Step 2: 迁移 `mart-ops-outbox-v1`。** `robot_outbox(dedupe_key PK, region, kind, business_date, title, body_md, at_user_ids JSON, status enum(pending/delivered/failed), attempts, created_at, delivered_at, last_error)`。
- [x] **Step 3: `OutboxRepository`。** `enqueue`（重复 dedupe_key → 返回 False）、`fetch_pending`（oldest-first，`attempts < max_attempts`）、`mark_delivered`、`register_failure`（attempts+1；达上限 → `failed`，否则保持 `pending`）。

## Task 2: robot 计算切换（remind / check）

- [x] **Step 1: 消息构建纯函数。** `build_reminder_message` / `build_check_message` / `build_ding_content`，与 `core.do_remind` / `core.do_check` 逐字一致（测试对拍）。
- [x] **Step 2: 任务规划。** `run_remind` / `run_check`：`dim_calendar` 判工作日（当月无日历行 = 显式失败，替代现行月份校验）、`common.metrics.daily_report` 求未填（aliases 支持）、`enqueue` 幂等（重复 → `already_sent`）、全员已填 → 不入队。check 额外入队 `kind='ding'`（@人 + cc）。
- [x] **Step 3: 行为变化点（有意）**：`missing`（表内有人但通讯录无映射）在 mart 世界恒为空——未填名单本就出自 `dim_robot_member`，人人有 user_id。

## Task 3: gateway 投递器

- [x] **Step 1: 轮询与分发。** `common/gateway/delivery.py` 的 `OutboxDeliveryWorker`：`fetch_pending` → 按 `kind` 分发（`remind/check/leaderboard` → 群 Markdown；`ding` → DING）；未登记 kind 记失败（达上限自然转 `failed`，不卡死队列）；单行失败不中断批次。投递语义**至少一次**（与现行 stateFile 同语义，已文档化）。
- [x] **Step 2: 真实投递器。** `common/gateway/dingtalk_deliverer.py`：群消息走 `DingTalkClient.send_group_markdown`（与现行机器人同一路径，@人透传、空 @ 列表转 None）；region 配置（robotCode/conversationId）字典注入，容器化后由 Nacos 提供（Task 4）。DING 保持现行 `dws ding` 命令契约（`DwsCommandDingSender`，契约文本逐字对拍），切换工作通知 API 的点收敛在 `ding_sender` 一个参数。
- [x] **Step 3: 状态回写。** 成功 `mark_delivered(delivered_at)`；异常 `register_failure`（每 kind 一个非泄露错误码：`group_send_failed` / `ding_send_failed` / `unknown_outbox_kind`）。
- [x] **Step 4: 测试。** `tests/common/test_gateway_delivery.py` 14 例：分发路由、@人透传、失败续批、上限转 failed、dws 契约逐字、region 配置查找。

## Task 4: 容器与配置

- [x] **Step 1: 区域配置通道（`common/region_config.py` + `regions.seed.json`）。** 区域**集合**由版本受控种子定义（新增区域须先进种子、代码评审）；Nacos 只做**字段覆盖**（group `REGIONS`、dataId `region-<region>.yaml`、缺失/不可达 fail-open 回种子）。robotCode / conversationId / tableUrl 在种子里是占位符，真实值由运维发布到 Nacos、不入 git。
- [x] **Step 2: `robot` 容器**（profile `robot`，`robot-hangzhou`；`python -m common.daily_robot.mart_cli once --confirm-local-test-write`，按 `remindHour`/`checkHour` 分流）。零凭据、零挂载、零 `--live-*` 旗标；门控 `require_business_run`（与提取层同保证，无外部调用）。
- [x] **Step 3: `dingtalk-gateway` 容器**（profile `dingtalk-gateway`；`python -m common.gateway.cli run --live-send ...`，长驻轮询、`--once` 单轮可验收）。只挂钉钉凭据文件（不挂 manifest）；门控 `require_gateway_run`——外发属**外部写入**，须显式 `--live-send`（区别于 `--live-read`）。
- [x] **Step 4: 注册与合约测试。** `pipelines.seed.yaml` 增 `robot-hangzhou`（kind=business，cron `0 18,20 * * *`）与 `dingtalk-gateway`（kind=apps，长驻无 schedule）；合约测试把守「robot 零凭据零外呼 / gateway 仅钉钉凭据」。

## Task 5: Stream 报数落库（次后增量）

- [x] **Step 1: 单连接按群路由多 region**（spec §9：同应用不可多连接）。`common/gateway/stream_handler.py` 的 `StreamReportHandler`：一个进程一条 Stream 连接，所有群消息按 `conversationId` → `region_for_conversation` 路由；未登记群静默 ACK；异常 rollback + 通用回执（不泄露异常原文，**有意改动**：现行 `⚠️ 处理报数时出错：{e}` 会插值异常）。
- [x] **Step 2: 报数解析 → 直接落 `mart_ops` 事实表。** `common/gateway/report_intake.py`：解析/门禁/回执文案与 `listener.py` 逐字对拍；身份走 `dim_robot_member`（`sender_staff_id` 直查 + region 匹配，替代白名单文件）；**业务键优先写入**（`(region, 表内用名, date)` 有行就地更新、无行插 `stream:` 主键，`STREAM_RUN_ID` 全零标记）；月累计/完成率走共享口径（含当天）。钉钉 AI 表由此降级为非真源、不回写。
- [x] **Step 3a: Stream 运行时接入。** `build_stream_client`（`Credential` + 单 client + topic 注册，与现行 listener 接法一致）；gateway CLI `run --with-stream --live-read`：单进程双职责（outbox 轮询守护线程 + Stream 主线程 `start_forever`，spec §2 单容器），compose 已切换到该命令；合约测试锁定。
- [ ] **Step 3b: 真群报数对照**（运维窗口）：执行 `docs/阶段4切换运行手册.md` 第 3 步验收清单。

## Task 6: 切换与退役

- [x] **Step 1: 榜单改读 `mart_ops`。** `common/daily_robot/mart_leaderboard.py`：`mart_collect`（`collect` 的 mart 版——跳过合计行、姓名 strip、无部门→未分组、无目标→target 0/rate None、同序排序键，全部对拍锁定）+ `build_leaderboard_view`（`RegionConfig` + `dim_calendar` → 现行 config 同形字典，restDays 由日历反推）。展示层零改动：`leaderboard.py` 提取纯函数 `render_bc_markdown`（`build_bc_markdown` 变为 collect+render 的薄包装，逐字节等价有测试）；`build_html` 直接吃 mart 视图（就绪已证明）。`mart_cli leaderboard` 子命令：采集 → 群播报 markdown → outbox（`kind='leaderboard'`，dedupe 后缀 HHmm，链接取 `leaderboardUrl`）。`RegionConfig` 增 `deptOrder`/`deptLabel`/`broadcastExclude`/`leaderboardUrl`。
- [x] **Step 1b: pages-leaderboard 容器。** `mart_cli leaderboard-html --output ...`（同一镜像的子命令）：采集 → 既有 `build_html` → 写挂载目录；`pages-hangzhou` 服务（profile `pages`，零凭据、唯一挂载 `/output`，`PAGES_OUTPUT_DIR` 可覆盖）+ `pipelines.seed.yaml` 登记（cron `30 8 * * *`）+ 合约测试把守。发布通道（QW Pages）维持现状由运维执行——容器只接管「生成 HTML」这半步。
- [ ] **Step 2: cron 切换到容器**；旧路径保留一个回退窗口。执行手册：`docs/阶段4切换运行手册.md` 第 4 步（先启新、对照 2 个工作日、再停旧、回退窗口 1 周）。
- [ ] **Step 3: 退役清单。** `recalc_totals`（重算+写回）、`stateFile`、`org_sync` 快照胶水、钉钉 AI 表的真源地位。执行手册：同文档第 5 步（含「关闭日报表同步」的前置⚠️：目标列当前仍由表同步提供，目标迁移方案需先确认）。
- [x] **Step 4: Nacos region 发布工具。** `region_config.publish_region_configs` / `publish_regions_from_env`（group `REGIONS`，真值文件仓库外保管，与种子同 schema 校验）+ `gateway.cli publish-regions --source ... [--if-missing]`。

---

## Verification（Task 1/2 已完成时）

- 单测：`python -m unittest discover -s tests -t .`（**315+ 通过**）。
- 文案对拍：`tests/common/test_mart_tasks.py` 中 remind/check/DING 三段文案与 `core.py` 模板逐字一致。
- 幂等：同一 `dedupe_key` 重复 `enqueue` → 第二次返回 False；`run_remind` 当日重跑 → `already_sent`，无重复 outbox 行。
