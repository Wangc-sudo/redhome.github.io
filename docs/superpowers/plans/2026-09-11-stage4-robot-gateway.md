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

## Task 3: gateway 投递器（下一增量）

- [ ] **Step 1: 轮询循环。** `fetch_pending` → 按 `kind` 分发：`remind/check/leaderboard` → 群 Markdown（`DingTalkClient.send_group_markdown`，robotCode/conversationId 来自 Nacos region dataId）；`ding` → 保持现行 `dws ding` 命令契约或工作通知 API（实测后定）。
- [ ] **Step 2: 状态回写。** 成功 `mark_delivered`；异常 `register_failure`（非泄露错误码）；达上限留 `failed` 供控制面告警。
- [ ] **Step 3: 测试。** fake client + fake repo：分发路由、@人透传、失败重试、上限转 failed。

## Task 4: 容器与配置

- [ ] **Step 1: `robot` 容器**（profile `robot`，cron 触发 `--once` 按小时分流 remind/check）；region 配置（displayName、tableUrl、aliases、robotCode、conversationId、cc）入 Nacos 多 dataId，种子兜底。
- [ ] **Step 2: `dingtalk-gateway` 容器**（profile `dingtalk-gateway`，长驻；挂钉钉凭据）。
- [ ] **Step 3: Compose 合约测试。** robot 零凭据零外呼；gateway 仅钉钉凭据。

## Task 5: Stream 报数落库（次后增量）

- [ ] **Step 1: 单连接按群路由多 region**（spec §9：同应用不可多连接）。
- [ ] **Step 2: 报数解析 → 直接落 `mart_ops` 事实表**（钉钉 AI 表降级为非真源、不回写）。
- [ ] **Step 3: 实测验证**（需真实凭据，运维窗口执行）。

## Task 6: 切换与退役

- [ ] **Step 1: `pages-leaderboard`** 改读 `mart_ops`（`common.metrics.daily_report` + 既有 HTML 构建）。
- [ ] **Step 2: cron 切换到容器**；旧路径保留一个回退窗口。
- [ ] **Step 3: 退役清单。** `recalc_totals`（重算+写回）、`stateFile`、`org_sync` 快照胶水、钉钉 AI 表的真源地位。

---

## Verification（Task 1/2 已完成时）

- 单测：`python -m unittest discover -s tests -t .`（**315+ 通过**）。
- 文案对拍：`tests/common/test_mart_tasks.py` 中 remind/check/DING 三段文案与 `core.py` 模板逐字一致。
- 幂等：同一 `dedupe_key` 重复 `enqueue` → 第二次返回 False；`run_remind` 当日重跑 → `already_sent`，无重复 outbox 行。
