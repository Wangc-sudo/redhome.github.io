# 阶段 4 切换执行 Checklist（日报机器人 → 容器化链路）

> 配套文档：`docs/阶段4切换运行手册.md`（原理与口径）、`docs/上云部署规划.md`。
> 用法：逐项执行并打勾；任何一项验收失败 → **停下，按该项回退点回退**，不带病前进。
> 执行人：______ 　切换窗口：____年__月__日 __:__ 起

---

## 0. 切换前预检（确认当前仍在旧链路）

- [ ] 现网机器桌面端 cron 5 个定时任务**仍在启用**，且当日 18:30/20:00 输出正常
- [ ] `mart_ops` 的 `robot_outbox` 表**无**近期 `delivered` 行（证明新链路未接过流量）
- [ ] Nacos `REGIONS` 组无 `region-hangzhou.yaml` 真值（或仍为占位符）
- [ ] 本地仓库在 `main` 最新、`git status` 干净
- [ ] Docker 宿主机可正常 `docker compose -f docker-compose.integration.yml config` 无报错
- [ ] 凭据文件（仓库外）就位：真实 region 配置 JSON、`source-credentials.json`、manifest
- [ ] 已通知相关群成员：切换窗口内可能出现双份提醒（双跑期）

## 1. 发布真实 region 配置到 Nacos

- [ ] 在**仓库外**准备真值文件（schema 同 `docker/integration/regions.seed.json`）
- [ ] 启动 Nacos：

```bash
docker compose -f docker-compose.integration.yml --profile nacos up -d nacos
```

- [ ] 发布配置：

```bash
PUBLIC_DATA_NACOS_SERVER=localhost:8848 \
python -m common.gateway.cli publish-regions --source /仓库外/regions-prod.json
```

- [ ] **验收**：Nacos console（group=`REGIONS`）可见 `region-hangzhou.yaml`，robotCode / conversationId / tableUrl 均为真值
- [ ] 无表区域（如 vanke）的 `monthlyTargets` 已随 region 配置发布
- [ ] 回退验证：删除该 dataId 后回退到种子值（确认可回退再发布回去）

## 2. 数据链路双跑对照（只读侧，不动现网 cron）

- [ ] 第 1 轮：

```bash
docker compose -f docker-compose.integration.yml --profile live-sync-dingtalk run --rm sync-dingtalk
docker compose -f docker-compose.integration.yml --profile live-sync-wdt      run --rm sync-wdt
docker compose -f docker-compose.integration.yml --profile extract            run --rm extract-mart
```

- [ ] 第 2 轮（间隔 ≥1 天）重复上述三条命令
- [ ] **验收**：两轮 `sync_runs.status='completed'`，无 `failed` / `projection_pending`
- [ ] **验收**：`dim_calendar`、`dim_robot_member` 行数与现网 `config.json` 的 org.archives 人数一致
- [ ] **验收**：`fact_*` 行数与 `record_id_digest` 两轮稳定
- [ ] **验收**：`mart_cli leaderboard`（dry 对照）未填名单/榜单数字与现网机器人当日输出一致
- [ ] 任一项不一致 → 记录差异、排查、重跑，**不进入第 3 步**

## 3. 真群报数对照（写侧，用测试群）

- [ ] 将测试群的 `robotCode` / `openConversationId` 发布为 region 配置（同第 1 步流程）
- [ ] 启动 gateway：

```bash
PUBLIC_DATA_LIVE_CREDENTIALS_PATH=/仓库外/source-credentials.json \
docker compose -f docker-compose.integration.yml --profile dingtalk-gateway run --rm dingtalk-gateway
```

- [ ] 测试群内 `@提醒事项 12800`
- [ ] **验收**：群内收到 ✅ 回执（金额、累计、完成率与手算一致）
- [ ] **验收**：`fact_daily_report_offline` 出现 `stream:hangzhou:<uid>:<date>` 行（或当日行更新）
- [ ] 再报一次不同金额 → **验收**：🔁 覆盖提示出现
- [ ] 非责任人发言 → **验收**：⛔ 拒绝回执
- [ ] 回退验证：停 gateway 容器，确认现网 listener 不受影响

## 4. cron 切换（robot / pages 上线）

- [ ] 启动新链路：`robot-hangzhou`（profile `robot`）、`pages-hangzhou`（profile `pages`）、`dingtalk-gateway`（长驻）
- [ ] 观察第 1 个工作日：18:30 提醒、20:00 催办+DING、次日 8:30 榜单，与现网输出比对（文案、@人、顺序）
- [ ] 观察第 2 个工作日：同上
- [ ] **验收**：`robot_outbox` 每日出现 `remind`/`check`/`ding`/`leaderboard` 行且 `status='delivered'`，无 `failed`
- [ ] **验收通过后**：注释现网 5 个 cron 项（**保留不删**，进入回退窗口）
- [ ] 回退窗口开始日期：____年__月__日（建议 1 周，至 ____年__月__日）
- [ ] 回退演练记录：恢复旧 cron + 停新容器 ≤ __ 分钟可完成（实际掐表一次）

## 5. 旧路径退役（回退窗口结束、无回退发生后执行）

- [ ] 5.1 从 `docker/integration/source-manifest.json` 的 `dingtalk.bases` 移除 `daily_report_offline` 与 `channel_daily_sales`（`dingtalk.org` 保留），重跑 sync+extract 验证
  - 前置确认：hangzhou/shaoxing 当月目标已迁入 region 配置 `monthlyTargets` 字段
- [ ] 5.2 删除 `core.recalc_totals` 与两个机器人的 `recalc_totals.py` 及对应 cron
- [ ] 5.3 删除旧 `hangzhou_reminder.py` / `shaoxing_reminder.py` 的 `stateFile` 机制
- [ ] 5.4 停用 `org_input_*.json` 快照流程，机器人 `members` 映射改读 `dim_robot_member`
- [ ] 5.5 逐个删除旧入口脚本（随对应容器验证完毕逐个删）：
  - [ ] `hangzhou_reminder.py`
  - [ ] `shaoxing_reminder.py`
  - [ ] 两个 `leaderboard_report.py`
  - [ ] `hangzhou_listener.py`
  - [ ] `shaoxing_listener.py`
- [ ] 每步删除后单独提交 commit，便于 `git revert` 回退

## 6. 切换后常态确认

- [ ] README「运行方式」一节更新为容器化链路（删除 Windows 本机 cron 描述）
- [ ] README「尚未实施」清单移除阶段 4 条目
- [ ] 运维知晓：关停/扩线走 Nacos `PIPELINES` 组改 `enabled`
- [ ] 运维知晓：新增区域 = `regions.seed.json` + `org.seed.json` + `pipelines.seed.yaml` + Nacos 发布 + 复制容器服务
- [ ] 日历跨年待办登记：HR 更新 `docker/integration/calendar.seed.json` 后重跑 `extract-mart`

---

## 回退速查

| 阶段 | 回退动作 |
|---|---|
| 第 1 步 | 删除 Nacos 对应 dataId |
| 第 2 步 | 无需回退（只读） |
| 第 3 步 | 停 gateway 容器 |
| 第 4 步（回退窗口内） | 恢复旧 cron + 停新容器 |
| 第 5 步 | `git revert` 对应 commit |
