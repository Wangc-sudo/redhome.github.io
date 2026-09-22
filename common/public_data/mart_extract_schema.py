"""提取层 schema：``raw_dingtalk`` -> ``mart_ops``（spec §8 阶段 3）。

本模块只承担两件事，不多不少：

* 提取层维护的每张 mart 表的 DDL；
* 每个被提取数据集的「源列 -> 目标列」映射。

两类产出：

* **事实表** —— 一张 raw 表投影成稳定、业务可读的形状。钉钉专有的技术列
  被丢弃，已作废的 ``achievement_rate`` 源列**刻意不投影**（spec §10：
  达成率一律派生、绝不搬运）。
* **维度表** —— ``dim_calendar``（工作日历，由版本受控的规则推导，因此不会
  漂移）与 ``dim_robot_member``（区域 -> 成员）。

业务线容器只读这些表，绝不接触 ``raw_*``（spec §7）。
"""

from dataclasses import dataclass


FACT_DAILY_REPORT_OFFLINE = "fact_daily_report_offline"
FACT_CHANNEL_DAILY_SALES = "fact_channel_daily_sales"
FACT_FIN_RECEIVABLES_AGING = "fact_fin_receivables_aging"
FACT_FIN_PREPAYMENT_INVOICE = "fact_fin_prepayment_invoice"
FACT_FIN_OFFLINE_DEPOSIT = "fact_fin_offline_deposit"
FACT_FIN_PLATFORM_DEPOSIT = "fact_fin_platform_deposit"
FACT_FIN_STORE_FUNDS = "fact_fin_store_funds"
DIM_CALENDAR = "dim_calendar"
DIM_ROBOT_MEMBER = "dim_robot_member"


@dataclass(frozen=True)
class ExtractDataset:
    """一张 raw 表到一张 mart 表的投影。"""

    dataset: str
    source_table: str
    target_table: str
    columns: tuple[tuple[str, str], ...]
    kind: str = "fact"
    source: str = "dingtalk"

    @property
    def source_columns(self) -> tuple:
        return tuple(source for source, _ in self.columns)

    @property
    def target_columns(self) -> tuple:
        return tuple(target for _, target in self.columns)


# 显式白名单即契约：它同时说明「投影哪些列」和「刻意丢掉哪些列」——
#   * ``dingtalk_record_id`` 作为主键来源单独处理（源记录身份）；
#   * ``achievement_rate`` 作废（spec §10），不投影，留 raw 层作双跑对照；
#   * ``parent_record_refs`` 是钉钉表间链接，业务无用，不投影；
#   * 技术列 ``synced_at`` / ``sync_run_id`` 由提取层重新打点，不透传。
EXTRACT_DATASETS = (
    ExtractDataset(
        dataset="daily_report_offline",
        source_table="daily_report_offline",
        target_table=FACT_DAILY_REPORT_OFFLINE,
        columns=(
            ("region", "region"),
            ("responsible_person", "responsible_person"),
            ("department", "department"),
            ("business_date", "business_date"),
            ("sales_amount", "sales_amount"),
            ("daily_target", "daily_target"),
            ("monthly_target", "monthly_target"),
            ("note", "note"),
        ),
    ),
    ExtractDataset(
        dataset="channel_daily_sales",
        source_table="channel_daily_sales",
        target_table=FACT_CHANNEL_DAILY_SALES,
        columns=(
            ("channel", "channel"),
            ("store_name", "store_name"),
            ("business_date", "business_date"),
            ("sales_amount", "sales_amount"),
            ("promotion_cost", "promotion_cost"),
            ("roi", "roi"),
            ("responsible_person", "responsible_person"),
        ),
    ),
    ExtractDataset(
        dataset="fin_offline_receivables_aging",
        source_table="fin_offline_receivables_aging",
        target_table=FACT_FIN_RECEIVABLES_AGING,
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
        kind="snapshot",
    ),
    ExtractDataset(
        dataset="fin_ecommerce_prepayment_supplier_invoice",
        source_table="fin_ecommerce_prepayment_supplier_invoice",
        target_table=FACT_FIN_PREPAYMENT_INVOICE,
        columns=(
            ("supplier_name", "supplier_name"),
            ("company_entity", "company_entity"),
            ("prepayment_ledger_amount", "prepayment_ledger_amount"),
            ("accounts_payable_estimated_ledger_amount", "ap_estimated_amount"),
            ("ledger_reconciliation_status", "ledger_reconciliation_status"),
            ("uninvoiced_amount", "uninvoiced_amount"),
            ("statement_date_raw", "statement_date_raw"),
        ),
        kind="snapshot",
    ),
    ExtractDataset(
        dataset="fin_offline_deposit_other_receivables",
        source_table="fin_offline_deposit_other_receivables",
        target_table=FACT_FIN_OFFLINE_DEPOSIT,
        columns=(
            ("company_entity", "company_entity"),
            ("supplier_name", "supplier_name"),
            ("project_name", "project_name"),
            ("cooperation_status", "cooperation_status"),
            ("deposit_balance", "deposit_balance"),
            ("updated_at", "updated_at"),
        ),
        kind="snapshot",
    ),
    ExtractDataset(
        dataset="fin_ecommerce_platform_deposit",
        source_table="fin_ecommerce_platform_deposit",
        target_table=FACT_FIN_PLATFORM_DEPOSIT,
        columns=(
            ("company_entity", "company_entity"),
            ("platform", "platform"),
            ("store_name", "store_name"),
            ("project_name", "project_name"),
            ("store_operating_status", "store_operating_status"),
            ("review_status", "review_status"),
            ("deposit_balance", "deposit_balance"),
        ),
        kind="snapshot",
    ),
    ExtractDataset(
        dataset="fin_ecommerce_store_funds_balance",
        source_table="fin_ecommerce_store_funds_balance",
        target_table=FACT_FIN_STORE_FUNDS,
        columns=(),
        kind="melt_store_funds",
    ),
    # B1-阶段二 Task 8：raw_wdt.dim_product → mart dim_product 全量镜像。
    # 注册顺序即执行顺序：镜像必须先于订单行展开（品牌反查依赖本表）。
    ExtractDataset(
        dataset="wdt_dim_product_mirror",
        source_table="dim_product",
        target_table="dim_product",
        columns=(),
        kind="dim_mirror",
        source="wdt",
    ),
    # B1-阶段二 Task 9：raw_wdt.wdt_records 交易 payload 展开成订单行。
    ExtractDataset(
        dataset="wdt_order_line_fact",
        source_table="wdt_records",
        target_table="fact_order_line",
        columns=(),
        kind="order_line_expand",
        source="wdt",
    ),
    # 仓库运作（2026-09-21）：wdt_records 销售出库 / 退货入库 payload
    # 展开成行级事实，口径与 fact_order_line 对齐（行展开 + 渠道归一）。
    ExtractDataset(
        dataset="wdt_stockout_line_fact",
        source_table="wdt_records",
        target_table="fact_stockout_line",
        columns=(),
        kind="stockout_line_expand",
        source="wdt",
    ),
    ExtractDataset(
        dataset="wdt_refund_line_fact",
        source_table="wdt_records",
        target_table="fact_refund_line",
        columns=(),
        kind="refund_line_expand",
        source="wdt",
    ),
)


def dataset_by_name(name: str) -> ExtractDataset:
    for dataset in EXTRACT_DATASETS:
        if dataset.dataset == name:
            return dataset
    raise KeyError(f"unregistered extract dataset: {name}")


# 提取层给 ``sync_dataset_summary`` 带来的新取值。该列原本是
# ENUM('dingtalk','wdt')，扩展而非新表——控制面仍需一个「线状态」入口
# （spec §4）。
_ALTER_SYNC_DATASET_SUMMARY_SOURCE_ENUM = (
    "ALTER TABLE `sync_dataset_summary` "
    "MODIFY COLUMN `source_name` ENUM('dingtalk','wdt','extract') NOT NULL"
)

# 业务列一律跟随 raw 的「可空」语义：投影层不得因为源侧空值而写入失败。
_FACT_DAILY_REPORT_OFFLINE_DDL = (
    "CREATE TABLE IF NOT EXISTS `fact_daily_report_offline` (\n"
    "  `source_record_id` VARCHAR(255) NOT NULL,\n"
    "  `region` VARCHAR(50) DEFAULT NULL,\n"
    "  `responsible_person` VARCHAR(255) DEFAULT NULL,\n"
    "  `department` VARCHAR(255) DEFAULT NULL,\n"
    "  `business_date` DATE DEFAULT NULL,\n"
    "  `sales_amount` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `daily_target` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `monthly_target` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `note` TEXT,\n"
    "  `synced_at` DATETIME(6) NOT NULL,\n"
    "  `sync_run_id` CHAR(36) NOT NULL,\n"
    "  PRIMARY KEY (`source_record_id`),\n"
    "  KEY `idx_region_date` (`region`, `business_date`),\n"
    "  KEY `idx_person_date` (`responsible_person`, `business_date`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_FACT_CHANNEL_DAILY_SALES_DDL = (
    "CREATE TABLE IF NOT EXISTS `fact_channel_daily_sales` (\n"
    "  `source_record_id` VARCHAR(255) NOT NULL,\n"
    "  `channel` VARCHAR(50) DEFAULT NULL,\n"
    "  `store_name` VARCHAR(255) DEFAULT NULL,\n"
    "  `business_date` DATE DEFAULT NULL,\n"
    "  `sales_amount` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `promotion_cost` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `roi` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `responsible_person` JSON DEFAULT NULL,\n"
    "  `synced_at` DATETIME(6) NOT NULL,\n"
    "  `sync_run_id` CHAR(36) NOT NULL,\n"
    "  PRIMARY KEY (`source_record_id`),\n"
    "  KEY `idx_channel_date` (`channel`, `business_date`),\n"
    "  KEY `idx_date_store` (`business_date`, `store_name`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_FACT_FIN_RECEIVABLES_AGING_DDL = (
    "CREATE TABLE IF NOT EXISTS `fact_fin_receivables_aging` (\n"
    "  `record_id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n"
    "  `counterparty_name` VARCHAR(500) DEFAULT NULL,\n"
    "  `receivable_category` VARCHAR(255) DEFAULT NULL,\n"
    "  `company_entity` VARCHAR(255) DEFAULT NULL,\n"
    "  `ending_balance` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `overdue_amount` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `aging_0_30` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `aging_31_60` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `updated_date` DATE DEFAULT NULL,\n"
    "  `synced_at` DATETIME(6) NOT NULL,\n"
    "  `sync_run_id` CHAR(36) NOT NULL,\n"
    "  PRIMARY KEY (`record_id`),\n"
    "  KEY `idx_fin_aging_counterparty` (`counterparty_name`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
)

_FACT_FIN_PREPAYMENT_INVOICE_DDL = (
    "CREATE TABLE IF NOT EXISTS `fact_fin_prepayment_invoice` (\n"
    "  `record_id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n"
    "  `supplier_name` VARCHAR(500) DEFAULT NULL,\n"
    "  `company_entity` VARCHAR(255) DEFAULT NULL,\n"
    "  `prepayment_ledger_amount` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `ap_estimated_amount` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `ledger_reconciliation_status` VARCHAR(255) DEFAULT NULL,\n"
    "  `uninvoiced_amount` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `statement_date` DATE DEFAULT NULL,\n"
    "  `statement_date_raw` VARCHAR(255) DEFAULT NULL,\n"
    "  `synced_at` DATETIME(6) NOT NULL,\n"
    "  `sync_run_id` CHAR(36) NOT NULL,\n"
    "  PRIMARY KEY (`record_id`),\n"
    "  KEY `idx_fin_prepay_supplier` (`supplier_name`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
)

_FACT_FIN_OFFLINE_DEPOSIT_DDL = (
    "CREATE TABLE IF NOT EXISTS `fact_fin_offline_deposit` (\n"
    "  `record_id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n"
    "  `company_entity` VARCHAR(255) DEFAULT NULL,\n"
    "  `supplier_name` VARCHAR(500) DEFAULT NULL,\n"
    "  `project_name` VARCHAR(500) DEFAULT NULL,\n"
    "  `cooperation_status` VARCHAR(255) DEFAULT NULL,\n"
    "  `deposit_balance` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `updated_at` DATETIME(6) DEFAULT NULL,\n"
    "  `synced_at` DATETIME(6) NOT NULL,\n"
    "  `sync_run_id` CHAR(36) NOT NULL,\n"
    "  PRIMARY KEY (`record_id`),\n"
    "  KEY `idx_fin_offdep_entity` (`company_entity`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
)

_FACT_FIN_PLATFORM_DEPOSIT_DDL = (
    "CREATE TABLE IF NOT EXISTS `fact_fin_platform_deposit` (\n"
    "  `record_id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n"
    "  `company_entity` VARCHAR(255) DEFAULT NULL,\n"
    "  `platform` VARCHAR(255) DEFAULT NULL,\n"
    "  `store_name` VARCHAR(500) DEFAULT NULL,\n"
    "  `project_name` VARCHAR(500) DEFAULT NULL,\n"
    "  `store_operating_status` VARCHAR(255) DEFAULT NULL,\n"
    "  `review_status` VARCHAR(255) DEFAULT NULL,\n"
    "  `deposit_balance` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `synced_at` DATETIME(6) NOT NULL,\n"
    "  `sync_run_id` CHAR(36) NOT NULL,\n"
    "  PRIMARY KEY (`record_id`),\n"
    "  KEY `idx_fin_platdep_entity` (`company_entity`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
)

_FACT_FIN_STORE_FUNDS_DDL = (
    "CREATE TABLE IF NOT EXISTS `fact_fin_store_funds` (\n"
    "  `record_id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n"
    "  `store_name` VARCHAR(500) DEFAULT NULL,\n"
    "  `channel` VARCHAR(255) DEFAULT NULL,\n"
    "  `company_entity` VARCHAR(255) DEFAULT NULL,\n"
    "  `month` VARCHAR(7) DEFAULT NULL,\n"
    "  `balance` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `synced_at` DATETIME(6) NOT NULL,\n"
    "  `sync_run_id` CHAR(36) NOT NULL,\n"
    "  PRIMARY KEY (`record_id`),\n"
    "  KEY `idx_fin_funds_store_month` (`store_name`, `month`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
)

# 口径真源占位见 spec §10：``source`` 现为 ``local``（随规则推导的 restDays），
# 用友 T+ 接入后改为 ``yonyou_tplus``；``note`` 预留给「钉钉排班校验源」的
# 差异说明（校验源只告警、绝不覆盖口径）。
_DIM_CALENDAR_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_calendar` (\n"
    "  `business_date` DATE NOT NULL,\n"
    "  `is_workday` TINYINT(1) NOT NULL,\n"
    "  `source` VARCHAR(32) NOT NULL,\n"
    "  `note` VARCHAR(255) DEFAULT NULL,\n"
    "  `synced_at` DATETIME(6) NOT NULL,\n"
    "  `sync_run_id` CHAR(36) NOT NULL,\n"
    "  PRIMARY KEY (`business_date`),\n"
    "  KEY `idx_is_workday` (`is_workday`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

# 成员维度随提取层一并落地（同一迁移），使 robot 的「未填 = 成员 × 日期
# LEFT JOIN 事实表」在通讯录直连接通前即有稳定表可依赖；写入方在下一增量
# 交付（spec §10 组织成员：走 API 直连）。
_DIM_ROBOT_MEMBER_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_robot_member` (\n"
    "  `user_id` VARCHAR(64) NOT NULL,\n"
    "  `name` VARCHAR(128) NOT NULL,\n"
    "  `region` VARCHAR(32) NOT NULL,\n"
    "  `dept_id` VARCHAR(64) DEFAULT NULL,\n"
    "  `dept_name` VARCHAR(255) DEFAULT NULL,\n"
    "  `is_active` TINYINT(1) NOT NULL DEFAULT 1,\n"
    "  `synced_at` DATETIME(6) NOT NULL,\n"
    "  `sync_run_id` CHAR(36) NOT NULL,\n"
    "  PRIMARY KEY (`user_id`),\n"
    "  KEY `idx_region` (`region`),\n"
    "  KEY `idx_name` (`name`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


def legacy_ddl_statements() -> tuple:
    """返回提取层 DDL（按依赖顺序：先扩 ENUM，后建表）。"""
    return (
        _ALTER_SYNC_DATASET_SUMMARY_SOURCE_ENUM,
        _FACT_DAILY_REPORT_OFFLINE_DDL,
        _FACT_CHANNEL_DAILY_SALES_DDL,
        _DIM_CALENDAR_DDL,
        _DIM_ROBOT_MEMBER_DDL,
    )


def finance_ddl_statements() -> tuple:
    return (
        _FACT_FIN_RECEIVABLES_AGING_DDL,
        _FACT_FIN_PREPAYMENT_INVOICE_DDL,
        _FACT_FIN_OFFLINE_DEPOSIT_DDL,
        _FACT_FIN_PLATFORM_DEPOSIT_DDL,
        _FACT_FIN_STORE_FUNDS_DDL,
    )


# B1-阶段二 Task 8：商品目录镜像（raw_wdt.dim_product 的 1:1 投影）。
# 业务列与 raw 保持一致；``synced_at``/``sync_run_id`` 为 mart 侧写入
# 打点，源侧同步时刻保留在 ``raw_json``。
_DIM_PRODUCT_DDL = (
    "CREATE TABLE IF NOT EXISTS `dim_product` (\n"
    "  `spec_no` VARCHAR(100) NOT NULL,\n"
    "  `barcode` VARCHAR(100) DEFAULT NULL,\n"
    "  `goods_id` VARCHAR(50) DEFAULT NULL,\n"
    "  `goods_no` VARCHAR(100) DEFAULT NULL,\n"
    "  `goods_name` VARCHAR(500) DEFAULT NULL,\n"
    "  `spec_name` VARCHAR(500) DEFAULT NULL,\n"
    "  `brand_name` VARCHAR(200) DEFAULT NULL,\n"
    "  `series_name` VARCHAR(100) DEFAULT NULL,\n"
    "  `class_name` VARCHAR(200) DEFAULT NULL,\n"
    "  `retail_price` DECIMAL(12,2) DEFAULT NULL,\n"
    "  `wholesale_price` DECIMAL(12,2) DEFAULT NULL,\n"
    "  `is_deleted` TINYINT(1) NOT NULL DEFAULT 0,\n"
    "  `raw_json` JSON DEFAULT NULL,\n"
    "  `synced_at` DATETIME(6) NOT NULL,\n"
    "  `sync_run_id` CHAR(36) NOT NULL,\n"
    "  PRIMARY KEY (`spec_no`),\n"
    "  KEY `idx_dim_product_brand` (`brand_name`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

# B1-阶段二 Task 9：订单行事实。``brand_name`` 在投影时经 mart
# ``dim_product`` 反查物化（维度漂移不回流历史行），未匹配填 ``未匹配``；
# ``platform_subsidy``/``shop_subsidy`` 首批固定 0（开放点 §7.3）。
_FACT_ORDER_LINE_DDL = (
    "CREATE TABLE IF NOT EXISTS `fact_order_line` (\n"
    "  `trade_no` VARCHAR(64) NOT NULL,\n"
    "  `line_no` INT UNSIGNED NOT NULL,\n"
    "  `trade_time` DATETIME(6) DEFAULT NULL,\n"
    "  `trade_status` VARCHAR(20) DEFAULT NULL,\n"
    "  `spec_no` VARCHAR(100) DEFAULT NULL,\n"
    "  `goods_name` VARCHAR(500) DEFAULT NULL,\n"
    "  `brand_name` VARCHAR(200) DEFAULT NULL,\n"
    "  `quantity` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `paid_amount` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `platform_subsidy` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `shop_subsidy` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `raw_json` JSON DEFAULT NULL,\n"
    "  `synced_at` DATETIME(6) NOT NULL,\n"
    "  `sync_run_id` CHAR(36) NOT NULL,\n"
    "  PRIMARY KEY (`trade_no`, `line_no`),\n"
    "  KEY `idx_order_line_time` (`trade_time`),\n"
    "  KEY `idx_order_line_spec` (`spec_no`),\n"
    "  KEY `idx_order_line_brand` (`brand_name`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


def order_line_ddl_statements() -> tuple:
    return (
        _DIM_PRODUCT_DDL,
        _FACT_ORDER_LINE_DDL,
    )


# 商品动销（2026-09-15）：订单行补店铺/渠道维度，供「分渠道热销商品」下钻。
# 单独一个迁移版本而非改写 v1 的 CREATE：v1 已在存量库应用且校验和入表，
# 改动其文本会被 live_migrations 判定为漂移并拒绝启动。新库先建 v1 表、
# 再由本语句补列，存量库只跑 ALTER，两条路径落到同一份结构。
# shop_name 保留源侧店铺原名；channel_name 为归一化的渠道（抖音/拼多多/
# 京东…），未命中关键词的店铺归入「其他」。
_FACT_ORDER_LINE_CHANNEL_DDL = (
    "ALTER TABLE `fact_order_line`\n"
    "  ADD COLUMN `shop_name` VARCHAR(200) DEFAULT NULL,\n"
    "  ADD COLUMN `channel_name` VARCHAR(100) DEFAULT NULL,\n"
    "  ADD KEY `idx_order_line_channel` (`channel_name`)"
)


def order_line_channel_ddl_statements() -> tuple:
    """fact_order_line 渠道维度补列（v1 之后独立演进的增量迁移）。"""
    return (_FACT_ORDER_LINE_CHANNEL_DDL,)


# 仓库运作（2026-09-21）：销售出库单行事实。``order_no`` 为 WDT 出库单号；
# ``consign_time`` 取发货时间（采集窗口 status_type=0 同源）；品牌直接取
# 出库明细 payload 的 ``brand_name``（WDT 侧已带），不经 dim_product 反查。
_FACT_STOCKOUT_LINE_DDL = (
    "CREATE TABLE IF NOT EXISTS `fact_stockout_line` (\n"
    "  `order_no` VARCHAR(64) NOT NULL,\n"
    "  `line_no` INT UNSIGNED NOT NULL,\n"
    "  `consign_time` DATETIME(6) DEFAULT NULL,\n"
    "  `status` VARCHAR(20) DEFAULT NULL,\n"
    "  `warehouse_no` VARCHAR(50) DEFAULT NULL,\n"
    "  `warehouse_name` VARCHAR(200) DEFAULT NULL,\n"
    "  `shop_name` VARCHAR(200) DEFAULT NULL,\n"
    "  `channel_name` VARCHAR(100) DEFAULT NULL,\n"
    "  `trade_no` VARCHAR(64) DEFAULT NULL,\n"
    "  `logistics_no` VARCHAR(100) DEFAULT NULL,\n"
    "  `logistics_name` VARCHAR(100) DEFAULT NULL,\n"
    "  `spec_no` VARCHAR(100) DEFAULT NULL,\n"
    "  `goods_name` VARCHAR(500) DEFAULT NULL,\n"
    "  `brand_name` VARCHAR(200) DEFAULT NULL,\n"
    "  `quantity` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `sell_price` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `paid_amount` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `raw_json` JSON DEFAULT NULL,\n"
    "  `synced_at` DATETIME(6) NOT NULL,\n"
    "  `sync_run_id` CHAR(36) NOT NULL,\n"
    "  PRIMARY KEY (`order_no`, `line_no`),\n"
    "  KEY `idx_stockout_consign_time` (`consign_time`),\n"
    "  KEY `idx_stockout_spec` (`spec_no`),\n"
    "  KEY `idx_stockout_warehouse` (`warehouse_no`),\n"
    "  KEY `idx_stockout_channel` (`channel_name`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

# 退货入库单行事实。``order_no`` 为 WDT 入库单号，``refund_no`` 为退货单号；
# ``check_time`` 取审核（入库）时间；退货金额取明细级
# ``refund_amount`` / ``actual_refund_amount``。
_FACT_REFUND_LINE_DDL = (
    "CREATE TABLE IF NOT EXISTS `fact_refund_line` (\n"
    "  `order_no` VARCHAR(64) NOT NULL,\n"
    "  `line_no` INT UNSIGNED NOT NULL,\n"
    "  `refund_no` VARCHAR(64) DEFAULT NULL,\n"
    "  `check_time` DATETIME(6) DEFAULT NULL,\n"
    "  `process_status` VARCHAR(20) DEFAULT NULL,\n"
    "  `warehouse_no` VARCHAR(50) DEFAULT NULL,\n"
    "  `shop_name` VARCHAR(200) DEFAULT NULL,\n"
    "  `channel_name` VARCHAR(100) DEFAULT NULL,\n"
    "  `reason` VARCHAR(255) DEFAULT NULL,\n"
    "  `logistics_no` VARCHAR(100) DEFAULT NULL,\n"
    "  `spec_no` VARCHAR(100) DEFAULT NULL,\n"
    "  `goods_name` VARCHAR(500) DEFAULT NULL,\n"
    "  `brand_name` VARCHAR(200) DEFAULT NULL,\n"
    "  `quantity` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `stockin_quantity` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `refund_amount` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `actual_refund_amount` DECIMAL(20,4) DEFAULT NULL,\n"
    "  `raw_json` JSON DEFAULT NULL,\n"
    "  `synced_at` DATETIME(6) NOT NULL,\n"
    "  `sync_run_id` CHAR(36) NOT NULL,\n"
    "  PRIMARY KEY (`order_no`, `line_no`),\n"
    "  KEY `idx_refund_check_time` (`check_time`),\n"
    "  KEY `idx_refund_spec` (`spec_no`),\n"
    "  KEY `idx_refund_no` (`refund_no`)\n"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


def stock_flow_ddl_statements() -> tuple:
    """销售出库 / 退货入库行事实表（独立迁移版本 mart-extract-stock-flow-v1）。"""
    return (
        _FACT_STOCKOUT_LINE_DDL,
        _FACT_REFUND_LINE_DDL,
    )


def ddl_statements() -> tuple:
    return (
        legacy_ddl_statements()
        + finance_ddl_statements()
        + order_line_ddl_statements()
        + stock_flow_ddl_statements()
    )
