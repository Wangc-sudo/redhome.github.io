# 猫超价格竞争力：Nacos + Docker 接入方案

日期：2026-11-03

## 1. 目标与边界

将现已验证的猫超价格竞争力采集能力，从 Windows 本地脚本升级为 dops 统一管理的生产功能：

- Nacos 管理运行开关、采集范围、周期、页大小、超时、重试和播报策略。
- 复用云端 Docker 中的 scheduler、ops-web、gateway、nginx 和 MySQL。
- 统一任务调度、手动运行、审计、运行流水、数据入库、截图索引和钉钉播报。
- 保留 10 个二级主体遍历、每页 100 条、自动翻页、完成后切回原主体。
- 保留市场同款价、当前普惠券后价、建议价、价差、商品规格和状态字段。
- 保留截图上传 ZOS，新增 CSV/JSON 上传、SHA256 和对象索引；数据库保存业务行。

明确不做：

- 不自动点击或提交“比价反馈”。
- 不启用内部最低价及其判定。
- 不绕过滑块或钉钉/猫超安全验证。
- 不虚构页面未提供的竞争店铺名。
- 不把猫超账号、ZOS 密钥、钉钉 Secret 或 agent 令牌写入 Nacos/Git。

## 2. 现状与关键约束

### 已验证能力

- Windows 可见 Chrome + CDP 复用已登录会话。
- 10/10 二级主体遍历，整轮 30 行，完整性标记和单主体失败重试。
- 每页 100 条、全表翻页、逐主体状态与独立行数。
- 本地 JSON/CSV、ZOS 截图、钉钉通道、看门狗和登录自启动。

### 架构约束

1. dops scheduler 在 Linux Docker 容器内运行，不能直接操作 Windows 桌面浏览器。
2. 猫超登录可能需要人工滑块，不能依赖云端无头浏览器重建会话。
3. 当前 Windows 采集机没有 Docker，不应为“全容器化”而改动已稳定的 GUI 会话。
4. 10 主体整轮实测约 3 分钟，发生重试时更长，调度必须防止任务重叠。
5. 现有 `manual_import` 是人工月/季/年文件导入通道，不适合直接承载高频价格快照。

因此采用边缘采集、云端编排的混合架构。

## 3. 总体架构

```text
Nacos
  ├─ PIPELINES/price-maochao-dispatch.yaml     定时/手动派发
  ├─ PIPELINES/price-maochao-finalize.yaml     租约与登录等待治理
  └─ PRICE_AGENT/maochao.yaml                  采集参数与主体范围
                         │
                         ▼
Docker：scheduler ──创建任务──► MySQL 任务队列
Docker：ops-web  ──手动运行/配置/审计/查询──────┤
                                               │
Windows priceagent ──HTTPS 拉取任务/心跳/回传───┘
  ├─ 复用可见 Chrome + CDP
  ├─ 10 主体采集、翻页、校验、切回
  ├─ 截图/CSV/JSON 上传 ZOS
  └─ 仅回传结构化结果与 ZOS 对象键
                         │
                         ▼
ops-web complete 事务 ──► MySQL 业务表 ──► robot_outbox ──► 钉钉群
scheduler finalize ──► 超时/登录等待终态与告警
```

### 职责划分

| 层 | 职责 |
|---|---|
| Windows agent | 浏览器会话、主体切换、页面采集、证据文件、ZOS 上传 |
| Nacos | 非敏感配置、运行开关、采集周期与策略 |
| scheduler | 定时/手动派发、租约与登录等待超时治理 |
| ops-web | 机器 API、原子完成/变化检测/outbox 入队、手动运行 |
| MySQL | 任务、运行、业务快照、对象索引和幂等键 |
| gateway/outbox | 统一钉钉投递与投递审计 |
| nginx | HTTPS 入口、限流、请求体限制和安全头 |

Windows 原生 agent 是浏览器边缘例外；云端控制面全部接入现有 Docker，不在采集机强制安装 Docker。

## 4. Nacos 设计

### 4.1 调度条目

Group：`PIPELINES`

Data ID：`price-maochao-dispatch.yaml`

```yaml
service_id: price-maochao-dispatch
kind: apps
category: 同步
enabled: false
schedule: "*/10 * * * *"
description: 猫超价格竞争力采集派发
```

初装保持 `enabled: false`，完成影子验证后再开启。调度只负责创建任务，不在容器内执行 Playwright。另注册 `price-maochao-finalize.yaml`（加工、每分钟、初装停用），负责租约与登录等待超时；业务完成、变化检测和 outbox 入队在 `complete` API 的同一事务中完成。

### 4.2 采集配置

Group：`PRICE_AGENT`

Data ID：`maochao.yaml`

```yaml
version: 1
enabled: true
page_size: 100
agent_poll_seconds: 5
page_ready_timeout_seconds: 60
subject_attempts: 2
job_lease_seconds: 900
heartbeat_interval_seconds: 15
login_wait_seconds: 1800
auth_poll_seconds: 15
subjects:
  - "110183794"
  - "110342174"
  - "110925450"
  - "114567069"
  - "115057440"
  - "115379341"
  - "115617823"
  - "115619469"
  - "115993131"
  - "116734559"
restore_subject: "110342174"
notify:
  enabled: true
  region: ops
  on_failure: true
  on_result_change: true
  full_rows_per_message: 10
artifacts:
  bucket: bucket-68cc
  prefix: price-competition
```

`claim` 返回该 job 固定使用的完整配置快照和 SHA-256；Windows 不持有 Nacos 凭据。配置变更只影响新 job，执行中不改变主体范围。`restore_subject` 必须属于 `subjects`。截图/CSV/JSON 在同一 job 内生成，不保留第二个 CDP 定时循环；云端派发周期为 10 分钟，替代旧脚本默认 180 秒循环。

Nacos 只保存非敏感配置；凭据通过服务器 env 或 Windows ACL 保护文件引用。

## 5. 任务协议与可靠性

### 5.1 API

经 `https://dt.yyhlzj.com/price-agent/api/` 反代至 ops-web 内部接口：

- `POST /jobs/claim`：原子领取任务，返回 job、一次性租约明文、配置快照和哈希。
- `POST /jobs/{job_id}/heartbeat`：校验 agent/租约并上报主体、页码、登录态和会话版本。
- `POST /jobs/{job_id}/complete`：提交主体汇总、业务行和文件索引，同时携带 agent/租约。
- `POST /jobs/{job_id}/fail`：提交非泄露错误码和阶段信息。
- `GET /config-version`：返回完整配置和哈希；虽为 GET，也必须机器认证。

认证使用独立 `PRICE_AGENT_TOKEN`，与人员会话分离。云端 env 只保存 `PRICE_AGENT_TOKEN_SHA256`，原始令牌仅在 Windows ACL 保护文件中。所有 `/price-agent/api/*` 方法都走专用 `require_agent`，不经过匿名只读或人员 admin 直通。HTTPS、30r/m 独立限流和 complete 2MB 请求体上限同时启用。

### 5.2 防重叠与幂等

- `queued/running/waiting_login` 通过 generated column + UNIQUE 映射为唯一活跃 pipeline，定时与手动派发竞争也由 DB 防重；scheduler 锁只覆盖派发子进程。
- `claim` 使用 `SELECT … FOR UPDATE SKIP LOCKED`；租约明文只在领取时返回，数据库保存 SHA-256。
- 超时后旧 agent 的迟到 heartbeat/complete 返回 409，不能覆盖新任务；手动运行遇到活跃任务返回现状，不伪报新建成功。
- `job_id + completion_hash` 保证 complete 重放返回原结果且不同载荷不能冒充重放。
- 服务端生成非空 `row_uid`（主体、ItemID、SkuID、页码、行号规范化哈希），唯一键为 `job_id + row_uid`，不受 NULL ItemID/SkuID 影响。
- 文件先上传 ZOS，再在业务行、对象索引、job 终态和 outbox 的同一事务中提交；文件键必须包含 job_id，影子包使用独立前缀。
- 心跳过期标记 `timed_out`，登录等待超期标记 `login_expired`；后续周期创建新 job，不自动重放部分写入。
- 单主体失败隔离；所有主体完成后必须切回固定 `restore_subject`。
- `complete=true` 仅表示所有要求主体均成功且页遍历完整；空主体允许没有分页选择器，非空主体必须验证每页 100 条。

## 6. 数据模型

新增三组表，最终字段以迁移评审为准：

1. `pd_price_agent_job`
   - 调度/手动来源、状态、优先级、租约哈希、心跳、attempt、配置快照/哈希、run_history_id、错误。
   - `auth_state/auth_checked_at/login_required_since/session_version` 只记录登录会话元数据，不记录 Cookie。
2. `fact_maochao_price_snapshot`
   - 业务日期、主体、二级部门、ItemID、SkuID、商品、规格、市场来源、市场商品、市场价、当前价、建议价、价差、页面状态、采集时间。二级部门取主体选择器完整名称的最后一段，不用主体 ID 代替部门名。
3. `pd_price_agent_artifact`
   - 任务、主体、类型（页面截图/行证据/JSON/CSV）、ZOS bucket/key、SHA256、大小、上传状态。

现有 `pd_ops_run_history` 继续承载调度流水；自定义 job 表承载 Windows 执行协议，两者通过 job/run ID 关联。

## 7. Docker 落地方式

优先复用现有服务，不新增常驻容器：

- `scheduler`：新增派发和后处理子命令。
- `ops-web`：新增 agent API、管理读面和审计。
- `gateway/outbox`：统一群播报。
- `nginx`：新增 HTTPS agent API 反代与限流。
- `dops-app` 镜像：仅在新增 Python 依赖时重建。

代码仍通过 `/opt/dops/repo` 只读挂载发布。普通模块改动无需修改 Dockerfile；只有依赖变化才由现有 `ops/deploy.sh` 重建镜像。

Windows agent 不使用 Docker，依赖独立固定在 `windows_agent/maochao/requirements.txt`，不改变云端镜像。安装后通过 `python -m windows_agent.maochao.agent` 启动；原始 token/ZOS 凭据只放在 Windows ACL 保护的 `agent.json`。

非敏感配置发布命令：`python -m common.price_agent.cli publish-config --live-read --confirm-local-test-write --if-missing`；需要变更现值时去掉 `--if-missing`，发布前后均校验 YAML 与配置哈希。

## 8. 代码与配置改动清单

新增：

- `common/price_agent/`：任务协议、校验、入库、后处理、播报。
- `windows_agent/maochao/`：现有采集器的正式版本与安装/升级脚本。
- 对应单元测试、API 集成测试和契约测试。
- 数据库迁移与 DDL 白名单登记。

修改：

- `common/public_data/scheduler.py`：注册派发/后处理命令。
- `docker/integration/pipelines.seed.yaml`：新增调度条目。
- `common/public_data/pipeline_taxonomy.py`：登记“同步/加工”分类。
- `common/ops_web/app.py`：agent API、手动运行和中文展示。
- `docker/cloud/nginx-pages.conf`：HTTPS 反代、限流和请求体限制。
- `common/public_data/live_migrations.py`：注册新表迁移。
- 相关 scheduler、ops-web、集成契约测试。

不修改 `REGIONS`；猫超价格采集是全局业务功能，不是组织区域。

## 9. 播报策略

- 失败、超时、ZOS 上传失败和登录失效进入 `price_alert`；价格/状态变化进入 `price_change`，默认投递 `ops` 区域群，可由非敏感配置调整。
- 正常结果仅在市场价格、当前价、建议价、价差或状态指纹变化时播报；指纹排除采集时间、业务日期、分页和文件信息。首次只建立基线，不把全量商品当作变化。
- 每条消息最多展示 10 行，当前以 job_id 追溯；P2 管理读面提供显式能力门禁的详情与短时效 ZOS 签名地址，不能依赖 ops-web 匿名只读 GET。
- 所有群消息进入 `robot_outbox`，按 job/事件/会话版本去重，保留投递状态；登录等待心跳不重复告警。
- Windows 现有 `price_notify.py` 在切换后退役，不再形成第二套播报审计。

## 10. Chrome 登录会话与人工恢复

采用“持久化 Chrome 登录会话”，不导出原始 Cookie：

1. Windows 使用独立固定 `user-data-dir`，agent 与可见 Chrome 在同一 Windows 用户、同一交互式桌面会话运行。Cookie 由 Chrome 加密持久化，agent 只记录 Profile 引用和会话元数据。
2. 每轮开始及采集中根据登录页、业务 iframe、失效/滑块文案判断登录态，不能仅看 Cookie expires。失效后停止主体切换和翻页，丢弃本轮内存部分结果。
3. job 进入 `waiting_login`，暂停领取新任务但继续心跳；以 job/agent/会话版本发送一次告警。`login_wait_seconds` 到期后由 finalize 标记 `login_expired`。
4. 运维在同一可见 Chrome 完成登录和滑块，Cookie 自动写回原 Profile，不人工复制粘贴。agent 不刷新、不抢焦点，检测到业务 frame 恢复后递增 `session_version`。
5. 原 job 从头重采，保证失效前后数据不混行；无需修改 Nacos 或重启云端。

原始 Cookie 不得进入 Nacos、Git、MySQL、日志、CSV/JSON、ZOS、API 或告警。元数据文件也只包含 `profile_id/session_version/auth_state/auth_checked_at`。

## 11. 分阶段实施

### P0：代码正式化与协议落地

- 将 `tmp` 中脚本整理进仓库，补测试。
- 建表、agent token、HTTPS API、任务租约和幂等。
- 本地现有循环继续运行，生产行为不变。

### P1：Nacos + Docker 影子验证

- 注册调度但保持禁用，先由 ops-web 手动派发。
- **单采集进程、单次页面操作、本地与云端双写**；停用旧 CDP 循环，禁止两个采集器同时操作同一 Chrome。
- 影子 ZOS 使用独立前缀；旧钉钉直发不同时开启。
- 对比 10 主体行数、字段、ZOS 文件和云端审计，至少验证 24 小时。

### P2：切换主链路

- Nacos 开启定时派发与 finalize。
- 停用 Windows 本地独立循环和直发钉钉，保留只读回退。
- ops-web 展示任务状态、主体完成度和文件链接，并区分派发成功、Windows 成功和投递成功。

### P3：数据产品化

- BI 看板按主体/商品展示市场价、当前价、建议价和历史趋势。
- 增加变化告警和 ZOS 生命周期管理。

## 12. 验收标准

- 10/10 主体均有明确终态，非空主体行数与页面一致，空主体不误报。
- 每页 100 条、翻页完整、固定恢复主体成功。
- 同一任务重复回传不产生重复业务行或重复群消息；超时旧租约回传被拒绝。
- 定时/手动并发、进程中断、心跳超时和 ZOS 失败均可观测，派发流水与 Windows job 可关联。
- Nacos 修改启停/周期/主体范围后，无需重启云端容器即可在新 job 生效。
- ops-web 手动“运行一次”能形成完整 Windows 执行闭环，运行中已有活跃 job 时返回现状。
- 截图、CSV、JSON、数据库记录和播报可通过同一 job_id 互相追溯。
- Cookie 失效后停止采集并只告警一次；人工登录后原 job 自动从头恢复，无重复业务行。
- 原始 Cookie、agent 令牌、猫超/ZOS/钉钉凭据均不入仓、不入库、不入播报。
- 全程无自动反馈、无内部低价判定。

## 13. 回退

- Nacos 将 `price-maochao-dispatch.enabled` 置为 `false` 即停止新派发，finalize 继续处理存量租约；配置启停由 seed→Nacos 回退，不能把删除配置当作停机。
- 保留 Windows 既有采集包和本地结果目录。恢复旧循环前先停止新 agent 并等待活跃 job 终止，任何时候只能有一个 CDP 控制者。
- 已入库数据不删除；通过 job_id 标记问题批次，不做物理覆盖式修数。
