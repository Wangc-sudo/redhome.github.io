# 受限真实源公共数据同步验收设计

## 目标

实现一次仅可从本地 Docker Ubuntu 容器显式启动的同步验收：只读拉取受控的钉钉 AI 表与旺店通（WDT）数据，写入本地 MySQL 8.4 的 `raw_dingtalk_test`、`raw_wdt_test` 与 `mart_ops_test`，并证明字段映射、完整分页、幂等、本地事务、命名锁和三库隔离。

业务模块后续只读取 `mart_ops`；本设计中的 `mart_ops_test` 先承载同步验收指标，不向业务模块暴露 `raw_*` 库。

## 非目标与不可变约束

- 不向钉钉 AI 表、钉钉群机器人、WDT 或真实 RDS 发起写操作。
- 不连接或写入任何真实 RDS。实时读取模式只接受 Compose 网络中的 `PUBLIC_DATA_RDS_HOST=mysql` 和三个名称均以 `_test` 结尾的数据库。
- 不读取、输出、复制、暂存或提交 `config.json`、`wdt_credentials.json`、`common/dingtalk/test_groups.json`、`config-local/`、日志、状态文件或 HTML 生成物。
- 不自动运行实时读取；现有 `test-runner` 继续只运行测试，不能因 Compose 启动、CI 或 cron 而访问外部来源。
- 不将“当前客户端返回了若干页”认定为全量。达到配置页数上限时，如果来源仍表示有下一页，必须以失败结束。
- 不在原始层重新解释业务事实、拆宽表或建立跨表业务外键。钉钉财务表的物理映射继续以 [财务钉钉 AI 表与 RDS 原始表映射](2026-09-10-finance-raw-dingtalk-table-mapping.md) 为准。

## 同步范围的定义

一次运行只处理其 manifest 明确列出的数据集；未列出即不读取、不写入。完成的定义如下：

1. 每个配置钉钉 Base 的 Sheet 列表被读取；每个配置 Sheet 必须存在且名称、字段集合和字段类型匹配 manifest 与已有原始表映射。
2. 每个配置 Sheet 的记录分页读取至来源返回 `hasMore=false`。若 `hasMore=true` 却没有 `nextToken`，或超出 `max_pages`，运行失败。
3. 每个 WDT 数据集仅调用允许的方法，并在其明确的 UTC 时间窗内完成全部页。订单类接口必须切成不大于 50 分钟的连续、不重叠窗口；其他接口必须在 manifest 中声明其最大窗口。
4. WDT 每条记录必须由 manifest 定义的稳定来源主键提取规则产生非空字符串。没有稳定来源主键的接口不得加入实时同步。
5. 所有配置数据集成功写入原始库并完成验收指标投影后，运行才记为 `completed`。

因此“全量”是**受控清单内每张表、每个接口及其明确时间范围的无静默截断读取**，而不是无边界地枚举 WDT 的全部历史业务数据。

## 运行前置条件与显式启动

实时读取使用单独的 `live-sync` Compose profile 和一次性 `sync-runner` 服务。该服务复用 Ubuntu 测试镜像，加入现有未暴露端口的 MySQL 网络；它不在默认 `docker compose up`、测试入口或 CI 服务中出现。

启动命令需要同时具有：

- `APP_ENV=test`；
- `INTEGRATION_TEST_RUNNER=1`；
- `PUBLIC_DATA_RDS_HOST=mysql`；
- `raw_dingtalk_test`、`raw_wdt_test`、`mart_ops_test`；
- CLI 的 `--live-read` 与 `--confirm-local-test-write` 两个独立确认参数；
- 从仓库外部只读挂载的来源 manifest 与钉钉、WDT 凭据文件。

运行器在验证上述条件、解析 manifest、验证所有表映射和迁移状态之前，不构造任何外部客户端。敏感挂载不进入 Docker build context、镜像层、测试输出、同步审计或 Git；命令只输出 run ID、数据集名、数量、主键摘要和无业务明细的错误代码。

## 受限来源访问层

### 钉钉

同步器依赖只读 gateway，而不是整个 `DingTalkClient`：gateway 仅暴露 Sheet 列表、字段列表和严格记录分页三个操作。它不持有也不转发 `update_records`、群消息或 webhook 方法。

现有 `DingTalkClient.list_records()` 在达到 `max_pages` 时直接返回累积记录，无法区分正常结束与截断。同步实现必须新增逐页读取能力，保留 `hasMore` 与 `nextToken`，并由 gateway 在以下情况抛出异常：

- `hasMore=true` 但 `nextToken` 缺失；
- 已读取 `max_pages` 页仍有下一页；
- 记录 ID 为空或重复。

钉钉的每条写入记录使用来源 `record.id` 作为 `dingtalk_record_id`，而不是业务字段组合。

### WDT

同步器只允许 manifest 枚举的下列现有查询方法：

| 数据集类型 | 唯一允许的 WDT 方法 |
|---|---|
| 订单明细 | `sales.TradeQuery.queryWithDetail` |
| 入库采购明细 | `wms.stockin.Purchase.queryWithDetail` |
| 库存快照 | `wms.StockSpec.search2` |

WDT 协议以 HTTP POST 执行上述查询；这是该来源的只读查询约定，不代表允许写操作。同步器不得暴露任意 `method` 参数，也不得调用 WDT 的创建、更新、删除或状态变更接口。

现有 `WdtClient.call_paged()` 同样会在到达 `max_pages` 时静默停止。实时路径必须使用逐页严格分页：当最后允许页仍返回完整页时抛出 `PaginationLimitExceeded`；响应没有可识别行列表或主键提取失败时抛出失败，不将其视为空页。

## Manifest 契约

`PUBLIC_DATA_CONFIG` 指向仓库外部的 JSON 文件。它不包含凭据，版本固定为 `1`，并且只接受以下结构：

```json
{
  "version": 1,
  "dingtalk": {
    "bases": [
      {
        "base_id": "base-id",
        "sheets": [
          {
            "sheet_id": "sheet-id",
            "sheet_name": "店铺扣点费用管理",
            "dataset": "finance_store_commission",
            "target_table": "fin_store_commission",
            "max_pages": 200,
            "field_mapping": {
              "公司主体": {"column": "company_entity", "source_type": "text"},
              "费用项目": {"column": "fee_item", "source_type": "text"}
            }
          }
        ]
      }
    ]
  },
  "wdt": {
    "datasets": [
      {
        "dataset": "wdt_trade_detail",
        "method": "sales.TradeQuery.queryWithDetail",
        "target_table": "wdt_records",
        "record_id_path": "trade_no",
        "page_size": 100,
        "max_pages": 1000,
        "window_start": "2026-09-01T00:00:00Z",
        "window_end": "2026-09-02T00:00:00Z",
        "max_window_minutes": 50,
        "params": {"time_type": "2"}
      }
    ]
  }
}
```

`field_mapping` 必须与目标表的业务列、已有财务映射和实时发现的字段类型一一对应；缺少、额外或类型不匹配的字段均为 schema drift。`target_table` 只能是迁移已创建的表，不能由配置拼接 SQL 标识符。WDT 的 `method`、`target_table`、`record_id_path`、页数、时间窗和参数白名单同样在解析时校验。

DingTalk Base 或 Sheet 的配置范围即为本次允许读取的 AI 表范围。未映射 Sheet 即使可被列举，也不读取其记录；但必须报告为清单外，避免误把配置遗漏当成全量完成。

## 本地表结构

### `raw_dingtalk_test`

财务 Sheet 的业务表按现有映射文档创建，仍具有下列技术列与约束：

```sql
dingtalk_record_id VARCHAR(255) NOT NULL,
synced_at DATETIME(6) NOT NULL,
sync_run_id CHAR(36) NOT NULL,
PRIMARY KEY (dingtalk_record_id),
KEY idx_synced_at (synced_at)
```

迁移为每张已映射 Sheet 生成完整业务列和该文档声明的索引。同步前的字段发现结果写入仅含元数据的表，供漂移审计：

```sql
CREATE TABLE dingtalk_schema_snapshots (
  base_id VARCHAR(255) NOT NULL,
  sheet_id VARCHAR(255) NOT NULL,
  fields_sha256 CHAR(64) NOT NULL,
  fields_json JSON NOT NULL,
  observed_at DATETIME(6) NOT NULL,
  PRIMARY KEY (base_id, sheet_id, fields_sha256),
  KEY idx_observed_at (observed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

该表记录表头契约，不保存业务记录历史，也不改变原始表的物理字段映射。

### `raw_wdt_test`

首轮 WDT 验收保留每个允许查询返回的原始 JSON；业务字段拆分留待经确认的 WDT 物理映射。表结构如下：

```sql
CREATE TABLE wdt_records (
  source_method VARCHAR(255) NOT NULL,
  source_record_id VARCHAR(255) NOT NULL,
  source_window_start DATETIME(6) NOT NULL,
  source_window_end DATETIME(6) NOT NULL,
  payload_json JSON NOT NULL,
  synced_at DATETIME(6) NOT NULL,
  sync_run_id CHAR(36) NOT NULL,
  PRIMARY KEY (source_method, source_record_id),
  KEY idx_sync_run_id (sync_run_id),
  KEY idx_source_window (source_method, source_window_start, source_window_end),
  KEY idx_synced_at (synced_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`source_record_id` 来自 manifest 的预注册提取规则；不以 JSON 全文哈希充当主键，因此同一业务记录的后续更新不会产生重复行。

### `mart_ops_test`

验收层只存放不含业务明细的同步投影：

```sql
CREATE TABLE sync_runs (
  sync_run_id CHAR(36) NOT NULL,
  status ENUM('started', 'raw_committed', 'projection_pending', 'completed', 'failed') NOT NULL,
  manifest_sha256 CHAR(64) NOT NULL,
  started_at DATETIME(6) NOT NULL,
  finished_at DATETIME(6) NULL,
  failure_code VARCHAR(100) NULL,
  PRIMARY KEY (sync_run_id),
  KEY idx_status_started_at (status, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE sync_dataset_summary (
  sync_run_id CHAR(36) NOT NULL,
  source_name ENUM('dingtalk', 'wdt') NOT NULL,
  dataset_name VARCHAR(255) NOT NULL,
  records_read BIGINT UNSIGNED NOT NULL,
  raw_records_written BIGINT UNSIGNED NOT NULL,
  record_id_digest CHAR(64) NOT NULL,
  completed_at DATETIME(6) NOT NULL,
  PRIMARY KEY (sync_run_id, source_name, dataset_name),
  KEY idx_dataset_completed_at (source_name, dataset_name, completed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`record_id_digest` 是排序后来源主键列表的 SHA-256，只用于比对两次运行的集合一致性，不包含记录内容。业务读取接口可读取完成状态和数据集覆盖情况，但不能通过该投影读取原始业务 JSON。

## 事务、锁与幂等

每个来源数据集使用来源库中的命名锁 `public-data:<source>:<dataset>`。锁成功必须遵循既有 `named_lock()` 的精确 `int(1)` 语义。

运行顺序为：

1. 生成 UUID run ID，在 `mart_ops_test.sync_runs` 写入 `started`。
2. 验证 manifest、迁移和来源 schema；验证失败时不开始任何原始数据事务。
3. 获取数据集命名锁，完整读取并验证该数据集的页、记录 ID 与字段映射；外部读取期间不打开本地写事务。
4. 在单个来源库事务中写入该数据集：钉钉按 `dingtalk_record_id` upsert，WDT 按 `(source_method, source_record_id)` upsert。转换或写入失败时回滚该数据集的全部本地写入。
5. 原始事务提交后，写入 `sync_dataset_summary`；所有数据集投影成功才将 run 标为 `completed`。

三个数据库之间不假设分布式事务。若原始库已提交而 mart 投影失败，run 标为 `projection_pending`，后续仅从本地 raw 数据重建投影，不重新读取外部来源。外部读取、schema drift、分页异常或原始写入失败均将 run 标为 `failed`，只记录失败代码与安全摘要。

第二次运行同一 manifest 时，主键数量与 `record_id_digest` 必须保持一致；允许 `synced_at` 和 `sync_run_id` 更新。该性质证明重复执行不产生重复原始记录。

## 验收测试

### 自动测试

1. manifest 解析拒绝生产环境、非 `_test` 库、非 `mysql` 主机、缺失双确认、未知表、未允许 WDT 方法、无来源主键、重叠或超限时间窗与 schema drift。
2. 只读 gateway 不提供钉钉写表或消息方法；带跟踪的测试传输验证钉钉请求只有读取端点和 GET，WDT 只使用 allowlist 中的查询方法。
3. 两个严格分页器覆盖正常结束、缺失 continuation token、恰好达到页上限、重复来源 ID 和未知 WDT 行列表；所有不完整情况都必须失败。
4. 字段转换测试以已有财务映射为准，覆盖 `VARCHAR`、`TEXT`、`DECIMAL(20,4)`、`DATE` 与 JSON；不改变 `*_raw` 字段语义。
5. Docker MySQL 集成测试验证迁移创建三库表、事务回滚、命名锁竞争、同主键两次 upsert 不增行、WDT 与钉钉不会写入对方 raw 库、mart 表不保存 WDT JSON 或钉钉业务列。
6. 宿主 Compose 契约测试验证 `live-sync` 是显式 profile，MySQL 无宿主端口，敏感目录不进入 build context，默认测试入口不包含实时同步命令。

### 经授权的真实来源验收

操作者显式启动 `live-sync` 后，运行报告必须显示每个 manifest 数据集的读取数、原始写入数、主键摘要和完成状态，不打印业务字段、记录 JSON、token、签名、账号、密码或外部响应正文。

验收成功的必要证据为：

1. 所有配置钉钉 Sheet 的 schema 与映射匹配，且每个 Sheet 读取到来源声明的最后一页。
2. 所有配置 WDT 时间窗连续覆盖、全部分页完成，且仅调用 allowlist 方法。
3. 三个本地 `_test` 数据库都有预期对象；钉钉与 WDT 记录分别只在各自 raw 库，验收投影只在 `mart_ops_test`。
4. 对同一 manifest 再运行一次后，每个数据集的主键数和 `record_id_digest` 均与首次一致，且未产生额外主键。
5. `sync_runs` 的两次记录均为 `completed`；若有失败或 `projection_pending`，验收失败，不能以部分数据宣布完成。

## 实施边界

实现将新增受限来源 gateway、严格分页器、manifest 验证、迁移执行器、raw repository、同步编排器、验收投影 CLI、Docker `live-sync` profile 以及上述单元与集成测试。现有通用客户端保留给机器人使用；实时同步不会调用其静默截断的聚合分页方法。

首轮 WDT 仅建立通用 JSON 原始镜像与验收投影。将 WDT 业务字段变成专用 raw 表或业务 mart 指标，必须另行补充物理字段映射、唯一键和业务口径，不能由本轮同步器推断。
