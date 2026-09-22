# mart 拆库设计（mart_facts / mart_dims / mart_queue）

日期：2026-09-16 · 作者：arch-agent · 状态：设计稿，**本轮只出设计与开关，不落地任何迁移**
关联：《BI建设指南》§8.2 目标架构、§8.3 优化项 #3（🟡 P1）、§8.4 拆分细化方案
红线：`common/public_data/settings.py` 零改动（本文只做只读分析，落地须 main 批准后进后续批次）

---

## 1. 现状勘误（与 §8.1/§8.3 的过时描述对齐）

| §8 旧描述 | 2026-09-16 实测现状 | 证据 |
|---|---|---|
| 「extract-mart 全量重建」 | fact 已是 `upsert_fact`（`ON DUPLICATE KEY UPDATE` 逐行幂等）；已有 `_plan_digest` / `_compute_digest` / `_save_summary` 审计链；仍整表替换的只有 `replace_dim_calendar`、`replace_dim_robot_member`——其**删除语义是有意设计**（维度由提取层完全拥有，被移除的月份/离职成员必须消失），写路径的「upsert + NOT IN 剪枝」等价增量改造进行中（pipe-agent 本轮，full-rebuild 开关保留作回退）；另有 `read_dataset` 全表扫描增量改造中（pipe-agent） | `extract_mart.py:158-179`（upsert）、`181-200` / `222-249`（dim 替换）、`142-156`（全表扫描）、`501-518`（digest） |
| 「gateway 耦合投递和消费」 | 已解耦：`outbox_repository.py` 提供 `enqueue` / `fetch_pending` / `mark_delivered` / `register_failure` + `max_attempts=5` 状态机（`pending→delivered/failed`）；`gateway/delivery.py` 只做轮询投递；`gateway/stream_handler.py` 已独立成 Stream 报数路由 | `outbox_repository.py:39-143`、`delivery.py:54-113`、`stream_handler.py:42-89` |
| 「mart_ops 承担过多职责」 | **仍然成立**，且比 §8.4 列的更杂：除 fact/dim/queue 外还混有控制面表 `sync_runs` / `sync_dataset_summary` / `pd_live_schema_migration` | `live_migrations.py:83-117`、`246-260` |

### 1.1 mart_ops 现有表全量盘点（13 张业务相关 + 3 张控制面）

| 类别 | 表 | 写入方 | 读取方 |
|---|---|---|---|
| 事实 | `fact_daily_report_offline`、`fact_channel_daily_sales`、`fact_order_line`、5 × `fact_fin_*`、`fact_manual_report` | extract-mart；**gateway 也直写** `fact_daily_report_offline`（Stream 报数，`report_intake.py:221-229`） | bi-web、robot、pages |
| 维度 | `dim_calendar`、`dim_target`、`dim_product`、`dim_robot_member` | extract-mart（calendar/member/product）、`load-target`（target） | bi-web、robot |
| 队列 | `robot_outbox` | robot 入队；gateway 轮询+回写 | robot、gateway |
| 控制面 | `sync_runs`、`sync_dataset_summary`、`pd_live_schema_migration` | sync-* / extract / migrate | 巡检、控制面 |

### 1.2 三条决定拆分形态的硬约束

1. **单一 RDS 实例**：`settings.py:109-114` 把所有库名（dingtalk/wdt/mart/manual）套进同一份 `host/port/user/password`。拆库只能是**同实例 schema 拆分**，不存在第二个实例可扩。
2. **存在跨 dim×fact 的单条 SQL JOIN**：`common/metrics/daily_report.py:230-242` 的 `fetch_unfilled_members` 是 `dim_robot_member LEFT JOIN fact_daily_report_offline`。跨实例拆分会直接打断这条查询；同实例拆分只需 schema 限定名（`mart_dims.dim_robot_member`）。
3. **robot 单连接双用途**：`mart_cli.py:180-181` 用同一条 mart 连接读事实/维度、写 `robot_outbox`；gateway 同样一条连接轮询 outbox 又经 `report_intake` 写事实。拆库后这些调用点需要按职责分连接，或依赖同实例跨 schema 访问。

---

## 2. 拆分方案

### 2.1 目标形态：同实例四 schema（mart_ops 收敛为控制面）

```
同一 RDS 实例（settings.py 单连接串现状不变）
├── mart_ops      → 收敛为控制面：sync_runs / sync_dataset_summary / pd_live_schema_migration
├── mart_facts    → fact_*（9 张，含 fact_manual_report）
├── mart_dims     → dim_calendar / dim_target / dim_product / dim_robot_member
└── mart_queue    → robot_outbox
```

**取舍：同实例 schema 拆分，不跨实例。** 依据：

- 跨实例拆分打断 `fetch_unfilled_members` 的 dim×fact JOIN（约束 2），要改成「两次查询 + Python 反连接」——`common.metrics` 口径面变大，违背「DB 侧只做结构性补全」的既定边界（`daily_report.py:138-139`）；
- settings.py 的单 host 假设（`settings.py:109-114`）意味着跨实例要引入第二套 RDS 连接配置，运维面凭空翻倍，而当前数据规模（BI建设指南 §3.1：百行~千行级）完全没有容量压力；
- 本拆分的真实收益是**职责边界、权限粒度、备份/迁移粒度**——同实例 schema 即可全部获得：可按 schema 授权（bi-web 只授 `mart_facts`+`mart_dims` 的 SELECT，不再能摸到 `robot_outbox`）、可按 schema 做备份策略（dims 低频、queue 可不进长周期备份）。

「独立扩缩容」这一 §8.4 宣称的收益**本轮明确放弃**——单实例下不存在独立扩缩容，写进「不做什么」。

### 2.2 连接串抽象（对照 settings.py 现有命名推导）

现有推导模式（`settings.py:83-91`）：`PUBLIC_DATA_MANUAL_DATABASE` 可选，缺省按 `raw_manual` + 环境后缀推导；铁律由统一校验环执行（`settings.py:93-98`：test 必须 `*_test`，production 必须无 `_test`）。

建议新增三个**可选**环境变量，完全复用同一模式：

| 环境变量 | 缺省推导（test / production） | 对应 Settings 字段（新增） |
|---|---|---|
| `PUBLIC_DATA_MART_FACTS_DATABASE` | `mart_facts_test` / `mart_facts` | `mart_facts_database` |
| `PUBLIC_DATA_MART_DIMS_DATABASE` | `mart_dims_test` / `mart_dims` | `mart_dims_database` |
| `PUBLIC_DATA_MART_QUEUE_DATABASE` | `mart_queue_test` / `mart_queue` | `mart_queue_database` |

- 三个名字并入 `database_names` 字典后即自动进入 `*_test` 铁律校验环（`settings.py:93-98`），无需新增校验代码；
- host/port/user/password 继续共享 `connection_values`（`settings.py:109-114`）——与「同实例拆分」决策一致；
- **灰度期兼容**：`PUBLIC_DATA_MART_DATABASE` 保持必填不动。过渡期各服务「新库未配 → 回落 mart_database」，由封装函数 `resolve_facts_db(settings) → DatabaseSettings`（返回新库或回落旧库）承载，业务代码不感知开关细节。该封装落在 `common/public_data/db.py` 旁的新模块（如 `mart_routing.py`），不进 settings.py 热路径；
- `DatabaseSettings` 是 frozen dataclass，回落只是返回 `settings.mart_database`，零拷贝成本。

### 2.3 迁移机制适配（live_migrations）

- `_MIGRATIONS` 的 target 维度（`live_migrations.py:246-260`）新增 `mart_facts` / `mart_dims` / `mart_queue` 三个 key，各挂新迁移版本（如 `mart-facts-v1`）；
- **禁止复用旧版本号改 DDL 文本**：`live_migrations.py:297-301` 的校验和机制会把「同版本改文本」判定为漂移并拒绝启动（`mart_extract_schema.py:430-446` 的注释就是先例）。搬表一律走「新版本 CREATE IF NOT EXISTS 到新 schema + 数据回填 + 旧表退役」三步；
- `pd_live_schema_migration` 跟踪表按连接（即按 schema）各存一份，天然支持分库独立推进。

### 2.4 各 schema 的迁移手法（按写入者数量分类）

| schema | 表 | 写入者 | 迁移手法 |
|---|---|---|---|
| mart_queue | `robot_outbox` | robot（enqueue）+ gateway（回写） | **排空-搬表-切换**：无需双写（见 §3.2） |
| mart_dims | 4 × dim_* | 每表单写入者（extract 或 load-target） | **RENAME + 回填校验**：单写入者，无并发写冲突，无需双写 |
| mart_facts | 9 × fact_* | extract（upsert）+ gateway（直写日报事实）+ manual-import | **双写 → 校验 → 切读 → 停旧写**（唯一需要双写的域） |

### 2.5 双写与回切开关

开关走环境变量（与全仓 env 驱动风格一致），不落 Nacos——拆库是发布动作，不是运行时编排：

| 开关 | 取值 | 语义 |
|---|---|---|
| `MART_SPLIT_FACTS_WRITE` | `off`（默认）/ `dual` / `new` | extract 与 gateway 报数对 fact 的写入面：只写旧 / 双写 / 只写新 |
| `MART_SPLIT_FACTS_READ` | `old`（默认）/ `new` | bi-web、robot、pages 的 fact 读取面 |
| `MART_SPLIT_DIMS_READ` | `old`（默认）/ `new` | dims 读取面（dims 单写，切读前已完成 RENAME，无需写开关） |
| `MART_SPLIT_QUEUE` | `off`（默认）/ `on` | robot 入队与 gateway 轮询指向 `mart_queue.robot_outbox` |

实现位置建议：`common/public_data/mart_routing.py` 集中解析上述开关并返回连接/表限定名；`common.metrics`、bi-web `queries.py`、extract、gateway 只依赖该模块。**关键代码改动面**（供后续批次估算）：

- `daily_report.py:230-242` 的 JOIN 需改为 schema 限定名（`mart_dims.dim_robot_member` × `mart_facts.fact_daily_report_offline`）——同实例下合法；
- `mart_cli.py:180-181` 拆为两条连接（读连接 + queue 连接）；
- gateway `report_intake` 的 fact 写连接与 outbox 轮询连接分离；
- extract 的 `_mart_connection` 拆为 facts/dims/control 三条（`extract_mart.py:57-60` 的仓储构造已注入化，改动面小）。

### 2.6 灰度顺序

**mart_queue → mart_dims → mart_facts**，风险递增、验证递增：

1. **mart_queue 先行**（风险最低、验证工具链）：
   - 表最小、写入者只有 robot/gateway 两方、`OutboxRepository`（`outbox_repository.py:31-143`）已是现成接缝；
   - 借它验证：新 target 的迁移版本机制、`*_test` 铁律对新库名生效、开关解析、合约测试改造。
2. **mart_dims 次之**：
   - 单写入者，`RENAME TABLE mart_ops.dim_x TO mart_dims.dim_x` 原子完成（同实例），回填校验后即可切读；
   - 借它验证：跨 schema JOIN 限定名改造（`daily_report.py:230-242`）、extract 三连接拆分。
3. **mart_facts 最后**：
   - 数据量最大、消费面最广（bi-web 全部卡片）、且有两个独立写入者（extract + gateway 报数），必须走完整双写周期。

### 2.7 数据一致性校验 SQL（每步切换前的准出条件）

```sql
-- (a) 行数核对（每张表，旧 vs 新）
SELECT
  (SELECT COUNT(*) FROM mart_ops.fact_daily_report_offline) AS old_cnt,
  (SELECT COUNT(*) FROM mart_facts.fact_daily_report_offline) AS new_cnt;

-- (b) 主键集合双向差异（必须均为 0 行；LEFT JOIN 写法兼容所有 MySQL 8）
SELECT o.source_record_id FROM mart_ops.fact_daily_report_offline o
LEFT JOIN mart_facts.fact_daily_report_offline n USING (source_record_id)
WHERE n.source_record_id IS NULL
UNION ALL
SELECT n.source_record_id FROM mart_facts.fact_daily_report_offline n
LEFT JOIN mart_ops.fact_daily_report_offline o USING (source_record_id)
WHERE o.source_record_id IS NULL;

-- (c) 内容摘要复用既有 digest 机制：extract 每轮已把「排序后主键的
--     SHA256」写进 sync_dataset_summary.record_id_digest
--     （extract_mart.py:514-518）。对新 schema 跑一次同等全量读，
--     重算 digest 与最近一次成功 run 的摘要比对，相等即内容一致。
SELECT dataset_name, record_id_digest, completed_at
FROM mart_ops.sync_dataset_summary
WHERE source_name = 'extract'
ORDER BY completed_at DESC;

-- (d) 业务列抽查（金额类事实必跑）：SUM/CRC 级对账
SELECT
  (SELECT COALESCE(SUM(sales_amount),0) FROM mart_ops.fact_daily_report_offline) AS old_amt,
  (SELECT COALESCE(SUM(sales_amount),0) FROM mart_facts.fact_daily_report_offline) AS new_amt;

-- (e) outbox 切换前准出：无在途行（或已人工搬移）
SELECT status, COUNT(*) FROM mart_ops.robot_outbox GROUP BY status;
```

准出门槛：(a)(b) 全表通过；(c) 每数据集 digest 相等；(d) 金额类事实逐表通过；(e) `pending` 为 0 或已搬移并复核。

---

## 3. 回滚预案

### 3.1 分阶段回滚（每个灰度阶段都有独立回退路径）

| 阶段 | 回滚动作 | 数据损失面 |
|---|---|---|
| 新 schema 已建、开关全 `off` | 直接 `DROP SCHEMA` 新库，无任何运行影响 | 无 |
| 双写中（`dual`） | 开关回 `off`；新库是冗余副本，可弃 | 无（旧库一直是全量真源） |
| 已切读（`*_READ=new`）、双写仍在 | 读开关回 `old` | 无（旧库仍被双写保持同步） |
| 已停旧写（`WRITE=new`）、旧表未删 | 写开关回 `dual`，用 `INSERT ... SELECT` 从新库回补旧库在「单写新」窗口的增量，重跑 §2.7 校验 | 需精确记录停旧写时间点 |
| 旧表已删 | **不可回滚**，只能靠备份恢复。因此旧表退役必须在「切读稳定运行 ≥ 2 周 + 一轮完整月末对账」之后 | — |

### 3.2 mart_queue 专项回滚（排空-搬表-切换的逆操作）

1. Nacos 注册表关停 robot / dingtalk-gateway（既有请求级门控，`mart_cli.py:103-110` 同款机制）；
2. `pending=0` 或 `INSERT INTO mart_ops.robot_outbox SELECT * FROM mart_queue.robot_outbox WHERE ...` 搬回在途行（`dedupe_key` 主键天然幂等防重）；
3. `MART_SPLIT_QUEUE=off` 回切，重启两服务；
4. 语义安全：outbox 是**至少一次**投递（`delivery.py:11-13`），搬移造成的重复入队由 `dedupe_key` 唯一键吸收（`outbox_repository.py:6-8`），不会产生重复群消息。

### 3.3 mart_dims 回滚

RENAME 是可逆原子操作：`RENAME TABLE mart_dims.dim_x TO mart_ops.dim_x`。切读开关回 `old` 后 extract 下次运行仍写旧表，维度无缝续跑。

---

## 4. 「不做什么」（本轮明确不立项项）

| # | 不做项 | 理由与依据 |
|---|---|---|
| 1 | **跨实例拆分 / 独立扩缩容** | settings.py 单 host 假设（`settings.py:109-114`）；跨实例打断 dim×fact JOIN（`daily_report.py:230-242`）；当前数据规模无容量需求。§8.4 的「独立扩缩容」收益本轮明确放弃 |
| 2 | **任何迁移落地** | 本轮只出设计与开关；settings.py、live_migrations、各服务代码零改动 |
| 3 | **settings.py 修改** | 红线。§2.2 的环境变量只是建议稿，落地须 main 批准后进后续批次 |
| 4 | **引入 stage_* 中间层** | 见 §5，结论：不引入 |
| 5 | **消息队列（Kafka/RabbitMQ）立项** | P2，§8.3 #6；只给接口预留建议（§6） |
| 6 | **docker-compose 变更** | 红线（他人独占）且本设计不需要：新库走环境变量缺省推导即可 |
| 7 | **bi-web API 版本化深化、Redis 可观测** | §8.3 #4/#5，非本任务范围（#4 的 `/api/v1` 路由已存在于 `app.py:13-37`，状态勘误已写回 §8.3） |
| 8 | **控制面表迁出 mart_ops** | `sync_runs` / `sync_dataset_summary` 被 sync 与 extract 双方写、被巡检读，语义上不属于 facts/dims/queue 任何一方；mart_ops 收敛为控制面库即清晰，不另立 `mart_control` 折腾 |
| 9 | **dim 替换的删除语义改动** | `replace_dim_calendar` / `replace_dim_robot_member` 的删除语义（被移除的月份/离职成员必须消失）**不是债、必须保留**——main 已裁决 pipe-agent 的剪枝必须覆盖「种子缩容删残留」方向。本轮 pipe-agent 做写路径的「upsert + NOT IN 剪枝」等价 upsert 化以降低写放大，full-rebuild 开关保留作回退；拆库设计不重复立项 |

---

## 5. stage_* 中间层评估（§8.2 目标架构）：**结论——不引入**

不骑墙，本轮明确**不引入 stage_* 中间层**，依据四条：

1. **raw 层已经承担 stage 的职责**。raw 按 stable_id 幂等去重（BI建设指南 §2.2.5）、来源原样落库且是唯一可重放层（`extract_mart.py:11-12`）；列清洗发生在 extract 读取侧的白名单投影（`extract_mart.py:142-156`：「作废列与钉钉技术列在 SQL 层就已排除」）。stage 层最典型的两个职责——「可重放」与「列级清洗」——已被 raw + extract 读取侧分摊完毕。
2. **当前投影没有值得独立一层的转换**。`EXTRACT_DATASETS`（`mart_extract_schema.py:58-174`）绝大多数是 1:1 列改名；仅有的真转换是 `melt_store_funds` 与 `order_line_expand` 两个 kind，已在投影器函数内封装、可单测。为两个函数单建一层库表，是把代码组织问题错当成数据架构问题。
3. **数据规模不支撑三层跳**。BI建设指南 §3.1 实测：最大表 2804 行。raw → stage → mart 三跳会把 T+1 链路的失败点、审计面、迁移面全部 ×1.5，收益为零。
4. **roadmap 已有正确的承接位**。B2 批次的「extract 聚合架构」（BI建设指南 §5）若引入多源聚合（库存/物流）、SCD2 维度或重清洗规则，届时 stage 的价值会真实出现——**触发条件：出现「一次清洗、多处消费」的共享中间结果，或单表投影行数超百万级**。届时重评，现在建是提前优化。

一句话：raw 即 stage。本设计的 mart_facts / mart_dims / mart_queue 拆分与「不引入 stage」互不依赖，可独立成立。

---

## 6. P2 消息队列接口预留建议（robot_outbox → Kafka/RabbitMQ，本轮不立项）

`OutboxRepository`（`outbox_repository.py:31-143`）已是天然接缝，建议保持其四方法契约**逐字稳定**，未来 MQ 化时在接缝下方换实现，不动调用方（robot / gateway）：

| 现契约 | MQ 化映射 |
|---|---|
| `enqueue(...)` → INSERT IGNORE | producer.send，key = `dedupe_key`（broker 层幂等/去重的锚点，沿用「唯一键承载幂等」的既定语义） |
| `fetch_pending(limit, max_attempts)` | consumer.subscribe（推模式替代轮询） |
| `mark_delivered` | 投递成功后 ack |
| `register_failure` / `max_attempts=5` | nack + broker 重试策略；达上限转 DLQ（对应现 `failed` 终态 + 控制面告警） |

> 2026-09-16 补记（delivery-agent 确认）：本轮投递状态机收尾后四方法签名逐字稳定，新增 `list_failed` / `requeue`（failed 行人工检视与重投）——MQ 化时对应 DLQ 的检视/重投工具，不影响上表映射；`report_intake` 直写事实表路径未动，债 #5 维持原判。

预留动作（仅建议，本轮不做）：

1. 抽 `OutboxTransport` 协议类（enqueue/fetch/ack/nack 四方法），`OutboxRepository` 作为其 MySQL 实现归位；
2. **保留 `robot_outbox` 表**作为审计与兜底（transactional outbox 本意）：MQ 化后 enqueue 仍落表，投递状态异步回写， broker 故障时 gateway 可回落轮询模式——与现有「至少一次 + dedupe_key」语义完全兼容；
3. 引入 MQ 的触发条件建议写死：「日投递行数 > 1 万」或「consumer 数量 > 2」。当前一个 gateway 单 consumer、日数十行，MQ 是纯负资产。

---

## 7. 架构债台账

| # | 位置 | 影响 | 建议优先级 | 本轮是否做 |
|---|---|---|---|---|
| 1 | `mart_ops` 多职责混合：事实+维度+队列+控制面同库（`live_migrations.py:246-260` 的 8 个 mart 迁移版本） | 无法按职责授权/备份；bi-web「只读 mart」实际也能摸到 outbox 与控制面 | P1 | **本轮出设计（本文档），不落地** |
| 2 | `fetch_unfilled_members` 跨 dim×fact 单条 SQL JOIN（`daily_report.py:230-242`） | 把拆分粒度锁死在同实例；跨实例方案必须重构此查询 | P1 | 本轮文档化（§1.2 约束 2） |
| 3 | `read_dataset` 全表扫描 raw（`extract_mart.py:142-156`） | raw 增长后 extract 线性变慢 | P0（进行中） | 否——pipe-agent 本轮增量改造 |
| 4 | `upsert_fact` 逐行 `cursor.execute`（`extract_mart.py:174-179`） | 大表投影时 N 次 round-trip；应 `executemany` 或批量 VALUES | P2 | 否，后续批次 |
| 5 | **文档与现实漂移**：`extract_mart.py:3-4` 宣称「提取层是业务 mart 表的唯一生产者」，但 gateway `report_intake.py:221-229` 直写 `fact_daily_report_offline` | 照文档做拆库/迁移会漏掉 gateway 这个写入者，导致双写漏写 | P1 | 本轮记录并写进 §2.4/§2.5（gateway 纳入双写面） |
| 6 | 控制面表（`sync_runs`/`sync_dataset_summary`）与业务表同库 | 巡检查询与业务查询互相影响；控制面无法独立保留策略 | P1 | 随拆库收敛（mart_ops 转控制面，§2.1） |
| 7 | live_migrations 校验和冻结 DDL 演进（`live_migrations.py:297-301`） | 任何既有表结构/归属调整必须新增版本，不可改旧版本——是保护也是摩擦 | P2 | 否，设计已遵循（§2.3） |
| 8 | §8.3 状态过时（outbox 状态机、fact upsert 已完成仍标「当前=旧状」） | 后人照过时文档返工 | P0 | **本轮已修（BI建设指南 §8.3 状态列）** |
| 9 | bi-web API 版本化 | URL 前缀 + `API-Version` header 双通道本轮落地中（默认 v1、未知版本 404，api-agent） | P2 | 进行中（api-agent 本轮交付） |
| 10 | Redis 缓存 fail-open 无命中率指标 | ~~无法评估缓存价值~~ 已有 `CacheMetrics` hit/miss/error/耗时计数 + `/diagnostics/cache` 只读端点 | P2 | **本轮已完成（api-agent）** |
| 11 | robot_outbox 表轮询，consumer 无法水平扩展 | 单 gateway 单 consumer 是投递瓶颈上限 | P2 | 否——§6 接口预留 |
| 12 | 投影类 7 个数据集仍全量替换（pipe-agent 输入）：5 × snapshot/melt（`fact_fin_*`，`replace_table`）+ `dim_product` 全量镜像 + `fact_order_line` payload 展开（投影器在 `extract_finance.py` / `extract_order_line.py`） | `replace_table` 无稳定行级键、源侧无水位（raw_wdt.dim_product / wdt_records 均无 updated_at）；增量化须先解「目标表稳定键 + 源侧水位」两个前置，未增量前每轮 extract 对这些表整表 DELETE+INSERT，写放大随数据增长 | P2 | 否，后续批次；本轮 pipe-agent 的 digest 跳过 + skipped 列机制可直接复用 |

---

## 8. 落地批次建议（供 main 排期，非本轮执行）

| 批次 | 内容 | 前置 |
|---|---|---|
| M1 | settings.py 增三可选环境变量 + `mart_routing.py` 开关解析 + 迁移 target 三新 key | 本文档评审通过 |
| M2 | mart_queue 迁移（排空-搬表-切换）+ robot/gateway 连接分离 | M1 |
| M3 | mart_dims 迁移（RENAME）+ `daily_report.py` 限定名改造 + extract 三连接 | M2 |
| M4 | mart_facts 双写 → §2.7 校验 → 切读 → 稳定 2 周 → 停旧写 → 旧表退役 | M3 |

每批验收以 §2.7 校验 SQL 全绿 + 既有单测/合约测试全绿为准。
