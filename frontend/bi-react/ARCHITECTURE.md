# BI-React 架构方案

> 上游契约：`bi-ui/CubeSchema.md`（数据口径）、`bi-ui/VISUALIZATION.md`（可视化规范）。
> 上游代码：`frontend/bi-ui/` 设计系统（tokens/base/components.css 已拷贝进 `src/styles/bi-ui/`）。

## 0. 现状（2026-09-15 已交付）

```
技术栈：React 18 + Vite 5 + TypeScript + echarts-for-react + react-grid-layout
构建：主包 26KB（gzip 10KB），echarts 独立 vendor 块
测试：vitest 76 passed / 4 skipped
路由：hash 路由（#/d/{dashboardId}）
```

---

## 1. 目录结构（与代码一致）

```
bi-react/
├─ package.json / vite.config.ts / tsconfig*.json
├─ index.html
├─ src/
│  ├─ main.tsx                    # 入口：import bi-ui css
│  ├─ App.tsx                     # 壳：Sidebar + hash 路由
│  ├─ vite-env.d.ts               # import.meta.env 类型
│  │
│  ├─ types/cube.ts               # CubeSchema 类型（对接 bi-ui/CubeSchema.md）
│  │
│  ├─ data/                       # 数据层
│  │  ├─ types.ts                 # 后端 API 类型（CardPayload / DashboardDef 等）
│  │  ├─ biWebClient.ts           # 统一 API 客户端（四端点）
│  │  ├─ errors.ts                # BiWebError 统一错误语义
│  │  ├─ useCube.ts               # 看板级共享缓存 + 轮询
│  │  ├─ useDashboard.ts          # 看板列表 / 定义 / 筛选选项
│  │  ├─ deriveGolden.test.ts     # 防回归守卫：derive.ts 已删（文件级断言 + golden 夹具自检）
│  │  ├─ severity.ts              # severity code → chip 映射
│  │  ├─ adapter/
│  │  │  ├─ cardToCube.ts         # 核心：CardPayload → CubeSchema 归一
│  │  │  ├─ cardToCube.test.ts    # 适配层单测（12 例）
│  │  │  └─ derivedCards.test.ts  # 后端已算派生指标透传测试（10 例）
│  │  └─ mock/
│  │     └─ fixtures.ts           # mock fixture（真实 payload 结构）
│  │
│  ├─ components/
│  │  ├─ common/
│  │  │  ├─ ErrorBoundary.tsx     # 错误边界
│  │  │  └─ States.tsx            # Loading / Empty / Error 三态
│  │  ├─ renderers/               # 原语渲染器（全部吃 CubeSchema）
│  │  │  ├─ ScalarCard.tsx        # KPI 卡
│  │  │  ├─ ChartCard.tsx          # echarts 图表（line/bar）
│  │  │  ├─ PieCard.tsx           # 饼/环图
│  │  │  ├─ TableCard.tsx         # 表格（吸顶 + 排序 + 合计）
│  │  │  ├─ AlertChip.tsx         # 四级告警色 + 文字
│  │  │  ├─ AnomalyList.tsx       # 异常清单（组件就绪，待后端卡）
│  │  │  └─ ConclusionBar.tsx     # 结论条（组件就绪，待后端卡）
│  │  └─ layout/
│  │     ├─ Sidebar.tsx           # 侧边栏导航
│  │     ├─ DashboardGrid.tsx     # react-grid-layout 拖拽布局
│  │     ├─ DashboardView.tsx     # 看板视图
│  │     ├─ FilterBar.tsx         # 筛选条
│  │     └─ WidgetCard.tsx        # 单卡组件（每卡独立订阅缓存）
│  │
│  ├─ dashboards/
│  │  └─ registry.ts              # 看板注册表（导航兜底）
│  │
│  ├─ hooks/
│  │  └─ useDrill.ts              # 下钻筛选状态
│  │
│  ├─ router/
│  │  └─ useHashRoute.ts           # hash 路由 #/d/{id}
│  │
│  ├─ utils/
│  │  └─ format.ts                # 金额缩放 / 百分比 / 涨跌 class
│  │
│  ├─ styles/                     # 设计系统（从 ../../bi-ui 拷贝）
│  │  ├─ bi-ui/
│  │  │  ├─ tokens.css
│  │  │  ├─ base.css
│  │  │  └─ components.css
│  │  └─ app.css                  # 应用壳布局
│  │
│  ├─ __tests__/
│  │  └─ app.smoke.test.tsx       # App 渲染冒烟（3 例）
│  └─ data/
│     ├─ deriveGolden.test.ts      # 防回归守卫（derive.ts 已删）
│     └─ ...
│
├─ .env.example                   # BI_WEB_TOKEN / VITE_MOCK 等
├─ vitest.config.ts
└─ tsconfig*.json
```

---

## 2. 核心数据流

```
后端 API（/api/v1/...）
    │
    ▼
biWebClient.ts        # 统一 fetch 出口，含 Authorization 头
    │
    ▼
useCube.ts            # 看板级共享缓存（7 卡 → 1 请求，300s 轮询）
    │
    ▼
cardToCube.ts         # CardPayload → CubeSchema 归一
    │                 # ├── backendDerivedOf：后端已算 severity 原样透传
    │                 # └── deriveCube：前端过渡派生（可关）
    ▼
渲染器（吃 CubeSchema）  # ScalarCard / ChartCard / TableCard / PieCard
    │
    ▼
react-grid-layout     # DashboardGrid 拖拽布局 + localStorage 持久化
```

---

## 3. 与后端契约

### 3.1 API 端点

| 端点 | 说明 |
|---|---|
| `GET /api/v1/dashboards` | 看板列表（导航） |
| `GET /api/v1/dashboards/{id}` | 看板定义（cards / filters / layout） |
| `GET /api/v1/options/{source}` | 筛选选项（regions/channels/months） |
| `GET /api/v1/d/{id}/cards/{card}` | 卡片数据 JSON |
| `GET /healthz` | 健康检查 |

### 3.2 前端适配层职责

`cardToCube.ts` 是**唯一转换点**：

| 后端 payload | 前端 CubeSchema | 说明 |
|---|---|---|
| `ScalarPayload` | `chart: 'scalar'` | 值 + delta + trend7 |
| `LinePayload` | `chart: 'line'` | dates × series |
| `BarPayload` | `chart: 'bar'` | categories × values |
| `TablePayload` | `chart: 'table'` | columns + rows |
| `PiePayload` | `chart: 'pie'` | items |

**铁律**：渲染器永远看不到 `CardPayload`，后端字段调整只影响 adapter。

---

## 4. 派生指标处理

### 4.1 后端已算（直接透传）

`kpi_shortfall` / `anomaly_top` 由后端 `common/bi_web/derived.py` 计算：
- `shortfall` / `rate` / `required_daily` / `severity` 在行内
- 前端 `backendDerivedOf()` 只做字段搬运，**不重算**

### 4.2 ~~前端过渡派生（临时债）~~ 已删除（2026-09-16）

后端 `derived.py` 已补齐全部派生口径且 golden 对拍一致，`derive.ts` + `derive.test.ts`
已按路线图 P3 **整文件删除**，`VITE_DERIVE` 开关一并移除。前端**零派生实现**：
后端没给 severity 的行不产 derived，渲染「—」；`has_fact=false` 由 TableCard 挂
「挂零」诊断角标（仅渲染，不参与判定）。

### 4.3 对拍机制

- Golden 夹具：`tests/fixtures/derived_golden.json`（24 条派生 + 4 条日历）
- 逐位对拍在 **Python 侧**（tests/ 跑同一夹具）；前端 `deriveGolden.test.ts` 改为
  文件级防回归守卫：derive.ts 不存在 / 无人 import / 无人重新导出本地派生·日历·告警函数，
  任一侧漂移都会变红，强制重走裁决

---

## 5. 组件清单（已实现）

| 组件 | 状态 | 说明 |
|---|---|---|
| `ScalarCard` | ✅ | KPI 值 + 进度条 |
| `ChartCard` | ✅ | echarts line/bar（通用） |
| `PieCard` | ✅ | 饼/环图 |
| `TableCard` | ✅ | 表格（吸顶/排序/合计） |
| `AlertChip` | ✅ | 四级告警（色盲友好） |
| `AnomalyList` | ✅ 组件就绪 | 待后端 `anomaly` 卡 |
| `ConclusionBar` | ✅ 组件就绪 | 待后端 `conclusion` 卡 |
| `WidgetCard` | ✅ | 单卡组件，独立订阅缓存 |
| `FilterBar` | ✅ | 后端 filters 驱动 |
| `DashboardGrid` | ✅ | 拖拽布局 + localStorage |

---

## 6. 测试覆盖

| 测试 | 数量 | 说明 |
|---|---|---|
| adapter 单测 | 12 | CardPayload → CubeSchema 归一 |
| 派生卡测试 | 10 | 后端已算 severity 透传 |
| derive 单测 | 14 | 过渡派生层 |
| golden 对拍 | 34 | 与后端 derived_golden.json 对拍 |
| App 冒烟 | 3 | 渲染不崩 |

**总计**：vitest 76 passed / 4 skipped

---

## 7. 已知限制

| 限制 | 影响 | 下一步 |
|---|---|---|
| 日历仅 2026-09 | 跨月查询退 p2 | P2：跨年日历扩展 |
| `ConclusionBar` / `AnomalyList` 无后端卡 | 组件已就绪 | 等后端补卡 |
| `derive.ts` 临时债 | 需最终删除 | 后端补齐口径后删除 |

---

## 8. 相关文档

- `frontend/bi-react/PROGRESS.md` — 开发进度与缺陷台账
- `frontend/bi-ui/CubeSchema.md` — 数据口径契约
- `docs/derived-metrics.md` — 派生指标清单
- `docs/BI建设指南.md` — 整体架构
