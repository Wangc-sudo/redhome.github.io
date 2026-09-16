# bi-web 阶段 B1：资金安全页 + 品牌维度（dim_product / 品牌树）设计

状态：待确认（评审稿；确认后走 writing-plans 出实施计划）

## 1. 背景与范围

阶段 A/B 已交付 4 页 16 卡（L1 驾驶舱 + l2-region/channel/people）。B1 覆盖《BI看板颗粒度需求表》两条需求与一块基础设施：

- **WP1 ⑩ 资金安全预警**（板块×往来对象×月）：应收款 / 预付款 / 供应商发票 / 店铺资金余额 / 保证金余额，超期项预警。五张 fin_* 表已由 sync-dingtalk 落 `raw_dingtalk`（2026-09-14 实盘：应收账龄 81 行、预付发票 52 行、保证金 55 行），**仅差 mart 投影与卡片**。
- **WP2 dim_product 灌库 + 品牌维度**：商品档案直采（`goods.Goods.queryWithSpec` 已探针验证 status=0，30 天窗口扫掠、限流重试均已实现）；⑫ 年度目标**品牌视图**（习酒 61631 万、古韵+大坛 10000 万）落地 L1。

**关键事实约束（设计前提）**：

1. **品牌级销售的唯一数据通路是 WDT 订单明细**——线下日报事实表（region/person/department/date/sales）与渠道日销表（channel/store/date/sales）均无 SKU/品牌粒度。品牌树必须新建 `fact_order_line`：raw_wdt.wdt_records（2804 行，`detail_list` JSON）按订单行展开 × dim_product 品牌映射。**业务已确认（2026-09-14）线下销售同样全量走 WDT**，该通路即全量口径。
2. **dim_product 落在 raw_wdt 库**（migration `wdt-dim-product-v1`），而 bi-web 合约只读 mart_ops——必须经 extract-mart 投影进 mart_ops，bi-web 不得跨库。
3. fin_daily_funds（31 天×5 收支列超宽表）**本批不投影**：需求⑩五项（应收/预付/发票/店铺资金/保证金）不含每日资金流水，melt 155 列的口径价值待业务确认后另批。
4. 百亿补贴两表（pdd/douyin）**本批不投影**：需求⑩未列，属电商经营页④的候选素材。

明确出界：库存③⑦、仓储⑧（B2）、C 类人工报表（④⑤⑥⑪⑬）、合同核销⑨（D 类）、线下渠道字典与业务单元清单（E 类）、L2 正式销售下钻链（待 B1+B2+E 齐备）。

### 1.1 数据源升级与降级策略（业务已确认）

- **优先级**：销售/商品/库存域以 WDT 旺店通为权威源；钉钉 AI 表格是过渡人工台账，随 WDT 接入范围扩大逐步退役。
- **降级规则**：同一指标双源可用时优先取 WDT；WDT 缺失（期间未覆盖、接口未接、历史未回填）自动降级 AI 表格，卡片脚注标识当前数据源（对拍透明）。
- **升级语义**：WDT 数据回填或新接口接入后，重投影即自动切换到 WDT 源，卡片零改动。
- **投影实现**：fact 表统一携带 `source_system` 列（`'wdt'` / `'dingtalk_aitable'`）；同期间双源并存时查询层只取 `wdt` 行。
- **B1 落点**：
  - 品牌维度（dim_product、fact_order_line、品牌树卡）：纯 WDT 源，无降级场景（AI 表格无品牌粒度）；
  - 资金安全页 fin_*：AI 表格单源（WDT 财务模块未接入），不挂降级；
  - 销售页现有卡（AI 表格源）的 WDT 升级与双源降级：B1 只落机制骨架（fact_order_line 带 `source_system` 列 + 查询层优先级取值），销售页口径迁移放 B2 订单投影规模化后，避免 B1 膨胀。

## 2. WP1：资金安全页

### 2.1 mart 投影（extract-mart 注册表追加 5 个数据集）

目标库 **mart_ops**（不新建 mart_finance 库；表名前缀 `fact_fin_*` 区分域）。沿用 `EXTRACT_DATASETS` 注册 + 白名单列投影 + upsert 幂等模式。

| raw 表 | mart 表 | 形态 | 主键 | 投影要点 |
|---|---|---|---|---|
| fin_offline_receivables_aging | `fact_fin_receivables_aging` | 状态表直投 | source_record_id | company_entity, counterparty_name, receivable_category, ending_balance, overdue_amount, aging_0_30, aging_31_60, updated_date |
| fin_ecommerce_prepayment_supplier_invoice | `fact_fin_prepayment_invoice` | 状态表直投 | source_record_id | company_entity, supplier_name, statement_date_raw→statement_date(解析), prepayment_ledger_amount, uninvoiced_amount, accounts_payable_estimated_ledger_amount, ledger_reconciliation_status |
| fin_offline_deposit_other_receivables | `fact_fin_offline_deposit` | 状态表直投 | source_record_id | company_entity, supplier_name, project_name, deposit_balance, cooperation_status, updated_at |
| fin_ecommerce_platform_deposit | `fact_fin_platform_deposit` | 状态表直投 | source_record_id | platform, store_name, project_name, deposit_balance, store_operating_status, review_status |
| fin_ecommerce_store_funds_balance | `fact_fin_store_funds` | **宽表 melt** | (store_name, month) | 8 个月列 `balance_202601..balance_202608` 展开为 (store_name, channel, company_entity, month DATE(月初), balance)；空值列不产出行 |

- statement_date_raw 为 VARCHAR，投影时按 `%Y-%m-%d`/`%Y/%m/%d` 两格式解析，失败置 NULL 并计数进 run 摘要（沿用 extract 审计语义）。
- 状态表语义 = 源表当前快照；raw 侧钉钉记录更新后重投影即覆盖，无历史拉链（首批不做 SCD，预警只看当前态）。

### 2.2 l2-finance 页面与卡片

| 卡片 | 图表 | 口径 |
|---|---|---|
| kpi_fin_overdue | scalar 扩展 | 应收超期：Σoverdue_amount + 笔数（overdue_amount>0）；副行「应收期末余额 X 万」 |
| table_fin_receivables | table | 往来对象×类别×期末余额×超期×账龄(0-30/31-60)，按 overdue 降序；overdue>0 行标红 |
| table_fin_prepayment | table | 供应商×预付台账金额×对账状态，按预付金额降序 |
| table_fin_uninvoiced | table | 供应商×未开票金额×对账日期×应付暂估，按未开票降序；超期标红（阈值开放点 §7） |
| table_fin_store_funds | table | 渠道×店铺×最新月余额（melt 后取 MAX(month)），按余额降序 |
| table_fin_deposit | table | 保证金合并视图（线下按供应商 / 平台按 platform×店铺）：项目×余额×合作/经营状态 |

- 筛选器：首批**不加**（数据量 50–80 行/表，一页可览；公司主体字段进表列）。
- L1 增补：**kpi_fin_alert**（scalar：超期应收笔数，副行金额）——首页「资金超期预警数」落地；点击跳 l2-finance（跨页导航复用 B 机制）。
- 导航：l2-finance `nav_order: 40`（接 l2-people 30 之后）。

### 2.3 错误处理与测试

沿用既有语义：单页 503 隔离、fail-open、参数白名单、错误无 SQL。新增测试：

- melt 投影单测：构造多月份/空值混合行，断言行数与 NULL 处理；
- statement_date 解析单测：两格式 + 非法值计数；
- 口径对拍：五表 mart 结果与钉钉 AI 表页面人工对拍（纳入评审 checklist）；
- 合约测试零改动（无 compose 变更）。

## 3. WP2：dim_product 灌库与品牌树

### 3.1 灌库执行（既有代码，零开发）

`python -m common.public_data.product_catalog` 全量扫掠（2015 起 30 天窗口、限流退避已实现）。灌库后产出**分类质量报告**：按 brand_name/series_name 分组计数，导出 `未分类` 清单（spec_no + goods_name）→ 业务确认后回填 SERIES_RULES/BRAND_KEYWORDS_FALLBACK（代码评审），重跑即收敛。历史遗留「476 个无品牌 SKU」以此清单为准闭环，不阻塞上线（未分类照常展示）。

### 3.2 dim_product 进 mart_ops

extract-mart 新增**维表镜像**投影类型（区别于 fact 白名单投影）：

- 源：raw_wdt.dim_product；目标：mart_ops.dim_product（同构 14 列，DDL 复用 `wdt-dim-product-v1` 定义）；
- 语义：`spec_no` PK upsert + 软删除标记同步（is_deleted），**不做全量替换**（数千 SKU 每次 DELETE+INSERT 无意义且闪断查询）；
- 注册进 `EXTRACT_DATASETS` 同表登记（新增 `kind: dim_mirror` 字段，默认 fact 兼容旧条目），run 摘要照常落 sync_dataset_summary。

### 3.3 fact_order_line（订单行事实表）

raw_wdt.wdt_records `detail_list` JSON 展开：

```
fact_order_line(trade_no, spec_no, goods_name, num, paid_amount, trade_time,
                brand_name, series_name,    -- 品牌/系列在投影时经 dim_product 映射物化
                source_system DEFAULT 'wdt')  -- 升级/降级策略载体，见 §1.1
PK (trade_no, spec_no, line_no)
```

- 增量幂等：按 PK upsert，重跑安全；
- 映射时机：**投影时** join 维度物化品牌列（卡片查询免 join，口径一处）；dim_product 更新（分类规则回填）后**全量重投影刷新品牌列**——重投影既有模式支持，跑批成本 2804 单量级可忽略；
- 无法映射 spec_no（订单行无编码只有品名）时回退 goods_name 关键词匹配（复用 product_catalog 同款规则函数，抽公共模块），仍未命中 → brand_name='未匹配' 并计数；
- 出界：退货/退款行冲销口径、订单状态过滤（哪些状态计入销售）——首批按 WDT 已付款订单计入，状态清单列开放点。

### 3.4 dim_target 品牌/系列扩展

种子 `target.seed.json` 追加两行（scope 设计复用现有 (scope, scope_key, year) PK）：

| scope | scope_key | annual_target | 说明 |
|---|---|---|---|
| brand | 习酒 | 616,310,000 | 公司核心品牌 |
| series_group | 古韵+大坛 | 100,000,000 | 需求表未拆分单系列，按组合落一行，不编造拆分 |

`replace_dim_target` 整体重放天然兼容；target_seed 校验器放行新 scope 枚举值（白名单加 brand/series_group）。

### 3.5 品牌树卡片（L1）

| 卡片 | 图表 | 口径 |
|---|---|---|
| kpi_brand_xijiu_progress | scalar+进度条 | 年累计 Σpaid_amount（brand='习酒'）÷ 61631 万；副行系列构成（君品/窖藏/古韵/大坛/其他 Top 占比） |
| kpi_series_group_progress | scalar+进度条 | 年累计 Σpaid_amount（brand='习酒' AND series IN ('古韵','大坛')）÷ 10000 万 |

- 年度累计口径与 kpi_annual_progress 同构（自然年，trade_time 年=2026）；
- 布局：L1 在现有年度目标卡（span 4）旁加两卡，行宽 12 栅格内重排（seed 变更，发布即生效）；
- 「未匹配/未分类」金额占比 >5% 时在卡内脚注提示（驱动业务回填分类规则）。

### 3.6 测试

- detail_list 展开单测：多行明细/空明细/缺 spec_no 回退/品牌未命中计数；
- 品牌列刷新单测：改 dim_product 分类 → 重投影 → fact_order_line 品牌列更新；
- 目标卡口径单测：分母取 MAX 不 SUM（沿用 dim_target 红线）；
- 对拍：品牌月累计与 WDT 后台「销售汇总-按品牌」人工对拍一个月。

## 4. 迭代工作流不变量

全程沿用「SQL 在代码、编排在 Nacos」：投影注册、卡片口径、分类规则全部代码评审；bi.seed.yaml 变更走 publish-bi；关停语义不变。compose 零改动、无新凭据、无新外呼（product_catalog 灌库为一次性人工执行，不进 compose 服务）。

## 5. 估算

| 工作包 | 内容 | 估算 |
|---|---|---|
| WP1 | 5 投影（含 1 melt + 日期解析）+ l2-finance 六卡 + L1 预警卡 + 测试对拍 | **5.5–7 人天** |
| WP2 | 灌库执行与分类报告 0.5 + dim 镜像投影 1 + fact_order_line 展开 2–3 + dim_target 扩展与品牌树卡 1–1.5 + 测试对拍 1 | **5.5–7 人天** |
| 合计 | | **11–14 人天** |

两 WP 无相互依赖，可并行；WP1 先交付（业务价值即时、零阻塞）。

## 6. 后续批次衔接

- B2（库存/效期/仓储）复用 fact_order_line 的订单投影机制与 dim_product（SKU 效期挂靠）；
- E 类业务单元清单固化后，l2-region 过渡链升级为正式「板块→业务单元→品牌→渠道→SKU」；
- ④ 电商月度经营页候选素材：fin_billion_subsidy_*、fin_ecommerce_promotion_recharge_balance（已在 raw，随 C 类月报通道一并定口径）。

## 7. 开放点（实施前需业务/评审确认）

1. **应收/未开票「超期」标红阈值**：overdue_amount>0 即红，还是按天数（30/60/90）分级？——影响 table 标红规则与 L1 预警计数口径。
2. ~~线下销售是否全量走 WDT~~ **已确认（2026-09-14）**：线下全量走 WDT，品牌树按全量口径设计；AI 表格定位为过渡源，升级/降级策略见 §1.1。
3. **订单状态计入范围**：WDT 哪些交易状态计入「销售」（已付款/已发货/已完成）；退款单是否冲销。
4. **古韵+大坛 10000 万不拆分**——按 series_group 组合行落地，后续业务给出拆分再改种子。
5. **未分类 SKU 清单**回填节奏：灌库后 1 周内业务确认，规则代码评审后重跑收敛。
6. fin_daily_funds / 百亿补贴两表：确认需求边界后另批投影。
