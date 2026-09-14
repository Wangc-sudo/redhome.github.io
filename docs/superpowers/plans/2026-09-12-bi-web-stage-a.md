# bi-web 阶段 A（L1 驾驶舱 ①③⑫）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地自研 BI 展示层的首批：FastAPI + Jinja2 + ECharts 的 `bi-web` 长驻服务，渲染 L1 首页驾驶舱的 ①线下日销 / ③电商渠道日销 / ⑫年度目标达成进度 五张卡；只读 `mart_ops`，零凭据、零外呼、回环端口暴露。

**Architecture:** 「SQL 在代码、编排在 Nacos」——卡片口径（SQL / 查询函数）活在 `common/bi_web/cards.py` + `queries.py`（可评审、可单测、可 diff）；Nacos 新组 `BI` 只存页面编排（哪个页面摆哪些卡、标题、span、enabled），加载链 `Nacos(group=BI) → 种子文件 → 内置最小默认`，fail-open 与管线注册表同构。`dim_target`（年度目标）走**独立小迁移 + `load-target` 种子重放命令**（spec §13 开放点①落定：目标是业务口径，不是 raw 投影）。注册表关停是**请求级门控**（30s TTL 缓存，Nacos 关 `bi-web` 即全站 503，无需重启）。

**Tech Stack:** Python 3.12、FastAPI 0.141.1、uvicorn 0.52.4、Jinja2 3.1.6、httpx 0.28.1（TestClient）、PyMySQL 1.1.1、MySQL 8.4、Docker Compose、标准库 `unittest`。

**Safety constraints:**

- `bi-web` 只读 `mart_ops`，**零写库**（连 SELECT 都只走 mart 连接，不触发任何 `require_*` 写门控）、零凭据挂载、零 `--live-*` 旗标、零外呼（ECharts 由**浏览器**拉 CDN，服务端不出网）——Compose 合约测试把守，沿用 robot 先例。
- 认证：`BI_WEB_TOKEN` 非空 → `/d/`、`/api/` 全部要求 `Authorization: Bearer <token>`（`/healthz` 豁免，探活不因认证失败）；token 为空 = 本地开发免认证。端口 `127.0.0.1:18080:8080` 仅回环（与 MySQL `13306` 同约定）。
- 错误响应与日志不得包含 SQL、traceback、DB 主机名、payload——照 `cli.py` 的安全输出纪律。
- 本计划不含 commit 步骤：本地 main、仅用户明确要求时提交、绝不 push（既有约定）。
- 不修改 `common.metrics.daily_report`（⑫的月口径单点已就绪，不重复测，spec §11）。

**数据事实修正（实现者必读——spec §7 的假设与真实数据不符，以本节为准）：**

| 事实 | 结论 |
|---|---|
| `fact_daily_report_offline.region` 存的是**中文** `杭州` / `绍兴`（已 `SELECT DISTINCT` 验证），不是 spec §7 假设的英文 id | SQL **不需要** `region IN (...)` 过滤（表内只有这两个值）；跨行汇总的正确性依赖下一行的合计排除 |
| offline 表含**合计行**（`responsible_person` 形如 `杭州合计`/`杭中合计`/`滨萧合计`/`余杭合计`/`绍兴合计`/`绍兴大区总合计`/`诸暨总合计`）| 一切 Σ`sales_amount` 必须加 `responsible_person NOT LIKE '%合计%'`——代码库 7 处既有惯例（core.py×4、leaderboard.py、listener.py、mart_leaderboard.py），不排除即双计 |
| 事实表**预填行到月底**（未来日期 sales_amount=0/NULL，Metabase 平线坑）| 一切查询必须 `business_date <= CURDATE()` |
| 金额单位**元**；dim_target 种子值以万元口径给出（线下 21041 / 电商 54980 / 餐厅 930 万）| 种子写入时换算为元；JS 层再格式化为万 |
| `fact_channel_daily_sales` 无合计行（469 行验证为 0）、无 `region` 列 | channel 侧只需截断未来日期 |

**spec §13 三个开放点的落定（写进实现，不留悬空）：**

1. `dim_target` 落库 = 独立小迁移 `mart-ops-dim-target-v1` + `load-target` 子命令整体重放种子。
2. 看板 Nacos 组名 = 常量 `BI`，dataId = `{dashboard_id}.yaml`。**不走** `PUBLIC_DATA_NACOS_GROUP`（那是 PIPELINES 组专用；compose 的 `x-nacos-env` 把它固定为 `PIPELINES`）。
3. 门控粒度 = 请求级 + 30s TTL 缓存（线程锁保护）——spec §5.1 括注的「enabled=false 启动即退出」被 §9/§13 的请求级语义取代：长驻进程不退出，`/d/`、`/api/` 全 503、`/healthz` 仍 200。

---

## File structure

| Path | Responsibility | 状态 |
|---|---|---|
| `common/public_data/target_seed.py` | `TargetRow` + `load_target_seed(path)`（校验）+ `replace_dim_target(conn, rows)`（事务内 DELETE+INSERT 整体重放） | 新增 |
| `docker/integration/target.seed.json` | 版本受控年度目标种子（`_说明`/`version`/`targets`，值已换算为元） | 新增 |
| `common/public_data/live_migrations.py` | 追加 `("mart-ops-dim-target-v1", "mart", _build_mart_dim_target_ddl())`（append-only，不动既有版本） | 修改 |
| `common/public_data/cli.py` | 新增惰性包装 `load_target_seed` / `replace_dim_target` / `publish_bi_seed`；新增子命令 `load-target` / `publish-bi` | 修改 |
| `requirements.txt` | 追加 fastapi==0.141.1、uvicorn==0.52.4、jinja2==3.1.6、httpx==0.28.1 | 修改 |
| `common/bi_web/__init__.py` | 空包标记 | 新增 |
| `common/bi_web/config.py` | `DashboardConfig`/`CardPlacement` 模型 + `parse_dashboard_config`（严格校验）+ `DashboardConfigSource` 契约及 Static/File/Nacos 三后端 + `build_dashboard_config_source(environ)` + `publish_dashboards`/`publish_bi_seed_from_env` + `load_seed` | 新增 |
| `common/bi_web/cards.py` | `Card(card_id, chart, run, params_schema)` + `KNOWN_CHARTS` + 五卡 `REGISTRY` + `validate_dashboard_config(dashboard, registry)` | 新增 |
| `common/bi_web/queries.py` | 五口径查询（合计排除 + CURDATE 截断 + 年度目标）→ 图表就绪 dict | 新增 |
| `common/bi_web/app.py` | `create_app(...)` 工厂 + Bearer 认证 + 请求级门控（TTL）+ 路由 `/`、`/d/{id}`、`/api/d/{id}/cards/{card_id}`、`/healthz` + `validate_seed_file(path, registry)`（启动校验）+ `__main__` 内嵌 uvicorn | 新增 |
| `common/bi_web/templates/base.html` | 壳模板（ECharts CDN + 静态资源引用） | 新增 |
| `common/bi_web/templates/dashboard.html` | 12 栅格卡片占位（`data-api` 指向卡片端点） | 新增 |
| `common/bi_web/static/style.css` | 栅格/卡片/进度条样式 | 新增 |
| `common/bi_web/static/dashboard.js` | 拉卡片端点 → 按 `payload.chart` 渲染（scalar/line/bar）→ 按 `refresh_seconds` 轮询 | 新增 |
| `docker/integration/bi.seed.yaml` | 看板编排种子（spec §5.2 原文：l1-cockpit 五卡，span 4/4/4/8/4） | 新增 |
| `docker-compose.integration.yml` | `bi-web` 服务（profile `bi-web`，回环端口，零挂载零旗标） | 修改 |
| `docker/integration/pipelines.seed.yaml` | 注册 `bi-web`（kind=business，reads=[mart_ops]） | 修改 |
| `tests/common/test_public_data_target_seed.py` | 种子解析 / 重放 / CLI 3 组测试 | 新增 |
| `tests/common/test_bi_web_config.py` | 模型校验 / 三后端 / publish | 新增 |
| `tests/common/test_bi_web_cards.py` | 注册表完整性 / 看板校验 | 新增 |
| `tests/common/test_bi_web_queries.py` | SQL 形状单测（fake cursor）+ 真库集成测试（`INTEGRATION_TEST_RUNNER=1`） | 新增 |
| `tests/common/test_bi_web_app.py` | 路由 / 认证 / 门控 / 启动校验（TestClient + fakes）+ 真库全栈冒烟 | 新增 |
| `tests/test_integration_environment.py` | `bi-web` Compose 合约测试 | 修改 |

---

## Task 1: `dim_target` 维度 + 种子重放命令

- [ ] **Step 1: 写失败测试（`tests/common/test_public_data_target_seed.py`）。**
  - `load_target_seed`：合法文件（临时 JSON）→ 3 个 `TargetRow`（`annual_target` 为 `Decimal`）；文件缺失 / 顶层非 mapping / 缺 `version` / `targets` 非列表 → `TargetSeedError`；行缺 `scope`/`scope_key`/`year`/`annual_target` → 错；`year` 越界（2019、2101）→ 错；`annual_target` 为负 → 错；`(scope, scope_key, year)` 重复 → 错；`_说明` 键允许且忽略（种子惯例）。
  - `replace_dim_target`：fake connection（记录 execute 调用）断言：事务内先 `DELETE FROM dim_target`，再参数化 `INSERT`（值绝不拼进 SQL 文本），返回行数。
  - CLI（`LoadTargetCliTests`，照抄 `ExtractMartCliTests` 的 `_patch_common` 模式，patch `require_extract_run`）：缺 `--confirm-local-test-write` → 非零退出且 `load_settings` 未被调用；正常路径（patch `load_target_seed` 返回行、`replace_dim_target` 返回 3，patch `connect`）→ 输出含 `targets_written=3` 与 `status=completed`；异常 → `status=failed`，无 traceback。
- [ ] **Step 2: 跑测试确认失败。** `python -m unittest tests.common.test_public_data_target_seed -v` → ImportError/AttributeError。
- [ ] **Step 3: DDL + 迁移。** `live_migrations.py` 追加：

  ```python
  _DIM_TARGET_DDL = (
      "CREATE TABLE IF NOT EXISTS `dim_target` (\n"
      "  `scope` VARCHAR(32) NOT NULL,\n"
      "  `scope_key` VARCHAR(64) NOT NULL,\n"
      "  `year` SMALLINT UNSIGNED NOT NULL,\n"
      "  `annual_target` DECIMAL(20,4) NOT NULL,\n"
      "  `note` VARCHAR(255) NULL,\n"
      "  PRIMARY KEY (`scope`, `scope_key`, `year`)\n"
      ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
  )

  def _build_mart_dim_target_ddl() -> tuple[str, ...]:
      return (_DIM_TARGET_DDL,)
  ```

  `_MIGRATIONS` 末尾追加 `("mart-ops-dim-target-v1", "mart", _build_mart_dim_target_ddl())`——append-only，既有 `test_public_data_live_migrations.py` 断言保持通过。
- [ ] **Step 4: 种子文件 `docker/integration/target.seed.json`。**

  ```json
  {
    "_说明": "版本受控的年度目标种子。annual_target 单位为元，由万元口径换算：线下 21041 万 / 电商 54980 万 / 餐厅 930 万。改目标 = 改本文件 + 重跑 load-target（整体重放）。scope=line 对应业务线；餐厅首批无事实表、不计入进度卡分母。",
    "version": 1,
    "targets": [
      {"scope": "line", "scope_key": "offline", "year": 2026, "annual_target": 210410000, "note": "线下 21041 万"},
      {"scope": "line", "scope_key": "channel", "year": 2026, "annual_target": 549800000, "note": "电商 54980 万"},
      {"scope": "line", "scope_key": "restaurant", "year": 2026, "annual_target": 9300000, "note": "餐厅 930 万（无事实表，首批不计入进度卡）"}
    ]
  }
  ```

  测试直接读仓库内文件断言三行换算正确（210410000 / 549800000 / 9300000），把「万元→元」换算固化为回归。
- [ ] **Step 5: `common/public_data/target_seed.py`。** `TargetSeedError(ValueError)`（消息不含文件内容）；`@dataclass(frozen=True) TargetRow(scope, scope_key, year, annual_target: Decimal, note="")`；`load_target_seed(path)`；`replace_dim_target(connection, rows)` 用 `common.public_data.db.transaction` 包 DELETE + executemany INSERT。
- [ ] **Step 6: CLI 子命令 `load-target`。** 惰性包装 + handler（结构照 `_handle_migrate`：连三库 `apply_live_migrations` 保证表存在，然后 mart 连接上 `replace_dim_target`；无管线门控——同 migrate 先例；安全门 `require_extract_run`）。子参数：`--seed`（默认 `docker/integration/target.seed.json`）、`--confirm-local-test-write`。注册进 handlers dict。
- [ ] **Step 7: 跑 Task 1 全部测试至绿。** `python -m unittest tests.common.test_public_data_target_seed tests.common.test_public_data_live_migrations -v`。

## Task 2: `bi_web.config` —— 看板模型 + `BI` 组 ConfigSource

- [ ] **Step 1: 写失败测试（`tests/common/test_bi_web_config.py`）。**
  - `parse_dashboard_config`：合法 mapping（spec §5.2 的 l1-cockpit 原文）→ `DashboardConfig`（cards 为 tuple，span/title 正确）；`None` → 内置最小默认（`enabled=True`、`cards=()`）；顶层/dashboards 条目非 mapping → `DashboardConfigError`；`span` 为 0/13/"4" → 错；`enabled` 非 bool → 错；`refresh_seconds` 为 0/86401/非 int → 错；`cards` 元素缺 `card` → 错；**未知键 → 错**（严格解析：编排在 Nacos，宽松解析会把 `car:` 拼写错误静默变成缺卡片）；`_说明` 键忽略。
  - `FileDashboardSource`：临时 YAML 文件读取；坏 YAML → 错。
  - `NacosDashboardSource`：注入 fake client —— 返回合法 YAML 文本 → 解析成功；抛异常 → 走 fallback；返回空 → fallback → 无 fallback 时最小默认。
  - `build_dashboard_config_source(environ)`：有 `PUBLIC_DATA_NACOS_SERVER` → Nacos 后端（fallback 为 seed 文件）；仅 `PUBLIC_DATA_BI_SEED` → File；都没有 → Static 空。**断言 Nacos 后端的 group 是 `BI` 而非 `PUBLIC_DATA_NACOS_GROUP` 的值**（compose 把它固定为 PIPELINES）。
  - `publish_dashboards`：fake client，`if_missing=True` 时已存在的 dataId 跳过、计数正确；dataId 为 `{dashboard_id}.yaml`。
- [ ] **Step 2: 跑测试确认失败。**
- [ ] **Step 3: 实现 `common/bi_web/config.py`。** 镜像 `pipeline_config.py` 的形态：

  ```python
  BI_GROUP = "BI"

  @dataclass(frozen=True)
  class CardPlacement:
      card: str
      title: str = ""
      span: int = 4

  @dataclass(frozen=True)
  class DashboardConfig:
      dashboard_id: str
      title: str = ""
      enabled: bool = True
      refresh_seconds: int = 300
      cards: tuple = ()

  def parse_dashboard_config(dashboard_id, data) -> DashboardConfig: ...

  class DashboardConfigSource:            # get_dashboard(dashboard_id)
  class StaticDashboardSource(...)
  class FileDashboardSource(...)          # 读 bi.seed.yaml
  class NacosDashboardSource(...)         # group=BI_GROUP, dataId=f"{id}.yaml", fallback, client 可注入
  def build_dashboard_config_source(environ=None): ...
  def load_seed(path) -> dict: ...
  def publish_dashboards(client, mapping, group=BI_GROUP, if_missing=False) -> int: ...
  def publish_bi_seed_from_env(seed_path, if_missing=False, environ=None) -> int: ...
  ```

  关键差异（相对 pipeline_config）：group 用常量 `BI_GROUP`，不读 `PUBLIC_DATA_NACOS_GROUP`；解析严格拒绝未知键；`PUBLIC_DATA_BI_SEED` 由 `build_dashboard_config_source` 直接读 env（与 `PUBLIC_DATA_PIPELINE_SEED` 先例一致——**不进 `Settings`**，不重复 calendar/org/region 的种子路径模式）。
- [ ] **Step 4: CLI `publish-bi`。** `cli.py` 惰性包装 `publish_bi_seed` + `_handle_publish_bi`（结构逐字照 `_handle_publish_pipelines`：`--seed` 默认 `docker/integration/bi.seed.yaml`、`--if-missing`，输出 `published=<n> dashboards`）。测试：调用包装、计数输出、异常 → `status=failed code=config_error`。
- [ ] **Step 5: `requirements.txt` 追加四行并本地安装。** `fastapi==0.141.1` / `uvicorn==0.52.4` / `jinja2==3.1.6` / `httpx==0.28.1`；`python -m pip install -r requirements.txt`（host 与镜像共用此文件，Dockerfile 已 `pip install -r`）。
- [ ] **Step 6: 跑 Task 2 测试至绿。**

## Task 3: `cards.py` 注册表 + `queries.py` 五口径

- [ ] **Step 1: 写失败测试（`tests/common/test_bi_web_cards.py`）。** 注册表恰好含 5 个 id（`kpi_offline_mtd`/`kpi_channel_mtd`/`kpi_annual_progress`/`trend_region_daily`/`bar_channel_mtd`）；每卡 `chart ∈ KNOWN_CHARTS`、`params_schema == {}`（首批无 URL 参数）；`validate_dashboard_config`：引用未知 card → `CardConfigError`；合法看板（五卡）→ 通过。
- [ ] **Step 2: 写失败测试（`tests/common/test_bi_web_queries.py` 单测部分，fake cursor 捕获 SQL 文本）。** 断言每条查询的 SQL 文本同时含 `NOT LIKE '%合计%'`（offline 侧三查询）、`business_date <= CURDATE()`、`business_date >=`（当月起 or 年初起）；channel 侧两查询含截断、**不含** 合计过滤；`annual_target` 查询限定 `scope = 'line' AND scope_key IN ('offline','channel') AND year = YEAR(CURDATE())`；SQL 全静态（无 `%` 格式化拼接用户值）。
- [ ] **Step 3: 实现 `common/bi_web/queries.py`。** 低层函数（测试直测）+ 卡片 run 函数：

  ```python
  def offline_mtd_total(conn) -> Decimal:
      "SELECT COALESCE(SUM(sales_amount), 0) FROM fact_daily_report_offline " \
      "WHERE business_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01') " \
      "AND business_date <= CURDATE() AND responsible_person NOT LIKE '%合计%'"

  def offline_annual_total(conn) -> Decimal:      # 同上，起点改 MAKEDATE(YEAR(CURDATE()), 1)
  def channel_mtd_total(conn) -> Decimal:
      "SELECT COALESCE(SUM(sales_amount), 0) FROM fact_channel_daily_sales " \
      "WHERE business_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01') " \
      "AND business_date <= CURDATE()"

  def channel_annual_total(conn) -> Decimal:     # 同上，起点改 MAKEDATE(YEAR(CURDATE()), 1)
  def annual_target_total(conn) -> Decimal:      # dim_target 两线合计（760,210,000 元量级）

  def region_daily_series(conn) -> dict:         # GROUP BY business_date, region；Python 侧对齐日期并补零
  def channel_mtd_ranking(conn) -> dict:         # GROUP BY channel ORDER BY total DESC

  def run_kpi_offline_mtd(conn, params) -> dict:      # {"chart": "scalar", "value": float, "unit": "元"}
  def run_kpi_channel_mtd(conn, params) -> dict:      # 同上
  def run_kpi_annual_progress(conn, params) -> dict:  # {"chart": "scalar", "value", "target", "rate", "unit": "元"}
  def run_trend_region_daily(conn, params) -> dict:   # {"chart": "line", "dates": ["09-01", ...], "series": [{"name": "杭州", "data": [...]}, ...]}
  def run_bar_channel_mtd(conn, params) -> dict:      # {"chart": "bar", "categories": [...], "values": [...], "unit": "元"}
  ```

  **⑫口径落定**：`kpi_annual_progress` 分子 = 线下 + 电商**两线年累计**（同合计排除 + 截断规则），分母 = `dim_target` 中 `offline` + `channel` 两行 `annual_target` 之和（21041 + 54980 = 76021 万 → 760,210,000 元）。餐厅 930 万无事实表、不入首批（种子 note 已记录）。`rate = value / target`（target 为 0 时 `rate = None`）。
- [ ] **Step 4: 实现 `common/bi_web/cards.py`。**

  ```python
  KNOWN_CHARTS = ("scalar", "line", "bar", "table")

  @dataclass(frozen=True)
  class Card:
      card_id: str
      chart: str                 # scalar | line | bar | table
      run: Callable              # (connection, params) -> chart 就绪 dict
      params_schema: dict        # 允许的 URL 参数白名单（首批全部为空 dict）

  class CardConfigError(ValueError):
      """看板引用了注册表中不存在的 card_id。"""

  REGISTRY = {card.card_id: card for card in _CARDS}   # 模块加载即校验 chart ∈ KNOWN_CHARTS，违者 import 失败

  def validate_dashboard_config(dashboard, registry) -> None:   # 未知 card_id → raise CardConfigError
  ```

- [ ] **Step 5: 跑单测至绿。**
- [ ] **Step 6: 真库集成测试（同文件，`@unittest.skipUnless(os.environ.get("INTEGRATION_TEST_RUNNER") == "1", ...)`）。** `Settings.from_environment()` → 三连接 `apply_live_migrations`（幂等）→ mart 连接上：
  - **差值法**（不与既有真实数据耦合）：`q0 = offline_mtd_total()` → 插入 fixture（`source_record_id` 前缀 `biweb-test:`、`region='biweb甲'`：正常行 100 + 200，合计行 999999（`responsible_person='biweb甲合计'`），未来行 888888（`business_date = 明天`））→ `q1 = offline_mtd_total()` → 断言 `q1 - q0 == 300`（合计行与未来行被正确排除）。
  - `region_daily_series`：fixture 两区域各两天已知值 → 两序列对应点相等、dates 升序、序列长度对齐（补零生效）。
  - `bar_channel_mtd`：插入 `channel='biweb测试渠道'` 值 123 → categories 含之、values 非升序（降序）。
  - `replace_dim_target`（仓库种子）→ `annual_target_total() == Decimal("760210000")`；`run_kpi_annual_progress` 的 `rate == value / target`（分子用差值法复核）。
  - **teardown 清理**：`DELETE ... WHERE source_record_id LIKE 'biweb-test:%' OR channel = 'biweb测试渠道' OR region LIKE 'biweb%'`，dim_target 恢复为仓库种子重放。
- [ ] **Step 7: 容器内跑集成测试至绿。** `docker compose -f docker-compose.integration.yml run --rm test-runner python -m unittest tests.common.test_bi_web_queries -v`。

## Task 4: `app.py` FastAPI 装配

- [ ] **Step 1: 写失败测试（`tests/common/test_bi_web_app.py`，`fastapi.testclient.TestClient` + 全 fake 注入）。** `create_app` 签名：

  ```python
  def create_app(*, settings, dashboard_source, registry=REGISTRY,
                 token=None, gate=None, db_connector=None, seed_path=None) -> FastAPI
  ```

  测试矩阵（fake dashboard source 用 dict 构造 `StaticDashboardSource`；fake registry 用真 `Card` + `run=lambda conn, params: {...}`；fake `db_connector` 返回 yield Mock 连接（`SELECT 1` → 一行）；fake `gate=lambda: True/False`）：
  - `GET /healthz` → 200 `{"status": "ok", "database": "ok"}`；连接抛错 → 503 `{"status": "unhealthy"}`（detail 无主机名/异常文本）。
  - `GET /` → 307 重定向 `/d/l1-cockpit`。
  - `GET /d/l1-cockpit`（五卡静态源）→ 200，正文含 5 个 `data-api="/api/d/l1-cockpit/cards/..."` 占位与 `data-refresh-seconds="300"`。
  - `GET /api/d/l1-cockpit/cards/kpi_offline_mtd` → 200，JSON 即 run 返回值，响应头 `Cache-Control: no-store`；卡片不在该页 → 404；未知 query 参数（`?foo=1`，schema 为空）→ 400。
  - 看板缺失 → 404；`enabled=false` → 404；定义损坏（source 抛 `DashboardConfigError`/`CardConfigError`）→ 503（只影响该页）。
  - 认证：`token="t"` → 无/错 Authorization → 401（`/d/`、`/api/` 均然）；正确 Bearer → 200；`/healthz` 永不 401；`token=None`/空 → 全开放。
  - 门控：`gate=lambda: False` → `/d/`、`/api/` 全 503，`/healthz` 仍 200；`gate=None`（默认）→ 构造 `_TTLGate`（fake check 计数：30s 内两次请求只查一次配置源；check 抛异常 → 视为 enabled，fail-open）。
  - 默认 `db_connector`：断言只以 `settings.mart_database` 建连（**raw 库零引用的代码级把守**）。
  - `seed_path` 指向含未知 card 的种子文件 → `create_app` 抛错（启动即暴露配置漂移）。
- [ ] **Step 2: 跑测试确认失败。**
- [ ] **Step 3: 实现 `common/bi_web/app.py`。** 要点：
  - 模块 docstring 同 `cli.py` 纪律：不打印凭据/payload/URL/traceback；HTTPException detail 一律泛化（`"unauthorized"`/`"unavailable"`/`"card_error"`）。
  - `_TTLGate`：`threading.Lock` + `time.monotonic`，`ttl_seconds=30.0`；默认 check = `build_config_source().get_pipeline(resolve_service_id() or "").enabled`（`from common.public_data.pipeline_config import build_config_source`——注意模块实际导出的是这个名字，不是 `build_pipeline_config_source`），异常 → True（fail-open，与 `cli._pipeline_enabled` 同语义）；`service_id` 在构造时解析一次。
  - 认证依赖：`Header(default=None)` 的 `authorization`，`token` 非空且不等于 `f"Bearer {token}"` → 401；只挂在 `/d/`、`/api/` 路由的 `dependencies`。
  - `/d/{dashboard_id}`：门控 → 404（缺失/禁用）→ 503（解析/校验损坏）→ `Jinja2Templates(directory=Path(__file__).parent / "templates")` 渲染 `dashboard.html`（传 `dashboard` 上下文）。
  - `/api/d/{dashboard_id}/cards/{card_id}`：同前置链 → placement 查找 → query 参数白名单校验（不在 `params_schema` 的键 → 400）→ `with db_connector():` 内 `card.run(conn, params)` → `JSONResponse(..., headers={"Cache-Control": "no-store"})`；run 异常 → 500 `"card_error"`。
  - `/healthz`：`SELECT 1`，无门控无认证。
  - `app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"))`。
  - `seed_path` 传入即调 `validate_seed_file(seed_path, registry)`（`config.load_seed` + `parse` + `cards.validate`）。
  - `main()`（`__main__`）：`Settings.from_environment()` → `token = os.environ.get("BI_WEB_TOKEN") or None` → `build_dashboard_config_source()` → `seed_path = os.environ.get("PUBLIC_DATA_BI_SEED") or None` → `uvicorn.run(app, host="0.0.0.0", port=8080)`。启动校验失败 → 打印一行安全消息 + `sys.exit(1)`。
- [ ] **Step 4: 跑 Task 4 测试至绿。**

## Task 5: 模板 + 静态资源 + `bi.seed.yaml`

- [ ] **Step 1: `docker/integration/bi.seed.yaml`**（spec §5.2 原文 + 一行注释说明「SQL 在代码，这里只管编排」）：

  ```yaml
  # 看板编排种子：卡片口径在 common/bi_web/cards.py（代码评审）；本文件只回答
  # 「哪个页面摆哪些卡、什么标题、什么布局、是否启用」。发布进 Nacos group=BI。
  l1-cockpit:
    title: "首页驾驶舱"
    enabled: true
    refresh_seconds: 300
    cards:
      - card: kpi_offline_mtd
        title: "线下本月累计销售"
        span: 4
      - card: kpi_channel_mtd
        title: "电商渠道本月累计销售"
        span: 4
      - card: kpi_annual_progress
        title: "年度目标达成进度"
        span: 4
      - card: trend_region_daily
        title: "区域日销趋势"
        span: 8
      - card: bar_channel_mtd
        title: "渠道本月排行"
        span: 4
  ```

  测试（`test_bi_web_config.py` 或独立 case）直接读仓库文件：解析成功、五卡 id 全在 `REGISTRY`、span 序列 `[4, 4, 4, 8, 4]`——把「种子与注册表不漂移」固化为回归（同 calendar.seed.json 先例）。
- [ ] **Step 2: `templates/base.html` + `templates/dashboard.html`。** base：`<html lang="zh-CN">`、`/static/style.css`、ECharts CDN `<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js">`（浏览器侧拉取，服务端不出网）、`/static/dashboard.js`。dashboard：`<main class="dashboard" data-refresh-seconds="{{ dashboard.refresh_seconds }}">` + 12 栅格 `.card.span-N` 占位，每卡 `data-card="{{ placement.card }}"`、`data-api="/api/d/{{ dashboard.dashboard_id }}/cards/{{ placement.card }}"`、`<h2>{{ placement.title }}</h2>` + 空 `.card-body`。
- [ ] **Step 3: `static/style.css` + `static/dashboard.js`。** CSS：12 列 grid（`.span-4` 1/3、`.span-8` 2/3）、卡片白底圆角、`.kpi-value` 大字号、`.progress/.progress-bar`。JS（原生，无框架）：遍历 `.card` → `fetch(dataset.api, {cache: "no-store"})` → 按 `payload.chart` 渲染——`scalar` 无 `target` → 大数字（`formatWan`：`(v/10000).toLocaleString("zh-CN") + " 万"`）；有 `target` → 数字 + 目标 + 进度条（`min(100, round(rate*100))%`）；`line`/`bar` → `echarts.init` + setOption（line：日期类目轴 + 多系列；bar：横向类目轴降序）。失败 → `.error`「加载失败」。按 `data-refresh-seconds`（缺省 300）`setInterval` 轮询。
- [ ] **Step 4: 跑全部 bi-web 测试至绿**（渲染断言在 Task 4 的 `/d/` 测试里，模板就位后通过）。

## Task 6: compose + 注册 + 合约测试 + 全栈冒烟

- [ ] **Step 1: 写失败合约测试（`tests/test_integration_environment.py`）。** `test_bi_web_is_opt_in_and_read_only`（沿用 `_compose_config("bi-web")` 模式，`@skipIf(INTEGRATION_TEST_RUNNER)`）：`profiles == ["bi-web"]`（opt-in）；`depends_on.mysql.condition == "service_healthy"`；**无 `volumes`**（零挂载）；command 为 `-m common.bi_web.app` 且**不含** `--live`；`ports == ["127.0.0.1:18080:8080"]`（回环）；`environment.PUBLIC_DATA_CONFIG` 指向 `public-data-test-config.json`（非 live manifest）；无 `INTEGRATION_TEST_RUNNER`（bi-web 零写库，不触发任何写门控）。**对 spec §9 的刻意偏离（记录在案）**：spec 要求合约测试同时断言「无 raw 库环境变量引用」，但 `Settings.from_environment` 强制要求全部 9 个环境变量（含 `PUBLIC_DATA_DINGTALK_DATABASE` / `PUBLIC_DATA_WDT_DATABASE`），compose 不带它们进程无法启动——故本断言不做；raw 库零引用的真正把守是 Task 4 的「默认 `db_connector` 只以 `settings.mart_database` 建连」代码级断言。
- [ ] **Step 2: compose 服务。** 追加到 `docker-compose.integration.yml`（robot 先例，env 块同构；`x-nacos-env` 锚点带入 Nacos 连接与 `PUBLIC_DATA_PIPELINE_SEED`）：

  ```yaml
    # 业务线 bi-web（阶段 A）：只读 mart_ops 的 L1 驾驶舱。零凭据、零挂载、
    # 零 --live-*；注册表请求级门控（Nacos 关 bi-web 即全站 503）。
    # 端口仅回环，与 mysql 13306 同约定。
    bi-web:
      profiles:
        - bi-web
      build:
        context: .
        dockerfile: docker/integration/Dockerfile
      entrypoint: ["python3"]
      depends_on:
        mysql:
          condition: service_healthy
      command:
        - -m
        - common.bi_web.app
      environment:
        <<: *nacos-env
        APP_ENV: test
        PUBLIC_DATA_RDS_HOST: mysql
        PUBLIC_DATA_RDS_PORT: "3306"
        PUBLIC_DATA_RDS_USER: public_data_test
        PUBLIC_DATA_RDS_PASSWORD: public-data-test-password
        PUBLIC_DATA_DINGTALK_DATABASE: raw_dingtalk_test
        PUBLIC_DATA_WDT_DATABASE: raw_wdt_test
        PUBLIC_DATA_MART_DATABASE: mart_ops_test
        PUBLIC_DATA_CONFIG: /app/docker/integration/public-data-test-config.json
        PUBLIC_DATA_SERVICE_ID: bi-web
        PUBLIC_DATA_BI_SEED: /app/docker/integration/bi.seed.yaml
        BI_WEB_TOKEN: ${BI_WEB_TOKEN:-}
        TZ: Asia/Shanghai
      ports:
        - "127.0.0.1:18080:8080"
  ```

- [ ] **Step 3: 注册。** `pipelines.seed.yaml` 追加：

  ```yaml
  bi-web:
    kind: business
    enabled: true
    reads: [mart_ops]
    description: "BI dashboards (L1/L2), read-only on mart_ops"
  ```

- [ ] **Step 4: 全栈集成冒烟（`tests/common/test_bi_web_app.py` 的 `INTEGRATION_TEST_RUNNER` 部分）。** `Settings.from_environment()` + `apply_live_migrations` + `FileDashboardSource("docker/integration/bi.seed.yaml")` + 真 `REGISTRY` + `TestClient`：`/healthz` 200 且 `database=ok`；`/d/l1-cockpit` 含 5 个 `data-api` 占位；`GET /api/d/l1-cockpit/cards/kpi_offline_mtd`（及另四卡）→ 200、`chart` 字段正确、`Cache-Control: no-store`（数值可能为 0——只断言结构与类型）。
- [ ] **Step 5: 跑测试至绿 + 全量回归。** `python -m unittest discover -s tests -t .`（本地）；容器内同命令（`docker compose run --rm test-runner`）。
- [ ] **Step 6: 手工冒烟（本地 Docker）。**
  1. `docker compose -f docker-compose.integration.yml --profile bi-web up -d --build bi-web`（mysql 已在跑；首启前先 `--profile extract run --rm extract-mart` 保证 mart 有数）。
  2. `docker compose run --rm test-runner python -m common.public_data.cli load-target --confirm-local-test-write` 落 dim_target。
  3. `curl -s http://127.0.0.1:18080/healthz` → 200。
  4. `curl -s http://127.0.0.1:18080/d/l1-cockpit | grep -c data-api` → 5。
  5. `curl -s "http://127.0.0.1:18080/api/d/l1-cockpit/cards/kpi_offline_mtd"` → JSON（数值 ≈ 当月线下真实合计，与 Metabase 试点同口径可比对）。
  6. 浏览器开 `http://127.0.0.1:18080/`：五卡渲染、进度条/折线/条形可见、300s 轮询。

---

## Verification

- **单测全量**：`python -m unittest discover -s tests -t .` → 既有 454 通过 / 13 跳过 **+ 新增约 70–80 例全绿**。
- **容器全量**：`docker compose -f docker-compose.integration.yml up --build --abort-on-container-exit test-runner`。
- **Compose 合约**：`docker compose -f docker-compose.integration.yml --profile bi-web config` 确认 bi-web 只有 `bi-web` profile、无 bind mount、回环端口。
- **合约红线**（一条都不能破）：bi-web 无凭据挂载 / 无 `--live-*` / 无外呼 / 只读 mart_ops / `/api/` 带 `no-store` / 错误响应无 SQL 与 traceback。
- **口径对拍**：`kpi_offline_mtd` 与 Metabase 试点看板（`http://127.0.0.1:3000/dashboard/2`）同月线下合计一致（合计排除 + 截断后）；`kpi_annual_progress.rate` = 两线年累计 ÷ 760,210,000。
- **运维新命令**：`load-target`（种子重放）、`publish-bi`（编排发布进 Nacos group=BI，`--if-missing` 幂等）。
