# BI 可视化组件与前端工程规范（bi-ui 扩展）

> 定位：本文件是 `CubeSchema.md`（数据契约）与 `components.css`（UI 骨架）之间的**桥梁**——
> 定义「图表组件如何消费 CubeSchema、如何交互、如何保证性能与可观测性」。
> 关系：`CubeSchema.md` 定义*数据长什么样*；`components.css` 定义*UI 骨架长什么样*；本文定义*组件怎么用这些数据*。
> 来源：用户提供的 BI 前端能力全景（可视化库 / 拖拽缩放 / 代码编辑 / 截图 / 架构·性能·错误监控 / 数据模型与渲染引擎 / 钻取）。

---

## 1. 图表库选型（决策）

| 库 | 角色 | 决策 |
|---|---|---|
| **ECharts** | 主图表引擎（折线/柱/饼/donut/散点/热力） | ✅ 采用，**本地引入**（已定 §6#4，离线可用） |
| **AntV (G2/G2Plot)** | 备选（关系图、图编辑、图分析） | ⏸ 暂不引入，避免双引擎维护；确有专属图型再评估 |
| **react-grid-layout** | 看板卡片拖拽/缩放布局 | ⚠️ 见 §5 —— 前置是 React 栈，当前原生 JS 装不下 |
| **CodeMirror** | SQL / 数据源代码编辑（高亮、实时校验） | ✅ 数据集/SQL 输入场景采用 |
| **html2canvas** | 看板截图导出（HTML → Canvas → 图片） | ✅ 导出分享场景采用 |

> 选型原则（来自材料）：库需**活跃社区 + 大规模数据能力 + 可离线**。ECharts 满足，故为 SoT；AntV 仅作补充，不并行维护。

---

## 2. 图表组件 ↔ CubeSchema 绑定（核心：组件无感知渲染）

组件**只读 schema 描述**，不写死字段名；字段含义由 `CubeSchema.md` 定义。

| schema 字段 | 组件用途 |
|---|---|
| `dimensions`（dept/name/store/channel/时间） | x 轴分类、图例、下钻节点 |
| `measures`（target/done/...） | y 轴系列、KPI 值 |
| `hierarchies`（dept→person 等） | 下钻/上卷的层级路径 |
| `derived`（shortfall/rate/severity/mom） | 颜色/排序/告警 chip —— **前端只读取，绝不重算**（§0.5 铁律） |

### 图表类型 × 绑定字段映射

| 图表 | 适用场景 | 绑定 |
|---|---|---|
| 折线 / 面积 | 趋势（日销、环比） | x=时间维度，y=measure |
| 柱状 / 条形 | 排名对比（缺口 TOP10） | x=维度(name/dept)，y=measure 或 shortfall |
| 饼 / donut | 构成占比（渠道/品类） | 维度=扇区，measure=值 |
| 散点 | 双指标分布 | x/y=两个 measure |
| KPI 卡 | 标量 | measure + rate + mom(方向色) |
| 异常清单 | 需今天动作的人 | severity 分级 + shortfall 降序（见 `CubeSchema.md §2.3`） |

---

## 3. 数据钻取（下钻 / 上卷）

- 协议已定义于 `CubeSchema.md §3`：**下钻 = 切 grain + 追加 query**，组件无感知。
- 交互流：图表 `click(node)` → `drill(dimension, node)` → 请求下一层 `schema.rows` → 复用同一组件渲染。
- **事件中心（Event Center）**：图表联动（筛选 broadcast）、上卷（breadcrumb 回退）走统一事件总线，**禁止组件间硬编码耦合**。联动/上卷即材料所述"数据能力"。

---

## 4. 大数据与边界展示优化

- **大数据**：ECharts `large: true` / `progressive` 分片渲染；大数据集走 `dataset` + `sampling`。
- **边界数据**（`CubeSchema.md §5`）：`null`→`—`、店铺数 0→「待补」、173% 预测→低置信灰标；图表对极端值做**截断/标注**，不静默渲染。
- **渲染性能**：GPU 渲染（ECharts 默认 canvas）、缓存静态层不重绘、聚合计算放 **Web Worker**，避免阻塞首屏。

---

## 5. 看板布局与拖拽缩放

- `react-grid-layout` 实现卡片拖拽/缩放，配置 `{i, x, y, w, h}` 栅格。
- ⚠️ **前置冲突**：当前 V1/V2 是**原生 JS（无构建链）**，react-grid-layout 等 React 库需引入 Vite/Webpack → 属**架构级决策**，不在路线 A（只统一 CSS）范围，落到路线 B 或更高。先记录为待定，不强行接入。

---

## 6. 性能监控（标准）

| 指标 | 采集点 | 目标 |
|---|---|---|
| 首屏看板加载时长 | navigationStart → 首屏卡片 paint | < 1.5s |
| 整看板加载时长 | → 末卡 paint / 数据就绪 | < 3s |
| 单图渲染时长 | echarts `setOption` 前后 | < 200ms |

- 采集：`performance.mark/measure` + 自定义 beacon 上报监控后端。
- **与 CubeSchema 配合**：数据拉取时长单独埋点，区分「数据慢」vs「渲染慢」（前者归后端 P0，后者归前端优化）。

---

## 7. 错误监控（标准）

- React 栈：`componentDidCatch` / 错误边界（Error Boundary）。
- 原生栈（当前 V1/V2）：`window.onerror` + `unhandledrejection`。
- **sourceMap 定位源码**：生产构建保留 map 或上传监控平台。
- 看板级错误：复用 `components.css` 的 `.error` / `.page-error` 展示，同时上报（不静默失败）。

---

## 8. 待你拍板（与统一方案 §6 呼应）

| # | 问题 | 影响 |
|---|---|---|
| 1 | **前端栈**：继续原生 JS（路线 A 兼容）还是迁移 React（路线 B + §5/§7 才能完整落地）？ | 决定 react-grid-layout / 错误边界能否用 |
| 2 | **AntV**：是否引入（双引擎维护成本）？ | 关系图等专属图型供给 |
| 3 | **监控后端**：性能/错误上报到哪（自建 / Sentry / 接现有）？ | 决定 §6/§7 上报落地方式 |

> 无论栈怎么选，`CubeSchema.md` 的数据契约与 `components.css` 的 UI 骨架不变——它们是栈无关的。
