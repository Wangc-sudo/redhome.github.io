# fact_manual_report 消费接口草案（BI 卡片 × 人工报表通道）

日期：2026-09-16 · 作者：data-agent（D 线）· 状态：草案，待 bi-backend-agent 评审落地
范围：只定义**消费接口**（SQL 片段 / 查询函数签名 / 载荷约定）；落地代码归 `common/bi_web/**`（bi-backend-agent 所有权），本文件不改任何 bi_web 代码。

---

## 1. 背景

人工报表导入通道（`common/public_data/manual_import/`）已就绪：模板 `ecommerce_monthly` / `restaurant_monthly` / `generic`，CLI `import-manual` 默认 dry-run。投影产物是 `mart_ops.fact_manual_report` 窄表。roadmap P1 明确欠「BI 侧卡片 SQL 消费 `fact_manual_report`」。本文件是该消费的接口契约，覆盖需求 ④⑤⑥⑪⑬。

## 2. 数据契约（投影层既有事实，消费方不得重新解释）

### 2.1 表结构（`common/public_data/manual_import/schema.py` 为唯一真源）

| 列 | 类型 | 语义 |
|---|---|---|
| `dataset` | VARCHAR(64) | 数据集身份，即模板名（`ecommerce_monthly` 等） |
| `period_type` | ENUM('month','quarter','year') | 报表时间颗粒度 |
| `period_start` / `period_end` | DATE | 期间**闭区间**，投影时已推导好，消费方直接用 |
| `dim_scope` | VARCHAR(64) | 维度字段名，多维用 `+` 连接（如 `channel` / `store`） |
| `dimension_value` | VARCHAR(255) | 维度取值，多维用 `\|` 连接，与 `dim_scope` 位置对齐 |
| `metric` | VARCHAR(64) | 指标字段名（如 `revenue` / `gross_profit` / `expense`） |
| `value` | DECIMAL(20,4) NULL | 指标值；**NULL = 选填未填，不是 0** |
| `unit` | VARCHAR(16) | 单位（当前模板一律 `元`） |
| `source_run_id` | CHAR(36) | 溯源到 `manual_import_runs` |
| `synced_at` | DATETIME(6) | 投影时间 |

唯一键 `(dataset, period_type, period_start, dim_scope, dimension_value, metric)`：**同一指标同一期间同一维度只有一行**，故对任意分组 `SUM(value)` 不会重复计数（无 melt 陷阱，与 `fact_daily_report_offline` 的 month_target 不同）。

### 2.2 行为语义

1. **覆盖语义**：同 dataset + 同 period 重导 = 整段先删后插。消费方看到的一定是最新版，不存在新旧两版混算。
2. **总计行不入库**：模板 `total_check` 声明的「合计」行只用于导入时对账，**不会**出现在窄表里。消费方**不需要** `NOT LIKE '%合计%'` 过滤（与线下日报表的关键差异）。
3. **无未来预填行**：人工月报按整个期间一次导入，不存在 `business_date > CURDATE()` 的预填行。消费方**不做 CURDATE 截断**——历史月就该看到全月数。
4. **空值诚实**：选填指标留空 → 不出行；`value` 为 NULL → 前端显示「—」，绝不替换成 0。

### 2.3 维度/指标字典（当前三个模板）

| dataset | period_type | dim_scope | metric（unit=元） | 支撑需求 |
|---|---|---|---|---|
| `ecommerce_monthly` | month | `channel` | `revenue` / `gross_profit` / `expense` | ④ |
| `restaurant_monthly` | month | `store` | `revenue` / `gross_profit` / `expense` | ⑤ |
| `showroom_monthly`（待克隆，P1） | month | `store` | 同上 | ⑪ |
| 季度预实 / 2025 基数（待 `generic` 实例化，<待填：财务表头>） | quarter / month | <待填> | <待填> | ⑥⑬ |

**metric 集合随模板演进会增列**。消费方一律按「窄行动态展开」写 SQL（见 §3.2），**绝不**把 metric 名写死成 `SUM(CASE WHEN metric='revenue' ...)` 的列——模板加一列指标，卡片 SQL 不该跟着改（投影器 docstring 的既定原则）。

## 3. 消费接口草案

### 3.1 放置与分层（与现有主线一致）

- SQL 常量 + `run_*` 卡片函数 → `common/bi_web/queries.py`（bi-backend-agent 落地）；
- 派生口径（毛利率、环比、同比）→ `common/bi_web/derived.py` 纯函数 + `tests/fixtures/derived_golden.json` 夹具（**SQL 只取事实，派生一律调 derived**，CubeSchema §2 既定纪律）；
- 金额单位一律 `元`（`_UNIT = "元"`），前端负责「万」格式化。

### 3.2 基础取数函数（窄行原样取回，Python 侧展开）

```python
# queries.py（建议追加）

#: 人工报表窄行：一个 dataset 一个期间的全部维度×指标行。
#: 不做 CURDATE 截断（无预填行），不排除合计（总计行不入库）。
_MANUAL_REPORT_ROWS_SQL = (
    "SELECT dim_scope, dimension_value, metric, value, unit "
    "FROM fact_manual_report "
    "WHERE dataset = %s AND period_start = %s"
)


def manual_report_rows(connection, *, dataset, period_start) -> list:
    """只读事实：某 dataset 某期间的窄行列表。

    ``period_start`` 由调用方按 period_type 推导（month→月首，
    quarter→季首，year→元旦），复用 ``month_bounds`` 的日历口径。
    返回 ``[{dim_scope, dimension_value, metric, value(Decimal|None), unit}]``。
    """
```

期间推导建议复用 `manual_import.template.parse_period` 已验证的 month/quarter/year 区间逻辑（或 `month_bounds`），**不在卡片层重写**。

### 3.3 Python 展开（窄行 → 宽行，模板无关）

```python
def pivot_manual_rows(rows, *, dim_scope) -> list:
    """窄行 → [{name, <metric>: Decimal|None, ...}]，按维度值聚合。

    * 只保留 ``dim_scope`` 匹配的行（防御多维模板）；
    * 同一 (dimension_value, metric) 在表内唯一，直接取值不 SUM；
    * 缺失指标 → 键不存在（消费方按 None 处理，显示「—」）。
    """
```

派生指标（如毛利率 = gross_profit ÷ revenue）**不在此处算**，交 derived 新增纯函数（如 `margin(part, whole)`，分母 0/None → None），并补 golden 夹具。

### 3.4 卡片函数签名（建议，按需求逐页）

```python
def run_table_manual_monthly(connection, params) -> dict:
    """④⑤⑪ 月度经营对比表：dataset/month 参数化，维度 × 指标 + 派生列。

    params: dataset（URL 白名单：ecommerce_monthly|restaurant_monthly|showroom_monthly）、
            month（缺省当前月）。
    载荷: {"chart": "table", "unit": "元", "month": ..., "dataset": ...,
           "columns": [{key:维度名}, 每指标一列, 派生列...],
           "rows": [...]}
    """

def run_kpi_manual_yoy(connection, params) -> dict:
    """⑬ 月度同比：同 dataset 本月 vs 去年同月 Σmetric。

    依赖 dim_calendar 跨年到 2025（见 2026-09-16-calendar-cross-year-design.md）
    与 2025 基数导入（generic 模板）。分子分母各取一次 manual_report_rows 后 SUM；
    基期为 NULL（未导入）→ rate=None → 前端「—」，绝不按 0 算 -100%。
    """
```

### 3.5 与既有卡片口径的一致性纪律（消费方必须遵守）

1. `params` 取值一律走 `%s` 绑定，dataset/month 在 app 层过白名单（同 `region/channel` 现状）；
2. 返回值 `Decimal → float` 在 `run_*` 边界完成；低层函数保持 `Decimal`；
3. 派生（环比/同比/毛利率/占比）一律 derived 纯函数 + golden 对拍，SQL 只取事实；
4. 静态 SQL 有 `StaticSqlTests` 钉死——新增 `_MANUAL_*` SQL 常量需同步追加式更新测试断言（不回退既有断言，参照 roadmap §3 裁决 #1）。

## 4. 投影侧缺口评估（D 线自查结论）

| 检查项 | 结论 |
|---|---|
| 字段完整性 | ✅ 无需补列：dataset/period/dim/metric/value/unit/run_id 已覆盖全部消费场景 |
| 索引 | ✅ 现有 `uk_manual_report(dataset, period_type, period_start, ...)` 与 `idx_manual_report_dataset_period(dataset, period_start)` 支撑 §3.2 的等值查询；月报数据量（每 dataset 每月 ≤ 数百窄行）无需新索引 |
| period_end | ✅ 已落库，季度/年度卡可直接用闭区间，BI 侧不必再推导 |
| 多维支持 | ✅ `dim_scope`/`dimension_value` 的 `+`/`|` 连接已定义；消费侧按位置拆即可 |

**结论：manual_import 投影零改动即可支撑消费；本任务不在独占范围补任何字段/索引。**

## 5. 转派事项（→ main 转 bi-backend-agent）

1. 在 `queries.py` 按 §3.2–3.4 落地 `_MANUAL_REPORT_ROWS_SQL` / `manual_report_rows` / `pivot_manual_rows` / `run_table_manual_monthly`（④⑤页先行）；
2. `derived.py` 增 `margin(part, whole)` 纯函数 + golden 夹具条目；
3. `app.py` 侧 dataset/month 参数白名单接线（bi-backend-agent 所有权内）；
4. ⑬ 同比卡**暂缓**：等日历跨年（本期仅出设计）与 2025 基数首填后启用；
5. 追加式更新 `StaticSqlTests` 断言。
