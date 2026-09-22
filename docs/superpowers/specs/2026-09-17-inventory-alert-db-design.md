# 库存动销 / 风险 SKU 的 DB 设计与 API→DB 过渡方案

日期：2026-09-17 · 范围：`wms.StockSpec.search2` 数据流（库存预警 / 已超卖）
前置文档：`docs/superpowers/specs/2026-09-17-broadcast-alerts-db-sourcing-design.md`（总方案，本文是其中 §4.1 的专项落地细化）
约束：**本文只出设计，不写实现代码、不实际拉取 WDT**（规模数字来自本机已有的 9/5 库存缓存快照，非新拉 API）

---

## 0. 结论

针对"动销"和"有风险"两类 SKU，把数据流从「脚本直连 WDT API」改造为「先落 `mart` 表、播报/BI 只读 DB」。

改造分两阶段（详见 §4）：
- **阶段一（双写）**：`stock_alert.py` 拉 API 后，把 forecast 结果额外入库，推送逻辑不变；
- **阶段二（只读切换）**：告警与看板改读 DB，API 拉取逻辑搬进 integration 投影层（`wms.StockSpec.search2` 在 manifest 已登记 29 个窗口 dataset，可直接复用）。

---

## 1. 哪些 SKU 进库（边界定义）

沿用 `stock_alert.py:117-143` 的 `forecast()` 判定，把"是否进库"显式成规则：

| 类别 | 判定 | 是否进库 |
|---|---|---|
| **动销** | `num_7days > 0` OR `num_month > 0` | ✅ 进库 |
| **紧急补货（风险）** | 可发 > 0 且 `可用/动销 ≤ 7天` 且 有动销 | ✅ 进库 |
| **已超卖（风险）** | 可发 ≤ 0 且（近7天动销 OR 近30天动销） | ✅ 进库 |
| **完全滞销** | `num_7days == 0` 且 `num_month == 0` | ❌ 不进库 |

> 关键不变量：**滞销复活不会静默丢失**。现有 `forecast()` 用 30 天口径兜底（断货 8 天后 7 天口径归零，但 30 天口径仍在）。只要保留这层兜底，一个 SKU 从"死库存"重新动销时，`num_month` 立刻 > 0，自动回到"动销"子集。所以"只存动销+风险"不会漏掉复活，这是可以放心收窄的前提。

### 1.1 实测规模（9/5 缓存快照，298 全量）

| 项 | 数量 | 占全量 |
|---|---|---|
| SKU 全量 | 298 | 100% |
| 有动销（7 或 30 天 >0） | 233 | 78% |
| 紧急补货（≤7 天） | 28 | 9% |
| 已超卖（可发 ≤0） | 22 | 7% |
| 完全滞销（不进库） | 99 | 33% |
| **进库子集（动销 OR 风险）** | **233** | **78%** |

> **重要发现**：单纯按"动销+风险"过滤，行数只压缩到 78%，收益有限。原因——超卖的 22 个几乎都在动销里，真正被剔除的是 99 个死库存。**如果目标是控大表，过滤不是主手段**（见 §3.2 备选）。

---

## 2. 字段映射（WDT 原生 → 表列）

`wms.StockSpec.search2`（`mask=1`）返回的原始字段与脚本内别名（`stock_alert.py:86-95`）不同，投影时必须对齐**原生字段名**，否则落库后行列错位：

| WDT 原生字段 | 脚本内别名 | 含义 | 表列 |
|---|---|---|---|
| `rec_id` | `spec_no` | 货品编码（唯一） | `spec_no` / PK 一部分 |
| `goods_name` | `goods_name` | 货品名 | `goods_name` |
| `available_send_stock` | `available` | 可发库存 | `available_qty` |
| `stock_num` | `stock_num` | 物理库存 | `stock_qty` |
| `num_7days` | `num_7days` | 近7天销量 | `qty_7days` |
| `num_month` | `num_month` | 近30天销量 | `qty_month` |
| `purchase_num` | `purchase_num` | 采购在途 | `purchase_intransit_qty` |
| （计算） | — | 跨仓合并标识（习水村+杭易，对应 `stock_alert.py:41` `WAREHOUSE_MAP`） | `warehouse_scope` |

> 跨仓合并：今天在脚本内把 01习水村 + 12杭易 求和成单条（`stock_alert.py:100-103`）。落库时保留 `warehouse_scope = 'ALL'`（合并口径）即可，BI 看板不需要拆仓视角；如需拆仓，另存两行 `习水村`/`杭易`。

---

## 3. 表设计

### 3.1 `fact_inventory_sku_daily`（降粒度子集版，本方案推荐）

只存"动销 OR 风险"的 SKU，新增 `is_moving` 标记，便于未来即便改全量也能瞬间过滤出关注集：

```sql
CREATE TABLE IF NOT EXISTS `fact_inventory_sku_daily` (
  `business_date`   DATE NOT NULL,
  `spec_no`         VARCHAR(100) NOT NULL,
  `warehouse_scope` VARCHAR(20)  NOT NULL,   -- ALL / 习水村 / 杭易
  `goods_name`      VARCHAR(500) DEFAULT NULL,
  `available_qty`   DECIMAL(20,4) NOT NULL,  -- 源 WDT available_send_stock
  `stock_qty`       DECIMAL(20,4) DEFAULT NULL,
  `qty_7days`       DECIMAL(20,4) DEFAULT NULL,
  `qty_month`       DECIMAL(20,4) DEFAULT NULL,
  `purchase_intransit_qty` DECIMAL(20,4) DEFAULT NULL,
  `daily_avg`       DECIMAL(20,4) DEFAULT NULL,  -- 按 §9 MOVING_WINDOW_DAYS 计算（窗口统一）
  `days_left`       DECIMAL(10,2) DEFAULT NULL,  -- 可售天数（同窗口口径）
  `moving_window_days` TINYINT NOT NULL DEFAULT 14,  -- 该日 days_left 所用窗口，可解释/可追溯
  `is_moving`       TINYINT(1) NOT NULL DEFAULT 0,  -- 是否动销子集
  `stock_state`     ENUM('HEALTHY','URGENT','OVERSOLD','DEAD') NOT NULL,
  `synced_at`       DATETIME(6) NOT NULL,
  `sync_run_id`     CHAR(36) NOT NULL,
  PRIMARY KEY (`business_date`,`spec_no`,`warehouse_scope`),
  KEY `idx_state_days` (`stock_state`,`days_left`),
  KEY `idx_moving` (`is_moving`,`stock_state`),
  KEY `idx_spec` (`spec_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
```

- `stock_state` 在投影期一次性算死，**播报与看板共用**，杜绝 §2 #1 的双口径分裂。
- `business_date` 进主键：`wdt_records` 是最新快照语义（PK 不含日期，跨窗口覆盖），直接投影会丢历史趋势（尤其"超卖持续天数"）。

### 3.1.1 日销明细表 `fact_sales_daily`（15 天窗口的数据基础）

为精确支持任意窗口（默认 **15 天**，用户裁定 9/17），不再依赖 WDT `search2` 的 `num_7days`/`num_month` 固定桶，改为**每日拉取 SKU×仓库×日期 的日销量落库，在 DB 内自行按窗口求和**。这样 `daily_avg = 近 N 天销量 / N` 是真实值、零插值误差；窗口 N 由 §9.2 的 `MOVING_WINDOW_DAYS` 配置。

```sql
CREATE TABLE IF NOT EXISTS `fact_sales_daily` (
  `business_date` DATE NOT NULL,
  `spec_no`       VARCHAR(100) NOT NULL,
  `warehouse_no`  VARCHAR(10)  NOT NULL,   -- 01 习水村 / 12 杭易（保留拆仓粒度）
  `qty_sold`      DECIMAL(20,4) NOT NULL DEFAULT 0,  -- 当日出库/销量
  `synced_at`     DATETIME(6) NOT NULL,
  `sync_run_id`   CHAR(36) NOT NULL,
  PRIMARY KEY (`business_date`,`spec_no`,`warehouse_no`),
  KEY `idx_spec_date` (`spec_no`,`business_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- **跨仓合并在查询层自然完成**：`PARTITION BY spec_no` 的窗口函数会自动累加该 SKU 在各仓的 `qty_sold`（§9.4）。

#### 数据源与每日拉取实现（已查代码核对 9/17）
**旗舰版 client 已打通**：`common/wdt/client.py` 的 `WdtClient.call(method, params)` 是通用通道（文件头注明"2026-09-03 实测验证通过"），任意已开通 method 直接 `client.call("xxx", params)` 即可，**无需新写客户端**。代码里已验证可用的销售类接口：
- `sales.TradeQuery.queryWithDetail`：`order_risk_alert.py:62-80` 的 `pull_orders` 已现成实现"按付款时间 60 分钟切片拉一天"；`extract_order_line.py` 已将其 `detail_list` 展开为 `fact_order_line`（`spec_no`+`num`+`trade_time`，且按 `trade_status` 剔除取消/全额退款）。

**日销来源三选一（口径对齐度 / 新增成本权衡）**：

| 路径 | 方法 | 仓库维度 | 口径 | 新增拉取 | 每日成本 |
|---|---|---|---|---|---|
| **A 复用现有数据** | 直接聚合 `fact_order_line`（`spec_no`+`quantity`+`trade_time`） | ❌ 无（按店铺/渠道） | 订单成交数量 | **零**（数据已在库） | 0 次 API |
| **B 出库明细（推荐）** | `wms.stockout.Sales.queryWithDetail`（`status_type=0` 发货时间） | ✅ `warehouse_no` | 销售出库量 | 需新拉 | 2 仓×24 切片 ≈ 48 次 |
| **C 聚合（最优调用）** | `vip_stat_sales_by_spec_shop_warehouse_query`（`consign_date`=昨日） | ✅ 店铺+仓库 | 发货量 | 需新拉（旗舰版待 `client.call` 一试） | **1 次** |

- **默认推荐 B**：与现有 `wms.StockSpec.search2`（库存、按仓）**同源同仓维度**，`days_left = 跨仓可发库存 / 出库日销` 口径最对齐；复用 `order_risk_alert.py:62-80` 的 60 分钟切片范式。
- **A 是零成本快速验证路径**：若业务认可"订单成交≈动销"，直接 `SELECT spec_no, DATE(trade_time), SUM(quantity) ... GROUP BY` 生成 `fact_sales_daily`，当天即可跑通 15 天窗口，无需新增任何 WDT 调用；代价是缺仓库维度、口径=订单非出库，需业务确认可接受。
- **C 调用最省但待试**：用通用 `client.call` 一试便知旗舰版是否开通；若开通则替代 B 成为最优。

- **写入逻辑**：对每个 `(spec_no, warehouse_no, 昨日)`（A 路径 `warehouse_no` 置空/合并）累加 `qty_sold` → `UPSERT` 入 `fact_sales_daily`；库存快照仍由 `wms.StockSpec.search2` 拉 `available_qty`（不变）。
- **净销量口径**：B/C 默认取发货量 `num`/`goods_count`（含赠品），是否剔除退款/退货待与运营确认（记入 §8 验收项）；A 路径 `fact_order_line` 已剔除 `trade_status∈{4,5,24}`（取消/待付/全额退款）。
- **保留期**：与库存表一致（如 90 天滚动），足够支撑 30 天窗口。
- `fact_inventory_sku_daily` 的 `qty_7days`/`qty_month` 降级为"WDT 原生口径对照冗余"，`days_left` 实际由本表算出（§9.4）。
- **历史回补**：A 直接重跑 `fact_order_line` 历史；B 受 60 分钟限制仅建议补近 30 天；C 按天逐日补。

### 3.2 备选：全量 + 过滤（当 78% 压缩不满足控表诉求时）

若《db-大表治理手册》对行数敏感，可**直接存全量 298**（不过滤），靠索引过滤：

- 优点：口径最完整、投影逻辑最简单（无需判定 is_moving）、复活零风险；
- 代价：行数 ≈ 298 × 保留天数（30 天 ≈ 8940 行/月），但**仍是很小的表**，远未到大表量级；
- 过滤靠 `idx_state_days` 或 `idx_moving` 即可，告警查询 `WHERE stock_state IN ('URGENT','OVERSOLD')` 走索引。

> 我的倾向：**直接用全量版**（§3.1 去掉 `is_moving` 过滤逻辑、保留该列作冗余标记即可）。78% 的收窄收益撑不起"过滤引入的口径边界复杂度"，而全量版在 298 SKU 规模下根本不是大表问题。请按 §7 验收时结合实际保留期裁定。

### 3.3 （可选）`fact_inventory_risk_event` —— 风险流水

把"超卖缺口扩大再报"（`stock_alert.py:170-181` 的 `OVERSELL_WORSEN_THRESHOLD` 状态机）搬进 DB，并支持"某 SKU 超卖持续 N 天"这类查询：

```sql
CREATE TABLE IF NOT EXISTS `fact_inventory_risk_event` (
  `event_date`    DATE NOT NULL,
  `spec_no`       VARCHAR(100) NOT NULL,
  `risk_type`     ENUM('URGENT','OVERSOLD') NOT NULL,
  `gap_qty`       DECIMAL(20,4) DEFAULT NULL,  -- 超卖缺口绝对值 / 紧急缺口
  `days_left`     DECIMAL(10,2) DEFAULT NULL,
  `first_seen_at` DATETIME(6) DEFAULT NULL,
  `resolved_at`   DATETIME(6) DEFAULT NULL,
  `synced_at`     DATETIME(6) NOT NULL,
  `sync_run_id`   CHAR(36) NOT NULL,
  PRIMARY KEY (`event_date`,`spec_no`,`risk_type`),
  KEY `idx_risk_type_date` (`risk_type`,`event_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
```

- `first_seen_at` / `resolved_at` 用 `broadcast_dedup`（总方案 §4.4）的同款去重键维护，跨机器不丢状态；
- 看板可加一张「持续超卖 TOP」卡，回答"哪些 SKU 已经缺货一周还没补"。

---

## 4. API → DB 过渡（两阶段，零停机）

| 阶段 | 动作 | 不变量 |
|---|---|---|
| **阶段一 双写** | `stock_alert.py` 在 `forecast()` 后，把结果 `INSERT` 进 `fact_inventory_sku_daily`（新写一行入库函数，API 调用不变） | 推送口径与今天完全一致；DB 数据与内存结果逐条比对应相等 |
| **阶段一验证** | 跑一次 `--dry`，比对"内存 forecast"与"查库结果"是否同序同数 | 不一致则阻断阶段二 |
| **阶段二 投影上移** | 新增 integration `EXTRACT_DATASET`（数据源 `wdt_records` → 本表），把 API 拉取从播报脚本剥离；`stock_alert.py` 改为调 `common/broadcast/queries.py::inventory_alerts(day)` 只读 DB | 播报脚本删除 `WdtClient` 依赖（总方案 §6.3） |
| **阶段二切换开关** | 用配置 `INVENTORY_SOURCE = api | db` 控制；先 db 影子比对，稳定后切 db | 单开关可回滚 |

> 阶段一**不删 API 调用**，只是多写一个库。这样即便 DB 写入失败（try/except 吞掉，见总方案 bi_snapshot 思路），播报照常推，DB 化是"增益"而非"风险"。

---

## 5. 去重状态入库（复用总方案）

`stock_alert.py` 现有 `.stock_alert_state.json` 本地文件做"今天已报过"去重——换机器/多实例即失效。迁移到总方案 §4.4 的 `broadcast_dedup` 表，键 `(domain='stock_alert', spec_no, business_date)`。与 §3.3 的 `fact_inventory_risk_event` 共享同一去重语义。

---

## 6. 回填

- `source-manifest.json` 中 `wms.StockSpec.search2` 已登记 **29 个窗口 dataset**（`_0000`~`_0028`）。历史回填直接按这些窗口回放，不重新设计切分。
- 回填只写 `fact_inventory_sku_daily`（**不回写 `wdt_records` 的投影入口**，避免覆盖最新快照，总方案 §5.4 坑①）。
- 水位判定复用现有 `sync_runs` / `sync_dataset_summary`，不另发明。

---

## 7. 验收

| 项 | 标准 |
|---|---|
| 阶段一 | 双写后，DB 中当日行的 `spec_no` / `available_qty` / `stock_state` 与脚本内存 forecast 逐条一致 |
| 阶段二 | `INVENTORY_SOURCE=db` 下播报输出与 `api` 模式逐行可比（允许排序/文案微调，数字必须一致） |
| 口径 | `stock_state` 与 BI 看板同一张表同一列，无第二套判定 |
| 收窄取舍 | 锁定"降粒度子集(233)"或"全量(298)"之一，记录保留天数与预期月行数 |

---

## 8. 风险与待定

| # | 项 | 说明 |
|---|---|---|
| 1 | ~~动销窗口分歧未决~~ ✅ **已解决（见 §9）** | 不再二选一裁决，改为 7→30 天可调参数 `MOVING_WINDOW_DAYS`（默认14）；落库加 `moving_window_days` 列标注口径，告警/BI/投影共用同一配置 |
| 2 | 收窄收益有限 | 实测 78%，若控表诉求强建议直接全量（§3.2） |
| 3 | 跨仓合并粒度 | 当前 `warehouse_scope='ALL'` 丢拆仓信息；若运营要分仓看，需存两行 |
| 4 | 大表风险 | 298 SKU × 保留期，即便全量也不是大表；但若未来扩仓/扩品需重估 |
| 5 | ~~WDT 不支持任意窗口~~ ✅ **已通过自助日销规避** | 不再依赖 `search2` 固定桶；改为每日取 SKU×日期 日销入 `fact_sales_daily` 自算（§9.1 路径 C）。**数据源已查代码核对（9/17）**：旗舰版 `WdtClient.call` 通用通道已打通（2026-09-03 实测）；代码已验证 `sales.TradeQuery.queryWithDetail` 并落地 `fact_order_line`（订单口径，零新增即可复用）。日销三路径见 §3.1.1：A 复用 `fact_order_line`(零成本) / B 出库明细 `wms.stockout.Sales.queryWithDetail`(同仓口径,推荐) / C 聚合 `vip_stat_...`(1次/天,旗舰版待试)。**剩余待决**：选 A 还是 B/C，及净销量是否剔退款（业务确认） |

---

## 9. 动销窗口可调设计（用户裁定：默认 15 天 + 自助日销）

### 9.1 约束与决策
`fetch_stock`（`stock_alert.py:92-93`）累加的销量来自 WDT 返回的固定字段 `num_7days`（近7天）与 `num_month`（近30天）。`search2` 请求参数只有 `warehouse_no/mask/start_time/end_time`（`:70-73`），**无法让 API 返回"15天销量"之类自定义窗口**。

**用户裁定（9/17）**：默认窗口 **15 天**；WDT 不支持任意窗口 → **改为自己每天取日销量落 `fact_sales_daily`，在 DB 内自己算**。这比"双桶插值"更彻底：拿到的是真实日销明细，15 天窗口是真·过去15天求和 / 15，零近似误差。

→ 实现路径：

| 路径 | 做法 | 精度 | 状态 |
|---|---|---|---|
| **C. 自助日销明细**（采用，主路径） | 每日拉 SKU×日期 日销量入 `fact_sales_daily`，DB 窗口函数按 N 天求和 / N | 精确（任意 N） | ✅ 用户选定 |
| A. 桶切换 | 窗口∈{7,30} 用 WDT 原值 | 精确但仅两档 | 备选 |
| B. 桶间插值 | 中间值两桶线性插值 | 近似 | 仅当不引入日销表时的降级 |

### 9.2 配置项
```python
MOVING_WINDOW_DAYS = int(os.environ.get("MOVING_WINDOW_DAYS", "15"))  # 7..30
assert 7 <= MOVING_WINDOW_DAYS <= 30
```
- 默认 **15**（用户裁定）：比 7 天更抗短期波动，比 30 天更灵敏；且 15 落在 7–30 区间内，靠自助日销可精确可得。
- 告警脚本、BI 查询、投影落库**共用同一配置**，根绝 §2 #1 双口径分裂。

### 9.3 日销明细表（数据基础）
见 §3.1.1 `fact_sales_daily`：每日一行（SKU × 仓库 × 日期），`qty_sold` = 当日出库/销量。

### 9.4 计算层（DB 窗口函数，任意窗口精确）
```sql
-- 跨仓合并后，近 N 天日均（N = MOVING_WINDOW_DAYS）
WITH daily AS (
  SELECT business_date, spec_no,
         SUM(qty_sold) AS day_qty          -- 跨仓自然合并（PARTITION BY spec_no 累加各仓）
  FROM fact_sales_daily
  WHERE business_date <= :as_of
  GROUP BY business_date, spec_no
)
SELECT spec_no, business_date,
       SUM(day_qty) OVER (
         PARTITION BY spec_no ORDER BY business_date
         ROWS BETWEEN ? PRECEDING AND CURRENT ROW   -- ? = N-1
       ) / ? AS daily_avg,                           -- ? = N
       -- days_left = available_total / daily_avg
FROM daily;
```
- `days_left = available_total / daily_avg`，`available_total` = 当日 `SUM(available_qty)` 跨仓（来自 `fact_inventory_sku_daily` 快照或实时 search2）。
- **断货兜底保留**：`was_active = EXISTS(SELECT 1 FROM fact_sales_daily WHERE spec_no=? AND business_date > :as_of - 30)` 仍只用于"是否仍在卖"的布尔判定（超卖/复活兜底），**不受窗口参数影响**——不会退回"断货第8天静默"（原 `stock_alert.py:127-128` 补丁本意）。

### 9.5 落库口径
- `fact_sales_daily` 存原始日销；`fact_inventory_sku_daily` 的 `daily_avg`/`days_left` 由 §9.4 回写（投影层算好，告警直读）。
- `moving_window_days` 列记录该日所用窗口（= MOVING_WINDOW_DAYS 取值），保证 `days_left` 可解释、可追溯、不"失真"。
- 看板切窗口：`common/broadcast/queries.py` 按当前 `MOVING_WINDOW_DAYS` 实时重算（数据已在 DB，无额外 API 成本）。

### 9.6 影响面
- 切窗口会改变 URGENT 边界 SKU（如 15→30，部分 SKU 可售天数跨过阈值进/出紧急）——预期行为；口径集中在配置，全链路一致。
- 与 §3.1 事实表 `moving_window_days` 列、`stock_state` 枚举共用，播报与 BI 同一数据源。
- **数据源风险转移**：不再依赖 WDT 固定窗口桶；改为依赖"WDT 日级销量明细接口可用性"——具体 API 名待确认（§8 #5 已降级为待确认项，非阻塞）。

