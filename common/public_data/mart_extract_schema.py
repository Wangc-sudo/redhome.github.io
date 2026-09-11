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
DIM_CALENDAR = "dim_calendar"
DIM_ROBOT_MEMBER = "dim_robot_member"


@dataclass(frozen=True)
class ExtractDataset:
    """一张 raw 表到一张 mart 表的投影。"""

    dataset: str
    source_table: str
    target_table: str
    columns: tuple

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


def ddl_statements() -> tuple:
    """返回提取层 DDL（按依赖顺序：先扩 ENUM，后建表）。"""
    return (
        _ALTER_SYNC_DATASET_SUMMARY_SOURCE_ENUM,
        _FACT_DAILY_REPORT_OFFLINE_DDL,
        _FACT_CHANNEL_DAILY_SALES_DDL,
        _DIM_CALENDAR_DDL,
        _DIM_ROBOT_MEMBER_DDL,
    )
