# 财务钉钉 AI 表与 RDS 原始表映射

## 范围

本说明仅定义钉钉 AI 表到 `raw_dingtalk` 数据库的原始镜像关系。一个钉钉 Sheet 对应一张物理 MySQL 表；不在原始层把月度或每日交叉列拆为指标事实表。

宽表的期间展开、跨表指标和 BI 口径应在后续 `mart_finance` 层完成，不改变这里的源表镜像关系。

## 通用规则

### 技术列

每张原始表都有以下非业务列。这些列不对应 AI 表头：

| DB 列 | 类型 | 来源/用途 |
|---|---|---|
| `dingtalk_record_id` | `VARCHAR(255) NOT NULL` | 钉钉记录 ID，主键 |
| `synced_at` | `DATETIME(6) NOT NULL` | 本次同步写入时间 |
| `sync_run_id` | `CHAR(36) NOT NULL` | 同步批次标识 |

统一约束和索引：

```sql
PRIMARY KEY (dingtalk_record_id),
KEY idx_synced_at (synced_at)
```

不在原始层添加跨表外键或业务唯一键。AI 表字段在本次发现中均未声明可靠的必填信息，因此业务列均允许 `NULL`。

### 字段类型转换

| 钉钉字段类型 | MySQL 类型 | 说明 |
|---|---|---|
| `text` | `VARCHAR(255)` 或 `TEXT` | 筛选维度用 `VARCHAR(255)`；说明类长文本用 `TEXT` |
| `singleSelect` | `VARCHAR(255)` | 保存选项文本，不固化选项 ID |
| `number`、`currency` | `DECIMAL(20,4)` | 统一保留金额、税率和数量精度 |
| `date` | `DATE` | 仅适用于钉钉实际类型为 `date` 的字段 |
| `user`、`multipleSelect`、`unidirectionalLink` | `JSON` | 完整保留人员、选项或关联记录结构 |

下表中的“钉钉字段”是唯一的业务字段来源。`*_raw` 表示钉钉字段实际为 `text`，原始层不做日期或金额强制转换。

## 表映射

### `fin_store_commission`

来源数据集：`finance_store_commission`；Sheet：店铺扣点费用管理。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 公司主体 | `company_entity` | `VARCHAR(255)` |
| 费用项目 | `fee_item` | `VARCHAR(255)` |
| 店铺名称 | `store_name` | `VARCHAR(255)` |
| 填写人 | `submitted_by` | `JSON` |
| 备注 | `remark_values` | `JSON` |
| 平台 | `platform` | `VARCHAR(255)` |
| 费用类别 | `fee_category` | `VARCHAR(255)` |
| 2026-01 至 2026-12 | `amount_2026_01` 至 `amount_2026_12` | 各 `DECIMAL(20,4)` |

索引：`KEY idx_company_store (company_entity, store_name)`。

### 纳税申报数据集：`finance_tax_filing`

#### `fin_tax_declaration_2026`

来源 Sheet：纳税申报核算表-2026。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 日期 | `tax_date` | `DATE` |
| 申报日期 | `filing_date` | `DATE` |
| 实际申报的增值税 | `actual_vat_amount` | `DECIMAL(20,4)` |
| 实际申报的收入 | `actual_declared_revenue` | `DECIMAL(20,4)` |
| 校验2 | `validation_note` | `TEXT` |
| 工会经费 | `union_fund_amount` | `DECIMAL(20,4)` |
| 社保 | `social_insurance_amount` | `DECIMAL(20,4)` |
| 申报的工资总额 | `declared_payroll_amount` | `DECIMAL(20,4)` |
| 个人所得税 | `individual_income_tax_amount` | `DECIMAL(20,4)` |
| 城镇土地使用税 | `urban_land_use_tax_amount` | `DECIMAL(20,4)` |
| 房产税 | `property_tax_amount` | `DECIMAL(20,4)` |
| 企业所得税 | `corporate_income_tax_amount` | `DECIMAL(20,4)` |
| 进项税额转出 | `input_tax_transfer_note` | `TEXT` |
| 上期留抵税额 | `prior_period_input_tax_credit` | `DECIMAL(20,4)` |
| 申报人 | `filer` | `JSON` |
| 公司主体 | `company_entity` | `VARCHAR(255)` |

索引：`KEY idx_company_tax_date (company_entity, tax_date)`。

#### `fin_tax_sales_reconciliation_2026`

来源 Sheet：销售核对表-2026。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 公司主体 | `company_entity` | `VARCHAR(255)` |
| 销售单销售金额（13%） | `sales_order_amount_13pct` | `DECIMAL(20,4)` |
| 销货单销售金额（6%） | `sales_delivery_amount_6pct` | `DECIMAL(20,4)` |
| 总账销售收入 | `general_ledger_sales_revenue` | `DECIMAL(20,4)` |
| 期间 | `period_date` | `DATE` |
| 填表人 | `submitted_by` | `JSON` |
| 销货单销售金额（其他税率） | `sales_delivery_amount_other_rate` | `DECIMAL(20,4)` |
| 销项税额2 | `output_tax_amount_2` | `DECIMAL(20,4)` |

索引：`KEY idx_company_period (company_entity, period_date)`。

#### `fin_tax_stamp_duty`

来源 Sheet：印花税核算表。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 应税凭证名称 | `taxable_document_name` | `VARCHAR(255)` |
| 计税金额 | `taxable_amount` | `DECIMAL(20,4)` |
| 适用税率 | `applicable_tax_rate` | `VARCHAR(255)` |
| 经办人 | `handler` | `JSON` |
| 公司主体 | `company_entity` | `VARCHAR(255)` |
| 所属纳税申报期 | `tax_filing_period` | `DATE` |

索引：`KEY idx_company_filing_period (company_entity, tax_filing_period)`。

#### `fin_tax_uninvoiced_sales_summary`

来源 Sheet：无票销售申报汇总表。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 销售公司名称 | `sales_company_name` | `VARCHAR(255)` |
| 无票销售税率 | `uninvoiced_sales_tax_rate` | `VARCHAR(255)` |
| 期间 | `period_date` | `DATE` |
| 开具以前月份发票税率 | `prior_period_invoice_tax_rate` | `VARCHAR(255)` |
| 预开票税率 | `pre_invoice_tax_rate` | `VARCHAR(255)` |
| 无票申报税率 | `uninvoiced_filing_tax_rate` | `VARCHAR(255)` |
| 经办人 | `handler` | `JSON` |

索引：`KEY idx_sales_company_period (sales_company_name, period_date)`。

#### `fin_tax_uninvoiced_sales`

来源 Sheet：无票销售登记。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 公司主体 | `company_entity` | `VARCHAR(255)` |
| 经办人 | `handler` | `JSON` |
| 无票销售税率 | `uninvoiced_sales_tax_rate` | `VARCHAR(255)` |
| 无票销售不含税金额 | `uninvoiced_sales_excl_tax_amount` | `DECIMAL(20,4)` |
| 期间 | `period_date` | `DATE` |

索引：`KEY idx_company_period (company_entity, period_date)`。

#### `fin_tax_prior_period_invoice`

来源 Sheet：开具以前月份发票登记表。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 公司主体 | `company_entity` | `VARCHAR(255)` |
| 经办人 | `handler` | `JSON` |
| 开具以前月份发票税率 | `prior_period_invoice_tax_rate` | `VARCHAR(255)` |
| 开具以前月份发票不含税金额 | `prior_period_invoice_excl_tax_amount_raw` | `VARCHAR(255)` |
| 期间 | `period_date` | `DATE` |
| 父记录 | `parent_record_refs` | `JSON` |

索引：`KEY idx_company_period (company_entity, period_date)`。

#### `fin_tax_pre_invoice`

来源 Sheet：预开票登记表。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 公司主体 | `company_entity` | `VARCHAR(255)` |
| 经办人 | `handler` | `JSON` |
| 预开票税率 | `pre_invoice_tax_rate` | `VARCHAR(255)` |
| 预开票不含税金额 | `pre_invoice_excl_tax_amount` | `DECIMAL(20,4)` |
| 期间 | `period_date` | `DATE` |

索引：`KEY idx_company_period (company_entity, period_date)`。

#### `fin_tax_input_invoice`

来源 Sheet：进项发票管理表。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 公司 | `company_entity` | `VARCHAR(255)` |
| 开票日期 | `invoice_date` | `DATE` |
| 销售方名称 | `seller_name` | `VARCHAR(255)` |
| 不含税金额 | `excl_tax_amount` | `DECIMAL(20,4)` |
| 税率 | `tax_rate` | `VARCHAR(255)` |
| 认证月份 | `certification_month` | `DATE` |
| 入账月份 | `accounting_month` | `DATE` |
| 经办人 | `handler` | `JSON` |
| 发票编号 | `invoice_number` | `VARCHAR(255)` |

索引：`KEY idx_company_invoice_date (company_entity, invoice_date)`，`KEY idx_invoice_number (invoice_number)`。

### `fin_ecommerce_prepayment_supplier_invoice`

来源数据集：`finance_ecommerce_prepayment_supplier_invoices`。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 说明要求 | `requirement_note` | `TEXT` |
| 应付-暂估账面 | `accounts_payable_estimated_ledger_amount` | `DECIMAL(20,4)` |
| 公司主体 | `company_entity` | `VARCHAR(255)` |
| 账账相符核对 | `ledger_reconciliation_status` | `VARCHAR(255)` |
| 未到货未到票情况说明 | `undelivered_uninvoiced_note` | `TEXT` |
| 核对人 | `reviewer` | `JSON` |
| 未到票金额 | `uninvoiced_amount` | `DECIMAL(20,4)` |
| 日期 | `statement_date_raw` | `VARCHAR(255)` |
| 账面预付账款 | `prepayment_ledger_amount` | `DECIMAL(20,4)` |
| 供应商名称 | `supplier_name` | `VARCHAR(255)` |
| 填写人 | `submitted_by` | `JSON` |

索引：`KEY idx_company_supplier (company_entity, supplier_name)`。

### `fin_ecommerce_promotion_recharge_balance`

来源数据集：`finance_ecommerce_promotion_recharge_balance`。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 公司主体 | `company_entity` | `VARCHAR(255)` |
| 店铺名称 | `store_name` | `VARCHAR(255)` |
| 平台 | `platform` | `VARCHAR(255)` |
| 费用项目 | `fee_item` | `VARCHAR(255)` |
| 期初余额 | `opening_balance` | `DECIMAL(20,4)` |
| 填写人 | `submitted_by` | `VARCHAR(255)` |
| 2026-01 至 2026-12 | `amount_2026_01` 至 `amount_2026_12` | 各 `DECIMAL(20,4)` |

索引：`KEY idx_company_store_fee (company_entity, store_name, fee_item)`。

### `fin_ecommerce_platform_deposit`

来源数据集：`finance_ecommerce_platform_deposit`。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 保证金缴纳时间 | `deposit_paid_at_raw` | `VARCHAR(255)` |
| 店铺是否正常运营 | `store_operating_status` | `VARCHAR(255)` |
| 平台 | `platform` | `VARCHAR(255)` |
| 填写人 | `submitted_by` | `JSON` |
| 公司主体 | `company_entity` | `VARCHAR(255)` |
| 保证金余额 | `deposit_balance` | `DECIMAL(20,4)` |
| 店铺名称 | `store_name` | `VARCHAR(255)` |
| 项目 | `project_name` | `VARCHAR(255)` |
| 是否复核 | `review_status` | `VARCHAR(255)` |
| 复核人 | `reviewer` | `JSON` |

索引：`KEY idx_company_store (company_entity, store_name)`。

### `fin_ecommerce_store_funds_balance`

来源数据集：`finance_ecommerce_store_funds_balance`。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 店铺名称 | `store_name` | `VARCHAR(255)` |
| 渠道 | `channel` | `VARCHAR(255)` |
| 公司主体 | `company_entity` | `VARCHAR(255)` |
| 是否复核 | `review_status` | `VARCHAR(255)` |
| 复核人 | `reviewer` | `JSON` |
| 填写人 | `submitted_by` | `JSON` |
| 父记录 | `parent_record_refs` | `JSON` |
| 202601 至 202608 | `balance_202601` 至 `balance_202608` | 各 `DECIMAL(20,4)` |

索引：`KEY idx_company_store (company_entity, store_name)`。

### 百亿补贴数据集：`finance_ecommerce_billion_subsidy`

#### `fin_billion_subsidy_pdd`

来源 Sheet：拼多多。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 日期 | `business_date` | `DATE` |
| 销售商品品名 | `product_name` | `VARCHAR(255)` |
| 百亿补贴比率 | `billion_subsidy_rate` | `DECIMAL(20,4)` |
| 费用返还比率 | `expense_rebate_rate` | `DECIMAL(20,4)` |
| 销售金额 | `sales_amount` | `DECIMAL(20,4)` |
| 销售店铺名称 | `store_name` | `VARCHAR(255)` |
| 申请开票日期 | `invoice_application_date` | `DATE` |
| 补贴到账日期 | `subsidy_received_date` | `DATE` |
| 补贴到账金额 | `subsidy_received_amount` | `DECIMAL(20,4)` |
| 填写人 | `submitted_by` | `JSON` |
| 财务确认人 | `finance_confirmer` | `JSON` |
| 开票金额 | `invoice_amount` | `DECIMAL(20,4)` |

索引：`KEY idx_store_business_date (store_name, business_date)`。

#### `fin_billion_subsidy_douyin`

来源 Sheet：抖音。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 日期 | `business_date` | `DATE` |
| 销售店铺名称 | `store_name` | `VARCHAR(255)` |
| 百亿补贴比率 | `billion_subsidy_rate` | `DECIMAL(20,4)` |
| 费用返还比率 | `expense_rebate_rate` | `DECIMAL(20,4)` |
| 销售金额 | `sales_amount` | `DECIMAL(20,4)` |
| 财务确认人 | `finance_confirmer` | `JSON` |
| 补贴到账金额 | `subsidy_received_amount` | `DECIMAL(20,4)` |
| 补贴到账日期 | `subsidy_received_date` | `DATE` |
| 申请开票日期 | `invoice_application_date` | `DATE` |
| 填写人 | `submitted_by` | `JSON` |
| 销售商品品名 | `product_name` | `VARCHAR(255)` |

索引：`KEY idx_store_business_date (store_name, business_date)`。

### `fin_daily_funds`

来源数据集：`finance_daily_funds`；Sheet：每日资金明细。该 Sheet 有 160 个字段：5 个固定字段和 31 天 × 5 类金额字段。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 公司 | `company_entity` | `VARCHAR(255)` |
| 银行 | `bank_name` | `VARCHAR(255)` |
| 期初余额 | `opening_balance` | `DECIMAL(20,4)` |
| 填表人 | `submitted_by` | `JSON` |
| 父记录 | `parent_record_refs` | `JSON` |

每日字段逐一按以下规则对应；`NN` 为 `01` 至 `31`，因此五条规则完整覆盖 155 个钉钉金额字段。

| 钉钉字段模式 | DB 列模式 | MySQL 类型 |
|---|---|---|
| `N日（货款/服务收入）` | `day_NN_goods_service_income` | `DECIMAL(20,4)` |
| `N日（货款/服务支出）` | `day_NN_goods_service_expense` | `DECIMAL(20,4)` |
| `N日（工薪社保支出）` | `day_NN_payroll_social_insurance_expense` | `DECIMAL(20,4)` |
| `N日（其他收入）` | `day_NN_other_income` | `DECIMAL(20,4)` |
| `N日其他支出` | `day_NN_other_expense` | `DECIMAL(20,4)` |

索引：`KEY idx_company_bank (company_entity, bank_name)`。

### `fin_offline_receivables_aging`

来源数据集：`finance_offline_receivables_aging`。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 往来单位 | `counterparty_name` | `VARCHAR(255)` |
| 账龄（31-60天） | `aging_31_60_days_amount` | `DECIMAL(20,4)` |
| 类别 | `receivable_category` | `VARCHAR(255)` |
| 账龄（0-30天） | `aging_0_30_days_amount` | `DECIMAL(20,4)` |
| 公司主体 | `company_entity` | `VARCHAR(255)` |
| 说明 | `description_note` | `TEXT` |
| 更新日期 | `updated_date` | `DATE` |
| 应收账款期末余额 | `accounts_receivable_ending_balance` | `DECIMAL(20,4)` |
| 逾期金额 | `overdue_amount` | `DECIMAL(20,4)` |
| 编制人 | `prepared_by` | `JSON` |
| 复核人 | `reviewer` | `JSON` |
| 复核时间 | `reviewed_at` | `DATE` |
| 业务部门确认 | `business_department_confirmation` | `VARCHAR(255)` |
| 确认人 | `confirmer` | `JSON` |
| 负责人 | `owner` | `JSON` |

索引：`KEY idx_company_counterparty (company_entity, counterparty_name)`，`KEY idx_updated_date (updated_date)`。

### `fin_offline_deposit_other_receivables`

来源数据集：`finance_offline_deposit_other_receivables`。

| 钉钉字段 | DB 列 | MySQL 类型 |
|---|---|---|
| 保证金缴纳时间 | `deposit_paid_at_raw` | `VARCHAR(255)` |
| 是否正常合作 | `cooperation_status` | `VARCHAR(255)` |
| 填写人 | `submitted_by` | `JSON` |
| 公司主体 | `company_entity` | `VARCHAR(255)` |
| 保证金余额 | `deposit_balance` | `DECIMAL(20,4)` |
| 往来单位（供应商） | `supplier_name` | `VARCHAR(255)` |
| 项目 | `project_name` | `VARCHAR(255)` |
| 更新时间 | `updated_at` | `DATE` |
| 复核人 | `reviewer` | `JSON` |
| 说明 | `description_note` | `TEXT` |

索引：`KEY idx_company_supplier (company_entity, supplier_name)`，`KEY idx_updated_at (updated_at)`。

## 未做的变换

- 不将 `user` 转为姓名或员工 ID：其钉钉原始对象保存在 JSON 中。
- 不将 `parent_record_refs` 变为外键：关联目标可能尚未同步或属于不同 Sheet。
- 不把 `statement_date_raw`、`deposit_paid_at_raw` 转为 `DATE`：对应钉钉字段类型是 `text`。
- 不为原始镜像表建立业务唯一键：业务去重规则应在来源表或后续指标层明确后再定义。
- 不在 `raw_dingtalk` 建立月度、每日金额的展开表：该工作属于 `mart_finance`。
