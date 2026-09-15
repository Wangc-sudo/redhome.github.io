# 统一设计系统方案 · bi-web × DIGITAL OPS

> 目标：把 18080（bi-web，通用 BI 渲染器）与 18081（DIGITAL OPS，经营总览门户）两套 token / 组件合并为**一份统一设计系统**，消除"门户跳详情视觉断裂、双服务器双样式"的问题。
> 原则：**以 V1 的设计系统为底座（更成熟、token 化、状态/无障碍覆盖完整），把 V2 的 chrome（侧边栏/面包屑/多图联动）作为 V1 的一个「概览看板模板」**。

---

## 0. 前置依赖（P0，必须先解决）

设计统一是"表面工程"，没有数据后端一切空谈。当前现状：

- V1(18080) `/api/v1/*` 全部 `404`（uvicorn 未实现这些路由）→ 看板 div 空。
- V2(18081) 是 Python `SimpleHTTP` 纯静态服务，无后端，其 `fetch('/api/v1/d/'+path)` 同样 404 → 全"加载中"。

**先确认后端跑在哪、是否同源**，否则统一设计后仍是空板。本方案假设后端会补齐，只解决"前端一致性"。

---

## 0.5 数据口径层统一（来自《前端与 BI》精读）

> 来源：`https://blog.csdn.net/qzmlyshao/article/details/136417531`（精读《前端与 BI》，BI 1.0 报表阶段四大模块：数据集 / 渲染引擎 / 数据模型 / 可视化）。

**核心原则（直接采纳）**：*"BI 业务是以数据为核心的，围绕数据计算模型确定一套固定的接口格式，取数不依赖组件，所有组件对标准数据都有对应的展现。"* —— 这正好印证 V1(bi-web) 的 API-first / 配置驱动路线，也是「设计系统 + 口径」统一的地基。

**为什么单列这一层**：上一轮只统一了「视觉 token」，但 V1 / V2 / 异常驱动看板 若各自在 JS 里定义「缺口 / 进度差 / 告警级别 / 环比」，视觉再统一也会在**数据含义**上分裂。所以「口径统一」要先于、且独立于「皮肤统一」。

### 统一数据口径（CubeSchema 思路）
| 概念（blog） | 含义 | 落到我们的统一标准 |
|---|---|---|
| 数据集 | 标准化二维表（列=字段，行=记录），所有看板的标准输入 | 三个前端**只消费**同一份二维结果集，禁止在 JS 里各算字段 |
| 数据模型 CubeSchema | 维度 / 度量 / 层系（hierarchy，支持上卷下钻） | 看板定义用 维度(时间/部门/渠道) + 度量(销售额/完成率) + 层系(区域→人员) 描述 |
| 派生/对比字段 | 同比、均值线等由后端算，前端无感知 | **缺口 / 进度差 / 告警级别 / 环比 一律放在数据模型层（后端）计算**，前端只渲染，避免 V1/V2/异常驱动各算一遍 |
| 固定取数接口 | 取数不依赖组件 | V1 的 `/api/v1/d/{id}` 作为唯一取数契约，V2 与异常驱动看板复用同一契约 |

### 下钻 = 层系钻取
- L1 驾驶舱 → L2 明细 = 层系下钻（区域→人员、渠道→店铺）。
- 采用 blog 的「**下钻 + 筛选**」模式：下钻到某节点时追加筛选条件（如 年=2019），对图表组件无感知，避免全量数据爆炸。

### 可视化边界（呼应优化方案 §2.4 数据缺陷显式化）
- 组件必须处理：空数据、字段缺失（`—` / 待补）、极端值（如 173% 预测）。
- 保护式补全 + 避让，确保异常数据不崩图。

### 对三套前端的约束
- ✅ **V1(bi-web)**：已是 API-first，作为口径承载层 + 渲染引擎。
- ⚠️ **V2(DIGITAL OPS) / 异常驱动看板**：按同一 CubeSchema 取数，派生指标不再前端硬算；告警级别由后端随数据下发（见优化方案 §2.3 的 P0–P2，但逻辑上收口到数据模型层）。
- ⚠️ 与 §0 后端 P0 强耦合：没有统一取数接口，口径统一就是空话。

---

## 1. 统一 Token（色板 / 字号 / 圆角 / 阴影）

以 V1 的语义命名为准，吸收 V2 的双色强调（蓝=线下、teal=电商，用于渠道区分）。

| 语义 Token | 旧 V1 (v1_style.css:8-19) | 旧 V2 (v2_style.css:1) | 统一值（提案） | 说明 |
|---|---|---|---|---|
| `--bg` 页面底 | `#f3f5f7` | `#f7f9fa` | `#f3f5f7` | 取 V1，沉一点更利于卡片浮起 |
| `--surface` 卡片底 | `#ffffff` | （白） | `#ffffff` | 统一 |
| `--border` 描边 | `#e6e8eb` | `#e7ecee` | `#e6e8eb` | 取 V1 |
| `--border-strong` 聚焦描边 | — | — | `#d0d7de` | 新增，focus 用 |
| `--text` 主文字 | `#1f2933` | `#20282d` | `#1f2933` | 基本一致 |
| `--text-muted` 次要文字 | `#667085` | `#78838d` | `#667085` | 取 V1 |
| `--primary` 主色 | `#2563eb` | `#376ade` | **待定**（见 §4） | 二选一，建议 `#2563eb` |
| `--primary-600` | — | — | `#1d4ed8` | hover/active 深一档 |
| `--primary-50` 浅底 | — | `#eaf0fd` | `#eff4ff` | 选中态背景 |
| `--primary-text` 选中字 | — | `#315fc6` | `#1e40af` | 导航 active 文字 |
| `--secondary` 次色(电商) | — | `#218879` | `#218879` | 吸收 V2 的 teal，渠道区分 |
| `--secondary-50` | — | `#edf4f1` | `#edf4f1` | 次色浅底 |
| `--track` 进度轨 | `#e9edf1` | — | `#e9edf1` | 统一 |
| `--danger` 危险/负 | `#b42318` | — | `#b42318` | 统一 |
| `--success` 成功/正 | `#067647` | — | `#067647` | 统一 |
| `--radius` 卡片圆角 | `10px` | `5-6px`(控件) | `10px` + `--radius-sm:6px` | 卡片用大、控件用小 |
| `--shadow` | `0 1px 2px/.06, 0 1px 3px/.1` | — | 沿用 V1 | 轻阴影统一 |

**新增语义（V2 有、V1 缺）**：`--secondary` 双色强调、`--border-strong` 聚焦描边、`--radius-sm` 控件圆角。

---

## 2. 组件库清单（统一后）

| 组件 | 现状 | 统一方案 |
|---|---|---|
| 看板容器 | V1：12 栅格 `.dashboard` + `.span-N` | **保留 V1 栅格**为 SoT；V2 概览页改为"一个 span-12 概览模板" |
| 卡片 Card | V1：白卡/轻阴影/圆角 | SoT，吸收 V2 的 KPI+donut 内联布局 |
| KPI 指标 | V1：大数字+进度条（34px, tabular-nums） | SoT；V2 的"大数字+饼图+图例"作为 KPI 的变体 |
| 侧边栏 Sidebar | V2：固定 210px + 品牌 + 导航 active + 外链图标 | **吸收进 V1**，作为概览模板的布局骨架（原 V1 只有顶部 `.topnav`） |
| 顶栏 Topbar / 面包屑 | V2：有 | 吸收为概览模板页头 |
| 筛选 Filter | V1：`.filters` 下拉；V2：`.filter-band` | 合并为一套 `.filter-bar`（响应式 850/480 断点沿用 V2） |
| 表格 Table | V1：吸顶/排序/合计/滚动（最完整） | **SoT**；V2 的搜索/导出/排序并入 |
| 趋势图 Trend | V1：echarts 卡；V2：段控(线下/电商)切换 | 统一 echarts 容器高度约定（V1 280 / V2 310 → 取 300） |
| 饼图/占比 | V2：channel-pie + legend | 新增为标准组件（V1 缺） |
| 环比 Delta | V1：升绿降红（`.delta.up/down`） | **配色待定**（见 §3） |
| 状态态 | V1：loading(`aria-busy`)/empty/error/page-error 全套 | **SoT**；V2 补上 error/empty 态（现仅有"加载中"） |
| 图标 | V2：lucide `<i data-lucide>` | 统一引入 lucide（V1 现无图标体系） |

---

## 3. 涨跌色约定（✅ 已确认：涨红跌绿）

V1 现用 **正绿负红**（国际惯例，`v1_style.css:242-248` + `v1_dashboard.js:63` 注释"正绿负红"）。国内经营/股票场景多为 **涨红跌绿**。

**决策（2026-09-15 dodo 拍板）**：默认 **涨红跌绿**（贴合老板看国内报表心智；A 股红=涨）。落地为 `tokens.css` 的 `--up: #b42318` / `--down: #067647`。

> 关键区分：**环比方向**用 `--up/--down`（涨红跌绿）；**健康度（完成率是否达标）**用 `--severity-*` 语义色（红=落后需干预，绿=达标）。同一数字不混用两套色（见 `tokens.css` 注释）。若做国际化 SaaS，仅翻 `--up/--down` 取值即可，语义告警层不变。

---

## 4. 架构建议（两条路线）

- **路线 A（轻量，先落地）**：抽一个 `bi-ui/` 设计系统包（`tokens.css` + `base.css` + `components.css`），V1、V2 都 `@import` 同一份。双服务器保留，但样式同源，门户跳详情不再断裂。**改动小、风险低**，本周可完成。
- **路线 B（彻底，后做）**：把 V2 的经营总览做成 V1 引擎里的一个"概览看板模板"（sidebar/topbar 作为模板布局），**合掉 18081 这台静态服务器**，统一由 18080 的 uvicorn 服务。需后端配合，且要补 V1 缺的 sidebar/饼图组件。

**建议：先 A 止血（视觉统一），再 B 根治（架构统一）。**

---

## 5. 落地步骤

| Phase | 内容 | 验收 |
|---|---|---|
| 0 | 补齐后端 `/api/v1/*`（前置依赖，见 §0） | 两个页面能拉到真实数据 |
| 1 | 产出 `bi-ui/tokens.css`（§1 表），V1/V2 各自的 `:root` 改为 `@import` | 双站色值一致，无硬编码差异 |
| 2 | 产出 `bi-ui/components.css`，合并表格/筛选/状态态/图标(lucide) | V2 补 error/empty 态；V1 补 sidebar/饼图 |
| 3 | echarts 来源统一（V1 改本地，跟 V2 一致，离线可用） | 断网也能出图 |
| 4（B 路线） | V2 概览模板化进 V1，下线 18081 | 单服务器、单设计系统 |

---

## 6. 待确认（✅ 已全部拍板，2026-09-15）

1. **范围**：⚠️ **已由用户改为路线 B（Vite+React 重建）**。详见新增 `bi-react/`（见 §7）。原因：VISUALIZATION.md 默认 React 生态，且 B 能真正收口三套前端为「一个 React 应用 + 一套 CubeSchema 契约 + 一套 bi-ui 设计系统」。原 A 的 `bi-ui/` 三件套改为被 React 应用 `@import` 复用，不再各自 `<link>`。
2. **主色**：✅ **`#2563eb`**（V1，弃 V2 `#376ade`）。
3. **涨跌色**：✅ **涨红跌绿**（国内惯例）。
4. **echarts**：✅ **本地引入**（V1 弃 jsdelivr CDN，与 V2 一致，离线可用）。
5. **口径所有权**：✅ **后端 CubeSchema 统一算**，前端只渲染、不再各算缺口/告警/环比。

---

## 7. 已落地产物（bi-ui/）

| 文件 | 内容 | 状态 |
|---|---|---|
| `bi-ui/tokens.css` | 合并 token（主色/四级告警/涨跌/间距/圆角/阴影/字体），每条带来源注释 | ✅ 已产出 |
| `bi-ui/base.css` | reset + 排版 + tabular-nums + focus-visible + reduced-motion + 12 栅格 | ✅ 已产出 |
| `bi-ui/components.css` | 卡片/KPI/进度条/表格(吸顶+排序+合计)/导航/告警chip/结论条/异常清单/饼图/状态态 | ✅ 已产出 |
| `bi-ui/CubeSchema.md` | 统一取数契约：缺口/告警/环比口径 + 字段规范 + 缺陷显式化 + 对比度核验 | ✅ 已产出 |
| `bi-ui/VISUALIZATION.md` | 可视化组件 ↔ CubeSchema 绑定 + 钻取事件中心 + 图表库选型 + 大数据/边界优化 + 性能/错误监控标准 | ✅ 已产出 |
| `bi-ui/MIGRATION.md` | V1/V2 接入 `<link>` 顺序 + 待删块清单 + echarts 本地化（A 路线用；B 路线改为 React `@import`，见下） | ✅ 已产出 |

### 新增：bi-react/（路线 B · Vite+React 重建，2026-09-15 选定）

| 文件 | 内容 |
|---|---|
| `bi-react/ARCHITECTURE.md` | 组件化方案：哑渲染器原则 / 数据层 / 原语渲染器 / 布局(react-grid-layout) / 交互中心(层系下钻+联动) / 错误·性能·截图·代码编辑 / 与 V1·V2 收口关系 / 迁移步骤 |
| `bi-react/package.json` `vite.config.ts` `tsconfig*.json` `index.html` | Vite+React+TS 脚手架；dev 代理 `/api`→`18080` 复用现有后端契约；`build.sourcemap:true` 用于错误定位 |
| `bi-react/src/types/cube.ts` | 类型化 CubeSchema（对接 `bi-ui/CubeSchema.md`）：缺口/告警/环比由后端算，前端只渲染 |
| `bi-react/src/data/{cubeClient,useCube}.ts` | 取数层：`fetchCube('/api/v1/d/{path}')` + 轮询 hook（loading/error/data 三态） |
| `bi-react/src/components/common/*` | `ErrorBoundary`（componentDidCatch 等价）+ `Loading/Empty/Error` 三态 |
| `bi-react/src/components/renderers/*` | `ScalarCard`/`ChartCard`/`TableCard`/`AlertChip`/`ConclusionBar`/`AnomalyList` —— 全部吃标准 CubeSchema |
| `bi-react/src/components/layout/*` | `DashboardGrid`(react-grid-layout 拖动缩放) + `Sidebar`(V2 门户 chrome) + `renderWidget` 路由 |
| `bi-react/src/dashboards/registry.ts` | 看板注册表：`overview`(V2门户) / `l2-region`等(V1路径) / `l1-cockpit`(异常驱动) 统一收口 |
| `bi-react/src/hooks/useDrill.ts` | 交互中心：层系下钻 + 图表联动事件总线（zustand） |

**运行**：`cd bi-react && npm i && npm run dev` → http://localhost:18090 （`/api` 已代理到 18080）。

**下一步（按优先级，B 路线）**：
1. **P0** 后端补齐 `/api/v1/*`（§0 前置）—— 不解则统一后仍空板。
2. **P1** 按 `MIGRATION.md` 在 18080/18081 接入 `bi-ui/`，删各自重复 token/组件块。
3. **P1** 把异常驱动看板的四级告警 + 结论条 + 异常清单接入 `bi-ui/` 组件（避免第三套样式）。
4. **P2** CubeSchema 落地后，前端移除所有 JS 内的缺口/告警/环比计算（§0.5 铁律）。
