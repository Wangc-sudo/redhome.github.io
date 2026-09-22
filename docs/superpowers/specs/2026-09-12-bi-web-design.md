# bi-web 自研 BI 展示层设计

> ⚠️ **文档状态**：本设计已**过时**，请以 `frontend/bi-react/ARCHITECTURE.md` 为准。
> 
> **更新（2026-09-15）**：前端技术栈已从 Jinja2 改为 **React + Vite + TypeScript**，
> 代码已交付并通过全量测试。详见 `frontend/bi-react/PROGRESS.md`。

状态：已过时（React 路线已落地，见 ARCHITECTURE.md）

## 2. 与既有服务的边界

| 服务 | 线 | 职责 | 与 bi-web 的关系 |
|---|---|---|---|
| `extract-mart` | apps | raw → mart 投影、维度 | bi-web 的唯一数据上游 |
| `robot` / `pages-hangzhou` | 业务 | 榜单/播报，cron 一次性 | 同级；共享 `common.metrics` 口径 |
| `control-api`（阶段 5，未建） | 控制面 | 线状态门面、启停审计 | **正交不合并**：control-api 管「线」，bi-web 管「数」 |
| Metabase 试点 | — | 临时参照/自助取数 | 主路径上线后退役 |

bi-web 与 robot 共用同一套口径模块（`common/metrics/daily_report.py`），杜绝「机器人一个数、看板一个数」（spec §9 已定原则的自然延伸）。

## 3. 技术栈

| 决策 | 选择 | 理由 |
|---|---|---|
| Web 框架 | **FastAPI** | 与阶段 5 `control-api` 既定选型一致；自带 OpenAPI |
| 渲染 | **Jinja2 服务端渲染 + ECharts（CDN）** | 无前端构建链；页面是「壳 + JSON 数据端点 + ECharts 配置」，与榜单 HTML 生成路径同源 |
| DB | `common.public_data.db.connect` + `pymysql` | 现有连接助手直接复用 |
| 配置 | `common.public_data.pipeline_config` 的 `ConfigSource` 契约 | Nacos/File/Static 三后端与 fail-open 已就绪，看板定义照搬同一模式 |
| 依赖新增 | `fastapi` + `uvicorn` + `jinja2` | 仅三个纯 Python 包，进 integration Dockerfile |

不引入：前端框架、node 构建、ORM、迁移工具（bi-web 零写库）。

## 4. 目录结构

```
common/bi_web/
├── __init__.py
├── app.py            # FastAPI 装配：路由、认证、静态文件、模板
├── config.py         # 看板定义模型 + ConfigSource 消费（group=BI）
├── cards.py          # 卡片注册表：card_id -> (执行函数, 图表类型) —— SQL/口径只活在这里
├── queries.py        # 首批 SQL（版本受控，评审可见）
├── templates/        # Jinja2：base.html / dashboard.html / card 宏
└── static/           # 少量 CSS/JS（ECharts 走 CDN，不打包）
docker/integration/
└── bi.seed.yaml      # 看板定义种子（版本受控，publish 进 Nacos group=BI）
```

**关键决策：SQL 与口径进代码库，Nacos 只存编排。** 卡片的数据集定义（SQL / 对 `common.metrics` 的调用）是口径，必须可评审、可单测、可 diff——放 Nacos 会绕开评审。Nacos 的看板定义只回答「哪个页面摆哪些卡、什么标题、什么布局、是否启用」，与管线注册表「决定要不要跑」的职责完全同构。

## 5. 配置模型

### 5.1 服务注册条目（既有机制，零新代码）

`pipelines.seed.yaml` 增加：

```yaml
bi-web:
  kind: business
  enabled: true
  reads: [mart_ops]
  description: "BI dashboards (L1/L2), read-only on mart_ops"
```

`enabled=false` 时进程启动即退出（复用 fail-open 门控语义）。

### 5.2 看板定义（新 group `BI`）

dataId = `{dashboard_id}.yaml`，namespace 按环境，与管线注册表同套 Nacos。种子 `docker/integration/bi.seed.yaml`：

```yaml
l1-cockpit:                      # dashboard_id -> GET /d/l1-cockpit
  title: "首页驾驶舱"
  enabled: true
  refresh_seconds: 300
  cards:
    - card: kpi_offline_mtd      # 引用 cards.py 的注册 id，不存在 = 启动即报错
      title: "线下本月累计销售"
      span: 4                    # 12 栅格占位
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

加载链：`Nacos(group=BI) → 种子文件 → 内置最小默认`，与 `NacosConfigSource` 同 fail-open 语义；看板定义缺失/损坏时该页 503，**不影响其他页**。发布工具复用 `publish-pipelines` 模式（`cli publish-bi --seed ...`，`--if-missing` 幂等）。

### 5.3 卡片注册表（代码侧）

```python
# cards.py
@dataclass(frozen=True)
class Card:
    card_id: str
    chart: str            # scalar | bar | line | table
    run: Callable         # (conn, params) -> chart 就绪的 dict
    params_schema: dict   # 允许的 URL 参数（如 month、region）白名单
```

启动时校验：seed 引用的每个 `card` 都在注册表内、每个注册卡的 `chart` 合法——**配置漂移在启动期暴露，不在浏览器里暴露**。

## 6. 路由

| 路由 | 内容 | 首批 |
|---|---|---|
| `GET /` | 重定向到默认 dashboard（`l1-cockpit`） | ✅ |
| `GET /d/{dashboard_id}` | 看板页（服务端渲染壳） | ✅ |
| `GET /api/d/{dashboard_id}/cards/{card_id}` | 卡片数据 JSON（ECharts option 的 data 部分） | ✅ |
| `GET /healthz` | 存活 + mart 连通性（`SELECT 1`） | ✅ |
| `GET /d/...` L2 各页 | 后续批次 | 阶段 B |

页面壳内嵌 ECharts CDN 脚本，按 `refresh_seconds` 轮询卡片端点；卡片端点带 `Cache-Control: no-store`，参数经 `params_schema` 白名单校验（防注入的第一道闸，SQL 本身全部参数化）。

## 7. 首批口径（①③⑫）

### ① 线下日销（fact_daily_report_offline）

- **kpi_offline_mtd（scalar）**：当月 Σ`sales_amount`，`region IN ('hangzhou','shaoxing')`，`business_date <= CURDATE()`——事实表有预填到月底的目标行，**必须截断未来日期**（Metabase 试点踩过的平线坑）。
- **trend_region_daily（line）**：按日 Σ`sales_amount` 分组 `region`，同截断。

### ③ 电商渠道日销（fact_channel_daily_sales）

- **kpi_channel_mtd（scalar）**：当月 Σ`sales_amount`，`business_date <= CURDATE()`。
- **bar_channel_mtd（bar）**：当月按 `channel` Σ`sales_amount` 降序；`promotion_cost`、`roi` 进 L2 电商页，首批不上。

### ⑫ 年度目标达成进度

- **口径单点复用**：`common.metrics.daily_report`（`fetch_workdays` + `fetch_month_facts` + `elapsed_workdays` + `summarize_people` → `achievement_rate`），月目标取 `MAX(monthly_target)`，**绝不 SUM**（melt 陷阱）。
- **kpi_annual_progress（scalar/进度条）**：`年累计 Σsales ÷ dim_target.annual_target`。
- **`dim_target` 建表 + 种子**：E 类小维度，口径已定——总 76951 万（线下 21041 / 电商 54980 / 餐厅 930）；表形 `(scope, scope_key, year, annual_target, note)`，PK `(scope, scope_key, year)`，值由版本受控 seed 落库（随 extract 或独立小迁移，实施时定）。**品牌维度（习酒 61631 万、古韵+大坛 10000 万）首批不做**——依赖 dim_product（goods_query status=99 根因已查明：那是企业版方法名，旗舰版正确接口为 `goods.Goods.queryWithSpec`，且该接口强制起止时间、单窗 ≤30 天，代码已按窗口扫掠订正并经真实探针验证 status=0；直拉单轨即可，原双轨合并方案作废）。

## 8. 认证与暴露

- **首批**：`BI_WEB_TOKEN` 环境变量注入的 Bearer token（nginx/网关层或直接 FastAPI 依赖），compose 端口绑定 `127.0.0.1:18080:8080`——与 MySQL `13306` 同约定，仅回环、不对局域网暴露。
- **后续**：钉钉免登（扫码/工作台应用）+ 按区域/角色的行级可见性，属阶段 C，届时评估是否复用 `dim_robot_member` 做身份映射。

## 9. Compose 与运行

```yaml
bi-web:
  profiles: [bi-web]
  build: { context: ., dockerfile: docker/integration/Dockerfile }
  entrypoint: ["python3"]
  command: ["-m", "common.bi_web.app"]   # uvicorn 内嵌启动
  depends_on: { mysql: { condition: service_healthy } }
  environment:
    <<: *nacos-env
    APP_ENV: test
    PUBLIC_DATA_RDS_HOST: mysql
    # ...三库连接变量同 robot（实际只用 MART）
    PUBLIC_DATA_SERVICE_ID: bi-web
    PUBLIC_DATA_BI_SEED: /app/docker/integration/bi.seed.yaml
    BI_WEB_TOKEN: ${BI_WEB_TOKEN:-}
    TZ: Asia/Shanghai
  ports:
    - "127.0.0.1:18080:8080"
```

零凭据零挂载：无 `--live-*` 旗标、无 live-input volume，合约测试把守（断言 compose 中 bi-web 无 credentials 挂载、无 raw 库环境变量引用——沿用 robot 的既有断言模式）。`PUBLIC_DATA_SERVICE_ID=bi-web` 使注册表关停即时生效（长驻进程改为**请求时门控**：每次渲染前读配置，Nacos 关闭即全站 503，无需重启）。

## 10. 迭代工作流

新增/调整看板的标准路径：

1. **改口径/加卡片** → 改 `cards.py` / `queries.py` + 单测，走代码评审；
2. **改编排（加页/调布局/换标题）** → 改 `bi.seed.yaml` → `publish-bi` 进 Nacos → 下次轮询生效（后续可加 Nacos listener 免轮询）；
3. **关停整个服务** → Nacos `PIPELINES/bi-web.yaml` 置 `enabled=false`。

「SQL 在代码、编排在 Nacos」使两类变更各有单一真源，杜绝「Nacos 里一段 SQL 和代码里一段 SQL 谁是真的」的漂移。

## 11. 测试

- **口径单测**：`queries.py` 的 SQL 在 test-runner 对 `mart_ops_test` 真实执行（集成测试先例已多）；`common.metrics` 复用部分不重复测。
- **配置校验单测**：seed 引用未知 card / 非法 chart / 非法 span 即抛错。
- **合约测试**：compose 中 bi-web 服务无凭据挂载、无 `--live-*`、无外呼。
- **冒烟**：`docker compose --profile bi-web up` 后 `GET /healthz` 200 + `GET /d/l1-cockpit` 含 5 个卡片占位。

## 12. 阶段划分与估算

| 阶段 | 内容 | 估算 |
|---|---|---|
| A（首批） | 骨架（app/config/cards/模板）+ L1 ①③⑫ 五卡 + 认证 + compose + 合约测试；`dim_target` 建表种子 | **5–8 人天** |
| B | L2 分析页（区域下钻、渠道明细、人员榜复用 leaderboard 视图）；dim_product 双轨落地后补品牌维度 | 视 B2 架构决策 |
| C | 钉钉免登、行级权限、Nacos listener 热更新、大屏模式 | 视需求 |

## 13. 开放点

- `dim_target` 落库方式：随 extract-mart 加维度投影 vs 独立小迁移 + seed 加载命令（倾向后者——目标是业务口径，不是 raw 投影）。
- 看板定义的 Nacos group 名 `BI` 与 dataId 命名（`{dashboard_id}.yaml`）。
- 长驻进程的注册表门控粒度：请求级（本设计）vs 启动级（robot 现状）——长驻服务请求级才合理，但带来「每请求一次 Nacos 读」的开销，实现时加短 TTL 缓存（如 30s）。
- L2 各页依赖 B2（extract 聚合投影）与 C/D 类决策，不在本设计收口。
