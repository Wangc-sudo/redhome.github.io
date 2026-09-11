# 提取层（`extract-mart`）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把本地 `raw_dingtalk` 的 `payload_json` 投影为业务可读的 `mart_ops` 事实表与维度表，使业务线（`robot`、`pages-leaderboard`）只读 `mart_ops` 即可出数，且**永不持有钉钉凭据、永不接触 `raw_*`**（spec §7）。

**Architecture:** 新增 apps 线的第三个容器 `extract-mart`（profile `extract`）。它与 `sync-*` 的关键差别是**不读任何外部源**：输入只有本地 raw 库，输出是 mart 的事实投影与派生维度，因此不需要 `--live-read`、不挂凭据。每次运行是独立的 `sync_run_id`（spec §6「run_id 按线独立」），复用 `sync_runs` / `sync_dataset_summary` 两张表并把摘要标为 `source_name='extract'`，使控制面仍只有**一个**「线状态」入口（spec §4）。投影是**全量重放**：raw 是唯一可重放层，故 `extract-mart` 从 raw 重建整张 mart 表、绝不回头读源，`ON DUPLICATE KEY UPDATE` 保证幂等。

**Tech Stack:** Python 3.12、标准库 `unittest`/`unittest.mock`、PyMySQL 1.1.1、MySQL 8.4、Docker Compose、Ubuntu 24.04。

**Safety constraints:**

- 提取层不得构造任何来源客户端（钉钉 / WDT），不得发起任何外部请求；单测与 `test-runner` 均不触网。
- `extract-mart` 服务**不得挂载** `source-credentials.json` 或 `manifest.json`，其 CLI 也**不接受** `--live-read`——凭据边界是提取层的存在前提，不是可选项。
- 写入只能落在 `_test` 库：沿用 `live_safety._require_local_test_target`（`APP_ENV=test`、`INTEGRATION_TEST_RUNNER=1`、`PUBLIC_DATA_RDS_HOST=mysql`、精确三库名）。
- 投影只允许搬运**白名单**列；任何配置字符串都不得直接成为 SQL 标识符。
- 不修改现有机器人入口、日报口径与真实 RDS。

---

## File structure

| Path | Responsibility | 状态 |
|---|---|---|
| `common/public_data/mart_extract_schema.py` | 提取层 DDL + 每个数据集的「源列 → 目标列」白名单；作废列与钉钉技术列刻意不投影 | 已落地 |
| `common/public_data/live_migrations.py` | 新增 `mart-extract-v1`：扩 `sync_dataset_summary.source_name` 枚举 + 建提取层四张表 | 已落地 |
| `common/public_data/extract_mart.py` | `MartExtractRepository`（读 raw / upsert mart / 替换日历）与 `MartExtractService`（run 生命周期、全量重放、摘要） | 已落地 |
| `common/calendar_utils.py` | `month_days()`（真实月份长度）与 `load_calendar_seed()`（解析版本受控 rule，`restDays` 一律推导） | 已落地 |
| `common/public_data/live_safety.py` | 抽出共享的 `_require_local_test_target`，新增 `require_extract_run`（无 `--live-read`） | 已落地 |
| `common/public_data/settings.py` | 可选 `PUBLIC_DATA_CALENDAR_SEED`（未配置 = 跳过日历步骤，而非失败） | 已落地 |
| `common/public_data/cli.py` | `extract-mart` 子命令 + `require_extract_run` / `load_calendar_seed` / `build_extract_service` 惰性包装 | 已落地 |
| `docker/integration/calendar.seed.json` | 版本受控的工作日历种子（只记 rule） | 已落地 |
| `docker-compose.integration.yml` | `extract-mart` 服务（profile `extract`，无任何凭据挂载） | 已落地 |
| `docker/integration/pipelines.seed.yaml` | 注册 `extract-mart`（`reads`/`schedule`） | 已落地 |
| `common/public_data/org_read.py` | 钉钉通讯录只读 gateway（部门子树 → 成员） | **待落地** |
| `common/public_data/extract_mart.py` | 追加 `dim_robot_member` 写入方（整体替换） | **待落地** |
| `tests/common/test_public_data_extract_mart.py` | 投影白名单、幂等 upsert、run 状态机、日历推导测试 | 已落地 |
| `tests/common/test_calendar_utils.py` | 新增 `TestLoadCalendarSeed` / `TestMonthDays`：种子解析、自证 2026-09、明确失败路径 | 已落地 |
| `tests/test_integration_environment.py` | `extract` profile 的 Compose 合约：可选启动且**凭据零挂载** | 已落地 |

---

## Task 1: 提取层 schema 与迁移

- [x] **Step 1: 定义投影白名单。** `EXTRACT_DATASETS` 显式登记每个数据集的列映射，`achievement_rate`（作废，spec §10）、`dingtalk_record_id`、`parent_record_refs`、技术列一律不出现——白名单即契约。
- [x] **Step 2: 定义 DDL。** 事实表 `fact_daily_report_offline` / `fact_channel_daily_sales`，维度表 `dim_calendar` / `dim_robot_member`；业务列一律跟随 raw 的「可空」语义，投影层不得因源侧空值写入失败。
- [x] **Step 3: 注册迁移。** `mart-extract-v1` 先 `ALTER TABLE sync_dataset_summary` 扩枚举（不改动已应用的 `mart-ops-v1`，避免校验和失配），再建表。
- [x] **Step 4: 校验。** `test_public_data_live_migrations.py` 既有断言（各库只建自己的表）保持通过。

## Task 2: 工作日历种子

- [x] **Step 1: `load_calendar_seed()`。** 校验 `version`、`source`、`months`；`restDays` 由 `generate_rest_days` 推导而非手工列举；`source=yonyou_tplus` 抛 `CalendarSourceUnavailable`（**明确失败，不静默降级**），未知来源抛 `CalendarError`。
- [x] **Step 2: `month_days()`。** 按真实月份长度取数（与 `Calendar` 的 1~30 简化口径区分）。
- [x] **Step 3: 种子文件。** `docker/integration/calendar.seed.json` 只放 rule；测试直接读仓库内文件断言 `[6, 13, 19, 25, 26, 27]`，把 spec §10 的自证固化为回归。

## Task 3: 提取服务

- [x] **Step 1: 仓储。** `read_dataset()` 在 SQL 层就排除非白名单列；`upsert_fact()` 走 `ON DUPLICATE KEY UPDATE` 且不覆盖主键；`replace_dim_calendar()` 用「整体替换」（日历是提取层完全拥有的派生维度，种子删月须随之消失）。
- [x] **Step 2: 服务。** `start_run` → 逐数据集（加锁 → 读 raw → 事务内写 mart → 落摘要）→ 物化日历 → `mark_completed`；`manifest_sha256` 填**投影计划摘要**（提取层没有 manifest，但计划就是「什么配置产出了这批数据」）。
- [x] **Step 3: 失败语义。** 与 `live_sync` 同构：只有「mart 已写、摘要失败」标 `projection_pending`，其余标 `failed`；`MartExtractError` 自带 failure code。

## Task 4: CLI 与安全门控

- [x] **Step 1: 门控。** 抽出 `_require_local_test_target`，`require_live_run` 复用它（行为与错误信息不变），新增 `require_extract_run`。
- [x] **Step 2: 子命令。** `extract-mart --confirm-local-test-write [--service]`；缺确认即退出且不 `load_settings`；Nacos `enabled` 门控失败开放（fail-open）。
- [x] **Step 3: 输出。** 复用 `_print_success`，只打印安全摘要，异常不泄露 traceback。

## Task 5: 容器与注册表

- [x] **Step 1: compose。** `extract-mart`（profile `extract`），`PUBLIC_DATA_CONFIG` 用本地测试 config（**不是** live manifest），`PUBLIC_DATA_CALENDAR_SEED` 指向种子；**无 volume**。
- [x] **Step 2: 合约测试。** `test_extract_runner_is_opt_in_and_credential_free` 断言：可选启动、无端口、命令含 `extract-mart`、**不含** `--live-read`、**不挂**凭据/manifest。
- [x] **Step 3: 注册。** `pipelines.seed.yaml` 增 `extract-mart`（`schedule: 0 4 * * *`，在 `project-mart` 之后）。

## Task 6: `dim_robot_member` 通讯录直连（下一增量）

- [ ] **Step 1: 写失败测试。** 以注入的 gateway 断言：部门子树展开为成员、`region` 归属正确、离职成员被移除（整体替换）、摘要以 `extract` 落库。
- [ ] **Step 2: 实现 `OrgReadGateway`。** 只读通讯录（`department/listsub` + `user/listid` + `user/get`），凭据仅由 apps 线持有；网关不持有任何写方法。
- [ ] **Step 3: 接入服务。** `extract_mart` 追加 `dim_robot_member` 整体替换步骤；未配置组织根部门时跳过该步骤（不失败）。
- [ ] **Step 4: 校验「未填」链路。** `dim_robot_member × 日期范围 LEFT JOIN 事实表` 能求出「未填」组合（口径的放置位置见 spec §9）。

---

## Verification

- 单测：`python -m unittest discover -s tests -t .`（当前 **242 通过 / 13 跳过**）。
- Docker 全量：`docker compose -f docker-compose.integration.yml up --build --abort-on-container-exit test-runner`。
- Compose 合约：`docker compose -f docker-compose.integration.yml --profile extract config`，确认 `extract-mart` 只有 `extract` profile、无 bind mount。
- 手工验收（本地 `_test` 库）：
  1. `--profile live-sync-dingtalk run --rm sync-dingtalk` 落 raw；
  2. `--profile extract run --rm extract-mart` 落 mart；
  3. 断言 `fact_daily_report_offline` 无 `achievement_rate` 列、`dim_calendar` 的 2026-09 休息日为 `[6, 13, 19, 25, 26, 27]`、`sync_runs.status='completed'` 且摘要 `source_name='extract'`。
