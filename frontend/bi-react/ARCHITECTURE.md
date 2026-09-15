# BI-React 架构方案（路线 B：Vite + React 重建）

> 上游契约：`bi-ui/CubeSchema.md`（数据口径）、`bi-ui/VISUALIZATION.md`（可视化/工程规范）、`bi-ui/tokens.css`+`components.css`（设计系统）。
> 本文件定义如何用 React 把"标准 CubeSchema 数据"渲染成看板，落实 CSDN 材料那句核心原则：
> **"围绕数据计算模型确定一套固定的接口格式，取数不依赖组件，所有组件对标准数据都有对应的展现。"**

---

## 0. 一句话架构

**数据（后端 CubeSchema）→ 数据层（cubeClient + useCube）→ 原语渲染器（吃标准结构）→ 布局层（react-grid-layout）→ 交互中心（层系下钻 / 图表联动）。**
组件是 CubeSchema 的**哑渲染器**：不知道"缺口怎么算、告警怎么分"，只负责把 `CubeSchema` 画出来。派生指标（缺口 / 进度差 / 告警级别 / 环比）**一律由后端算**，前端零计算。

---

## 1. 目录结构

```
bi-react/
├─ package.json / vite.config.ts / tsconfig*.json   # 脚手架
├─ index.html
├─ src/
│  ├─ main.tsx                 # 入口：import bi-ui token + react-grid-layout css
│  ├─ App.tsx                  # 壳：Sidebar + 路由到 DashboardGrid
│  ├─ styles/index.css         # @import ../../bi-ui/{tokens,base,components}.css
│  ├─ types/cube.ts            # 类型化 CubeSchema（对接 CubeSchema.md）
│  ├─ data/
│  │  ├─ cubeClient.ts         # fetch `/api/v1/d/{path}` + CubeError
│  │  └─ useCube.ts            # 轮询 hook：{data,loading,error}
│  ├─ components/
│  │  ├─ common/
│  │  │  ├─ ErrorBoundary.tsx  # componentDidCatch 等价 + 上报
│  │  │  └─ States.tsx         # Loading / Empty / Error 三态
│  │  ├─ renderers/            # 原语渲染器（全部吃 CubeSchema 标准结构）
│  │  │  ├─ ScalarCard.tsx     # KPI：值 + delta(涨红跌绿) + 进度
│  │  │  ├─ ProgressCard.tsx
│  │  │  ├─ ChartCard.tsx      # echarts-for-react，option 由 cube 推导
│  │  │  ├─ TableCard.tsx      # 吸顶 + 排序 + 合计 + 缺失(—)处理
│  │  │  ├─ DonutCard.tsx
│  │  │  ├─ AlertChip.tsx      # 四级告警：色 + 文字 + 图标（色盲友好）
│  │  │  ├─ ConclusionBar.tsx  # 结论条
│  │  │  └─ AnomalyList.tsx    # 异常清单：缺口 TOP N（对接优化方案 §2.3）
│  │  └─ layout/
│  │     ├─ DashboardGrid.tsx  # react-grid-layout 拖动/缩放
│  │     └─ Sidebar.tsx        # V2 DIGITAL OPS 门户 chrome
│  ├─ dashboards/registry.ts   # 看板注册表（l2-region 等 + overview 门户）
│  └─ hooks/useDrill.ts        # 层系下钻 + 图表联动 事件总线
```

---

## 2. 数据层（取数不依赖组件）

`cubeClient.ts` 只认一套契约路径 `/api/v1/d/{path}`，返回 `CubeSchema`（见 `types/cube.ts`）。
`useCube(path, {intervalMs})` 负责轮询 + 三态（loading/error/data），组件只 `const {data,loading,error}=useCube(def.cubePath)`。
**组件内禁止出现任何 `target-done`、告警阈值、环比计算逻辑** —— 这些都在 `CubeSchema.derived` 里由后端给好。

---

## 3. 原语渲染器（哑渲染器清单）

每个渲染器 props = 一段 CubeSchema 标准结构，输出 = 一段标准 DOM（复用 `bi-ui/components.css` 的 class）。

| 渲染器 | 吃的数据 | 关键行为 |
|---|---|---|
| `ScalarCard` | `CubeRow` + `derived[key]` | 值 + `--up/--down` delta + 进度条；缺失显示 `—` 不渲染数 |
| `ChartCard` | `CubeSchema` (dimensions/measures/rows) | 按 dataType 推导 echarts option；大数据走 `large:true` + `sampling` |
| `TableCard` | `CubeSchema` | 吸顶、`missing` 行显 `—` 且**不参与排序**、合计行、ARIA |
| `AlertChip` | `DerivedMetric.alert` | P0/P1/P2/OK 四色 + 文字 + 图标（不只靠颜色，色盲友好） |
| `ConclusionBar` | `CubeSchema.derived` 摘要 | 一句话结论（对接优化方案"结论条"） |
| `AnomalyList` | `CubeSchema.derived` 排序后 TOP N | 缺口降序，直接回答"今天该打谁电话" |

所有渲染器外层包 `ErrorBoundary`，单卡报错不拖垮整板。

---

## 4. 布局层（拖动 / 缩放）

`DashboardGrid` 用 `react-grid-layout`：布局来自 `registry.ts` 的 `WidgetDef{x,y,w,h}`，用户拖拽后本地持久化（localStorage）。
> 材料原话："React-grid-layout 可以实现图表的拖动和缩放布局，只需简单配置即可。" —— 正好对应。

---

## 5. 交互中心（层系下钻 / 图表联动）

`useDrill.ts` 维护 `path / filters / drillLevel`，并提供极简事件总线 `emit/on`：
- **下钻/上卷**：层系字段（`CubeSchema.hierarchies.levels`）顺序推进，`drillDown(field)` → 重新 `useCube` 取下一层 cube。
- **图表联动**：一个图 emit `filter:{dim,value}`，其他图 `on` 后加 filter 重取。
> 材料："事件中心可以实现图表联动、上卷下钻等数据能力。" —— 这里用 zustand + 事件总线落地。

---

## 6. 可观测（错误 / 性能 / 截图 / 代码编辑）

- **错误监控**：`ErrorBoundary`（React 边界，等价 `componentDidCatch`）+ `window.onerror` 兜底 + **`build.sourcemap:true`** 定位源码。上报到监控（待接）。
- **性能监控**：`App` 挂载时记 `performance.now()`，首屏看板渲染完成打点；整板加载时长 = 全部 `useCube` resolve 时刻 − 挂载时刻。
- **截图分享**：`html2canvas` 包裹 `DashboardGrid` 导出 PNG（材料：将 HTML 转 Canvas 保存分享）。
- **代码编辑**：数据源/SQL 编辑用 `CodeMirror`（材料：实时高亮，适用 SQL 输入）。

---

## 7. 与 V1 / V2 的关系（收口三套前端）

- **V1（bi-web 通用渲染器）→ 用本 React 应用重写**：V1 的 `dashboard.js` 按 pathname 拉 `/api/v1/*` 的逻辑，迁移为 `cubeClient + useCube + DashboardGrid`。
- **V2（DIGITAL OPS 门户）→ 作为 `overview` 模板进 `registry.ts`**：V2 的 Sidebar/面包屑 chrome 保留为 `Sidebar.tsx`，其 KPI/donut/明细表映射为对应渲染器。
- **异常驱动看板（L1 驾驶舱）→ 作为 `l1-cockpit` 模板进注册表**：直接用 `ConclusionBar + AnomalyList + AlertChip`，吸收其四级告警色（已在 `bi-ui/tokens.css` 的 `--severity-*`）。
- **设计系统**：`main.tsx` 直接 `@import bi-ui` 三件套，**不再有第三套 token**。

> 三套前端 → 一个 React 应用 + 一个 CubeSchema 契约 + 一套 bi-ui 设计系统。这正是路线 B 相对 A 的代价（重写）换来的终态。

---

## 8. 迁移步骤（低风险、可回滚）

1. 本脚手架 `npm i && npm run dev`（dev 代理 `/api` → `18080`，复用现有后端契约）。
2. 先接 `l2-region`（V1 现成 path）跑通端到端，确认 `bi-ui` token 在 React 下生效。
3. 逐个把 V2 页面、`l1-cockpit` 迁成 registry 模板。
4. 验证通过后，18080 的旧 vanilla 服务可下线（保留回滚：旧服务进程不删，随时切回）。
5. **前置 P0**：`18080 /api/v1/*` 当前 404，必须先让后端把 CubeSchema 接口实现，否则 React 同样空板。

---

## 9. 待确认（小项）

- **TypeScript**：默认开（数据契约强类型，推荐）。可改 JS+JSX。
- **状态库**：默认 `zustand`（轻、无 Provider 嵌套）。可换 Redux。
- **echarts 封装**：默认 `echarts-for-react`。
- **路由**：默认轻量 `hash` 路由（避免服务端配置），与 `pathname` 拉取习惯兼容。
