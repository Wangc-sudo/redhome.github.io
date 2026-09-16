# 资金安全页（需求⑩）实施草案

日期：2026-09-16 · 状态：**草案（未落地，不写生产数据、不动 bi.seed.yaml 正式编排）**
Owner：bi-backend-agent 临时持有 · 输入：`docs/bi-需求落地对照-2026-09-15.md` §1 需求⑩、§5.1 阈值表

> 本文件只回答三件事：① raw→mart 投影现状（已在线，无需新建）；② 卡片 SQL 与派生口径草案；
> ③ seed 编排草案。**唯一阻塞项：超期标准待财务确认**——按行业默认 `>60 天` 先行，
> 文中所有该默认值处均标注「待财务确认」。

---

## 1. 数据现状（实证于 mart_ops_test，2026-09-15）

| mart 表 | 行数 | raw 源（raw_dingtalk） | 钉钉 Sheet |
|---|---|---|---|
| `fact_fin_receivables_aging` | 81 | `fin_offline_receivables_aging` | 线下—应收账款账龄分析表 |
| `fact_fin_store_funds` | 300 | `fin_ecommerce_store_funds_balance` | 店铺资金余额核对表 |
| `fact_fin_prepayment_invoice` | 52 | `fin_ecommerce_prepayment_supplier_invoice` | 电商预付及供应商发票管理表 |
| `fact_fin_offline_deposit` | 55 | `fin_offline_deposit_other_receivables` | 线下保证金及其他应收管理-202602 |
| `fact_fin_platform_deposit` | 60 | `fin_ecommerce_platform_deposit` | 平台保证金管理 |

结论与需求对照一致：**五表已在 mart，只缺页面对账口径**。

---

## 2. raw→mart 投影（已在线，本文档只登记，无新增工作）

投影管线：`raw_dingtalk.fin_*`（钉钉 AI 表 1:1 镜像，映射见
`docs/superpowers/specs/2026-09-10-finance-raw-dingtalk-table-mapping.md`）
→ `common/public_data/mart_extract_schema.py` 的 5 个 `ExtractDataset`
→ `common/public_data/extract_finance.py` 两个投影函数 → mart `fact_fin_*`。

### 2.1 快照投影（`project_snapshot`，kind=`snapshot`，整表替换）

| dataset | 列映射（raw → mart） |
|---|---|
| `fin_offline_receivables_aging` | `counterparty_name`、`receivable_category`、`company_entity`、`accounts_receivable_ending_balance`→`ending_balance`、`overdue_amount`、`aging_0_30_days_amount`→`aging_0_30`、`aging_31_60_days_amount`→`aging_31_60`、`updated_date` |
| `fin_ecommerce_prepayment_supplier_invoice` | `supplier_name`、`company_entity`、`prepayment_ledger_amount`、`accounts_payable_estimated_ledger_amount`→`ap_estimated_amount`、`ledger_reconciliation_status`、`uninvoiced_amount`、`statement_date_raw`；**追加派生列** `statement_date`（`parse_statement_date` 解析 `%Y-%m-%d`/`%Y/%m/%d`，不可解析计数进 `statement_date_unparsed`） |
| `fin_offline_deposit_other_receivables` | `company_entity`、`supplier_name`、`project_name`、`cooperation_status`、`deposit_balance`、`updated_at` |
| `fin_ecommerce_platform_deposit` | `company_entity`、`platform`、`store_name`、`project_name`、`store_operating_status`、`review_status`、`deposit_balance` |

### 2.2 宽表 melt（`project_store_funds_melt`，kind=`melt_store_funds`）

`fin_ecommerce_store_funds_balance` 的 `balance_202601`…`balance_202608`（`BALANCE_MONTHS`）
按 `(store_name, channel, company_entity) × month` 展开为窄行 `(store_name, channel,
company_entity, month, balance)`，空值跳过并计数。300 行 = 店铺数 × 有值月份数。

### 2.3 已知缺口（登记，不阻塞本页）

- **账龄只有两桶**：raw 侧只有 `0-30`、`31-60` 两列，没有 `>60` 列。`>60 天` 金额只能由
  后端差额推导（见 §3.2），口径需财务确认（与「超期标准」同一个确认项）。
- **无跨期历史**：快照表整表替换，只保留最新一期；账龄趋势需要历史快照立项（不在本草案）。
- `fact_fin_store_funds` 月份覆盖 2026-01..08，9 月起随同步自动扩列（raw 加列后
  `BALANCE_MONTHS` 需同步扩——投影层已登记此维护点）。

---

## 3. 卡片 SQL 草案（未实现）

铁律沿用：**SQL 只取事实，派生一律后端算**（`docs/derived-metrics.md`）。
金额单位统一 **元**，前端按 `unit`/`format: wan` 格式化，SQL 不做万换算。
建议新建 `common/bi_web/fin_derived.py` 纯函数模块（零 DB 依赖，与
`derived.py` 同形态），配 golden 夹具 `tests/fixtures/fin_derived_golden.json`
双端对拍——不在 `derived.py` 里加财务口径，保持 CubeSchema §2 边界纯净。

### 3.1 `kpi_fin_receivables_overdue`（scalar）

应收逾期总览：Σ逾期金额、Σ期末余额、逾期占比（后端除法，除零护栏）。

```sql
SELECT COALESCE(SUM(overdue_amount), 0) AS overdue,
       COALESCE(SUM(ending_balance), 0) AS balance
FROM fact_fin_receivables_aging
```

载荷：`{value: overdue, target: balance, rate: overdue/balance or None, unit: "元"}`。

### 3.2 `table_fin_receivables_aging`（table）

应收账龄明细：按往来单位一行，含差额推导的 `>60 天` 桶与超期标记。

```sql
SELECT counterparty_name, receivable_category, company_entity,
       ending_balance, overdue_amount, aging_0_30, aging_31_60, updated_date
FROM fact_fin_receivables_aging
ORDER BY overdue_amount DESC, counterparty_name
```

后端派生（`fin_derived`，SQL 不碰）：

- `aging_over_60 = ending_balance − aging_0_30 − aging_31_60`（任一分量缺失 → `None`，
  前端「—」+ 缺陷角标，不静默补 0）——**差额口径待财务确认**；
- `overdue_flag`：默认 `aging_over_60 > 0 或 overdue_amount > 0` → 告警；
  **超期标准 >60 天为行业默认值（需求对照 §5.1），待财务确认后写入阈值表**；
- 数据新鲜度：`updated_date` 原样透传，页面角标提示账龄表编制日期。

### 3.3 `table_fin_prepayment_uninvoiced`（table）

预付/未到票明细：预付账款与未到票金额挂账天数。

```sql
SELECT supplier_name, company_entity, prepayment_ledger_amount,
       ap_estimated_amount, ledger_reconciliation_status, uninvoiced_amount,
       statement_date
FROM fact_fin_prepayment_invoice
ORDER BY uninvoiced_amount DESC, supplier_name
```

后端派生：

- `days_outstanding = CURDATE() − statement_date`（`statement_date` 为 `None`
  —— raw 日期不可解析 —— → `None`，前端「—」，**不猜日期**）；
- `overdue_flag`：`uninvoiced_amount > 0 且 days_outstanding > 60` → 告警
  （**>60 天默认，待财务确认**）；
- `ledger_reconciliation_status`（账账相符核对）原样透传，值 ≠ 相符类文本的行
  由前端标黄——枚举值清单待财务给（登记，不阻塞）。

### 3.4 `table_fin_deposit_status`（table，线下+平台保证金合一）

```sql
-- 线下保证金
SELECT company_entity, supplier_name AS counterparty, project_name,
       cooperation_status AS status, deposit_balance, updated_at, '线下' AS source
FROM fact_fin_offline_deposit
UNION ALL
-- 平台保证金
SELECT company_entity, store_name AS counterparty, project_name,
       store_operating_status AS status, deposit_balance, NULL AS updated_at, '平台' AS source
FROM fact_fin_platform_deposit
ORDER BY deposit_balance DESC
```

后端派生：`status` 非正常合作/正常运营 → `recoverable_flag`（可退未退关注项）；
状态枚举「正常」取值清单待财务/运营确认（登记）。`review_status`（是否复核）
仅平台侧有，原样透传。

### 3.5 `trend_fin_store_funds`（line）

店铺资金余额月度趋势（melt 窄表天然适配）：

```sql
SELECT month, store_name, channel, balance
FROM fact_fin_store_funds
ORDER BY month, store_name
```

后端组装：月升序轴 × 店铺系列（复用 `_align_series_rows` 同款零填充思路）；
可选 channel 筛选（新增筛选 source 需动 `app._FILTER_SOURCE_QUERIES` 与
`config.KNOWN_FILTER_SOURCES`，与 shortfall 的 grain 增量同一批做，本批不动 app 层）。

---

## 4. seed 编排草案（**不写入** `docker/integration/bi.seed.yaml`，待 main 裁决后追加）

```yaml
l2-fund-safety:
  title: "资金安全"
  enabled: true
  refresh_seconds: 300
  nav_order: 40
  cards:
    - card: kpi_fin_receivables_overdue
      title: "应收逾期总览"
      span: 4
    - card: trend_fin_store_funds
      title: "店铺资金余额趋势"
      span: 8
    - card: table_fin_receivables_aging
      title: "应收账龄明细"
      span: 12
    - card: table_fin_prepayment_uninvoiced
      title: "预付与未到票"
      span: 6
    - card: table_fin_deposit_status
      title: "保证金状态"
      span: 6
```

守门要求（roadmap §3 裁决 #1，追加式）：新卡注册进 `cards._CARDS` 后，必须**同步追加**
`tests/common/test_bi_web_cards.py` 的 `STAGE_B_CARD_IDS` / `EXPECTED_CHARTS` /
`EXPECTED_RUN_FUNCTIONS` / `EXPECTED_PARAMS_SCHEMA` 与 `len(REGISTRY)` 计数；
`set(IDS)==set(REGISTRY)` 断言本身原样保留，只追加不放弱。

---

## 5. 阈值与阻塞项

| 项 | 默认值（出处：需求对照 §5.1） | 状态 |
|---|---|---|
| 应收超期标准 | >60 天（行业默认）；`aging_over_60` 差额口径 | **待财务确认**（唯一阻塞项） |
| 预付未到票超期 | >60 天挂账 | **待财务确认**（同一确认项） |
| 账账相符/合作状态枚举 | 透传原文，不固化选项 | 待财务/运营给清单（不阻塞） |
| 阈值落库 | 确认后写 `dim_threshold`（现无此表，随阈值类统一立项） | 登记 |

**建议**：默认值先行实现（卡片标注「口径待财务确认」角标），财务确认后只改阈值常量/
阈值表，不动 SQL 与派生函数——与需求对照 §5「行业默认值先行，业务确认或改数」一致。

---

## 6. 验收口径（实现时）

- `python -m unittest discover -s tests -t . -k bi_web -v` 全绿（327 例基线 + 新增）；
- `fin_derived` golden 夹具逐位对拍（参照 `derived_golden.json` 形态：期望值手写、
  标注 source、None 不静默补 0）；
- 守门断言 `set(STAGE_B_CARD_IDS)==set(REGISTRY)` 存在且通过（计数 23 → 28）；
- 不碰生产数据/生产库；卡片 SQL 一律只读 mart。
