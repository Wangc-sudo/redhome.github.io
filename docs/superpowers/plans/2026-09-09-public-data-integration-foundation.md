# Public Data Integration Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立独立、环境隔离的公共 RDS 数据集成层，定时将钉钉 AI 表和旺店通 ERP 的只读原始数据同步至 MySQL，并为已验证的钉钉考勤连接器预留安全启用门槛；日报机器人仅作为后续消费者。

**Architecture:** 新增 `common.public_data`，由环境设置、MySQL 连接与迁移、原始记录仓储、数据源连接器、同步服务和无副作用 CLI 组成。每个数据源仅把配置白名单内的数据落到 RDS；业务机器人不得在播报、提醒或榜单运行时直调钉钉或旺店通。`APP_ENV=test|production` 决定唯一允许的数据库和配置，测试群的 `TEST_MODE=1` 仍只用于消息路由，不承担数据库隔离职责。

**Tech Stack:** Python 3.12+、PyMySQL 1.1.1、MySQL 8.0、标准库 `unittest`/`unittest.mock`、现有 `common.dingtalk.DingTalkClient`、`common.wdt.WdtClient`、本机千问办公桌面端 cron。

**Safety constraints:**

- 不提交 RDS 地址、账号、密码、钉钉 `appSecret`、旺店通凭据、真实 Base/Sheet ID、真实人员 ID 或真实群配置。
- 不发送群消息、DING，不调用 `DingTalkClient.update_records()`，不改写 AI 表，不改造现有日报脚本，不变更真实 cron、ECS、systemd 或生产 RDS。
- 测试和生产使用不同的数据库凭据、数据库名与网络访问策略；代码拒绝 `APP_ENV` 与数据库名不匹配的启动。
- 考勤公开页面尚未提供可实现的请求/响应契约。本计划只实现“禁用且显式拒绝调用”的考勤门槛；获取官方 endpoint、权限、字段、分页与限流证据后，另立实施计划接通真实考勤 API，绝不猜测接口。

---

## File structure

| Path | Responsibility |
|---|---|
| `requirements.txt` | 固定公共数据层唯一新增的运行时依赖 PyMySQL。 |
| `README.md` | 说明安装公共数据层依赖和环境隔离约束。 |
| `common/public_data/__init__.py` | 导出公共设置、同步服务和异常类型。 |
| `common/public_data/settings.py` | 读取并验证 `APP_ENV`、RDS 环境变量与非敏感数据源清单。 |
| `common/public_data/config.example.json` | 无密钥、默认禁用的数据源映射模板。 |
| `common/public_data/db.py` | MySQL 连接、事务及 MySQL 命名锁。 |
| `common/public_data/migrations.py` | 版本化 SQL 迁移、校验和与环境哨兵校验。 |
| `common/public_data/migrations/001_initial.sql` | 同步运行、游标、源记录与字段快照的初始表结构。 |
| `common/public_data/repository.py` | 原始记录幂等 upsert、游标、运行日志和只读查询。 |
| `common/public_data/sync_service.py` | 以源/数据集为粒度加锁、写入运行状态并协调连接器。 |
| `common/public_data/cli.py` | `migrate`、`sync`、`status` 三个不发送消息的命令入口。 |
| `common/public_data/connectors/dingtalk_notable.py` | 复用 AI 表读取接口并落地表结构及记录快照。 |
| `common/public_data/connectors/wdt.py` | 复用旗舰版 WDT 客户端、窗口化分页和水位去重。 |
| `common/public_data/connectors/attendance.py` | 在缺少官方契约时拒绝真实考勤同步。 |
| `ops/public_data_sync/schedule.example.json` | 可供本机 cron 使用的无凭据同步频率清单，不安装调度。 |
| `tests/common/test_public_data_settings.py` | 环境隔离、凭据缺失和配置验证测试。 |
| `tests/common/test_public_data_migrations.py` | 迁移排序、校验和、环境哨兵和锁测试。 |
| `tests/common/test_public_data_repository.py` | 原始记录、游标、运行日志与幂等性测试。 |
| `tests/common/test_public_data_dingtalk.py` | AI 表分页、字段变化和只读同步测试。 |
| `tests/common/test_public_data_wdt.py` | WDT 窗口、分页、水位和失败回滚测试。 |
| `tests/common/test_public_data_attendance.py` | 禁用考勤连接器绝不发起 HTTP 调用的测试。 |
| `tests/common/test_public_data_cli.py` | CLI 参数、环境传递和无消息副作用测试。 |
| `tests/integration/test_public_data_mysql.py` | 仅显式开启时连接测试 RDS 的迁移与仓储集成测试。 |

---

### Task 1: Establish dependency and environment boundaries

**Files:**
- Create: `requirements.txt`
- Create: `common/public_data/__init__.py`
- Create: `common/public_data/settings.py`
- Create: `common/public_data/config.example.json`
- Modify: `README.md`
- Test: `tests/common/test_public_data_settings.py`

- [ ] **Step 1: Write failing environment-isolation tests.**

```python
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.public_data.settings import ConfigError, Settings


class TestSettings(unittest.TestCase):
    def _env(self, **overrides):
        values = {
            "APP_ENV": "test",
            "PUBLIC_DATA_RDS_HOST": "127.0.0.1",
            "PUBLIC_DATA_RDS_PORT": "3306",
            "PUBLIC_DATA_RDS_USER": "public_data_test",
            "PUBLIC_DATA_RDS_PASSWORD": "not-a-real-secret",
            "PUBLIC_DATA_RDS_DATABASE": "digital_ops_test",
        }
        values.update(overrides)
        return values

    def test_loads_test_environment_with_test_database(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "sources.json"
            config.write_text('{"dingtalkAiTables": [], "wangdian": {"datasets": []}}', encoding="utf-8")
            with patch.dict(os.environ, self._env(PUBLIC_DATA_CONFIG=str(config)), clear=True):
                settings = Settings.from_environment()
        self.assertEqual(settings.app_env, "test")
        self.assertEqual(settings.database.name, "digital_ops_test")

    def test_rejects_unknown_environment(self):
        with patch.dict(os.environ, self._env(APP_ENV="staging"), clear=True):
            with self.assertRaisesRegex(ConfigError, "APP_ENV"):
                Settings.from_environment()

    def test_rejects_test_environment_without_test_database_suffix(self):
        with patch.dict(os.environ, self._env(PUBLIC_DATA_RDS_DATABASE="digital_ops_prod"), clear=True):
            with self.assertRaisesRegex(ConfigError, "_test"):
                Settings.from_environment()

    def test_rejects_production_environment_using_test_database(self):
        with patch.dict(os.environ, self._env(APP_ENV="production"), clear=True):
            with self.assertRaisesRegex(ConfigError, "production"):
                Settings.from_environment()

    def test_rejects_missing_password(self):
        env = self._env()
        del env["PUBLIC_DATA_RDS_PASSWORD"]
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ConfigError, "PUBLIC_DATA_RDS_PASSWORD"):
                Settings.from_environment()
```

- [ ] **Step 2: Run the settings tests to confirm the package is absent.**

Run: `python -m unittest tests.common.test_public_data_settings -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'common.public_data'`.

- [ ] **Step 3: Add the fixed database dependency and configuration template.**

Create root `requirements.txt` with exactly:

```text
PyMySQL==1.1.1
```

Create `common/public_data/config.example.json` with only disabled, nonsecret mappings:

```json
{
  "dingtalkAiTables": [],
  "wangdian": {
    "datasets": []
  },
  "attendance": {
    "enabled": false
  }
}
```

The runtime configuration file is selected only through `PUBLIC_DATA_CONFIG`; operators copy this template to a file under ignored `config-local/`. Keep all credentials in the following environment variables, never in JSON: `PUBLIC_DATA_RDS_HOST`, `PUBLIC_DATA_RDS_PORT`, `PUBLIC_DATA_RDS_USER`, `PUBLIC_DATA_RDS_PASSWORD`, `PUBLIC_DATA_RDS_DATABASE`, `DINGTALK_APP_KEY`, `DINGTALK_APP_SECRET`, `DINGTALK_OPERATOR_ID`, `WDT_SID`, `WDT_APP_KEY`, and `WDT_APP_SECRET`.

- [ ] **Step 4: Implement strict settings loading.**

Implement these public types and rules in `settings.py`:

```python
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


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
    database: DatabaseSettings
    source_config_path: Path

    @classmethod
    def from_environment(cls, environ=None):
        values = os.environ if environ is None else environ
        app_env = values.get("APP_ENV", "")
        if app_env not in {"test", "production"}:
            raise ConfigError("APP_ENV must be test or production")
        required = (
            "PUBLIC_DATA_RDS_HOST",
            "PUBLIC_DATA_RDS_PORT",
            "PUBLIC_DATA_RDS_USER",
            "PUBLIC_DATA_RDS_PASSWORD",
            "PUBLIC_DATA_RDS_DATABASE",
            "PUBLIC_DATA_CONFIG",
        )
        missing = [name for name in required if not values.get(name)]
        if missing:
            raise ConfigError(f"missing required setting: {missing[0]}")
        try:
            port = int(values["PUBLIC_DATA_RDS_PORT"])
        except ValueError as exc:
            raise ConfigError("PUBLIC_DATA_RDS_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ConfigError("PUBLIC_DATA_RDS_PORT must be between 1 and 65535")
        database_name = values["PUBLIC_DATA_RDS_DATABASE"]
        if app_env == "test" and not database_name.endswith("_test"):
            raise ConfigError("test database must end in _test")
        if app_env == "production" and database_name.endswith("_test"):
            raise ConfigError("production database cannot end in _test")
        source_config_path = Path(values["PUBLIC_DATA_CONFIG"])
        try:
            config_value = json.loads(source_config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError("PUBLIC_DATA_CONFIG does not exist") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError("PUBLIC_DATA_CONFIG must contain JSON") from exc
        if not isinstance(config_value, dict):
            raise ConfigError("PUBLIC_DATA_CONFIG must contain a JSON object")
        return cls(
            app_env=app_env,
            database=DatabaseSettings(
                host=values["PUBLIC_DATA_RDS_HOST"],
                port=port,
                user=values["PUBLIC_DATA_RDS_USER"],
                password=values["PUBLIC_DATA_RDS_PASSWORD"],
                name=database_name,
            ),
            source_config_path=source_config_path,
        )
```

Add `import json` and `import os` before the displayed imports. The factory must use `os.environ` when `environ is None`, require `APP_ENV` to be exactly `test` or `production`, require every `PUBLIC_DATA_RDS_*` value, parse the port as an integer between 1 and 65535, and require the source configuration file to exist and contain a JSON object. The returned settings are immutable and no exception includes a password.

Create `__init__.py` to export `ConfigError`, `DatabaseSettings`, and `Settings` only.

- [ ] **Step 5: Document installation without changing existing robot startup.**

Add this concise subsection after README’s current runtime bullets:

```markdown
### 公共数据层

公共数据同步使用 `PyMySQL`，首次在目标环境执行 `python -m pip install -r requirements.txt`。运行前必须显式设置 `APP_ENV=test` 或 `APP_ENV=production`，并使用与该环境对应的独立 RDS 凭据和数据库；`TEST_MODE` 仅切换钉钉测试群，不能替代 `APP_ENV`。
```

- [ ] **Step 6: Re-run the focused tests.**

Run: `python -m unittest tests.common.test_public_data_settings -v`

Expected: PASS. No network, RDS, DingTalk, WDT or message call occurs.

---

### Task 2: Add migration and connection infrastructure with environment sentinels

**Files:**
- Create: `common/public_data/db.py`
- Create: `common/public_data/migrations.py`
- Create: `common/public_data/migrations/001_initial.sql`
- Test: `tests/common/test_public_data_migrations.py`

- [ ] **Step 1: Write failing migration tests using fake DB-API objects.**

Create `TestMigrations` with these exact tests:

1. `test_applies_sql_files_in_version_order_once` creates `001_first.sql` and `002_second.sql` in `migration_directory`, invokes `migrate(connection, "test", migration_directory)` twice, and asserts each SQL body is recorded once in filename order.
2. `test_rejects_changed_checksum_for_applied_version` runs `001_initial.sql`, rewrites its content, then asserts the second `migrate(connection, "test", migration_directory)` call raises `MigrationError` before that changed SQL executes.
3. `test_rejects_database_initialized_for_other_environment` returns `{"app_env": "production"}` for the environment lookup and asserts `migrate(connection, "test", migration_directory)` raises `MigrationError` without applying new files.
4. `test_releases_mysql_lock_when_migration_raises` makes executing `001_initial.sql` raise `RuntimeError`, then asserts the fake cursor recorded `RELEASE_LOCK` after the exception.

The fake connection must record executed SQL and emulate `fetchone()` for migration rows, environment sentinel rows and `GET_LOCK`. Assert that an applied version is not executed twice, a changed checksum raises `MigrationError`, an existing `production` sentinel rejects a `test` start, and `RELEASE_LOCK` runs from `finally` even when migration SQL fails.

- [ ] **Step 2: Run the focused migration tests to confirm they fail.**

Run: `python -m unittest tests.common.test_public_data_migrations -v`

Expected: FAIL because `db` and `migrations` do not exist.

- [ ] **Step 3: Implement the minimal MySQL connection and named-lock helpers.**

In `db.py`, expose:

```python
from contextlib import contextmanager


def connect(database_settings):
    """Return a PyMySQL connection with autocommit disabled and UTF-8 enabled."""


@contextmanager
def transaction(connection):
    """Commit only after the body succeeds; otherwise roll back and re-raise."""


@contextmanager
def named_lock(connection, lock_name, timeout_seconds=30):
    """Acquire MySQL GET_LOCK and always release it."""
```

`connect` must pass `charset="utf8mb4"`, `cursorclass=pymysql.cursors.DictCursor`, `autocommit=False`, and only values from `DatabaseSettings`. `named_lock` must execute `SELECT GET_LOCK(%s, %s) AS acquired`, raise `LockUnavailable` unless `acquired == 1`, and execute `SELECT RELEASE_LOCK(%s)` in `finally`.

- [ ] **Step 4: Create the initial migration schema.**

`001_initial.sql` must create only these tables, all with `ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`:

```sql
CREATE TABLE IF NOT EXISTS pd_schema_migration (
  version VARCHAR(64) NOT NULL,
  checksum CHAR(64) NOT NULL,
  applied_at DATETIME(6) NOT NULL,
  PRIMARY KEY (version)
);

CREATE TABLE IF NOT EXISTS pd_environment (
  singleton TINYINT NOT NULL,
  app_env VARCHAR(16) NOT NULL,
  initialized_at DATETIME(6) NOT NULL,
  PRIMARY KEY (singleton),
  CONSTRAINT chk_pd_environment_singleton CHECK (singleton = 1)
);

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
  error_detail TEXT NULL,
  PRIMARY KEY (run_id),
  KEY idx_pd_sync_run_lookup (app_env, source_name, dataset, started_at)
);

CREATE TABLE IF NOT EXISTS pd_sync_cursor (
  app_env VARCHAR(16) NOT NULL,
  source_name VARCHAR(64) NOT NULL,
  dataset VARCHAR(128) NOT NULL,
  cursor_value VARCHAR(255) NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (app_env, source_name, dataset)
);

CREATE TABLE IF NOT EXISTS pd_source_schema (
  app_env VARCHAR(16) NOT NULL,
  source_name VARCHAR(64) NOT NULL,
  dataset VARCHAR(128) NOT NULL,
  schema_sha256 CHAR(64) NOT NULL,
  schema_json JSON NOT NULL,
  observed_at DATETIME(6) NOT NULL,
  PRIMARY KEY (app_env, source_name, dataset)
);

CREATE TABLE IF NOT EXISTS pd_source_record (
  app_env VARCHAR(16) NOT NULL,
  source_name VARCHAR(64) NOT NULL,
  dataset VARCHAR(128) NOT NULL,
  external_id VARCHAR(255) NOT NULL,
  source_updated_at DATETIME(6) NULL,
  payload_sha256 CHAR(64) NOT NULL,
  payload_json JSON NOT NULL,
  first_seen_at DATETIME(6) NOT NULL,
  last_seen_at DATETIME(6) NOT NULL,
  last_seen_run_id CHAR(36) NOT NULL,
  PRIMARY KEY (app_env, source_name, dataset, external_id),
  KEY idx_pd_source_record_seen (app_env, source_name, dataset, last_seen_at),
  KEY idx_pd_source_record_updated (app_env, source_name, dataset, source_updated_at)
);
```

- [ ] **Step 5: Implement versioned migrations and the sentinel.**

In `migrations.py`, expose `MigrationError`, `discover_migrations(directory)`, and `migrate(connection, app_env, migration_directory)`. Discovery accepts only files named `NNN_description.sql`, sorts by filename, and calculates the SHA-256 of the UTF-8 bytes. `migrate` must acquire `public-data-schema`, create `pd_schema_migration` before inspecting applied versions, reject checksum drift, execute each unapplied file inside `transaction`, then insert its version and checksum. Finally, it must insert `(1, app_env, UTC timestamp)` into `pd_environment` if absent, or raise `MigrationError` when the stored environment differs.

- [ ] **Step 6: Run focused migration tests.**

Run: `python -m unittest tests.common.test_public_data_migrations -v`

Expected: PASS with only fake connections. A separate real-RDS integration test is deferred to Task 7.

---

### Task 3: Implement an idempotent raw-data repository

**Files:**
- Create: `common/public_data/repository.py`
- Test: `tests/common/test_public_data_repository.py`

- [ ] **Step 1: Write failing repository tests.**

Create `TestSourceRecordRepository` with these exact cases:

1. `test_same_payload_upsert_keeps_first_seen_and_updates_last_seen` upserts `{"b": 2, "a": 1}` then `{"a": 1, "b": 2}` for one external id and asserts equal canonical hashes, unchanged `first_seen_at`, and refreshed `last_seen_at`/run id.
2. `test_changed_payload_updates_hash_and_payload` upserts the same external id with a different value and asserts the parameterized duplicate-key update carries the new JSON and SHA-256.
3. `test_cursor_advances_only_after_records_commit` makes record persistence raise and asserts `save_cursor` was not called; after a successful persistence it asserts the exact next cursor is saved once.
4. `test_status_query_never_returns_other_environment_runs` creates a repository with `app_env="test"`, calls `latest_runs`, and asserts the query parameters contain `test` and no unscoped query is issued.

Use a fake cursor to capture parameters. Assert JSON is serialized with `ensure_ascii=False`, `sort_keys=True`, and compact separators; this makes payload hashes stable. The failed-transaction test must verify cursor persistence is skipped when record persistence raises.

- [ ] **Step 2: Run the repository tests to verify they fail.**

Run: `python -m unittest tests.common.test_public_data_repository -v`

Expected: FAIL because `repository.py` is absent.

- [ ] **Step 3: Add repository value objects and deterministic serialization.**

Create these types:

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class SourceRecord:
    external_id: str
    payload: dict[str, Any]
    source_updated_at: datetime | None = None


@dataclass(frozen=True)
class SyncRun:
    run_id: str
    app_env: str
    source_name: str
    dataset: str
```

Implement `canonical_json(value)` using `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`, and `payload_sha256(value)` over its UTF-8 encoding.

- [ ] **Step 4: Implement the repository contract.**

Expose these exact `SourceRepository(connection, app_env)` methods:

| Method | Inputs | Result and required side effect |
|---|---|---|
| `start_run` | `source_name`, `dataset` | Returns a new `SyncRun` and inserts a `running` row with a UUID. |
| `finish_run` | `run`, `status`, `read_count`, `upserted_count`, optional `error_detail` | Sets finish time, counts and status for exactly `run.run_id`. |
| `get_cursor` | `source_name`, `dataset` | Returns the current cursor string or `None`. |
| `save_cursor` | `source_name`, `dataset`, `cursor_value` | Inserts or updates exactly one environment-scoped cursor. |
| `upsert_schema` | `source_name`, `dataset`, `schema` | Returns whether the canonical schema hash changed and persists the raw schema. |
| `upsert_records` | `run`, iterable of `SourceRecord` | Returns the number received and performs parameterized duplicate-key upserts. |
| `list_current_records` | `source_name`, `dataset` | Returns raw `SourceRecord` values for the current environment only. |
| `latest_runs` | optional `source_name`, optional `dataset`, optional `limit=20` | Returns at most `limit` environment-scoped run dictionaries newest first. |

`upsert_records` must use parameterized `INSERT ... ON DUPLICATE KEY UPDATE`, never format data into SQL. Preserve `first_seen_at` for existing rows, update `last_seen_at` and `last_seen_run_id` on every observation, and update payload columns only when the hash differs. Every query includes `self.app_env`; `list_current_records` returns raw JSON dictionaries and performs no business aggregation.

- [ ] **Step 5: Run repository tests.**

Run: `python -m unittest tests.common.test_public_data_repository -v`

Expected: PASS. No source connector is invoked.

---

### Task 4: Synchronize DingTalk AI-table snapshots read-only

**Files:**
- Create: `common/public_data/connectors/__init__.py`
- Create: `common/public_data/connectors/dingtalk_notable.py`
- Create: `common/public_data/sync_service.py`
- Test: `tests/common/test_public_data_dingtalk.py`

- [ ] **Step 1: Write failing AI-table connector tests.**

Create `TestDingTalkNotableConnector` with these exact cases:

1. `test_emits_each_record_id_as_external_id` returns two records from the fake and asserts the connector emits `SourceRecord` objects with external ids `rec-1` and `rec-2`.
2. `test_records_field_definition_fingerprint` passes equivalent field-definition dictionaries with reversed key order and asserts their canonical schema hashes match.
3. `test_rejects_missing_record_id` returns a record with no `id` and asserts `SourceContractError` before repository persistence starts.
4. `test_never_calls_update_records` gives the fake an `update_records` method that raises `AssertionError`; a successful read proves that method is never called.

The test data must include two field definitions with reversed key order and records such as `{"id": "rec-1", "fields": {"金额": 1280}}`. Assert the schema fingerprint is stable, `rec-1` is the external identifier, a missing `id` raises `SourceContractError`, and the fake’s `update_records` attribute is neither read nor called.

- [ ] **Step 2: Run the focused test.**

Run: `python -m unittest tests.common.test_public_data_dingtalk -v`

Expected: FAIL because the connector is missing.

- [ ] **Step 3: Define the nonsecret source mapping.**

Add this disabled example object to `common/public_data/config.example.json`:

```json
{
  "dingtalkAiTables": [
    {
      "enabled": false,
      "dataset": "channel_daily_sales",
      "baseId": "",
      "sheetId": ""
    }
  ],
  "wangdian": {"datasets": []},
  "attendance": {"enabled": false}
}
```

At runtime, each enabled mapping must have nonempty `dataset`, `baseId`, and `sheetId`; duplicate `dataset` values are rejected before any network call. `sheetId` is intentionally configured explicitly, so sync does not choose a same-named sheet at runtime.

- [ ] **Step 4: Implement the connector and shared synchronization coordinator.**

In `dingtalk_notable.py`, create `SourceContractError` and `DingTalkNotableConnector(client, mapping)`. Its `read()` method must:

1. call `client.list_fields(mapping["baseId"], mapping["sheetId"])` once;
2. call `client.list_records(mapping["baseId"], mapping["sheetId"])` once, relying on the existing client’s internal pagination;
3. return the raw field definitions plus `SourceRecord(str(record["id"]), record)` for every record;
4. reject a non-dictionary record or empty record id.

In `sync_service.py`, create `SyncService(repository, connection, app_env)` with `sync_snapshot(source_name, dataset, connector)`. It must acquire `public-data:{app_env}:{source_name}:{dataset}`, call `repository.start_run`, persist schema and records inside one transaction, mark the run `success`, and mark it `failed` with a credential-free error summary if any read or write fails. It must not update a cursor for full AI-table snapshots and must not import `common.test_group` or any message sender.

Instantiate `DingTalkClient` only from `DINGTALK_APP_KEY`, `DINGTALK_APP_SECRET`, and `DINGTALK_OPERATOR_ID`; it must be passed into the connector and only its read APIs are used.

- [ ] **Step 5: Run the focused connector tests.**

Run: `python -m unittest tests.common.test_public_data_dingtalk -v`

Expected: PASS. The tests prove the public layer cannot write AI-table data.

---

### Task 5: Synchronize configuration-whitelisted Wangdian datasets incrementally

**Files:**
- Create: `common/public_data/connectors/wdt.py`
- Test: `tests/common/test_public_data_wdt.py`

- [ ] **Step 1: Write failing WDT connector tests.**

Use a fake `WdtClient.call_paged` and a mapping with a known row identifier and update timestamp field. Create `TestWdtConnector` with these exact cases:

1. `test_renders_only_window_placeholders_in_params` provides nested parameters containing `{window_start}` and `{window_end}`, asserts both render in UTC ISO-8601 form, and asserts `{unknown}` raises `SourceContractError` before `call_paged` runs.
2. `test_uses_overlap_window_and_returns_maximum_watermark` starts from `2026-09-09T10:00:00+00:00`, configures `overlapMinutes=10`, asserts the rendered start is `2026-09-09T09:50:00+00:00`, and asserts the returned cursor is the latest row timestamp.
3. `test_rejects_row_without_configured_identifier` returns a row without the configured `idField` and asserts `SourceContractError`.
4. `test_does_not_advance_cursor_when_page_read_fails` makes `call_paged` raise `WdtError` and asserts `SyncService` does not call `save_cursor`.

Assert only `{window_start}` and `{window_end}` are substituted; any other brace-delimited token raises `SourceContractError`. Given an old cursor and `overlapMinutes=10`, assert the request starts ten minutes before the cursor. A missing ID fails the dataset run; no cursor is saved after the fake raises.

- [ ] **Step 2: Run the focused WDT tests to verify failure.**

Run: `python -m unittest tests.common.test_public_data_wdt -v`

Expected: FAIL because the connector does not exist.

- [ ] **Step 3: Add the exact configurable WDT dataset contract.**

Document and validate every enabled object below; incomplete objects are rejected before a WDT call:

```json
{
  "enabled": false,
  "dataset": "orders",
  "method": "",
  "params": {},
  "idField": "",
  "updatedAtField": "",
  "overlapMinutes": 10,
  "pageSize": 40
}
```

`method`, `idField`, and `updatedAtField` must be nonempty only when `enabled` is true. The operator copies an already proven method and its exact parameter names from the existing verified WDT scripts into ignored local configuration. The connector does not invent WDT method names, parameter names, or response field names. `params` may contain only literal JSON values plus exact `{window_start}` and `{window_end}` values.

- [ ] **Step 4: Implement WDT extraction without duplicating the existing client’s signing code.**

Implement `WdtConnector(client, mapping)` with `read(cursor_value, now)`. It must parse the ISO-8601 cursor in UTC, subtract `overlapMinutes`, render the two permitted placeholders in nested parameter values, and call only:

```python
client.call_paged(
    mapping["method"],
    rendered_params,
    page_size=mapping["pageSize"],
)
```

For each returned dictionary, use `str(row[mapping["idField"]])` as `SourceRecord.external_id`, parse `updatedAtField` only as an ISO-8601 timestamp, and set `source_updated_at`. Return `(records, next_cursor)` where `next_cursor` is the maximum valid row timestamp formatted in UTC; when no valid rows exist, return the input cursor unchanged. Do not bypass `common.wdt.WdtClient`, use the deprecated `openapi2` client, or log signed request parameters.

Add this method to `SyncService`, importing `datetime` and `timezone` from `datetime` plus `named_lock` and `transaction` from `common.public_data.db`:

```python
def sync_incremental(self, source_name: str, dataset: str, connector) -> SyncRun:
    lock_name = f"public-data:{self.app_env}:{source_name}:{dataset}"
    with named_lock(self.connection, lock_name):
        run = self.repository.start_run(source_name, dataset)
        read_count = 0
        upserted_count = 0
        try:
            cursor_value = self.repository.get_cursor(source_name, dataset)
            records, next_cursor = connector.read(
                cursor_value,
                datetime.now(timezone.utc),
            )
            read_count = len(records)
            with transaction(self.connection):
                upserted_count = self.repository.upsert_records(run, records)
                self.repository.save_cursor(source_name, dataset, next_cursor)
                self.repository.finish_run(
                    run,
                    "success",
                    read_count,
                    upserted_count,
                )
        except Exception as error:
            with transaction(self.connection):
                self.repository.finish_run(
                    run,
                    "failed",
                    read_count,
                    upserted_count,
                    type(error).__name__,
                )
            raise
    return run
```

The success status, record upserts and cursor write are one transaction. Therefore any extraction or persistence error either happens before the cursor write or rolls that write back; the separate failed-run transaction records only the exception class name and never a request, credential or payload value.

- [ ] **Step 5: Run focused WDT tests.**

Run: `python -m unittest tests.common.test_public_data_wdt -v`

Expected: PASS. The only WDT calls are mocks; existing channel scripts remain unchanged.

---

### Task 6: Add a safe attendance contract gate instead of guessing an API

**Files:**
- Create: `common/public_data/connectors/attendance.py`
- Test: `tests/common/test_public_data_attendance.py`

- [ ] **Step 1: Write failing disabled-attendance tests.**

```python
import unittest

from common.public_data.connectors.attendance import (
    AttendanceContractUnavailable,
    AttendanceConnector,
)


class TestAttendanceConnector(unittest.TestCase):
    def test_disabled_connector_refuses_before_client_is_called(self):
        called = []
        connector = AttendanceConnector(enabled=False, request=lambda *args: called.append(args))
        with self.assertRaises(AttendanceContractUnavailable):
            connector.read("2026-09-09", ["u1"])
        self.assertEqual(called, [])
```

Add a second test proving `enabled=True` is also rejected until an immutable, verified contract object is supplied; `request` must still have no calls.

- [ ] **Step 2: Run the focused attendance tests.**

Run: `python -m unittest tests.common.test_public_data_attendance -v`

Expected: FAIL because the connector is absent.

- [ ] **Step 3: Implement only the refusal boundary.**

Implement these exact public types:

```python
from typing import Any, Callable, NoReturn


class AttendanceContractUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "attendance synchronization is disabled until the official API "
            "contract is recorded and contract tests have passed"
        )


class AttendanceConnector:
    def __init__(
        self,
        enabled: bool,
        request: Callable[..., Any],
        contract: object | None = None,
    ) -> None:
        self.enabled = enabled
        self.request = request
        self.contract = contract

    def read(self, date: str, user_ids: list[str]) -> NoReturn:
        raise AttendanceContractUnavailable()
```

`read` must always raise `AttendanceContractUnavailable` without evaluating `enabled`, `contract`, `date`, `user_ids`, or invoking `request`. The fixed exception text states the source is disabled until the official contract is recorded and contract tests have passed; it contains no credentials. Do not add `pd_attendance_day`, authorization scopes, endpoint strings, request payloads, or a generic HTTP fallback in this task.

- [ ] **Step 4: Record the exact unlock criteria for a separate attendance implementation plan.**

Do not enable the configuration flag until all six items are independently evidenced from official DingTalk materials or an authorized test tenant: (1) endpoint and HTTP method, (2) app type and permissions, (3) required user identifier type, (4) date/timezone semantics, (5) request and response pagination schema, and (6) handling of no schedule, leave and compensatory time off. The follow-up plan must add redacted success, empty-page, pagination and permission-denied fixtures before it implements the actual connector and attendance projection table.

- [ ] **Step 5: Run focused attendance tests.**

Run: `python -m unittest tests.common.test_public_data_attendance -v`

Expected: PASS and zero HTTP calls.

---

### Task 7: Provide a no-message CLI, schedule template, and test-RDS verification

**Files:**
- Create: `common/public_data/cli.py`
- Create: `ops/public_data_sync/schedule.example.json`
- Create: `tests/common/test_public_data_cli.py`
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_public_data_mysql.py`

- [ ] **Step 1: Write failing CLI tests.**

Cover these command lines with patched `Settings.from_environment`, `connect`, `migrate`, connector factories and `SyncService`:

```python
python -m common.public_data.cli migrate
python -m common.public_data.cli sync --source dingtalk-ai
python -m common.public_data.cli sync --source wdt
python -m common.public_data.cli status --limit 5
```

Assert `migrate` calls only migration code, each `sync` source only instantiates its matching connector, `status` uses the current environment repository, and no command imports `common.dingtalk.send_markdown`, `common.test_group`, `common.daily_robot`, or a robot-local module. Test invalid source names return exit status 2 without opening a connection.

- [ ] **Step 2: Run the CLI test to verify it fails.**

Run: `python -m unittest tests.common.test_public_data_cli -v`

Expected: FAIL because `cli.py` does not exist.

- [ ] **Step 3: Implement CLI commands and the schedule template.**

The CLI accepts exactly these subcommands:

```text
migrate
sync --source {dingtalk-ai,wdt,attendance}
status [--limit N]
```

It loads `Settings` before connecting, validates config mappings, and returns `0` on success, `1` for an integration or configuration failure, and `2` for invalid arguments. `sync --source attendance` must fail through `AttendanceContractUnavailable` until the separate attendance plan is completed. Standard output may include environment, source, dataset, counts and status; it must never print passwords, tokens, signed WDT parameters, source payloads or generated SQL.

Create this schedule template without installing it:

```json
{
  "jobs": [
    {
      "name": "dingtalk-ai-sync",
      "everyMinutes": 15,
      "command": "python -m common.public_data.cli sync --source dingtalk-ai"
    },
    {
      "name": "wangdian-sync",
      "everyMinutes": 30,
      "command": "python -m common.public_data.cli sync --source wdt"
    }
  ]
}
```

A later operator may place these commands in the existing desktop cron only after local and test-RDS verification. This plan does not edit `daily_scheduler.py`, register a cron task, or run either job against a real source.

- [ ] **Step 4: Add opt-in test-RDS integration coverage.**

`tests/integration/test_public_data_mysql.py` must skip unless both `PUBLIC_DATA_RUN_INTEGRATION=1` and `APP_ENV=test`. It must additionally assert that `Settings.from_environment().database.name` ends in `_test`, then execute `migrate`, upsert one synthetic `SourceRecord`, read it back, and remove only that record by its test-specific external id in a transaction. The test must never accept `APP_ENV=production`, even if the caller sets `PUBLIC_DATA_RUN_INTEGRATION=1`.

- [ ] **Step 5: Run unit, syntax, and gated integration verification.**

Run:

```bash
python -m unittest discover -s tests/common -t . -v
python -m compileall common ops
python -m unittest tests.integration.test_public_data_mysql -v
```

Expected: common unit tests pass; compileall passes; the integration test reports `skipped` without explicit test-RDS authorization and environment. After the user has separately authorized a test-RDS run and supplied only test credentials through the environment, run:

```bash
PUBLIC_DATA_RUN_INTEGRATION=1 APP_ENV=test python -m unittest tests.integration.test_public_data_mysql -v
```

Expected: PASS against the test database only. Do not run an external-source sync, send a message, or connect to production as part of this plan.

---

## Consumer adoption boundary

This foundation deliberately leaves the current robot implementations untouched. A later, separately reviewed consumer plan may:

1. migrate the channel reports from direct AI-table/WDT reads to `SourceRepository.list_current_records`; it uses only `APP_ENV`, RDS and data synchronization, not attendance exemption, unfilled audits or leaderboard progress;
2. add an official-contract-backed attendance projection and have Hangzhou/Shaoxing daily robots obtain reportable members from RDS; and
3. retain `common.test_group.resolve_target` for message test/prod routing independently from RDS selection.

## Plan self-review

- **Scope:** Covers environment isolation, RDS schema/migrations, raw DingTalk AI table and WDT synchronization, CLI scheduling groundwork, test-RDS verification, and a non-guessing attendance gate. It intentionally excludes robot behavior, deployment and production actions.
- **Safety:** Every external credential is environment-only; unit tests mock all external clients; real integration tests require both an explicit flag and `APP_ENV=test`.
- **Consistency:** All source records, cursors, run logs and queries are namespaced by `app_env`; full snapshots do not advance a cursor, incremental WDT synchronization updates its cursor atomically with records.
- **Known decision:** The exact DingTalk attendance API contract is not present in the public page content available during planning. The plan therefore fails closed rather than inventing an endpoint or response schema.
