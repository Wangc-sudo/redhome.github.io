# 受限真实源公共数据同步实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在显式启用的 Docker `live-sync` profile 中，只读同步 manifest 明确列出的钉钉 AI 表和 WDT 查询数据到本地三个 MySQL `_test` 库，并可证明完整分页、映射、幂等、事务、锁与库隔离。

**Architecture:** 在 `common.public_data` 中新增一个独立的实时同步边界：先验证运行安全门槛和 JSON manifest，再创建只读来源 gateway。钉钉和 WDT gateway 分别实现不会静默截断的逐页读取；数据集在来源 raw 库内持有命名锁、完成外部读取后以单库事务 upsert，最后将不含业务明细的统计投影写入 `mart_ops_test`。跨库不使用分布式事务：原始库成功而投影失败时标记 `projection_pending`，下次只从本地 raw 数据重建投影。

**Tech Stack:** Python 3.12、标准库 `unittest`/`unittest.mock`、PyMySQL 1.1.1、既有 `common.dingtalk`/`common.wdt` HTTP 协议实现、MySQL 8.4、Docker Compose、Ubuntu 24.04。

**Safety constraints:**

- 自动化单元测试、Docker 全量测试、默认 `test-runner`、CI 和 cron 均不得构造钉钉或 WDT 客户端，且不得发起任何外部请求。
- 实时读取只能由 `docker compose --profile live-sync run --rm sync-runner` 加 `--live-read`、`--confirm-local-test-write` 两个 CLI 参数触发；运行器拒绝 `APP_ENV != test`、`INTEGRATION_TEST_RUNNER != 1`、`PUBLIC_DATA_RDS_HOST != mysql` 或不是精确三个 `_test` 库的环境。
- 不得向钉钉 AI 表、群机器人、webhook、WDT 或任何真实 RDS 写入；同步模块不得导入或调用 `DingTalkClient.update_records()`、群消息或 webhook 方法。
- 不读取、打印、复制、暂存或提交 `config.json`、`wdt_credentials.json`、`common/dingtalk/test_groups.json`、`config-local/`、日志、状态文件或 HTML 生成物。真实运行时所需的 manifest 和新定义的 source-credentials JSON 仅从仓库外的只读 bind mount 提供，程序日志中不得输出其内容。
- manifest 只能选择预注册的 raw 表和 WDT allowlist 方法。任何配置字符串都不能直接成为 SQL 标识符或 WDT method。
- 不修改现有机器人入口、日报逻辑、真实 RDS、外部 scheduler 或本机计划任务。

---

## File structure

| Path | Responsibility |
|---|---|
| `common/public_data/manifest.py` | 将无凭据 version-1 manifest 解析为不可变对象，校验数据集、表、字段、WDT 方法和 UTC 时间窗。 |
| `common/public_data/live_safety.py` | 在来源客户端构造前验证 `live-sync` 的双确认、本地 Docker MySQL 和精确三库边界。 |
| `common/public_data/finance_schema.py` | 以受版本控制的 Python 常量登记财务钉钉 Sheet、物理表、业务列、字段类型和索引。 |
| `common/public_data/live_migrations.py` | 仅从预注册 schema 生成并应用 raw/mart DDL，记录迁移校验和。 |
| `common/public_data/dingtalk_read.py` | 仅暴露 Sheet、字段和严格分页记录读取的钉钉 gateway。 |
| `common/public_data/wdt_read.py` | 仅暴露三个预允许查询方法、时间窗切分和严格分页的 WDT gateway。 |
| `common/public_data/raw_repository.py` | 受控标识符的钉钉/WDT 原始层 upsert、字段转换和本地 raw 统计查询。 |
| `common/public_data/mart_repository.py` | `sync_runs` 状态机、无业务明细的数据集摘要和投影重建查询。 |
| `common/public_data/live_sync.py` | 按数据集获取命名锁、协调外部读取、raw 单库事务和 mart 补偿状态。 |
| `common/public_data/cli.py` | `migrate`、`status`、`live-sync` 和 `rebuild-projection` 命令；所有实时动作都要求双确认。 |
| `common/public_data/config.example.json` | 不含 ID、凭据且默认没有数据集的 version-1 manifest 模板。 |
| `docker-compose.integration.yml` | 新增只在 `live-sync` profile 中出现的单次 `sync-runner` 服务。 |
| `docker/integration/live-source-credentials.example.json` | 不含真实值的凭据文件格式模板，供仓库外复制使用。 |
| `README.md` | 给操作人员的显式本地实时验收前置条件、命令和安全说明。 |
| `tests/common/test_public_data_manifest.py` | Manifest 与双确认安全门槛测试。 |
| `tests/common/test_public_data_live_migrations.py` | DDL 生成、迁移校验和、表白名单和三库对象测试。 |
| `tests/common/test_public_data_dingtalk_read.py` | 钉钉只读接口、schema drift 与严格分页测试。 |
| `tests/common/test_public_data_wdt_read.py` | WDT 方法 allowlist、窗口、主键、响应行列表和严格分页测试。 |
| `tests/common/test_public_data_raw_repository.py` | 原始层字段转换、参数化 upsert 和来源库隔离测试。 |
| `tests/common/test_public_data_live_sync.py` | 运行状态、数据集锁、事务边界、幂等摘要和投影补偿测试。 |
| `tests/common/test_public_data_cli.py` | CLI 参数、客户端延迟构造与安全日志测试。 |
| `tests/integration/test_public_data_live_mysql.py` | Docker MySQL 中的 DDL、真实事务、锁竞争、upsert 和跨库隔离测试。 |
| `tests/test_integration_environment.py` | `live-sync` profile、只读挂载、默认不触发实时读取的 Compose 合约测试。 |

---

### Task 1: Add a versioned manifest and live-run safety gate

**Files:**
- Create: `common/public_data/manifest.py`
- Create: `common/public_data/live_safety.py`
- Modify: `common/public_data/config.example.json`
- Test: `tests/common/test_public_data_manifest.py`

- [ ] **Step 1: Write failing manifest and safety tests.**

```python
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.public_data.live_safety import LiveRunRejected, require_live_run
from common.public_data.manifest import ManifestError, load_manifest
from common.public_data.settings import Settings


class ManifestAndLiveSafetyTests(unittest.TestCase):
    def _manifest(self):
        return {
            "version": 1,
            "dingtalk": {
                "bases": [
                    {
                        "base_id": "base-1",
                        "sheets": [
                            {
                                "sheet_id": "sheet-1",
                                "sheet_name": "店铺扣点费用管理",
                                "dataset": "finance_store_commission",
                                "target_table": "fin_store_commission",
                                "max_pages": 2,
                                "field_mapping": {
                                    "公司主体": {"column": "company_entity", "source_type": "text"},
                                    "费用项目": {"column": "fee_item", "source_type": "text"},
                                },
                            }
                        ],
                    }
                ]
            },
            "wdt": {
                "datasets": [
                    {
                        "dataset": "trade-window",
                        "method": "sales.TradeQuery.queryWithDetail",
                        "target_table": "wdt_records",
                        "record_id_path": "trade_no",
                        "page_size": 100,
                        "max_pages": 2,
                        "window_start": "2026-09-01T00:00:00Z",
                        "window_end": "2026-09-01T00:50:00Z",
                        "max_window_minutes": 50,
                        "params": {"time_type": "2"},
                    }
                ]
            },
        }

    def _settings(self, **overrides):
        values = {
            "APP_ENV": "test",
            "INTEGRATION_TEST_RUNNER": "1",
            "PUBLIC_DATA_RDS_HOST": "mysql",
            "PUBLIC_DATA_RDS_PORT": "3306",
            "PUBLIC_DATA_RDS_USER": "public_data_test",
            "PUBLIC_DATA_RDS_PASSWORD": "local-only",
            "PUBLIC_DATA_DINGTALK_DATABASE": "raw_dingtalk_test",
            "PUBLIC_DATA_WDT_DATABASE": "raw_wdt_test",
            "PUBLIC_DATA_MART_DATABASE": "mart_ops_test",
            "PUBLIC_DATA_CONFIG": "unused-by-test",
        }
        values.update(overrides)
        return Settings(
            app_env=values["APP_ENV"],
            dingtalk_database=Settings.from_environment.__func__.__globals__["DatabaseSettings"](
                values["PUBLIC_DATA_RDS_HOST"], 3306, "public_data_test", "local-only",
                values["PUBLIC_DATA_DINGTALK_DATABASE"],
            ),
            wdt_database=Settings.from_environment.__func__.__globals__["DatabaseSettings"](
                values["PUBLIC_DATA_RDS_HOST"], 3306, "public_data_test", "local-only",
                values["PUBLIC_DATA_WDT_DATABASE"],
            ),
            mart_database=Settings.from_environment.__func__.__globals__["DatabaseSettings"](
                values["PUBLIC_DATA_RDS_HOST"], 3306, "public_data_test", "local-only",
                values["PUBLIC_DATA_MART_DATABASE"],
            ),
            source_config_path=Path(values["PUBLIC_DATA_CONFIG"]),
        )

    def test_loads_only_version_one_with_registered_tables_and_methods(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(self._manifest()), encoding="utf-8")
            manifest = load_manifest(path)

        self.assertEqual("finance_store_commission", manifest.dingtalk_bases[0].sheets[0].dataset)
        self.assertEqual("sales.TradeQuery.queryWithDetail", manifest.wdt_datasets[0].method)

    def test_rejects_schema_drift_unknown_table_and_unallowed_wdt_method(self):
        for mutate, message in (
            (lambda value: value["dingtalk"]["bases"][0]["sheets"][0]["field_mapping"].pop("费用项目"), "field_mapping"),
            (lambda value: value["dingtalk"]["bases"][0]["sheets"][0].__setitem__("target_table", "from_manifest"), "target_table"),
            (lambda value: value["wdt"]["datasets"][0].__setitem__("method", "stock.adjust"), "method"),
        ):
            with self.subTest(message=message):
                value = self._manifest()
                mutate(value)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "manifest.json"
                    path.write_text(json.dumps(value), encoding="utf-8")
                    with self.assertRaisesRegex(ManifestError, message):
                        load_manifest(path)

    def test_rejects_overlapping_or_non_utc_wdt_windows(self):
        value = self._manifest()
        duplicate = dict(value["wdt"]["datasets"][0])
        duplicate["dataset"] = "overlap"
        duplicate["window_start"] = "2026-09-01T00:25:00Z"
        value["wdt"]["datasets"].append(duplicate)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ManifestError, "overlap"):
                load_manifest(path)

    def test_requires_both_flags_and_exact_local_docker_test_boundary(self):
        settings = self._settings()
        with patch.dict(os.environ, {"INTEGRATION_TEST_RUNNER": "1"}, clear=True):
            require_live_run(settings, live_read=True, confirm_local_test_write=True)

        for changed, message in (
            ({"live_read": False}, "--live-read"),
            ({"confirm_local_test_write": False}, "--confirm-local-test-write"),
            ({"settings": self._settings(PUBLIC_DATA_RDS_HOST="127.0.0.1")}, "PUBLIC_DATA_RDS_HOST"),
            ({"settings": self._settings(PUBLIC_DATA_WDT_DATABASE="raw_wdt_shadow_test")}, "raw_wdt_test"),
        ):
            with self.subTest(message=message):
                arguments = {"settings": settings, "live_read": True, "confirm_local_test_write": True}
                arguments.update(changed)
                with patch.dict(os.environ, {"INTEGRATION_TEST_RUNNER": "1"}, clear=True):
                    with self.assertRaisesRegex(LiveRunRejected, message):
                        require_live_run(**arguments)
```

Replace the `Settings.from_environment.__func__.__globals__` lookup in the test with a normal `from common.public_data.settings import DatabaseSettings` import before committing. It is shown only to make the intended fixture fields explicit; the committed test must instantiate `DatabaseSettings` directly.

- [ ] **Step 2: Run the focused tests and confirm they fail because the modules do not exist.**

Run: `python -m unittest tests.common.test_public_data_manifest -v`

Expected: FAIL with `ModuleNotFoundError` for `common.public_data.manifest` or `common.public_data.live_safety`; no network request occurs.

- [ ] **Step 3: Implement immutable manifest types and parsing with strict registries.**

Create these public types in `manifest.py`; use `datetime.fromisoformat(value.replace("Z", "+00:00"))`, then reject a parsed timestamp unless `tzinfo is timezone.utc`.

```python
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class FieldMapping:
    source_name: str
    column: str
    source_type: str


@dataclass(frozen=True)
class DingTalkSheet:
    base_id: str
    sheet_id: str
    sheet_name: str
    dataset: str
    target_table: str
    max_pages: int
    fields: tuple[FieldMapping, ...]


@dataclass(frozen=True)
class WdtDataset:
    dataset: str
    method: str
    target_table: str
    record_id_path: str
    page_size: int
    max_pages: int
    window_start: datetime
    window_end: datetime
    max_window_minutes: int
    params: dict[str, Any]


@dataclass(frozen=True)
class SourceManifest:
    dingtalk_sheets: tuple[DingTalkSheet, ...]
    wdt_datasets: tuple[WdtDataset, ...]
    sha256: str
```

`load_manifest(path)` must read UTF-8 JSON once, hash exactly those bytes with SHA-256, require a top-level object with `version == 1`, reject unknown top-level keys, and require `dingtalk.bases`/`wdt.datasets` lists. It must only accept tables returned by `finance_schema.table_definition(name)` and exactly `wdt_records`; only accept `source_type` values `text`, `singleSelect`, `number`, `currency`, `date`, `user`, `multipleSelect`, and `unidirectionalLink`; ensure every registered business column appears once and has the registered source type. Validate each WDT method against a fixed frozenset, each `record_id_path` as a nonempty dotted identifier, `1 <= page_size <= 1000`, `1 <= max_pages <= 1000`, a positive `max_window_minutes`, `window_start < window_end`, duration no longer than the declared maximum, and no overlapping WDT windows for the same method. Reject IDs, datasets, table names and mapping names duplicated within their source scope.

Replace `config.example.json` with a valid, no-source version-1 document:

```json
{
  "version": 1,
  "dingtalk": {
    "bases": []
  },
  "wdt": {
    "datasets": []
  }
}
```

- [ ] **Step 4: Implement the pre-client live-run gate.**

Create `live_safety.py` with a single public function that is called before reading the credential file or constructing a gateway:

```python
import os


class LiveRunRejected(RuntimeError):
    pass


def require_live_run(settings, *, live_read, confirm_local_test_write, environ=None):
    values = os.environ if environ is None else environ
    if live_read is not True:
        raise LiveRunRejected("--live-read is required")
    if confirm_local_test_write is not True:
        raise LiveRunRejected("--confirm-local-test-write is required")
    if settings.app_env != "test":
        raise LiveRunRejected("APP_ENV must be test")
    if values.get("INTEGRATION_TEST_RUNNER") != "1":
        raise LiveRunRejected("INTEGRATION_TEST_RUNNER must be 1")
    databases = (
        settings.dingtalk_database,
        settings.wdt_database,
        settings.mart_database,
    )
    if any(database.host != "mysql" for database in databases):
        raise LiveRunRejected("PUBLIC_DATA_RDS_HOST must be mysql")
    names = tuple(database.name for database in databases)
    if names != ("raw_dingtalk_test", "raw_wdt_test", "mart_ops_test"):
        raise LiveRunRejected("live sync requires raw_dingtalk_test, raw_wdt_test, mart_ops_test")
```

Do not add a bypass, an allow-host list, a `TEST_MODE` condition, or a fallback to a host-provided database. Keep error strings free of any credential value.

- [ ] **Step 5: Re-run the focused tests.**

Run: `python -m unittest tests.common.test_public_data_manifest -v`

Expected: PASS. The test suite exercises only temporary manifest files and fake settings.

- [ ] **Step 6: Commit the manifest boundary.**

```bash
git add common/public_data/manifest.py common/public_data/live_safety.py common/public_data/config.example.json tests/common/test_public_data_manifest.py
git commit -m "feat: validate restricted live sync manifests"
```

---

### Task 2: Register raw schemas and apply safe per-database migrations

**Files:**
- Create: `common/public_data/finance_schema.py`
- Create: `common/public_data/live_migrations.py`
- Test: `tests/common/test_public_data_live_migrations.py`
- Test: `tests/integration/test_public_data_live_mysql.py`

- [ ] **Step 1: Write failing migration and table-registry tests.**

```python
import unittest

from common.public_data.finance_schema import table_definition
from common.public_data.live_migrations import LiveMigrationError, apply_live_migrations


class FakeCursor:
    def __init__(self):
        self.executed = []
        self.rows = {}

    def execute(self, query, parameters=None):
        self.executed.append((query, parameters))

    def fetchone(self):
        return None

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()
        self.commit_calls = 0
        self.rollback_calls = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1


class LiveMigrationTests(unittest.TestCase):
    def test_registered_finance_table_has_documented_columns_and_indexes(self):
        table = table_definition("fin_store_commission")

        self.assertEqual("fin_store_commission", table.name)
        self.assertIn(("company_entity", "VARCHAR(255)"), [(column.name, column.mysql_type) for column in table.columns])
        self.assertIn("idx_company_store", [index.name for index in table.indexes])
        self.assertIn("dingtalk_record_id", [column.name for column in table.technical_columns])

    def test_migrations_use_only_registered_identifiers_and_record_checksums(self):
        dingtalk = FakeConnection()
        wdt = FakeConnection()
        mart = FakeConnection()

        apply_live_migrations(dingtalk, wdt, mart)

        dingtalk_sql = "\n".join(query for query, _ in dingtalk.cursor_instance.executed)
        wdt_sql = "\n".join(query for query, _ in wdt.cursor_instance.executed)
        mart_sql = "\n".join(query for query, _ in mart.cursor_instance.executed)
        self.assertIn("CREATE TABLE IF NOT EXISTS `fin_store_commission`", dingtalk_sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS `dingtalk_schema_snapshots`", dingtalk_sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS `wdt_records`", wdt_sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS `sync_runs`", mart_sql)
        self.assertNotIn("from_manifest", dingtalk_sql + wdt_sql + mart_sql)

    def test_rejects_checksum_drift_before_running_changed_schema(self):
        connection = FakeConnection()
        with self.assertRaisesRegex(LiveMigrationError, "checksum"):
            apply_live_migrations(connection, connection, connection, applied_checksums={"raw-dingtalk-v1": "0" * 64})
```

The production function does not need an `applied_checksums` argument; it is a test seam that the test should replace with a fake cursor row returned from `pd_live_schema_migration`. The committed test must exercise the real migration lookup and assert that the changed DDL is not executed after drift is found.

Add a real-MySQL integration test that calls `apply_live_migrations()` with three `connect()` results and asserts, through `information_schema.tables`, that `fin_store_commission`/`dingtalk_schema_snapshots` exist only in `raw_dingtalk_test`, `wdt_records` only in `raw_wdt_test`, and `sync_runs`/`sync_dataset_summary` only in `mart_ops_test`.

- [ ] **Step 2: Run focused fake migration tests to observe the missing registry and migrator.**

Run: `python -m unittest tests.common.test_public_data_live_migrations -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'common.public_data.finance_schema'`.

- [ ] **Step 3: Encode the finance table registry without runtime Markdown parsing.**

In `finance_schema.py`, add frozen `ColumnDefinition`, `IndexDefinition`, and `TableDefinition` dataclasses. A table definition owns a tuple of nullable business columns, the three non-null technical columns, and table-local secondary indexes. Expose only:

```python
def table_definition(name):
    try:
        return _TABLES[name]
    except KeyError as exc:
        raise KeyError(f"unregistered DingTalk target table: {name}") from exc


def all_table_definitions():
    return tuple(_TABLES[name] for name in sorted(_TABLES))
```

Transcribe every table, column type and index from `docs/superpowers/specs/2026-09-10-finance-raw-dingtalk-table-mapping.md` into `_TABLES` exactly. The registry must include this complete table set:

```text
fin_store_commission
fin_tax_declaration_2026
fin_tax_sales_reconciliation_2026
fin_tax_stamp_duty
fin_tax_uninvoiced_sales_summary
fin_tax_uninvoiced_sales
fin_tax_prior_period_invoice
fin_tax_pre_invoice
fin_tax_input_invoice
fin_ecommerce_prepayment_supplier_invoice
fin_ecommerce_promotion_recharge_balance
fin_ecommerce_platform_deposit
fin_ecommerce_store_funds_balance
fin_billion_subsidy_pdd
fin_billion_subsidy_douyin
fin_daily_funds
fin_offline_receivables_aging
fin_offline_deposit_other_receivables
```

For `fin_daily_funds`, generate its 155 business amount columns from the five source/column patterns and `range(1, 32)` during module initialization, not by copying only a subset. Preserve `statement_date_raw`, `deposit_paid_at_raw`, and `prior_period_invoice_excl_tax_amount_raw` as `VARCHAR(255)` exactly as the mapping document states. Define field-type expectations beside each business column so the manifest validator can require its `source_type` without inferring it from column names.

- [ ] **Step 4: Implement deterministic DDL and migration bookkeeping.**

Create `live_migrations.py` with `LiveMigrationError` and `apply_live_migrations(dingtalk_connection, wdt_connection, mart_connection)`. Use an internal immutable migration tuple `(version, target, sql)` where `target` is one of `dingtalk`, `wdt`, `mart`; calculate a SHA-256 checksum of each UTF-8 SQL string. For each connection:

1. Create `pd_live_schema_migration(version VARCHAR(64) NOT NULL, checksum CHAR(64) NOT NULL, applied_at DATETIME(6) NOT NULL, PRIMARY KEY (version)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`.
2. Read applied versions with parameter-free `SELECT version, checksum FROM pd_live_schema_migration`.
3. Reject a matching version with a different checksum before issuing its DDL.
4. Execute unrecorded DDL and insert its version/checksum/UTC timestamp inside `transaction(connection)`.

Generate identifiers only from `finance_schema` and literal names in this task; quote each as a MySQL backtick identifier after checking it matches `^[a-z][a-z0-9_]*$`. Never pass manifest values to DDL generation.

The dingtalk migration must create every registered finance table with its business columns nullable, technical columns:

```sql
`dingtalk_record_id` VARCHAR(255) NOT NULL,
`synced_at` DATETIME(6) NOT NULL,
`sync_run_id` CHAR(36) NOT NULL,
PRIMARY KEY (`dingtalk_record_id`),
KEY `idx_synced_at` (`synced_at`)
```

and its documented table indexes, plus exactly:

```sql
CREATE TABLE IF NOT EXISTS `dingtalk_schema_snapshots` (
  `base_id` VARCHAR(255) NOT NULL,
  `sheet_id` VARCHAR(255) NOT NULL,
  `fields_sha256` CHAR(64) NOT NULL,
  `fields_json` JSON NOT NULL,
  `observed_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`base_id`, `sheet_id`, `fields_sha256`),
  KEY `idx_observed_at` (`observed_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

Create the literal `wdt_records`, `sync_runs`, and `sync_dataset_summary` tables exactly as specified in `docs/superpowers/specs/2026-09-10-restricted-live-public-data-sync-design.md:157-201`; do not add raw JSON to `mart_ops_test` or business columns to `wdt_records`.

- [ ] **Step 5: Re-run fake and Docker MySQL migration tests.**

Run: `python -m unittest tests.common.test_public_data_live_migrations -v`

Expected: PASS with fake DB-API connections.

Run: `docker compose -f docker-compose.integration.yml run --rm test-runner python3 -m unittest tests.integration.test_public_data_live_mysql -v`

Expected: PASS against only `mysql:8.4` and the three local `_test` databases; no external source client is constructed.

- [ ] **Step 6: Commit the fixed raw/mart schemas.**

```bash
git add common/public_data/finance_schema.py common/public_data/live_migrations.py tests/common/test_public_data_live_migrations.py tests/integration/test_public_data_live_mysql.py
git commit -m "feat: add local public data raw schemas"
```

---

### Task 3: Build strict, read-only DingTalk gateway and schema verification

**Files:**
- Create: `common/public_data/dingtalk_read.py`
- Test: `tests/common/test_public_data_dingtalk_read.py`

- [ ] **Step 1: Write failing tests against an injected transport.**

```python
import unittest

from common.public_data.dingtalk_read import DingTalkReadError, DingTalkReadGateway
from common.public_data.manifest import DingTalkSheet, FieldMapping


class DingTalkReadGatewayTests(unittest.TestCase):
    def _sheet(self):
        return DingTalkSheet(
            base_id="base-1", sheet_id="sheet-1", sheet_name="店铺扣点费用管理",
            dataset="finance_store_commission", target_table="fin_store_commission", max_pages=2,
            fields=(
                FieldMapping("公司主体", "company_entity", "text"),
                FieldMapping("费用项目", "fee_item", "text"),
            ),
        )

    def test_exposes_only_read_operations_and_uses_get_for_notable_requests(self):
        requests = []
        responses = iter([
            {"value": [{"id": "sheet-1", "name": "店铺扣点费用管理"}]},
            {"value": [{"name": "公司主体", "type": "text"}, {"name": "费用项目", "type": "text"}]},
            {"records": [{"id": "record-1", "fields": {}}], "hasMore": False},
        ])
        gateway = DingTalkReadGateway("key", "secret", "operator", request_json=lambda url, method, body, headers: requests.append((url, method, body)) or next(responses))

        gateway.validate_sheet(self._sheet())
        records = gateway.read_records(self._sheet())

        self.assertEqual(["record-1"], [record["id"] for record in records])
        self.assertFalse(hasattr(gateway, "update_records"))
        self.assertFalse(hasattr(gateway, "send_group_markdown"))
        self.assertEqual({"GET"}, {method for _, method, _ in requests})
        self.assertTrue(all(body is None for _, _, body in requests))

    def test_rejects_missing_token_page_limit_duplicate_or_blank_record_id(self):
        cases = (
            ([{"records": [{"id": "a", "fields": {}}], "hasMore": True}], "nextToken"),
            ([{"records": [{"id": "a", "fields": {}}], "hasMore": True, "nextToken": "p2"}, {"records": [{"id": "b", "fields": {}}], "hasMore": True, "nextToken": "p3"}], "max_pages"),
            ([{"records": [{"id": "a", "fields": {}}, {"id": "a", "fields": {}}], "hasMore": False}], "duplicate"),
            ([{"records": [{"id": "", "fields": {}}], "hasMore": False}], "record id"),
        )
        for responses, message in cases:
            with self.subTest(message=message):
                gateway = DingTalkReadGateway("key", "secret", "operator", request_json=lambda *_: responses.pop(0))
                with self.assertRaisesRegex(DingTalkReadError, message):
                    gateway.read_records(self._sheet())

    def test_rejects_sheet_name_or_field_schema_drift_before_record_reads(self):
        calls = []
        gateway = DingTalkReadGateway(
            "key", "secret", "operator",
            request_json=lambda url, *_: calls.append(url) or {"value": [{"id": "sheet-1", "name": "renamed"}]},
        )

        with self.assertRaisesRegex(DingTalkReadError, "sheet_name"):
            gateway.validate_sheet(self._sheet())

        self.assertEqual(1, len(calls))
```

- [ ] **Step 2: Run the new tests and confirm the missing gateway fails.**

Run: `python -m unittest tests.common.test_public_data_dingtalk_read -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'common.public_data.dingtalk_read'`.

- [ ] **Step 3: Implement a gateway that cannot write.**

`DingTalkReadGateway` must not subclass `DingTalkClient`, must not store a `DingTalkClient` instance, and must not import its group/webhook/update functions. Implement a private token cache and a private `_request_json` callable that defaults to an adapter around `common.dingtalk.client._http_json`. The only public operations are:

```python
class DingTalkReadGateway:
    def list_sheets(self, base_id): ...
    def list_fields(self, base_id, sheet_id): ...
    def validate_sheet(self, sheet): ...
    def read_records(self, sheet, page_size=100): ...
```

`list_sheets`, `list_fields`, and record pages must invoke the notable URLs already used by `DingTalkClient` with `method="GET"`, no body, `operatorId` query argument, and an access-token header. `validate_sheet` must find exactly the configured `sheet_id`, verify its `name == sheet.sheet_name`, load fields, and compare the exact set of `(name, type)` values to the mapping entries. Before comparison, translate registered source types to their DingTalk type names one-to-one; no unconfigured source field, extra remote field, missing remote field, or type mismatch may be ignored. It returns `(fields_sha256, canonical_fields_json)` where JSON is `ensure_ascii=False`, `sort_keys=True`, and compact separators.

`read_records` starts with no token, issues one page per request, insists that each response `records` or `value` is a list, and maintains a set of nonempty string `record["id"]`. On `hasMore is True`, it requires a nonempty string `nextToken`; if that page was the configured `max_pages`, raise `DingTalkReadError("max_pages ...")` instead of silently returning partial results. A final page with `hasMore` false is successful even when it is exactly page `max_pages`. Raise sanitized error messages that identify only the dataset and failure kind, never headers, URLs with tokens, or response bodies.

- [ ] **Step 4: Add an explicit schema-snapshot repository-facing value.**

Add a frozen `DingTalkSchemaSnapshot` dataclass in `dingtalk_read.py`:

```python
@dataclass(frozen=True)
class DingTalkSchemaSnapshot:
    base_id: str
    sheet_id: str
    fields_sha256: str
    fields_json: str
```

Return this snapshot from `validate_sheet`; the raw repository added in Task 5 will persist it before raw rows. This keeps schema metadata separate from business field payloads and lets the synchronizer validate all configured Sheets before beginning a raw transaction.

- [ ] **Step 5: Run the focused gateway tests.**

Run: `python -m unittest tests.common.test_public_data_dingtalk_read -v`

Expected: PASS. Every fake request is a GET to a notable read route and none is an update/message route.

- [ ] **Step 6: Commit the DingTalk read boundary.**

```bash
git add common/public_data/dingtalk_read.py tests/common/test_public_data_dingtalk_read.py
git commit -m "feat: add strict read-only dingtalk gateway"
```

---

### Task 4: Add WDT allowlist, contiguous windows, and strict pagination

**Files:**
- Create: `common/public_data/wdt_read.py`
- Test: `tests/common/test_public_data_wdt_read.py`

- [ ] **Step 1: Write failing WDT read tests.**

```python
import unittest
from datetime import datetime, timezone

from common.public_data.manifest import WdtDataset
from common.public_data.wdt_read import PaginationLimitExceeded, WdtReadError, WdtReadGateway


class WdtReadGatewayTests(unittest.TestCase):
    def _dataset(self, **changes):
        values = {
            "dataset": "trade-window",
            "method": "sales.TradeQuery.queryWithDetail",
            "target_table": "wdt_records",
            "record_id_path": "trade_no",
            "page_size": 2,
            "max_pages": 2,
            "window_start": datetime(2026, 9, 1, tzinfo=timezone.utc),
            "window_end": datetime(2026, 9, 1, 1, 40, tzinfo=timezone.utc),
            "max_window_minutes": 50,
            "params": {"time_type": "2"},
        }
        values.update(changes)
        return WdtDataset(**values)

    def test_uses_only_allowlisted_method_and_non_overlapping_fifty_minute_windows(self):
        calls = []
        gateway = WdtReadGateway(call=lambda method, params, **page: calls.append((method, params, page)) or {"data": {"order": []}})

        records = gateway.read_dataset(self._dataset())

        self.assertEqual([], records)
        self.assertEqual(3, len(calls))
        self.assertEqual({"sales.TradeQuery.queryWithDetail"}, {call[0] for call in calls})
        self.assertEqual([0, 0, 0], [call[2]["page_no"] for call in calls])
        self.assertEqual("2026-09-01 00:00:00", calls[0][1]["start_time"])
        self.assertEqual("2026-09-01 00:50:00", calls[0][1]["end_time"])
        self.assertEqual("2026-09-01 01:40:00", calls[-1][1]["end_time"])

    def test_rejects_unknown_response_shape_full_final_page_and_missing_stable_id(self):
        dataset = self._dataset(window_end=datetime(2026, 9, 1, 0, 50, tzinfo=timezone.utc))
        for response, message in (
            ({"data": {"unexpected": []}}, "row list"),
            ({"data": {"order": [{"trade_no": "one"}, {"trade_no": "two"}]}}, "max_pages"),
            ({"data": {"order": [{"trade_no": ""}]}}, "record_id_path"),
        ):
            with self.subTest(message=message):
                gateway = WdtReadGateway(call=lambda *_args, **_kwargs: response)
                expected = PaginationLimitExceeded if message == "max_pages" else WdtReadError
                with self.assertRaisesRegex(expected, message):
                    gateway.read_dataset(dataset)

    def test_rejects_unallowlisted_method_without_invoking_transport(self):
        calls = []
        gateway = WdtReadGateway(call=lambda *args, **kwargs: calls.append((args, kwargs)))
        dataset = self._dataset(method="stock.adjust")

        with self.assertRaisesRegex(WdtReadError, "method"):
            gateway.read_dataset(dataset)

        self.assertEqual([], calls)
```

- [ ] **Step 2: Run focused tests and verify the module is absent.**

Run: `python -m unittest tests.common.test_public_data_wdt_read -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'common.public_data.wdt_read'`.

- [ ] **Step 3: Implement the sealed WDT reader.**

In `wdt_read.py`, define this fixed method allowlist and never expose a public method that takes a caller-supplied method string:

```python
ALLOWED_WDT_METHODS = frozenset(
    {
        "sales.TradeQuery.queryWithDetail",
        "wms.stockin.Purchase.queryWithDetail",
        "wms.StockSpec.search2",
    }
)
```

`WdtReadGateway` may receive a private injected `call` callable for tests and production binds it to `WdtClient(...).call`. Its sole public action is `read_dataset(dataset)`. Before each source call, verify `dataset.method in ALLOWED_WDT_METHODS`; copy `dataset.params` and add only the source time keys for the current UTC window. Do not mutate manifest dictionaries.

Split the half-open interval `[window_start, window_end)` into intervals no longer than `max_window_minutes`: each next start is the prior end, and the last end equals `window_end`. Convert only UTC instants to WDT's existing `"%Y-%m-%d %H:%M:%S"` format. For each window, call page 0 upward with `page_size`, `page_no`, and `calc_total=0`. A response must have `data` as a mapping and exactly one known list-valued key among `order`, `list`, or `goods_list`; an unknown/missing/multiple shape raises `WdtReadError`.

Append returned dictionaries only after extracting the nonempty string at `record_id_path`, allowing nested mapping lookup for dotted paths. A short page or empty list ends its window. If the `max_pages`-th page is full, raise `PaginationLimitExceeded` instead of silently returning a partial window. Deduplicate nothing in the reader: preserve source rows so the repository can upsert the same stable ID; reject a duplicated record ID within the same page/window because it makes the claimed source page ambiguous. Error messages may name the dataset/method but never include WDT response data, URL, signing material, account, or request parameters.

- [ ] **Step 4: Add duplicate-ID and boundary-window tests.**

Extend the test class with one test that returns the same `trade_no` twice in one response and expects `WdtReadError`, and one with a 100-minute interval where the fake transport receives exactly `[00:00,00:50)` and `[00:50,01:40)` without a gap or overlap. Add a test for `wms.StockSpec.search2` using `goods_list`, proving that the accepted response key is validated rather than treated as a generic empty response.

- [ ] **Step 5: Run the focused WDT tests.**

Run: `python -m unittest tests.common.test_public_data_wdt_read -v`

Expected: PASS with only injected in-memory callables; no WDT HTTP client is instantiated.

- [ ] **Step 6: Commit the WDT read boundary.**

```bash
git add common/public_data/wdt_read.py tests/common/test_public_data_wdt_read.py
git commit -m "feat: add strict wdt read gateway"
```

---

### Task 5: Persist raw rows and mart status using source-local transactions

**Files:**
- Create: `common/public_data/raw_repository.py`
- Create: `common/public_data/mart_repository.py`
- Test: `tests/common/test_public_data_raw_repository.py`

- [ ] **Step 1: Write failing repository tests.**

```python
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from common.public_data.raw_repository import DingTalkRawRepository, WdtRawRepository


class FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, query, parameters=None):
        self.executed.append((query, parameters))

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()

    def cursor(self):
        return self.cursor_instance


class RawRepositoryTests(unittest.TestCase):
    def test_dingtalk_upsert_transforms_registered_types_and_never_uses_manifest_table_sql(self):
        connection = FakeConnection()
        repository = DingTalkRawRepository(connection)
        repository.upsert_records(
            "fin_store_commission",
            [{"id": "record-1", "fields": {"公司主体": "甲公司", "填写人": [{"id": "u1"}], "2026-01": "12.3400"}}],
            sync_run_id="00000000-0000-0000-0000-000000000001",
            synced_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

        query, parameters = connection.cursor_instance.executed[-1]
        self.assertIn("INSERT INTO `fin_store_commission`", query)
        self.assertNotIn("from_manifest", query)
        self.assertIn(Decimal("12.3400"), parameters)
        self.assertTrue(any(value == '[{"id":"u1"}]' for value in parameters))

    def test_wdt_upsert_uses_method_and_stable_source_id_primary_key(self):
        connection = FakeConnection()
        repository = WdtRawRepository(connection)
        repository.upsert_records(
            method="sales.TradeQuery.queryWithDetail",
            records=[({"trade_no": "T-1", "amount": 2}, "T-1")],
            window_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 9, 1, 0, 50, tzinfo=timezone.utc),
            sync_run_id="00000000-0000-0000-0000-000000000001",
            synced_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

        query, parameters = connection.cursor_instance.executed[-1]
        self.assertIn("ON DUPLICATE KEY UPDATE", query)
        self.assertIn("T-1", parameters)
        self.assertIn('{"amount":2,"trade_no":"T-1"}', parameters)

    def test_projection_queries_only_raw_run_metadata_and_never_payload_json(self):
        connection = FakeConnection()
        repository = WdtRawRepository(connection)
        repository.summary_for_run("00000000-0000-0000-0000-000000000001")

        query, parameters = connection.cursor_instance.executed[-1]
        self.assertIn("sync_run_id", query)
        self.assertNotIn("payload_json", query)
        self.assertEqual(("00000000-0000-0000-0000-000000000001",), parameters)
```

Add separate tests for missing/invalid decimal and date values that expect a conversion exception before an INSERT executes, JSON serialization with `ensure_ascii=False`, `sort_keys=True`, compact separators, and a mart repository test proving `sync_dataset_summary` inserts only name/count/digest/timestamps—not raw field values or WDT JSON.

- [ ] **Step 2: Run focused repository tests and confirm they fail before implementation.**

Run: `python -m unittest tests.common.test_public_data_raw_repository -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'common.public_data.raw_repository'`.

- [ ] **Step 3: Implement registered DingTalk conversions and safe upserts.**

`DingTalkRawRepository` must accept a raw-dingtalk connection and resolve every table through `finance_schema.table_definition`. Use its predefined identifier list to build parameterized `INSERT ... ON DUPLICATE KEY UPDATE` SQL; set every business column from the current source record and update only business columns, `synced_at`, and `sync_run_id` on duplicate. Never concatenate an identifier from the manifest or a DingTalk record.

Implement these conversion rules from the table registry:

| Registered MySQL type | Accepted source value | Stored value |
|---|---|---|
| `VARCHAR(255)` / `TEXT` | `None` or `str` | `None` or unchanged string |
| `DECIMAL(20,4)` | `None`, `int`, `float`, `str`, `Decimal` | `Decimal(str(value))`, rejecting non-finite values |
| `DATE` | `None` or `YYYY-MM-DD` string | `date.fromisoformat(value)` |
| `JSON` | any JSON-serializable value | canonical JSON string |

Do not coerce arbitrary objects to text and do not reinterpret columns whose registered names end in `_raw`. A conversion failure identifies only table/column/type and occurs before the transaction persists any record. Persist `DingTalkSchemaSnapshot` through a parameterized insert with `INSERT IGNORE` so repeated discovery does not duplicate an identical `(base_id, sheet_id, fields_sha256)` record.

`WdtRawRepository.upsert_records()` must accept `(row, stable_id)` pairs produced by the gateway; write canonical payload JSON to literal `wdt_records` with `(source_method, source_record_id)` as the duplicate key and update window bounds, payload, `synced_at`, and `sync_run_id` on repeated IDs.

- [ ] **Step 4: Implement mart run state and safe summary data.**

`MartRepository` must provide only these state transitions:

```python
start_run(sync_run_id, manifest_sha256, started_at)
mark_raw_committed(sync_run_id)
mark_completed(sync_run_id, finished_at)
mark_failed(sync_run_id, failure_code, finished_at)
mark_projection_pending(sync_run_id, failure_code, finished_at)
save_dataset_summary(sync_run_id, source_name, dataset_name, records_read, raw_records_written, record_id_digest, completed_at)
```

Validate status transitions in Python: `started -> raw_committed -> completed`; `started -> failed`; `raw_committed -> projection_pending`; and `projection_pending -> completed` only through projection rebuild. The query parameters may include only run IDs, manifest hash, status, failure code, dataset/source names, counts, digests and timestamps. Never accept a raw record object or `payload_json` argument.

Use `hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()` for each dataset record-ID digest. Reject an empty source ID at the caller boundary even though an empty configured dataset is valid.

- [ ] **Step 5: Run repository tests.**

Run: `python -m unittest tests.common.test_public_data_raw_repository -v`

Expected: PASS. Captured SQL uses registered/literal identifiers and every value is a DB-API parameter.

- [ ] **Step 6: Commit raw and mart repositories.**

```bash
git add common/public_data/raw_repository.py common/public_data/mart_repository.py tests/common/test_public_data_raw_repository.py
git commit -m "feat: persist restricted source sync results"
```

---

### Task 6: Orchestrate locks, transactions, idempotency, and projection compensation

**Files:**
- Create: `common/public_data/live_sync.py`
- Test: `tests/common/test_public_data_live_sync.py`

- [ ] **Step 1: Write failing orchestration tests with fake gateway/repository dependencies.**

```python
import unittest
from unittest.mock import Mock

from common.public_data.live_sync import LiveSyncService


class LiveSyncServiceTests(unittest.TestCase):
    def _service(self):
        self.dingtalk_gateway = Mock()
        self.wdt_gateway = Mock()
        self.raw_repositories = Mock()
        self.mart_repository = Mock()
        self.connections = Mock()
        return LiveSyncService(
            dingtalk_gateway=self.dingtalk_gateway,
            wdt_gateway=self.wdt_gateway,
            raw_repositories=self.raw_repositories,
            mart_repository=self.mart_repository,
            connections=self.connections,
            now=lambda: "now",
            new_run_id=lambda: "00000000-0000-0000-0000-000000000001",
        )

    def test_reads_before_raw_transaction_then_writes_summary_and_completed_state(self):
        service = self._service()
        service.sync = Mock()
        # Replace this with the smallest real manifest fixture after implementing the public API.
        # Assert call order: start_run, gateway validation/read, raw transaction/upsert,
        # mark_raw_committed, save_dataset_summary, mark_completed.

    def test_raw_commit_followed_by_projection_failure_marks_pending_without_rereading_source(self):
        service = self._service()
        # Arrange save_dataset_summary to raise after raw upsert succeeds.
        # Assert mark_projection_pending and exactly one gateway read.

    def test_external_read_failure_marks_failed_without_opening_raw_transaction(self):
        service = self._service()
        # Arrange a pagination/schema exception before raw persistence.
        # Assert mark_failed and no raw-repository upsert.
```

Replace the comments with real fake objects and exact assertions before running the red phase; do not commit a test containing a placeholder comment. Add a fourth test where an existing `projection_pending` run causes the service to rebuild projection from raw repository summaries and does not construct/read a source gateway.

- [ ] **Step 2: Run the orchestration tests and confirm the service is missing.**

Run: `python -m unittest tests.common.test_public_data_live_sync -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'common.public_data.live_sync'`.

- [ ] **Step 3: Implement the minimal explicit service API.**

Use a dependency-injected service so unit tests never need MySQL, HTTP, or credentials:

```python
class LiveSyncService:
    def sync(self, manifest): ...
    def rebuild_projection(self, sync_run_id): ...
```

`sync(manifest)` must first reject a pending projection returned by `mart_repository`; this prevents a subsequent sync from replacing the raw row `sync_run_id` needed to rebuild the prior run. It then creates a UUID run ID and calls `start_run`. Before any raw transaction, validate all DingTalk Sheet schemas and keep their snapshots in memory. For each data set, acquire `named_lock` on a connection for its own source database using exactly `public-data:dingtalk:<dataset>` or `public-data:wdt:<dataset>`, complete source reads while no `transaction()` context is open, then persist the dataset in one source-local `transaction()` block.

After each source-local commit, call `mark_raw_committed` (idempotently after the first raw commit) and write its mart summary. When all summaries succeed, call `mark_completed`. On a schema, pagination, gateway, conversion or raw-transaction exception, call `mark_failed` with a fixed safe code (`schema_drift`, `pagination_incomplete`, `source_read_failed`, `raw_write_failed`, or `conversion_failed`) and re-raise an exception without source payload. If raw persistence committed but a mart summary/state write fails, use a new mart connection to call `mark_projection_pending` with `projection_write_failed`, re-raise, and never re-read external data during recovery.

`rebuild_projection(sync_run_id)` may run only for a `projection_pending` run. It queries each raw repository by `sync_run_id` for record IDs/counts, derives the same digest, writes summaries, and transitions to `completed`. It must not receive or invoke either gateway.

- [ ] **Step 4: Replace the placeholder tests with concrete ordering and rollback assertions.**

Use small fakes that append an event name whenever they run. Assert this exact successful order for one DingTalk data set:

```python
[
    "mart.start",
    "dingtalk.validate",
    "lock.public-data:dingtalk:finance_store_commission",
    "dingtalk.read",
    "raw.transaction.begin",
    "raw.snapshot",
    "raw.upsert",
    "raw.transaction.commit",
    "mart.raw_committed",
    "mart.summary",
    "mart.completed",
]
```

For a raw upsert failure, assert the transaction fake rolls back, `mart.failed("raw_write_failed")` occurs, and `mart.summary` never occurs. For projection failure, assert raw commit occurred once, `mart.projection_pending("projection_write_failed")` occurs through a fresh mart repository/connection, and a subsequent `rebuild_projection()` obtains counts/digests solely from raw repositories.

- [ ] **Step 5: Run the focused service tests.**

Run: `python -m unittest tests.common.test_public_data_live_sync -v`

Expected: PASS with no network call and no live credential parsing.

- [ ] **Step 6: Commit the orchestration service.**

```bash
git add common/public_data/live_sync.py tests/common/test_public_data_live_sync.py
git commit -m "feat: coordinate restricted live source sync"
```

---

### Task 7: Add a non-leaking CLI and explicit Docker live-sync profile

**Files:**
- Create: `common/public_data/cli.py`
- Create: `docker/integration/live-source-credentials.example.json`
- Modify: `common/public_data/__init__.py`
- Modify: `docker-compose.integration.yml`
- Modify: `tests/common/test_public_data_cli.py`
- Modify: `tests/test_integration_environment.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing CLI and Compose contract tests.**

```python
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from common.public_data.cli import main


class LiveSyncCliTests(unittest.TestCase):
    def test_live_sync_rejects_missing_confirmation_before_credentials_or_gateways(self):
        with patch("common.public_data.cli.load_settings") as load_settings, patch("common.public_data.cli.load_source_credentials") as load_credentials, patch("common.public_data.cli.build_gateways") as build_gateways:
            with self.assertRaises(SystemExit) as raised:
                main(["live-sync", "--live-read"])

        self.assertNotEqual(0, raised.exception.code)
        load_settings.assert_not_called()
        load_credentials.assert_not_called()
        build_gateways.assert_not_called()

    def test_live_sync_prints_only_safe_summary(self):
        output = io.StringIO()
        with patch("common.public_data.cli.load_settings"), patch("common.public_data.cli.require_live_run"), patch("common.public_data.cli.load_manifest"), patch("common.public_data.cli.load_source_credentials"), patch("common.public_data.cli.build_service") as build_service:
            build_service.return_value.sync.return_value = {
                "run_id": "00000000-0000-0000-0000-000000000001",
                "datasets": [{"source": "wdt", "dataset": "trade-window", "records_read": 2, "raw_records_written": 2, "record_id_digest": "a" * 64}],
            }
            with redirect_stdout(output):
                main(["live-sync", "--live-read", "--confirm-local-test-write", "--source-credentials", "/run/live-input/source-credentials.json"])

        text = output.getvalue()
        self.assertIn("trade-window", text)
        self.assertIn("records_read=2", text)
        self.assertNotIn("appSecret", text)
        self.assertNotIn("payload_json", text)
```

Extend `tests/test_integration_environment.py` so `docker compose ... config --format json` asserts that `sync-runner` has `profiles == ["live-sync"]`, depends on healthy `mysql`, uses the existing Ubuntu build image, has no `ports`, and its command includes both confirmation flags. Assert it has read-only bind mounts for only `/run/live-input/manifest.json` and `/run/live-input/source-credentials.json`; assert `test-runner` still has no `profiles`, no external credentials mount, and runs only `run-tests.sh`.

- [ ] **Step 2: Run the focused tests and verify the missing CLI/profile behavior fails.**

Run: `python -m unittest tests.common.test_public_data_cli tests.test_integration_environment -v`

Expected: FAIL because `common.public_data.cli` and the `live-sync` profile do not exist.

- [ ] **Step 3: Implement lazy CLI construction and sanitized output.**

`cli.py` must expose `main(argv=None)` and use `argparse` subcommands:

```text
migrate
status
live-sync --live-read --confirm-local-test-write --source-credentials PATH
rebuild-projection SYNC_RUN_ID --confirm-local-test-write
```

For `live-sync`, inspect `argv` for both literal confirmation flags before loading settings, parsing manifest, reading `--source-credentials`, opening MySQL, or building gateways. Then call `Settings.from_environment()`, `require_live_run()`, `load_manifest()`, `apply_live_migrations()`, load the new credentials JSON, build gateway/service dependencies, and call `sync()`. The credentials file schema is exactly:

```json
{
  "dingtalk": {
    "app_key": "replace-outside-repository",
    "app_secret": "replace-outside-repository",
    "operator_id": "replace-outside-repository"
  },
  "wdt": {
    "sid": "replace-outside-repository",
    "app_key": "replace-outside-repository",
    "app_secret": "replace-outside-repository"
  }
}
```

`load_source_credentials(path)` validates those exact nonempty string keys and never interpolates their values in exception messages. Its error must contain only `invalid source credentials`. It reads only the explicit runtime path supplied by the operator; do not reuse or inspect existing repository credential files.

The only success line format is:

```text
run_id=<uuid> source=<dingtalk|wdt> dataset=<name> records_read=<n> raw_records_written=<n> record_id_digest=<sha256> status=completed
```

The only failure line format is `run_id=<uuid-or-unknown> status=failed code=<fixed-safe-code>`. No traceback, JSON serialization, URL, environment dump, credential field, source payload, raw column name, or response body may be printed. Return a nonzero status for rejection or failure.

- [ ] **Step 4: Add the Compose profile without making it default.**

Add this service under `services` in `docker-compose.integration.yml`; retain the existing `mysql` and `test-runner` entries unchanged:

```yaml
  sync-runner:
    profiles:
      - live-sync
    build:
      context: .
      dockerfile: docker/integration/Dockerfile
    depends_on:
      mysql:
        condition: service_healthy
    command:
      - python3
      - -m
      - common.public_data.cli
      - live-sync
      - --live-read
      - --confirm-local-test-write
      - --source-credentials
      - /run/live-input/source-credentials.json
    environment:
      APP_ENV: test
      PUBLIC_DATA_RDS_HOST: mysql
      PUBLIC_DATA_RDS_PORT: "3306"
      PUBLIC_DATA_RDS_USER: public_data_test
      PUBLIC_DATA_RDS_PASSWORD: public-data-test-password
      PUBLIC_DATA_DINGTALK_DATABASE: raw_dingtalk_test
      PUBLIC_DATA_WDT_DATABASE: raw_wdt_test
      PUBLIC_DATA_MART_DATABASE: mart_ops_test
      PUBLIC_DATA_CONFIG: /run/live-input/manifest.json
      INTEGRATION_TEST_RUNNER: "1"
    volumes:
      - type: bind
        source: ${PUBLIC_DATA_LIVE_MANIFEST_PATH:-/dev/null}
        target: /run/live-input/manifest.json
        read_only: true
      - type: bind
        source: ${PUBLIC_DATA_LIVE_CREDENTIALS_PATH:-/dev/null}
        target: /run/live-input/source-credentials.json
        read_only: true
```

The `/dev/null` defaults exist only so host-side Compose contract validation has no secret-path prerequisite. The CLI rejects them as invalid inputs. Do not add this service to `run-tests.sh`, CI, `depends_on` of `test-runner`, or default `docker compose up` execution.

Create `docker/integration/live-source-credentials.example.json` using the nonsecret schema from Step 3. Update `common/public_data/__init__.py` only to export public non-credential types (`ManifestError`, `SourceManifest`, `LiveRunRejected`, `LiveSyncService`); do not export credential loading or gateways as a convenience API.

- [ ] **Step 5: Document the manually authorized command without putting paths or values in Git.**

Append a `### 受限实时同步验收` README section that states the command is manual-only, does no external writes, must run from the Docker host, and uses a manifest/credential file stored outside the repository. Include this exact shape with placeholders:

```bash
PUBLIC_DATA_LIVE_MANIFEST_PATH=/absolute/path/manifest.json \
PUBLIC_DATA_LIVE_CREDENTIALS_PATH=/absolute/path/source-credentials.json \
docker compose -f docker-compose.integration.yml --profile live-sync run --rm sync-runner
```

Document that the command is successful only after a second identical run reports unchanged per-dataset `record_id_digest` and raw primary-key counts, and that any `failed` or `projection_pending` run is an acceptance failure. Do not document any production hostname, production database, real ID, secret, or a command that sends a DingTalk message.

- [ ] **Step 6: Run CLI and Compose contract tests.**

Run: `python -m unittest tests.common.test_public_data_cli tests.test_integration_environment -v`

Expected: PASS. The host contract may invoke `docker compose config` but must not start the `sync-runner` profile or contact an external source.

- [ ] **Step 7: Commit the opt-in runtime boundary.**

```bash
git add common/public_data/cli.py common/public_data/__init__.py docker-compose.integration.yml docker/integration/live-source-credentials.example.json tests/common/test_public_data_cli.py tests/test_integration_environment.py README.md
git commit -m "feat: add opt-in local live sync runner"
```

---

### Task 8: Prove end-to-end local MySQL behavior and prepare the manual live acceptance run

**Files:**
- Modify: `tests/integration/test_public_data_live_mysql.py`
- Modify: `README.md`
- Test: `tests/integration/test_public_data_live_mysql.py`
- Test: `tests/common/test_public_data_manifest.py`
- Test: `tests/common/test_public_data_dingtalk_read.py`
- Test: `tests/common/test_public_data_wdt_read.py`
- Test: `tests/common/test_public_data_raw_repository.py`
- Test: `tests/common/test_public_data_live_sync.py`
- Test: `tests/common/test_public_data_cli.py`
- Test: `tests/test_integration_environment.py`

- [ ] **Step 1: Write real-MySQL integration tests for upsert, rollback, lock and isolation.**

Under the existing `INTEGRATION_TEST_RUNNER == "1"` decorator, add tests that use only fake/in-memory gateway responses and the Docker `mysql` service:

1. Apply live migrations, upsert one registered DingTalk record twice with different `sync_run_id`, then assert `raw_dingtalk_test.fin_store_commission` contains one primary-key row and the newest run ID.
2. Start a source-local transaction, persist a WDT row, raise an exception, then assert `raw_wdt_test.wdt_records` has no row for that stable ID.
3. Use two connections to `raw_dingtalk_test` to prove `public-data:dingtalk:finance_store_commission` excludes the second connection until the first releases it.
4. Run a fake DingTalk dataset and a fake WDT dataset through `LiveSyncService`; query `information_schema.tables` and `information_schema.columns` to prove raw_dingtalk does not have `wdt_records`, raw_wdt does not have `fin_store_commission`, and `mart_ops_test.sync_dataset_summary` has no `payload_json`, DingTalk business column, or WDT business column.
5. Execute the same fake manifest twice; compare raw primary-key `COUNT(*)` and the two `sync_dataset_summary.record_id_digest` values, and assert both `sync_runs.status` values are `completed`.
6. Force the first mart summary insert to fail after a raw commit, verify `projection_pending`, then invoke `rebuild_projection` with gateways intentionally unavailable and verify the run becomes `completed` from local raw metadata only.

- [ ] **Step 2: Run the new integration test first and observe missing or incomplete behavior.**

Run: `docker compose -f docker-compose.integration.yml run --rm test-runner python3 -m unittest tests.integration.test_public_data_live_mysql -v`

Expected before completing Tasks 2–7: FAIL at the first missing schema/repository/service behavior. Do not permit this test to select `sync-runner`, mount credentials, or use a real source client.

- [ ] **Step 3: Complete the minimal implementation until the integration test passes.**

Do not add test-only production APIs. Fix only the missing registry, migration, repository, or service behavior identified by the failing assertion. Preserve the source-local transaction rule: external fake reads finish before `transaction()` begins, raw writes roll back only in their source database, and mart contains only run metadata/summary values.

- [ ] **Step 4: Run the complete automated suite in Docker.**

Run: `docker compose -f docker-compose.integration.yml run --rm test-runner`

Expected: all robot tests plus public-data unit/integration tests pass. Host-only Compose contract tests may be skipped inside the container only because they require Docker-host access; no test may activate `live-sync`.

Then run the host-only Compose contract tests:

Run: `python -m unittest tests.test_integration_environment -v`

Expected: PASS. This verifies the profile is opt-in, MySQL remains unexposed, build context excludes sensitive paths, and `test-runner` has no real-source command.

- [ ] **Step 5: Perform the manual live-read acceptance only after the automated evidence is green and an operator provides external files.**

This is an operator action, not an automated test and not an action for an agent to run without a fresh explicit instruction. Before the first run, the operator must independently verify all of the following:

```text
APP_ENV=test
INTEGRATION_TEST_RUNNER=1
PUBLIC_DATA_RDS_HOST=mysql
PUBLIC_DATA_DINGTALK_DATABASE=raw_dingtalk_test
PUBLIC_DATA_WDT_DATABASE=raw_wdt_test
PUBLIC_DATA_MART_DATABASE=mart_ops_test
manifest and source-credentials files are outside the repository
```

Run twice with the exact same external manifest:

```bash
PUBLIC_DATA_LIVE_MANIFEST_PATH=/absolute/path/manifest.json \
PUBLIC_DATA_LIVE_CREDENTIALS_PATH=/absolute/path/source-credentials.json \
docker compose -f docker-compose.integration.yml --profile live-sync run --rm sync-runner
```

For each run, retain only the safe CLI summaries. Acceptance requires every configured dataset to show `status=completed`, the second run's per-dataset digest and raw primary-key count to equal the first, and no `projection_pending`/`failed` row. If any failure occurs, stop; do not retry with broader source scope, more permissive pagination, external writes, or a non-test database.

- [ ] **Step 6: Commit test and documentation evidence.**

```bash
git add tests/integration/test_public_data_live_mysql.py README.md
git commit -m "test: verify restricted public data sync path"
```

---

## Final verification checklist

- [ ] `python -m unittest tests.common.test_public_data_manifest tests.common.test_public_data_live_migrations tests.common.test_public_data_dingtalk_read tests.common.test_public_data_wdt_read tests.common.test_public_data_raw_repository tests.common.test_public_data_live_sync tests.common.test_public_data_cli tests.test_integration_environment -v` passes from the Docker host without real-source access.
- [ ] `docker compose -f docker-compose.integration.yml run --rm test-runner` passes and does not contain `live-sync` in its entrypoint or command.
- [ ] `docker compose -f docker-compose.integration.yml config --format json` shows `sync-runner` only in the `live-sync` profile, no MySQL host port, and only read-only external-input bind mounts for the profile service.
- [ ] The implementation never imports/calls DingTalk write, message, DING or webhook functions from the live-sync path, and WDT accepts only the three fixed read methods.
- [ ] All raw table identifiers originate in `finance_schema` or literal DDL; no manifest record, table name, field name, WDT method or source ID is concatenated into SQL.
- [ ] A manually authorized two-run live test is performed only by an operator with external files and proves identical primary-key counts/digests, completion status, and strict three-database separation.
