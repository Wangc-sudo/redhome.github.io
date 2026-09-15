# BI Web 阶段 B1 实施计划（人工交接版）

> **交接说明**：本文档面向人工执行。12 个任务均已拆解到「改动文件 + 代码骨架 + 验证方式」粒度，可直接照做。
> **口径准绳**：`docs/superpowers/specs/2026-09-14-bi-web-stage-b1-design.md`（卡片口径、出界清单、开放点以 spec 为准；本文档只管怎么落地）。
> **当前状态**：设计已定稿，代码零改动，12 个任务全部待执行。

**目标**：bi-web 上线资金安全二级页（6 卡 + L1 预警卡）与品牌维度（dim_product 品牌资产 + L1 两卡），支撑 10 月经营会演示。

**架构**：完全复用既有链路 `source-manifest → raw → extract-mart 投影 → mart_ops ← bi-web 查询 ← Nacos bi.seed`。新增内容全部是「投影数据集注册 + 卡片注册 + seed 布局」，**app.py / config.py / 合约面零改动**。投影层引入 `kind` 分派机制（见 §4 设计决策 1）。

**技术栈**：Python 3.12（common/ 单 repo）、MySQL 8（raw_dingtalk / raw_wdt / mart_ops 三库）、Flask(bi-web) + 原生 JS SPA（dashboard.js）、PyMySQL、unittest/pytest。

---

## 1. 开工前需要人先办的 3 件事

这三件事不阻塞写代码（Task 1-6 可立即开工），但阻塞最终验收。

### 1.1 开放点评审（5 项，均有保守默认值，评审后单点调整）

| # | 开放点（spec 章节） | 首批默认值 | 评审后改哪里 |
|---|---|---|---|
| 1 | 超期标红阈值（§7.1） | `overdue_amount > 0` 即标红 | `queries.py` `_is_overdue_highlight` 一处 |
| 2 | 订单状态计入范围（§7.3） | 「已付款计入」，排除取消/关闭类 | `extract_order_line.py` `_EXCLUDED_TRADE_STATUSES` 一处 |
| 3 | 古韵+大坛系列组口径（§7.4） | 系列名 `LIKE '%古韵%'/'%大坛%'` | `queries.py` `_SERIES_GROUP_TOTAL_SQL` 改静态 IN |
| 4 | 未分类 SKU 回填节奏（§7.5） | 未匹配金额占比 >5% 时卡面警示 | Task 7 产出未识别清单交业务 |
| 5 | fin_daily_funds 边界 | 本阶段出界，不做 | — |

### 1.2 WDT 凭据与 payload 字段抽样（Task 9 前置）

确认 WDT 凭据文件可用，然后抽样确认订单 payload 字段名（Task 9 代码的字段名以此次抽样为准）：

```sql
SELECT source_record_id, payload_json FROM raw_wdt.wdt_records
WHERE source_method = 'sales.TradeQuery.queryWithDetail' LIMIT 2;
```

需确认：trade 层 `trade_no` / `trade_time` / 状态字段名，detail_list 行内 `spec_no` / `goods_name` / `num` / `paid` 的实际拼写。**与本文档不符时以实盘为准修订常量，并回写 spec §3.3。**

### 1.3 dim_product 灌库（即 Task 7，建议在 Task 8 开发前完成）

```bash
cd e:\repos\digital-ops && python -m common.public_data.product_catalog
```

DDL `wdt-dim-product-v1` 已存在（apply_live_migrations 自动建表）；该命令做全量 upsert + 打印摘要（spec_no 覆盖率、品牌识别率、品牌分布 Top 10）。随后跑 Task 7 的对拍 SQL，产出两份清单：

- 习酒/古韵系列构成清单 → 作为 Task 10 seed scope_key 与 Task 11「古韵+大坛」组口径的实盘依据
- 未识别品牌（brand_name 为 NULL）Top 清单 → 导出给业务回填（开放点 §7.5）

---

## 2. 环境与常用命令

```bash
cd e:\repos\digital-ops

# 测试（单文件 / 全量）
python -m pytest tests/common/test_public_data_extract_mart.py -x -q
python -m pytest tests/common -x -q

# 数据链路
python -m common.public_data.cli extract-mart     # 投影重跑（全量快照表幂等）
python -m common.public_data.cli publish-bi       # seed 发布到 Nacos
python -m common.public_data.cli load-target      # dim_target 灌库
python -m common.public_data.product_catalog      # dim_product 灌库（Task 7）
```

测试写法约定：投影层用 fake repository 断言「读到的行 → replace_table 收到的列与行」；queries 层用 fake cursor 捕获 SQL 文本做静态断言（无 `%` 拼接、过滤条件为字面量）。参照 `tests/common/test_public_data_extract_mart.py`、`tests/common/test_bi_web_queries.py` 既有模式。

每个 Task 完成后立即跑对应测试并单独 commit（commit message 在各 Task 末尾给出）。

---

## 3. 任务总览

| Task | 内容 | 主要文件 | 依赖 |
|---|---|---|---|
| 1 | 注册 5 个 fin 数据集 + 5 张 DDL | mart_extract_schema.py | — |
| 2 | finance snapshot/melt 投影器 + kind 分派 | extract_finance.py(新)、extract_mart.py、cli.py | 1 |
| 3 | 资金安全七卡 SQL + run_ 函数 | queries.py | 1 |
| 4 | cards.py 注册七卡 | cards.py | 3 |
| 5 | 前端：scalar note 副行 + table 行标红 | dashboard.js、style.css | 3 |
| 6 | seed：l2-finance 页 + L1 预警卡 | bi.seed.yaml | 2/3/4/5 |
| 7 | 【人工】dim_product 灌库 + 质量对拍 | 无代码 | WDT 凭据 |
| 8 | dim_product 镜像到 mart_ops | extract_order_line.py(新)、mart_extract_schema.py | 1 |
| 9 | fact_order_line 订单行展开 × 品牌物化 | extract_order_line.py、mart_extract_schema.py | 8、§1.2 |
| 10 | dim_target 追加 brand/series_group 两行 | target.seed.json | 7 |
| 11 | 品牌两卡（习酒 / 古韵+大坛） | queries.py、cards.py | 9、10 |
| 12 | L1 三卡补齐 + 端到端验收 | bi.seed.yaml | 全部 |

**执行顺序建议**：Task 1→2→3→4→5→6（WP1 完整可演示）→ 7→8→9→10→11→12（WP2）。Task 8/9 可与 WP1 并行开发，但 Task 9 的真实数据验证依赖 §1.2 与 Task 7。

---

## 4. 设计决策（实现约束，改动前先读）

1. **`ExtractDataset` 增加两字段**（默认值兼容现有两条目）：
   - `kind: str = "fact"` — `fact`（现有增量 upsert）/ `snapshot`（全量替换）/ `melt_store_funds` / `dim_mirror` / `order_line_expand`
   - `source: str = "dingtalk"` — 源库路由：`dingtalk`→raw_dingtalk，`wdt`→raw_wdt
2. **状态台账表一律 `snapshot` 全量替换**（事务内 DELETE + 批量 INSERT）：台账行会被更新，tuple-hash 增量会残留旧行导致 SUM 双算；台账百行级，全量替换无成本，语义=当前快照，天然免疫重复行（spec 出界项 2）。`order_line_expand`/`dim_mirror` 同样全量替换——dim_product 品牌映射更新后重跑 extract 即刷新物化列（spec §1.1 升级语义载体）。
3. **`fact_order_line` 主键 `(trade_no, line_no)`**：spec_no 可空不能进 PK；line_no 为展开序号天然行内唯一（对 spec 草稿 `(trade_no, spec_no, line_no)` 的修正）。
4. **前端只加两个通用扩展点**（向后兼容：旧卡 payload 无新字段行为不变）：
   - scalar 卡 `note` 字段 → 主值下方副行文本（覆盖 spec 全部「副行/脚注」需求）
   - table 行 `_row_class: "danger"` → 行标红（columns 循环只取 columns 定义，`_` 前缀键天然不参与渲染）
5. **订单状态过滤首批「已付款计入」**：排除状态常量集中在 `extract_order_line._EXCLUDED_TRADE_STATUSES`（开放点 §7.3，评审后只改一处）。WDT payload 字段名以 §1.2 实盘抽样为准。
6. **超期标红阈值未决**：首批 `overdue_amount > 0` 即标红，判断集中在 queries 一处（开放点 §7.1）。
7. **`target_seed.py` 零改动**：`_parse_row` 对 scope 只校验非空字符串、无白名单；品牌行不污染年度总目标卡靠 `_ANNUAL_TARGET_SQL` 既有 `scope='line'` 过滤。

---

## 5. 文件改动清单

| 文件 | 动作 | 职责 |
|---|---|---|
| `common/public_data/mart_extract_schema.py` | 修改 | ExtractDataset 加 kind/source；注册 7 个数据集；DDL 追加 7 张表 |
| `common/public_data/extract_mart.py` | 修改 | Repository 加 wdt 连接 + `replace_table()` + `read_table()`/`read_wdt_*`；Service 按 kind 分派 |
| `common/public_data/extract_finance.py` | 新建 | WP1：snapshot 投影、store_funds melt、statement_date 解析 |
| `common/public_data/extract_order_line.py` | 新建 | WP2：dim_product 镜像、订单行展开 × 品牌物化 |
| `common/public_data/cli.py` | 修改 | `build_extract_service` 向 Repository 传 wdt_conn |
| `common/bi_web/queries.py` | 修改 | WP1 七卡 + WP2 两卡的 SQL 常量与 run_* 函数 |
| `common/bi_web/cards.py` | 修改 | 注册 9 张新卡 |
| `common/bi_web/web/dashboard.js` | 修改 | scalar `note` 副行 + table `_row_class` 行标红 |
| `common/bi_web/web/style.css` | 修改 | `.kpi-note` 与 `tr.danger` 样式 |
| `docker/integration/bi.seed.yaml` | 修改 | L1 追加 3 卡；新增 l2-finance 页 6 卡 |
| `docker/integration/target.seed.json` | 修改 | 追加 brand/series_group 两行 |
| `tests/common/test_public_data_extract_finance.py` | 新建 | WP1 投影单测 |
| `tests/common/test_public_data_extract_order_line.py` | 新建 | WP2 投影单测 |
| `tests/common/test_public_data_extract_mart.py` | 修改 | 注册表/DDL 断言更新、replace_table 测试 |
| `tests/common/test_bi_web_queries.py` | 修改 | 9 卡 SQL 静态性与口径测试 |
| `tests/common/test_bi_web_cards.py` | 修改 | 注册常量更新 |

**零改动文件**（其正确性由测试守护）：`common/bi_web/app.py`、`common/bi_web/config.py`、`common/public_data/target_seed.py`、`docker/integration/compose.yaml`。

---

## 6. 任务详情

### Task 1: mart_extract_schema 扩展 + 5 张 fin 表注册与 DDL

**文件**：改 `common/public_data/mart_extract_schema.py`；测 `tests/common/test_public_data_extract_mart.py`

**实现**：

1) `ExtractDataset` 加字段：

```python
@dataclass(frozen=True)
class ExtractDataset:
    dataset: str
    source_table: str
    target_table: str
    columns: tuple[tuple[str, str], ...]
    kind: str = "fact"        # fact | snapshot | melt_store_funds | dim_mirror | order_line_expand
    source: str = "dingtalk"  # dingtalk | wdt
```

2) 注册 5 个 WP1 数据集（追加到 EXTRACT_DATASETS；列名严格对照 `finance_schema.py` raw 侧 DDL）：

```python
ExtractDataset(
    dataset="fin_offline_receivables_aging",
    source_table="fin_offline_receivables_aging",
    target_table="fact_fin_receivables_aging",
    kind="snapshot",
    columns=(
        ("counterparty_name", "counterparty_name"),
        ("receivable_category", "receivable_category"),
        ("company_entity", "company_entity"),
        ("accounts_receivable_ending_balance", "ending_balance"),
        ("overdue_amount", "overdue_amount"),
        ("aging_0_30_days_amount", "aging_0_30"),
        ("aging_31_60_days_amount", "aging_31_60"),
        ("updated_date", "updated_date"),
    ),
),
ExtractDataset(
    dataset="fin_ecommerce_prepayment_supplier_invoice",
    source_table="fin_ecommerce_prepayment_supplier_invoice",
    target_table="fact_fin_prepayment_invoice",
    kind="snapshot",
    columns=(
        ("supplier_name", "supplier_name"),
        ("company_entity", "company_entity"),
        ("prepayment_ledger_amount", "prepayment_ledger_amount"),
        ("accounts_payable_estimated_ledger_amount", "ap_estimated_amount"),
        ("ledger_reconciliation_status", "ledger_reconciliation_status"),
        ("uninvoiced_amount", "uninvoiced_amount"),
        ("statement_date_raw", "statement_date_raw"),
    ),
),
ExtractDataset(
    dataset="fin_offline_deposit_other_receivables",
    source_table="fin_offline_deposit_other_receivables",
    target_table="fact_fin_offline_deposit",
    kind="snapshot",
    columns=(
        ("company_entity", "company_entity"),
        ("supplier_name", "supplier_name"),
        ("project_name", "project_name"),
        ("cooperation_status", "cooperation_status"),
        ("deposit_balance", "deposit_balance"),
        ("updated_at", "updated_at"),
    ),
),
ExtractDataset(
    dataset="fin_ecommerce_platform_deposit",
    source_table="fin_ecommerce_platform_deposit",
    target_table="fact_fin_platform_deposit",
    kind="snapshot",
    columns=(
        ("company_entity", "company_entity"),
        ("platform", "platform"),
        ("store_name", "store_name"),
        ("project_name", "project_name"),
        ("store_operating_status", "store_operating_status"),
        ("review_status", "review_status"),
        ("deposit_balance", "deposit_balance"),
    ),
),
ExtractDataset(
    dataset="fin_ecommerce_store_funds_balance",
    source_table="fin_ecommerce_store_funds_balance",
    target_table="fact_fin_store_funds",
    kind="melt_store_funds",
    columns=(),  # melt 展开列由 extract_finance 自带清单
),
```

3) `ddl_statements()` 追加 5 张表（charset 与 raw 侧一致 `utf8mb4_0900_ai_ci`）。注意 `fact_fin_prepayment_invoice` 同时落 `statement_date_raw`（原样）与 `statement_date`（解析后，Task 2 填充），并含 `uninvoiced_amount` 列：

```sql
CREATE TABLE IF NOT EXISTS `fact_fin_receivables_aging` (
  `record_id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `counterparty_name` VARCHAR(500) DEFAULT NULL,
  `receivable_category` VARCHAR(255) DEFAULT NULL,
  `company_entity` VARCHAR(255) DEFAULT NULL,
  `ending_balance` DECIMAL(20,4) DEFAULT NULL,
  `overdue_amount` DECIMAL(20,4) DEFAULT NULL,
  `aging_0_30` DECIMAL(20,4) DEFAULT NULL,
  `aging_31_60` DECIMAL(20,4) DEFAULT NULL,
  `updated_date` DATE DEFAULT NULL,
  `synced_at` DATETIME(6) NOT NULL,
  `sync_run_id` CHAR(36) NOT NULL,
  PRIMARY KEY (`record_id`),
  KEY `idx_fin_aging_counterparty` (`counterparty_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `fact_fin_prepayment_invoice` (
  `record_id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `supplier_name` VARCHAR(500) DEFAULT NULL,
  `company_entity` VARCHAR(255) DEFAULT NULL,
  `prepayment_ledger_amount` DECIMAL(20,4) DEFAULT NULL,
  `ap_estimated_amount` DECIMAL(20,4) DEFAULT NULL,
  `ledger_reconciliation_status` VARCHAR(255) DEFAULT NULL,
  `uninvoiced_amount` DECIMAL(20,4) DEFAULT NULL,
  `statement_date` DATE DEFAULT NULL,
  `statement_date_raw` VARCHAR(100) DEFAULT NULL,
  `synced_at` DATETIME(6) NOT NULL,
  `sync_run_id` CHAR(36) NOT NULL,
  PRIMARY KEY (`record_id`),
  KEY `idx_fin_prepay_supplier` (`supplier_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `fact_fin_offline_deposit` (
  `record_id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `company_entity` VARCHAR(255) DEFAULT NULL,
  `supplier_name` VARCHAR(500) DEFAULT NULL,
  `project_name` VARCHAR(500) DEFAULT NULL,
  `cooperation_status` VARCHAR(255) DEFAULT NULL,
  `deposit_balance` DECIMAL(20,4) DEFAULT NULL,
  `updated_at` DATETIME(6) DEFAULT NULL,
  `synced_at` DATETIME(6) NOT NULL,
  `sync_run_id` CHAR(36) NOT NULL,
  PRIMARY KEY (`record_id`),
  KEY `idx_fin_offdep_entity` (`company_entity`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `fact_fin_platform_deposit` (
  `record_id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `company_entity` VARCHAR(255) DEFAULT NULL,
  `platform` VARCHAR(255) DEFAULT NULL,
  `store_name` VARCHAR(500) DEFAULT NULL,
  `project_name` VARCHAR(500) DEFAULT NULL,
  `store_operating_status` VARCHAR(255) DEFAULT NULL,
  `review_status` VARCHAR(255) DEFAULT NULL,
  `deposit_balance` DECIMAL(20,4) DEFAULT NULL,
  `synced_at` DATETIME(6) NOT NULL,
  `sync_run_id` CHAR(36) NOT NULL,
  PRIMARY KEY (`record_id`),
  KEY `idx_fin_platdep_entity` (`company_entity`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `fact_fin_store_funds` (
  `record_id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `store_name` VARCHAR(500) DEFAULT NULL,
  `channel` VARCHAR(255) DEFAULT NULL,
  `company_entity` VARCHAR(255) DEFAULT NULL,
  `month` VARCHAR(7) DEFAULT NULL,
  `balance` DECIMAL(20,4) DEFAULT NULL,
  `synced_at` DATETIME(6) NOT NULL,
  `sync_run_id` CHAR(36) NOT NULL,
  PRIMARY KEY (`record_id`),
  KEY `idx_fin_funds_store_month` (`store_name`, `month`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
```

**测试**：`test_registered_datasets_match_stage_a_scope` 断言数量 2→7 并改名 `..._stage_b1_scope`；`test_ddl_covers_every_extract_table` 断言 7 张表各有 DDL 且含 `sync_run_id`、`synced_at` 与目标列名（`ending_balance`、`ap_estimated_amount`、`uninvoiced_amount`、`statement_date`、`month`）。

**提交**：`feat(extract-mart): register five fin snapshot datasets with DDL (B1 Task 1)`

---

### Task 2: extract_finance.py — snapshot 投影 + melt + 日期解析 + Repository/Service 分派

**文件**：新建 `common/public_data/extract_finance.py`；改 `extract_mart.py`、`cli.py`；新建测试 `tests/common/test_public_data_extract_finance.py`

**实现**：

1) Repository 扩展（extract_mart.py）：

```python
def __init__(self, raw_connection, mart_connection, wdt_connection=None) -> None:
    self._raw = raw_connection
    self._mart = mart_connection
    self._wdt = wdt_connection  # WP2 kind 使用；None 时 wdt 源数据集 fail loudly

@property
def wdt_connection(self):
    if self._wdt is None:
        raise MartExtractError("wdt source connection is not configured")
    return self._wdt

def read_table(self, table: str) -> list[dict]:
    """SELECT * 无白名单读取（snapshot/melt 用）。"""

def replace_table(self, target_table: str, columns: Sequence[str], rows: list[dict]) -> int:
    """Full-snapshot replace: DELETE all + batch INSERT, one transaction."""
    col_sql = ", ".join(f"`{c}`" for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with transaction(self._mart) as cur:
        cur.execute(f"DELETE FROM `{target_table}`")
        if rows:
            cur.executemany(
                f"INSERT INTO `{target_table}` ({col_sql}) VALUES ({placeholders})",
                [[r.get(c) for c in columns] for r in rows],
            )
    return len(rows)
```

2) Service 按 kind 分派（`_extract_dataset` 开头）。import 必须惰性放在分派内——WP2 的 extract_order_line 在 Task 8 才有实现，顶层 import 会让 Task 2 无法独立通过：

```python
if dataset.kind != "fact":
    from common.public_data import extract_finance, extract_order_line
    projectors = {
        "snapshot": extract_finance.project_snapshot,
        "melt_store_funds": extract_finance.project_store_funds_melt,
        "dim_mirror": extract_order_line.project_dim_product_mirror,
        "order_line_expand": extract_order_line.project_order_lines,
    }
    projector = projectors.get(dataset.kind)
    if projector is None:
        raise MartExtractError(f"unknown extract kind {dataset.kind!r}")
    return projector(self._repository, dataset, run_id, synced_at)
```

本 Task 先建 `extract_order_line.py` 空壳（两函数 `raise NotImplementedError`）。

3) cli.py 传 wdt 连接：`MartExtractRepository(dingtalk_conn, mart_conn, wdt_connection=wdt_conn)`。

4) extract_finance.py 主体：

```python
"""WP1 finance snapshot projections: full-replace semantics + melt + date parsing."""

from __future__ import annotations

from datetime import date, datetime

_STATEMENT_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d")

#: balance_YYYYMM columns projected out of fin_ecommerce_store_funds_balance.
BALANCE_MONTHS = tuple(f"2026{m:02d}" for m in range(1, 9))


def parse_statement_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in _STATEMENT_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def project_snapshot(repository, dataset, run_id: str, synced_at: datetime) -> dict:
    rows, _ = repository.read_dataset(dataset)
    for row in rows:
        row["sync_run_id"] = run_id
        row["synced_at"] = synced_at
    unparsed = 0
    if dataset.target_table == "fact_fin_prepayment_invoice":
        for row in rows:
            parsed = parse_statement_date(row.pop("statement_date_raw", None))
            row["statement_date"] = parsed
            if parsed is None:
                unparsed += 1
    columns = [t for _, t in dataset.columns if t != "statement_date_raw"]
    if dataset.target_table == "fact_fin_prepayment_invoice":
        columns.append("statement_date")
    columns += ["synced_at", "sync_run_id"]
    written = repository.replace_table(dataset.target_table, columns, rows)
    return {
        "dataset": dataset.dataset, "target_table": dataset.target_table,
        "records_read": len(rows), "records_new": written,
        "records_updated": 0, "records_skipped": 0,
        "statement_date_unparsed": unparsed if dataset.target_table == "fact_fin_prepayment_invoice" else None,
    }


def project_store_funds_melt(repository, dataset, run_id: str, synced_at: datetime) -> dict:
    rows = repository.read_table(dataset.source_table)  # columns=() 不走白名单，直接 SELECT *
    melted: list[dict] = []
    skipped_empty = 0
    for row in rows:
        base = {k: row.get(k) for k in ("store_name", "channel", "company_entity")}
        for ym in BALANCE_MONTHS:
            balance = row.get(f"balance_{ym}")
            if balance is None or str(balance).strip() == "":
                skipped_empty += 1
                continue
            melted.append({**base, "month": f"{ym[:4]}-{ym[4:]}", "balance": balance,
                           "synced_at": synced_at, "sync_run_id": run_id})
    written = repository.replace_table(
        dataset.target_table,
        ["store_name", "channel", "company_entity", "month", "balance", "synced_at", "sync_run_id"],
        melted,
    )
    return {
        "dataset": dataset.dataset, "target_table": dataset.target_table,
        "records_read": len(rows), "records_new": written,
        "records_updated": 0, "records_skipped": skipped_empty,
        "balance_months": len(BALANCE_MONTHS),
    }
```

**测试**：

- `TestParseStatementDate`：`"2026-09-01"`→date / `"2026/09/01"`→date / `"2026.9.1"`→None / `""`→None / None→None / date 原样
- `TestProjectSnapshot`：fake repository 返回 3 行；断言 replace_table 收到列集合正确、`statement_date_raw` 被弹出、unparsed 计数进摘要 dict
- `TestProjectStoreFundsMelt`：1 行 raw（8 个月列含 2 个 None）→ 6 行 mart，month 格式 `"2026-03"`；重跑一趟验证 replace 语义（fake 记录 DELETE 调用 == 1）

**提交**：`feat(extract-mart): finance snapshot/melt projectors with kind dispatch (B1 Task 2)`

---

### Task 3: queries.py — WP1 资金安全七卡

**文件**：改 `common/bi_web/queries.py`；测 `tests/common/test_bi_web_queries.py`

**实现**：

1) SQL 常量（全部静态、禁 `%` 拼接，与既有 `_PREFIX` 注释约定一致）：

```python
_FIN_OVERDUE_SQL = """
SELECT
  COALESCE(SUM(overdue_amount), 0) AS overdue_total,
  SUM(CASE WHEN overdue_amount > 0 THEN 1 ELSE 0 END) AS overdue_count,
  COALESCE(SUM(ending_balance), 0) AS ending_balance_total
FROM fact_fin_receivables_aging
"""

_FIN_RECEIVABLES_SQL = """
SELECT counterparty_name, company_entity, receivable_category,
       ending_balance, overdue_amount, aging_0_30, aging_31_60, updated_date
FROM fact_fin_receivables_aging
ORDER BY overdue_amount DESC, ending_balance DESC
LIMIT 500
"""

_FIN_PREPAYMENT_SQL = """
SELECT company_entity, ledger_reconciliation_status,
       SUM(prepayment_ledger_amount) AS prepayment_amount
FROM fact_fin_prepayment_invoice
GROUP BY company_entity, ledger_reconciliation_status
ORDER BY company_entity, prepayment_amount DESC
"""

_FIN_UNINVOICED_SQL = """
SELECT supplier_name, uninvoiced_amount, statement_date
FROM fact_fin_prepayment_invoice
ORDER BY uninvoiced_amount DESC
LIMIT 500
"""

_FIN_STORE_FUNDS_SQL = """
SELECT store_name, channel, company_entity, month, balance
FROM fact_fin_store_funds
ORDER BY month DESC, store_name
LIMIT 1000
"""

_FIN_DEPOSIT_SQL = """
SELECT '线下' AS source, company_entity, supplier_name AS counterparty,
       project_name, NULL AS platform, NULL AS store_name,
       SUM(deposit_balance) AS deposit_amount
FROM fact_fin_offline_deposit
GROUP BY company_entity, supplier_name, project_name
UNION ALL
SELECT '平台' AS source, company_entity, NULL AS counterparty,
       project_name, platform, store_name,
       SUM(deposit_balance) AS deposit_amount
FROM fact_fin_platform_deposit
GROUP BY company_entity, platform, store_name, project_name
ORDER BY deposit_amount DESC
LIMIT 500
"""
```

2) run_* 函数（7 个，模式照抄现有 run_ 函数：threading.Lock + `_connection()`）：

```python
def _fin_overdue_row() -> dict:
    lock = threading.Lock()
    with lock, _connection() as conn, conn.cursor() as cur:
        cur.execute(_FIN_OVERDUE_SQL)
        return cur.fetchone() or {}

def _is_overdue_highlight(row: dict) -> bool:
    """开放点 §7.1：首批 overdue>0 即标红，阈值评审后只改这里。"""
    return (row.get("overdue_amount") or 0) > 0

def run_kpi_fin_overdue() -> dict:
    row = _fin_overdue_row()
    count = int(row.get("overdue_count") or 0)
    ending = _fmt_wan_text(row.get("ending_balance_total"))
    return {"value": row.get("overdue_total"),
            "note": f"超期 {count} 笔 · 应收期末余额 {ending}"}

def run_kpi_fin_alert() -> dict:
    row = _fin_overdue_row()
    return {"value": int(row.get("overdue_count") or 0),
            "note": f"超期金额 {_fmt_wan_text(row.get('overdue_total'))}"}

def run_table_fin_receivables() -> dict:
    ...  # rows 逐个加 _row_class: "danger" if _is_overdue_highlight(row)
    # columns: 往来单位/公司主体/类别/应收期末余额(wan)/超期金额(wan)/0-30天(wan)/31-60天(wan)/更新日期

def run_table_fin_prepayment() -> dict:
    ...  # columns: 公司主体/对账状态/预付台账金额(wan)

def run_table_fin_uninvoiced() -> dict:
    ...  # columns: 供应商/未开票金额(wan)/对账日期

def run_table_fin_store_funds() -> dict:
    ...  # columns: 店铺/渠道/公司主体/月份/余额(wan)

def run_table_fin_deposit() -> dict:
    ...  # columns: 来源/公司主体/往来单位/项目/平台/店铺/保证金余额(wan)
```

工具函数 `_fmt_wan_text(v)`：`v/10000` 取整 + "万"，None→"—"。

**注意**：表 rows 的 date/datetime/Decimal 原样返回（columns format 已声明 wan）。**执行时先确认 `_run_card` 的 JSON 序列化路径是否兜底 date/Decimal**——若不支持，在 run_ 内转 isoformat 字符串。

**测试**：追加 `TestStageB1FinanceQueries`——捕获 SQL 文本断言：无 `%` 拼接、`_FIN_OVERDUE_SQL` 不含 WHERE（快照表全量）、deposit 双源 UNION 两分支表名正确、store_funds 含 `month` 列；run_ 行为：断言 note 文案、`_row_class` 只在 overdue>0 行出现、uninvoiced 排序 LIMIT 500。

**提交**：`feat(bi-web): finance safety queries and seven cards (B1 Task 3)`

---

### Task 4: cards.py 注册 WP1 七卡

**文件**：改 `common/bi_web/cards.py`；测 `tests/common/test_bi_web_cards.py`

**实现**（param_names 全空——资金卡无 URL 筛选器）：

```python
_card("kpi_fin_overdue", "应收超期", "scalar", {}),
_card("kpi_fin_alert", "资金预警", "scalar", {}),
_card("table_fin_receivables", "应收超期与账龄明细", "table", {}),
_card("table_fin_prepayment", "供应商预付与对账", "table", {}),
_card("table_fin_uninvoiced", "供应商未开票金额", "table", {}),
_card("table_fin_store_funds", "店铺资金余额", "table", {}),
_card("table_fin_deposit", "保证金台账", "table", {}),
```

**测试**：`STAGE_B_CARD_IDS` 追加 7 个 id（或新增 `STAGE_B1_FIN_CARD_IDS` 并在注册完整性断言里 union）；`L1_PARAMLESS_CARD_IDS` 追加 `kpi_fin_alert`。KNOWN_CHARTS 不变（scalar/table 已存在）。

**提交**：`feat(bi-web): register finance safety cards (B1 Task 4)`

---

### Task 5: 前端扩展 — scalar note 副行 + table 行标红

**文件**：改 `common/bi_web/web/dashboard.js`、`common/bi_web/web/style.css`

**实现**：

1) dashboard.js `renderScalar`：dod 分支副行之后 / progress 分支 progress-wrap 之后，追加通用 note 渲染（与 target/dod 不互斥，payload 有 note 就渲染）。用既有 `esc()` 防注入：

```js
if (payload && typeof payload.note === "string" && payload.note) {
  html += `<div class="kpi-note muted">${esc(payload.note)}</div>`;
}
```

2) dashboard.js `renderTable` 行渲染：

```js
html += rows.map(r => `<tr${r._row_class ? ` class="${esc(r._row_class)}"` : ""}>` + ...).join("");
```

3) style.css（参照既有 `.kpi-sub`/`.kpi-dod` 的样式变量，色值以文件现有变量为准调整）：

```css
.kpi-note { font-size: 12px; margin-top: 4px; }
tr.danger td { background: rgba(255, 77, 79, 0.10); }
```

**验证**：起 bi-web 服务，用 Task 3 的 run_ 输出喂 `/bi/api/card`，确认副行与标红渲染；旧卡（payload 无新字段）DOM 无变化。

**提交**：`feat(bi-web): scalar note line and table row highlight (B1 Task 5)`

---

### Task 6: bi.seed.yaml — l2-finance 页 + L1 预警卡 + publish 验证

**文件**：改 `docker/integration/bi.seed.yaml`

**实现**：

1) L1 追加预警卡（kpi_annual_progress 之后；Task 12 再在同一行补品牌两卡，最终 4+4+4）：

```yaml
    - card_id: kpi_fin_alert
      title: 资金预警
      chart: scalar
      span: 4
```

2) 追加 l2-finance 页（nav_order 5 在现有 2/3/4 之后，dashboards 追加为第 4 个页）：

```yaml
  - page_id: l2-finance
    label: 资金安全
    nav_order: 5
    cards:
    - card_id: kpi_fin_overdue
      title: 应收超期
      chart: scalar
      span: 4
    - card_id: table_fin_receivables
      title: 应收超期与账龄明细
      chart: table
      span: 12
    - card_id: table_fin_prepayment
      title: 供应商预付与对账
      chart: table
      span: 12
    - card_id: table_fin_uninvoiced
      title: 供应商未开票金额
      chart: table
      span: 12
    - card_id: table_fin_store_funds
      title: 店铺资金余额
      chart: table
      span: 12
    - card_id: table_fin_deposit
      title: 保证金台账
      chart: table
      span: 12
```

**验证**：

```bash
python -m pytest tests/common/test_bi_web_config.py tests/common/test_bi_web_app.py tests/common/test_bi_web_cards.py -x -q
python -m common.public_data.cli publish-bi   # 需 Nacos 环境
```

seed 改动会触发 `validate_dashboard_config` 全链路（config 解析 → cards 注册比对）；若 config/app 测试有页数/卡数硬断言，同步更新。publish 后确认 Nacos `DEFAULT_GROUP/bi-dashboard` 更新且 bi-web `/bi/api/dashboard` 返回 l2-finance。

**提交**：`feat(bi-web): finance safety dashboard seed (B1 Task 6)`

---

### Task 7: 【人工】dim_product 灌库 + 分类质量报告

**无代码改动，无需 commit。** 依赖：WDT 凭据文件可用（无凭据则 Task 9/11 的真实数据验证转联调环境，单测不受影响）。

1) 执行灌库：`python -m common.public_data.product_catalog`（全量 upsert + 摘要打印：spec_no 覆盖率、品牌识别率、品牌分布 Top 10）。

2) 分类质量对拍（spec §4.1 前置 checklist）：

```sql
SELECT brand_name, COUNT(*) AS specs FROM raw_wdt.dim_product
WHERE is_deleted = 0 GROUP BY brand_name ORDER BY specs DESC;

SELECT brand_name, series_name, COUNT(*) FROM raw_wdt.dim_product
WHERE is_deleted = 0 AND brand_name IN ('习酒','古韵') GROUP BY brand_name, series_name;
```

3) 产出并归档：习酒/古韵系列构成清单（Task 10/11 的实盘依据）；未识别品牌 Top 清单（导出给业务回填，开放点 §7.5）。摘要贴入执行报告。

---

### Task 8: dim_mirror — mart_ops.dim_product 镜像

**文件**：改 `common/public_data/extract_order_line.py`（Task 2 空壳）；改 `mart_extract_schema.py`；新建测试 `tests/common/test_public_data_extract_order_line.py`

**实现**：

1) mart 侧 dim_product DDL（`ddl_statements()` 追加；同构 raw_wdt 14 列 + extract 技术列。mart 侧 `synced_at` 语义 = 进 mart 时刻，源侧同步时刻保留在 raw_json）：

```sql
CREATE TABLE IF NOT EXISTS `dim_product` (
  `spec_no` VARCHAR(100) NOT NULL,
  `barcode` VARCHAR(100) DEFAULT NULL,
  `goods_no` VARCHAR(100) DEFAULT NULL,
  `goods_name` VARCHAR(500) DEFAULT NULL,
  `short_name` VARCHAR(500) DEFAULT NULL,
  `spec_name` VARCHAR(500) DEFAULT NULL,
  `brand_name` VARCHAR(200) DEFAULT NULL,
  `class_name` VARCHAR(200) DEFAULT NULL,
  `series_name` VARCHAR(100) DEFAULT NULL,
  `classify_source` VARCHAR(20) DEFAULT NULL,
  `spec_code` VARCHAR(100) DEFAULT NULL,
  `is_deleted` TINYINT NOT NULL DEFAULT 0,
  `raw_json` LONGTEXT,
  `synced_at` DATETIME(6) NOT NULL,
  `sync_run_id` CHAR(36) NOT NULL,
  PRIMARY KEY (`spec_no`),
  KEY `idx_dim_product_brand` (`brand_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
```

2) 注册数据集：

```python
ExtractDataset(
    dataset="wdt_dim_product_mirror",
    source_table="dim_product",
    target_table="dim_product",
    kind="dim_mirror",
    source="wdt",
    columns=(),
),
```

3) extract_order_line.py 实现 `project_dim_product_mirror`（替换 NotImplementedError 空壳）：

```python
_DIM_PRODUCT_COLUMNS = (
    "spec_no", "barcode", "goods_no", "goods_name", "short_name", "spec_name",
    "brand_name", "class_name", "series_name", "classify_source", "spec_code",
    "is_deleted", "raw_json",
)

def project_dim_product_mirror(repository, dataset, run_id, synced_at):
    rows = repository.read_wdt_table(dataset.source_table)  # SELECT * FROM raw_wdt.dim_product
    skipped_no_spec = 0
    out = []
    for row in rows:
        if not (row.get("spec_no") or "").strip():
            skipped_no_spec += 1
            continue
        out.append({**{c: row.get(c) for c in _DIM_PRODUCT_COLUMNS},
                    "synced_at": synced_at, "sync_run_id": run_id})
    written = repository.replace_table(
        dataset.target_table, list(_DIM_PRODUCT_COLUMNS) + ["synced_at", "sync_run_id"], out)
    return {"dataset": dataset.dataset, "target_table": dataset.target_table,
            "records_read": len(rows), "records_new": written,
            "records_updated": 0, "records_skipped": skipped_no_spec}
```

Repository 加 `read_wdt_table(table)`：用 `wdt_connection` 属性做 `SELECT *`（wdt 连接缺失时 fail loudly——设计决策 1 路由）。

**测试**：fake repository 含 wdt 行（1 行 spec_no 空 + 1 行正常）→ 断言 skip 计数、replace_table 列集合、wdt 连接被使用；注册表断言 7→8。

**提交**：`feat(extract-mart): dim_product mirror projection (B1 Task 8)`

---

### Task 9: order_line_expand — fact_order_line 订单行展开 × 品牌物化

**文件**：改 `extract_order_line.py`、`mart_extract_schema.py`；测 `tests/common/test_public_data_extract_order_line.py`

**前置**：§1.2 字段抽样已完成（trade/detail 字段名以实盘为准）。

**实现**：

1) fact_order_line DDL（`ddl_statements()` 追加）：

```sql
CREATE TABLE IF NOT EXISTS `fact_order_line` (
  `trade_no` VARCHAR(100) NOT NULL,
  `line_no` INT UNSIGNED NOT NULL,
  `spec_no` VARCHAR(100) DEFAULT NULL,
  `goods_name` VARCHAR(500) DEFAULT NULL,
  `num` DECIMAL(20,4) DEFAULT NULL,
  `paid_amount` DECIMAL(20,4) DEFAULT NULL,
  `trade_time` DATETIME DEFAULT NULL,
  `brand_name` VARCHAR(200) DEFAULT NULL,
  `series_name` VARCHAR(100) DEFAULT NULL,
  `source_system` VARCHAR(32) NOT NULL DEFAULT 'wdt',
  `synced_at` DATETIME(6) NOT NULL,
  `sync_run_id` CHAR(36) NOT NULL,
  PRIMARY KEY (`trade_no`, `line_no`),
  KEY `idx_order_line_brand_time` (`brand_name`, `trade_time`),
  KEY `idx_order_line_time` (`trade_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
```

2) 注册数据集：

```python
ExtractDataset(
    dataset="wdt_order_line_expand",
    source_table="wdt_records",
    target_table="fact_order_line",
    kind="order_line_expand",
    source="wdt",
    columns=(),
),
```

3) `project_order_lines` 实现：

```python
_TRADE_METHOD = "sales.TradeQuery.queryWithDetail"

#: 开放点 §7.3：首批「已付款计入」。取消/关闭类状态在此排除，评审后只改这里。
_EXCLUDED_TRADE_STATUSES: frozenset[str] = frozenset()  # §1.2 实盘确认后填充

_UNMATCHED_BRAND = "未匹配"

def _load_brand_lookup(repository) -> tuple[dict, int]:
    """mart dim_product → {spec_no: (brand_name, series_name)}；返回 (lookup, conflict_count)。
    同 spec_no 多行：brand_name 非空优先；两非空冲突 → 丢弃该键（计入 conflict）。"""

def project_order_lines(repository, dataset, run_id, synced_at):
    lookup, dim_conflicts = _load_brand_lookup(repository)
    trades = repository.read_wdt_trades(_TRADE_METHOD)  # SELECT source_record_id, payload_json WHERE source_method=%s
    out, unmatched_lines, skipped_status, bad_payload = [], 0, 0, 0
    for trade in trades:
        payload = json.loads(trade["payload_json"])  # JSONDecodeError → bad_payload += 1; continue
        status = str(payload.get("trade_status") or "")
        if status in _EXCLUDED_TRADE_STATUSES:
            skipped_status += 1
            continue
        trade_no = payload.get("trade_no") or trade["source_record_id"]
        trade_time = _parse_trade_time(payload.get("trade_time"))  # "%Y-%m-%d %H:%M:%S" → datetime | None
        for i, d in enumerate(payload.get("detail_list") or [], start=1):
            spec_no = (d.get("spec_no") or "").strip() or None
            info = lookup.get(spec_no) if spec_no else None
            if info is None:
                unmatched_lines += 1
            out.append({
                "trade_no": trade_no, "line_no": i,
                "spec_no": spec_no, "goods_name": d.get("goods_name"),
                "num": d.get("num"), "paid_amount": d.get("paid"),
                "trade_time": trade_time,
                "brand_name": info[0] if info else _UNMATCHED_BRAND,
                "series_name": info[1] if info else None,
                "source_system": "wdt",
                "synced_at": synced_at, "sync_run_id": run_id,
            })
    written = repository.replace_table(dataset.target_table, [...12 列...], out)
    return {"dataset": ..., "records_read": len(trades), "records_new": written,
            "records_updated": 0, "records_skipped": skipped_status,
            "unmatched_lines": unmatched_lines, "dim_conflicts": dim_conflicts,
            "bad_payload": bad_payload}
```

Repository 加 `read_wdt_trades(method)`。计数键（unmatched_lines / dim_conflicts / bad_payload）随 `_print_success` 摘要原样输出——`sync_dataset_summary` 表结构不动。

**测试**：fake 3 笔 trade（1 笔排除状态、1 笔 detail 2 行其中 1 行 spec 未匹配、1 笔坏 JSON）→ 断言行数、line_no 序号、`未匹配` 物化、三类计数；`_load_brand_lookup` 冲突两态；注册表断言 8→9；DDL 覆盖断言 9 张表。

**提交**：`feat(extract-mart): order line expansion with brand materialization (B1 Task 9)`

---

### Task 10: dim_target 扩展 — brand/series_group 两行 seed

**文件**：改 `docker/integration/target.seed.json`；测 `tests/common/test_public_data_target_seed.py`

**实现**：seed 追加两行（值 = Task 7 实盘系列清单确认后填入；下方为占位结构）：

```json
{"scope": "brand", "scope_key": "习酒", "year": 2026, "annual_target": 616310000},
{"scope": "series_group", "scope_key": "古韵+大坛", "year": 2026, "annual_target": 0}
```

**注意**：`annual_target` 单位沿用现有 seed 口径——**先对照文件现有行确认是元还是万元**，以现有行单位为准（61631 万为 spec 目标值）。古韵+大坛目标值业务未定 → 先 0 占位并在卡面 note 提示「目标待录入」，或等评审后填。

`target_seed.py` 零改动（设计决策 7）；`load-target` 全量替换语义天然覆盖新行。

**测试**：test_public_data_target_seed.py 追加用例——两行新 scope 解析通过、year/annual_target 类型正确。然后执行灌库：

```bash
python -m common.public_data.cli load-target
python -m pytest tests/common/test_public_data_target_seed.py -x -q
```

**提交**：`feat(public-data): brand and series-group targets in dim_target seed (B1 Task 10)`

---

### Task 11: queries.py 品牌两卡 + cards.py 注册

**文件**：改 `queries.py`、`cards.py`；测 `test_bi_web_queries.py`、`test_bi_web_cards.py`

**实现**：

1) SQL 常量：

```python
_ORDER_LINE_YEAR_WINDOW = (
    "trade_time >= MAKEDATE(YEAR(CURDATE()), 1) AND trade_time <= NOW()"
)

_BRAND_XIJIU_TOTAL_SQL = f"""
SELECT COALESCE(SUM(paid_amount), 0) AS total
FROM fact_order_line
WHERE brand_name = '习酒' AND {_ORDER_LINE_YEAR_WINDOW}
"""

_BRAND_XIJIU_SERIES_SQL = f"""
SELECT COALESCE(series_name, '未匹配') AS series, SUM(paid_amount) AS total
FROM fact_order_line
WHERE brand_name = '习酒' AND {_ORDER_LINE_YEAR_WINDOW}
GROUP BY series ORDER BY total DESC
"""

#: 开放点 §7.4：古韵+大坛组口径首批按系列名 LIKE，Task 7 实盘清单确认后改静态 IN。
_SERIES_GROUP_TOTAL_SQL = f"""
SELECT COALESCE(SUM(paid_amount), 0) AS total
FROM fact_order_line
WHERE (series_name LIKE '%古韵%' OR series_name LIKE '%大坛%')
  AND {_ORDER_LINE_YEAR_WINDOW}
"""

_UNMATCHED_RATIO_SQL = f"""
SELECT
  SUM(CASE WHEN brand_name = '未匹配' THEN paid_amount ELSE 0 END) AS unmatched,
  SUM(paid_amount) AS total_all
FROM fact_order_line
WHERE {_ORDER_LINE_YEAR_WINDOW}
"""

_TARGET_BY_SCOPE_SQL = """
SELECT annual_target FROM dim_target
WHERE scope = %s AND scope_key = %s AND year = YEAR(CURDATE())
"""
```

`_TARGET_BY_SCOPE_SQL` 是 `annual_target_total()` 的参数化推广——**保留原函数不动**，新增 `target_for_scope(scope, scope_key)`。

2) run_* 函数（与 `run_kpi_annual_progress` 同构：value/target/progress 三键 + note）：

```python
def run_kpi_brand_xijiu_progress() -> dict:
    # value=年累计 paid_amount；target=61631万（dim_target）；progress 复用
    # note=系列构成文本，如 "系列构成：君品 45% · 窖藏 30% · 金钻 15% …"
    # 未匹配占比（_UNMATCHED_RATIO_SQL）> 5% 时 note 追加 " · 警示：未匹配 X%"

def run_kpi_series_group_progress() -> dict:
    # 同构；target 为 0/未录入时 note 提示 "目标待录入"，progress 不渲染
```

3) cards.py 注册：

```python
_card("kpi_brand_xijiu_progress", "习酒品牌年累计", "scalar", {}),
_card("kpi_series_group_progress", "古韵+大坛系列年累计", "scalar", {}),
```

**测试**：

- queries：fake 断言——`_ORDER_LINE_YEAR_WINDOW` 年初锚 `MAKEDATE(YEAR(CURDATE()), 1)` 字面出现；品牌过滤字面量 `'习酒'` 在 SQL 内（静态、非拼接用户值）；目标查询走 `%s` 参数；note 系列构成按占比降序拼接、未匹配 >5% 才追加警示
- cards：注册断言追加 2 卡

**提交**：`feat(bi-web): brand and series-group progress cards (B1 Task 11)`

---

### Task 12: seed L1 品牌两卡 + 端到端验收

**文件**：改 `docker/integration/bi.seed.yaml`

**实现**：L1 补齐第三行（kpi_fin_alert 之前插入，构成 4+4+4）：

```yaml
    - card_id: kpi_brand_xijiu_progress
      title: 习酒品牌年累计
      chart: scalar
      span: 4
    - card_id: kpi_series_group_progress
      title: 古韵+大坛系列年累计
      chart: scalar
      span: 4
    - card_id: kpi_fin_alert
      title: 资金预警
      chart: scalar
      span: 4
```

**验证**：全量测试 `python -m pytest tests/common -x -q`（含 config/cards/app 合约；页数/卡数硬断言失败则同步更新常量）。然后进入 §7 端到端验收。

**提交**：`feat(bi-web): L1 brand and finance-alert cards seed (B1 Task 12)`

---

## 7. 端到端验收 checklist（Task 12 完成后执行）

```bash
python -m common.public_data.cli extract-mart
python -m common.public_data.cli publish-bi
```

- [ ] extract 摘要：9 数据集全绿；`fact_fin_store_funds` 行数 = 店铺数 × 非空月数；`statement_date_unparsed` / `unmatched_lines` / `dim_conflicts` 计数可见
- [ ] 资金安全页 6 卡渲染；抽 1 家超期往来单位与 AI 表格原值对拍一致；超期行标红生效
- [ ] L1 三新卡渲染；习酒年累计与 WDT 后台「已付款订单金额」口径对拍（差异写清口径说明）
- [ ] 品牌卡 note 系列构成、未匹配占比警示（>5% 时）生效
- [ ] 断 raw_dingtalk / raw_wdt 单源故障 → 对应卡 503 占位、其余卡正常（复用阶段 B 隔离测试姿势）
- [ ] 旧页（l2-region/channel/people）零回归
- [ ] 出界项确认未滑入：fin_daily_funds 未建、无资金明细下钻页、无 AI 表格源迁移

---

## 8. 全景核对

| 项 | 覆盖 Task |
|---|---|
| spec §2 WP1 六卡 + L1 预警 | Task 1-6 |
| spec §3 WP2 dim_product 镜像 + fact_order_line | Task 7-9 |
| spec §3.4 dim_target 扩展 | Task 10 |
| spec §3.5 品牌两卡 | Task 11-12 |
| spec §1.1 升级/降级机制骨架（source_system 列） | Task 9（列）+ Task 11（查询层预留） |
| spec §4.1 开放点保守默认值 | 决策 5/6 + Task 10 占位 + Task 11 注释锚点 |
| 出界项（fin_daily_funds、明细页等） | 不在任何 Task，验收时确认未滑入 |

---

## 9. 注意事项与风险

- **金额单位**：target.seed.json 的 `annual_target` 单位以文件现有行为准（元或万元），Task 10 执行前必须先确认；卡面展示统一走 `_fmt_wan_text`（元→万）。
- **快照语义**：5 张 fin 表 + dim_product + fact_order_line 均为全量替换，重跑 extract-mart 幂等；不要在查询层加去重/取最新逻辑。
- **payload 字段名**：Task 9 所有字段名（trade_status、detail_list 内字段）以 §1.2 实盘抽样为准，发现不符先改常量再开发。
- **升级/降级机制**：本阶段只落骨架（fact_order_line 的 `source_system` 列 + 查询层预留），现有销售页卡（l2-region/channel/people）的口径迁移在 B2，**不要在本阶段动它们**。
- **故障隔离**：WDT 源连接缺失时，wdt 源数据集必须 fail loudly（抛 MartExtractError），不允许静默写空表。



