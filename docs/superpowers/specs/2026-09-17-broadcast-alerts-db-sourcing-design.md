# 播报层强制走 DB 建设方案（含缺数回填）

日期：2026-09-17 · 范围：`digital-ops`（机器人播报 / `common.public_data` / `common.bi_web`）
处理对象：库存补货提醒、已超卖、订单风险防控、采购入库提醒（四域，对应 17:00 / 15:00 / 10:00 三个播报）
确立的处理原则（2026-09-17 返评）：
- **一切以 BI 为主**：BI 看板是经营数据的主出口，播报只是 BI 指标的推送形态之一
- **DB 取不到时，禁止回退直连 WDT**（用户确认）：`fallback` 会把临时权宜永久化，是口径分裂的成因
- **BI 的目的之一就是减少群播报**：一天五次的定时播报应收敛，只保留"需要人立刻行动"的异常

输入：用户要求「跨播报的读写层**必须有强制要求走 DB**；DB 里没有数据时，**先补齐 DB 里的数据**」
基准文档：`docs/superpowers/specs/2026-09-16-broadcast-bi-unification-design.md`、`docs/BI建设指南.md`、`docs/db-大表治理手册-2026-09-17.md`

---

## 0. 一句话结论

**读写层必须是 DB，不能是文件快照**——否则等于在已经分裂的三套口径之外，再造第四套。

而"走 DB"这件事，在"以 BI 为主、播报要收敛"的前提下，从"最好这么做"升级为**非做不可**：播报一旦收缩，BI 看板就成了这些数据的**唯一出口**，数据不落库，业务就真的没地方看了。换句话说，**DB 化不是 BI 的前置优化，是播报能安全关掉的前提**。

同时，**"缺数据"是常态而非异常**：三条 WDT 线的 sync 是定时窗口拉取，天然会有空洞。因此这套设计里「回填」不是一次性补数动作，而是**每次播报前的强制前置检查**（先补齐，再播报；补不齐就明确告知延迟，而不是偷偷改直连 API）。

落到现有底座的方式：**不改现有的 raw → mart 分层架构，只补它缺的表**。播报层从"调 API 的客户端"降级为"读 mart 的消费方"。

---

## 1. 为什么不能做成文件快照（否决理由）

我最初实现了一版 `snapshots/<domain>-<date>.json` 的文件快照层，已删除。否决它的理由同时也是"为什么必须走 DB"的理由：

| # | 问题 | 后果 |
|---|---|---|
| 1 | **口径分裂会固化** | 播报一侧写文件、BI 一侧读 mart，同一个"超卖数"两边各算一次。这正好是基准稿 §2 已经点名的分裂点，快照层会把它从临时现象变成长期结构 |
| 2 | **不可 join** | 文件快照失去了和 `dim_product`、`dim_calendar`、`fact_channel_daily_sales` 关联的能力。而用户要的四域本来就互相咬合——超卖 SKU 是不是在搞促销？入库了为什么还缺货？这些问题在文件里答不了 |
| 3 | **部署不共享** | 现在跑在本机，计划迁 ECS（`README_ECS部署指南.md`）。文件系统快照在多实例/换机场景下会静默丢失，历史无从回看 |
| 4 | **重复造轮子** | `sync_runs` / `sync_dataset_summary` 已经把"时间、行数、成败、水位"四要素做完（`live_migrations.py:90-115`），文件方案要自己再实现一遍状态机 |
| 5 | **DDL 门禁绕不开** | 想给文件方案加索引/约束做不到；而 DB 方案新表有成熟通道 |

---

## 2. 现状：DB 里到底有什么、缺什么

### 2.1 底座（已就绪，可直接依赖）

| 项 | 事实 | 位置 |
|---|---|---|
| 数据库 | MySQL 8.4（阿里云 RDS），`pymysql`，`autocommit=False` + `DictCursor` | `common/public_data/db.py:10-20` |
| 分层 | `raw_dingtalk` / `raw_wdt` / `raw_manual` → `mart_ops` | `settings.py:32-39` |
| 建表通道 | `_MIGRATIONS` 注册表 + 版本号 + sha256 校验和，**改文本即启动失败** | `live_migrations.py:314-391` |
| 同步状态 | `sync_runs`（5 态状态机）、`sync_dataset_summary`（行数/摘要/成败） | `live_migrations.py:90-115` |
| 水位 | `last_extract_started_at()`，刻意用 `started_at` 而非 `completed_at`（避免漏掉运行期新写入的行） | `extract_mart.py:398-418` |
| 增量跳过 | `record_id_digest` 比对，相同则 skipped=1 但**仍写摘要行**，审计不丢 | `extract_mart.py:420-434`、`:683-692` |
| CLI | `migrate` / `live-sync` / `rebuild-projection` / `extract-mart` | `cli.py:736-746` |

> `_test` 后缀铁律（`settings.py:112-117`）：新增任何库都必须进 `database_names`，否则绕过校验。**本方案不新增库**，四域全部落在既有的 `mart`。

### 2.2 四域缺口（决定性的三个）

| 域 | DB 现状 | 够不够做播报 |
|---|---|---|
| 库存补货 / 已超卖 | ❌ **无专用表**。WDT 已在拉（manifest 29 个 dataset，`source-manifest.json:2791-3138`），但全落在 `wdt_records.payload_json` 整包 JSON，无下游投影 | 不够。字段 `available_send_stock` / `num_7days` / `num_month` 只活在 `.py` 里 |
| 采购入库 | ❌ **无专用表**。同上（`source-manifest.json:2443-2790`，29 个 dataset） | 不够。`details_list[]` 明细埋在 JSON 里，无法按 SKU / 时间聚合查询 |
| 订单风险防控 | ⚠️ **有表但缺关键字段**。`fact_order_line` 已存在（`mart_extract_schema.py:399-420`，含 v2 的 `shop_name`/`channel_name`，`:436-441`） | **卡在一个字段**：风控判定依赖 `receiver_area`（`order_risk_alert.py:102`、`normalize_area()` 在 `:83-95`），而 `fact_order_line` **不含该列**。风控目前无法走 DB |
| 渠道日报（对照组） | ✅ `fact_channel_daily_sales` 已有（`mart_extract_schema.py:212-228`），但 11:00 播报仍直连 AI 表 | 数据已在，只差切换消费端 |

**结论**：四域里三域要**新建/加列**，一域（销售）只需**切换**。工作量集中在 L1 明细层的三张落地。

---

## 3. 目标架构：播报只读 mart

```
  ┌─ L0 raw（已存在，不动）──────────────────────────────────┐
  │  wdt_records   (source_method, source_record_id, payload_json) ← 整包 JSON 快照 │
  │  channel_daily_sales_*                                                          │
  └───────────────────────────┬──────────────────────────────────┘
                              │ extract (投影 / 抽列)
  ┌─ L1 mart 明细（★本方案新建/加列）────────────────────────┐
  │  fact_inventory_sku_daily   库存+超卖（日快照，可 join）       │
  │  fact_purchase_inbound      采购入库明细（事件，append 语义）  │
  │  fact_order_line            ★ALTER 加 receiver_area           │
  │  fact_channel_daily_sales   已存在，直接消费                   │
  └───────────────────────────┬──────────────────────────────────┘
                              │ 指标口径层（唯一实现）
  ┌─ L2 播报/BI 共享层 ──────────────────────────────────────┐
  │  bi_alert_queries(): 补货清单 / 超卖清单 / 风控 grouping / 入库汇总 │
  │  ├─ 17:00 库存预警播报 ─┐                                    │
  │  ├─ 15:00 订单风控播报 ─┤ 同一函数 → markdown 形态           │
  │  ├─ 10:00 采购入库播报 ─┤                                    │
  │  ├─ 11:00 渠道日报   ─┘                                     │
  │  └─ bi-web 看板四卡（同一函数 → 图表形态）                    │
  └──────────────────────────────────────────────────────────┘
```

关键约束：**播报层禁止再持有 `WdtClient` 调用**。今天 `stock_alert.py:70`、`purchase_alert.py:66`、`order_risk_alert.py:180` 都在直调 API，这是要消掉的东西。

---

## 4. 表设计（DDL 为评审稿，最终以 `live_migrations` 登记文本为准）

### 4.1 `fact_inventory_sku_daily` —— 库存补货 + 已超卖

超卖不是独立的数据源，它就是库存的一个状态（可发 ≤ 0），**不应单独建表**，与补货共享一张日快照。

```sql
CREATE TABLE IF NOT EXISTS `fact_inventory_sku_daily` (
  `business_date`   DATE NOT NULL,
  `spec_no`         VARCHAR(100) NOT NULL,
  `warehouse_scope` VARCHAR(20)  NOT NULL,   -- ALL / 习水村 / 杭易（跨仓合并口径标识）
  `goods_name`      VARCHAR(500) DEFAULT NULL,
  `available_qty`   DECIMAL(20,4) NOT NULL,  -- 可发库存（源字段 WDT available_send_stock）
  `stock_qty`       DECIMAL(20,4) DEFAULT NULL,
  `qty_7days`       DECIMAL(20,4) DEFAULT NULL,
  `qty_month`       DECIMAL(20,4) DEFAULT NULL,
  `purchase_intransit_qty` DECIMAL(20,4) DEFAULT NULL,
  `daily_avg`       DECIMAL(20,4) DEFAULT NULL,  -- 投影时算好，见 §4.5
  `days_left`       DECIMAL(10,2) DEFAULT NULL,  -- 可售天数
  `stock_state`     ENUM('HEALTHY','URGENT','OVERSOLD','DEAD') NOT NULL,
  `synced_at`       DATETIME(6) NOT NULL,
  `sync_run_id`     CHAR(36) NOT NULL,
  PRIMARY KEY (`business_date`,`spec_no`,`warehouse_scope`),
  KEY `idx_state_days` (`stock_state`,`days_left`),
  KEY `idx_spec` (`spec_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
```

- **为什么 `business_date` 进主键**：`wdt_records` 是最新快照语义（PK 无日期，跨窗口覆盖，见 `raw_repository.py:263-264`），直接投影到无日期主键的表会丢历史。库存虽然业务上"只关心最新"，但**有没有恶化要走历史**（对应现有 `OVERSELL_WORSEN_THRESHOLD` 恶化再报逻辑，`stock_alert.py:172-181`）。
- `stock_state` 在投影期一次性算死，**播报和看板都必须用它**，杜绝两套库存口径（基准稿 §2 #1）。

### 4.2 `fact_purchase_inbound` —— 采购入库

```sql
CREATE TABLE IF NOT EXISTS `fact_purchase_inbound` (
  `purchase_no`   VARCHAR(64) NOT NULL,
  `spec_no`       VARCHAR(100) NOT NULL,
  `warehouse_name` VARCHAR(50) DEFAULT NULL,   -- 由 WAREHOUSE_MAP 01/12 翻译后存中文
  `goods_name`    VARCHAR(500) DEFAULT NULL,
  `qty`           DECIMAL(20,4) NOT NULL,
  `stockin_time`  DATETIME(6) DEFAULT NULL,    -- 业务发生时间
  `status`        VARCHAR(20) DEFAULT NULL,
  `synced_at`     DATETIME(6) NOT NULL,
  `sync_run_id`   CHAR(36) NOT NULL,
  PRIMARY KEY (`purchase_no`,`spec_no`),
  KEY `idx_stockin_time` (`stockin_time`),
  KEY `idx_spec_time` (`spec_no`,`stockin_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
```

**必须是 append 语义**（区别于库存快照）：采购是事件，同一张单+同一个 SKU 只出现一次，`purchase_no|spec_no` 天然幂等。现有去重键（`purchase_alert.py:76`）与之一致，可直接搬。

### 4.3 `fact_order_line` 加列 —— 风控的唯一阻塞点

```sql
ALTER TABLE `fact_order_line`
  ADD COLUMN `receiver_area_raw` VARCHAR(255) DEFAULT NULL,
  ADD COLUMN `receiver_area_norm` VARCHAR(64) DEFAULT NULL,
  ADD KEY `idx_order_line_area` (`receiver_area_norm`);
```

- **新版本号**（`mart-extract-order-line-v3`），**不得改写 v1/v2 文本**——存量库校验和不匹配会直接拒绝启动（`live_migrations.py:373-379`；`mart_extract_schema.py:430-435` 有同样的教训）。
- `receiver_area_norm` 由 `order_risk_alert.py:83-95` 的 `normalize_area()` 逻辑在**投影期算好落库**（三级省市区拼接、去除相邻重复项），避免风控查一次做一次字符串处理。
- ⚠️ **拼多多口径要承认并落进数据**：API 按隐私协议不返回地区（`order_risk_alert.py:8-11`），`receiver_area_norm` 为 NULL。现有播报把它单列提示。落库后这条提示应由查询层明确返回 `pdd_no_area_count`，而不是让看板静默少订单。

### 4.4 `broadcast_dedup` —— 去重状态搬进 DB

现状：`stock_alert.py` 用 `.stock_alert_state.json`、`purchase_alert.py` 用 `.purchase_state.json` 做"今天已报过"的去重。**换机器/多实例即失效，会导致重复播报或漏报**。

```sql
CREATE TABLE IF NOT EXISTS `broadcast_dedup` (
  `domain`        VARCHAR(50) NOT NULL,     -- stock_alert / purchase_alert / order_risk
  `dedup_key`     VARCHAR(255) NOT NULL,    -- spec_no / purchase_no|spec_no / shop|area|date
  `business_date` DATE NOT NULL,
  `last_value`    DECIMAL(20,4) DEFAULT NULL,  -- 上次缺口值，用于恶化判定
  `last_sent_at`  DATETIME(6) NOT NULL,
  `meta_json`     JSON DEFAULT NULL,
  PRIMARY KEY (`domain`,`dedup_key`,`business_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
```

### 4.5 口径一致性的硬要求

| 指标 | 现有（分歧） | 落库后（唯一） |
|---|---|---|
| 日均动销 | `stock_alert.py` 用 7 天；BI 口径用 30 天×15 天安全库存 | `daily_avg` 在投影层按**统一 windows 规则**算好后入库，两端口都读这一列 |
| 可售天数 | 播报自算 `available/动销` | `days_left` 同上 |
| 是否超卖 | 播报谓词 `available<=0` + 动销兜底（`stock_alert.py:126-131`） | `stock_state='OVERSOLD'`，投影层一次性判定 |

> 口径应尽量 push down 到 SQL（参照 `mart_extract_schema.py` 既有做法），而不是在 Python 里循环。

---

## 5. 缺数回填：**先补 DB，再播报**

这是用户提的核心要求。设计为三层，按触发时机区分：

### 5.1 L0 —— 播报前强制前置检查（每次都跑）

每个播报在取数前先问 DB："我要的那天数据齐了没？"

判定依据复用现有水位机制，不要另发明：

```
SELECT s.`dataset_name`, s.`records_read`, MAX(r.`started_at`)
FROM `sync_dataset_summary` s
JOIN `sync_runs` r ON r.`sync_run_id` = s.`sync_run_id`
WHERE s.`dataset_name` IN (...) AND r.`status` = 'completed'
GROUP BY s.`dataset_name`
```

- **有数据** → 正常往下走；
- **无数据 / 状态非 completed** → 触发 **L1 补数**；
- **L1 也补不出来** → 走 §7 降级（**不许改直连 API**）。

### 5.2 L1 —— 窗口补数（增量，分钟级到小时级）

复用现有 sync 能力，只限定时间窗：

- WDT 已有 `max_window_minutes=50` 的硬约束（`manifest.py:319-402`），且**同 method 的窗口不可重叠**。补数时要按此切片，不要试图一次拉三天。
- 库存是"最新快照"语义，补当天一个窗口即可；
- 采购入库要按 `stockin_time` 补齐到"最后一次成功同步时刻"，逐窗追平；
- 每次补数记 `sync_runs`（新建 run_id）+ `sync_dataset_summary`，**复用现有回滚/重试语义**，不做特殊通道。

### 5.3 L2 —— 历史回填（一次性 / 追赶用）

- **有利现实**：`source-manifest.json` 里 WDT 三个 method 各已登记 **29 个窗口 dataset**（`_0000`~`_0028`）。历史回填不必重新设计切分，直接按这些窗口回放即可。
- 回填范围由业务决定保留多久（建议：**采购入库 ≥ 90 天**（要算周转）、**库存日快照 ≥ 30 天**（要看恶化趋势）、**订单 ≥ 已有时长**）。
- **执行纪律**：回填 = 重复执行受控窗口的 sync，**不是新通道**。它必须产生同样的 `sync_runs` 记录，否则审计链断裂。
- **进度可观测**：每完成一个 dataset 写一条 summary，看板/运维页能直接看到"回填到第几段"。

### 5.4 回填必须避开的两个坑

1. **`wdt_records` 覆盖语义**：它的 PK 是 `(source_method, source_record_id)`，不含时间（`raw_repository.py:263-264`）。回放历史窗口会**把最新值覆盖成旧值**，导致"回填之后当前库存反而变旧了"。因此历史回填**只能写 L1 的事实表，不允许回写 `wdt_records` 的投影入口**，或者至少保证回填窗口严格升序、最后一次一定是最新窗口。
2. **`stock_state` / `daily_avg` 的时间对齐**：它们是在投影时算的。回填旧数据时要用**当时的窗口**重算，不能拿今天的动销去标昨天的状态。否则历史全部失真（回看"上周就超卖了"这类结论会错）。

---

## 6. 播报侧改造：只留异常告警，其余交 BI

已确立：**BI 的目的之一就是不再频繁播报**。所以这一轮不是把五个播报做得更漂亮，而是**给它们做减法**。

### 6.1 五条播报的处置建议

处置原则：**定时 → 事件驱动；清单型 → BI；只有"需要人立刻动手"的才值得群推送。**

| 播报 | 现状 | 建议 | 理由 |
|---|---|---|---|
| 09:00 热卖品监控 | 定时推 Top15 + 危险品保底 | **取消**，并入 BI 的 SKU 驾驶舱 | 纯"看一眼"的清单不是告警；registry 里已有 `pie_sku_mtd` / `table_sku_hot_total` / `table_sku_hot_brand` / `table_sku_hot_channel` / `kpi_sku_mtd`（`cards.py:138-154`） |
| 10:00 采购入库提醒 | 定时推近 24h 明细 | **取消**，改为 BI 明细表 | 入库是既成事实，没有"再通知一遍"的价值；争议时才需要回头查 |
| 11:00 渠道日报 | 定时推全渠道 | **取消定时**，BI 看板常驻；仅保留"目标进度落后 ≥ N pt"触发式告警 | 看板随时可看；真正需要打扰人的是"掉队了"，不是"今天数据出来了" |
| 15:00 订单风控 | 定时推风险分组 | **保留**，改为超阈值触发 | 唯一具备"发现即可止损"性质的域，值得打扰人 |
| 17:00 库存预警 | 定时推补货清单 | **降级为事件驱动**：仅当日新增超卖 / 断货恶化时推 | 常规补货清单属日常运营，BI 自查即可 |

**结果：一天固定 5 条 → 常态 0 ~ 1 条。** 群回归"有事才响"，其余交给随时可查的 BI。

### 6.2 告警判定必须落在查询层，不能脚本算了再决定发不发

触发式告警的门槛必须是 **SQL 的一部分**（同 §4.5 的口径 push down），这样同一个"是否异常"的判断在群里和看板里一致。否则会出现：群说"落后 14.3pt 告警"，看板标黄色，两边各判一次。

### 6.3 读取契约（强制）

1. 播报脚本**不得** `import WdtClient`、不得出现任何上游 API host；
2. 只允许通过统一查询层（建议 `common/broadcast/queries.py`，与 `common/bi_web/queries.py` 同构）读 mart；
3. 该查询层同时被 bi-web 复用 → **群里那行数字和看板那个柱子出自同一个 SELECT**（基准稿 §3 的目标落地于此）；
4. 返回值必须带 `as_of`（数据时间）与 `stale` 标记，播报文案里要显示"数据截至 HH:MM"，避免跨天延迟被误读。

---

## 7. 降级纪律（重要，避免口径复发）

> 本条已经过确认并采纳为硬纪律（2026-09-17 返评）：**DB 取不到时不允许退回直连 WDT / AI 表**。理由很直接——今天的分裂就是这么来的，`fallback` 会把临时的权宜永久化。

与 §6.1 叠加后含义更强：既然播报要收敛成"异常才响"，那么**因为一次数据缺失就回退到直连**（进而引入第二套口径）的收益远小于风险。宁可这一次不响。

替代做法：

| 场景 | 行为 |
|---|---|
| 当日数据未到 | 发"数据延迟"提示卡（写明缺失域、最后成功同步时间），**不发可能错的旧数据** |
| 部分域缺失 | 发已有域，缺失域标"暂缺"，整条不发/部分发由业务定 |
| DB 连接失败 | 报错 + 告警，进程退出码非 0，由 `daily_scheduler` 记 `✘`（现有机制已有），下一次调度自然重试 |

---

## 8. 迁移步骤与验收

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **P0 分支整理** | 主工作树里 `stock_alert.py:35` / `purchase_alert.py:32` / `order_risk_alert.py:35` 仍是 `from common.test_group import ...`，而该模块在 `common/` 重构后已迁到 `common/dingtalk/test_group.py`（旧分支 `.worktrees/.../common/test_group.py` 里仍对）。**这只影响本分支能否就地运行与本地验证，不构成线上故障**——正式环境调度的是另一份已上线的部署（`README_ECS部署指南.md` §四 / 附二：ECS 上是 `scp` 过去的旧快照，且不含本轮的四个新播报） | 本分支四个 `--dry` 可跑，用于 P3/P4 的数字比对 |
| **P1 表就绪** | 在 `live_migrations._MIGRATIONS` 登记 3 张新表 + 1 条 ALTER（新版本号）；DDL 必须在白名单文件内，否则 `test_db_ddl_gate` 报红 | `python -m common.public_data.cli migrate` 幂等通过，重复执行无变更 |
| **P2 投影** | 新增 `wdt_records` → 两张事实表的 extract 数据集，写入 `EXTRACT_DATASETS`（`mart_extract_schema.py:58-174`） | `extract-mart` 跑完，`records_read > 0`，`sync_dataset_summary` 有行 |
| **P3 回填** | 按 §5.3 回放窗口，补满建议保留期 | 覆盖期内每个业务日都能查到；且看"昨天 vs 今天"的 `days_left` 趋势是真实变化 |
| **P4 切换** | 播报脚本替换为读查询层；删除 `WdtClient` 依赖 | 四个播报输出与改造前**逐行可比**（允许排序/文案变化，数字必须一致） |
| **P5 BI 复用** | bi-web 新增四张卡，与播报共用同一查询函数 | 改一个 SQL，群消息和看板同时变 |
| **P6 去重入库** | `.stock_alert_state.json` / `.purchase_state.json` → `broadcast_dedup` | 连续跑两次不重播；换机器后仍能识别今日已播 |

**P4 的数字比对是硬验收**：口径统一的目的不是"看起来统一"，是"同一个数只有一种算法"。

---

## 9. 风险与待裁决

| # | 风险/开放问题 | 建议 |
|---|---|---|
| 1 | **动销窗口分歧（7 天 vs 30 天）** 基准稿已列为待裁决项 | 必须先有业务结论才能定 `daily_avg` 的算法；库存预警与 BI 两边长期口径不一，这是最容易引发客诉的点 |
| 2 | 库存日快照体量：SKU 数 × 保留天数，可能撞上《db-大表治理手册》关注的大表问题 | 上线前估算行数；必要时降粒度（只保留有动销/有风险的 SKU 快照），或按业务日分区 |
| 3 | 拼多多地区字段缺失是源头限制，DB 层无法变魔术 | 数据层显式记录并 `-1` 标识；评估是否值得接拼多多开放平台补字段 |
| 4 | 回填期间旧窗口可能已被上游回收（WDT 保留期有限） | 启动前先试拉一个最老的小窗口验证可得性，再决定保留策略 |
| 5 | `receiver_area_norm` 的归一化规则要与旧 Excel 流程产出对齐 | 用旧流程导出的历史样本做一次双跑比对 |

---

## 10. 与既有计划的边界

- 本方案**不改** bi-web 的看板/卡片机制，只增加四张卡的数据来源；
- 本方案**不改** `raw_repository` / `wdt_read` / `manifest` 的现有行为，只新增 dataset 条目与投影；
- 11:00 渠道日报的改变最小（数据已在 `fact_channel_daily_sales`），可作为**第一个验证 P5 端到端的样板**；
- `数字化/钉钉/渠道日报机器人/channel-bi-page/` 目录目前是前一轮的临时渲染产物，若保留 HTML 生成能力，需先就"静态页由谁托管、link 如何进群消息"达成一致（当前尚无公网托管）。
