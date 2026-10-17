---
description: digital-ops 共同维护规则——代码真源、生产只读纪律、环境事实、已知坑。改动本仓库前必读。
globs: ["**/*"]
alwaysApply: true
---

# digital-ops 共同维护规则

> 完整版 onboarding 与待办池：`docs/新成员接手与共同维护-提示词-2026-10-07.md`（含拓扑图、端口速查）。
> 本文件是精简版，只放**每次动手都要遵守**的部分。两者冲突时以 docs 那篇为准并回填。

## 铁律

1. **代码真源只有 GitHub `main`**。云上 `/opt/dops/repo` 是部署副本，**禁止在云上手贴代码**。改动走：本地分支 → 本机测试 → PR → CI 绿 → 合 main → 各主机 `ops/deploy.sh` 拉模式 CD 自拉。
2. **生产数据只读优先**。任何写操作前先说清：改什么、影响行数、回滚方式、备份位置。DELETE/UPDATE 生产表需维护者点头。
3. **不确定就说不确定**，禁止编造执行结果；环境事实先探查再写。

## 绝不

- 不 `git push --force`、不 `reset --hard` 他人分支、不删他人安全组规则（**只加不删**）。
- 不 `ALTER` 生产表；schema 不符改 manifest 或停下升级，不回退数据。
- 不用 `skip` / 删断言的方式让测试变绿；守门断言只能显式同步。

## 环境事实（2026-10-07）

- 全在天翼云华东1·南京（**非华为云**）。dops-app 公网 `203.205.91.241`（内 192.168.0.3）：容器 `dingtalk-gateway` / `dops-scheduler` / `dops-nginx`(8300) / `dops-bi-web`(18080) / `dops-ops-web`(18100) / `dops-ntp`。
- dops-ctrl `192.168.0.4` 跑 Nacos（管道注册表 + 配置）；dops-ci 是灾备机（`DOPS_SYNC_ONLY=1`，只同步代码不起服务）。
- RDS `192.168.0.7:13049`；Redis `192.168.0.6:6379`；Nacos 8848 仅 VPC 内网可达。
- 暴露面：8300 公网全开（80/443 未 ICP 备案被拦，临时）；18080 仅公司公网 IP；18100 仅运维来源；22 白名单。**切 80/443 需先备案。**
- 本机已无 Docker、无本地集成环境；集成测试跑 GitHub Actions CI。

## 已知坑

| 坑 | 做法 |
|---|---|
| `db.connect` 是 pymysql **DictCursor** | 禁 `dict(fetchall())`（单行两列会捏成假行）；按键取值 |
| 时间列 **UTC**：`sync_runs.started_at`、`raw_*.synced_at` | 但 `robot_outbox.created_at/delivered_at` 是**北京时间**；跨表 join 先确认口径 |
| 列名：`raw_records_written`（非 `records_written`）；`fact_daily_report_offline` 无 `person_id/fill_date/is_test` | 先 `DESCRIBE` 再写 SQL |
| 月目标是 **melt 形** | 一律 `MAX(monthly_target)`，绝不 `SUM` |
| 改 `app.env` 后 `restart` 不生效 | 必须 `docker compose up -d` 重建 |
| `docker cp` 对只读挂载失效 | `cat 文件 \| docker exec -i <容器> sh -c 'cat > 路径'` |
| PowerShell `>` 毁二进制流；`curl` 是 `IWR` 别名 | 远端落盘再 scp；用 `curl.exe` |
| SSH 多行 `python -c` 必炸、PowerShell 吞引号 | 本地写文件 → scp → 远端执行 |
| 汇总时名称含「合计」的行 | 必须排除 |
| pymysql `LIKE` 字面量 | `%` 写成 `%%` |
| 同一现象可能有多种定性 | 定性前逐 sheet / 逐数据集复现，不看 run 级计数下结论 |
| `sync_runs` 只有 `manifest_sha256` 无 `service_id` | 多 pipeline 共用 manifest，「同 sha 同刻」易误判为并发竞争 |

## 数据有两条写入通道（排查「数据没更新」先分清）

1. `sync-dingtalk` / `sync-wdt` / `extract-mart` **批量通道**（定时，成败记 `sync_runs` + `sync_dataset_summary`）。
2. 机器人报数 **stream 通道**（群消息 → `robot_inbox` append-only → 权限校验 → fact + `robot_outbox`），**不经过 sync**，`sync_run_id=00000000` 是其特征。

## 关停/变更一条线

Nacos data-id `<service_id>.yaml`（组 `PIPELINES`）与 seed `docker/integration/pipelines.seed.yaml` **必须双写** `enabled`，否则 Nacos 抖动即复活。scheduler 每 30s 重读，无需重建容器。

## 口径类改动

完成率、月目标、归属、催办名单等**先评审再动手**，禁止「顺手修了」。当前权威口径文档：`docs/渠道日销机器人填报机制方案-2026-09-29.md`、`docs/线下整体销售数据自动汇总方案.md`、`docs/BI建设指南.md`。

## 协作（两人 + 两个助手同时维护）

1. **单一 owner**：动手前先到 `docs/维护分工与任务认领表-2026-10-07.md` 占坑——改 owner + 日期并**立刻 push**；谁先提交谁做。开工前 `git pull`。
2. **表在仓库里**：认领表靠 git 同步，改表本身也是提交，别夹带其他改动。
3. **互斥**：改 `source-manifest.json` / `pipelines.seed.yaml` + Nacos / `docker-compose.dops.yml` / `app.env` 前先确认无人同时在改；手动跑 sync 避开定时批次（02/08/14/20 点北京），`compose up -d` 避开 08:30 / 18:30 / 20:00 业务时点。
4. **分工面**：生产写操作、业务裁决、涉及钱的事项归现有维护者；只读巡检、文档校对、可回滚的纯代码改动归新同学。
5. **收工更新状态**，不留「进行中」过夜。

## ⚠️ 旧口径提醒

`.workbuddy/memory/MEMORY.md`（工作区长期记忆）停留在 2026-09-10/09-28：称三台 ECS「全部测试环境」、生产在「本机 Windows + 千问 cron」、Git「绝不 push」。这些均已演进（已全量迁云、走 GitHub PR 必须 push 分支、ECS 承载生产机器人与对外榜单）。**以本文件与 docs 那篇为准。**
