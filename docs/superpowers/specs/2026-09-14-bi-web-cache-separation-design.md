# bi-web 缓存（Redis·冷热分离）与代码级前后端分离设计

日期：2026-09-14。关联：[bi-web 设计](2026-09-12-bi-web-design.md)、[阶段 B 设计](2026-09-12-bi-web-stage-b-design.md)。

## 1. 背景与动机

2026-09-14 实测（本地一体化栈）：

| 请求 | 实测 | 原因 |
|---|---|---|
| `/healthz` | 6.6ms | 不碰 Nacos |
| 卡片 API（冷/热） | 8.0s / 4.0s | gate + resolve 各 1 次失败读取 / 仅 resolve 1 次 |
| `/d/l1-cockpit` | 19.9s | resolve 1 次 + 导航 N+1 共 4 次 = 5 次失败读取 |

根因：Nacos 容器停止（`Exited 143`，两天前），`PUBLIC_DATA_NACOS_SERVER=nacos:8848` 在容器内不可解析；`NacosDashboardSource.get_dashboard` **完全没有缓存**（gate 有 30s TTL、筛选项有 30s TTL，唯独看板定义没有），且 `_nav_entries` 每渲染一页对每个看板各读一次（N+1）。每次失败的 `get_config` 恰好 ≈4 秒（DNS 失败 + nacos client 重试退避）。

用户拍板（2026-09-14）：按方案 C 修配置缓存，并叠加 Redis 数据缓存、数据冷热分离、代码级前后端分离。

## 2. 总原则（沿用既有不变量，一条不破）

- SQL 在代码、布局在 Nacos/种子；bi-web **只读 mart_ops、零写库、零凭据挂载、端口仅回环**（`127.0.0.1:18080:8080`）。
- **缓存一律 fail-open**：Redis 不可达 → 直查 mart，绝不 503；Nacos 不可达 → 种子兜底（现状语义保持）。
- 安全输出纪律不变：响应与日志不出现 SQL、traceback、DB 主机名、payload。
- ECharts 仍由浏览器从 CDN 拉取（服务端零出网），前端无框架、无构建步骤。
- 改动范围：`common/bi_web/` + compose（新增 opt-in redis 服务）+ `requirements.txt`（pinned redis 客户端）。**不碰 mart/extract 数据层**；`test_bi_web_queries.py` 的口径断言一条不动。

## 3. 决策与假设（记录在案）

- **D1 冷热分离 = 缓存分层，不是存储分层**。窗口含当天（`business_date <= CURDATE()` 仍开放，随 extract 同步在变）= **热**，短 TTL；已封月（参数 month < 当前月，未来行截断后不再变）= **冷**，长 TTL。存储层冷热（拆表/归档）归 extract-mart，不在本范围。
- **D2 Redis 是可选增强，不是硬依赖**。`PUBLIC_DATA_REDIS_URL` 缺省为空 → 进程内缓存兜底（语义等价现状的单容器部署）；配置了才连。compose 中 redis 走 opt-in profile，bi-web **不 `depends_on` redis**（否则 redis 挂 = bi-web 挂，违反 fail-open）。
- **D3 前后端分离 = API-first，不上 SPA 框架**。后端停止渲染数据相关 HTML，全部走 `/api/v1/`；前端为纯静态（index.html + dashboard.js + style.css）经 StaticFiles 提供，客户端路由。**不上 React/Vue/Vite**：现状「浏览器无法自带 Authorization 头」（token 面向 API 消费）、「服务端零出网」两条约束不允许引入构建链与 CORS。
- **D4 人员榜快照**（`queries._people_snapshot` 60s 进程内缓存）不进 Redis：数据经 `mart_collect` 多步聚合、口径已被测试锁定，进程内缓存足够；Redis 只缓存**卡片 JSON 载荷**。
- **D5 键与周期**：缓存键 = `biweb:card:{card_id}:{规范化参数}:{周期类别}`；周期类别 `cur`（热，键随日翻转）或 `YYYY-MM`（冷，键随月翻转）。参数规范化 = 按名排序拼接，杜绝键爆炸。

## 4. 阶段拆分

### 阶段 1：方案 C —— 看板定义 TTL 缓存（消灭 20s）

`NacosDashboardSource.get_dashboard` / `dashboard_ids` 增加 TTL 缓存（默认 30s，与 gate 同档、可注入）。缓存窗口内导航 N+1 归零；窗口外一次读取失败回落种子。**行为语义不变**（Nacos 为准、种子兜底、30s 陈旧上限），只消除每请求远程读取。

### 阶段 2：Redis 数据缓存 + 冷热分层

- 新模块 `common/bi_web/cache.py`：
  - `CardCache` 协议：`get(key)` / `set(key, payload, ttl)` / `close()`。
  - `InProcessCardCache`（TTL + per-key 锁，缺省后端）。
  - `RedisCardCache`（`redis` 客户端，fail-open：任何异常 → 视为 miss，set 失败静默）。
  - `build_card_cache()`：`PUBLIC_DATA_REDIS_URL` 非空 → Redis 后端，否则进程内。
  - `period_class(params)`：无 month 或 month == 当前月 → `("cur", HOT_TTL)`；month < 当前月 → `(month, COLD_TTL)`。
- TTL：热 = 300s（对齐页面 `refresh_seconds`），冷 = 86400s；常量集中、可注入。
- 接线：`app.py` 卡片路由对 `card.run` 结果读穿缓存；`Cache-Control: no-store` 响应头**保留**（HTTP 层不缓存，缓存只存在于服务端）。防惊群：同一键并发 miss 只算一次（per-key 锁）。
- compose：新增 `redis` 服务（`redis:7-alpine`，profile `bi-web-redis`，仅回环 `127.0.0.1:16379:6379`，无挂载到 bi-web）；bi-web 增加 `PUBLIC_DATA_REDIS_URL: ${PUBLIC_DATA_REDIS_URL:-}`。
- `requirements.txt`：`redis==5.2.1`（pinned，与其余依赖同纪律）。

### 阶段 3：API-first 前后端分离

- 新增路由（沿用 Bearer + gate + 404/503/400 语义）：
  - `GET /api/v1/dashboards` → 导航列表（enabled、按 nav_order）。
  - `GET /api/v1/dashboards/{id}` → 看板定义（title、refresh_seconds、filters、cards 的 span/title/param 白名单/on_click）。
  - `GET /api/v1/options/{source}` → 筛选项（regions/channels/months）。
  - `GET /api/v1/d/{id}/cards/{card}` → 卡片载荷（现 `/api/d/...` 的版本化别名，逻辑同源）。
- 前端：`common/bi_web/web/`（index.html + dashboard.js + style.css），StaticFiles 提供；客户端路由读 `location.pathname`（`/d/{id}`），导航/筛选/卡片全部经 v1 API 构建。
- 后端删除 Jinja2 渲染路径（`templates/` 退役）；`jinja2` 依赖暂留 `requirements.txt`（后续统一清理，避免本批扩大爆炸半径）。
- `/d/{id}` 返回静态 index.html（壳无数据），保持既有 URL 可收藏、可直进。

## 5. 不变量与测试

- 每阶段 TDD（先红后绿）；阶段间独立可回退。
- 阶段 1：`test_bi_web_config.py` 新增——缓存命中不二次读源、TTL 过期重读、失败回落种子、`dashboard_ids` 同样缓存。
- 阶段 2：`test_bi_web_cache.py` 新增——键规范化（参数顺序无关）、热/冷 TTL 分派、进程内后端 TTL 过期、Redis 后端 fail-open（异常 → 直查且不缓存坏值）、per-key 锁防惊群；`test_integration_environment.py` 新增断言——redis 为 opt-in、bi-web 无 `depends_on.redis`、redis 端口仅回环。
- 阶段 3：`test_bi_web_app.py` 更新——v1 路由 JSON 形状、`/d/{id}` 返回静态壳、旧模板断言替换为静态资源断言；静态前端 `index.html` 200。
- 全量回归：`python -m unittest discover -s tests -t .`（本地 + 容器 test-runner 同命令）。

## 6. 开放点

1. Redis 连接形态：本地无 auth；若将来进生产/网关层，凭据经环境注入、URL 不落库（沿用现有凭据边界）。
2. 冷 TTL 与 extract-mart 同步节奏的再校验：封月数据理论上不变，若出现回补（历史月重跑 extract），需手动清 `biweb:card:*:{month}` 前缀——先以 TTL 兜底，运维手册补一条。
3. 阶段 3 后 `jinja2` / `templates/` 的清理归后续小任务。
