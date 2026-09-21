# BI 建设指南

状态：现行有效（2026-09-14 首次编制，随建设批次滚动更新）
需求源：《BI看板颗粒度需求表.xlsx》（总经理 12 条需求，供 IT 参考）
架构基准：`docs/superpowers/specs/2026-09-12-bi-web-design.md`、`2026-09-12-bi-web-stage-b-design.md`

---

## 1. 需求全景与覆盖现状

### 1.1 需求清单（①~⑭）

| 编号 | 模块 | 板块 | 频率 | 颗粒度 | 建议层级 | 现状 |
|---|---|---|---|---|---|---|
| ① | 板块日销售额 | 线下+电商+餐厅 | 日(T+1) | 板块×日 | L1 | 🟡 线下+电商已上线；**餐厅无数据源** |
| ② | 销售结构下钻 | 三大板块 | 日(T+1) | 业务单元×品牌×渠道×SKU×日 | L2 | 🟡 过渡形态（区域/渠道/店铺）已上线；品牌×SKU 待 B1+B2 |
| ③ | 库存补货预警 | 全公司 | 日 | SKU×仓库×日 | L1 预警 | ❌ B2（WDT 库存 API 待盘点） |
| ④ | 电商月度经营 | 电商 | 月 | 渠道×月（收入/毛利/费用） | L2 | ❌ C 类（以运营月报为准） |
| ⑤ | 餐厅月度经营 | 餐厅 | 月 | 门店×月 | L2 | ❌ C 类 + 餐厅板块无采集源 |
| ⑥ | 季度经营预实 | 三大板块 | 季 | 板块×季（预算 vs 实际） | L2 | ❌ C 类（以财务季度预实表为准） |
| ⑦ | 库存周转与效期 | 全公司 | 月 | SKU×仓库×月 | L2 预警 | ❌ B2 |
| ⑧ | 仓库运作 | 仓储 | 日 | 仓库×原因×订单×日 | L2 | ❌ B2 + WDT 物流/发货 API 未接入 |
| ⑨ | 合同/方案/核销进度 | 线下+电商 | 周 | 品牌×渠道×方案×周 | L2 | ❌ D 类（缺真实数据源，需先建填报通道） |
| ⑩ | 资金安全预警 | 全公司 | 月 | 板块×往来对象×月 | L2 预警 | 🟡 fin_* 五表已入 raw，**待投影进 mart（B1）** |
| ⑪ | 体验馆月度经营 | 万科&大莲花 | 月 | 门店×月 | L2 | ❌ C 类（数据量小，按月即可） |
| ⑫ | 年度目标达成进度 | 全公司 | 日(T+1) | 公司+板块+品牌（日累计） | L1 | 🟡 板块视图已上线；**品牌树（习酒 61631 万、古韵+大坛 10000 万）待 B1** |
| ⑬ | 月度销售同比 | 全公司分列 | 月 | 板块×品牌×月 | L2 | ❌ 需 2025 年历史数据导入或积累 |
| ⑭ | 日环比变动 | 三大板块分列 | 日(T+1) | 板块×日，下钻单元×品牌 | L1 | 🟡 线下+电商两卡已上线；餐厅缺源 |

**覆盖率**：12 条需求中 4 条部分覆盖、8 条缺口。

### 1.2 页面地图（目标 11 页 vs 当前 4 页）

| 页面 | 层级 | 覆盖需求 | 现状 |
|---|---|---|---|
| 首页·经营驾驶舱 `l1-cockpit` | L1 | ①③⑩⑫⑭ | ✅ 已上线（7 卡）；缺库存预警数、资金超期预警数两个指标卡 |
| 销售分析页 | L2 | ①② | 🟡 过渡形态：`l2-region`（区域×部门）+ `l2-channel`（渠道×店铺）；正式链「板块→业务单元→品牌→渠道→SKU」待 B1+B2+E |
| 人员榜 `l2-people` | L2 | （既有口径看板化） | ✅ 已上线（4 卡） |
| 电商经营页 | L2 | ④ | ❌ C 类月报通道 |
| 餐厅经营页 | L2 | ⑤ | ❌ C 类 + 缺源 |
| 季度预实分析页 | L2 | ⑥ | ❌ C 类 |
| 库存与效期页 | L2 | ③⑦ | ❌ B2 |
| 仓储运作页 | L2 | ⑧ | ❌ B2 + API 接入 |
| 合同与核销页 | L2 | ⑨ | ❌ D 类 |
| 资金安全页 | L2 | ⑩ | ❌ B1（数据已在 raw，仅差投影+卡片） |
| 体验馆月度经营页 | L2 | ⑪ | ❌ C 类 |
| 月度同比分析页 | L2 | ⑬ | ❌ 历史数据依赖 |

> 落地节奏遵循需求表建议：**先首页驾驶舱跑稳（数据对准），再铺 L2 各页**。当前处于该阶段。

---

## 2. 总体架构

### 2.1 三层数据流

```
钉钉 AI 表 / 旺店通 / (未来)人工报表
        │  sync-dingtalk / sync-wdt（凭据挂载，non-leaking CLI）
        ▼
raw_dingtalk / raw_wdt            ← 原始层：来源原样落库，stable_id 幂等去重
        │  extract-mart（零凭据，只读 raw）
        ▼
mart_ops                          ← 投影层：fact_* 事实表 + dim_* 维表 + sync_runs 审计
        │  bi-web 只读本库
        ▼
bi-web（FastAPI 后端） + bi-react（React 前端）← 展示层：SQL 在代码、编排在 Nacos
```

### 2.2 核心架构原则（不可违背）

1. **SQL 在代码、编排在 Nacos**：卡片口径（SQL/`common.metrics` 调用）全部版本受控进 `common/bi_web/queries.py`、`cards.py`，可评审可单测；Nacos `group=BI` 只存「哪页摆哪些卡、标题、布局、筛选器、启用态」。
2. **bi-web 只读 `mart_ops`**：不碰 raw、不挂源凭据、不外呼——compose 合约测试把守。
3. **fail-open 配置链**：Nacos → 种子文件 → 内置默认；单页 503 不影响其他页。
4. **口径单点复用**：与日报机器人共享 `common.metrics`，杜绝「机器人一个数、看板一个数」。
5. **幂等采集**：raw 层按 stable_id 去重，sync 可安全重跑。

### 2.3 服务边界

| 服务 | 线 | 职责 |
|---|---|---|
| `sync-dingtalk` / `sync-wdt` | apps | 源 → raw 采集（凭据在仓库外挂载） |
| `extract-mart` | apps | raw → mart 投影、dim_calendar/dim_target 种子 |
| `bi-web` | 业务 | 看板后端 API（FastAPI，18080 仅回环） |
| `bi-react` | 业务 | 看板前端（React + Vite + TypeScript，18090 端口） |
| `robot-*` | 业务 | 日报/榜单机器人（共享口径） |

---

## 3. 数据资产盘点（2026-09-14 实测）

### 3.1 已有资产

| 层 | 表 | 行数 | 内容 | 支撑需求 |
|---|---|---|---|---|
| raw_dingtalk | `daily_report_offline` | 549 | 线下日报（杭州 359 + 绍兴 190） | ①⑭⑫ |
| raw_dingtalk | `channel_daily_sales` | 557 | 渠道日销（天猫/京东/拼多多/猫超/即时零售/直播/私域） | ①⑭⑫ |
| raw_dingtalk | `fin_daily_funds` 等 fin_* 五表 | ~260 | 资金/应收账龄/预付发票/保证金/百亿补贴 | ⑩（待投影） |
| raw_wdt | `wdt_records` | 2804 | 旺店通订单明细（trade_no 幂等） | ②SKU（待投影） |
| mart_ops | `fact_daily_report_offline` | 549 | 线下日销事实 | 已上线 |
| mart_ops | `fact_channel_daily_sales` | 557 | 渠道日销事实 | 已上线 |
| mart_ops | `dim_calendar` | 30 | 工作日历（种子版本受控） | ①⑭ |
| mart_ops | `dim_target` | 3 | 年度目标（总 76951 万：线下 21041 / 电商 54980 / 餐厅 930） | ⑫ |
| mart_ops | `dim_product` | 0 | **已建表待灌**（旗舰版 `goods.Goods.queryWithSpec` 已探针验证 status=0） | ②⑫ 品牌树 |

### 3.2 缺口数据源矩阵

| 需求 | 缺什么 | 解法类别 |
|---|---|---|
| ①⑤⑥⑭ 餐厅板块 | 餐厅无任何采集源 | 先定：系统对接 or 钉钉 AI 表日报填报 |
| ② SKU×品牌 | dim_product 为空；476 个无品牌 SKU 分类规则未定 | B1：商品投影 + 分类规则（业务提供） |
| ③⑦ 库存/效期 | WDT 库存快照接入方式待盘点（stock_specs 探针大多 0 行） | B2：定库存真源（WDT 库存查询 or ERP） |
| ⑧ 仓储运作 | WDT 发货/物流 API 未接入 | B2：manifest 加数据集即可采 |
| ⑨ 合同/核销 | 无真源 | D 类：先建钉钉 AI 表填报通道，再进管线 |
| ④⑤⑥⑪⑬ 月报类 | 毛利/费用/预算非系统数据 | C 类：人工报表导入通道（运营/财务月报） |
| ⑬ 同比 | 2025 年同期数据不在库 | 历史报表一次性导入 or 满一年后自然具备 |

---

## 4. 维度与指标字典

### 4.1 维度字典（全看板统一，只留 7 个切法）

| 维度 | 目标取值 | 现状 |
|---|---|---|
| 业务板块 | 线下 / 电商 / 餐厅 / 体验馆 | 餐厅、体验馆缺源 |
| 业务单元 | 各板块下设（**清单待板块负责人提供并固化**，E 类） | 以 department 过渡 |
| 品牌→系列→SKU | 茅台1935、金王子、习酒（君品/窖藏/古韵/大坛）、古越龙山、女儿红等 | 待 dim_product 灌库（B1） |
| 渠道 | 线下：流通/宴席/商超/团购/体验馆；电商：平台店铺 | 电商渠道已上线；线下渠道字典待盘点（E 类） |
| 客户/终端网点 | 终端网点 5000+ 家 | 未建 |
| 仓库/批次 | 仓库、效期批次 | 未建（B2） |
| 时间 | 日(T+1) / 周 / 月 / 季 | 已实现 |

### 4.2 核心指标口径（代码内单一真源）

| 指标 | 口径 | 位置 |
|---|---|---|
| 日销售额 | Σsales_amount，`business_date ≤ CURDATE()` 截断未来，排除合计行 | `queries.py` |
| 日环比 | 基期=前一**自然日**（非前一数据日）；prev 缺失/为 0 → 「—」 | 阶段 B §7 |
| 月目标 | Σ各人员 MAX(monthly_target)，**绝不跨行 SUM 目标**（melt 陷阱） | 阶段 B §3.2 |
| 达成率 | Σcompleted ÷ Σtarget（非个人率平均） | `common.metrics` |
| 年度目标达成 | 年累计 Σsales ÷ dim_target.annual_target | `kpi_annual_progress` |
| ROI | Σsales ÷ Σpromo（非行级均值）；推广费缺失 → 「—」 | 阶段 B §3.3 |
| 波动预警 | 环比超 ±20% 标红——**阈值待业务确认后入 Nacos 配置** | 待确认项 |

### 4.3 电商人员业绩归属（集合口径，2026-09-18）

电商人员业绩单独成页（`l2-ecom-people`），归属采用**集合口径**而非人均切分：

- **归属来源**：电商负责人来自钉钉 AI 表 `user[]` 集合（≤3 人，可随时变更）。整店日销售额与整店月目标**归集合内每位负责人名下**——不切分、不均摊。
- **预期副作用**：电商人员业绩汇总 > 渠道合计，属正常现象，与区域销售（大区汇总 > 公司合计）同构，不做对齐。
- **落库约定**：`region='电商'` 写入 `fact_daily_report_offline`，`source_record_id` 使用 `ecom:*` 命名空间隔离；`monthly_target` melt 后每行携带，读侧取 MAX（绝不跨行 SUM 目标，同 4.2）。
- **派生口径**：缺口 / 所需日均 / 四级告警复用 `common/bi_web/derived`，与区域销售同语义，不另立口径。

---

## 5. 建设路线图

| 批次 | 内容 | 依赖 | 产出 | 状态 |
|---|---|---|---|---|
| A | 骨架 + L1 五卡 + 认证 + 合约测试 | — | l1-cockpit | ✅ 完成 |
| B | L2 三页 + ⑭ 环比卡 + 筛选器/下钻/导航机制 | A | l2-region/channel/people | ✅ 完成 |
| **B1** | dim_product 灌库 + 品牌维度卡 + fin_* 五表投影 + **资金安全页**（11–14 人天） | 设计稿已出（`specs/2026-09-14-bi-web-stage-b1-design.md`）；WP1 资金安全无阻塞，WP2 品牌树待评审确认线下是否全量走 WDT | 资金安全页、⑫品牌树、②品牌层 | 设计评审中 |
| **B2** | extract 聚合架构 + 库存/效期/仓储三模块 + WDT 库存&物流 API 数据集 | 库存真源盘点 | 库存与效期页、仓储运作页、L1 预警卡 | 待 B1 |
| C 类 | 人工报表导入通道（电商④/餐厅⑤/预实⑥/体验馆⑪/同比⑬） | 报表模板与财务/运营对齐 | 5 个月报页 | 与 B1/B2 并行可谈 |
| D 类 | 合同/方案/核销填报通道建设 → 进管线 | 业务流程梳理 | 合同与核销页 | 待业务 |
| E 类 | 业务单元清单固化 + 线下渠道字典统一 | 板块负责人 | 正式下钻链替换过渡形态 | 待业务 |
| 阶段 C | 钉钉免登、行级权限、Nacos listener 热更新、大屏模式 | — | 全员推广 | 视需求 |

**建议优先级**：B1（数据已在 raw，投入最小、立竿见影——资金安全页 + 品牌树）→ C 类模板对齐（业务沟通周期长，先启动）→ B2（技术依赖最重）→ E/D。

---

## 6. 工程操作手册

### 6.1 新增一张卡片（5 步）

1. **写口径**：`common/bi_web/queries.py` 加参数化 SQL（或复用 `common.metrics`）；
2. **注册**：`cards.py` 注册 `card_id → (run, chart, params_schema)`；
3. **测试**：口径单测在 test-runner 对 `mart_ops_test` 真实执行 + 配置校验单测；
4. **编排**：`docker/integration/bi.seed.yaml` 目标页 `cards:` 加引用（含 title/span）；
5. **发布**：bi-web 容器内 `publish_bi_seed_from_env(..., if_missing=False)` 写入 Nacos，TTL 30s 内生效。

### 6.2 新增一个页面

seed 增加 dashboard 条目（`title/enabled/nav_order/filters/cards`）→ 发布 → 导航自动出现。L1 不加筛选器；L2 筛选器声明 `filters:`（region/channel/month 三种 source，服务端维表渲染）。

### 6.3 接入一个新数据源

1. `docker/integration/source-manifest.json` 加数据集声明（allowlist 方法 + target_table + 窗口）；
2. 跑 `sync-*` 入 raw（凭据文件按《阶段4切换运行手册》放仓库外）；
3. `extract_mart.py` 加投影（raw → fact/dim）；
4. 按 6.1 上卡。

### 6.4 发布/回滚/关停

- 编排改错：重发旧版 seed 即回滚（Nacos 为准）；
- 整页下线：Nacos 该 dataId 置 `enabled: false`，导航自动消失；
- 整站关停：Nacos `PIPELINES/bi-web.yaml` 置 `enabled=false`（请求级门控，30s 内全站 503）。

---

## 7. 治理与运维

| 事项 | 规范 |
|---|---|
| 口径变更 | 必走代码评审 + 单测；禁止在 Nacos 写 SQL |
| 预警阈值 | 业务确认 → 写入 Nacos 看板定义（不硬编码） |
| 数据更新 | 日数据 T+1（sync 每日跑批）；月报类 C 类通道按月导入 |
| 权限暴露 | 当前 `127.0.0.1:18080` + Bearer token；阶段 C 钉钉免登 + 行级权限 |
| 运行审计 | `mart_ops.sync_runs` / `sync_dataset_summary` 记录每次采集与投影 |
| 故障语义 | 单页配置损坏 → 该页 503，其余页不受影响 |

### 待业务确认清单（阻塞项汇总）

1. 波动预警阈值（±20% 是否合适）——①⑭
2. 各板块「业务单元」清单——②E 类
3. 476 个无品牌 SKU 的分类规则——B1
4. 餐厅板块数据来源（系统 or 填报）——①⑤⑥⑭
5. 合同/核销业务归口与填报字段——⑨
6. 线下渠道字典（流通/宴席/商超/团购/体验馆映射）——E 类

---

## 8. 架构评估与优化建议（2026-09-15）

### 8.1 当前架构评估

#### ✅ 做得好的地方

| 方面 | 评价 |
|---|---|
| **零凭据泄露设计** | 优秀——错误响应泛化，凭据分层隔离 |
| **只读 mart_ops 规则** | 业务服务不碰 raw_*，防止数据污染 |
| **Profile 组合** | Docker profile 切分灵活，按需启动 |
| **幂等写入** | `ON DUPLICATE KEY UPDATE` 保证可重跑 |
| **SQL 在代码、编排在 Nacos** | 口径可评审、可单测，配置与代码职责分离 |

#### ⚠️ 架构问题

| # | 问题 | 影响 |
|---|---|---|
| 1 | **mart_ops 承担过多职责** | 业务事实 + 维度 + 日历 + 目标 + 投递队列表混在一起，难以独立扩缩容 |
| 2 | **extract-mart 全量重建** | 每次运行重写 dim_calendar/dim_target，增量数据也要全量扫描 raw_* |
| 3 | **dingtalk-gateway 耦合投递和消费** | 投递逻辑和状态机混在一起，失败重试难以独立测试 |
| 4 | **bi-web 无 API 版本管理** | 前端版本切换时，后端无法渐进灰度 |
| 5 | **Redis 缓存缺乏可观测性** | fail-open 但无命中率指标，无法评估缓存价值 |

### 8.2 目标架构（优化后）

```
┌─────────────────────────────────────────────────────────────────┐
│                         数据层（分层清晰）                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐         │
│  │  raw_*      │───▶│  stage_*    │───▶│  mart_*     │         │
│  │  (原始数据)  │    │  (中间层)    │    │  (业务汇总)  │         │
│  │             │    │  清洗/标准化  │    │  只读视图    │         │
│  └─────────────┘    └─────────────┘    └─────────────┘         │
│                                                                  │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐         │
│  │  queue_*    │    │  config_*   │    │  metrics_*  │         │
│  │  (投递队列)  │    │  (种子配置)  │    │  (可观测)    │         │
│  └─────────────┘    └─────────────┘    └─────────────┘         │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 8.3 优化建议与优先级

| 优先级 | # | 优化项 | 当前 | 优化后 | 收益 | 风险 | 状态（2026-09-16 勘） |
|---|---|---|---|---|---|---|---|
| 🔴 P0 | 1 | **extract-mart 增量写入** | 全量覆盖 | upsert 只写变化行 + 定期快照 | 同步时间 10min→1min | 高（涉数据迁移） | 🟡 进行中：fact 已 upsert（`extract_mart.py` `upsert_fact`，ON DUPLICATE KEY UPDATE）+ digest 审计链已建；`read_dataset` 全表扫描与 dim_calendar/dim_robot_member 写路径「upsert + 剪枝」等价增量改造中（pipe-agent；删除语义不变，full-rebuild 开关保留回退） |
| 🔴 P0 | 2 | **投递状态机拆分** | gateway 单体 | stream-handler 独立状态机 + dingtalk-deliverer 封装 | 可独立测试失败重试 | 中（接口不变） | ✅ 已完成（收尾中）：`outbox_repository.py` 已有 enqueue/fetch_pending/mark_delivered/register_failure + max_attempts 状态机；`delivery.py` 仅轮询投递、`stream_handler.py` 已独立；delivery-agent 本轮收尾 |
| 🟡 P1 | 3 | **mart 按用途拆库** | mart_ops 混合 | mart_facts / mart_dims / mart_queue 分离 | 独立扩缩容、独立备份 | 高（连接串改） | 🟡 设计稿已出：`specs/2026-09-16-mart-split-design.md`（同实例 schema 拆分，mart_ops 收敛为控制面；「独立扩缩容」收益已放弃，只保职责/权限/备份粒度）；本轮不落地迁移 |
| 🟡 P1 | 4 | **API 版本化** | 无版本 | URL 或 header `API-Version` | 前后端解耦，灰度发布 | 低（可共存） | 🟡 进行中：api-agent 本轮落地 URL 前缀 + `API-Version` header 双通道（默认 v1、未知版本 404，见 `bi_web/app.py`） |
| 🟢 P2 | 5 | **Redis 可观测** | fail-open | metrics 记录 cache_hit/miss/error | 可评估缓存价值 | 低 | ✅ 已完成：`CacheMetrics` hit/miss/error/耗时计数 + `/diagnostics/cache` 顶层只读诊断端点（api-agent） |
| 🟢 P2 | 6 | **消息队列** | robot_outbox 表 | Kafka/RabbitMQ | 支持多 consumer | 中（引入新组件） | 本轮不立项；接口预留建议见 `specs/2026-09-16-mart-split-design.md` §6 |

### 8.4 mart 拆分细化方案

```
mart_ops/
├── mart_facts/           # fact_* 只读汇总表
│   ├── fact_daily_report_offline
│   ├── fact_channel_daily_sales
│   └── ...
├── mart_dims/            # 维度与配置表
│   ├── dim_calendar       # 静态，变更少
│   ├── dim_target        # 目标配置
│   └── dim_product       # 商品维度
└── mart_queue/           # 投递状态机（独立队列库）
    └── robot_outbox
```

**拆分理由**：
- `mart_dims` 可独立更新频率（dim_calendar 年更新一次）
- `mart_queue` 是状态机，不是业务汇总，适合独立服务
- `mart_facts` 可按需加读副本

### 8.5 extract-mart 增量改造方案

```sql
-- 当前（全量覆盖）
INSERT INTO mart_ops.dim_calendar (...) SELECT ... FROM raw_dingtalk.calendar;

-- 优化后（upsert）
INSERT INTO mart_ops.dim_calendar (...)
SELECT ... FROM raw_dingtalk.calendar
WHERE updated_at > (SELECT MAX(sync_ts) FROM mart_ops.sync_runs WHERE table_name='dim_calendar')
ON DUPLICATE KEY UPDATE ...;
```

**收益**：
- 增量同步时间从 ~10min 降至 ~1min
- 降低对源数据库的压力
- 保留变更历史（通过 updated_at 字段）

---

## 9. 文档索引

- 需求源：`e:\repos\BI看板颗粒度需求表.xlsx`
- 展示层设计（旧，已过时）：`docs/superpowers/specs/2026-09-12-bi-web-design.md`
- **前端架构**：`frontend/bi-react/ARCHITECTURE.md`（React + Vite + TypeScript）
- **前端进度**：`frontend/bi-react/PROGRESS.md`
- **派生指标**：`docs/derived-metrics.md`
- 阶段 B 设计：`docs/superpowers/specs/2026-09-12-bi-web-stage-b-design.md`
- 阶段 B1 设计：`docs/superpowers/specs/2026-09-14-bi-web-stage-b1-design.md`
- 缓存分离设计：`docs/superpowers/specs/2026-09-14-bi-web-cache-separation-design.md`
- 数据契约：`frontend/bi-ui/CubeSchema.md`
- 可视化规范：`frontend/bi-ui/VISUALIZATION.md`
- 采集运行手册：`docs/阶段4切换运行手册.md`
- 命令速查：`docs/命令速查表.md`
