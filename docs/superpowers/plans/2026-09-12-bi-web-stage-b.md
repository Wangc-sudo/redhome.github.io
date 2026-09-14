# bi-web 阶段 B 实施计划：L2 三页 + L1 日环比卡

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在零管线/零 compose/零新表的前提下交付阶段 B：3 个 L2 分析页（l2-region / l2-channel / l2-people）+ L1 增补 ⑭ 日环比两卡，引入配置驱动筛选器、bar 点击下钻、跨页导航、table 图表类型与 scalar 载荷扩展。

**Architecture:** 沿用阶段 A 的「SQL 在代码、编排在 Nacos」分层：`queries.py` 扩展参数化口径查询（`%s` 占位符 + params 元组，值先经 app 层维表校验），`cards.py` 注册表扩到 16 卡，`config.py` 增加 `filters`/`nav_order`/`on_click` 三个配置字段与 `dashboard_ids()` 枚举契约，`app.py` 页面路由渲染筛选下拉与导航并对参数值做第二道闸门，前端仍是 Jinja2 SSR + 原生 JS + ECharts CDN（无框架、无构建）。

**Tech Stack:** Python 3 stdlib unittest（非 pytest）、FastAPI/TestClient、pymysql、Jinja2、原生 JS、ECharts 5.5.1 CDN、Docker Compose 集成环境（MySQL 127.0.0.1:13306）。

**Spec:** `docs/superpowers/specs/2026-09-12-bi-web-stage-b-design.md`（口径与验收的唯一来源）

---

## 全局约定（每个任务都适用）

- **测试命令**（仓库根 `E:\repos\digital-ops`）：
  - 宿主机：`python -m unittest tests.common.test_bi_web_X -v`（X = config / queries / cards / app）
  - 日志降噪：命令后接 `| grep -E "^(Ran|OK|FAILED|ERROR)"`
  - 容器（集成层，需 MySQL 已在跑，不得重启/拆除栈）：先 `docker compose -f docker-compose.integration.yml build test-runner`（Dockerfile `COPY . /app`，源码变了必须重建），再 `docker compose -f docker-compose.integration.yml run --rm --entrypoint python3 test-runner -m unittest tests.common.test_bi_web_X -v`
- **TDD 铁律**：每个任务先写 RED 测试、跑出预期失败、再写 GREEN 实现、跑到全绿。失败原因必须是「功能缺失」，不是拼写错误。
- **不提交**：沿用阶段 A 惯例，本计划所有任务**不含 git 提交步骤**；工作树保持在 HEAD `ebd831c` 之上未提交状态，等用户明确指示。计划文件本身也不提交。
- **改动面纪律**：不碰 docker-compose.integration.yml、不碰合约测试、不新增任何文件（全部为修改既有文件）、不读取/展示任何凭证类文件。
- **金额单位**：SQL 层一律元（Decimal），run_* 载荷转 float，前端按「万」格式化。
- **测试隔离注意**：Task 6 引入的 `_PEOPLE_CACHE` 是模块级状态——人员榜相关测试类的 `setUp` 必须清缓存（计划内已写明），否则跨测试串数据。

## File Structure（本计划零新文件，全部修改）

| 文件 | 职责改动 |
|---|---|
| `common/bi_web/config.py` | +`FilterSpec` 数据类；`DashboardConfig.nav_order`/`filters`；`CardPlacement.on_click`；严格解析与跨字段校验（on_click 必须落在页面 filters 内）；`DashboardConfigSource.dashboard_ids()` 枚举契约 |
| `common/bi_web/queries.py` | +`_fetch_rows`/`_fetch_one`/`_fetch_scalar(params)`；三个维表下拉查询；DoD 两卡；l2-region 三卡；l2-channel 四卡；l2-people 四卡（mart_collect 复用 + 60s 快照缓存）；模块 docstring 改口（阶段 B 引入参数化 SQL） |
| `common/bi_web/cards.py` | +`_param_card` 构造器；11 张新卡；trend_region_daily / bar_channel_mtd 两张复用卡扩 params_schema → REGISTRY 16 卡 |
| `common/bi_web/app.py` | +导航条目构建 `_navigation_entries`；页面路由查维表渲染筛选下拉（失败 503）；卡片 API 第二道值闸门（非法 region/channel/month → 400）；向模板传 `card_params` |
| `common/bi_web/templates/base.html` | +顶部导航块（enabled 看板按 nav_order 升序，当前页高亮） |
| `common/bi_web/templates/dashboard.html` | +筛选器表单块；卡片 `data-params`（该卡接受的 URL 参数白名单）与 `data-onclick-param` 属性 |
| `common/bi_web/static/dashboard.js` | 整文件重写（保持阶段 A 全部行为）：table 渲染分支、DoD scalar 扩展（环比色 + 迷你趋势）、单位感知格式化、筛选变更 replaceState + 带参重拉（参数交集）、bar 点击下钻 |
| `common/bi_web/static/style.css` | +导航/筛选/table/升绿降红/迷你趋势样式（文件末尾追加） |
| `docker/integration/bi.seed.yaml` | L1 顶部加 kpi_offline_dod / kpi_channel_dod（span 6+6）；新增 l2-region / l2-channel / l2-people 三条目（filters + nav_order + on_click） |
| `tests/common/test_bi_web_config.py` | +阶段 B 解析测试、`dashboard_ids()` 枚举测试；BiSeedFileTests 断言改四页面新形态 |
| `tests/common/test_bi_web_queries.py` | +FakeConnection 的 months 伪表键；维表/DoD/L2 单测；集成层插入语句扩列（department/monthly_target/store_name/promotion_cost）+ 口径对拍新测试 |
| `tests/common/test_bi_web_cards.py` | 注册表断言 5 → 16；参数白名单断言重划 |
| `tests/common/test_bi_web_app.py` | +页面筛选/导航/值闸门单测；集成层 l1 卡数 5 → 7、+L2 页面冒烟 |

**任务依赖顺序**：Task 1（config）→ Task 2（维表查询 + 取数助手）→ Task 3/4/5/6（四组口径卡，互相独立但都依赖 2）→ Task 7（注册表）→ Task 8（app）→ Task 9（前端）→ Task 10（seed）→ Task 11（集成测试）→ Task 12（浏览器冒烟）。

---

### Task 1: config.py 阶段 B 配置字段 + dashboard_ids() 枚举

**Files:**
- Modify: `common/bi_web/config.py`
- Test: `tests/common/test_bi_web_config.py`

- [ ] **Step 1: 写 RED 测试**

在 `tests/common/test_bi_web_config.py` 中：导入区补充 `DashboardConfigSource`（加进现有 `from common.bi_web.config import (...)` 的名字列表），然后在 `ParseDashboardConfigTests` 类之后、`StaticDashboardSourceTests` 类之前插入两个新测试类：

```python
class StageBParseTests(unittest.TestCase):
    """阶段 B 字段：nav_order / filters / on_click（与既有字段同一套严格规则）。"""

    def test_defaults_nav_order_zero_and_no_filters(self):
        config = parse_dashboard_config("l2-region", {"title": "区域下钻"})

        self.assertEqual(0, config.nav_order)
        self.assertEqual((), config.filters)

    def test_parses_nav_order(self):
        config = parse_dashboard_config("l2-region", {"nav_order": 10})

        self.assertEqual(10, config.nav_order)

    def test_parses_filters_into_filter_spec_tuple(self):
        data = {
            "filters": [
                {"param": "region", "source": "regions", "label": "区域"},
                {"param": "month", "source": "months"},
            ]
        }

        config = parse_dashboard_config("l2-region", data)

        self.assertEqual(2, len(config.filters))
        first, second = config.filters
        self.assertEqual("region", first.param)
        self.assertEqual("regions", first.source)
        self.assertEqual("区域", first.label)
        self.assertEqual("", second.label)

    def test_parses_on_click_param(self):
        data = {
            "filters": [{"param": "channel", "source": "channels"}],
            "cards": [{"card": "bar_channel_mtd", "on_click": {"param": "channel"}}],
        }

        config = parse_dashboard_config("l2-channel", data)

        self.assertEqual("channel", config.cards[0].on_click)

    def test_rejects_invalid_nav_order(self):
        for nav_order in (-1, "10", True, 10.5, None):
            with self.subTest(nav_order=nav_order):
                with self.assertRaises(DashboardConfigError):
                    parse_dashboard_config("x", {"nav_order": nav_order})

    def test_rejects_filters_that_are_not_a_list(self):
        for filters in ({"param": "region"}, "regions", 42):
            with self.subTest(filters=filters):
                with self.assertRaises(DashboardConfigError):
                    parse_dashboard_config("x", {"filters": filters})

    def test_rejects_filter_element_that_is_not_a_mapping(self):
        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", {"filters": ["region"]})

    def test_rejects_filter_without_param_or_source(self):
        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", {"filters": [{"source": "regions"}]})
        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", {"filters": [{"param": "region"}]})

    def test_rejects_unknown_filter_source(self):
        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config(
                "x", {"filters": [{"param": "region", "source": "regionz"}]}
            )

    def test_rejects_unknown_filter_key(self):
        data = {"filters": [{"param": "region", "source": "regions", "lable": "区域"}]}

        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", data)

    def test_rejects_non_string_filter_label(self):
        data = {"filters": [{"param": "region", "source": "regions", "label": 42}]}

        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", data)

    def test_rejects_duplicate_filter_param_on_one_page(self):
        data = {
            "filters": [
                {"param": "region", "source": "regions"},
                {"param": "region", "source": "regions"},
            ]
        }

        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", data)

    def test_rejects_on_click_param_missing_from_page_filters(self):
        data = {
            "filters": [{"param": "month", "source": "months"}],
            "cards": [{"card": "bar_channel_mtd", "on_click": {"param": "channel"}}],
        }

        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("l2-channel", data)

    def test_rejects_on_click_that_is_not_a_mapping(self):
        data = {
            "filters": [{"param": "channel", "source": "channels"}],
            "cards": [{"card": "bar_channel_mtd", "on_click": "channel"}],
        }

        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", data)

    def test_rejects_on_click_without_param_key(self):
        data = {
            "filters": [{"param": "channel", "source": "channels"}],
            "cards": [{"card": "bar_channel_mtd", "on_click": {}}],
        }

        with self.assertRaises(DashboardConfigError):
            parse_dashboard_config("x", data)

    def test_underscore_note_keys_stay_allowed_in_new_fields(self):
        data = {
            "nav_order": 10,
            "filters": [{"param": "region", "source": "regions", "_说明": "x"}],
            "cards": [{"card": "bar_channel_mtd", "on_click": {"param": "region", "_说明": "x"}}],
        }

        config = parse_dashboard_config("x", data)

        self.assertEqual(10, config.nav_order)
        self.assertEqual("region", config.cards[0].on_click)

    def test_error_messages_do_not_leak_field_values(self):
        with self.assertRaises(DashboardConfigError) as ctx:
            parse_dashboard_config(
                "x", {"filters": [{"param": "region", "source": "SECRET-LEAK-CHECK"}]}
            )
        self.assertNotIn("SECRET-LEAK-CHECK", str(ctx.exception))


class DashboardIdsTests(_SeedFileTestCase):
    """阶段 B 导航枚举：dashboard_ids() 各后端行为。"""

    def test_static_source_lists_its_ids_sorted(self):
        source = StaticDashboardSource({"l2-region": {}, "l1-cockpit": {}})

        self.assertEqual(("l1-cockpit", "l2-region"), source.dashboard_ids())

    def test_file_source_delegates_to_the_seed_mapping(self):
        path = self._write_seed("l1-cockpit:\n  title: 首页驾驶舱\n")

        source = FileDashboardSource(path)

        self.assertEqual(("l1-cockpit",), source.dashboard_ids())

    def test_nacos_source_delegates_to_the_fallback(self):
        fallback = StaticDashboardSource({"l1-cockpit": {}})
        source = NacosDashboardSource(
            server="nacos:8848", client=_FakeNacosClient(), fallback=fallback
        )

        self.assertEqual(("l1-cockpit",), source.dashboard_ids())

    def test_nacos_source_without_fallback_lists_nothing(self):
        source = NacosDashboardSource(server="nacos:8848", client=_FakeNacosClient())

        self.assertEqual((), source.dashboard_ids())

    def test_base_contract_defaults_to_no_ids(self):
        class _Bare(DashboardConfigSource):
            def get_dashboard(self, dashboard_id):
                return None

        self.assertEqual((), _Bare().dashboard_ids())
```

- [ ] **Step 2: 跑 RED**

```
python -m unittest tests.common.test_bi_web_config -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`FAILED (errors=...)`——`parse_dashboard_config` 不认识 `nav_order`（unknown key 报错）且没有 `dashboard_ids`（AttributeError）。注意 `test_parses_nav_order` 之外的用例多数以 DashboardConfigError 失败方式为「意外抛错/意外通过」混合；关键是**新字段相关断言失败而既有测试不挂**。

- [ ] **Step 3: 写 GREEN 实现**

`common/bi_web/config.py` 修改四处。

（1）常量区（`MAX_SPAN = 12` 之后）：

```python
DEFAULT_NAV_ORDER = 0

_ALLOWED_DASHBOARD_KEYS = frozenset(
    {"title", "enabled", "refresh_seconds", "cards", "nav_order", "filters", "_说明"}
)
_ALLOWED_CARD_KEYS = frozenset({"card", "title", "span", "on_click", "_说明"})
_ALLOWED_FILTER_KEYS = frozenset({"param", "source", "label", "_说明"})
_ALLOWED_ON_CLICK_KEYS = frozenset({"param", "_说明"})

#: 筛选器 ``source`` 可指向的维表查询名；与 ``app._FILTER_SOURCE_QUERIES``
#: 的键集合由测试对拍保持一致（漂移=红构建，而非运行期 KeyError）。
KNOWN_FILTER_SOURCES = frozenset({"regions", "channels", "months"})
```

（2）数据类区（`CardPlacement` 之前插入 `FilterSpec`，并扩两个字段）：

```python
@dataclass(frozen=True)
class FilterSpec:
    """One dropdown filter: *param* is the URL query key, *source* names the
    dimension query that fills the dropdown."""

    param: str
    source: str
    label: str = ""


@dataclass(frozen=True)
class CardPlacement:
    """One card placed on a dashboard grid (``span`` is 1..12 columns)."""

    card: str
    title: str = ""
    span: int = DEFAULT_SPAN
    on_click: str | None = None


@dataclass(frozen=True)
class DashboardConfig:
    """Resolved configuration for a single dashboard.

    There is deliberately no ``to_mapping``: the version-controlled seed is
    the source of truth and is published verbatim (see
    :func:`publish_dashboards`).
    """

    dashboard_id: str
    title: str = ""
    enabled: bool = True
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS
    nav_order: int = DEFAULT_NAV_ORDER
    cards: tuple = ()
    filters: tuple = ()
```

（3）`parse_dashboard_config`：在 `refresh_seconds` 校验之后、`cards = data.get(...)` 之前插入 `nav_order` 解析；在 `return DashboardConfig(...)` 之前插入 filters 解析与跨字段校验，并改构造调用：

```python
    nav_order = data.get("nav_order", DEFAULT_NAV_ORDER)
    if isinstance(nav_order, bool) or not isinstance(nav_order, int):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' field 'nav_order' must be an integer"
        )
    if nav_order < 0:
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' field 'nav_order' must be >= 0"
        )
```

```python
    filters = data.get("filters", [])
    if not isinstance(filters, list):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' field 'filters' must be a list"
        )
    parsed_filters = tuple(
        _parse_filter(dashboard_id, index, raw)
        for index, raw in enumerate(filters)
    )
    _reject_duplicate_filter_params(dashboard_id, parsed_filters)
    _reject_unknown_on_click_params(dashboard_id, parsed_cards, parsed_filters)

    return DashboardConfig(
        dashboard_id=dashboard_id,
        title=title,
        enabled=enabled,
        refresh_seconds=refresh_seconds,
        nav_order=nav_order,
        cards=parsed_cards,
        filters=parsed_filters,
    )
```

（注意：原 `cards=tuple(_parse_card(...) ...)` 内联表达式提为局部变量 `parsed_cards = tuple(_parse_card(dashboard_id, index, raw) for index, raw in enumerate(cards))`，位置保持在 `cards` 列表校验之后。）

（4）`_parse_card` 末尾的 `return CardPlacement(...)` 之前插入 on_click 解析，并改返回值；模块尾部（`_parse_card` 之后）新增三个函数：

```python
    on_click = raw.get("on_click")
    on_click_param = None
    if on_click is not None:
        if not isinstance(on_click, dict):
            raise DashboardConfigError(
                f"dashboard '{dashboard_id}' cards[{index}] field 'on_click' "
                "must be a mapping"
            )
        if any(key not in _ALLOWED_ON_CLICK_KEYS for key in on_click):
            raise DashboardConfigError(
                f"dashboard '{dashboard_id}' cards[{index}] field 'on_click' "
                "has an unknown key"
            )
        on_click_param = on_click.get("param")
        if not isinstance(on_click_param, str) or not on_click_param:
            raise DashboardConfigError(
                f"dashboard '{dashboard_id}' cards[{index}] field 'on_click' "
                "needs a non-empty 'param'"
            )

    return CardPlacement(card=card, title=title, span=span, on_click=on_click_param)
```

```python
def _parse_filter(dashboard_id, index, raw):
    if not isinstance(raw, dict):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' filters[{index}] must be a mapping"
        )
    if any(key not in _ALLOWED_FILTER_KEYS for key in raw):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' filters[{index}] has an unknown key"
        )
    param = raw.get("param")
    if not isinstance(param, str) or not param:
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' filters[{index}] needs a non-empty 'param'"
        )
    source = raw.get("source")
    if not isinstance(source, str) or not source:
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' filters[{index}] needs a non-empty 'source'"
        )
    if source not in KNOWN_FILTER_SOURCES:
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' filters[{index}] has an unknown source"
        )
    label = raw.get("label", "")
    if not isinstance(label, str):
        raise DashboardConfigError(
            f"dashboard '{dashboard_id}' filters[{index}] field 'label' "
            "must be a string"
        )
    return FilterSpec(param=param, source=source, label=label)


def _reject_duplicate_filter_params(dashboard_id, filters):
    seen = set()
    for spec in filters:
        if spec.param in seen:
            raise DashboardConfigError(
                f"dashboard '{dashboard_id}' declares a filter param twice"
            )
        seen.add(spec.param)


def _reject_unknown_on_click_params(dashboard_id, cards, filters):
    """on_click 的参数必须出现在同页 filters 中（spec §5，启动即报错）。"""
    filter_params = {spec.param for spec in filters}
    for placement in cards:
        if placement.on_click is not None and placement.on_click not in filter_params:
            raise DashboardConfigError(
                f"dashboard '{dashboard_id}' cards on_click param "
                "is not a page filter"
            )
```

（5）枚举契约——`DashboardConfigSource` 与三个后端各加一个方法：

```python
class DashboardConfigSource:
    """Contract: resolve a dashboard's config by id."""

    def get_dashboard(self, dashboard_id):  # pragma: no cover - interface
        raise NotImplementedError

    def dashboard_ids(self):
        """Ids this source can enumerate for the top navigation.

        基类返回空（不可枚举）；具体后端覆写。app 层把枚举异常当作
        「无导航」处理（fail-open），而不是让整页 5xx。
        """
        return ()
```

`StaticDashboardSource` 加：

```python
    def dashboard_ids(self):
        return tuple(sorted(self._by_id))
```

`FileDashboardSource` 加：

```python
    def dashboard_ids(self):
        return self._delegate.dashboard_ids()
```

`NacosDashboardSource` 加：

```python
    def dashboard_ids(self):
        if self._fallback is not None:
            return self._fallback.dashboard_ids()
        return ()
```

- [ ] **Step 4: 跑 GREEN**

```
python -m unittest tests.common.test_bi_web_config -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`OK`（既有全部测试 + 新增两组全过；Nacos 枚举走 fallback 与既有 fail-open 语义一致）。

---

### Task 2: queries.py 取数助手 + 三个筛选器维表查询

**Files:**
- Modify: `common/bi_web/queries.py`
- Test: `tests/common/test_bi_web_queries.py`

- [ ] **Step 1: 写 RED 测试**

`tests/common/test_bi_web_queries.py`：

（1）导入区把 `region_options, channel_options, month_options` 加进 `from common.bi_web.queries import (...)`。

（2）`FakeConnection._TABLES` 之后、`_table_of` 方法改为（先认 UNION 伪表键，再按表名子串）：

```python
    _TABLES = (
        "fact_daily_report_offline",
        "fact_channel_daily_sales",
        "dim_target",
        "months",  # 伪表键：month_options 的 UNION 查询横跨两张事实表
    )

    def _table_of(self, sql):
        if "UNION" in sql:
            return "months"
        for table in self._TABLES:
            if table in sql:
                return table
        return None
```

（3）在 `ChannelMtdRankingTests` 类之后插入新测试类：

```python
class DimensionOptionsTests(unittest.TestCase):
    """筛选器维表查询（spec §4）：regions / channels / months。"""

    def test_region_options_sql_shape_and_values(self):
        connection = FakeConnection(
            rowsets={"fact_daily_report_offline": [
                {"region": "杭州"}, {"region": "绍兴"},
            ]}
        )

        options = region_options(connection)

        self.assertEqual(["杭州", "绍兴"], options)
        sql, parameters = connection.executed[0]
        self.assertIn("SELECT DISTINCT region FROM fact_daily_report_offline", sql)
        self.assertIn("ORDER BY region", sql)
        self.assertIsNone(parameters)

    def test_channel_options_sql_shape_and_values(self):
        connection = FakeConnection(
            rowsets={"fact_channel_daily_sales": [{"channel": "天猫"}]}
        )

        options = channel_options(connection)

        self.assertEqual(["天猫"], options)
        sql, parameters = connection.executed[0]
        self.assertIn("SELECT DISTINCT channel FROM fact_channel_daily_sales", sql)
        self.assertIn("ORDER BY channel", sql)
        self.assertIsNone(parameters)

    def test_month_options_union_both_fact_tables_plus_current_month_desc(self):
        connection = FakeConnection(
            rowsets={"months": [{"month": "2026-09"}, {"month": "2026-08"}]}
        )

        options = month_options(connection)

        self.assertEqual(["2026-09", "2026-08"], options)
        sql, parameters = connection.executed[0]
        self.assertIn("FROM fact_daily_report_offline", sql)
        self.assertIn("FROM fact_channel_daily_sales", sql)
        self.assertIn("CURDATE()", sql)
        self.assertIn("ORDER BY 1 DESC", sql)
        self.assertIsNone(parameters)
```

- [ ] **Step 2: 跑 RED**

```
python -m unittest tests.common.test_bi_web_queries -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`FAILED (errors=...)`——ImportError（`region_options` 等不存在）。

- [ ] **Step 3: 写 GREEN 实现**

`common/bi_web/queries.py`：

（1）模块 docstring 的第四段（"All SQL is fully static..."）替换为（改口阶段 B 参数化查询）：

```
Stage-A SQL is fully static; stage B adds parameterized queries (the
month / region / channel windowed lookups) that bind values as ``%s``
placeholders with a params tuple -- a value is never spliced into the SQL
text, and URL values are validated against the dimension tables upstream
in the app layer.  Money unit is 元.  The low-level queries return
:class:`~decimal.Decimal`; the ``run_*`` card functions convert to
``float`` for JSON payloads.
```

（2）`_fetch_scalar` 之后新增两个取数助手，并把 `_fetch_scalar` 改为接受可选参数（`cursor.execute(sql, params)`，静态调用点不传即 `None`，FakeCursor/`SqlShapeTestCase.sole_static_sql` 的 `parameters is None` 断言不受影响）：

```python
def _fetch_scalar(connection, sql, params=None) -> Decimal:
    """Run one total query and return its single value as Decimal."""
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    finally:
        cursor.close()
    if not row:
        return Decimal(0)
    value = next(iter(row.values()))
    return Decimal(0) if value is None else Decimal(value)


def _fetch_one(connection, sql, params=None):
    """Run one query and return its single raw value (may be a date/None)."""
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    finally:
        cursor.close()
    if not row:
        return None
    return next(iter(row.values()))


def _fetch_rows(connection, sql, params=None):
    """Run one row query and return its rows as list of dicts."""
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    finally:
        cursor.close()
    return [dict(row) for row in rows]
```

（3）`_CHANNEL_RANKING_SQL` 之后新增三条维表 SQL 常量与三个函数：

```python
_REGION_OPTIONS_SQL = (
    "SELECT DISTINCT region FROM fact_daily_report_offline ORDER BY region"
)

_CHANNEL_OPTIONS_SQL = (
    "SELECT DISTINCT channel FROM fact_channel_daily_sales ORDER BY channel"
)

_MONTH_OPTIONS_SQL = (
    "SELECT DISTINCT DATE_FORMAT(business_date, '%Y-%m') AS month "
    "FROM fact_daily_report_offline "
    "UNION "
    "SELECT DISTINCT DATE_FORMAT(business_date, '%Y-%m') "
    "FROM fact_channel_daily_sales "
    "UNION "
    "SELECT DATE_FORMAT(CURDATE(), '%Y-%m') "
    "ORDER BY 1 DESC"
)


def region_options(connection) -> list:
    """筛选器「区域」下拉：DISTINCT region，升序（SQL 全静态）。"""
    return [row["region"] for row in _fetch_rows(connection, _REGION_OPTIONS_SQL)
            if row["region"]]


def channel_options(connection) -> list:
    """筛选器「渠道」下拉：DISTINCT channel，升序（SQL 全静态）。"""
    return [row["channel"] for row in _fetch_rows(connection, _CHANNEL_OPTIONS_SQL)
            if row["channel"]]


def month_options(connection) -> list:
    """筛选器「月份」下拉：两事实表 DISTINCT 月 ∪ 当前月，降序。

    只出现有数据的月份（数据积累后自动出新选项）；当前月恒在列，
    无数据也允许选择（空结果而非 400，spec §8）。
    """
    return [row["month"] for row in _fetch_rows(connection, _MONTH_OPTIONS_SQL)]
```

- [ ] **Step 4: 跑 GREEN**

```
python -m unittest tests.common.test_bi_web_queries -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`OK`（StaticSqlTests 只迭代阶段 A 的 7 个静态函数，不受影响）。

### Task 3: l2-region 取数（kpi_region_mtd / trend 参数化 / bar_department_mtd）

**Files:**
- Modify: `common/bi_web/queries.py`
- Test: `tests/common/test_bi_web_queries.py`

**口径要点（本任务锁定的三件事）：**

1. **pymysql `%%` 转义**：驱动是 pymysql（`common/public_data/db.py` 用 `pymysql.connect`）。`cursor.execute(sql, params)` 在 params 非 None 时做 `sql % params` 的 Python 格式化（`Cursor.mogrify`），因此**带参 SQL 里的字面 `%` 必须双写成 `%%`**——`NOT LIKE '%%合计%%'` 格式化后 MySQL 实际收到 `NOT LIKE '%合计%'`。静态 SQL（params=None）不做格式化，保持单 `%`（阶段 A 全部如此，不动）。此约定适用于阶段 B 所有带参且含字面 `%` 的 SQL（Task 4/5 同）。单测的 FakeCursor 只记录不格式化，**漏写 `%%` 只有集成测试会炸**——所以单测里用 `_PARAM_SUMMARY_EXCLUSION` 锚点把双写也锁死。
2. **月参数用 BETWEEN 区间而非 DATE_FORMAT**：`month_bounds("YYYY-MM")` 借 `common.calendar_utils.month_days` 算 (月首, 月末)，SQL 用 `business_date BETWEEN %s AND %s`。避开 `DATE_FORMAT(business_date, '%Y-%m') = %s`（字面 `%` 又要转义一层，且包函数不可走 business_date 索引）。当前月截断仍靠 `business_date <= CURDATE()`。
3. **月目标查询刻意不做 CURDATE 截断**：与 `summarize_people` 同口径——月目标是月级常量，melt 后每行重复携带，预填未来行不影响 `MAX(monthly_target)`；取数（Σsales）截断、目标（ΣMAX）不截断，不对称是有意的。

**L1 静态路径保留**：`run_trend_region_daily` 无参时仍走阶段 A 的静态 `region_daily_series`（`StaticSqlTests` 与既有 `RunPayloadTests` 零改动）；带参才走新的参数化路径。

- [ ] **Step 1: 写 RED 测试**

`tests/common/test_bi_web_queries.py` 三处改动：

（1）锚点区（`_SUMMARY_EXCLUSION` 之后）加带参版锚点：

```python
#: The parametrized-query form -- pymysql formats ``sql % params`` when
#: parameters are passed, so every literal ``%`` must be doubled there.
_PARAM_SUMMARY_EXCLUSION = "responsible_person NOT LIKE '%%合计%%'"
```

（2）import 块（Task 2 已加入 `channel_options` / `month_options` / `region_options` 的基础上）补齐为：

```python
from common.bi_web.queries import (
    annual_target_total,
    channel_annual_total,
    channel_mtd_ranking,
    channel_mtd_total,
    channel_options,
    department_mtd_ranking,
    month_bounds,
    month_options,
    offline_annual_total,
    offline_mtd_total,
    region_daily_series,
    region_month_target,
    region_mtd_total,
    region_options,
    run_bar_channel_mtd,
    run_bar_department_mtd,
    run_kpi_annual_progress,
    run_kpi_channel_mtd,
    run_kpi_offline_mtd,
    run_kpi_region_mtd,
    run_trend_region_daily,
)
```

（3）`RunPayloadTests` 类之后、`# Integration layer` 注释块之前，插入四个新测试类：

```python
class ParameterizedSqlShapeTests(unittest.TestCase):
    """阶段 B 参数化查询共用断言：恰好一条语句 + 参数元组逐位相等。"""

    def sole_parameterized_sql(self, connection, expected_params):
        self.assertEqual(1, len(connection.executed))
        sql, parameters = connection.executed[0]
        self.assertEqual(tuple(expected_params), parameters)
        return sql


class RegionParamTests(ParameterizedSqlShapeTests):
    """l2-region 取数：region 过滤 / 月区间 / 合计排除（%% 双写）。"""

    def test_region_mtd_total_with_region(self):
        connection = FakeConnection(scalars={"fact_daily_report_offline": Decimal("1")})

        region_mtd_total(
            connection,
            region="杭州",
            first_day=date(2026, 9, 1),
            last_day=date(2026, 9, 30),
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30), "杭州")
        )
        self.assertIn("FROM fact_daily_report_offline", sql)
        self.assertIn("business_date BETWEEN %s AND %s", sql)
        self.assertIn("region = %s", sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, sql)

    def test_region_mtd_total_without_region_omits_region_filter(self):
        connection = FakeConnection(scalars={"fact_daily_report_offline": Decimal("1")})

        region_mtd_total(
            connection, first_day=date(2026, 9, 1), last_day=date(2026, 9, 30)
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30))
        )
        self.assertNotIn("region = %s", sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, sql)

    def test_region_mtd_total_returns_zero_when_no_rows(self):
        connection = FakeConnection()

        value = region_mtd_total(
            connection, first_day=date(2026, 9, 1), last_day=date(2026, 9, 30)
        )

        self.assertEqual(Decimal(0), value)

    def test_region_month_target_sums_per_person_max(self):
        connection = FakeConnection(scalars={"fact_daily_report_offline": Decimal("1")})

        region_month_target(
            connection,
            region="杭州",
            first_day=date(2026, 9, 1),
            last_day=date(2026, 9, 30),
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30), "杭州")
        )
        self.assertIn("SELECT COALESCE(SUM(mx), 0)", sql)
        self.assertIn("MAX(monthly_target)", sql)
        self.assertIn("GROUP BY responsible_person", sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, sql)
        # 月目标是月级属性：与 summarize_people 同口径取全月行 MAX，
        # 刻意不做 CURDATE 截断（预填未来行不影响 MAX）。
        self.assertNotIn(_TRUNCATION, sql)

    def test_department_mtd_ranking_sql_shape_and_null_department(self):
        connection = FakeConnection(
            rowsets={"fact_daily_report_offline": [
                {"department": "零售一组", "total": Decimal("30")},
                {"department": None, "total": Decimal("10")},
            ]}
        )

        ranking = department_mtd_ranking(
            connection,
            region="杭州",
            first_day=date(2026, 9, 1),
            last_day=date(2026, 9, 30),
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30), "杭州")
        )
        self.assertIn("GROUP BY department", sql)
        self.assertIn("ORDER BY total DESC", sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, sql)
        self.assertEqual(["零售一组", "未分组"], ranking["categories"])
        self.assertEqual([Decimal("30"), Decimal("10")], ranking["values"])


class MonthBoundsTests(unittest.TestCase):
    def test_month_bounds_expands_to_month_ends(self):
        self.assertEqual(
            (date(2026, 9, 1), date(2026, 9, 30)), month_bounds("2026-09")
        )

    def test_month_bounds_handles_february_leap_year(self):
        self.assertEqual(
            (date(2024, 2, 1), date(2024, 2, 29)), month_bounds("2024-02")
        )


class _RegionFakeConnection(FakeConnection):
    """kpi_region_mtd 两条查询同表不同聚合：按 SQL 内容区分 value/target。"""

    def scripted_fetchone(self, sql):
        if "MAX(monthly_target)" in sql:
            value = self._scalars.get("region_target")
            return {"total": Decimal("0") if value is None else value}
        return super().scripted_fetchone(sql)


class RegionRunPayloadTests(unittest.TestCase):
    def test_run_kpi_region_mtd_payload(self):
        connection = _RegionFakeConnection(
            scalars={
                "fact_daily_report_offline": Decimal("12"),
                "region_target": Decimal("40"),
            }
        )

        payload = run_kpi_region_mtd(connection, {"region": "杭州", "month": "2026-09"})

        self.assertEqual(
            {
                "chart": "scalar",
                "value": 12.0,
                "target": 40.0,
                "rate": 12.0 / 40.0,
                "unit": "元",
            },
            payload,
        )
        sql, parameters = connection.executed[0]
        self.assertIn("SUM(sales_amount)", sql)
        self.assertIn("region = %s", sql)
        self.assertEqual((date(2026, 9, 1), date(2026, 9, 30), "杭州"), parameters)

    def test_run_kpi_region_mtd_defaults_month_to_current(self):
        connection = _RegionFakeConnection(
            scalars={
                "fact_daily_report_offline": Decimal("12"),
                "region_target": Decimal("0"),
            }
        )

        payload = run_kpi_region_mtd(connection, {"region": "杭州"})

        self.assertIsNone(payload["rate"])
        sql, parameters = connection.executed[0]
        first_day, last_day, region = parameters
        self.assertEqual("杭州", region)
        self.assertEqual(date.today().replace(day=1), first_day)
        self.assertEqual(first_day, last_day.replace(day=1))

    def test_run_trend_region_daily_with_region_single_series(self):
        rows = [
            {"business_date": date(2026, 9, 2), "region": "杭州", "total": Decimal("5")},
            {"business_date": date(2026, 9, 3), "region": "杭州", "total": Decimal("6")},
        ]
        connection = FakeConnection(rowsets={"fact_daily_report_offline": rows})

        payload = run_trend_region_daily(
            connection, {"region": "杭州", "month": "2026-09"}
        )

        self.assertEqual(
            {
                "chart": "line",
                "dates": ["09-02", "09-03"],
                "series": [{"name": "杭州", "data": [5.0, 6.0]}],
            },
            payload,
        )
        sql, parameters = connection.executed[0]
        self.assertIn("region = %s", sql)
        self.assertIn("GROUP BY business_date, region", sql)
        self.assertEqual((date(2026, 9, 1), date(2026, 9, 30), "杭州"), parameters)

    def test_run_trend_region_daily_month_without_region_two_series(self):
        rows = [
            {"business_date": date(2026, 8, 2), "region": "杭州", "total": Decimal("5")},
            {"business_date": date(2026, 8, 2), "region": "绍兴", "total": Decimal("3")},
        ]
        connection = FakeConnection(rowsets={"fact_daily_report_offline": rows})

        payload = run_trend_region_daily(connection, {"month": "2026-08"})

        self.assertEqual(["08-02"], payload["dates"])
        self.assertEqual(
            [
                {"name": "杭州", "data": [5.0]},
                {"name": "绍兴", "data": [3.0]},
            ],
            payload["series"],
        )
        sql, parameters = connection.executed[0]
        self.assertNotIn("region = %s", sql)
        self.assertEqual((date(2026, 8, 1), date(2026, 8, 31)), parameters)

    def test_run_trend_region_daily_without_params_keeps_static_l1_path(self):
        connection = FakeConnection(rowsets={"fact_daily_report_offline": []})

        run_trend_region_daily(connection, {})

        self.assertEqual(1, len(connection.executed))
        sql, parameters = connection.executed[0]
        self.assertIsNone(parameters)
        self.assertIn(_MONTH_START, sql)

    def test_run_bar_department_mtd_payload(self):
        rows = [{"department": "零售一组", "total": Decimal("30")}]
        connection = FakeConnection(rowsets={"fact_daily_report_offline": rows})

        payload = run_bar_department_mtd(
            connection, {"region": "杭州", "month": "2026-08"}
        )

        self.assertEqual(
            {
                "chart": "bar",
                "categories": ["零售一组"],
                "values": [30.0],
                "unit": "元",
            },
            payload,
        )
        sql, parameters = connection.executed[0]
        self.assertIn("GROUP BY department", sql)
        self.assertEqual((date(2026, 8, 1), date(2026, 8, 31), "杭州"), parameters)
```

- [ ] **Step 2: 跑 RED**

```
python -m unittest tests.common.test_bi_web_queries -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`FAILED (errors=...)`——ImportError（`department_mtd_ranking` 等不存在）。

- [ ] **Step 3: 写 GREEN 实现**

`common/bi_web/queries.py` 五处改动：

（1）顶部 import（stdlib 组之后加 common 依赖）：

```python
from datetime import datetime
from decimal import Decimal

from common.calendar_utils import month_days
```

（2）`_mmdd` 之后新增月界换算助手：

```python
def month_bounds(month):
    """'YYYY-MM' → (月首, 月末) 日期对（纯日历计算，复用 calendar_utils）。

    带参 SQL 用 ``business_date BETWEEN %s AND %s`` 而非
    ``DATE_FORMAT(business_date, '%Y-%m') = %s``：避开字面 ``%`` 的
    pymysql 双写（窗口小、两写一处即可），且区间谓词可走索引。
    """
    year, _, number = month.partition("-")
    days = month_days(int(year), int(number))
    return days[0], days[-1]
```

（3）`region_daily_series` 的函数体替换为（docstring 原文保留；对齐逻辑抽出共用）：

```python
def _align_region_rows(rows) -> dict:
    """按升序日期轴对齐各区域序列并零填充（region_daily_series 的核心）。"""
    dates = sorted({row["business_date"] for row in rows})
    index_of = {business_date: index for index, business_date in enumerate(dates)}
    by_region = {}
    for row in rows:
        region = row["region"] or ""
        index = index_of[row["business_date"]]
        by_region.setdefault(region, [Decimal(0)] * len(dates))[index] = Decimal(
            row["total"] or 0
        )
    return {
        "dates": [_mmdd(business_date) for business_date in dates],
        "series": [
            {"name": region, "data": by_region[region]} for region in sorted(by_region)
        ],
    }


def region_daily_series(connection) -> dict:
    return _align_region_rows(_fetch_rows(connection, _REGION_DAILY_SQL))
```

（4）`channel_mtd_ranking` 之后新增 l2-region 取数段：

```python
def _optional_region_filter(region):
    """可选 region 过滤：返回 (SQL 片段, 追加参数元组)；片段为空即全区域。"""
    if region:
        return "AND region = %s ", (region,)
    return "", ()


def region_mtd_total(connection, *, region=None, first_day, last_day) -> Decimal:
    """选中区域（缺省全区域）某自然月 Σsales（截断未来 + 排除合计行）。

    带参 SQL 的字面 ``%`` 双写（pymysql ``sql % params`` 约定）：
    ``'%%合计%%'`` 格式化后 MySQL 收到 ``'%合计%'``。
    """
    region_sql, region_params = _optional_region_filter(region)
    sql = (
        "SELECT COALESCE(SUM(sales_amount), 0) "
        "FROM fact_daily_report_offline "
        "WHERE business_date BETWEEN %s AND %s "
        f"{region_sql}"
        "AND business_date <= CURDATE() "
        "AND responsible_person NOT LIKE '%%合计%%'"
    )
    return _fetch_scalar(connection, sql, (first_day, last_day) + region_params)


def region_month_target(connection, *, region=None, first_day, last_day) -> Decimal:
    """区域月目标 = Σ各人员 MAX(monthly_target)（melt 陷阱：绝不跨行 SUM）。

    与 ``summarize_people`` 同口径取**全月行** MAX：月目标是月级常量，
    预填未来行不影响 MAX，故不做 CURDATE 截断（与取数查询刻意不对称）。
    """
    region_sql, region_params = _optional_region_filter(region)
    sql = (
        "SELECT COALESCE(SUM(mx), 0) "
        "FROM ("
        "SELECT responsible_person, MAX(monthly_target) AS mx "
        "FROM fact_daily_report_offline "
        "WHERE business_date BETWEEN %s AND %s "
        f"{region_sql}"
        "AND responsible_person NOT LIKE '%%合计%%' "
        "GROUP BY responsible_person"
        ") t"
    )
    return _fetch_scalar(connection, sql, (first_day, last_day) + region_params)


def department_mtd_ranking(connection, *, region=None, first_day, last_day) -> dict:
    """选中区域（缺省全区域）某自然月按部门 Σsales 降序；空部门→未分组。"""
    region_sql, region_params = _optional_region_filter(region)
    sql = (
        "SELECT department, SUM(sales_amount) AS total "
        "FROM fact_daily_report_offline "
        "WHERE business_date BETWEEN %s AND %s "
        f"{region_sql}"
        "AND business_date <= CURDATE() "
        "AND responsible_person NOT LIKE '%%合计%%' "
        "GROUP BY department "
        "ORDER BY total DESC"
    )
    rows = _fetch_rows(connection, sql, (first_day, last_day) + region_params)
    return {
        "categories": [row["department"] or "未分组" for row in rows],
        "values": [Decimal(row["total"] or 0) for row in rows],
    }


def region_month_daily_series(connection, *, region=None, first_day, last_day) -> dict:
    """参数化的区域日销序列（L2 用）；有 region 单系列、无则全区域系列。

    L1 无参路径仍走静态 ``region_daily_series``（StaticSqlTests 锁定）。
    """
    region_sql, region_params = _optional_region_filter(region)
    sql = (
        "SELECT business_date, region, SUM(sales_amount) AS total "
        "FROM fact_daily_report_offline "
        "WHERE business_date BETWEEN %s AND %s "
        f"{region_sql}"
        "AND business_date <= CURDATE() "
        "AND responsible_person NOT LIKE '%%合计%%' "
        "GROUP BY business_date, region"
    )
    rows = _fetch_rows(connection, sql, (first_day, last_day) + region_params)
    return _align_region_rows(rows)
```

（5）run 函数段：`run_trend_region_daily` 整体替换为（L1 无参走静态路径，带参走新路径）：

```python
def run_trend_region_daily(connection, params) -> dict:
    """区域日销趋势: L1 无参=当前月全区域（静态）；带参=region/month。"""
    region = params.get("region")
    month = params.get("month")
    if region is None and month is None:
        series = region_daily_series(connection)
    else:
        month = month or datetime.now().strftime("%Y-%m")
        first_day, last_day = month_bounds(month)
        series = region_month_daily_series(
            connection, region=region, first_day=first_day, last_day=last_day
        )
    return {
        "chart": "line",
        "dates": series["dates"],
        "series": [
            {"name": entry["name"], "data": [float(value) for value in entry["data"]]}
            for entry in series["series"]
        ],
    }
```

`run_bar_channel_mtd` 之后新增两个 run 函数：

```python
def run_kpi_region_mtd(connection, params) -> dict:
    """区域当月累计（模块 ① 区域下钻）: region/month 参数化 scalar + 进度条。

    value=当月 Σsales（截断+排合计）；target=Σ各人员 MAX(monthly_target)
    （melt 安全）；rate=value/target（target 0→None）。region 缺省全区域，
    month 缺省当前月（应用本地时钟；容器 TZ Asia/Shanghai）。
    """
    region = params.get("region")
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    first_day, last_day = month_bounds(month)
    value = region_mtd_total(
        connection, region=region, first_day=first_day, last_day=last_day
    )
    target = region_month_target(
        connection, region=region, first_day=first_day, last_day=last_day
    )
    rate = None if target == 0 else float(value) / float(target)
    return {
        "chart": "scalar",
        "value": float(value),
        "target": float(target),
        "rate": rate,
        "unit": _UNIT,
    }


def run_bar_department_mtd(connection, params) -> dict:
    """区域部门当月排行（模块 ① 区域下钻）: region/month bar 降序。"""
    region = params.get("region")
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    first_day, last_day = month_bounds(month)
    ranking = department_mtd_ranking(
        connection, region=region, first_day=first_day, last_day=last_day
    )
    return {
        "chart": "bar",
        "categories": ranking["categories"],
        "values": [float(value) for value in ranking["values"]],
        "unit": _UNIT,
    }
```

- [ ] **Step 4: 跑 GREEN**

```
python -m unittest tests.common.test_bi_web_queries -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`OK`——含阶段 A 全部既有用例（`region_daily_series` 重构行为不变、`RunPayloadTests` 的 `{}` 入参走静态路径）。

### Task 4: L1 ⑭ 日环比两卡（queries.py）

**Files:**
- Modify: `common/bi_web/queries.py`
- Test: `tests/common/test_bi_web_queries.py`

口径要点：

1. **两步查询定锚**：第一步 `MAX(business_date)`（`business_date <= CURDATE()`，线下侧带合计排除）取「表内最新数据日」；第二步 `BETWEEN %s AND %s` 窗口按日 `Σsales_amount`，两端日期由 Python 计算（`latest−6 … latest`）。绝不把窗口写成 `DATE_SUB` 之类的 SQL 函数——日期参数走绑定值。
2. **静态/参数化混合的 `%` 规则（本任务最易错点）**：定锚 SQL 全静态（params=None）→ 合计排除用单 `%`：`NOT LIKE '%合计%'`；窗口 SQL 参数化 → 必须 `NOT LIKE '%%合计%%'`。同一张卡的两条 SQL 写法不同，正是 pymysql 规则的体现——集成测试才是漏写 `%%` 的唯一捕获层，单测靠 `_PARAM_SUMMARY_EXCLUSION` 锚串锁住。
3. **环比基期=前一自然日**（`latest−1`，非前一数据日，spec §3.1/§7）；`prev` 缺失或为 0 → `delta_pct=None`；`trend7`=近 7 自然日逐日值，缺数日 `value=None`（前端断线）；空表 → 全空载荷（`value 0.0`，`date/prev/delta_pct None`，`trend7 []`）。

- [ ] **Step 1: 写 RED 测试**

`tests/common/test_bi_web_queries.py`——在 Task 3 新增的 `RegionRunPayloadTests` 之后、`# Integration layer` 注释之前插入；同时向 `from common.bi_web.queries import (` 块按字母序追加 4 个名字：`channel_dod`、`offline_dod`、`run_kpi_channel_dod`、`run_kpi_offline_dod`。

```python
class _DodFakeConnection(FakeConnection):
    """DoD 卡两条 SQL 读同一张表：按 SQL 内容分流。

    MAX(business_date) 定锚查询走 scalars（None=空表，返回 None 行），
    窗口 GROUP BY 查询照常走 rowsets。
    """

    def scripted_fetchone(self, sql):
        if "MAX(business_date)" in sql:
            value = self._scalars.get(self._table_of(sql))
            if value is None:
                return None
            return {"d": value}
        return super().scripted_fetchone(sql)


class DodShapeTests(unittest.TestCase):
    """定锚 SQL 全静态（单 %），窗口 SQL 参数化（双 %）。"""

    def dod_statements(self, connection, first_day, latest):
        self.assertEqual(2, len(connection.executed))
        latest_sql, latest_params = connection.executed[0]
        window_sql, window_params = connection.executed[1]
        self.assertIsNone(latest_params)
        self.assertEqual((first_day, latest), window_params)
        return latest_sql, window_sql

    def test_offline_dod_sql_shape(self):
        connection = _DodFakeConnection(
            scalars={"fact_daily_report_offline": date(2026, 9, 10)}
        )

        offline_dod(connection)

        latest_sql, window_sql = self.dod_statements(
            connection, date(2026, 9, 4), date(2026, 9, 10)
        )
        self.assertIn("MAX(business_date)", latest_sql)
        self.assertIn("FROM fact_daily_report_offline", latest_sql)
        self.assertIn(_TRUNCATION, latest_sql)
        self.assertIn(_SUMMARY_EXCLUSION, latest_sql)
        self.assertIn("FROM fact_daily_report_offline", window_sql)
        self.assertIn("BETWEEN %s AND %s", window_sql)
        self.assertIn(_PARAM_SUMMARY_EXCLUSION, window_sql)
        self.assertIn("GROUP BY business_date", window_sql)

    def test_channel_dod_sql_shape(self):
        connection = _DodFakeConnection(
            scalars={"fact_channel_daily_sales": date(2026, 9, 10)}
        )

        channel_dod(connection)

        latest_sql, window_sql = self.dod_statements(
            connection, date(2026, 9, 4), date(2026, 9, 10)
        )
        self.assertIn("MAX(business_date)", latest_sql)
        self.assertIn("FROM fact_channel_daily_sales", latest_sql)
        self.assertIn(_TRUNCATION, latest_sql)
        self.assertNotIn("合计", latest_sql)
        self.assertIn("FROM fact_channel_daily_sales", window_sql)
        self.assertIn("BETWEEN %s AND %s", window_sql)
        self.assertNotIn("合计", window_sql)
        self.assertIn("GROUP BY business_date", window_sql)


class OfflineDodTests(unittest.TestCase):
    """⑭ 线下：全载荷、prev 缺失、prev 为 0、空表。"""

    def connection_with(self, rows, latest=date(2026, 9, 10)):
        return _DodFakeConnection(
            scalars={"fact_daily_report_offline": latest},
            rowsets={"fact_daily_report_offline": rows},
        )

    def test_full_payload_with_delta_and_trend7(self):
        rows = [
            {"business_date": date(2026, 9, 8), "total": Decimal("80")},
            {"business_date": date(2026, 9, 9), "total": Decimal("100")},
            {"business_date": date(2026, 9, 10), "total": Decimal("120")},
        ]

        payload = run_kpi_offline_dod(self.connection_with(rows), {})

        self.assertEqual(
            {
                "chart": "scalar",
                "value": 120.0,
                "date": "2026-09-10",
                "prev": 100.0,
                "delta_pct": 0.2,
                "trend7": [
                    {"date": "2026-09-04", "value": None},
                    {"date": "2026-09-05", "value": None},
                    {"date": "2026-09-06", "value": None},
                    {"date": "2026-09-07", "value": None},
                    {"date": "2026-09-08", "value": 80.0},
                    {"date": "2026-09-09", "value": 100.0},
                    {"date": "2026-09-10", "value": 120.0},
                ],
                "unit": "元",
            },
            payload,
        )

    def test_prev_missing_yields_delta_none(self):
        rows = [{"business_date": date(2026, 9, 10), "total": Decimal("120")}]

        payload = run_kpi_offline_dod(self.connection_with(rows), {})

        self.assertEqual(120.0, payload["value"])
        self.assertIsNone(payload["prev"])
        self.assertIsNone(payload["delta_pct"])

    def test_prev_zero_yields_delta_none(self):
        rows = [
            {"business_date": date(2026, 9, 9), "total": Decimal("0")},
            {"business_date": date(2026, 9, 10), "total": Decimal("120")},
        ]

        payload = run_kpi_offline_dod(self.connection_with(rows), {})

        self.assertEqual(0.0, payload["prev"])
        self.assertIsNone(payload["delta_pct"])

    def test_empty_table_yields_empty_payload(self):
        connection = _DodFakeConnection(
            scalars={"fact_daily_report_offline": None}
        )

        payload = run_kpi_offline_dod(connection, {})

        self.assertEqual(
            {
                "chart": "scalar",
                "value": 0.0,
                "date": None,
                "prev": None,
                "delta_pct": None,
                "trend7": [],
                "unit": "元",
            },
            payload,
        )


class ChannelDodTests(unittest.TestCase):
    """⑭ 电商：同口径载荷 + 空表（渠道表无合计行）。"""

    def test_full_payload(self):
        connection = _DodFakeConnection(
            scalars={"fact_channel_daily_sales": date(2026, 9, 10)},
            rowsets={
                "fact_channel_daily_sales": [
                    {"business_date": date(2026, 9, 9), "total": Decimal("40")},
                    {"business_date": date(2026, 9, 10), "total": Decimal("60")},
                ]
            },
        )

        payload = run_kpi_channel_dod(connection, {})

        self.assertEqual(60.0, payload["value"])
        self.assertEqual("2026-09-10", payload["date"])
        self.assertEqual(40.0, payload["prev"])
        self.assertEqual(0.5, payload["delta_pct"])
        self.assertEqual(7, len(payload["trend7"]))
        self.assertEqual({"date": "2026-09-10", "value": 60.0}, payload["trend7"][-1])

    def test_empty_table_yields_empty_payload(self):
        connection = _DodFakeConnection(
            scalars={"fact_channel_daily_sales": None}
        )

        payload = run_kpi_channel_dod(connection, {})

        self.assertEqual(0.0, payload["value"])
        self.assertIsNone(payload["date"])
        self.assertEqual([], payload["trend7"])
```

- [ ] **Step 2: 跑 RED**

```
python -m unittest tests.common.test_bi_web_queries -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`FAILED (errors=...)`——ImportError（`channel_dod` 等不存在）。

- [ ] **Step 3: 写 GREEN 实现**

`common/bi_web/queries.py`：

（1）导入行 `from datetime import datetime` 改为：

```python
from datetime import datetime, timedelta
```

（2）在 Task 3 新增的 `region_month_daily_series` 之后插入四条 SQL 常量与三个低层函数：

```python
_OFFLINE_DOD_LATEST_SQL = (
    "SELECT MAX(business_date) AS d "
    "FROM fact_daily_report_offline "
    "WHERE business_date <= CURDATE() "
    "AND responsible_person NOT LIKE '%合计%'"
)

_CHANNEL_DOD_LATEST_SQL = (
    "SELECT MAX(business_date) AS d "
    "FROM fact_channel_daily_sales "
    "WHERE business_date <= CURDATE()"
)

_OFFLINE_DOD_WINDOW_SQL = (
    "SELECT business_date, SUM(sales_amount) AS total "
    "FROM fact_daily_report_offline "
    "WHERE business_date BETWEEN %s AND %s "
    "AND responsible_person NOT LIKE '%%合计%%' "
    "GROUP BY business_date"
)

_CHANNEL_DOD_WINDOW_SQL = (
    "SELECT business_date, SUM(sales_amount) AS total "
    "FROM fact_channel_daily_sales "
    "WHERE business_date BETWEEN %s AND %s "
    "GROUP BY business_date"
)


def _dod_totals(connection, sql, latest):
    """近 7 自然日窗口的按日 Σsales 映射（缺数日不在映射中）。"""
    first_day = latest - timedelta(days=6)
    rows = _fetch_rows(connection, sql, (first_day, latest))
    return {row["business_date"]: row["total"] for row in rows}


def _dod_core(totals, latest):
    """由按日 Σsales 映射组装环比核：value/prev/delta_pct/trend7。"""
    def _num(value):
        return None if value is None else float(value)

    value = _num(totals.get(latest))
    prev = _num(totals.get(latest - timedelta(days=1)))
    delta_pct = None
    if prev not in (None, 0) and value is not None:
        delta_pct = (value - prev) / prev
    trend7 = [
        {
            "date": (latest - timedelta(days=offset)).isoformat(),
            "value": _num(totals.get(latest - timedelta(days=offset))),
        }
        for offset in range(6, -1, -1)
    ]
    return {
        "value": value,
        "date": latest.isoformat(),
        "prev": prev,
        "delta_pct": delta_pct,
        "trend7": trend7,
    }


def offline_dod(connection):
    """⑭ 线下日环比核：最新数据日 Σsales、前一自然日、近 7 自然日。"""
    latest = _fetch_one(connection, _OFFLINE_DOD_LATEST_SQL)
    if latest is None:
        return None
    return _dod_core(_dod_totals(connection, _OFFLINE_DOD_WINDOW_SQL, latest), latest)


def channel_dod(connection):
    """⑭ 电商日环比核：同口径，源 fact_channel_daily_sales（无合计行）。"""
    latest = _fetch_one(connection, _CHANNEL_DOD_LATEST_SQL)
    if latest is None:
        return None
    return _dod_core(_dod_totals(connection, _CHANNEL_DOD_WINDOW_SQL, latest), latest)
```

（3）在 Task 3 新增的 `run_bar_department_mtd` 之后插入：

```python
def _run_dod(core):
    """⑭ 环比卡载荷：空表给出全空核（value 0，其余 None，trend7 空）。"""
    payload = {
        "chart": "scalar",
        "value": 0.0,
        "date": None,
        "prev": None,
        "delta_pct": None,
        "trend7": [],
        "unit": _UNIT,
    }
    if core is not None:
        payload.update(core)
    return payload


def run_kpi_offline_dod(connection, params):
    """⑭ 线下日环比卡（L1，无参）。"""
    return _run_dod(offline_dod(connection))


def run_kpi_channel_dod(connection, params):
    """⑭ 电商日环比卡（L1，无参）。"""
    return _run_dod(channel_dod(connection))
```

- [ ] **Step 4: 跑 GREEN**

```
python -m unittest tests.common.test_bi_web_queries -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`OK`。

### Task 5: l2-channel 取数（trend_channel_daily / bar(month) / table 两张）

**Files:**
- Modify: `common/bi_web/queries.py`
- Test: `tests/common/test_bi_web_queries.py`

口径要点：

1. **复用卡参数化（双路径）**：`run_bar_channel_mtd` 无 month 仍走阶段 A 静态 `channel_mtd_ranking`（`StaticSqlTests` 与既有 `RunPayloadTests` 零改动）；带 month 走新 `channel_month_ranking`。与 Task 3 的 `run_trend_region_daily` 同构。
2. **ROI=Σsales÷Σpromo，Python 计算**（spec §3.3/§7）：SQL 聚合 `SUM(sales_amount)` 与 `SUM(promotion_cost)`，除法在 run 层做——绝不取表内行级 roi 源列（那是均值口径）。`SUM(promotion_cost)` 组内全 NULL → NULL（直播 48/48、京东 12/12 全月无推广费）→ payload `None` → 前端「—」；promo 为 0 同样 ROI=None（除零护栏）。
3. **店铺数=COUNT(DISTINCT store_name)**：melt 后同店跨日多行，必须 DISTINCT；渠道对比行内即店铺数（≤10/渠道）。
4. **table_store_mtd 双形态**：无 channel=全部 32 店含渠道列（`GROUP BY store_name, channel`）；有 channel=该渠道店铺（数据天然 ≤10 行，不加 LIMIT 不分页）。排名由服务端 `enumerate` 计算（spec §6）。列结构随形态变化：无 channel 时多一列「渠道」。
5. 渠道表无合计行：所有渠道 SQL 不带合计排除（`assertNotIn("合计", ...)` 锁死）。`_align_region_rows` 泛化为 `_align_series_rows(rows, name_key)`，区域/渠道趋势共用同一对齐核心（升序日期轴、零填充、序列名升序、MM-DD）。

- [ ] **Step 1: 写 RED 测试**

`tests/common/test_bi_web_queries.py` 两处改动：

（1）import 块（Task 3 的 21 名 + Task 4 的 4 名基础上）按字母序追加 7 个名字：`channel_mtd_comparison`、`channel_month_daily_series`、`channel_month_ranking`、`run_table_channel_mtd`、`run_table_store_mtd`、`run_trend_channel_daily`、`store_mtd_ranking`。

（2）Task 4 新增的 `ChannelDodTests` 之后、`# Integration layer` 注释之前，插入两个测试类：

```python
class ChannelMonthParamTests(ParameterizedSqlShapeTests):
    """l2-channel 取数：月区间 / channel 过滤（渠道表无合计行）。"""

    def test_channel_month_ranking_sql_shape(self):
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": []})

        channel_month_ranking(
            connection, first_day=date(2026, 9, 1), last_day=date(2026, 9, 30)
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30))
        )
        self.assertIn("FROM fact_channel_daily_sales", sql)
        self.assertIn("business_date BETWEEN %s AND %s", sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertIn("GROUP BY channel", sql)
        self.assertIn("ORDER BY total DESC", sql)
        self.assertNotIn("合计", sql)

    def test_channel_month_daily_series_sql_shape(self):
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": []})

        channel_month_daily_series(
            connection, first_day=date(2026, 9, 1), last_day=date(2026, 9, 30)
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30))
        )
        self.assertIn("GROUP BY business_date, channel", sql)
        self.assertIn(_TRUNCATION, sql)
        self.assertNotIn("合计", sql)

    def test_channel_mtd_comparison_sql_shape(self):
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": []})

        channel_mtd_comparison(
            connection, first_day=date(2026, 9, 1), last_day=date(2026, 9, 30)
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30))
        )
        self.assertIn("SUM(sales_amount)", sql)
        self.assertIn("SUM(promotion_cost)", sql)
        self.assertIn("COUNT(DISTINCT store_name)", sql)
        self.assertIn("GROUP BY channel", sql)
        self.assertIn("ORDER BY sales DESC", sql)
        self.assertIn(_TRUNCATION, sql)

    def test_store_mtd_ranking_without_channel_sql_shape(self):
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": []})

        store_mtd_ranking(
            connection, first_day=date(2026, 9, 1), last_day=date(2026, 9, 30)
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30))
        )
        self.assertIn("GROUP BY store_name, channel", sql)
        self.assertIn("ORDER BY total DESC", sql)
        self.assertNotIn("channel = %s", sql)

    def test_store_mtd_ranking_with_channel_sql_shape(self):
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": []})

        store_mtd_ranking(
            connection,
            channel="天猫",
            first_day=date(2026, 9, 1),
            last_day=date(2026, 9, 30),
        )

        sql = self.sole_parameterized_sql(
            connection, (date(2026, 9, 1), date(2026, 9, 30), "天猫")
        )
        self.assertIn("channel = %s", sql)
        self.assertIn("GROUP BY store_name, channel", sql)


class ChannelRunPayloadTests(unittest.TestCase):
    """l2-channel 四卡载荷：trend / bar(month) / 渠道对比表 / 店铺排行表。"""

    def test_run_trend_channel_daily_payload(self):
        rows = [
            {"business_date": date(2026, 9, 2), "channel": "天猫", "total": Decimal("5")},
            {"business_date": date(2026, 9, 2), "channel": "京东", "total": Decimal("3")},
            {"business_date": date(2026, 9, 3), "channel": "天猫", "total": Decimal("7")},
        ]
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": rows})

        payload = run_trend_channel_daily(connection, {"month": "2026-09"})

        self.assertEqual(
            {
                "chart": "line",
                "dates": ["09-02", "09-03"],
                "series": [
                    {"name": "京东", "data": [3.0, 0.0]},
                    {"name": "天猫", "data": [5.0, 7.0]},
                ],
            },
            payload,
        )
        sql, parameters = connection.executed[0]
        self.assertEqual((date(2026, 9, 1), date(2026, 9, 30)), parameters)

    def test_run_bar_channel_mtd_with_month_uses_parameterized_path(self):
        rows = [{"channel": "天猫", "total": Decimal("300")}]
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": rows})

        payload = run_bar_channel_mtd(connection, {"month": "2026-08"})

        self.assertEqual(
            {"chart": "bar", "categories": ["天猫"], "values": [300.0], "unit": "元"},
            payload,
        )
        sql, parameters = connection.executed[0]
        self.assertEqual((date(2026, 8, 1), date(2026, 8, 31)), parameters)

    def test_run_bar_channel_mtd_without_month_keeps_static_path(self):
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": []})

        run_bar_channel_mtd(connection, {})

        sql, parameters = connection.executed[0]
        self.assertIsNone(parameters)
        self.assertIn(_MONTH_START, sql)

    def test_run_table_channel_mtd_payload_with_roi(self):
        rows = [
            {"channel": "天猫", "sales": Decimal("300"), "promo": Decimal("60"), "stores": 3},
            {"channel": "直播", "sales": Decimal("48"), "promo": None, "stores": 1},
            {"channel": "拼多多", "sales": Decimal("90"), "promo": Decimal("0"), "stores": 2},
        ]
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": rows})

        payload = run_table_channel_mtd(connection, {"month": "2026-09"})

        self.assertEqual(
            {
                "chart": "table",
                "columns": [
                    {"key": "channel", "title": "渠道"},
                    {"key": "sales", "title": "本月销售额", "format": "wan"},
                    {"key": "promo", "title": "推广费", "format": "wan"},
                    {"key": "roi", "title": "ROI", "format": "ratio"},
                    {"key": "stores", "title": "店铺数"},
                ],
                "rows": [
                    {"channel": "天猫", "sales": 300.0, "promo": 60.0, "roi": 5.0, "stores": 3},
                    {"channel": "直播", "sales": 48.0, "promo": None, "roi": None, "stores": 1},
                    {"channel": "拼多多", "sales": 90.0, "promo": 0.0, "roi": None, "stores": 2},
                ],
            },
            payload,
        )

    def test_run_table_store_mtd_all_stores_includes_channel_column(self):
        rows = [
            {"store_name": "天猫官方旗舰店", "channel": "天猫", "total": Decimal("300")},
            {"store_name": "京东自营", "channel": "京东", "total": Decimal("100")},
        ]
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": rows})

        payload = run_table_store_mtd(connection, {"month": "2026-09"})

        self.assertEqual(
            {
                "chart": "table",
                "columns": [
                    {"key": "rank", "title": "排名"},
                    {"key": "store", "title": "店铺"},
                    {"key": "channel", "title": "渠道"},
                    {"key": "sales", "title": "本月销售额", "format": "wan"},
                ],
                "rows": [
                    {"rank": 1, "store": "天猫官方旗舰店", "channel": "天猫", "sales": 300.0},
                    {"rank": 2, "store": "京东自营", "channel": "京东", "sales": 100.0},
                ],
            },
            payload,
        )
        sql, parameters = connection.executed[0]
        self.assertEqual((date(2026, 9, 1), date(2026, 9, 30)), parameters)

    def test_run_table_store_mtd_single_channel_omits_channel_column(self):
        rows = [{"store_name": "天猫官方旗舰店", "channel": "天猫", "total": Decimal("300")}]
        connection = FakeConnection(rowsets={"fact_channel_daily_sales": rows})

        payload = run_table_store_mtd(connection, {"channel": "天猫", "month": "2026-09"})

        self.assertEqual(
            {
                "chart": "table",
                "columns": [
                    {"key": "rank", "title": "排名"},
                    {"key": "store", "title": "店铺"},
                    {"key": "sales", "title": "本月销售额", "format": "wan"},
                ],
                "rows": [{"rank": 1, "store": "天猫官方旗舰店", "sales": 300.0}],
            },
            payload,
        )
        sql, parameters = connection.executed[0]
        self.assertIn("channel = %s", sql)
        self.assertEqual((date(2026, 9, 1), date(2026, 9, 30), "天猫"), parameters)
```

- [ ] **Step 2: 跑 RED**

```
python -m unittest tests.common.test_bi_web_queries -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`FAILED (errors=...)`——ImportError（`channel_month_ranking` 等不存在）。

- [ ] **Step 3: 写 GREEN 实现**

`common/bi_web/queries.py` 三处改动：

（1）`_align_region_rows` 整体替换为泛化版（改名 + name_key 参数），并把两处调用点改为传 `"region"`：

```python
def _align_series_rows(rows, name_key) -> dict:
    """按升序日期轴对齐各名称序列并零填充（区域/渠道趋势共用核心）。"""
    dates = sorted({row["business_date"] for row in rows})
    index_of = {business_date: index for index, business_date in enumerate(dates)}
    by_name = {}
    for row in rows:
        name = row[name_key] or ""
        index = index_of[row["business_date"]]
        by_name.setdefault(name, [Decimal(0)] * len(dates))[index] = Decimal(
            row["total"] or 0
        )
    return {
        "dates": [_mmdd(business_date) for business_date in dates],
        "series": [
            {"name": name, "data": by_name[name]} for name in sorted(by_name)
        ],
    }


def region_daily_series(connection) -> dict:
    return _align_series_rows(_fetch_rows(connection, _REGION_DAILY_SQL), "region")
```

`region_month_daily_series` 的收尾行由 `return _align_region_rows(rows)` 改为 `return _align_series_rows(rows, "region")`。

（2）Task 4 新增的 `channel_dod` 之后插入渠道 SQL 常量与低层函数：

```python
_CHANNEL_MONTH_RANKING_SQL = (
    "SELECT channel, SUM(sales_amount) AS total "
    "FROM fact_channel_daily_sales "
    "WHERE business_date BETWEEN %s AND %s "
    "AND business_date <= CURDATE() "
    "GROUP BY channel "
    "ORDER BY total DESC"
)

_CHANNEL_MONTH_DAILY_SQL = (
    "SELECT business_date, channel, SUM(sales_amount) AS total "
    "FROM fact_channel_daily_sales "
    "WHERE business_date BETWEEN %s AND %s "
    "AND business_date <= CURDATE() "
    "GROUP BY business_date, channel"
)

_CHANNEL_MTD_COMPARISON_SQL = (
    "SELECT channel, "
    "SUM(sales_amount) AS sales, "
    "SUM(promotion_cost) AS promo, "
    "COUNT(DISTINCT store_name) AS stores "
    "FROM fact_channel_daily_sales "
    "WHERE business_date BETWEEN %s AND %s "
    "AND business_date <= CURDATE() "
    "GROUP BY channel "
    "ORDER BY sales DESC"
)


def _optional_channel_filter(channel):
    """可选 channel 过滤：返回 (SQL 片段, 追加参数元组)；空即全渠道。"""
    if channel:
        return "AND channel = %s ", (channel,)
    return "", ()


def channel_month_ranking(connection, *, first_day, last_day) -> dict:
    """指定月渠道 Σsales 降序排行（参数化版 channel_mtd_ranking）。"""
    rows = _fetch_rows(connection, _CHANNEL_MONTH_RANKING_SQL, (first_day, last_day))
    return {
        "categories": [row["channel"] or "" for row in rows],
        "values": [Decimal(row["total"] or 0) for row in rows],
    }


def channel_month_daily_series(connection, *, first_day, last_day) -> dict:
    """指定月按日 Σsales 分渠道序列（渠道名升序，零填充）。"""
    rows = _fetch_rows(connection, _CHANNEL_MONTH_DAILY_SQL, (first_day, last_day))
    return _align_series_rows(rows, "channel")


def channel_mtd_comparison(connection, *, first_day, last_day) -> list:
    """渠道对比行：Σsales、Σpromo（组内全空→NULL）、店铺数（DISTINCT）。"""
    return _fetch_rows(connection, _CHANNEL_MTD_COMPARISON_SQL, (first_day, last_day))


def store_mtd_ranking(connection, *, channel=None, first_day, last_day) -> dict:
    """店铺当月排行：Σsales 降序、服务端名次；无 channel 含渠道列。"""
    channel_sql, channel_params = _optional_channel_filter(channel)
    sql = (
        "SELECT store_name, channel, SUM(sales_amount) AS total "
        "FROM fact_channel_daily_sales "
        "WHERE business_date BETWEEN %s AND %s "
        f"{channel_sql}"
        "AND business_date <= CURDATE() "
        "GROUP BY store_name, channel "
        "ORDER BY total DESC"
    )
    rows = _fetch_rows(connection, sql, (first_day, last_day) + channel_params)
    columns = [{"key": "rank", "title": "排名"}, {"key": "store", "title": "店铺"}]
    if channel is None:
        columns.append({"key": "channel", "title": "渠道"})
    columns.append({"key": "sales", "title": "本月销售额", "format": "wan"})
    table_rows = []
    for index, row in enumerate(rows, start=1):
        entry = {"rank": index, "store": row["store_name"]}
        if channel is None:
            entry["channel"] = row["channel"]
        entry["sales"] = float(Decimal(row["total"] or 0))
        table_rows.append(entry)
    return {"columns": columns, "rows": table_rows}
```

（3）run 函数段：`run_bar_channel_mtd` 整体替换为双路径版：

```python
def run_bar_channel_mtd(connection, params) -> dict:
    """渠道当月排行: L1 无参=当前月（静态）；l2-channel 带 month 参数化。"""
    month = params.get("month")
    if month is None:
        ranking = channel_mtd_ranking(connection)
    else:
        first_day, last_day = month_bounds(month)
        ranking = channel_month_ranking(
            connection, first_day=first_day, last_day=last_day
        )
    return {
        "chart": "bar",
        "categories": ranking["categories"],
        "values": [float(value) for value in ranking["values"]],
        "unit": _UNIT,
    }
```

Task 4 新增的 `run_kpi_channel_dod` 之后追加三个 run 函数：

```python
def run_trend_channel_daily(connection, params) -> dict:
    """电商渠道日销趋势（l2-channel）: month 缺省当前月，分渠道多系列。"""
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    first_day, last_day = month_bounds(month)
    series = channel_month_daily_series(
        connection, first_day=first_day, last_day=last_day
    )
    return {
        "chart": "line",
        "dates": series["dates"],
        "series": [
            {"name": entry["name"], "data": [float(value) for value in entry["data"]]}
            for entry in series["series"]
        ],
    }


def run_table_channel_mtd(connection, params) -> dict:
    """渠道对比表（模块 ② 渠道×店铺子集）: Σsales/Σpromo/ROI/店铺数。

    ROI=Σsales÷Σpromo 在此层计算（绝不取行级 roi 源列）；promo 全空
    → NULL → 前端「—」；promo 为 0 → ROI None（除零护栏）。
    """
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    first_day, last_day = month_bounds(month)
    rows = channel_mtd_comparison(connection, first_day=first_day, last_day=last_day)
    table_rows = []
    for row in rows:
        sales = float(row["sales"] or 0)
        promo = None if row["promo"] is None else float(row["promo"])
        roi = None if promo in (None, 0) else sales / promo
        table_rows.append(
            {
                "channel": row["channel"],
                "sales": sales,
                "promo": promo,
                "roi": roi,
                "stores": int(row["stores"] or 0),
            }
        )
    return {
        "chart": "table",
        "columns": [
            {"key": "channel", "title": "渠道"},
            {"key": "sales", "title": "本月销售额", "format": "wan"},
            {"key": "promo", "title": "推广费", "format": "wan"},
            {"key": "roi", "title": "ROI", "format": "ratio"},
            {"key": "stores", "title": "店铺数"},
        ],
        "rows": table_rows,
    }


def run_table_store_mtd(connection, params) -> dict:
    """店铺排行表（模块 ② 渠道×店铺子集）: channel/month；无 channel 含渠道列。"""
    channel = params.get("channel")
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    first_day, last_day = month_bounds(month)
    table = store_mtd_ranking(
        connection, channel=channel, first_day=first_day, last_day=last_day
    )
    return {"chart": "table", "columns": table["columns"], "rows": table["rows"]}
```

- [ ] **Step 4: 跑 GREEN**

```
python -m unittest tests.common.test_bi_web_queries -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`OK`——含既有 `ChannelMtdRankingTests` 与 `RunPayloadTests.test_run_bar_channel_mtd_payload`（`{}` 走静态路径）。

### Task 6: l2-people 取数（mart_collect 复用 + 快照缓存 + 四卡）

**Files:**
- Modify: `common/bi_web/queries.py`
- Test: `tests/common/test_bi_web_queries.py`

口径要点：

1. **复用不重写（本任务的核心纪律）**：人员口径零新增——参与人数 / Σ完成额 / 总达成率 / 人员榜全部由 `common.daily_robot.mart_leaderboard.mart_collect` 派生（跳合计行、dept 空→未分组、target 缺→0、`(-rate(None→-1), -completed, -target)` 排序、elapsed=已过工作日，全部沿用既有测试锁定的行为）。`mart_collect(connection, *, region, business_date, include_today)` 内部两条查询（`dim_calendar` 工作日 + 当月事实行）；`MartTaskError`（日历缺行）按 spec §8 降级为**空结果而非报错**，其余 DB 异常照常上抛走 503。
2. **模块级快照缓存 `_PEOPLE_CACHE`**：键 `(region, month)`，TTL 60s（`time.monotonic()`）。页面一次加载的四张卡（三 scalar + 一 table）只触发一轮采集查询；无 region=杭州+绍兴两次采集后合并重排（排序键与 mart_collect 逐字相同）。**模块状态警告（全局约定已声明）：本任务测试必须在 `setUp` 里 `_PEOPLE_CACHE.clear()`，否则同键测试互相吃到陈旧快照。**
3. **锚点映射**：month=当前月 → `business_date=今天`；历史月 → 月末。`include_today=True` 与日报机器人同口径（当天已填计入分子）。月份下拉只含「有数据月 ∪ 当前月」（Task 2 `_MONTH_OPTIONS_SQL`），未来月不会出现，不设额外护栏。
4. **`kpi_people_count` 的 unit 是「人」**：`dashboard.js` 现行 `formatWan` 无条件 ÷10000——Task 9 必须把 scalar 格式化改为按 unit 分派（`元`→万换算，`人`→原样千分位）。本任务先锁定载荷契约 `{"chart": "scalar", "value": 2.0, "unit": "人"}`。

依赖：`month_bounds`（Task 3 落地；执行顺序 1→12 已保证）。

- [ ] **Step 1: 写 RED 测试**

`tests/common/test_bi_web_queries.py` 三处改动：

（1）import 区两处：`from common.bi_web.queries import (` 块（Task 3/4/5 累计 32 名基础上）按字母序追加 4 个名字 `run_kpi_people_completed`、`run_kpi_people_count`、`run_kpi_people_rate`、`run_table_people_leaderboard`；块之前加一行模块导入（清缓存用）：

```python
from common.bi_web import queries as bi_web_queries
```

（2）Task 2 改过的 `FakeConnection._TABLES` 元组末尾追加日历表键：

```python
    _TABLES = (
        "fact_daily_report_offline",
        "fact_channel_daily_sales",
        "dim_target",
        "months",  # 伪表键：month_options 的 UNION 查询横跨两张事实表
        "dim_calendar",  # l2-people: fetch_workdays 的日历查询
    )
```

（3）Task 5 新增的 `ChannelRunPayloadTests` 之后、`# Integration layer` 注释之前，插入两个 fixture 函数、一个 fake 子类与一个测试类：

```python
def _people_calendar_rows():
    """2026-08 工作日三日（供 mart 路径的 dim_calendar 查询）。"""
    return [
        {"business_date": date(2026, 8, 3)},
        {"business_date": date(2026, 8, 4)},
        {"business_date": date(2026, 8, 5)},
    ]


def _hangzhou_people_facts():
    """杭州三人形：张三（部分填写）、李四（无部门无目标）、合计行（须被排除）。"""
    return [
        {
            "region": "杭州",
            "responsible_person": "张三",
            "department": "零售一组",
            "business_date": date(2026, 8, 3),
            "sales_amount": Decimal("30"),
            "monthly_target": Decimal("100"),
        },
        {
            "region": "杭州",
            "responsible_person": "张三",
            "department": "零售一组",
            "business_date": date(2026, 8, 4),
            "sales_amount": Decimal("40"),
            "monthly_target": Decimal("100"),
        },
        {
            "region": "杭州",
            "responsible_person": "李四",
            "department": None,
            "business_date": date(2026, 8, 3),
            "sales_amount": Decimal("10"),
            "monthly_target": None,
        },
        {
            "region": "杭州",
            "responsible_person": "合计",
            "department": None,
            "business_date": date(2026, 8, 3),
            "sales_amount": Decimal("999"),
            "monthly_target": None,
        },
    ]


def _shaoxing_people_facts():
    """绍兴一人形：王五（rate 0.9，用于合并重排断言）。"""
    return [
        {
            "region": "绍兴",
            "responsible_person": "王五",
            "department": "绍兴一组",
            "business_date": date(2026, 8, 3),
            "sales_amount": Decimal("90"),
            "monthly_target": Decimal("100"),
        },
    ]


class _PeopleFakeConnection(FakeConnection):
    """mart 路径 fake：事实行按最近一条语句的 region 参数过滤
    （模拟 fetch_month_facts 的 ``WHERE region = %s``）；日历行走 rowsets。"""

    def scripted_fetchall(self, sql):
        if "fact_daily_report_offline" in sql:
            region = self.executed[-1][1][0]
            rows = [
                row
                for row in self._rowsets.get("fact_daily_report_offline", [])
                if row.get("region") == region
            ]
            return [dict(row) for row in rows]
        return super().scripted_fetchall(sql)


class PeopleRunTests(unittest.TestCase):
    """l2-people 四卡：mart_collect 复用、快照缓存、日历缺行降级、两区合并。"""

    def setUp(self):
        bi_web_queries._PEOPLE_CACHE.clear()

    def hangzhou_connection(self):
        return _PeopleFakeConnection(
            rowsets={
                "dim_calendar": _people_calendar_rows(),
                "fact_daily_report_offline": _hangzhou_people_facts(),
            }
        )

    def test_kpi_people_count_excludes_summary_rows(self):
        payload = run_kpi_people_count(
            self.hangzhou_connection(), {"region": "杭州", "month": "2026-08"}
        )

        self.assertEqual({"chart": "scalar", "value": 2.0, "unit": "人"}, payload)

    def test_kpi_people_completed_sums_elapsed_workdays(self):
        payload = run_kpi_people_completed(
            self.hangzhou_connection(), {"region": "杭州", "month": "2026-08"}
        )

        self.assertEqual({"chart": "scalar", "value": 80.0, "unit": "元"}, payload)

    def test_kpi_people_rate_is_sum_ratio_not_personal_average(self):
        payload = run_kpi_people_rate(
            self.hangzhou_connection(), {"region": "杭州", "month": "2026-08"}
        )

        # Σcompleted÷Σtarget=80/100=0.8（个人率平均会得到 0.35）
        self.assertEqual(
            {
                "chart": "scalar",
                "value": 80.0,
                "target": 100.0,
                "rate": 0.8,
                "unit": "元",
            },
            payload,
        )

    def test_run_table_people_leaderboard_payload(self):
        payload = run_table_people_leaderboard(
            self.hangzhou_connection(), {"region": "杭州", "month": "2026-08"}
        )

        self.assertEqual(
            {
                "chart": "table",
                "columns": [
                    {"key": "rank", "title": "排名"},
                    {"key": "name", "title": "姓名"},
                    {"key": "dept", "title": "部门"},
                    {"key": "completed", "title": "完成额", "format": "wan"},
                    {"key": "target", "title": "月目标", "format": "wan"},
                    {"key": "rate", "title": "达成率", "format": "percent"},
                    {"key": "unfilled", "title": "未完成缺口"},
                ],
                "rows": [
                    {
                        "rank": 1,
                        "name": "张三",
                        "dept": "零售一组",
                        "completed": 70.0,
                        "target": 100.0,
                        "rate": 0.7,
                        "unfilled": 1,
                    },
                    {
                        "rank": 2,
                        "name": "李四",
                        "dept": "未分组",
                        "completed": 10.0,
                        "target": 0.0,
                        "rate": None,
                        "unfilled": 2,
                    },
                ],
            },
            payload,
        )

    def test_people_cards_share_one_snapshot(self):
        connection = self.hangzhou_connection()
        params = {"region": "杭州", "month": "2026-08"}

        run_kpi_people_count(connection, params)
        run_kpi_people_completed(connection, params)
        run_kpi_people_rate(connection, params)
        run_table_people_leaderboard(connection, params)

        # 四张卡共用一轮采集：dim_calendar + 事实表各一条
        self.assertEqual(2, len(connection.executed))

    def test_missing_calendar_degrades_to_empty_result(self):
        connection = FakeConnection(rowsets={"fact_daily_report_offline": []})
        params = {"region": "杭州", "month": "2026-08"}

        count_payload = run_kpi_people_count(connection, params)
        table_payload = run_table_people_leaderboard(connection, params)

        self.assertEqual(0.0, count_payload["value"])
        self.assertEqual([], table_payload["rows"])
        # 日历缺行即 MartTaskError：事实查询不再发起（空结果而非报错，spec §8）
        self.assertEqual(1, len(connection.executed))

    def test_no_region_merges_both_regions_and_resorts(self):
        connection = _PeopleFakeConnection(
            rowsets={
                "dim_calendar": _people_calendar_rows(),
                "fact_daily_report_offline": (
                    _hangzhou_people_facts() + _shaoxing_people_facts()
                ),
            }
        )

        count_payload = run_kpi_people_count(connection, {"month": "2026-08"})
        table_payload = run_table_people_leaderboard(connection, {"month": "2026-08"})

        self.assertEqual(3.0, count_payload["value"])
        # 合并后重排：王五 0.9 压过张三 0.7，李四（rate None）垫底
        self.assertEqual("王五", table_payload["rows"][0]["name"])
        self.assertEqual("张三", table_payload["rows"][1]["name"])
        self.assertEqual("李四", table_payload["rows"][2]["name"])
        # 两区各一轮采集（日历 + 事实各两条），第二张卡走缓存
        self.assertEqual(4, len(connection.executed))

    def test_run_kpi_people_count_defaults_month_to_current(self):
        connection = _PeopleFakeConnection(
            rowsets={
                "dim_calendar": [{"business_date": date.today().replace(day=1)}],
                "fact_daily_report_offline": [],
            }
        )

        payload = run_kpi_people_count(connection, {"region": "杭州"})

        self.assertEqual(0.0, payload["value"])
        sql, parameters = connection.executed[0]
        self.assertIn("dim_calendar", sql)
        self.assertEqual(
            month_bounds(date.today().strftime("%Y-%m")), parameters
        )
```

- [ ] **Step 2: 跑 RED**

```
python -m unittest tests.common.test_bi_web_queries -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`FAILED (errors=...)`——ImportError（`run_kpi_people_count` 等不存在）。

- [ ] **Step 3: 写 GREEN 实现**

`common/bi_web/queries.py` 三处改动：

（1）导入区两处：stdlib 块顶部加 `import time`；common 块（Task 3 已有 `from common.calendar_utils import month_days`）追加两行：

```python
import time
from datetime import datetime, timedelta
from decimal import Decimal

from common.calendar_utils import month_days
from common.daily_robot.mart_leaderboard import mart_collect
from common.daily_robot.mart_tasks import MartTaskError
```

（导入链纯 Python：`mart_leaderboard` → `calendar_utils` + `mart_tasks` + `metrics.daily_report`，无 DB/凭证副作用，与日报机器人同链。）

（2）Task 5 新增的 `store_mtd_ranking` 之后（低层函数区末尾）插入快照机制：

```python
_PEOPLE_REGIONS = ("杭州", "绍兴")
_PEOPLE_CACHE_TTL_SECONDS = 60.0
_PEOPLE_CACHE = {}


def _people_params(params):
    """run 层共用解析：region（None=杭州+绍兴两榜合并）、month（缺省当前月）。"""
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    return params.get("region"), month


def _people_as_of(month):
    """月参数 → mart_collect 锚点日：当前月=今天，历史月=月末。

    elapsed 以锚点截断（include_today=True 含当天），历史月取月末即
    全月工作日；未来月不在 month_options（数据月 ∪ 当前月），不设护栏。
    """
    first_day, last_day = month_bounds(month)
    today = datetime.now().date()
    if (first_day.year, first_day.month) == (today.year, today.month):
        return today
    return last_day


def _people_for_region(connection, region, month):
    """单区域人员行；dim_calendar 缺行（MartTaskError）降级为空列表。"""
    try:
        data = mart_collect(
            connection,
            region=region,
            business_date=_people_as_of(month),
            include_today=True,
        )
    except MartTaskError:
        return []
    return list(data.people)


def _people_snapshot(connection, region, month):
    """l2-people 四卡共享的人员快照（模块级 60s 缓存）。

    页面一次加载的四张卡（三 scalar + 一 table）只触发一轮采集查询；
    region=None 时杭州+绍兴两榜合并后按 ``(-rate(None→-1), -completed,
    -target)`` 重排（排序键与 mart_collect 逐字相同），单区域沿用
    mart_collect 自带排序。
    """
    key = (region, month)
    cached = _PEOPLE_CACHE.get(key)
    if cached is not None and time.monotonic() < cached[0]:
        return cached[1]
    if region is None:
        people = []
        for one_region in _PEOPLE_REGIONS:
            people.extend(_people_for_region(connection, one_region, month))
        people.sort(
            key=lambda person: (
                -(person["rate"] if person["rate"] is not None else -1),
                -person["completed"],
                -person["target"],
            )
        )
    else:
        people = _people_for_region(connection, region, month)
    _PEOPLE_CACHE[key] = (
        time.monotonic() + _PEOPLE_CACHE_TTL_SECONDS,
        people,
    )
    return people
```

（3）Task 5 新增的 `run_table_store_mtd` 之后追加四个 run 函数：

```python
def run_kpi_people_count(connection, params) -> dict:
    """参与人数: 榜上人员数（unit=人，前端不做万换算）。"""
    region, month = _people_params(params)
    people = _people_snapshot(connection, region, month)
    return {"chart": "scalar", "value": float(len(people)), "unit": "人"}


def run_kpi_people_completed(connection, params) -> dict:
    """Σ完成额: 已过工作日 sales_amount 合计（元）。"""
    region, month = _people_params(params)
    people = _people_snapshot(connection, region, month)
    return {
        "chart": "scalar",
        "value": float(sum(person["completed"] for person in people)),
        "unit": _UNIT,
    }


def run_kpi_people_rate(connection, params) -> dict:
    """总达成率: Σcompleted÷Σtarget（非个人率平均；Σtarget=0 → None）。"""
    region, month = _people_params(params)
    people = _people_snapshot(connection, region, month)
    completed = sum(person["completed"] for person in people)
    target = sum(person["target"] for person in people)
    rate = None if target == 0 else completed / target
    return {
        "chart": "scalar",
        "value": float(completed),
        "target": float(target),
        "rate": rate,
        "unit": _UNIT,
    }


def run_table_people_leaderboard(connection, params) -> dict:
    """人员榜: 服务端名次；未完成缺口=未填工作日数（spec §3.4）。"""
    region, month = _people_params(params)
    people = _people_snapshot(connection, region, month)
    rows = [
        {
            "rank": index,
            "name": person["name"],
            "dept": person["dept"],
            "completed": float(person["completed"]),
            "target": float(person["target"]),
            "rate": person["rate"],
            "unfilled": person["unfilled"],
        }
        for index, person in enumerate(people, start=1)
    ]
    return {
        "chart": "table",
        "columns": [
            {"key": "rank", "title": "排名"},
            {"key": "name", "title": "姓名"},
            {"key": "dept", "title": "部门"},
            {"key": "completed", "title": "完成额", "format": "wan"},
            {"key": "target", "title": "月目标", "format": "wan"},
            {"key": "rate", "title": "达成率", "format": "percent"},
            {"key": "unfilled", "title": "未完成缺口"},
        ],
        "rows": rows,
    }
```

- [ ] **Step 4: 跑 GREEN**

```
python -m unittest tests.common.test_bi_web_queries -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`OK`。特别注意 `test_missing_calendar_degrades_to_empty_result` 走的是普通 `FakeConnection`（日历 rowsets 缺省为空）——它同时验证 `_TABLES` 追加 `"dim_calendar"` 后既有 fake 行为不回归。

### Task 7: cards.py 注册表扩到 16 卡 + params_schema 声明

**Files:**
- Modify: `common/bi_web/cards.py`
- Test: `tests/common/test_bi_web_cards.py`
- Modify: `tests/common/test_bi_web_app.py`（1 处断言改子集，见 Step 1（6））

**口径要点：**

1. 5 旧卡 + 11 新卡 = 16；测试对 REGISTRY 键集合做**精确**断言（不是子集），任何漂移（多卡/少卡/拼错 id）当场红构建。
2. `params_schema` 从「空 dict 占位」升级为 **param 名 → filter source 名** 的映射（值取 `config.KNOWN_FILTER_SOURCES` 词表：`regions`/`channels`/`months`）。app 层（Task 8）拿这个映射做值闸（非法值 → 400）。import 时校验值在词表内——未知 source 与未知 chart 同级防护，启动即报错。`config.py` 只 import 标准库，`cards.py → config.py` 无环。
3. 复用卡的参数化扩展：`trend_region_daily` → `{region, month}`、`bar_channel_mtd` → `{month}`（双路径，见 Task 3/5）；L1 五张 scalar 卡（含两张新 dod 卡）保持无参数——L1 总览页无参，spec §4。

- [ ] **Step 1: 写 RED 测试**

`tests/common/test_bi_web_cards.py` 五处修改。

（1）导入区改为：

```python
from common.bi_web import queries
from common.bi_web.cards import (
    KNOWN_CHARTS,
    REGISTRY,
    Card,
    CardConfigError,
    _card,
    validate_dashboard_config,
)
from common.bi_web.config import (
    KNOWN_FILTER_SOURCES,
    CardPlacement,
    DashboardConfig,
)
```

（2）三个模块级常量块（`FIRST_BATCH_CARD_IDS` / `EXPECTED_CHARTS` / `EXPECTED_RUN_FUNCTIONS`）整体替换为四个：

```python
#: The stage-B card ids: five stage-A cards plus eleven new ones (16 in all).
STAGE_B_CARD_IDS = (
    "kpi_offline_mtd",
    "kpi_channel_mtd",
    "kpi_annual_progress",
    "trend_region_daily",
    "bar_channel_mtd",
    "kpi_offline_dod",
    "kpi_channel_dod",
    "kpi_region_mtd",
    "bar_department_mtd",
    "trend_channel_daily",
    "table_channel_mtd",
    "table_store_mtd",
    "kpi_people_count",
    "kpi_people_completed",
    "kpi_people_rate",
    "table_people_leaderboard",
)

#: The five scalar cards the L1 cockpit places without any URL parameter.
L1_PARAMLESS_CARD_IDS = (
    "kpi_offline_mtd",
    "kpi_channel_mtd",
    "kpi_annual_progress",
    "kpi_offline_dod",
    "kpi_channel_dod",
)

#: card_id -> expected chart kind.
EXPECTED_CHARTS = {
    "kpi_offline_mtd": "scalar",
    "kpi_channel_mtd": "scalar",
    "kpi_annual_progress": "scalar",
    "trend_region_daily": "line",
    "bar_channel_mtd": "bar",
    "kpi_offline_dod": "scalar",
    "kpi_channel_dod": "scalar",
    "kpi_region_mtd": "scalar",
    "bar_department_mtd": "bar",
    "trend_channel_daily": "line",
    "table_channel_mtd": "table",
    "table_store_mtd": "table",
    "kpi_people_count": "scalar",
    "kpi_people_completed": "scalar",
    "kpi_people_rate": "scalar",
    "table_people_leaderboard": "table",
}

#: card_id -> the queries.run_* function it must be bound to.
EXPECTED_RUN_FUNCTIONS = {
    "kpi_offline_mtd": queries.run_kpi_offline_mtd,
    "kpi_channel_mtd": queries.run_kpi_channel_mtd,
    "kpi_annual_progress": queries.run_kpi_annual_progress,
    "trend_region_daily": queries.run_trend_region_daily,
    "bar_channel_mtd": queries.run_bar_channel_mtd,
    "kpi_offline_dod": queries.run_kpi_offline_dod,
    "kpi_channel_dod": queries.run_kpi_channel_dod,
    "kpi_region_mtd": queries.run_kpi_region_mtd,
    "bar_department_mtd": queries.run_bar_department_mtd,
    "trend_channel_daily": queries.run_trend_channel_daily,
    "table_channel_mtd": queries.run_table_channel_mtd,
    "table_store_mtd": queries.run_table_store_mtd,
    "kpi_people_count": queries.run_kpi_people_count,
    "kpi_people_completed": queries.run_kpi_people_completed,
    "kpi_people_rate": queries.run_kpi_people_rate,
    "table_people_leaderboard": queries.run_table_people_leaderboard,
}

#: card_id -> URL-parameter whitelist, param name -> filter source.
EXPECTED_PARAMS_SCHEMA = {
    "kpi_offline_mtd": {},
    "kpi_channel_mtd": {},
    "kpi_annual_progress": {},
    "trend_region_daily": {"region": "regions", "month": "months"},
    "bar_channel_mtd": {"month": "months"},
    "kpi_offline_dod": {},
    "kpi_channel_dod": {},
    "kpi_region_mtd": {"region": "regions", "month": "months"},
    "bar_department_mtd": {"region": "regions", "month": "months"},
    "trend_channel_daily": {"month": "months"},
    "table_channel_mtd": {"month": "months"},
    "table_store_mtd": {"channel": "channels", "month": "months"},
    "kpi_people_count": {"region": "regions", "month": "months"},
    "kpi_people_completed": {"region": "regions", "month": "months"},
    "kpi_people_rate": {"region": "regions", "month": "months"},
    "table_people_leaderboard": {"region": "regions", "month": "months"},
}
```

（3）`_l1_cockpit()` 整体替换（Task 10 的 seed 布局：7 张 L1 卡）：

```python
def _l1_cockpit():
    """A valid dashboard placing the seven L1 cockpit cards (Task 10 seed)."""
    l1_card_ids = (
        "kpi_offline_dod",
        "kpi_channel_dod",
        "kpi_offline_mtd",
        "kpi_channel_mtd",
        "kpi_annual_progress",
        "trend_region_daily",
        "bar_channel_mtd",
    )
    return DashboardConfig(
        dashboard_id="l1-cockpit",
        title="首页驾驶舱",
        cards=tuple(
            CardPlacement(card=card_id, title=f"t-{card_id}", span=4)
            for card_id in l1_card_ids
        ),
    )
```

（4）`RegistryTests` 整体替换：

```python
class RegistryTests(unittest.TestCase):
    """Registry completeness and per-card invariants."""

    def test_registry_contains_exactly_the_stage_b_cards(self):
        self.assertEqual(set(STAGE_B_CARD_IDS), set(REGISTRY))
        self.assertEqual(16, len(REGISTRY))
        for card_id in STAGE_B_CARD_IDS:
            self.assertIsInstance(REGISTRY[card_id], Card)

    def test_every_card_uses_a_known_chart(self):
        for card_id in STAGE_B_CARD_IDS:
            with self.subTest(card_id=card_id):
                self.assertIn(REGISTRY[card_id].chart, KNOWN_CHARTS)
                self.assertEqual(
                    EXPECTED_CHARTS[card_id], REGISTRY[card_id].chart
                )

    def test_l1_scalar_cards_have_no_url_parameters(self):
        for card_id in L1_PARAMLESS_CARD_IDS:
            with self.subTest(card_id=card_id):
                self.assertEqual({}, REGISTRY[card_id].params_schema)

    def test_parameterized_card_whitelists_match_spec(self):
        for card_id, expected in EXPECTED_PARAMS_SCHEMA.items():
            with self.subTest(card_id=card_id):
                self.assertEqual(expected, REGISTRY[card_id].params_schema)

    def test_params_schema_values_are_known_filter_sources(self):
        for card_id in STAGE_B_CARD_IDS:
            for source in REGISTRY[card_id].params_schema.values():
                with self.subTest(card_id=card_id, source=source):
                    self.assertIn(source, KNOWN_FILTER_SOURCES)

    def test_card_builder_rejects_unknown_filter_source(self):
        with self.assertRaises(CardConfigError):
            _card(
                "bad_card", "scalar", queries.run_kpi_offline_mtd,
                {"region": "regionz"},
            )

    def test_every_card_binds_its_queries_run_function(self):
        for card_id, run_function in EXPECTED_RUN_FUNCTIONS.items():
            with self.subTest(card_id=card_id):
                self.assertEqual(run_function, REGISTRY[card_id].run)

    def test_known_charts_are_the_four_supported_kinds(self):
        self.assertEqual(("scalar", "line", "bar", "table"), KNOWN_CHARTS)
```

（5）`ValidateDashboardConfigTests` 里 `test_valid_five_card_dashboard_passes` 改名并沿用新 helper：

```python
    def test_valid_l1_dashboard_passes(self):
        # Must not raise.
        validate_dashboard_config(_l1_cockpit(), REGISTRY)
```

（6）`tests/common/test_bi_web_app.py` 的 `test_default_registry_is_the_real_registry` 末行断言改为子集（阶段 A 恰好「注册表 == L1 五卡」，阶段 B 起注册表是 16 卡而 L1 fixture 仍是 5 卡——这处不改，本任务的 GREEN 会红在 app 模块上）：

```python
        self.assertEqual(200, client.get("/d/l1-cockpit").status_code)
        self.assertTrue(set(_L1_CARD_IDS) <= set(REGISTRY))
```

（该断言改动前后都通过——它是兼容性 re-scope，不是 RED；位置放在本任务是让注册表增长时 app 模块全程保持绿。）

- [ ] **Step 2: 跑 RED**

```
python -m unittest tests.common.test_bi_web_cards -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`FAILED (failures=..., errors=...)`——`STAGE_B_CARD_IDS` 里 11 个新 id 不在 REGISTRY；`EXPECTED_RUN_FUNCTIONS` 取 `queries.run_kpi_offline_dod` 等抛 AttributeError；`KNOWN_FILTER_SOURCES` 不存在（ImportError）；`_card` 收第 4 个位置参数抛 TypeError。既有 `ValidateDashboardConfigTests` 大多仍绿（`_l1_cockpit` 的 7 个 id 里 2 个不在注册表 → `test_valid_l1_dashboard_passes` 也红）。

- [ ] **Step 3: 写 GREEN 实现**

`common/bi_web/cards.py` 三处修改（依赖 Task 3/4/5/6 已把 16 个 run 函数全部备齐）。

（1）模块 docstring 第一段后插入阶段 B 说明，并加 import：

```python
from common.bi_web import queries
from common.bi_web.config import KNOWN_FILTER_SOURCES
```

docstring 替换为：

```python
"""The bi-web card registry (stage A Task 3 / stage B Task 7).

The code-owned half of the ``SQL lives in code, layout lives in Nacos``
split: each card id binds a chart kind, the ``run`` function from
:mod:`common.bi_web.queries` and the URL-parameter whitelist.  Stage A
placed five parameter-less cards; stage B reuses two of them with
parameters (``trend_region_daily`` region+month, ``bar_channel_mtd``
month) and adds eleven cards -- sixteen in all.

The whitelist maps param name -> filter source from
``config.KNOWN_FILTER_SOURCES``: the app layer resolves the source to a
dimension lookup for the value gate (invalid value -> 400).  The registry
is validated at import time -- an unknown chart kind or an unknown
filter source fails the import, so configuration drift can never reach a
running process silently.  :func:`validate_dashboard_config` closes the
loop with Task 2's :class:`~common.bi_web.config.DashboardConfig` -- a
dashboard may only place card ids that exist in the registry.
"""
```

（2）`Card` docstring 与 `_card` 整体替换：

```python
@dataclass(frozen=True)
class Card:
    """One registry entry.

    ``run`` is ``(connection, params) -> chart-ready dict`` (see
    :mod:`common.bi_web.queries`); ``params_schema`` maps param name ->
    filter source name (``regions``/``channels``/``months``) -- the
    URL-parameter whitelist and its value domain in one mapping.
    """

    card_id: str
    chart: str
    run: Callable
    params_schema: dict


def _card(card_id, chart, run, params_schema=None):
    """Build one card with both halves import-time validated.

    An unknown chart kind or an unknown filter source raises here, at
    module load -- the same protection class for both.
    """
    if chart not in KNOWN_CHARTS:
        raise CardConfigError(f"card '{card_id}' has unknown chart '{chart}'")
    schema = dict(params_schema or {})
    if any(source not in KNOWN_FILTER_SOURCES for source in schema.values()):
        raise CardConfigError(f"card '{card_id}' has an unknown filter source")
    return Card(card_id=card_id, chart=chart, run=run, params_schema=schema)
```

（3）`_CARDS` 整体替换为 16 项：

```python
_CARDS = (
    _card("kpi_offline_mtd", "scalar", queries.run_kpi_offline_mtd),
    _card("kpi_channel_mtd", "scalar", queries.run_kpi_channel_mtd),
    _card("kpi_annual_progress", "scalar", queries.run_kpi_annual_progress),
    _card(
        "trend_region_daily", "line", queries.run_trend_region_daily,
        {"region": "regions", "month": "months"},
    ),
    _card(
        "bar_channel_mtd", "bar", queries.run_bar_channel_mtd,
        {"month": "months"},
    ),
    _card("kpi_offline_dod", "scalar", queries.run_kpi_offline_dod),
    _card("kpi_channel_dod", "scalar", queries.run_kpi_channel_dod),
    _card(
        "kpi_region_mtd", "scalar", queries.run_kpi_region_mtd,
        {"region": "regions", "month": "months"},
    ),
    _card(
        "bar_department_mtd", "bar", queries.run_bar_department_mtd,
        {"region": "regions", "month": "months"},
    ),
    _card(
        "trend_channel_daily", "line", queries.run_trend_channel_daily,
        {"month": "months"},
    ),
    _card(
        "table_channel_mtd", "table", queries.run_table_channel_mtd,
        {"month": "months"},
    ),
    _card(
        "table_store_mtd", "table", queries.run_table_store_mtd,
        {"channel": "channels", "month": "months"},
    ),
    _card(
        "kpi_people_count", "scalar", queries.run_kpi_people_count,
        {"region": "regions", "month": "months"},
    ),
    _card(
        "kpi_people_completed", "scalar", queries.run_kpi_people_completed,
        {"region": "regions", "month": "months"},
    ),
    _card(
        "kpi_people_rate", "scalar", queries.run_kpi_people_rate,
        {"region": "regions", "month": "months"},
    ),
    _card(
        "table_people_leaderboard", "table",
        queries.run_table_people_leaderboard,
        {"region": "regions", "month": "months"},
    ),
)
```

- [ ] **Step 4: 跑 GREEN**

```
python -m unittest tests.common.test_bi_web_cards tests.common.test_bi_web_queries tests.common.test_bi_web_config -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`OK`（三个模块一起跑，确认注册表扩展没碰坏口径与配置测试）。

---

### Task 8: app.py 路由层——跨页导航 + 筛选器渲染 + 参数值闸

**Files:**
- Modify: `common/bi_web/app.py`
- Test: `tests/common/test_bi_web_app.py`
- 不动模板：本任务只把 nav / filters / card_params 放进 `dashboard.html` 的模板上下文（stage A 模板不消费这些键，零影响）；渲染归 Task 9。

**机制要点（本任务锁定的五件事）：**

1. **导航 `_nav_entries(dashboard_source)`**：`dashboard_ids()` 枚举 → 逐个 `get_dashboard` → 只收 enabled 且有卡的 → 按 `(nav_order, dashboard_id)` 排序。枚举抛异常（含未实现 `dashboard_ids` 的鸭子类型源）→ 记一条 WARNING（仅类名）+ 返回 `()`（fail-open，页面照常 200，绝不让整页 5xx——Task 1 的契约注释）；单条 corrupt 条目 → 跳过 + 一条 WARNING（仅类名）。**顺序纪律：`dashboard_page` 里先 `_resolve_dashboard` 再算 nav**——corrupt 当前页在 resolve 处 503，nav 永不执行，既有「恰好一条 WARNING」断言不破。
2. **`_FILTER_SOURCE_QUERIES` 键集对拍**：`{"regions": queries.region_options, "channels": queries.channel_options, "months": queries.month_options}`，键集必须等于 `config.KNOWN_FILTER_SOURCES`——由测试对拍锁死（Task 1 注释定的契约：漂移=红构建，不是运行期 KeyError）。`app.py` 顶层 import `queries` 无环：`cards.py` 本就模块级 import `queries`，`app` 已传递依赖它。
3. **`_TTLOptionSets`**：选项按 source TTL 缓存（30s，锁内查询，照抄 `_TTLGate` 模式）。**失败不缓存、答 `None`**——同一个 None 喂两条路径：API 值闸 fail-open（坏值顶多表现为空卡，符合 spec §8「无数据组合→空结果而非报错」，绝不 5xx）；页面渲染转 503 单页（筛选器是页面契约的一部分，可见故障优于静默空下拉，与 corrupt 定义→503 同语义）。
4. **双闸**：既有键白名单 400（零改动）之后加值闸——`option_sets.contains(schema[key], value)` 为假 → 400 `bad_request`（安全文案，不回显值）。`params_schema` 的值此时已是维表 source 名（Task 7 注册表侧锁定；`replace()` 直改 dataclass 绕过校验是测试内部行为，不设防）。
5. **兼容性 re-scope**：既有 `test_whitelisted_query_parameter_reaches_run` 的 `params_schema={"days": {"type": "integer"}}` 值是 dict——值闸会拿它当 source 名查表，dict 不可哈希直接 TypeError。本任务把它改写为 region/维表场景（改前后都绿：改前没有值闸、参数直达 run；改后走真实闸门）。另有两处兼容钉子：L1 无 filters → 页面渲染**不开任何连接**（阶段 A 行为，钉死防回归）；`_CorruptDashboardSource` 假源补 `dashboard_ids` 委托（导航枚举需要，stage A 假源没这个方法）。

- [ ] **Step 1: 写 RED 测试**

`tests/common/test_bi_web_app.py` 七处改动：

（1）模块 docstring 的 bullet 列表（`gate:` 条目之后、integration smoke 条目之前）加一条：

```python
* stage B mechanics: navigation fail-open (enumeration errors never 5xx
  a page), filter options TTL-cached with page-local 503 on failure, and
  the param double gate (unknown key -> 400, value outside its dimension
  table -> 400, dimension failure -> fail-open);
```

（2）import 区两行替换：

```python
from common.bi_web.app import (
    _FILTER_SOURCE_QUERIES,
    _TTLGate,
    _TTLOptionSets,
    _nav_entries,
    create_app,
    main,
)
from common.bi_web.cards import Card, CardConfigError, REGISTRY
from common.bi_web.config import (
    DashboardConfigError,
    FileDashboardSource,
    KNOWN_FILTER_SOURCES,
    StaticDashboardSource,
)
```

（3）`_l1_mapping` 之后加 L2 构造助手与共用筛选器常量：

```python
def _l2_mapping(filters, cards=("kpi_offline_mtd",), nav_order=10):
    """A valid L2 seed entry carrying page filters."""
    return {
        "title": "分析页",
        "enabled": True,
        "refresh_seconds": 300,
        "nav_order": nav_order,
        "filters": [dict(spec) for spec in filters],
        "cards": [{"card": card, "title": f"t-{card}", "span": 4} for card in cards],
    }


#: The region+month filter pair shared by all three L2 pages.
_REGION_MONTH_FILTERS = (
    {"param": "region", "source": "regions", "label": "区域"},
    {"param": "month", "source": "months", "label": "月份"},
)
```

（4）`_CountingPipelineSource` 之后加维度假件（关键词匹配 Task 2 的三条维表 SQL：`DISTINCT region` / `DISTINCT channel` / `UNION`，互不碰撞；`dimension_sql` 跨请求累积，供 TTL 测试计数）：

```python
class _DimensionCursor:
    """Cursor recording SQL into its connection, serving rows by keyword."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, params=None):
        self._connection.dimension_sql.append(sql)

    def fetchall(self):
        sql = self._connection.dimension_sql[-1]
        for keyword, rows in self._connection.rows_by_keyword.items():
            if keyword in sql:
                return list(rows)
        return []

    def close(self):
        pass


class _DimensionConnection:
    """Connection whose cursors serve filter-option rows by SQL keyword."""

    def __init__(self, rows_by_keyword):
        self.rows_by_keyword = rows_by_keyword
        self.dimension_sql = []

    def cursor(self):
        return _DimensionCursor(self)

    def close(self):
        pass


#: keyword -> canned rows for the three filter sources.
_REGION_ROWS = {"DISTINCT region": [{"region": "杭州"}, {"region": "绍兴"}]}
_CHANNEL_ROWS = {"DISTINCT channel": [{"channel": "直播"}, {"channel": "京东"}]}
_MONTH_ROWS = {"UNION": [{"month": "2026-09"}, {"month": "2026-08"}]}


def _dimension_connector(rows_by_keyword):
    """``db_connector`` fake yielding one shared dimension connection."""
    connection = _DimensionConnection(rows_by_keyword)

    @contextmanager
    def connector():
        yield connection

    connector.connection = connection
    return connector


def _counting_connector(connection):
    """``db_connector`` fake counting connections opened."""
    calls = []

    @contextmanager
    def connector():
        calls.append(1)
        yield connection

    connector.calls = calls
    return connector


def _failing_connector(error):
    """``db_connector`` fake raising *error* on entry, counting attempts."""
    calls = []

    @contextmanager
    def connector():
        calls.append(1)
        raise error
        yield  # pragma: no cover - unreachable

    connector.calls = calls
    return connector
```

（5）`_CorruptDashboardSource` 类内（`get_dashboard` 之后）补枚举委托：

```python
    def dashboard_ids(self):
        return self._delegate.dashboard_ids()
```

（6）改写 `CardApiTests.test_whitelisted_query_parameter_reaches_run`（schema 值 dict→source 名；这是值闸的「合法值直达 run」正路径）：

```python
    def test_whitelisted_query_parameter_reaches_run(self):
        registry = _FakeRegistry()
        registry["kpi_offline_mtd"] = replace(
            registry["kpi_offline_mtd"], params_schema={"region": "regions"}
        )
        client = TestClient(
            _build_app(
                registry=registry, db_connector=_dimension_connector(_REGION_ROWS)
            )
        )

        response = client.get(
            "/api/d/l1-cockpit/cards/kpi_offline_mtd", params={"region": "杭州"}
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(registry.run_calls))
        self.assertEqual("kpi_offline_mtd", registry.run_calls[0][0])
        self.assertEqual({"region": "杭州"}, registry.run_calls[0][2])
```

（7）`TTLGateUnitTests` 之后（`DefaultDbConnectorTests` 之前）插入五个测试类：

```python
class FilterSourceParityTests(unittest.TestCase):
    """``_FILTER_SOURCE_QUERIES`` must track ``KNOWN_FILTER_SOURCES``.

    config.py validates filter sources against its vocabulary at parse
    time and cards.py validates params_schema against the same vocabulary
    at import time -- this pins the app-side query map to it, so a new
    source turns the build red here instead of KeyErrors at runtime.
    """

    def test_filter_source_queries_match_known_filter_sources(self):
        self.assertEqual(set(KNOWN_FILTER_SOURCES), set(_FILTER_SOURCE_QUERIES))


class NavTests(unittest.TestCase):
    """Top navigation: enabled dashboards by nav_order, fail-open."""

    def test_lists_enabled_dashboards_by_nav_order(self):
        source = StaticDashboardSource({
            "l2-people": _l2_mapping(_REGION_MONTH_FILTERS, nav_order=30),
            "l1-cockpit": _l1_mapping(),
            "l2-region": _l2_mapping(_REGION_MONTH_FILTERS, nav_order=10),
        })

        nav = _nav_entries(source)

        self.assertEqual(
            (
                ("l1-cockpit", "首页驾驶舱"),
                ("l2-region", "分析页"),
                ("l2-people", "分析页"),
            ),
            nav,
        )

    def test_disabled_dashboards_are_excluded(self):
        source = StaticDashboardSource({"l1-cockpit": _l1_mapping(enabled=False)})

        self.assertEqual((), _nav_entries(source))

    def test_corrupt_entries_are_skipped_and_logged_safely(self):
        source = _CorruptDashboardSource(
            {
                "l1-cockpit": _l1_mapping(),
                "l2-region": _l2_mapping(_REGION_MONTH_FILTERS),
            },
            corrupt_ids=("l2-region",),
        )

        with self.assertLogs("common.bi_web.app", level="WARNING") as logs:
            nav = _nav_entries(source)

        self.assertEqual((("l1-cockpit", "首页驾驶舱"),), nav)
        joined = "\n".join(logs.output)
        self.assertIn("DashboardConfigError", joined)
        self.assertNotIn("l2-region", joined)

    def test_enumeration_failure_answers_no_nav(self):
        class _UnenumerableSource(StaticDashboardSource):
            def dashboard_ids(self):
                raise RuntimeError("nacos list failed")

        self.assertEqual((), _nav_entries(_UnenumerableSource({"l1-cockpit": _l1_mapping()})))

    def test_page_renders_when_enumeration_fails(self):
        class _UnenumerableSource(StaticDashboardSource):
            def dashboard_ids(self):
                raise RuntimeError("nacos list failed")

        client = TestClient(
            _build_app(dashboard_source=_UnenumerableSource({"l1-cockpit": _l1_mapping()}))
        )

        self.assertEqual(200, client.get("/d/l1-cockpit").status_code)


class FilterRenderingTests(unittest.TestCase):
    """Filter dropdowns: server-side options via db_connector, TTL-cached."""

    def test_page_with_filters_loads_each_source_once_within_the_ttl(self):
        connector = _dimension_connector({**_REGION_ROWS, **_MONTH_ROWS})
        source = StaticDashboardSource(
            {"l1-cockpit": _l1_mapping(), "l2-region": _l2_mapping(_REGION_MONTH_FILTERS)}
        )
        client = TestClient(_build_app(dashboard_source=source, db_connector=connector))

        first = client.get("/d/l2-region")
        second = client.get("/d/l2-region")

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(2, len(connector.connection.dimension_sql))

    def test_page_without_filters_opens_no_connection(self):
        connector = _counting_connector(_DimensionConnection({}))
        client = TestClient(_build_app(db_connector=connector))

        response = client.get("/d/l1-cockpit")

        self.assertEqual(200, response.status_code)
        self.assertEqual([], connector.calls)

    def test_dimension_fetch_failure_is_unavailable_and_page_local(self):
        secret = RuntimeError("connect to mart-secret-host.example.test with pw123")
        source = StaticDashboardSource(
            {"l1-cockpit": _l1_mapping(), "l2-region": _l2_mapping(_REGION_MONTH_FILTERS)}
        )
        client = TestClient(
            _build_app(dashboard_source=source, db_connector=_fake_db_connector(error=secret))
        )

        broken = client.get("/d/l2-region")
        healthy = client.get("/d/l1-cockpit")

        self.assertEqual(503, broken.status_code)
        self.assertEqual("unavailable", broken.json()["detail"])
        self.assertEqual(200, healthy.status_code)
        self.assertNotIn("mart-secret-host", broken.text)
        self.assertNotIn("pw123", broken.text)

    def test_dimension_fetch_failure_logs_the_class_name_only(self):
        secret = RuntimeError("connect to mart-secret-host.example.test with pw123")
        source = StaticDashboardSource({"l2-region": _l2_mapping(_REGION_MONTH_FILTERS)})
        client = TestClient(
            _build_app(dashboard_source=source, db_connector=_fake_db_connector(error=secret))
        )

        with self.assertLogs("common.bi_web.app", level="WARNING") as logs:
            response = client.get("/d/l2-region")

        self.assertEqual(503, response.status_code)
        self.assertEqual(1, len(logs.output))
        joined = "\n".join(logs.output)
        self.assertIn("RuntimeError", joined)
        self.assertNotIn("mart-secret-host", joined)
        self.assertNotIn("pw123", joined)


class ValueGateTests(unittest.TestCase):
    """The second gate: param values must live in their dimension tables."""

    def _client(self, registry, db_connector):
        registry["kpi_offline_mtd"] = replace(
            registry["kpi_offline_mtd"], params_schema={"region": "regions"}
        )
        return TestClient(_build_app(registry=registry, db_connector=db_connector))

    def test_invalid_dimension_value_is_rejected(self):
        registry = _FakeRegistry()
        client = self._client(registry, _dimension_connector(_REGION_ROWS))

        response = client.get(
            "/api/d/l1-cockpit/cards/kpi_offline_mtd", params={"region": "不存在的区域"}
        )

        self.assertEqual(400, response.status_code)
        self.assertEqual("bad_request", response.json()["detail"])
        self.assertNotIn("不存在的区域", response.text)

    def test_value_gate_fails_open_when_the_dimension_query_raises(self):
        registry = _FakeRegistry()
        client = self._client(
            registry, _fake_db_connector(error=RuntimeError("dimension query failed"))
        )

        response = client.get(
            "/api/d/l1-cockpit/cards/kpi_offline_mtd", params={"region": "任意值"}
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual({"region": "任意值"}, registry.run_calls[0][2])

    def test_value_gate_uses_the_ttl_cache(self):
        registry = _FakeRegistry()
        connector = _dimension_connector(_REGION_ROWS)
        client = self._client(registry, connector)

        client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd", params={"region": "杭州"})
        client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd", params={"region": "绍兴"})

        self.assertEqual(1, len(connector.connection.dimension_sql))


class OptionSetUnitTests(unittest.TestCase):
    """``_TTLOptionSets``: TTL caching, failure retry, fail-open contains."""

    def test_options_are_cached_within_the_ttl(self):
        connector = _counting_connector(_DimensionConnection(_REGION_ROWS))
        option_sets = _TTLOptionSets(connector, ttl_seconds=3600.0)

        first = option_sets.options("regions")
        second = option_sets.options("regions")

        self.assertEqual(("杭州", "绍兴"), first)
        self.assertEqual(first, second)
        self.assertEqual(1, len(connector.calls))

    def test_failed_fetch_is_not_cached_and_retries(self):
        connector = _failing_connector(RuntimeError("mart down"))
        option_sets = _TTLOptionSets(connector, ttl_seconds=3600.0)

        self.assertIsNone(option_sets.options("regions"))
        self.assertIsNone(option_sets.options("regions"))
        self.assertEqual(2, len(connector.calls))

    def test_contains_fails_open_on_error(self):
        option_sets = _TTLOptionSets(_failing_connector(RuntimeError("mart down")))

        self.assertTrue(option_sets.contains("regions", "任意值"))

    def test_contains_rejects_unknown_values(self):
        option_sets = _TTLOptionSets(_dimension_connector(_REGION_ROWS))

        self.assertTrue(option_sets.contains("regions", "杭州"))
        self.assertFalse(option_sets.contains("regions", "别处"))

    def test_unknown_source_answers_none_and_contains_open(self):
        option_sets = _TTLOptionSets(_dimension_connector(_REGION_ROWS))

        self.assertIsNone(option_sets.options("no_such_source"))
        self.assertTrue(option_sets.contains("no_such_source", "任意值"))
```

- [ ] **Step 2: 跑 RED**

```
python -m unittest tests.common.test_bi_web_app -v 2>&1 | tail -8
```

预期：`FAILED (errors=1)`——模块级导入失败，`ImportError: cannot import name '_FILTER_SOURCE_QUERIES' from 'common.bi_web.app'`（`_nav_entries`/`_TTLOptionSets` 同因）。模块加载不了属预期 RED 形态：新符号尚未存在。

- [ ] **Step 3: 写 GREEN 实现**

`common/bi_web/app.py` 七处改动：

（1）import 区：stdlib 组加 `from types import SimpleNamespace`；bi_web 组把 queries 引进来（无环，见机制要点 2）：

```python
from types import SimpleNamespace

from common.bi_web import queries
from common.bi_web.cards import REGISTRY, CardConfigError, validate_dashboard_config
from common.bi_web.config import DashboardConfigError, load_seed, parse_dashboard_config
```

（2）模块 docstring 两处 routing bullet 替换（`GET /d/` 条目与 `GET /api/` 条目）：

```python
* ``GET /d/{dashboard_id}`` -- server-rendered shell, one placeholder per
  placed card, polling at ``refresh_seconds``; the shell context also
  carries the top navigation (every enabled dashboard by ``nav_order``),
  the page's filter dropdowns (options loaded from the mart, TTL-cached)
  and each card's whitelisted param names;
```

```python
* ``GET /api/d/{dashboard_id}/cards/{card_id}`` -- card JSON with
  ``Cache-Control: no-store``; URL parameters are whitelisted against
  ``Card.params_schema`` and their values validated against the
  dimension option sets (TTL-cached, fail-open on a failed lookup -- a
  bad value can only ever surface as an empty card, never a 5xx); the
  SQL itself is fully static;
```

（3）`_GATE_TTL_SECONDS = 30.0` 之后加：

```python
#: How long a filter option set stays cached (one dimension query per
#: source per window, mirroring the gate TTL).
_FILTER_TTL_SECONDS = 30.0
```

（4）`_LOGGER = logging.getLogger(__name__)` 之后加查询映射（对拍契约见机制要点 2）：

```python
#: Filter source name -> option query.  The key set is pinned to
#: ``config.KNOWN_FILTER_SOURCES`` by the test suite -- drift is a red
#: build, not a runtime KeyError.
_FILTER_SOURCE_QUERIES = {
    "regions": queries.region_options,
    "channels": queries.channel_options,
    "months": queries.month_options,
}
```

（5）`_TTLGate` 类之后加 `_TTLOptionSets`（锁内查询照抄 `_TTLGate`；失败不缓存、答 None 的语义见机制要点 3）：

```python
class _TTLOptionSets:
    """Filter option sets cached per source with a TTL (thread-safe).

    One dimension query per source per TTL window instead of one per
    request.  A failed query is never cached and answers ``None``: the
    value gate then fails open (a bad value only ever surfaces as an
    empty card, never a 5xx), while the page route turns it into a
    local 503.
    """

    def __init__(self, db_connector, source_queries=None,
                 ttl_seconds=_FILTER_TTL_SECONDS):
        self._db_connector = db_connector
        self._queries = (
            _FILTER_SOURCE_QUERIES if source_queries is None else source_queries
        )
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._options = {}
        self._options_at = {}

    def _fetch(self, source):
        query = self._queries.get(source)
        if query is None:
            return None
        try:
            with self._db_connector() as connection:
                return tuple(query(connection))
        except Exception as exc:
            _LOGGER.warning("filter options failed to load: %s", type(exc).__name__)
            return None

    def options(self, source):
        """Cached option tuple, or ``None`` (failed / unknown source)."""
        with self._lock:
            now = time.monotonic()
            cached_at = self._options_at.get(source)
            if cached_at is None or now - cached_at >= self._ttl_seconds:
                fetched = self._fetch(source)
                if fetched is None:
                    self._options.pop(source, None)
                    self._options_at.pop(source, None)
                    return None
                self._options[source] = fetched
                self._options_at[source] = now
            return self._options.get(source)

    def contains(self, source, value):
        """Fail-open membership: unknown source or failed lookup -> True."""
        options = self.options(source)
        return True if options is None else value in options
```

（6）`_resolve_dashboard` 之后（`create_app` 之前）加三个模块级助手（顺序纪律见机制要点 1——nav 在 `dashboard_page` 里于 resolve 之后调用）：

```python
def _nav_entries(dashboard_source):
    """Top-navigation entries: every enabled dashboard, by nav_order.

    Enumeration failures degrade to "no navigation" (fail-open, never a
    5xx); a corrupt sibling entry is skipped with one safe WARNING --
    the broken page itself already answers 503 when visited directly.
    """
    try:
        ids = dashboard_source.dashboard_ids()
    except Exception as exc:
        _LOGGER.warning("dashboard enumeration failed: %s", type(exc).__name__)
        return ()
    entries = []
    for dashboard_id in ids:
        try:
            dashboard = dashboard_source.get_dashboard(dashboard_id)
        except DashboardConfigError as exc:
            _LOGGER.warning(
                "dashboard config failed to resolve: %s", type(exc).__name__
            )
            continue
        if dashboard.enabled and dashboard.cards:
            entries.append(
                (dashboard.nav_order, dashboard.dashboard_id, dashboard.title)
            )
    return tuple(
        (dashboard_id, title) for _, dashboard_id, title in sorted(entries)
    )


def _filter_views(dashboard, option_sets):
    """One render-ready view per page filter; 503 when options fail."""
    views = []
    for spec in dashboard.filters:
        options = option_sets.options(spec.source)
        if options is None:
            raise HTTPException(status_code=503, detail="unavailable")
        views.append(
            SimpleNamespace(param=spec.param, label=spec.label, options=options)
        )
    return tuple(views)


def _card_params(dashboard, registry):
    """card_id -> its whitelisted param names, for the page's JS wiring.

    The frontend intersects page URL params with this per-card list, so
    a page-level param never 400s a card that does not declare it.
    """
    return {
        placement.card: tuple(registry[placement.card].params_schema)
        for placement in dashboard.cards
    }
```

（7）`create_app` 三处：

a. `if gate is None: gate = _TTLGate()` 之后加：

```python
    option_sets = _TTLOptionSets(db_connector)
```

b. `dashboard_page` 整个函数体替换（先 resolve 再 nav，上下文四键）：

```python
    @app.get("/d/{dashboard_id}", dependencies=[Depends(require_bearer)])
    def dashboard_page(dashboard_id: str, request: Request):
        dashboard = _resolve_dashboard(dashboard_id, dashboard_source, registry, gate)
        return _TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "dashboard": dashboard,
                "nav": _nav_entries(dashboard_source),
                "filters": _filter_views(dashboard, option_sets),
                "card_params": _card_params(dashboard, registry),
            },
        )
```

c. `card_data` 里键白名单 400 之后、`try:` 之前插入值闸：

```python
        if any(key not in card.params_schema for key in params):
            raise HTTPException(status_code=400, detail="bad_request")
        for key, value in params.items():
            if not option_sets.contains(card.params_schema[key], value):
                raise HTTPException(status_code=400, detail="bad_request")
```

- [ ] **Step 4: 跑 GREEN**

```
python -m unittest tests.common.test_bi_web_app tests.common.test_bi_web_cards tests.common.test_bi_web_config tests.common.test_bi_web_queries -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`OK`（四个模块一起跑：app 新矩阵 + 既有矩阵零回归——无参 API 调用不触发值闸，L1 页面渲染不开连接，corrupt/healthz/auth/gate 的「恰好一条 WARNING」断言全部保持）。

### Task 9: 前端层 — 导航、筛选条、data-params 与 JS 重写

**Files:**
- Modify: `common/bi_web/templates/base.html`（`<body>` 内 `{% block content %}` 之前插顶部导航块）
- Modify: `common/bi_web/templates/dashboard.html`（整文件替换：筛选条 + `data-params`/`data-onclick-param`）
- Modify: `common/bi_web/static/dashboard.js`（整文件重写，阶段 A 行为全保留）
- Modify: `common/bi_web/static/style.css`（文件末尾追加阶段 B 样式）
- Test: `tests/common/test_bi_web_app.py`（`DashboardPageTests` 扩充）

**前置事实（Task 8 交付的模板上下文键，直接消费）：**
- `nav`：tuple of `(dashboard_id, title)`，已按 `nav_order` 升序排好，失败 fail-open 为空 `()`
- `filters`：tuple of `SimpleNamespace(param, label, options)`，`options` 是字符串 tuple（页面无筛选器时为空 tuple）
- `card_params`：dict `{card_id: (param 名, ...)}`，页面每张卡都有键；未声明参数的卡值为空 tuple（模板用 `.get()` + 真值判断跳过属性输出）
- `placement.on_click`：str 或 None（Task 1 数据类字段）

**JS 层的行为基线（重写后必须逐条保留，阶段 A 测试与用户已验收）：**
1. `ensureChart` 先 `classList.add("chart")` 再 `echarts.init`（echarts init 时快照容器尺寸——注释原样保留）
2. `window` resize 监听 + `window.echarts` 存在性守卫
3. fetch `cache: "no-store"`；非 ok 抛错；任何失败 → `.error`「加载失败」
4. 轮询间隔取 `data-refresh-seconds`，缺省 300 秒
5. 全局 `echarts` 只在数据回调里触碰（CDN 未就绪时安静降级为错误态）
6. `renderBar` 的 `inverse: true`（第一名排最上）与其注释

**模板改动不碰的既有断言（安全边界，改前先想清楚）：**
- `test_renders_one_placeholder_per_card_with_refresh_seconds`：`5 == body.count('data-api="/api/d/l1-cockpit/cards/')` —— `data-api` 属性原样保留
- `test_page_wires_local_assets_and_a_deferred_echarts_cdn_script`：style.css / dashboard.js / CDN / defer 全不动
- Task 8 的 `FilterRenderingTests` 只断状态码与连接计数，不看标记

---

- [ ] **Step 1: RED — DashboardPageTests 四个新渲染测试**

`test_renders_one_placeholder_per_card_with_refresh_seconds` 之后、`test_page_wires_local_assets_and_a_deferred_echarts_cdn_script` 之前插入：

```python
    def test_page_renders_top_navigation_with_current_page_highlight(self):
        source = StaticDashboardSource(
            {"l1-cockpit": _l1_mapping(), "l2-region": _l2_mapping(_REGION_MONTH_FILTERS)}
        )
        client = TestClient(_build_app(dashboard_source=source))

        body = client.get("/d/l1-cockpit").text

        self.assertIn('href="/d/l1-cockpit"', body)
        self.assertIn('href="/d/l2-region"', body)
        self.assertIn("首页驾驶舱", body)
        self.assertIn("分析页", body)
        self.assertEqual(1, body.count('class="topnav-link current"'))
        self.assertLess(
            body.index('href="/d/l1-cockpit"'),
            body.index('href="/d/l2-region"'),
        )

    def test_page_with_filters_renders_one_select_per_filter(self):
        source = StaticDashboardSource(
            {"l2-region": _l2_mapping(_REGION_MONTH_FILTERS)}
        )
        client = TestClient(
            _build_app(
                dashboard_source=source,
                db_connector=_dimension_connector({**_REGION_ROWS, **_MONTH_ROWS}),
            )
        )

        body = client.get("/d/l2-region").text

        self.assertEqual(2, body.count("<select"))
        self.assertIn('data-param="region"', body)
        self.assertIn('data-param="month"', body)
        self.assertIn("区域", body)
        self.assertIn("月份", body)
        self.assertIn('<option value="">全部</option>', body)
        self.assertIn('<option value="杭州">杭州</option>', body)
        self.assertIn('<option value="绍兴">绍兴</option>', body)
        self.assertIn('<option value="2026-09">2026-09</option>', body)

    def test_page_emits_each_cards_param_whitelist(self):
        registry = _FakeRegistry()
        registry["kpi_offline_mtd"] = replace(
            registry["kpi_offline_mtd"],
            params_schema={"region": "regions", "month": "months"},
        )
        source = StaticDashboardSource(
            {"l1-cockpit": _l1_mapping(cards=("kpi_offline_mtd", "kpi_channel_mtd"))}
        )
        client = TestClient(_build_app(dashboard_source=source, registry=registry))

        body = client.get("/d/l1-cockpit").text

        self.assertIn('data-params="region month"', body)
        self.assertEqual(1, body.count("data-params="))

    def test_page_emits_onclick_param_for_drilldown_cards(self):
        source = StaticDashboardSource(
            {
                "l2-channel": {
                    "title": "渠道下钻",
                    "enabled": True,
                    "refresh_seconds": 300,
                    "nav_order": 20,
                    "filters": [dict(spec) for spec in _REGION_MONTH_FILTERS],
                    "cards": [
                        {
                            "card": "bar_channel_mtd",
                            "title": "t-bar",
                            "span": 6,
                            "on_click": {"param": "region"},
                        }
                    ],
                }
            }
        )
        client = TestClient(
            _build_app(
                dashboard_source=source,
                db_connector=_dimension_connector({**_REGION_ROWS, **_MONTH_ROWS}),
            )
        )

        body = client.get("/d/l2-channel").text

        self.assertIn('data-onclick-param="region"', body)
```

（第四个测试里 `on_click` 用 `"param": "region"` 而不是 channel——`_l2_mapping` 的筛选器是 region/month，Task 1 校验 on_click 必须落在同页 filters 内；这里测的是模板输出属性，不关心语义上是哪个参数。）

- [ ] **Step 2: 运行确认 RED**

```bash
python -m unittest tests.common.test_bi_web_app.DashboardPageTests -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`FAILED`，4 个失败——导航测试（无 `href="/d/l2-region"`）、筛选测试（无 `<select`）、data-params 测试（无 `data-params`）、on_click 测试（无 `data-onclick-param`）。四个全红。

- [ ] **Step 3: GREEN — base.html 插导航块**

两处改动：

（a）首行注释「阶段 A」改「阶段 B」（其余文字不动）。

（b）`<body>` 标签之后、`{% block content %}{% endblock %}` 之前插入：

```html
    {%- if nav %}
    <nav class="topnav">
      {%- for entry_id, entry_title in nav %}
      <a class="topnav-link{{ ' current' if entry_id == dashboard.dashboard_id else '' }}"
         href="/d/{{ entry_id }}">{{ entry_title }}</a>
      {%- endfor %}
    </nav>
    {%- endif %}
```

（`nav` 为空 tuple 时整个块不输出——`_nav_entries` fail-open 后页面本体不受影响。`dashboard` 变量在 base.html 已可用：`<title>` 一直引用它。）

- [ ] **Step 4: GREEN — dashboard.html 整文件替换**

```html
{% extends "base.html" %}
{% block content %}
{%- if filters %}
<div class="filters" role="group" aria-label="筛选">
  {%- for spec in filters %}
  <label class="filter">
    <span class="filter-label">{{ spec.label }}</span>
    <select data-param="{{ spec.param }}">
      <option value="">全部</option>
      {%- for option in spec.options %}
      <option value="{{ option }}">{{ option }}</option>
      {%- endfor %}
    </select>
  </label>
  {%- endfor %}
</div>
{%- endif %}
<main class="dashboard" data-refresh-seconds="{{ dashboard.refresh_seconds }}">
  {%- for placement in dashboard.cards %}
  <section class="card span-{{ placement.span }}" data-card="{{ placement.card }}"
           data-api="/api/d/{{ dashboard.dashboard_id }}/cards/{{ placement.card }}"
           {%- if card_params.get(placement.card) %} data-params="{{ card_params[placement.card] | join(' ') }}"
           {%- endif %}
           {%- if placement.on_click %} data-onclick-param="{{ placement.on_click }}"
           {%- endif %}>
    <h2>{{ placement.title }}</h2>
    <div class="card-body"></div>
  </section>
  {%- endfor %}
</main>
{% endblock %}
```

（与阶段 A 的差异只有三处：筛选条块、`data-params`（该卡接受的 URL 参数白名单，空 tuple 不输出属性）、`data-onclick-param`（下钻参数）。`data-card`/`data-api`/`data-refresh-seconds`/标题/卡体全部原样。）

- [ ] **Step 5: 运行确认模板层 GREEN**

```bash
python -m unittest tests.common.test_bi_web_app -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`OK`（新增 4 个测试通过；既有 DashboardPageTests 断言不受影响——`data-api` 计数仍为 5，资源接线断言原样命中）。

- [ ] **Step 6: GREEN — dashboard.js 整文件重写**

（宿主机 unittest 没有 JS 运行时，JS 行为无法 RED-GREEN；其契约面由 Step 1 的模板测试锁定（`data-params`/`data-onclick-param`/`data-param` 必须存在），行为验收推迟到 Task 12 浏览器冒烟。重写时逐条对照上文「JS 层的行为基线」。）

```js
/* bi-web 看板前端（阶段 B）：原生 JS，无框架、无构建。
 *
 * 阶段 A 行为全部保留：每张 .card 按 data-api 拉取（no-store），按
 * payload.chart 分派渲染，拉取/渲染失败一律展示 .error「加载失败」，
 * 轮询间隔取 <main data-refresh-seconds>（缺省 300 秒），窗口 resize
 * 同步图表实例。字段口径见 common/bi_web/queries.py。
 * 阶段 B 新增：
 *   - 筛选下拉（.filters select[data-param]）变更 → URL replaceState
 *     不刷页 → 全部卡片带参重拉；参数按卡各自 data-params 白名单做
 *     交集，页面级参数不会 400 未声明它的兄弟卡；轮询沿用当前 URL。
 *   - scalar 按 unit 分派格式化：元 → 万；人 → 原样千分位。
 *   - 日环比 scalar 扩展字段 date/prev/delta_pct/trend7：次行
 *     「前一日 X · 环比 ±X%」（升绿降红）+ 卡内迷你趋势（缺数日断线）。
 *   - table 渲染：columns[].format ∈ wan/percent/ratio，null → 「—」，
 *     空结果显示「暂无数据」占位行。
 *   - data-onclick-param 的 bar 卡：点击系列 → 设该参数（同步下拉）→
 *     replaceState → 全卡重拉。
 * ECharts 由 base.html 在浏览器侧从 CDN 加载，服务端零出网；全局
 * echarts 只在数据回调里触碰（CDN 未就绪时页面安静降级为错误态）。
 */
(() => {
  "use strict";

  const DEFAULT_REFRESH_SECONDS = 300;
  const ERROR_TEXT = "加载失败";
  const EMPTY_TEXT = "暂无数据";
  const PLACEHOLDER = "—";

  /* ---- 单位感知格式化 ---------------------------------------------------- */

  /* 元 → 万，中文千分位（GM 口径：金额一律以「万」展示）。 */
  const formatWan = (value) => (value / 10000).toLocaleString("zh-CN") + " 万";

  /* 人 → 原样千分位（不带「万」）。 */
  const formatCount = (value) => value.toLocaleString("zh-CN");

  /* scalar 大数字按 payload.unit 分派；未知单位退回原样字符串。 */
  const formatScalarValue = (payload) => {
    if (payload.unit === "元") return formatWan(payload.value);
    if (payload.unit === "人") return formatCount(payload.value);
    return String(payload.value);
  };

  /* 表格单元格：null/undefined → 「—」；wan → 元转万；percent → 比率
   * 转百分比（1 位小数）；ratio → 2 位小数；缺省 → 原样字符串。 */
  const formatCell = (value, format) => {
    if (value === null || value === undefined) return PLACEHOLDER;
    if (format === "wan") return formatWan(value);
    if (format === "percent") return (value * 100).toFixed(1) + "%";
    if (format === "ratio") return Number(value).toFixed(2);
    return String(value);
  };

  /* 达成率 → 进度条百分比；rate 为 null（目标为 0）时返回 null，调用方展示「—」。 */
  const progressPercent = (rate) => {
    if (rate === null || rate === undefined || !isFinite(rate)) return null;
    return Math.min(100, Math.round(rate * 100));
  };

  /* 环比 → {text, up, down}；缺前值/前值为 0（delta_pct=null）→ null。 */
  const deltaPercent = (deltaPct) => {
    if (deltaPct === null || deltaPct === undefined || !isFinite(deltaPct)) {
      return null;
    }
    const rounded = Math.round(deltaPct * 1000) / 10;
    const sign = rounded > 0 ? "+" : "";
    return {
      text: `${sign}${rounded.toFixed(1)}%`,
      up: rounded > 0,
      down: rounded < 0,
    };
  };

  /* ---- echarts 封装 ------------------------------------------------------- */

  /* 返回（或首次创建）该卡体的图表实例。必须先挂 .chart 再 init：
   * echarts 在 init() 时快照容器尺寸，空 .card-body 只有 min-height
   * 140px；先加类、随后 init 内部读取 clientWidth/clientHeight 会触发
   * 同步回流，快照到的才是 .chart 的 280px（否则图表永远半高）。 */
  const ensureChart = (body) => {
    body.classList.add("chart");
    return echarts.getInstanceByDom(body) || echarts.init(body);
  };

  /* 卡体上若挂着图表实例则释放（scalar / table / 错误态复用同一卡体）。 */
  const disposeChart = (body) => {
    if (body.classList.contains("chart")) {
      const chart = echarts.getInstanceByDom(body);
      if (chart) chart.dispose();
      body.classList.remove("chart");
    }
  };

  /* ---- 标量 KPI ---------------------------------------------------------- */

  /* 日环比次行：前一日 X · 环比 ±X%（升绿降红）· 数据日，下挂 7 日
   * 迷你趋势（connectNulls:false——缺数日断线，null = 无数）。 */
  const renderDodSub = (body, payload) => {
    const sub = document.createElement("div");
    sub.className = "kpi-sub";

    const prevText = payload.prev === null || payload.prev === undefined
      ? PLACEHOLDER
      : formatWan(payload.prev);
    const prev = document.createElement("span");
    prev.textContent = `前一日 ${prevText}`;
    sub.appendChild(prev);

    const delta = deltaPercent(payload.delta_pct);
    if (delta !== null) {
      const arrow = document.createElement("span");
      arrow.className = `delta${delta.up ? " up" : delta.down ? " down" : ""}`;
      arrow.textContent = `环比 ${delta.text}`;
      sub.appendChild(arrow);
    }

    if (payload.date) {
      const date = document.createElement("span");
      date.className = "kpi-date";
      date.textContent = payload.date;
      sub.appendChild(date);
    }
    body.appendChild(sub);

    if (Array.isArray(payload.trend7) && payload.trend7.length) {
      const trend = document.createElement("div");
      trend.className = "trend7";
      body.appendChild(trend);
      /* 同 ensureChart 的快照约束：.trend7 高度由 CSS 先定死再 init。 */
      const chart = echarts.getInstanceByDom(trend) || echarts.init(trend);
      chart.setOption(
        {
          grid: { left: 8, right: 8, top: 8, bottom: 8 },
          xAxis: {
            type: "category",
            show: false,
            data: payload.trend7.map((point) => point.date),
          },
          yAxis: { type: "value", show: false },
          series: [
            {
              type: "line",
              showSymbol: false,
              connectNulls: false,
              data: payload.trend7.map((point) =>
                point.value === null ? null : point.value
              ),
            },
          ],
        },
        true
      );
    }
  };

  const renderScalar = (body, payload) => {
    disposeChart(body);
    body.textContent = "";

    const value = document.createElement("div");
    value.className = "kpi-value";
    value.textContent = formatScalarValue(payload);
    body.appendChild(value);

    if (payload.date !== undefined) {
      renderDodSub(body, payload);
      return;
    }

    if (payload.target === undefined || payload.target === null) return;

    const percent = progressPercent(payload.rate);
    const sub = document.createElement("div");
    sub.className = "kpi-sub";
    sub.textContent = percent === null
      ? `目标 ${formatWan(payload.target)} · 达成 ${PLACEHOLDER}`
      : `目标 ${formatWan(payload.target)} · 达成 ${percent}%`;
    body.appendChild(sub);

    const track = document.createElement("div");
    track.className = "progress";
    const bar = document.createElement("div");
    bar.className = "progress-bar";
    bar.style.width = `${percent === null ? 0 : percent}%`;
    track.appendChild(bar);
    body.appendChild(track);
  };

  /* ---- 折线 / 柱状 -------------------------------------------------------- */

  const renderLine = (body, payload) => {
    ensureChart(body).setOption({
      tooltip: { trigger: "axis", valueFormatter: formatWan },
      legend: { top: 0 },
      grid: { left: 8, right: 16, top: 36, bottom: 8, containLabel: true },
      xAxis: { type: "category", boundaryGap: false, data: payload.dates },
      yAxis: { type: "value", axisLabel: { formatter: formatWan } },
      series: payload.series.map((entry) => ({
        name: entry.name,
        type: "line",
        showSymbol: false,
        data: entry.data,
      })),
    }, true);
  };

  /* bar 点击下钻：设参数（同步下拉）→ replaceState → 全卡重拉。 */
  const wireDrill = (card, body) => {
    const param = card.dataset.onclickParam;
    if (!param) return;
    const chart = echarts.getInstanceByDom(body);
    if (!chart) return;
    chart.off("click"); /* 轮询会复用实例，先解绑避免 handler 叠加 */
    chart.on("click", (event) => {
      if (event && event.name) setParam(param, String(event.name));
    });
  };

  const renderBar = (card, body, payload) => {
    ensureChart(body).setOption({
      tooltip: { valueFormatter: formatWan },
      grid: { left: 8, right: 24, top: 8, bottom: 8, containLabel: true },
      // categories 已按金额降序返回；inverse: true 让第一名排在最上方。
      xAxis: { type: "value", axisLabel: { formatter: formatWan } },
      yAxis: { type: "category", inverse: true, data: payload.categories },
      series: [{ type: "bar", barMaxWidth: 28, data: payload.values }],
    }, true);
    wireDrill(card, body);
  };

  /* ---- 表格 ---------------------------------------------------------------- */

  const renderTable = (body, payload) => {
    disposeChart(body);
    body.textContent = "";

    const columns = payload.columns || [];
    const table = document.createElement("table");
    table.className = "data-table";

    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    columns.forEach((column) => {
      const th = document.createElement("th");
      th.textContent = column.title;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    if (payload.rows && payload.rows.length) {
      payload.rows.forEach((row) => {
        const tr = document.createElement("tr");
        columns.forEach((column) => {
          const td = document.createElement("td");
          td.textContent = formatCell(row[column.key], column.format);
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
    } else {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.className = "empty-cell";
      td.colSpan = columns.length;
      td.textContent = EMPTY_TEXT;
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    body.appendChild(table);
  };

  /* ---- 分派与失败态 -------------------------------------------------------- */

  const showError = (body) => {
    disposeChart(body);
    body.textContent = "";
    const box = document.createElement("div");
    box.className = "error";
    box.textContent = ERROR_TEXT;
    body.appendChild(box);
  };

  const render = (card, body, payload) => {
    if (payload && payload.chart === "scalar") return renderScalar(body, payload);
    if (payload && payload.chart === "line") return renderLine(body, payload);
    if (payload && payload.chart === "bar") return renderBar(card, body, payload);
    if (payload && payload.chart === "table") return renderTable(body, payload);
    showError(body); // 未知 chart 类型（前端只实现 scalar/line/bar/table）
  };

  /* ---- URL 参数与取数 ------------------------------------------------------ */

  /* 当前页 URL 参数（每次现取：轮询沿用筛选后的 URL）。 */
  const pageParams = () => new URLSearchParams(window.location.search);

  /* 某张卡实际要带的参数：页面参数 ∩ 该卡 data-params 白名单。 */
  const cardParams = (card) => {
    const allowed = (card.dataset.params || "").split(/\s+/).filter(Boolean);
    const current = pageParams();
    const params = new URLSearchParams();
    allowed.forEach((name) => {
      const value = current.get(name);
      if (value !== null) params.set(name, value);
    });
    return params;
  };

  const cardUrl = (card) => {
    const query = cardParams(card).toString();
    return query ? `${card.dataset.api}?${query}` : card.dataset.api;
  };

  const loadCard = (card) => {
    const body = card.querySelector(".card-body");
    fetch(cardUrl(card), { cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((payload) => render(card, body, payload))
      .catch(() => showError(body));
  };

  /* ---- 筛选下拉与 URL 同步 -------------------------------------------------- */

  /* URL → 下拉框选中项（空值 = 全部）。 */
  const syncSelects = () => {
    const current = pageParams();
    document.querySelectorAll(".filters select[data-param]").forEach((select) => {
      select.value = current.get(select.dataset.param) || "";
    });
  };

  /* 设/清一个参数 → replaceState 不刷页 → 全卡重拉 → 下拉同步。 */
  const setParam = (name, value) => {
    const params = pageParams();
    if (value === null || value === "") params.delete(name);
    else params.set(name, value);
    const query = params.toString();
    window.history.replaceState(
      null,
      "",
      query ? `?${query}` : window.location.pathname
    );
    document.querySelectorAll(".card").forEach(loadCard);
    syncSelects();
  };

  const wireFilters = () => {
    document.querySelectorAll(".filters select[data-param]").forEach((select) => {
      select.addEventListener("change", () => {
        setParam(select.dataset.param, select.value);
      });
    });
  };

  /* ---- 启动与轮询 ---------------------------------------------------------- */

  const refreshSeconds = () => {
    const raw = parseInt(
      document.querySelector("[data-refresh-seconds]")?.dataset.refreshSeconds,
      10,
    );
    return Number.isFinite(raw) ? raw : DEFAULT_REFRESH_SECONDS;
  };

  /* 窗口尺寸变化（含加载后跨 900px 断点）时把在用图表实例同步到新画布
   * 尺寸——echarts 实例不会自动跟随容器。CDN 未加载（无全局 echarts）
   * 或卡体暂无实例时静默跳过，绝不抛错。 */
  const resizeCharts = () => {
    document.querySelectorAll(".card-body.chart").forEach((body) => {
      const chart = window.echarts && window.echarts.getInstanceByDom(body);
      if (chart) chart.resize();
    });
  };

  const start = () => {
    wireFilters();
    syncSelects();
    const cards = Array.from(document.querySelectorAll(".card"));
    cards.forEach(loadCard);
    setInterval(() => cards.forEach(loadCard), refreshSeconds() * 1000);
    window.addEventListener("resize", resizeCharts);
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
```

（与阶段 A 的逐段对照：`renderScalar` 只多了 `payload.date` 分派与 `formatScalarValue`；`renderLine` 原样；`renderBar` 加 `wireDrill` 且签名多收 `card`；`render` 分派多 `table` 分支与 `card` 透传；`loadCard` 的 URL 从 `card.dataset.api` 改为 `cardUrl(card)`；其余原样。）

- [ ] **Step 7: GREEN — style.css 文件末尾追加**

```css
/* ---- 阶段 B：导航 / 筛选 / 表格 / 环比 / 迷你趋势 -------------------- */

.topnav {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 10px 16px;
  background: var(--card-bg);
  border-bottom: 1px solid var(--card-border);
}

.topnav-link {
  padding: 6px 14px;
  border-radius: 999px;
  color: var(--text-muted);
  text-decoration: none;
  font-size: 14px;
}

.topnav-link:hover {
  color: var(--accent);
}

.topnav-link.current {
  background: var(--accent);
  color: #ffffff;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  padding: 12px 16px 0;
  max-width: 1480px;
  margin: 0 auto;
}

.filter {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.filter-label {
  font-size: 13px;
  color: var(--text-muted);
}

.filter select {
  padding: 4px 8px;
  border: 1px solid var(--card-border);
  border-radius: 6px;
  background: var(--card-bg);
  color: var(--text);
  font: inherit;
}

.data-table {
  width: 100%;
  border-collapse: collapse;
  font-variant-numeric: tabular-nums;
}

.data-table th,
.data-table td {
  padding: 6px 10px;
  text-align: right;
  border-bottom: 1px solid var(--card-border);
  white-space: nowrap;
}

.data-table th:first-child,
.data-table td:first-child {
  text-align: left;
}

.data-table th {
  color: var(--text-muted);
  font-weight: 600;
}

.empty-cell {
  text-align: center !important;
  color: var(--text-muted);
  padding: 24px 0;
}

.delta {
  margin-left: 10px;
}

.delta.up {
  color: #067647; /* 升绿 */
}

.delta.down {
  color: var(--error-fg); /* 降红 */
}

.kpi-date {
  margin-left: 10px;
  color: var(--text-muted);
}

/* 迷你趋势：echarts init 前高度必须确定（init 快照容器尺寸）。 */
.trend7 {
  margin-top: 8px;
  height: 56px;
}
```

- [ ] **Step 8: 运行确认前端层 GREEN**

```bash
python -m unittest tests.common.test_bi_web_app tests.common.test_bi_web_cards tests.common.test_bi_web_config tests.common.test_bi_web_queries -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`OK`（JS/CSS 是静态资源，宿主机测试只确认服务与模板零回归；`test_serves_style_css`/`test_serves_dashboard_js` 仍 200）。

- [ ] **Step 9: JS 行为验收推迟说明**

JS 运行时行为（筛选联动、参数交集取数、环比色、迷你趋势断线、表格格式化、bar 下钻、轮询沿用 URL）无法在宿主机 unittest 覆盖，验收放在两处：
- Task 11 集成层：TestClient 断言带参 API 的 JSON payload（含 `date`/`prev`/`delta_pct`/`trend7` 与 table `columns[].format`），即 JS 消费的数据契约；
- Task 12 浏览器冒烟：真实浏览器逐条走 spec §9 矩阵。

### Task 10: bi.seed.yaml 四页面形态

**Files:**
- Modify: `docker/integration/bi.seed.yaml`（整文件替换：L1 增补两卡 + 三个 L2 页）
- Test: `tests/common/test_bi_web_config.py`（`BiSeedFileTests` 断言改四页面新形态）

**布局决策（spec §3 卡片表 + §5 导航序）：**
- l1-cockpit：dod 两卡 span 6+6 置顶（「每天开板先看环比」），原五卡不动 → 7 卡 spans `(6, 6, 4, 4, 4, 8, 4)`，`nav_order: 0`
- l2-region（nav_order 10，region+month）：kpi_region_mtd 4 + trend_region_daily 8 + bar_department_mtd 12
- l2-channel（nav_order 20，channel+month）：bar_channel_mtd 4（`on_click: {param: channel}`）+ trend_channel_daily 8 + table_channel_mtd 6 + table_store_mtd 6
- l2-people（nav_order 30，region+month）：三 scalar 各 4 + table_people_leaderboard 12

**前置事实：** 本任务执行时 Task 7 已把 REGISTRY 扩到 16 卡，`validate_dashboard_config` 能认全部新卡；Task 1 的 `KNOWN_FILTER_SOURCES` 认 `regions`/`channels`/`months` 三个 source。seed 改动要在容器里生效需重建 bi-web 镜像（Dockerfile COPY 源码）——统一放到 Task 11 的重建步骤，本任务只跑宿主机测试。

---

- [ ] **Step 1: RED — BiSeedFileTests 改四页面断言**

`tests/common/test_bi_web_config.py` 中 `BiSeedFileTests` 整类替换为：

```python
class BiSeedFileTests(unittest.TestCase):
    """The shipped seed must not drift from the card registry.

    Reads the repository file directly (the ``calendar.seed.json``
    precedent): parsing succeeds, every placed card id exists in the real
    ``REGISTRY``, every filter source is known, and each page's card
    sequence / spans / filters / nav_order match the shipped stage-B
    layout -- so seed/registry drift is a red build, not a 503 at
    startup.
    """

    def test_seed_parses_places_only_registry_cards_with_the_planned_spans(self):
        mapping = load_seed(_REPO_BI_SEED_PATH)

        self.assertEqual(
            {"l1-cockpit", "l2-region", "l2-channel", "l2-people"}, set(mapping)
        )
        configs = {
            dashboard_id: parse_dashboard_config(dashboard_id, mapping[dashboard_id])
            for dashboard_id in mapping
        }
        for config in configs.values():
            validate_dashboard_config(config, REGISTRY)

        l1 = configs["l1-cockpit"]
        self.assertEqual(0, l1.nav_order)
        self.assertEqual((), l1.filters)
        self.assertEqual(7, len(l1.cards))
        self.assertEqual(
            ("kpi_offline_dod", "kpi_channel_dod", "kpi_offline_mtd",
             "kpi_channel_mtd", "kpi_annual_progress", "trend_region_daily",
             "bar_channel_mtd"),
            tuple(placement.card for placement in l1.cards),
        )
        self.assertEqual(
            (6, 6, 4, 4, 4, 8, 4),
            tuple(placement.span for placement in l1.cards),
        )

        region = configs["l2-region"]
        self.assertEqual(10, region.nav_order)
        self.assertEqual(
            (("region", "regions", "区域"), ("month", "months", "月份")),
            tuple(
                (spec.param, spec.source, spec.label) for spec in region.filters
            ),
        )
        self.assertEqual(
            ("kpi_region_mtd", "trend_region_daily", "bar_department_mtd"),
            tuple(placement.card for placement in region.cards),
        )
        self.assertEqual(
            (4, 8, 12), tuple(placement.span for placement in region.cards)
        )

        channel = configs["l2-channel"]
        self.assertEqual(20, channel.nav_order)
        self.assertEqual(
            (("channel", "channels", "渠道"), ("month", "months", "月份")),
            tuple(
                (spec.param, spec.source, spec.label) for spec in channel.filters
            ),
        )
        self.assertEqual(
            ("bar_channel_mtd", "trend_channel_daily", "table_channel_mtd",
             "table_store_mtd"),
            tuple(placement.card for placement in channel.cards),
        )
        self.assertEqual("channel", channel.cards[0].on_click)
        self.assertIsNone(channel.cards[1].on_click)
        self.assertEqual(
            (4, 8, 6, 6), tuple(placement.span for placement in channel.cards)
        )

        people = configs["l2-people"]
        self.assertEqual(30, people.nav_order)
        self.assertEqual(
            (("region", "regions", "区域"), ("month", "months", "月份")),
            tuple(
                (spec.param, spec.source, spec.label) for spec in people.filters
            ),
        )
        self.assertEqual(
            ("kpi_people_count", "kpi_people_completed", "kpi_people_rate",
             "table_people_leaderboard"),
            tuple(placement.card for placement in people.cards),
        )
        self.assertEqual(
            (4, 4, 4, 12), tuple(placement.span for placement in people.cards)
        )
```

- [ ] **Step 2: 运行确认 RED**

```bash
python -m unittest tests.common.test_bi_web_config.BiSeedFileTests -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`FAILED`——seed 只有 `l1-cockpit` 一个键，`{"l1-cockpit", "l2-region", "l2-channel", "l2-people"} != {"l1-cockpit"}`。

- [ ] **Step 3: GREEN — bi.seed.yaml 整文件替换**

```yaml
# 看板编排种子：卡片口径在 common/bi_web/cards.py（代码评审）；本文件只回答
# 「哪个页面摆哪些卡、什么标题、什么布局、是否启用」。发布进 Nacos group=BI。
# 阶段 B：nav_order 驱动顶部导航；filters 驱动筛选条；on_click 驱动 bar 下钻。
l1-cockpit:
  title: "首页驾驶舱"
  enabled: true
  refresh_seconds: 300
  nav_order: 0
  cards:
    - card: kpi_offline_dod
      title: "线下日环比"
      span: 6
    - card: kpi_channel_dod
      title: "电商渠道日环比"
      span: 6
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
l2-region:
  title: "区域下钻"
  enabled: true
  refresh_seconds: 300
  nav_order: 10
  filters:
    - {param: region, source: regions, label: "区域"}
    - {param: month, source: months, label: "月份"}
  cards:
    - card: kpi_region_mtd
      title: "区域本月累计销售"
      span: 4
    - card: trend_region_daily
      title: "区域日销趋势"
      span: 8
    - card: bar_department_mtd
      title: "部门本月排行"
      span: 12
l2-channel:
  title: "渠道明细"
  enabled: true
  refresh_seconds: 300
  nav_order: 20
  filters:
    - {param: channel, source: channels, label: "渠道"}
    - {param: month, source: months, label: "月份"}
  cards:
    - card: bar_channel_mtd
      title: "渠道本月排行"
      span: 4
      on_click: {param: channel}
    - card: trend_channel_daily
      title: "渠道日销趋势"
      span: 8
    - card: table_channel_mtd
      title: "渠道本月对比"
      span: 6
    - card: table_store_mtd
      title: "店铺本月排行"
      span: 6
l2-people:
  title: "人员榜"
  enabled: true
  refresh_seconds: 300
  nav_order: 30
  filters:
    - {param: region, source: regions, label: "区域"}
    - {param: month, source: months, label: "月份"}
  cards:
    - card: kpi_people_count
      title: "参与人数"
      span: 4
    - card: kpi_people_completed
      title: "本月完成额"
      span: 4
    - card: kpi_people_rate
      title: "总达成率"
      span: 4
    - card: table_people_leaderboard
      title: "人员销售榜"
      span: 12
```

- [ ] **Step 4: 运行确认 GREEN**

```bash
python -m unittest tests.common.test_bi_web_config -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`OK`（BiSeedFileTests 四页面断言通过；同文件既有解析/枚举/fail-open 测试零回归——L1 解析测试用的内联 YAML 不受仓库 seed 改动影响）。

### Task 11: 集成测试层（真实 MySQL 差值法对拍 + 全栈冒烟扩充）

**Files:**
- Modify: `tests/common/test_bi_web_queries.py`（集成段）
- Modify: `tests/common/test_bi_web_app.py`（集成段）

**本任务性质：验证层，不是新功能层。** 取数与装配已在 Task 3-9 交付并被宿主单测锁定；本层用真实 MySQL 锁死它们的**执行正确性**——尤其是 pymysql `%%` 双写：宿主 FakeCursor 只记录不做 `sql % params` 格式化，漏写双写只有这层会炸（`ValueError: unsupported format character` 或合计行漏进合计）。含字面 `%` 的带参 SQL 共五条：`region_mtd_total` / `region_month_target` / `department_mtd_ranking` / `region_month_daily_series` / `_OFFLINE_DOD_WINDOW_SQL`（均 `%%合计%%`），Task 11 的测试逐一覆盖。容器跑挂了就回 `queries.py` 修双写——这正是本任务存在的意义。

**差值法新技法（沿用四原则：断言只看 fixture 增量、种植合计/未来/预填行证明它们不改结果、绝不耦合既有数据、teardown 清理）：**

1. **区域级差值**：断言只针对 fixture 专属的 region（`biweb甲`/`biweb乙`/`biweb丙`）与 channel（`biweb测试渠道*`）——真实 mart 不可能有这些名字，所以全表 GROUP BY 结果里「我们的行」完全确定，可做绝对断言。
2. **DoD 差值**：latest 是全局 `MAX(business_date) <= CURDATE()`，今日插行后 latest 必为 today（真实数据不可能越界未来）。value 断言用 `base + 增量`，base = before 的 value 当其 date 已是 today，否则 0（今日无真实行）。
3. **人员榜对拍（oracle 设计）**：直接调 `mart_collect` 作基准，run 函数载荷与它逐字段相等；同时先**读**真实 `dim_calendar` 取已知工作日与全月工作日数，获得 completed/unfilled 的绝对断言。绝不写 `dim_calendar`（共享真实数据，只读）。
4. **app 层 fresh client 技法**：`_TTLOptionSets` 是 per-app 实例（Task 8），`_fresh_client()` 在 fixture 插入后新建 app——值闸必读到插入后的维表，测试与执行顺序解耦。
5. **月末边界**：today 为当月最后一天时 tomorrow 落次月——月目标测试用条件期望（melt 陷阱断言不受影响），渠道/店铺测试的未来行落哪个月都被 BETWEEN 或 CURDATE 排除，无需特判。

**既有测试零改动**：fixture 层做成分层 helper（旧 5 元组/4 元组签名保留、内部补 None 委托给新 full 版），既有 5 个差值法测试一行不动。清理 SQL 是行级 DELETE，与列集无关，无需改动。

- [ ] **Step 1: queries 侧 fixture 层扩充**

`tests/common/test_bi_web_queries.py` 集成段（`BiWebQueriesIntegrationTests` 之前的常量区与类体）四处改动：

（1）import 区追加一行（`mart_collect` 作对拍 oracle；`bi_web_queries` 别名 Task 6 已引入）：

```python
from common.daily_robot.mart_leaderboard import mart_collect
```

（2）两条 INSERT 常量整体替换（新增 `department`/`monthly_target` 与 `store_name`/`promotion_cost`，均可空）：

```python
_OFFLINE_INSERT_SQL = (
    "INSERT INTO `fact_daily_report_offline` "
    "(`source_record_id`, `region`, `responsible_person`, `business_date`, "
    "`sales_amount`, `department`, `monthly_target`, `synced_at`, `sync_run_id`) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(6), %s)"
)

_CHANNEL_INSERT_SQL = (
    "INSERT INTO `fact_channel_daily_sales` "
    "(`source_record_id`, `channel`, `business_date`, `sales_amount`, "
    "`store_name`, `promotion_cost`, `synced_at`, `sync_run_id`) "
    "VALUES (%s, %s, %s, %s, %s, %s, NOW(6), %s)"
)
```

（3）两条清理常量不动（行级 DELETE 按 `source_record_id` 前缀 + region/channel 谓词，新列不影响覆盖面）。

（4）`BiWebQueriesIntegrationTests` 类体：`setUpClass` 之后加 `setUp`（人员缓存隔离，全局约定）；`_insert_channel_rows` 之后加分层 helper 与三个读维表助手：

```python
    def setUp(self):
        bi_web_queries._PEOPLE_CACHE.clear()

    def _insert_offline_rows(self, rows):
        """rows: (suffix, region, responsible_person, business_date, amount)."""
        self._insert_offline_rows_full(
            [
                (suffix, region, person, business_date, amount, None, None)
                for suffix, region, person, business_date, amount in rows
            ]
        )

    def _insert_offline_rows_full(self, rows):
        """rows: (suffix, region, responsible_person, business_date, amount,
        department, monthly_target)。"""
        with transaction(self.mart_connection):
            with self.mart_connection.cursor() as cursor:
                cursor.executemany(
                    _OFFLINE_INSERT_SQL,
                    [
                        (
                            _FIXTURE_PREFIX + suffix,
                            region,
                            responsible_person,
                            business_date,
                            sales_amount,
                            department,
                            monthly_target,
                            _SYNC_RUN_ID,
                        )
                        for (
                            suffix,
                            region,
                            responsible_person,
                            business_date,
                            sales_amount,
                            department,
                            monthly_target,
                        ) in rows
                    ],
                )

    def _insert_channel_rows_full(self, rows):
        """rows: (suffix, channel, business_date, amount, store_name,
        promotion_cost)。"""
        with transaction(self.mart_connection):
            with self.mart_connection.cursor() as cursor:
                cursor.executemany(
                    _CHANNEL_INSERT_SQL,
                    [
                        (
                            _FIXTURE_PREFIX + suffix,
                            channel,
                            business_date,
                            sales_amount,
                            store_name,
                            promotion_cost,
                            _SYNC_RUN_ID,
                        )
                        for (
                            suffix,
                            channel,
                            business_date,
                            sales_amount,
                            store_name,
                            promotion_cost,
                        ) in rows
                    ],
                )

    def _current_month_bounds(self):
        """服务器时钟的当前月 → (month 串, (月首, 月末))。"""
        month = self._server_month_start().strftime("%Y-%m")
        return month, month_bounds(month)

    def _first_workday_of(self, first_day, last_day):
        """真实 dim_calendar 的首个工作日；None 即该月无日历行。"""
        with self.mart_connection.cursor() as cursor:
            cursor.execute(
                "SELECT business_date FROM dim_calendar "
                "WHERE is_workday = 1 AND business_date BETWEEN %s AND %s "
                "ORDER BY business_date LIMIT 1",
                (first_day, last_day),
            )
            row = cursor.fetchone()
        return None if row is None else row["business_date"]

    def _workday_count(self, first_day, last_day):
        """真实 dim_calendar 的当月工作日数（unfilled 绝对断言的基数）。"""
        with self.mart_connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM dim_calendar "
                "WHERE is_workday = 1 AND business_date BETWEEN %s AND %s",
                (first_day, last_day),
            )
            return int(cursor.fetchone()["n"])
```

- [ ] **Step 2: queries 侧八个新差值法测试**

`BiWebQueriesIntegrationTests` 末尾（`test_region_daily_series_alignment_and_zero_fill` 之后）追加：

```python
    def test_region_month_target_max_per_person_and_mtd_truncation(self):
        today = self._server_today()
        tomorrow = today + timedelta(days=1)
        month, (first_day, last_day) = self._current_month_bounds()

        self._insert_offline_rows_full(
            [
                ("melt-past-1", "biweb甲", "biweb甲人员", today, Decimal("100"), "biweb一部", Decimal("1000")),
                ("melt-past-2", "biweb甲", "biweb甲人员", today, Decimal("50"), "biweb一部", Decimal("3000")),
                ("melt-future", "biweb甲", "biweb甲人员", tomorrow, Decimal("888888"), "biweb一部", Decimal("5000")),
                ("melt-summary", "biweb甲", "biweb甲合计", today, Decimal("999999"), None, Decimal("999999")),
            ]
        )

        # melt 陷阱：Σ各人员 MAX(monthly_target)，绝不跨行 SUM（SUM=9000）。
        # 月末边界：tomorrow 落次月时被 BETWEEN 排除，期望退为 3000
        # （melt 陷阱断言 3000 vs 4000 依旧成立，CURDATE 截断证明当日不足）。
        expected_target = Decimal("5000") if tomorrow <= last_day else Decimal("3000")
        self.assertEqual(
            expected_target,
            region_month_target(
                self.mart_connection, region="biweb甲", first_day=first_day, last_day=last_day
            ),
        )
        # 取数对称面：Σsales 截断未来 + 排除合计（无排除会是 1000149）。
        self.assertEqual(
            Decimal("150"),
            region_mtd_total(
                self.mart_connection, region="biweb甲", first_day=first_day, last_day=last_day
            ),
        )

        payload = run_kpi_region_mtd(
            self.mart_connection, {"region": "biweb甲", "month": month}
        )
        self.assertEqual("scalar", payload["chart"])
        self.assertEqual(150.0, payload["value"])
        self.assertEqual(float(expected_target), payload["target"])
        self.assertEqual(150.0 / float(expected_target), payload["rate"])
        self.assertEqual("元", payload["unit"])

    def test_department_mtd_ranking_orders_desc_and_labels_null_dept(self):
        today = self._server_today()
        month, (first_day, last_day) = self._current_month_bounds()

        self._insert_offline_rows_full(
            [
                ("dept-a", "biweb乙", "biweb乙甲", today, Decimal("300"), "biweb乙一部", None),
                ("dept-b", "biweb乙", "biweb乙乙", today, Decimal("100"), "biweb乙二部", None),
                ("dept-c", "biweb乙", "biweb乙丙", today, Decimal("50"), None, None),
                ("dept-sum", "biweb乙", "biweb乙合计", today, Decimal("999999"), "biweb乙一部", None),
            ]
        )

        ranking = department_mtd_ranking(
            self.mart_connection, region="biweb乙", first_day=first_day, last_day=last_day
        )
        self.assertEqual(["biweb乙一部", "biweb乙二部", "未分组"], ranking["categories"])
        self.assertEqual([Decimal("300"), Decimal("100"), Decimal("50")], ranking["values"])

        payload = run_bar_department_mtd(
            self.mart_connection, {"region": "biweb乙", "month": month}
        )
        self.assertEqual("bar", payload["chart"])
        self.assertEqual(["biweb乙一部", "biweb乙二部", "未分组"], payload["categories"])
        self.assertEqual([300.0, 100.0, 50.0], payload["values"])
        self.assertEqual("元", payload["unit"])

    def test_offline_dod_latest_becomes_today_with_consistent_delta(self):
        today = self._server_today()
        before = offline_dod(self.mart_connection)
        base = before["value"] if before and before["date"] == today.isoformat() else 0.0

        self._insert_offline_rows_full(
            [
                ("dod-normal", "biweb丙", "biweb丙人员", today, Decimal("100"), None, None),
                ("dod-summary", "biweb丙", "biweb丙合计", today, Decimal("999999"), None, None),
            ]
        )

        after = offline_dod(self.mart_connection)
        # 今日插行 → 全局 latest 必为 today；value = base + 100（合计行被排除）。
        self.assertEqual(today.isoformat(), after["date"])
        self.assertAlmostEqual(base + 100.0, after["value"], places=2)
        # trend7：7 个条目、ISO 日期自 6 天前递增到今天。
        self.assertEqual(7, len(after["trend7"]))
        self.assertEqual(
            [(today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)],
            [entry["date"] for entry in after["trend7"]],
        )
        # delta_pct 内部一致性（prev 取决于真实数据，条件断言）。
        if after["prev"] not in (None, 0.0):
            self.assertAlmostEqual(
                (after["value"] - after["prev"]) / after["prev"],
                after["delta_pct"],
                places=9,
            )

        payload = run_kpi_offline_dod(self.mart_connection, {})
        self.assertEqual("scalar", payload["chart"])
        self.assertEqual(after["value"], payload["value"])
        self.assertEqual(after["date"], payload["date"])
        self.assertEqual(after["trend7"], payload["trend7"])
        self.assertEqual("元", payload["unit"])

    def test_channel_dod_and_run_payload(self):
        today = self._server_today()
        before = channel_dod(self.mart_connection)
        base = before["value"] if before and before["date"] == today.isoformat() else 0.0

        self._insert_channel_rows_full(
            [("dod-channel", "biweb测试渠道", today, Decimal("100"), None, None)]
        )

        after = channel_dod(self.mart_connection)
        self.assertEqual(today.isoformat(), after["date"])
        self.assertAlmostEqual(base + 100.0, after["value"], places=2)

        payload = run_kpi_channel_dod(self.mart_connection, {})
        self.assertEqual("scalar", payload["chart"])
        self.assertEqual(after["value"], payload["value"])
        self.assertEqual("元", payload["unit"])

    def test_run_table_channel_mtd_aggregates_promo_and_roi(self):
        today = self._server_today()
        tomorrow = today + timedelta(days=1)
        month, _ = self._current_month_bounds()

        self._insert_channel_rows_full(
            [
                ("cmp-a", "biweb测试渠道", today, Decimal("200"), "biweb店铺A", Decimal("100")),
                ("cmp-b", "biweb测试渠道", today, Decimal("100"), "biweb店铺B", None),
                ("cmp-zero", "biweb测试渠道零", today, Decimal("100"), "biweb店铺C", Decimal("0")),
                ("cmp-null", "biweb测试渠道空", today, Decimal("100"), "biweb店铺D", None),
                ("cmp-future", "biweb测试渠道", tomorrow, Decimal("777"), "biweb店铺A", Decimal("1")),
            ]
        )

        payload = run_table_channel_mtd(self.mart_connection, {"month": month})
        self.assertEqual("table", payload["chart"])
        self.assertEqual(
            ["channel", "sales", "promo", "roi", "stores"],
            [column["key"] for column in payload["columns"]],
        )
        rows = {row["channel"]: row for row in payload["rows"]}
        # 组内聚合：Σsales=300（未来行被排除，否则 1077）、Σpromo=100（NULL 不计）；
        # ROI 在 run 层算（Σsales÷Σpromo），绝不取行级 roi 源列。
        self.assertEqual(300.0, rows["biweb测试渠道"]["sales"])
        self.assertEqual(100.0, rows["biweb测试渠道"]["promo"])
        self.assertEqual(3.0, rows["biweb测试渠道"]["roi"])
        self.assertEqual(2, rows["biweb测试渠道"]["stores"])
        # promo=0 → 除零护栏 → roi None；聚合 NULL → promo None → roi None（两条 None 路径）。
        self.assertEqual(100.0, rows["biweb测试渠道零"]["sales"])
        self.assertEqual(0.0, rows["biweb测试渠道零"]["promo"])
        self.assertIsNone(rows["biweb测试渠道零"]["roi"])
        self.assertIsNone(rows["biweb测试渠道空"]["promo"])
        self.assertIsNone(rows["biweb测试渠道空"]["roi"])

    def test_run_table_store_mtd_drills_by_channel_and_switches_columns(self):
        today = self._server_today()
        tomorrow = today + timedelta(days=1)
        month, _ = self._current_month_bounds()

        self._insert_channel_rows_full(
            [
                ("store-a", "biweb测试渠道", today, Decimal("300"), "biweb店铺A", None),
                ("store-b", "biweb测试渠道", today, Decimal("100"), "biweb店铺B", None),
                ("store-c", "biweb测试渠道二", today, Decimal("999"), "biweb店铺C", None),
                ("store-future", "biweb测试渠道", tomorrow, Decimal("888888"), "biweb店铺B", None),
            ]
        )

        drill = run_table_store_mtd(
            self.mart_connection, {"channel": "biweb测试渠道", "month": month}
        )
        self.assertEqual("table", drill["chart"])
        # 指定 channel：fixture 渠道无真实店铺 → 行集完全确定，且无 channel 列。
        self.assertEqual(
            ["rank", "store", "sales"], [column["key"] for column in drill["columns"]]
        )
        self.assertEqual(
            [("biweb店铺A", 1, 300.0), ("biweb店铺B", 2, 100.0)],
            [(row["store"], row["rank"], row["sales"]) for row in drill["rows"]],
        )

        full = run_table_store_mtd(self.mart_connection, {"month": month})
        self.assertIn("channel", [column["key"] for column in full["columns"]])
        rows = {row["store"]: row for row in full["rows"]}
        # 未来行排除：店铺B 停在 100（否则 888988）。
        self.assertEqual(300.0, rows["biweb店铺A"]["sales"])
        self.assertEqual(100.0, rows["biweb店铺B"]["sales"])
        self.assertEqual("biweb测试渠道二", rows["biweb店铺C"]["channel"])
        # 全表行序按 Σsales 降序：真实店铺穿插其间，fixture 三家相对次序确定。
        names = [row["store"] for row in full["rows"]]
        self.assertLess(names.index("biweb店铺C"), names.index("biweb店铺A"))
        self.assertLess(names.index("biweb店铺A"), names.index("biweb店铺B"))

    def test_region_month_daily_series_parameterized_region_filter(self):
        today = self._server_today()
        month, (first_day, last_day) = self._current_month_bounds()

        self._insert_offline_rows_full(
            [
                ("series-r", "biweb甲", "biweb甲人员", today, Decimal("100"), None, None),
                ("series-r-sum", "biweb甲", "biweb甲合计", today, Decimal("999999"), None, None),
            ]
        )

        series = region_month_daily_series(
            self.mart_connection, region="biweb甲", first_day=first_day, last_day=last_day
        )
        self.assertEqual(["biweb甲"], [entry["name"] for entry in series["series"]])
        self.assertEqual([today.strftime("%m-%d")], series["dates"])
        self.assertEqual(len(series["dates"]), len(series["series"][0]["data"]))
        # 合计行被 `%%合计%%` 排除（这条 SQL 是双写证明点之一）。
        self.assertEqual([Decimal("100")], series["series"][0]["data"])

    def test_people_cards_reconcile_with_mart_collect_on_real_calendar(self):
        month_end = self._server_month_start() - timedelta(days=1)
        month = month_end.strftime("%Y-%m")
        first_day = month_end.replace(day=1)
        workday = self._first_workday_of(first_day, month_end)
        if workday is None:
            self.skipTest(f"dim_calendar 无 {month} 日历行（先跑 extract-mart）")
        workdays = self._workday_count(first_day, month_end)

        self._insert_offline_rows_full(
            [
                ("ppl-hz", "杭州", "biweb人员甲", workday, Decimal("100"), "biweb部门", Decimal("1000")),
                ("ppl-sx", "绍兴", "biweb人员乙", workday, Decimal("50"), None, Decimal("100")),
                ("ppl-sum", "杭州", "biweb部门合计", workday, Decimal("9999"), "biweb部门", None),
            ]
        )

        # oracle：直接调 mart_collect（历史月锚点=月末，与 _people_as_of 一致）。
        direct_hz = mart_collect(
            self.mart_connection, region="杭州", business_date=month_end, include_today=True
        ).people
        direct_sx = mart_collect(
            self.mart_connection, region="绍兴", business_date=month_end, include_today=True
        ).people
        by_name = {person["name"]: person for person in direct_hz}
        self.assertIn("biweb人员甲", by_name)
        self.assertNotIn("biweb部门合计", by_name)
        # 绝对口径：已知工作日上一行 → completed 确定值、unfilled = 全月工作日 − 1。
        self.assertEqual(100.0, by_name["biweb人员甲"]["completed"])
        self.assertEqual(workdays - 1, by_name["biweb人员甲"]["unfilled"])
        self.assertAlmostEqual(0.1, by_name["biweb人员甲"]["rate"])

        # run 层对拍：载荷与 mart_collect 逐字段一致（含缓存路径）。
        table = run_table_people_leaderboard(
            self.mart_connection, {"region": "杭州", "month": month}
        )
        rows = {row["name"]: row for row in table["rows"]}
        self.assertIn("biweb人员甲", rows)
        self.assertEqual(by_name["biweb人员甲"]["completed"], rows["biweb人员甲"]["completed"])
        self.assertEqual(by_name["biweb人员甲"]["rate"], rows["biweb人员甲"]["rate"])
        self.assertEqual(by_name["biweb人员甲"]["unfilled"], rows["biweb人员甲"]["unfilled"])
        self.assertEqual("biweb部门", rows["biweb人员甲"]["dept"])

        count = run_kpi_people_count(
            self.mart_connection, {"region": "杭州", "month": month}
        )
        self.assertEqual(float(len(direct_hz)), count["value"])
        self.assertEqual("人", count["unit"])

        completed = run_kpi_people_completed(
            self.mart_connection, {"region": "杭州", "month": month}
        )
        self.assertEqual(
            float(sum(person["completed"] for person in direct_hz)),
            completed["value"],
        )

        rate = run_kpi_people_rate(
            self.mart_connection, {"region": "杭州", "month": month}
        )
        total_completed = sum(person["completed"] for person in direct_hz)
        total_target = sum(person["target"] for person in direct_hz)
        self.assertEqual(float(total_completed), rate["value"])
        self.assertEqual(float(total_target), rate["target"])
        self.assertEqual(total_completed / total_target, rate["rate"])

        # region 缺省：两区合并 + 全局重排（fixture 两人相对次序由 rate 决定）。
        merged = run_table_people_leaderboard(self.mart_connection, {"month": month})
        names = [row["name"] for row in merged["rows"]]
        self.assertIn("biweb人员甲", names)
        self.assertIn("biweb人员乙", names)
        self.assertLess(names.index("biweb人员乙"), names.index("biweb人员甲"))
```

（人员测试的月份锚点说明：历史月 `_people_as_of` 返回月末 = `month_end`，与直接调 `mart_collect` 的 `business_date` 逐字一致；若上月无日历行（新栈）则 skipTest——日历行由 extract-mart 落库，本测试只读不写。）

- [ ] **Step 3: host 冒烟（导入与零回归）**

```bash
python -m unittest tests.common.test_bi_web_queries tests.common.test_bi_web_app tests.common.test_bi_web_config -v | grep -E "^(Ran|OK|FAILED|ERROR)"
```

预期：`OK`（宿主上集成类 skip；import 错误在这一步就会炸出来）。

- [ ] **Step 4: 容器跑 queries 集成**

先重建（Dockerfile `COPY . /app`，测试文件变了必须重建），再跑：

```bash
docker compose -f docker-compose.integration.yml build test-runner
docker compose -f docker-compose.integration.yml run --rm --entrypoint python3 test-runner -m unittest tests.common.test_bi_web_queries -v
```

预期：`OK`——既有 5 个差值法测试 + 新增 8 个全绿。**若 FAILED 且 traceback 含 `ValueError: unsupported format character`**：某条带参 SQL 的字面 `%` 漏了双写，回 `queries.py` 对应函数改 `%%` 后重跑；若合计断言失败（多算了 999999）：`%%合计%%` 拼写或 LIKE 模式有误。MySQL 栈保持运行，不动 compose 服务。

- [ ] **Step 5: app 侧扩充（七卡 L1 + L2 冒烟 + 双闸集成）**

`tests/common/test_bi_web_app.py` 五处改动：

（1）import 区追加两行（`transaction` 宿主可导入；`queries` 别名用于清缓存）：

```python
from common.bi_web import queries as bi_web_queries
from common.public_data.db import transaction
```

（2）集成段常量区（`_RECON_TWO_LINE_TARGET_SQL` 之后）追加：

```python
_STAGE_B_L1_CARD_IDS = (
    "kpi_offline_dod",
    "kpi_channel_dod",
    "kpi_offline_mtd",
    "kpi_channel_mtd",
    "kpi_annual_progress",
    "trend_region_daily",
    "bar_channel_mtd",
)

_APP_FIXTURE_PREFIX = "biweb-app-test:"
_APP_SYNC_RUN_ID = "00000000-0000-0000-0000-000000000002"

_APP_OFFLINE_INSERT_SQL = (
    "INSERT INTO `fact_daily_report_offline` "
    "(`source_record_id`, `region`, `responsible_person`, `business_date`, "
    "`sales_amount`, `synced_at`, `sync_run_id`) "
    "VALUES (%s, %s, %s, %s, %s, NOW(6), %s)"
)

_APP_OFFLINE_CLEANUP_SQL = (
    "DELETE FROM `fact_daily_report_offline` "
    "WHERE `source_record_id` LIKE 'biweb-app-test:%'"
)
```

（3）`BiWebAppIntegrationTests` 类体（本文件自己的集成类，即 `setUpClass` 装配 TestClient 的那个）：`setUpClass` 之后加 `setUp`/`tearDown`/`_server_today`/`_fresh_client`：

```python
    def setUp(self):
        bi_web_queries._PEOPLE_CACHE.clear()

    def tearDown(self):
        with transaction(self.mart_connection):
            with self.mart_connection.cursor() as cursor:
                cursor.execute(_APP_OFFLINE_CLEANUP_SQL)

    def _server_today(self):
        with self.mart_connection.cursor() as cursor:
            cursor.execute("SELECT CURDATE() AS today")
            return cursor.fetchone()["today"]

    def _fresh_client(self):
        """新 app 实例：_TTLOptionSets 按 app 实例持有，必读到插入后的维表。"""
        return TestClient(
            create_app(
                settings=self.settings,
                dashboard_source=FileDashboardSource(_REPO_BI_SEED_PATH),
                registry=REGISTRY,
            )
        )
```

（4）`test_l1_cockpit_page_places_the_five_cards` 整体替换为七卡版；`test_every_card_answers_its_chart_payload_with_no_store` 的 charts 字典整体替换：

```python
    def test_l1_cockpit_page_places_the_seven_cards(self):
        response = self.client.get("/d/l1-cockpit")

        self.assertEqual(200, response.status_code)
        self.assertEqual(7, response.text.count('data-api="/api/d/l1-cockpit/cards/'))
        for card_id in _STAGE_B_L1_CARD_IDS:
            self.assertIn(
                f'data-api="/api/d/l1-cockpit/cards/{card_id}"', response.text
            )
```

```python
        charts = {
            "kpi_offline_dod": "scalar",
            "kpi_channel_dod": "scalar",
            "kpi_offline_mtd": "scalar",
            "kpi_channel_mtd": "scalar",
            "kpi_annual_progress": "scalar",
            "trend_region_daily": "line",
            "bar_channel_mtd": "bar",
        }
```

（5）`test_kpi_annual_progress_rate_reconciles_with_two_line_target` 之后追加三个新测试；`_assert_payload_structure` 整体替换（分支扩到全部 16 卡）：

```python
    def test_every_l2_card_answers_its_chart_payload_with_no_store(self):
        placements = {
            "l2-region": {
                "kpi_region_mtd": "scalar",
                "trend_region_daily": "line",
                "bar_department_mtd": "bar",
            },
            "l2-channel": {
                "bar_channel_mtd": "bar",
                "trend_channel_daily": "line",
                "table_channel_mtd": "table",
                "table_store_mtd": "table",
            },
            "l2-people": {
                "kpi_people_count": "scalar",
                "kpi_people_completed": "scalar",
                "kpi_people_rate": "scalar",
                "table_people_leaderboard": "table",
            },
        }
        for dashboard_id, charts in placements.items():
            for card_id, chart in charts.items():
                with self.subTest(dashboard=dashboard_id, card=card_id):
                    response = self.client.get(
                        f"/api/d/{dashboard_id}/cards/{card_id}"
                    )

                    self.assertEqual(200, response.status_code)
                    self.assertEqual("no-store", response.headers["cache-control"])
                    payload = response.json()
                    self.assertEqual(chart, payload["chart"])
                    self._assert_payload_structure(card_id, payload)

    def test_l2_pages_render_two_filters_and_full_navigation(self):
        card_counts = {"l2-region": 3, "l2-channel": 4, "l2-people": 4}
        for dashboard_id, expected in card_counts.items():
            with self.subTest(dashboard=dashboard_id):
                response = self.client.get(f"/d/{dashboard_id}")

                self.assertEqual(200, response.status_code)
                body = response.text
                self.assertEqual(
                    expected, body.count(f'data-api="/api/d/{dashboard_id}/cards/')
                )
                self.assertEqual(2, body.count("<select"))
                self.assertIn('data-param="region"', body)
                self.assertIn('data-param="month"', body)
                self.assertEqual(1, body.count('class="topnav-link current"'))
                for nav_id in ("l1-cockpit", "l2-region", "l2-channel", "l2-people"):
                    self.assertIn(f'href="/d/{nav_id}"', body)

        channel_body = self.client.get("/d/l2-channel").text
        self.assertIn('data-onclick-param="channel"', channel_body)
        self.assertIn('data-params="channel month"', channel_body)

    def test_l2_api_accepts_valid_params_and_rejects_invalid_values(self):
        today = self._server_today()
        month = today.strftime("%Y-%m")
        with transaction(self.mart_connection):
            with self.mart_connection.cursor() as cursor:
                cursor.executemany(
                    _APP_OFFLINE_INSERT_SQL,
                    [
                        (
                            _APP_FIXTURE_PREFIX + "gate-a",
                            "biweb甲",
                            "biweb甲人员",
                            today,
                            100,
                            _APP_SYNC_RUN_ID,
                        ),
                        (
                            _APP_FIXTURE_PREFIX + "gate-b",
                            "biweb乙",
                            "biweb乙人员",
                            today,
                            0,
                            _APP_SYNC_RUN_ID,
                        ),
                    ],
                )
        client = self._fresh_client()

        # 合法组合：值闸放行 fixture 区域（DISTINCT region 已含 biweb甲/乙）。
        ok = client.get(
            "/api/d/l2-region/cards/kpi_region_mtd",
            params={"region": "biweb甲", "month": month},
        )
        self.assertEqual(200, ok.status_code)
        payload = ok.json()
        self.assertEqual("scalar", payload["chart"])
        self.assertEqual(100.0, payload["value"])
        self.assertEqual(0.0, payload["target"])
        self.assertIsNone(payload["rate"])

        # 无数据组合（spec §8）：零额区域 → 200 空载荷，绝不 400。
        empty = client.get(
            "/api/d/l2-region/cards/kpi_region_mtd",
            params={"region": "biweb乙", "month": month},
        )
        self.assertEqual(200, empty.status_code)
        self.assertEqual(0.0, empty.json()["value"])

        trend = client.get(
            "/api/d/l2-region/cards/trend_region_daily",
            params={"region": "biweb甲", "month": month},
        ).json()
        self.assertEqual("line", trend["chart"])
        self.assertEqual(["biweb甲"], [entry["name"] for entry in trend["series"]])
        self.assertEqual([today.strftime("%m-%d")], trend["dates"])
        self.assertEqual([100.0], trend["series"][0]["data"])

        # 值闸拒绝：不在维表里的值 → 400，安全文案且值绝不回显。
        bad = client.get(
            "/api/d/l2-region/cards/kpi_region_mtd",
            params={"region": "不存在区域"},
        )
        self.assertEqual(400, bad.status_code)
        self.assertEqual("bad_request", bad.json()["detail"])
        self.assertNotIn("不存在区域", bad.text)

        # 键白名单（阶段 A 既有行为）在集成层同样成立。
        unknown = client.get(
            "/api/d/l2-region/cards/kpi_region_mtd", params={"wat": "1"}
        )
        self.assertEqual(400, unknown.status_code)
        self.assertEqual("bad_request", unknown.json()["detail"])
```

```python
    def _assert_payload_structure(self, card_id, payload):
        """Structure and type only: real data may legitimately be zero."""
        if card_id in ("kpi_offline_mtd", "kpi_channel_mtd"):
            self.assertIsInstance(payload["value"], float)
            self.assertEqual("元", payload["unit"])
        elif card_id in ("kpi_offline_dod", "kpi_channel_dod"):
            self.assertIsInstance(payload["value"], float)
            self.assertIsInstance(payload["date"], (str, type(None)))
            self.assertIsInstance(payload["prev"], (float, type(None)))
            self.assertIsInstance(payload["delta_pct"], (float, type(None)))
            self.assertIsInstance(payload["trend7"], list)
            for entry in payload["trend7"]:
                self.assertIsInstance(entry["date"], str)
                self.assertIsInstance(entry["value"], (float, type(None)))
            self.assertEqual("元", payload["unit"])
        elif card_id in (
            "kpi_annual_progress",
            "kpi_region_mtd",
            "kpi_people_rate",
        ):
            self.assertIsInstance(payload["value"], float)
            self.assertIsInstance(payload["target"], float)
            # ``None`` only while dim_target is empty (before load-target).
            self.assertIsInstance(payload["rate"], (float, type(None)))
            self.assertEqual("元", payload["unit"])
        elif card_id == "kpi_people_count":
            self.assertIsInstance(payload["value"], float)
            self.assertEqual("人", payload["unit"])
        elif card_id == "kpi_people_completed":
            self.assertIsInstance(payload["value"], float)
            self.assertEqual("元", payload["unit"])
        elif card_id in ("trend_region_daily", "trend_channel_daily"):
            self.assertIsInstance(payload["dates"], list)
            for entry in payload["series"]:
                self.assertIsInstance(entry["name"], str)
                self.assertEqual(len(payload["dates"]), len(entry["data"]))
                for point in entry["data"]:
                    self.assertIsInstance(point, float)
        elif card_id in ("bar_channel_mtd", "bar_department_mtd"):
            self.assertEqual(len(payload["categories"]), len(payload["values"]))
            for category in payload["categories"]:
                self.assertIsInstance(category, str)
            for value in payload["values"]:
                self.assertIsInstance(value, float)
            self.assertEqual("元", payload["unit"])
        elif card_id in (
            "table_channel_mtd",
            "table_store_mtd",
            "table_people_leaderboard",
        ):
            self.assertIsInstance(payload["columns"], list)
            for column in payload["columns"]:
                self.assertIn("key", column)
                self.assertIn("title", column)
            self.assertIsInstance(payload["rows"], list)
        else:
            self.fail(f"payload structure not asserted for {card_id}")
```

（`else: self.fail(...)` 兜底：注册表再加卡而这里没跟上分支时测试直接红，不静默漏断言。`kpi_region_mtd` 与 `kpi_people_rate` 共用 annual 分支——三者载荷同形：value/target/rate + unit 元。）

- [ ] **Step 6: 容器跑 app 集成**

```bash
docker compose -f docker-compose.integration.yml build test-runner
docker compose -f docker-compose.integration.yml run --rm --entrypoint python3 test-runner -m unittest tests.common.test_bi_web_app -v
```

预期：`OK`——既有 6 个全栈冒烟（五卡测试已改七卡）+ 新增 3 个全绿，对拍 print 行可见。MySQL/bi-web 栈保持运行不重启。

### Task 12: 浏览器冒烟（spec §9 验收矩阵）

**Files:**
- 无代码改动——纯验证任务。这是全计划唯一不做 TDD 的任务：浏览器行为没有宿主测试运行时，JS 契约已由 Task 9 模板测试 + Task 11 TestClient 集成锁定，本任务补最后一层（真实浏览器 + 真实容器 + 真实 CDN ECharts）。
- 产物：对话内截图 + 结果矩阵。**截图不落盘、不入库、不提交**；发现问题回对应任务修代码并重跑其测试，绝不为了过冒烟改 JS 绕过测试。

**铁律（执行者必读）：**
1. Docker 栈**绝不下、绝不重启**：mysql 与其余容器保持原样，本任务只允许重建/重建后启动 `bi-web` 一个服务（Task 8-10 改了 app/模板/JS/seed，镜像 `COPY . /app` 已过期，必须重建）。
2. `BI_WEB_TOKEN` 在 compose 里默认为空 = 免鉴权；浏览器导航无法带 Bearer 头。若冒烟拿到 401，说明启动时 shell 里 export 过它——`unset` 后重建 bi-web 再来（见 Step 1）。
3. 下拉变更与 bar 点击都走 Task 9 的 `setParam`：URL `replaceState` 不刷页 → 全卡按各自 `data-params` 白名单带参重拉 → 下拉同步。冒烟断言的就是这条链路。

**前置：** Task 11 两步容器测试全绿之后再做本任务（冒烟是最后一关）。

---

- [ ] **Step 1: 重建并启动 bi-web（只此一个服务）**

```bash
docker compose -f docker-compose.integration.yml --profile bi-web up -d --build bi-web
```

预期：`bi-web` 镜像重建、容器重启；`mysql` 已在运行且健康，作为依赖只被检查、不被重建（`--profile bi-web` 必带：该服务挂在 profile 下）。

健康与鉴权检查：

```bash
curl -s http://127.0.0.1:18080/healthz
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18080/d/l1-cockpit
```

预期：`{"status": "ok", "database": "ok"}`；`200`。若第二个返回 `401`：按铁律 2 处理——`unset BI_WEB_TOKEN`，重跑上面的 `up` 命令（仍只动 bi-web），再验一次。

- [ ] **Step 2: 四页渲染**

浏览器工具按序执行（browser-use MCP）：

1. `navigate_page` → `http://127.0.0.1:18080/d/l1-cockpit`；`wait_for` text=`["首页驾驶舱"]`（导航标题服务端渲染，出现即页面就绪）；`take_screenshot`。
2. 依次 `navigate_page` → `/d/l2-region`、`/d/l2-channel`、`/d/l2-people`；每页 `wait_for` text=`["区域", "月份"]` 或 `["渠道", "月份"]`（筛选条标签，服务端渲染）；`take_screenshot`。
3. L1 页 `list_console_messages`——预期无 JS 报错。
4. CDN 检查：`evaluate_script` → `() => typeof window.echarts`，预期返回 `"object"`。若 `"undefined"`：CDN 被本机网络环境拦截，记为**环境问题**，停止本任务并向用户单独汇报（图表交互后续步骤无法进行）。

每页人工核对（看截图）：

| 页面 | 检查点 |
|---|---|
| L1 | 顶部导航 4 链接（首页驾驶舱/区域下钻/渠道明细/人员榜），当前页高亮；7 张卡；两张环比卡含「前一日 … 万 · 环比 ±…%」次行与 7 日迷你趋势折线 |
| l2-region | 筛选条 2 个下拉（区域/月份，各含「全部」）；3 张卡（KPI/折线/部门 bar）；无「加载失败」 |
| l2-channel | 筛选条 2 个下拉（渠道/月份）；4 张卡（bar/折线/两张表格）；无「加载失败」 |
| l2-people | 筛选条 2 个下拉（区域/月份）；4 张卡（3 KPI + 人员榜表格）；无「加载失败」 |

- [ ] **Step 3: 筛选变更 → URL + 数据联动（l2-region）**

（a）下拉变更。`evaluate_script`（选第一个非空选项，数据无关）：

```js
() => {
  const select = document.querySelector('.filters select[data-param="region"]');
  const option = Array.from(select.options).find((o) => o.value);
  select.value = option.value;
  select.dispatchEvent(new Event("change", {bubbles: true}));
  return {picked: option.value, search: window.location.search};
}
```

预期：返回 `search` 形如 `?region=%E6%9D%AD%E5%B7%9E`（URL 编码后的所选项）；**页面未整页刷新**（`replaceState`）。随后：

- `take_screenshot`：区域日销趋势只剩所选区域的单系列；部门 bar 卡变为该区域的部门。
- `list_network_requests`：对 `/api/d/l2-region/cards/...` 的请求带 `?region=...` 查询串。

（b）URL 直进 + 下拉回填。`navigate_page` → `http://127.0.0.1:18080/d/l2-region?region=<picked>`（用 (a) 返回的 `picked` 值）。`evaluate_script` → `() => document.querySelector('select[data-param="region"]').value`，预期等于 `<picked>`（`syncSelects`：URL→下拉）。此时截图里折线单系列与 (a) 一致。

（c）非法值的降级路径（顺带验证，spec §8「非法参数值→400 安全文案」）。`navigate_page` → `http://127.0.0.1:18080/d/l2-region?region=不存在区域`。预期：**页面本身 200 照常渲染**（页面路由不校验查询参数），卡片的 API 请求 400，卡体显示「加载失败」错误态——不是白屏、不是堆栈。验完导航回正常 URL 继续。

（month/channel 两个下拉与 region 走同一条 `change → setParam` 接线；channel 的参数链路在 Step 4 由下钻完整覆盖，month 不再单独冒烟。）

- [ ] **Step 4: bar 点击 → channel 下钻（l2-channel）**

`navigate_page` → `http://127.0.0.1:18080/d/l2-channel`，`wait_for` text=`["渠道", "月份"]`，等 bar 渲染后 `take_screenshot`（留点击前证据）。

`evaluate_script`——用 `convertToPixel` 算出第一根非零柱子的中心像素，向 canvas 派发真实 `MouseEvent`（zrender 命中测试 → ECharts click → `wireDrill` 的 handler → `setParam` → `replaceState` → 全卡重拉，**完整链路**）：

```js
() => {
  const body = document.querySelector('[data-card="bar_channel_mtd"] .card-body');
  const chart = window.echarts.getInstanceByDom(body);
  const option = chart.getOption();
  const values = option.series[0].data;
  const index = values.findIndex((v) => v > 0);
  if (index < 0) return {error: "no non-zero bar"};
  const name = option.yAxis[0].data[index];
  const [x, y] = chart.convertToPixel({seriesIndex: 0}, [values[index] / 2, index]);
  const rect = body.getBoundingClientRect();
  body.querySelector("canvas").dispatchEvent(new MouseEvent("click", {
    clientX: Math.round(rect.left + x),
    clientY: Math.round(rect.top + y),
    bubbles: true,
  }));
  return {clicked: name, search: window.location.search};
}
```

预期：返回 `clicked` = 所点渠道名，`search` 形如 `?channel=...`；页面未刷新。随后：

- `evaluate_script` → `() => document.querySelector('select[data-param="channel"]').value`，预期等于 `clicked`（下钻同步下拉，`syncSelects`）。
- `take_screenshot`：`table_store_mtd`（店铺本月排行）按所点渠道过滤且列收缩为 排名/店铺/销售额（channel 列消失，Task 5 条件列语义）；折线只剩该渠道系列。
- `list_network_requests`：卡片请求带 `?channel=...`。

回退路径（仅当上面返回的 `search` 不含 `channel=` 时）：zrender 5 优先指针事件，换内部派发接口重试一次——`evaluate_script`：

```js
() => {
  const body = document.querySelector('[data-card="bar_channel_mtd"] .card-body');
  const chart = window.echarts.getInstanceByDom(body);
  const option = chart.getOption();
  const values = option.series[0].data;
  const index = values.findIndex((v) => v > 0);
  if (index < 0) return {error: "no non-zero bar"};
  const [x, y] = chart.convertToPixel({seriesIndex: 0}, [values[index] / 2, index]);
  chart.getZr().handler.dispatch("click", {zrX: x, zrY: y});
  return {clicked: option.yAxis[0].data[index], search: window.location.search};
}
```

两条路都不通才算 ❌——回去查 `wireDrill`（Task 9），先在宿主跑 `tests.common.test_bi_web_app -v` 复现模板属性断言，修完按该任务的 GREEN 步骤重跑，再回来重做本步。

- [ ] **Step 5: table 渲染内容检查**

三张表格卡的结构在 Step 2 截图已见骨架；本步查内容。`evaluate_script`（l2-people 为例）：

```js
() => Array.from(
  document.querySelectorAll('[data-card="table_people_leaderboard"] thead th')
).map((th) => th.textContent)
```

预期（Task 6 载荷 columns 的 title 序列）：`["排名", "姓名", "部门", "完成额", "月目标", "达成率", "未完成缺口"]`。

同法抽查：l2-channel 的 `table_channel_mtd` 表头为 渠道/销售额/推广费/ROI/店铺数（null 单元格显示「—」）；`table_store_mtd` 为 排名/店铺/销售额。真实库有杭州/绍兴人员数据时人员榜应见数据行；无数据月份的组合显示「暂无数据」占位行（spec §8「空结果而非报错」）——两者都是合法结果，如实记录即可。

- [ ] **Step 6: 导航互达**

`navigate_page` → `http://127.0.0.1:18080/d/l1-cockpit`，`take_snapshot` 拿 topnav 链接 uid，用 `click` 依次走：「区域下钻」→「渠道明细」→「人员榜」→「首页驾驶舱」。每次点击后 `wait_for`（对应页的服务端文本：区域/渠道/人员/首页驾驶舱）+ `take_screenshot`。

预期：每次整页导航成功（URL 变为对应 `/d/` 路径）；当前页链接高亮（`topnav-link current`）。导航是普通 `<a>` 跳转，**URL 查询参数不跨页保留**——这是预期行为，不是缺陷。

- [ ] **Step 7: 结果矩阵汇报 + 收尾**

按 spec §9 的五行验收矩阵汇报，每行 ✅/❌ + 对应截图：

| spec §9 验收项 | 结果 |
|---|---|
| 四页渲染 | |
| 筛选变更→URL+数据联动 | |
| bar 点击→channel 下钻 | |
| table 渲染 | |
| 导航互达 | |

任何 ❌：定位到对应任务，先跑该任务的测试命令复现（宿主 unittest / test-runner 容器），修复并重跑全绿后，单独重做本任务失败的那一步。

全部 ✅ 后收尾确认（不下栈）：

```bash
docker compose -f docker-compose.integration.yml ps bi-web mysql
```

预期：两个服务都 `running`——bi-web 以冒烟通过的版本在线，mysql 原样。

---

**计划到此为止。** Task 1-12 顺序执行；每个任务自带 RED-GREEN 循环与提交前验证（本批沿用阶段 A 惯例：计划不含 git 提交步骤，由用户统一指示提交时机）。
