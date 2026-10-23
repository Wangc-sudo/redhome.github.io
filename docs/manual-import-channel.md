# 人工报表导入通道（C 类数据源）

需求 ④⑤⑥⑪⑬ 的共同瓶颈是：毛利、费用、预算、2025 历史基数都是**人工
报表**，无进库通道。本通道是通用底座：一份 YAML 模板 + 一条 CLI 命令，即
「人工报表 → raw_manual → mart_ops.fact_manual_report」，BI 只读 mart。

## 一、模板规范

模板目录：`docker/integration/manual-import-templates/<dataset>.yaml`
（文件名即 `--template` 取值，必须是 `[a-z][a-z0-9_]*`）。

| 字段 | 说明 |
| --- | --- |
| `version` | 固定 `1`，结构变更即 +1，旧模板当场拒绝 |
| `dataset` / `target_table` | 数据集身份（raw 按 dataset 归档，mart 按 dataset 投影） |
| `period_type` | `month` / `quarter` / `year`，决定 `period_start`/`period_end` |
| `columns[].source` | 报表里的表头原文（允许空格、全角） |
| `columns[].field` | 标准字段名，进库与投影都用它 |
| `columns[].role` | `period` / `dimension` / `metric`（至少 1 维度 + 1 指标） |
| `columns[].type` | `period` / `string` / `decimal` / `int` / `date` |
| `columns[].required` | 必填为空即报错，整行不导入 |
| `columns[].enum` | 取值白名单；取值固定的字典（渠道、板块）才设，门店/人员不设 |
| `columns[].unit` | 指标单位（如 `元`），随窄表落库，避免卡片猜单位 |
| `total_check` | 合计校验：表达式只支持 `sum(<指标>) == total[.<指标>]`（不 eval）；`total_row` 指定报表总计行的维度取值；`tolerance` 为相对容差（绝对下限 0.01） |
| `extreme_value_threshold` | 单项指标绝对值超过该值给 warning（单位填错是最常见事故） |

新增数据集 = 新增一个 YAML，不改代码、不改表结构；可从 `generic.yaml`
占位模板复制（接入步骤见文末 SOP）。

## 二、CLI 用法

```bash
# 1) 默认 dry-run：只打印校验报告，一个字节都不写
python -m common.public_data.cli import-manual \
    --template ecommerce_monthly --file 电商月报-2026-08.xlsx --period 2026-08

# 2) 确认无误后落库（raw_manual + mart_ops）
python -m common.public_data.cli import-manual \
    --template ecommerce_monthly --file 电商月报-2026-08.xlsx \
    --period 2026-08 --apply --imported-by 张三
```

* `--period` 必须与模板 `period_type` 匹配（`2026-08` / `2026-Q3` / `2026`）；
* 报告含 `errors` 时**整批拒绝**（`status=rejected`），退出码非 0，不半截入库；
* 文件支持 CSV（UTF-8，容忍 BOM）与 XLSX（首个工作表，需 openpyxl）。

## 三、落库与幂等

| 库表 | 内容 |
| --- | --- |
| `raw_manual.manual_import_runs` | 导入审计头：run_id/dataset/模板版本/文件名/sha256/期间/行数/status/导入人/时间 |
| `raw_manual.manual_import_row` | 原始行：`source_json`（报表原样，可复核）+ `fields_json`（标准化字段） |
| `mart_ops.fact_manual_report` | 窄表：`dataset, period_type, period_start, period_end, dim_scope, dimension_value, metric, value, unit, source_run_id` |

* **同文件重复导入**：`uk_dataset_period_file` 命中即 `duplicate`，一行不写；
* **同期间换文件重导**：raw 与 mart 均按 dataset+period **先删后插**，覆盖而非追加；
* 报表「合计」行只用于对账，**不导入**（避免与明细重复计数）；
* 建表走 `python -m common.public_data.cli migrate`（`raw-manual-v1` +
  `mart-ops-manual-report-v1`），`raw_manual_test` 库见
  `docker/integration/mysql-init/002-create-manual-database.sql`。

## 四、BI 取数

```sql
SELECT dimension_value, SUM(value)
FROM mart_ops.fact_manual_report
WHERE dataset = 'ecommerce_monthly' AND metric = 'revenue'
  AND period_start >= '2026-01-01' AND period_start < '2027-01-01'
GROUP BY dimension_value;
```

卡片 SQL 与前端呈现由**口径主线**负责，本通道只保证窄表按上述契约供数。

## 五、接入新报表 SOP（业务自助，不用改代码）

1. **业务给表头**：拿到报表样例（表头原文 + 一行数据 + 是否有「合计」行）；
2. **复制模板**：`cp generic.yaml <dataset>.yaml`，改 `dataset`（必须全局唯一，
   同名会互相覆盖）与 `target_table`；`period_type` 按颗粒度选 month/quarter/year；
3. **填列映射**：每列 `source` 换成表头原文，`field` 定标准字段名——
   **field 即 BI 侧 metric 名**，定名前先与口径主线对齐；取值固定的维度加 `enum`；
4. **dry-run**：`import-manual --template <dataset> --file <样例> --period <期间>`，
   看校验报告；`errors` 非 0 就按行号回改报表或模板，直到 `status=ok`；
5. **落库**：确认后加 `--apply --imported-by <姓名>`（同文件重跑幂等，换文件覆盖）；
6. **交接口径主线**：告诉他 `fact_manual_report` 里 dataset=<dataset> 的 metric
   命名与单位，卡片 SQL 由他写，本通道不碰 bi_web。

占位模板（`source` 为「待填-*」）会被当场拒绝，防止未填就 `--apply`。

## 六、平台账单（渠道BI 阶段4 B 方案，2026-10-23）

财务/运营每月从各平台后台导出账单，整理成五列后导入：

```
月份,平台,店铺,费用科目,金额
2025-01,拼多多,PDD习酒旗舰店,推广-全站,12345.60
2025-01,天猫,,平台扣点,5200.50
```

模板 `platform_bill_monthly`（平台 × 店铺 × 费用科目 × 月，单位元）。

```bash
docker exec -w /opt/dops/repo -e PYTHONPATH=/opt/dops/repo dops-scheduler \
  python3 -m common.public_data.cli import-manual \
  --template platform_bill_monthly --file /tmp/平台账单-2026-10.csv \
  --period 2026-10 --apply --imported-by <姓名>
```

**与财务AI表三张账单卡的分工**：既有 `table_qd_expense*` / `table_qd_bill_*` 读
`fact_fin_store_expense`（店铺扣点 + 推广充值，财务AI表同步），本模板补的是财务表
里**没有**的推广类型明细（关键词/人群/全站）与发货费/电商税/运费险/服务费。两者
互补、不重复计数。A 方案（平台 API 直连）上线后只需换源，卡面不变。

**BI 侧两张卡**（渠道看板，`dataset=platform_bill_monthly`）：

* `table_qd_bill_manual` —— 按费用科目汇总 + 占比；
* `table_qd_bill_manual_detail` —— 平台 × 店铺 × 费用科目 明细，金额降序。

**月份口径**：账单是月报、导入常滞后于月份，两卡取「不晚于锚点月的**最近一个已
导入月份**」，月份号显示在列标题（`金额(2025-01)`）上——不静默冒充当月；从未导入
则空表且标题不带月份。

**窄表形态**（写卡片 SQL 前必读）：多维度被压成 `dim_scope`（字段名 `+` 连接）
与 `dimension_value`（取值 `|` 连接），空维度留空串：

```
dim_scope        = 'platform+store+fee_item'
dimension_value  = '天猫||平台扣点'      # 无店铺
metric           = 'amount'   unit = '元'
```

## 七、遗留项

* ⑥季度预实 / ⑨ / ⑪体验馆 / ⑬月度同比 的模板待业务给表头，照上节 SOP 填即可；
* 暂不提供「按期间重投历史」的独立命令，重投即重跑 `import-manual --apply`；
* 平台账单**真实月份数据尚缺**：现库里只有财务样例（2025-01，3 行）用于验收，
  拿到真实账单后按上节命令整月导入即可覆盖（同期间换文件=先删后插）。
