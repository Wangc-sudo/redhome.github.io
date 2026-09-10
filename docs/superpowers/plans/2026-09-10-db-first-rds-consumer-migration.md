# DB-first RDS 数据同步、消费者改造与目录迁移实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 先将 DingTalk AI 表和旺店通数据同步到按来源隔离的 RDS 物理镜像表，再让日报、预警、播报与 BI 仅查询 RDS；所有业务读取切换并完成监听器写路径设计后，最后迁移至 `business → apps → common` 目录结构。

**Architecture:** `raw_dingtalk`、`raw_wdt` 与 `mart_ops` 是同一 RDS 实例中按责任隔离的三个数据库；测试环境使用对应的 `_test` 数据库。数据源连接器是唯一可读取 DingTalk AI 表或 WDT 的运行代码，业务消费者只通过数据集市查询接口获得数据，DingTalk 仅保留消息投递和后续经批准的专用写网关。每张 DingTalk AI Sheet 与每个 WDT 数据集都有明确、允许列表化的物理镜像表契约；镜像表保存当前源状态而非记录版本历史。

**Tech Stack:** Python 3.12、PyMySQL 1.1.1、MySQL 8.0、标准库 `unittest`/`unittest.mock`、现有 `common.dingtalk.DingTalkClient` 与 `common.wdt.WdtClient`、GitHub Actions。

---

## 范围、安全约束与执行顺序

- 本计划替代 `docs/superpowers/plans/2026-09-09-public-data-integration-foundation.md` 和 `docs/superpowers/plans/2026-09-10-business-apps-common-migration.md` 中**尚未执行**且会继续让业务脚本直读数据源的步骤；两份旧计划保留为历史记录，不改写。
- 只改主工作区 `E:\repos\digital-ops`；绝不修改 `.worktrees/public-data-integration-foundation`。
- 不读取、展示、暂存或提交 `config.json`、`wdt_credentials.json`、`common/dingtalk/test_groups.json`、`config-local/`、日志、状态或 HTML 生成物。
- 测试只使用 fake DB-API、mock 客户端和临时目录。除非用户单独授权，否则不连接任何 RDS、不读取真实 DingTalk/WDT 数据、不发送消息、不执行 DingTalk 写表、不注册 cron/Windows 计划任务/自启。
- `APP_ENV=test|production` 选择完整数据库组；`TEST_MODE=1` 只决定消息测试群，绝不能选择数据库。
- 普通业务读取不得调用 `list_sheets`、`list_fields`、`list_records`、`WdtClient.call` 或 `WdtClient.call_paged`；这些外部读取只允许存在于 `common/public_data.connectors/`。`update_records` 既不属于同步器也不属于普通业务读取；它仅可在 Task 9 经批准、单独测试的日报写模型中使用。
- 监听器的报数写入与 AI 表写回属于独立写路径，不能在本计划中机械改写或随目录移动上线。
- 所有 Git 暂存必须使用精确路径；不得使用 `git add .` 或 `git add -A`。仅在用户明确要求时创建本地提交，绝不推送。

### 最终依赖与数据流

```text
DingTalk AI 表 ──┐
                 ├─ common.public_data.connectors ── raw_dingtalk
WDT OpenAPI ─────┘                                      raw_wdt
                                                           │
                                              受控清洗/指标投影
                                                           │
                                                        mart_ops
                                                           │
                                business ──> apps ──> common.data_access
                                                           │
                                         DingTalk 仅用于消息投递
```

### 目标数据库组

| 环境 | DingTalk 原始镜像 | WDT 原始镜像 | 运营数据集市 |
|---|---|---|---|
| `test` | `raw_dingtalk_test` | `raw_wdt_test` | `mart_ops_test` |
| `production` | `raw_dingtalk` | `raw_wdt` | `mart_ops` |

三个数据库必须位于同一 RDS 实例，才能由受控投影任务执行跨库查询；它们不得共享表。应用启动拒绝测试环境使用非 `_test` 名称，也拒绝生产环境使用 `_test` 名称。

---

### Task 1: 修复当前公共客户端迁移的导入基线

**Files:**
- Modify: `common/daily_robot/core.py`
- Modify: `common/dingtalk/test_group.py`
- Modify: `tests/common/test_daily_robot.py`
- Test: `tests/common/test_dingtalk.py`
- Test: `tests/common/test_test_group.py`

- [ ] **Step 1: 记录当前受影响测试的失败原因。**

Run:

```bash
python -m unittest tests.common.test_daily_robot tests.common.test_dingtalk tests.common.test_test_group -v
```

Expected: `test_daily_robot` 因 `common.test_group` 已迁移而报 `ModuleNotFoundError`；不得将此既有半迁移状态归因于 RDS 代码。

- [ ] **Step 2: 保持公开 API，修正日报核心对测试群解析器的导入。**

在 `common/daily_robot/core.py` 将唯一的导入改为：

```python
from common.dingtalk import DingTalkClient, DingTalkError, send_markdown
from common.dingtalk.test_group import resolve_target
```

在 `common/dingtalk/test_group.py` 中，仅将面向用户的路径提示改为 `common/dingtalk/test_groups.json`。不得读取该文件，不得改变 `TEST_MODE=1` 的路由语义。

- [ ] **Step 3: 运行基线测试。**

Run:

```bash
python -m unittest discover -s tests -t . -v
```

Expected: `test_daily_robot`、`test_dingtalk` 与 `test_test_group` 通过；本步骤不移除任何直读 AI 表/WDT 的旧行为，也不执行其入口。若发现这三个模块以外的既有失败，记录完整失败信息并停止后续实施，先由用户确认其归属或单独制定修复计划。

---

### Task 2: 建立按来源隔离的数据库设置与安全连接边界

**Files:**
- Create: `requirements.txt`
- Create: `common/public_data/__init__.py`
- Create: `common/public_data/settings.py`
- Create: `common/public_data/db.py`
- Create: `common/public_data/config.example.json`
- Create: `tests/common/test_public_data_settings.py`
- Create: `tests/common/test_public_data_db.py`
- Modify: `.gitignore`
- Modify: `README.md`

- [ ] **Step 1: 写入会失败的环境隔离测试。**

`tests/common/test_public_data_settings.py` 必须覆盖下列环境，且通过临时 JSON 配置文件提供空的数据集清单：

```python
valid_test_environment = {
    "APP_ENV": "test",
    "PUBLIC_DATA_RDS_HOST": "127.0.0.1",
    "PUBLIC_DATA_RDS_PORT": "3306",
    "PUBLIC_DATA_RDS_USER": "public_data_test",
    "PUBLIC_DATA_RDS_PASSWORD": "not-a-real-secret",
    "PUBLIC_DATA_DINGTALK_DATABASE": "raw_dingtalk_test",
    "PUBLIC_DATA_WDT_DATABASE": "raw_wdt_test",
    "PUBLIC_DATA_MART_DATABASE": "mart_ops_test",
    "PUBLIC_DATA_CONFIG": "<temporary file>",
}
```

测试 `Settings.from_environment()`：接受上述环境；拒绝未知 `APP_ENV`；拒绝任何缺失值或非整数端口；拒绝 `test` 环境中任一数据库不以 `_test` 结尾；拒绝 `production` 环境中任一数据库以 `_test` 结尾；拒绝无法解析为 JSON object 的配置。断言异常文本不包含密码。

`tests/common/test_public_data_db.py` 用 fake `pymysql.connect` 验证 `connect()` 只传入 `DatabaseSettings` 的值、`charset="utf8mb4"`、`DictCursor` 与 `autocommit=False`；再验证 `transaction()` 在异常时回滚，`named_lock()` 无论函数体是否抛错都调用 `RELEASE_LOCK`。

- [ ] **Step 2: 运行测试以确认模块尚不存在。**

Run:

```bash
python -m unittest tests.common.test_public_data_settings tests.common.test_public_data_db -v
```

Expected: 因 `common.public_data` 不存在而失败。

- [ ] **Step 3: 实现不可变设置模型和连接辅助函数。**

创建根目录 `requirements.txt`，内容严格为：

```text
PyMySQL==1.1.1
```

在 `settings.py` 暴露以下类型：

```python
@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    user: str
    password: str
    name: str


@dataclass(frozen=True)
class Settings:
    app_env: str
    dingtalk_database: DatabaseSettings
    wdt_database: DatabaseSettings
    mart_database: DatabaseSettings
    source_config_path: Path
```

`Settings.from_environment(environ=None)` 只接受 `test` 和 `production`，读取上述 `PUBLIC_DATA_*` 环境变量，构造三个同 host/port/user/password、不同 name 的 `DatabaseSettings`。它在读取前检查全部必填值，在解析前检查端口范围 1–65535，并对三个数据库应用环境后缀规则。它读取 `PUBLIC_DATA_CONFIG` 但只接受 JSON object；绝不能把密码拼入错误消息。

在 `db.py` 暴露：

```python
def connect(database_settings):
    ...

@contextmanager
def transaction(connection):
    ...

@contextmanager
def named_lock(connection, lock_name, timeout_seconds=30):
    ...
```

`named_lock` 使用参数化 `SELECT GET_LOCK(%s, %s) AS acquired`，非 `1` 时抛 `LockUnavailable`，在 `finally` 中执行参数化 `SELECT RELEASE_LOCK(%s)`。

`config.example.json` 初始内容为：

```json
{
  "dingtalkAiTables": [],
  "wangdian": {"datasets": []},
  "dailyReportProjections": [],
  "channelProjections": []
}
```

在 `.gitignore` 增加精确规则：

```gitignore
config-local/public-data/
```

在 README 的运行说明之后增加公共数据层说明：安装 `requirements.txt`、显式设置 `APP_ENV`、必须使用隔离数据库，且 `TEST_MODE` 不能替代 `APP_ENV`。`PUBLIC_DATA_CONFIG` 必须指向从 `common/public_data/config.example.json` 复制到 `config-local/public-data/` 的本地契约文件；不得记录真实连接信息、Base/Sheet ID 或 WDT 凭据。

- [ ] **Step 4: 重新运行聚焦测试。**

Run:

```bash
python -m unittest tests.common.test_public_data_settings tests.common.test_public_data_db -v
```

Expected: PASS；不连接 RDS 或任何外部服务。

---

### Task 3: 用显式物理表契约取代通用 JSON 原始记录仓储

**Files:**
- Create: `common/public_data/contracts.py`
- Create: `common/public_data/schema.py`
- Create: `common/public_data/migrations.py`
- Create: `common/public_data/migrations/raw/001_metadata.sql`
- Create: `tests/common/test_public_data_contracts.py`
- Create: `tests/common/test_public_data_migrations.py`
- Modify: `common/public_data/config.example.json`

- [ ] **Step 1: 写失败的表契约与 DDL 测试。**

使用如下不含真实数据源标识的 mapping fixture：

```python
mapping = {
    "enabled": True,
    "dataset": "example_daily_report",
    "table": "example_daily_report",
    "baseId": "base-for-test-only",
    "sheetId": "sheet-for-test-only",
    "columns": [
        {"sourceField": "Owner", "sourceType": "text", "column": "owner_name", "type": "VARCHAR(128)", "nullable": False},
        {"sourceField": "Amount", "sourceType": "number", "column": "amount", "type": "DECIMAL(20,4)", "nullable": True},
    ],
}
```

`tests/common/test_public_data_contracts.py` 必须断言：

1. `SourceTableContract.from_mapping(mapping, "dingtalk")` 接受合法 mapping，并保存每个业务列非空的 `sourceType`；
2. 重复 `dataset`、重复 `column`、不匹配 `[a-z][a-z0-9_]*` 的表/列标识符、空 `sourceField`、空/缺失 `sourceType` 均在网络调用之前失败；
3. 仅允许 `VARCHAR(n)`、`TEXT`、`DECIMAL(20,4)`、`DATE`、`DATETIME(6)`、`JSON`、`BIGINT` 作为 MySQL 业务列类型；
4. DingTalk 连接器在写库前将 `list_fields()` 的 `{name, type}` 与启用契约的所有 `{sourceField, sourceType}` 逐项精确比对；缺少声明字段、出现未声明字段或任一类型不同都抛 `SourceContractError`；
5. DingTalk 契约的技术主键是 `dingtalk_record_id`，WDT 契约的技术主键是 `wdt_record_id`；
6. `create_table_sql(contract)` 包含允许列表业务列、`source_row_hash`、`synced_at`、`sync_run_id`、正确主键和 `ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`，但不把任何数据值插入 SQL；
7. `load_source_contracts()` 只接受一个 JSON object 中的 `dingtalkAiTables` 或 `wangdian.datasets`，跳过禁用项，并在连接、迁移或连接器构造前拒绝重复 dataset、重复 table、缺失 source 必填字段和未知键。

`tests/common/test_public_data_migrations.py` 使用 fake DB-API 验证：元数据迁移按文件名排序、应用过的版本校验 SHA-256、checksum 改变拒绝继续、环境哨兵不匹配拒绝继续、失败时释放命名锁。还要验证对映射表执行 `CREATE TABLE IF NOT EXISTS` 时只使用通过 `SourceTableContract` 验证后的标识符。

- [ ] **Step 2: 运行测试，确认契约层不存在。**

Run:

```bash
python -m unittest tests.common.test_public_data_contracts tests.common.test_public_data_migrations -v
```

Expected: FAIL，因为契约和迁移模块不存在。

- [ ] **Step 3: 创建元数据表与确定性的物理表生成器。**

`migrations.py` 在扫描任一迁移目录前，先用固定、无数据值的 DDL 建立 `pd_schema_migration`：

```sql
CREATE TABLE IF NOT EXISTS pd_schema_migration (
  version VARCHAR(64) NOT NULL,
  checksum CHAR(64) NOT NULL,
  applied_at DATETIME(6) NOT NULL,
  PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

随后按文件名排序、校验 SHA-256 并应用 `migrations/raw/`。`migrations/raw/001_metadata.sql` 仅在每个 raw 数据库中建立以下运行元数据表：

```sql
CREATE TABLE IF NOT EXISTS pd_environment (
  singleton TINYINT NOT NULL,
  app_env VARCHAR(16) NOT NULL,
  initialized_at DATETIME(6) NOT NULL,
  PRIMARY KEY (singleton),
  CONSTRAINT chk_pd_environment_singleton CHECK (singleton = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pd_sync_run (
  run_id CHAR(36) NOT NULL,
  app_env VARCHAR(16) NOT NULL,
  source_name VARCHAR(64) NOT NULL,
  dataset VARCHAR(128) NOT NULL,
  status VARCHAR(16) NOT NULL,
  started_at DATETIME(6) NOT NULL,
  finished_at DATETIME(6) NULL,
  read_count INT NOT NULL DEFAULT 0,
  upserted_count INT NOT NULL DEFAULT 0,
  error_class VARCHAR(128) NULL,
  PRIMARY KEY (run_id),
  KEY idx_pd_sync_run_lookup (app_env, source_name, dataset, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pd_source_schema (
  source_name VARCHAR(64) NOT NULL,
  dataset VARCHAR(128) NOT NULL,
  schema_sha256 CHAR(64) NOT NULL,
  schema_json JSON NOT NULL,
  observed_at DATETIME(6) NOT NULL,
  PRIMARY KEY (source_name, dataset)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS pd_sync_cursor (
  source_name VARCHAR(64) NOT NULL,
  dataset VARCHAR(128) NOT NULL,
  cursor_value VARCHAR(255) NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (source_name, dataset)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`SourceTableContract` 必须生成当前状态镜像表：

```sql
-- DingTalk 镜像表的固定技术列
`dingtalk_record_id` VARCHAR(255) NOT NULL,
`source_row_hash` CHAR(64) NOT NULL,
`synced_at` DATETIME(6) NOT NULL,
`sync_run_id` CHAR(36) NOT NULL,
PRIMARY KEY (`dingtalk_record_id`),
KEY `idx_synced_at` (`synced_at`),
KEY `idx_sync_run_id` (`sync_run_id`)
```

WDT 表使用 `wdt_record_id`，并额外包含可空 `source_updated_at DATETIME(6)` 和索引 `idx_source_updated_at`。业务列完全来自契约；不建立通用 `payload_json`、`first_seen_at` 或版本记录表。

`load_source_contracts(source_config_path, source_name)` 只从 `Settings.source_config_path` 读取本地 JSON object；它忽略 `enabled=false` 的对象，并在任何连接或网络调用前拒绝重复 `dataset`、重复物理 `table`、未知 source、缺少必填字段或不合法业务列。启用的 DingTalk 每列必须含非空 `sourceType`；启用的 WDT 数据集必须含可解析为带时区 ISO-8601 的 `initialCursor`，它是尚无 `pd_sync_cursor` 时唯一允许的首次同步起点。`dingtalk` 对应 `dingtalkAiTables`，`wdt` 对应 `wangdian.datasets`。

将 `config.example.json` 更新为下列禁用示例；真实文件只能从该示例复制到 `config-local/public-data/`：

```json
{
  "dingtalkAiTables": [
    {
      "enabled": false,
      "dataset": "example_daily_report",
      "table": "example_daily_report",
      "baseId": "",
      "sheetId": "",
      "columns": [
        {
          "sourceField": "",
          "sourceType": "",
          "column": "",
          "type": "VARCHAR(128)",
          "nullable": true
        }
      ]
    }
  ],
  "wangdian": {
    "datasets": [
      {
        "enabled": false,
        "dataset": "example_orders",
        "table": "example_orders",
        "method": "",
        "requests": [],
        "idField": "",
        "updatedAtField": "",
        "initialCursor": "",
        "windowMinutes": 60,
        "overlapMinutes": 10,
        "pageSize": 100,
        "columns": []
      }
    ]
  },
  "dailyReportProjections": [],
  "channelProjections": []
}
```

`migrate_raw_database(connection, app_env, contracts)` 只执行 `migrations/raw/` 的版本化元数据迁移，再检查/写入环境哨兵，最后建立传入 source 的允许物理镜像表。它不得执行 `CREATE DATABASE`，数据库由 RDS 管理员预先创建。

- [ ] **Step 4: 记录实际字段发现与映射的人工门槛。**

只有用户授权的 `APP_ENV=test` 只读发现命令才能调用 `list_fields()`。对每个 AI Sheet，先将发现得到的 `{name, type}` 与业务方确认过的映射（包括每列 `sourceType`）写入已忽略的 `config-local/public-data/sources.json`，再手工复制**不含 Base/Sheet ID**的表结构、列类型、主键和索引说明到 `docs/superpowers/specs/`。已有财务 Sheet 与 `raw_dingtalk` 映射文档保持为该证据来源。

WDT 映射也写入同一 `config-local/public-data/sources.json` 的 `wangdian.datasets`；每个数据集必须来自当前已验证脚本中的 method、参数、行 ID 和更新时间字段，并由业务确认一个带时区的 `initialCursor`，不能猜测字段名、接口或首次同步起点。

- [ ] **Step 5: 重新运行聚焦测试。**

Run:

```bash
python -m unittest tests.common.test_public_data_contracts tests.common.test_public_data_migrations -v
```

Expected: PASS；只使用 fake connection。

---

### Task 4: 实现 DingTalk AI 表到 `raw_dingtalk` 的只读当前状态镜像

**Files:**
- Create: `common/public_data/connectors/__init__.py`
- Create: `common/public_data/connectors/dingtalk_notable.py`
- Create: `common/public_data/raw_repository.py`
- Create: `common/public_data/sync_service.py`
- Create: `common/public_data/cli.py`
- Create: `tests/common/test_public_data_dingtalk.py`
- Create: `tests/common/test_public_data_cli.py`

- [ ] **Step 1: 编写失败的 DingTalk 全量镜像测试。**

使用 fake `DingTalkClient`，给出字段定义与两条记录：

```python
fields = [{"name": "Owner", "type": "text"}, {"name": "Amount", "type": "number"}]
records = [
    {"id": "rec-1", "fields": {"Owner": "Alice", "Amount": 1280}},
    {"id": "rec-2", "fields": {"Owner": "Bob", "Amount": None}},
]
```

测试必须验证：

1. `DingTalkSnapshotConnector.read()` 各调用一次 `list_fields` 和 `list_records`，从不读取或调用 `update_records`；
2. 空记录 ID、非 object record、契约未声明字段或发现字段类型漂移均在写库前抛 `SourceContractError`；
3. `RawMirrorRepository.replace_snapshot()` 对每一行使用参数化 upsert，将 `dingtalk_record_id`、业务列、hash、同步时间和 run ID 写入；
4. 只有在整张表读取、契约校验和所有 upsert 都成功后，才删除当前物理表中 `sync_run_id <> 本次 run` 的旧行；
5. 读取/校验/写入任一失败都不执行删除，`pd_sync_run` 仅记录异常类名，不记录 token、payload 或字段值；
6. `tests/common/test_public_data_cli.py` 使用 patch 的 `Settings.from_environment`、`connect`、契约加载器、`DingTalkClient` 和 `SyncService`，验证 `migrate --source dingtalk` 只调用 `migrate_raw_database()`，`sync --source dingtalk` 只构造 DingTalk 连接器；非法参数返回 2，且在调用 `Settings` 或 `connect` 前失败。

- [ ] **Step 2: 运行测试以确认连接器不存在。**

Run:

```bash
python -m unittest tests.common.test_public_data_dingtalk -v
```

Expected: FAIL，因为只读连接器与镜像仓储尚未实现。

- [ ] **Step 3: 实现只读连接器、当前状态替换与事务边界。**

`DingTalkSnapshotConnector(client, contract)` 只允许：

```python
fields = client.list_fields(contract.base_id, contract.sheet_id)
records = client.list_records(contract.base_id, contract.sheet_id)
```

它返回契约校验后的记录；记录 hash 使用业务列的稳定 JSON 序列化 `ensure_ascii=False, sort_keys=True, separators=(",", ":")` 的 SHA-256。连接器不得创建消息客户端、读取测试群配置或调用任何写 API。

`SyncService.sync_dingtalk_snapshot(contract)` 使用：

```python
lock_name = f"public-data:{self.app_env}:dingtalk:{contract.dataset}"
```

命名锁范围覆盖读取、校验和持久化。它先建立 `running` run，再读取源数据；成功路径使用单一 `transaction()` 执行 schema snapshot、所有镜像 upsert、过期行删除和 `success` run 更新。失败路径在独立事务中只写 `failed` 与 `type(error).__name__`，随后重新抛出异常。

CLI 暴露 `main(argv: list[str] | None = None) -> int`，并增加下列不带副作用的命令形状：

```bash
python -m common.public_data.cli migrate --source dingtalk
python -m common.public_data.cli sync --source dingtalk
```

解析成功后才加载 `Settings` 和本地 DingTalk 契约。`migrate` 只通过 `connect(Settings.dingtalk_database)` 打开连接并调用 `migrate_raw_database(connection, ...)` 根据本地契约建表；`sync` 才从 `DINGTALK_APP_KEY`、`DINGTALK_APP_SECRET` 与 `DINGTALK_OPERATOR_ID` 构造连接器并读取源。两者均不得发送消息或修改 AI 表；配置、连接器或数据库错误返回 1，参数错误返回 2，错误文本不得回显凭据、payload 或 SQL。

- [ ] **Step 4: 运行聚焦测试。**

Run:

```bash
python -m unittest tests.common.test_public_data_dingtalk tests.common.test_public_data_cli -v
```

Expected: PASS；没有真实 HTTP、AI 表写入或群消息。

---

### Task 5: 实现 WDT 到 `raw_wdt` 的受控增量镜像

**Files:**
- Create: `common/public_data/connectors/wdt.py`
- Create: `tests/common/test_public_data_wdt.py`
- Modify: `common/public_data/contracts.py`
- Modify: `common/public_data/raw_repository.py`
- Modify: `common/public_data/sync_service.py`
- Modify: `common/public_data/cli.py`
- Modify: `tests/common/test_public_data_cli.py`

- [ ] **Step 1: 编写失败的 WDT 契约与水位测试。**

使用如下测试 mapping：

```python
mapping = {
    "enabled": True,
    "dataset": "example_orders",
    "table": "example_orders",
    "method": "example.method",
    "requests": [{"start_time": "{window_start}", "end_time": "{window_end}"}],
    "idField": "trade_no",
    "updatedAtField": "modified_at",
    "initialCursor": "2026-09-09T10:00:00+00:00",
    "windowMinutes": 60,
    "overlapMinutes": 10,
    "pageSize": 100,
    "columns": [
        {"sourceField": "trade_no", "column": "trade_no", "type": "VARCHAR(128)", "nullable": False},
        {"sourceField": "modified_at", "column": "modified_at", "type": "DATETIME(6)", "nullable": False}
    ]
}
```

`tests/common/test_public_data_wdt.py` 必须断言：

1. 仅 `{window_start}` 与 `{window_end}` 可递归替换，其他花括号 token 在 API 调用前失败；
2. 输入已保存 cursor 为 `2026-09-09T10:00:00+00:00`、`overlapMinutes=10` 时，首窗从 `09:50:00+00:00` 开始；
3. 没有已保存 cursor 时，首窗只从契约的 `initialCursor - overlapMinutes` 开始；缺失、无法解析或不带时区的 `initialCursor` 在 API 调用前失败，绝不以当前时间或硬编码日期代替；
4. 每个窗口只调用既有 `WdtClient.call_paged(method, params, page_size=...)`，不复制签名逻辑；
5. 任意行缺 `idField`、更新时间不是 ISO-8601 或不匹配物理列契约时失败；
6. 成功时 cursor 等于本次成功行最大的 UTC 更新时间；失败时不更新 `pd_sync_cursor`；
7. records、cursor 与 `success` 状态在同一 transaction 内提交。

- [ ] **Step 2: 运行测试以确认连接器不存在。**

Run:

```bash
python -m unittest tests.common.test_public_data_wdt -v
```

Expected: FAIL，因为 WDT 增量连接器不存在。

- [ ] **Step 3: 实现窗口化抽取和原始镜像写入。**

`WdtIncrementalConnector(client, contract)` 先读取 `pd_sync_cursor`：存在时使用其 cursor；不存在时只使用已验证的 `contract.initial_cursor`。它按 `windowMinutes` 将从该起点减去 `overlapMinutes` 到当前 UTC 的区间切片；每个已配置 request 模板都执行一遍。它只接受当前脚本已验证的 `method` 和参数作为本地 mapping 输入，绝不在代码中硬编码 `sales.TradeQuery.queryWithDetail`、`wms.StockSpec.search2` 或 `wms.stockin.Purchase.queryWithDetail` 的未知字段契约。

对每个行 object：

```python
external_id = str(row[contract.id_field])
source_updated_at = parse_iso8601(row[contract.updated_at_field])
```

`RawMirrorRepository.upsert_incremental()` 参数化更新 `raw_wdt.<table>` 当前状态。它不删除旧行，因为增量 WDT API 没有已确认的删除语义；如未来获得删除标识或全量快照契约，另加测试后实现。

`SyncService.sync_wdt_incremental(contract)` 以 `public-data:{app_env}:wdt:{dataset}` 加锁，在一个事务中执行 upsert、cursor 保存和 `success`。它必须只保留错误类型名。

CLI 增加：

```bash
python -m common.public_data.cli migrate --source wdt
python -m common.public_data.cli sync --source wdt
```

扩展 `tests/common/test_public_data_cli.py`：`migrate --source wdt` 只连接 `Settings.wdt_database` 并调用 `migrate_raw_database()`；`sync --source wdt` 只构造 `WdtIncrementalConnector`，绝不构造 DingTalk 连接器或读取 DingTalk 契约。两条命令沿用 Task 4 的安全退出码与敏感信息限制。

- [ ] **Step 4: 写入当前渠道 WDT 数据集的人工配置清单。**

在不读取凭据的前提下，由开发人员从这些已跟踪脚本的调用参数和业务代码提炼契约，保存到已忽略本地 mapping：

```text
数字化/钉钉/渠道日报机器人/order_risk_alert.py
数字化/钉钉/渠道日报机器人/stock_alert.py
数字化/钉钉/渠道日报机器人/purchase_alert.py
数字化/钉钉/渠道日报机器人/hot_items_monitor.py
```

每个数据集在测试环境读验证后，才补充可提交的字段/主键/索引设计文档。不得读取或复制 `wdt_credentials.json`，不得调用生产数据源。

- [ ] **Step 5: 运行聚焦测试。**

Run:

```bash
python -m unittest tests.common.test_public_data_wdt tests.common.test_public_data_cli -v
```

Expected: PASS；所有 WDT 调用由 mock 承担。

---

### Task 6: 建立 `mart_ops` 投影与只读查询接口

**Files:**
- Create: `common/public_data/projections.py`
- Create: `common/public_data/query.py`
- Create: `common/public_data/migrations/mart/001_mart_ops.sql`
- Create: `tests/common/test_public_data_projections.py`
- Create: `tests/common/test_public_data_query.py`
- Modify: `common/public_data/migrations.py`
- Modify: `common/public_data/cli.py`
- Modify: `tests/common/test_public_data_cli.py`
- Modify: `tests/common/test_public_data_migrations.py`

- [ ] **Step 1: 写失败的投影和查询接口测试。**

`tests/common/test_public_data_projections.py` 使用 fake raw rows 测试五个当前消费者所需的最小读模型：

```text
mart_channel_order         # 订单号、付款时间、店铺、收货地区、状态
mart_channel_stock         # SKU、仓库、可发库存、7日销量、采购在途、采集时间
mart_channel_daily_metric  # 日期、渠道、销售额、推广费、去重店铺数、投影时间
mart_channel_month_target  # 月份、渠道、月销售目标、投影时间
mart_daily_report_value    # 区域、报表月、责任人、项目部、日期、数值、目标、采集时间
```

测试验证：订单、库存、渠道日报和日报宽表的字段映射仅来自已验证 projection contract；渠道日报投影按 `(report_day, channel_code)` 聚合销售额、推广费并计算 `COUNT(DISTINCT shop_name)`；月目标按 `(report_month, channel_code)` 聚合；日报宽表被展开为每人每天一行；缺少身份/期间/数值/渠道映射时抛 `ProjectionContractError`；每一次投影使用参数化 SQL；业务读取不访问 raw 表。

`tests/common/test_public_data_query.py` 为下列接口使用 fake cursor；两个 reader 都只接收一个已打开的 `mart_ops` DB-API connection，不读取环境变量、不自行连接数据库：

```python
class ChannelDataReader:
    def __init__(self, mart_connection): ...
    def orders_for_payment_day(self, day: date) -> list[dict]: ...
    def latest_stock_by_warehouse(self, warehouse_codes: list[str]) -> list[dict]: ...
    def purchases_between(self, start: datetime, end: datetime) -> list[dict]: ...
    def daily_metrics_for_days(self, days: list[date]) -> list[dict]: ...
    def month_targets(self, report_month: str) -> list[dict]: ...

class DailyReportReader:
    def __init__(self, mart_connection): ...
    def status_for_day(self, region: str, report_month: str, day: int) -> list[dict]: ...
    def values_for_month(self, region: str, report_month: str) -> list[dict]: ...
```

断言每条查询仅引用 `mart_ops` 表、使用绑定参数、没有 `DingTalkClient`/`WdtClient` 导入，且用例无法通过从 raw 表读取而成功。

扩展 `tests/common/test_public_data_migrations.py`：`migrate_mart_database()` 只扫描 `migrations/mart/`、建立并校验该库自己的迁移账本和环境哨兵，绝不执行 `pd_sync_run`、`pd_source_schema`、`pd_sync_cursor` 或 raw 镜像表的 DDL。

- [ ] **Step 2: 运行测试以确认投影层不存在。**

Run:

```bash
python -m unittest tests.common.test_public_data_projections tests.common.test_public_data_query -v
```

Expected: FAIL，因为投影和读接口不存在。

- [ ] **Step 3: 建立数据集市表和跨公司跨期间索引。**

`migrate_mart_database(connection, app_env)` 使用与 raw 迁移相同的固定迁移账本 bootstrap，但只扫描 `migrations/mart/`，并在所有 mart DDL 成功后检查/写入该数据库自己的环境哨兵。`migrations/mart/001_mart_ops.sql` 先建立该哨兵表：

```sql
CREATE TABLE IF NOT EXISTS pd_environment (
  singleton TINYINT NOT NULL,
  app_env VARCHAR(16) NOT NULL,
  initialized_at DATETIME(6) NOT NULL,
  PRIMARY KEY (singleton),
  CONSTRAINT chk_pd_environment_singleton CHECK (singleton = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

随后在 `mart_ops`/`mart_ops_test` 中建立下列已知稳定结构；所有数据源字段具体映射由本地 contract 驱动：

```sql
CREATE TABLE IF NOT EXISTS mart_channel_order (
  order_id VARCHAR(128) NOT NULL,
  paid_at DATETIME(6) NOT NULL,
  shop_name VARCHAR(255) NOT NULL,
  receiver_area VARCHAR(255) NULL,
  order_status VARCHAR(64) NOT NULL,
  source_updated_at DATETIME(6) NULL,
  projected_at DATETIME(6) NOT NULL,
  PRIMARY KEY (order_id),
  KEY idx_paid_status_shop_area (paid_at, order_status, shop_name, receiver_area),
  KEY idx_source_updated_at (source_updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS mart_channel_stock (
  snapshot_at DATETIME(6) NOT NULL,
  warehouse_code VARCHAR(64) NOT NULL,
  sku VARCHAR(128) NOT NULL,
  goods_name VARCHAR(255) NULL,
  available_stock DECIMAL(20,4) NOT NULL,
  stock_quantity DECIMAL(20,4) NOT NULL,
  sales_7d DECIMAL(20,4) NOT NULL,
  purchase_quantity DECIMAL(20,4) NOT NULL,
  PRIMARY KEY (snapshot_at, warehouse_code, sku),
  KEY idx_latest_sku_warehouse (sku, warehouse_code, snapshot_at),
  KEY idx_warehouse_snapshot (warehouse_code, snapshot_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS mart_channel_daily_metric (
  report_day DATE NOT NULL,
  channel_code VARCHAR(64) NOT NULL,
  sales_amount DECIMAL(20,4) NOT NULL,
  promotion_fee DECIMAL(20,4) NOT NULL,
  shop_count INT UNSIGNED NOT NULL,
  projected_at DATETIME(6) NOT NULL,
  PRIMARY KEY (report_day, channel_code),
  KEY idx_channel_day (channel_code, report_day)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS mart_channel_month_target (
  report_month CHAR(7) NOT NULL,
  channel_code VARCHAR(64) NOT NULL,
  monthly_sales_target DECIMAL(20,4) NOT NULL,
  projected_at DATETIME(6) NOT NULL,
  PRIMARY KEY (report_month, channel_code),
  KEY idx_channel_month (channel_code, report_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS mart_daily_report_value (
  region VARCHAR(64) NOT NULL,
  report_month CHAR(7) NOT NULL,
  responsible_name VARCHAR(128) NOT NULL,
  department_name VARCHAR(128) NULL,
  report_day DATE NOT NULL,
  amount DECIMAL(20,4) NULL,
  monthly_target DECIMAL(20,4) NULL,
  source_record_id VARCHAR(255) NOT NULL,
  projected_at DATETIME(6) NOT NULL,
  PRIMARY KEY (region, report_month, responsible_name, report_day),
  KEY idx_region_period_day_member (region, report_month, report_day, responsible_name),
  KEY idx_period_department_day (report_month, department_name, report_day)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

财务指标另在已存在的 `mart_finance` 设计范围实现，并预建形如 `(company_code, metric_period, metric_code)` 的复合索引；不要将跨公司/跨期间索引错误放进 `raw_dingtalk`。

- [ ] **Step 4: 实现投影和查询命令。**

`ProjectionService` 只从 `raw_dingtalk`、`raw_wdt` 的契约表读取，并把已验证字段投影到 `mart_ops`。`channel-daily-metrics` 从渠道明细原始表按 `(日期, 渠道)` 计算销售额、推广费和去重店铺数；`channel-month-targets` 从渠道目标原始表按 `(月份, 渠道)` 汇总目标。每个投影先检查本次源同步 run 为 `success`，再在 transaction 内 upsert 数据集市；空数据集必须完成空投影而非保留过期数据。

CLI 增加：

```bash
python -m common.public_data.cli migrate --source mart
python -m common.public_data.cli project --dataset channel-orders
python -m common.public_data.cli project --dataset channel-stock
python -m common.public_data.cli project --dataset channel-daily-metrics
python -m common.public_data.cli project --dataset channel-month-targets
python -m common.public_data.cli project --dataset daily-report
```

扩展 `tests/common/test_public_data_cli.py`：`migrate --source mart` 只连接 `Settings.mart_database` 并调用 `migrate_mart_database()`；每个 `project` 只打开其所需 raw 数据库和 `mart_database`，不构造 DingTalk/WDT 客户端。无效 dataset 在打开连接前返回 2。这些命令不读取外部来源、不发送消息、不写 AI 表；数据库连接只由 `Settings` 提供。

- [ ] **Step 5: 运行聚焦测试。**

Run:

```bash
python -m unittest tests.common.test_public_data_projections tests.common.test_public_data_query tests.common.test_public_data_migrations tests.common.test_public_data_cli -v
```

Expected: PASS；读模型隔离与 mart 迁移隔离得到测试证明。

---

### Task 7: 将渠道日报和 WDT 预警消费者切换为 `mart_ops` 只读

**Files:**
- Modify: `数字化/钉钉/渠道日报机器人/main.py`
- Modify: `数字化/钉钉/渠道日报机器人/run_today.py`
- Modify: `数字化/钉钉/渠道日报机器人/channel_report.py`
- Modify: `数字化/钉钉/渠道日报机器人/order_risk_alert.py`
- Modify: `数字化/钉钉/渠道日报机器人/stock_alert.py`
- Modify: `数字化/钉钉/渠道日报机器人/purchase_alert.py`
- Modify: `数字化/钉钉/渠道日报机器人/hot_items_monitor.py`
- Create: `tests/channel_daily/__init__.py`
- Create: `tests/channel_daily/test_database_consumers.py`

- [ ] **Step 1: 为每个业务口径写失败的数据库消费者测试。**

使用 fake `ChannelDataReader`，不 mock `WdtClient` 或 `DingTalkClient`：

1. 订单风险用 `orders_for_payment_day()` 返回的订单执行现有 `detect_groups()`，同店铺、地区、日期达到阈值时生成既有消息文本；
2. 库存提醒用 `latest_stock_by_warehouse(["01", "12"])` 的记录合并 SKU，并保持现有紧急/超卖阈值；
3. 采购提醒与热品监控只使用相应数据库读接口返回值；
4. `main.py`、`run_today.py` 和 `channel_report.py` 仅用 `daily_metrics_for_days()` 与 `month_targets()` 计算当日销售额、推广费、店铺数、环比、月累计、目标和达成率；
5. `main.py` 和 `run_today.py` 接收数据库读取的数据集，不再构造业务数据源 `DingTalkClient` 或自定义 AI 表 HTTP 客户端；
6. 每个 `--dry` 命令仍只打印，不调用 `send_markdown()`；正常模式的群发送 mock 保持现有测试群路由行为。

对每个改造模块增加 AST/文本静态断言：禁止导入 `WdtClient` 或遗留 `wdt_client`，禁止 `.call(`、`.call_paged(`、`.list_records(`、`.list_fields(`、`list_sheets(`，并禁止出现 `/notable/` 路径、AI 表读取辅助方法（如 `sheets`/`records`）或在模块内直接导入 HTTP 请求库。该检查覆盖 `run_today.py` 的现有自定义 `DT` 客户端，DingTalk 消息投递仍必须经过共享发送函数。

- [ ] **Step 2: 运行测试以记录当前直接源读取行为。**

Run:

```bash
python -m unittest tests.channel_daily.test_database_consumers -v
```

Expected: FAIL，因为当前脚本直连 WDT 或 DingTalk AI 表。

- [ ] **Step 3: 注入只读 reader，保留消息投递与业务算法。**

将每个脚本的外部数据加载替换为 `ChannelDataReader`。入口边界统一使用 `Settings.from_environment()`、`connect(settings.mart_database)` 和 `ChannelDataReader(connection)` 创建一次已关闭责任明确的 reader，再将其注入纯业务函数；reader 本身不得读取环境变量或创建连接。示例：

```python
def load_orders(reader, day):
    return reader.orders_for_payment_day(day)
```

`main.py`、`run_today.py` 和 `channel_report.py` 只处理由 reader 提供的 DB 行；DingTalk 客户端只允许保留在最终消息发送路径。`order_risk_alert.py`、`stock_alert.py`、`purchase_alert.py`、`hot_items_monitor.py` 删除 WDT 客户端创建和所有网络抽取循环，同时保留状态去重文件、`--dry` 与消息格式。

本阶段不移动目录、不改 scheduler、不删除 `wdt_client.py` 薄包装；待没有消费者引用后再删除。

- [ ] **Step 4: 运行聚焦与回归测试。**

Run:

```bash
python -m unittest tests.channel_daily.test_database_consumers tests.common.test_dingtalk tests.common.test_test_group -v
```

Expected: PASS；测试输出中不出现网络请求。

---

### Task 8: 将杭州、绍兴的只读日报能力切换为 `mart_daily_report_value`

**Files:**
- Modify: `common/daily_robot/core.py`
- Modify: `common/daily_robot/leaderboard.py`
- Modify: `数字化/钉钉/杭州日报机器人/hangzhou_reminder.py`
- Modify: `数字化/钉钉/杭州日报机器人/check_data.py`
- Modify: `数字化/钉钉/杭州日报机器人/leaderboard_report.py`
- Modify: `数字化/钉钉/shaoxing_daily_robot/shaoxing_reminder.py`
- Modify: `数字化/钉钉/shaoxing_daily_robot/check_data.py`
- Modify: `数字化/钉钉/shaoxing_daily_robot/leaderboard_report.py`
- Modify: `tests/common/test_daily_robot.py`
- Create: `tests/common/test_daily_report_reader.py`

- [ ] **Step 1: 写失败的日报数据库读取测试。**

`tests/common/test_daily_report_reader.py` 用 fake query rows 验证：`DailyReportReader.status_for_day()` 把 `report_month` 格式为 `YYYY-MM` 的数据库记录分为已填、未填和跳过的合计行；`values_for_month()` 返回榜单和体检所需的日值/目标；查询没有 DingTalk client 依赖。

更新 `tests/common/test_daily_robot.py`，令 `fetch_status()`、`do_remind()` 与 `do_check()` 接收 `DailyReportReader` 或等价注入依赖；现有测试继续 mock `fetch_status` 与 `send_group`。新增断言：不完整成员、空责任人、未来日期数据、目标/累计不符等体检逻辑基于 reader 记录得到与旧算法一致的结果。

- [ ] **Step 2: 运行测试以确认当前直接 AI 表读取行为。**

Run:

```bash
python -m unittest tests.common.test_daily_report_reader tests.common.test_daily_robot -v
```

Expected: FAIL，因为 `fetch_status`、`check_data` 与榜单采集仍直接构造 `DingTalkClient`。

- [ ] **Step 3: 改造只读逻辑，保留消息投递。**

将 `common/daily_robot/core.py` 中的签名改为：

```python
def fetch_status(reader, region, report_month, day):
    ...

def check_data(reader, region, report_month, now):
    ...

def do_remind(config, state, now, day, reader):
    ...

def do_check(config, state, now, day, reader):
    ...
```

将 `common/daily_robot/leaderboard.py` 的 `collect(reader, region, report_month, include_today=False)` 改为以 `DailyReportReader.values_for_month()` 的结果计算。杭州、绍兴的提醒、检查和榜单入口在命令边界执行 `settings = Settings.from_environment()`、`connection = connect(settings.mart_database)`、`reader = DailyReportReader(connection)`，以 `now.strftime("%Y-%m")` 传入期间，并在 `finally` 中关闭连接；核心函数只接收 reader。它们仅保留 `DingTalkClient` 的 `send_group`/`send_markdown` 消息投递调用。

`org_sync.py` 继续读取本地通讯录快照并更新成员映射，不创建 `DailyReportReader`，也不属于本任务的 AI 表读改造。禁止在上述只读命令中调用 `list_records`、`list_fields` 或 `update_records`。本阶段不修改 `listener.py`、`recalc_totals()`、`hangzhou_listener.py`、`shaoxing_listener.py`；这四个写路径由下一任务隔离。

- [ ] **Step 4: 运行聚焦与完整单元测试。**

Run:

```bash
python -m unittest tests.common.test_daily_report_reader tests.common.test_daily_robot -v
python -m unittest discover -s tests -t . -v
```

Expected: PASS；测试期间不连接外部数据源。

---

### Task 9: 为报数监听器和合计回写建立独立的写模型设计门槛

**Files:**
- Create: `docs/superpowers/specs/2026-09-10-daily-report-write-model-design.md`
- Create after approval in a separate plan: `docs/superpowers/plans/2026-09-10-daily-report-write-model-implementation.md`
- Deferred test: `tests/common/test_daily_report_write_model.py`

- [ ] **Step 1: 记录当前写路径边界，不改写功能。**

文档必须明确下列当前直写行为不属于普通业务读取：

```text
common/daily_robot/listener.py      # Stream 报数、读 AI 表、update_records
common/daily_robot/core.py          # recalc_totals() 调用 update_records
数字化/钉钉/杭州日报机器人/hangzhou_listener.py
数字化/钉钉/shaoxing_daily_robot/shaoxing_listener.py
```

- [ ] **Step 2: 在实施前取得并记录唯一写模型决策。**

设计文档必须在以下两种模型中选择一个，未获明确批准不得继续：

```text
A. RDS 为唯一写模型：Stream 输入在数据库事务中写入日报值；AI 表仅通过可审计 outbox 投影更新。
B. AI 表仍为写模型：Stream 只更新 AI 表；确认成功后触发只读同步，业务读取等待已完成的 RDS run。
```

无论选择哪种模型，文档都必须定义：身份匹配键、幂等命令 ID、事务边界、失败重试、AI 表写回是否存在、源/RDS 延迟时的用户反馈、回放规则以及禁止群消息重复发送的条件。

- [ ] **Step 3: 只在模型获批后创建独立实施计划，不在本计划实现。**

新的写模型实施计划必须先写 `tests/common/test_daily_report_write_model.py` 的失败测试，至少验证重复 Stream 事件只写一次、失败不会把未提交值暴露给 reader、失败的 AI 表投影可重试且不回滚已确认的写模型；随后才定义实现与迁移步骤。这个任务是本计划的显式暂停点：不得在没有批准的设计下猜测 endpoint、写入顺序或一致性策略，也不得在本计划创建写模型生产代码或测试。

---

### Task 10: 在所有消费者完成 DB-only 改造后执行 `business → apps → common` 目录迁移

**Files:**
- Move: `common/daily_robot/` → `apps/robot/daily/`
- Move: `common/broadcaster.py` → `apps/broadcast/dingtalk.py`
- Move: `数字化/钉钉/杭州日报机器人/` → `business/offline/hangzhou/daily/`
- Move: `数字化/钉钉/shaoxing_daily_robot/` → `business/offline/shaoxing/daily/`
- Move: `数字化/钉钉/渠道日报机器人/` → `business/online/channel_daily/`
- Create: `apps/__init__.py`, `apps/robot/__init__.py`, `apps/broadcast/__init__.py`
- Create: `business/__init__.py`, `business/online/__init__.py`, `business/offline/__init__.py`
- Create: `business/offline/hangzhou/__init__.py`, `business/offline/shaoxing/__init__.py`
- Create: `business/offline/hangzhou/daily/__init__.py`, `business/offline/shaoxing/daily/__init__.py`, `business/online/channel_daily/__init__.py`
- Preserve: the moved `apps/robot/daily/__init__.py`
- Create: `tests/architecture/test_dependency_boundaries.py`
- Modify: affected imports, `business/offline/hangzhou/daily/register_autostart.bat`, `business/offline/shaoxing/daily/register_autostart.bat`, CI, runtime documents

- [ ] **Step 1: 先写静态依赖边界测试。**

`tests/architecture/test_dependency_boundaries.py` 遍历 Python AST，并断言：

```text
business imports only business, apps, common, and Python standard/dependency modules
apps imports only apps, common, and Python standard/dependency modules
common never imports apps or business
business and apps never import common.public_data.connectors
business and apps contain no direct DingTalk AI table/WDT read calls
```

允许 `apps.broadcast` 使用 `common.dingtalk.send_markdown`，允许 `common.public_data.connectors` 使用数据源客户端。获批写模型若需要 AI 表访问，必须由 `common.public_data` 的受控写网关封装；`apps`/`business` 只能调用其写服务，不能直接导入数据源客户端或发起 `/notable/` 请求。测试对发现的违规文件显示相对路径与导入/属性名称。

- [ ] **Step 2: 运行依赖测试以确认旧目录不满足目标。**

Run:

```bash
python -m unittest tests.architecture.test_dependency_boundaries -v
```

Expected: FAIL，因为旧目录和残存直接源读取尚未迁移。

- [ ] **Step 3: 只在 Task 7、Task 8 通过，并且 Task 9 获批后的独立写模型实施计划已完成、测试通过后移动已跟踪源码。**

Run:

```bash
mkdir -p apps/robot apps/broadcast business/online business/offline/hangzhou business/offline/shaoxing
git mv common/daily_robot apps/robot/daily
git mv common/broadcaster.py apps/broadcast/dingtalk.py
git mv "数字化/钉钉/杭州日报机器人" business/offline/hangzhou/daily
git mv "数字化/钉钉/shaoxing_daily_robot" business/offline/shaoxing/daily
git mv "数字化/钉钉/渠道日报机器人" business/online/channel_daily
```

只为新建的父包和区域包创建空 `__init__.py`；随 `common/daily_robot/` 移动的 `apps/robot/daily/__init__.py` 必须保留其现有公开导出。将导入改为绝对包导入或同包相对导入。使用 `python -m business...` 作为唯一入口，删除只为目录深度存在的 `sys.path.insert()` 与 `Path.parents[N]` repo-root 推导；保留 `BASE_DIR` 对本地配置、状态、日志的相对定位。

每个 BAT 自启包装器保留现有后台行为，但 Python 启动行改为对应模块入口；不得硬编码机器绝对路径、不得执行或注册该 BAT。

- [ ] **Step 4: 迁移后以模块命令验证导入且不触发外部副作用。**

Run:

```bash
python -c "from apps.robot.daily import today_info; from apps.broadcast.dingtalk import Broadcaster; import business.online.channel_daily.main; print('imports ok')"
python -m unittest tests.architecture.test_dependency_boundaries -v
```

Expected: 输出 `imports ok`，依赖边界测试 PASS，且不读取真实配置或调用网络。

---

### Task 11: 最后更新 CI、当前运行文档与受控验证

**前置条件：** 仅在 Task 7、Task 8 均通过，Task 9 获批后的独立写模型实施计划已完成并通过其测试，且 Task 10 的目录迁移及依赖边界测试均已通过后执行。本任务不得用文档或 CI 变更绕过这些条件。

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `docs/命令速查表.md`
- Modify: `docs/云迁移指南.md`
- Modify: `business/offline/hangzhou/daily/README.md`
- Modify: `business/offline/shaoxing/daily/README.md`
- Modify: `business/online/channel_daily/README_ECS部署指南.md`
- Modify: `数字化/钉钉/榜单服务/部署指南.md`
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_public_data_mysql.py`

- [ ] **Step 1: 写入受控 test-RDS 集成测试。**

`tests/integration/test_public_data_mysql.py` 必须在以下条件以外跳过：

```python
os.environ.get("PUBLIC_DATA_RUN_INTEGRATION") == "1"
and os.environ.get("APP_ENV") == "test"
```

测试还必须断言三个数据库名均以 `_test` 结尾。它仅对测试库运行 `migrate_raw_database()`（使用临时合成物理表契约）及 `migrate_mart_database()`，以 `integration-test-<uuid>` 写入一条合成物理镜像行，再读取并在同一 transaction 中按该测试 ID 删除。测试不能读取或同步任何真实数据源，也不能在 `production` 环境执行。

- [ ] **Step 2: 将 CI 改为检查新模块和单元测试。**

CI 先安装依赖，再执行：

```bash
python -m pip install -r requirements.txt
python -m compileall -q common apps business
python -m unittest discover -s tests -t . -v
```

保留全部 `config.example.json` 的 JSON 校验。CI 不设置 RDS、DingTalk 或 WDT 凭据，不执行集成测试、不运行 `sync`/`project` 命令、不发送消息。

- [ ] **Step 3: 更新当前运行文档，不改写历史计划。**

README、命令速查表和部署文档只给出以下已实现且由运维确认后才可接入 scheduler 的命令：

```bash
python -m common.public_data.cli migrate --source dingtalk
python -m common.public_data.cli migrate --source wdt
python -m common.public_data.cli migrate --source mart
python -m common.public_data.cli sync --source dingtalk
python -m common.public_data.cli sync --source wdt
python -m common.public_data.cli project --dataset channel-orders
python -m common.public_data.cli project --dataset channel-stock
python -m common.public_data.cli project --dataset channel-daily-metrics
python -m common.public_data.cli project --dataset channel-month-targets
python -m common.public_data.cli project --dataset daily-report
```

文档必须要求先完成 test RDS 对账，再切换每一个业务模块；不得声称自动切换既有 scheduler。目录迁移完成后，命令速查表使用 `python -m business...` 模块命令。

- [ ] **Step 4: 运行最终本地验证，不执行外部操作。**

Run:

```bash
python -m unittest discover -s tests -t . -v
python -m compileall -q common apps business
python -m unittest tests.integration.test_public_data_mysql -v
git diff --check
```

Expected: 单元测试和编译通过；集成测试在未同时设置 `PUBLIC_DATA_RUN_INTEGRATION=1` 与 `APP_ENV=test` 时报告 `skipped`；`git diff --check` 无空白错误。只有用户明确授权并通过环境变量提供测试库凭据后，才可运行：

```bash
PUBLIC_DATA_RUN_INTEGRATION=1 APP_ENV=test python -m unittest tests.integration.test_public_data_mysql -v
```

不得在本计划内运行真实源同步、生产 RDS、DingTalk 写表、群发送或 scheduler 注册。

---

## 迁移验收门槛

1. 每个已启用数据集都有本地允许列表契约、物理 raw 表、契约测试与一个成功的 test-RDS 对账记录；DingTalk 表头名称和 `sourceType`、WDT 字段、ID、`initialCursor` 与后续水位均无猜测。
2. `raw_dingtalk` 与 `raw_wdt` 只保存当前来源镜像；run、schema 和 cursor 元数据独立保存，不把它们伪装成业务历史版本。
3. `mart_ops` 的订单、库存、渠道日报按日指标、渠道月目标和日报读模型都有稳定主键、期间/区域/渠道索引与只读 query 测试。
4. 渠道日报、杭州和绍兴的只读功能不再直读 DingTalk AI 表或 WDT；DingTalk 仅用于消息投递。
5. Stream 报数和 AI 表写回先完成 Task 9 的书面模型决策；其幂等、事务和重试测试只在获批后的独立实施计划中完成，完成前不得移动或启用含写路径的日报包。
6. 只有在前五项成立且独立写模型实施计划通过后，才允许执行目录移动和运维命令变更；外部 scheduler 由运维人员单独确认后切换。

## Plan self-review

- **DB-first coverage:** 数据库隔离、物理原始镜像、DingTalk/WDT 同步、数据集市、业务消费者、写模型和最后的目录迁移都按依赖顺序列出。
- **Source safety:** 所有外部读/写只在明确定义的连接器或批准写模型内；单元测试没有网络，真实 test-RDS 需要双重显式开关。
- **Schema focus:** 物理表由字段允许列表定义；技术主键、同步字段、索引和跨期间/跨公司指标索引各归属正确层级，不使用通用 JSON 记录表替代实体结构。
- **Known pause:** 监听器写模型未经业务批准，计划显式停止在设计门槛，避免猜测 AI 表与 RDS 的最终一致性策略。
