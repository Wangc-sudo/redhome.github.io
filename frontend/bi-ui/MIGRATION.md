# bi-ui 接入指南（V1 / V2）

> 目标：把 18080（bi-web）与 18081（DIGITAL OPS）的样式统一到 `bi-ui/` 三件套，消除双套 token / 组件。
> 范围：路线 A（只统一 CSS，双服务器保留）。不动业务逻辑、不动后端。

---

## 0. 部署 bi-ui 静态资源

两个服务器各自独立，需把 `bi-ui/` 放到各自能服务的静态路径下：

```
# 18080 (bi-web)：放进 web/ 同级
<root>/web/bi-ui/tokens.css
<root>/web/bi-ui/base.css
<root>/web/bi-ui/components.css

# 18081 (DIGITAL OPS)：放进静态根
<root>/bi-ui/tokens.css
<root>/bi-ui/base.css
<root>/bi-ui/components.css
```

> 若两站能共享一块静态存储（如同一对象桶 / NFS），只需一份 `bi-ui/`，两站都引用同一 URL 前缀，后续升级只改一处。

---

## 1. V1（18080 / bi-web）

### 1.1 在 `web/index.html` 的 `<head>` 内、原 `web/style.css` **之前**加三行：

```html
<link rel="stylesheet" href="/web/bi-ui/tokens.css">
<link rel="stylesheet" href="/web/bi-ui/base.css">
<link rel="stylesheet" href="/web/bi-ui/components.css">
<link rel="stylesheet" href="/web/style.css">   <!-- 仅留 V1 专属覆盖，可大幅瘦身 -->
```

### 1.2 从 `web/style.css` 删除（已被 bi-ui 覆盖，避免重复定义打架）：

| 区块 | 行号（原始 v1_style.css） | 说明 |
|---|---|---|
| `:root { … }` 全部 token | 8-19 | 改由 tokens.css 提供 |
| `*` / `body` | 21-34 | 已在 base.css |
| `.dashboard` + `.span-*` | 36-64 | 已在 base.css |
| `.card` / `.card h2` / `.card-body*` | 66-100 | 已在 components.css |
| `.kpi-*` / `.progress*` | 102-131 | 已在 components.css |
| `.error` / `.page-error` | 133-148 | 已在 components.css |
| `.topnav*` / `.filters*` | 150-206 | 已在 components.css |
| `.data-table*` / `.empty-cell` | 208-236 | 已在 components.css |
| `.delta*` | 238-253 | 已在 components.css（涨红跌绿） |
| `.page-head*` / `.page-actions*` | 261-308 | 已在 components.css |
| `.table-*` / `.th-sort*` / `tfoot` | 310-403 | 已在 components.css |

删完后 `web/style.css` 只保留 **V1 独有的 JS 配套微调**（如有），否则可整文件删除、仅留三行 bi-ui link。

### 1.3 echarts 本地化（§6 决策 #4）

`web/dashboard.js` 当前从 jsdelivr CDN 引入 echarts → 断网即挂。改为本地：

```html
<!-- 删除：<script src="https://cdn.jsdelivr.net/npm/echarts@...></script> -->
<script src="/web/echarts.min.js"></script>   <!-- 与 V2 同源，离线可用 -->
```

把 `echarts.min.js` 放到 `web/` 下（V2 已有 `/echarts.min.js`，可复用同一份）。

---

## 2. V2（18081 / DIGITAL OPS）

### 2.1 在 `index.html` 的 `<head>` 内、原 `style.css` / `pages.css` **之前**加三行：

```html
<link rel="stylesheet" href="/bi-ui/tokens.css">
<link rel="stylesheet" href="/bi-ui/base.css">
<link rel="stylesheet" href="/bi-ui/components.css">
<link rel="stylesheet" href="/style.css">
<link rel="stylesheet" href="/pages.css">   <!-- 仅留 V2 专属覆盖 -->
```

### 2.2 从 `style.css` / `pages.css` 删除（已被 bi-ui 覆盖）：

| 区块 | 原始位置 | 说明 |
|---|---|---|
| `:root{--ink/--muted/--line/--blue/--teal/--bg}` | v2_style.css:1 | token 已并入 tokens.css（`--blue` 弃用，主色用 `--primary`） |
| `.sidebar*` / `.brand*` / `.nav-*` / `.external` / `.sidebar-bottom*` / `.db-icon` | v2_style.css | 已在 components.css（配色改 token） |
| `.workspace` / `.topbar` | v2_style.css | `.topbar` 已在 components.css（`.breadcrumb` 复用） |
| `.filter-band*` / `#page-filters*` / `.page-analysis` / `.table-tabs*` / `.text-cell` / `.rate-indicator` / `.table-footer` / `.sales-mix*` / `.chart-large*` / `.chart-error` / `.detail-section` / `#detail-*` | v2_pages.css | 已在 components.css |

删完保留：V2 独有的**布局骨架**（如 `.workspace{margin-left:210px}` 让出侧边栏宽度）这类 1-2 行定位规则，以及任何未被 bi-ui 覆盖的业务特例。

> 注：V2 用 `color-mix` 的地方（如导航 active 底色）已在 components.css 用 `color-mix(in srgb, var(--primary) 12%, #fff)` 实现，无需保留原硬编码。

---

## 3. 验证（接入后）

1. 打开 18080 `/d/l2-region` 与 18081 `/`，**两站主色一致（蓝 #2563eb）、卡片圆角/阴影一致、表格吸顶一致**。
2. 断网刷新两站，图表仍出图（echarts 本地）。
3. V1 的 `.delta.up` 现应为**红**（涨红跌绿）；V2 无 delta 不受影响。
4. 门禁：接入后跑 V1 的 pre-commit（ruff/eslint/prettier）确认无 CSS 语法错；无新增 JS 计算缺口/告警（§0.5 铁律）。

---

## 4. 异常驱动看板（L1 驾驶舱）接入

该看板此前用第三套 Primer token（`#d1242f` 等）。统一后：
- 其 `--danger/--warn-strong/--warn/--ok/--info` 直接映射到 `tokens.css` 的 `--severity-p0/p1/p2/ok` 与 `--info`，**删除其自带 :root**。
- 结论条 / 异常清单 / 四级告警 chip 用 `components.css` 的 `.conclusion-bar` / `.anomaly-list` / `.alert-chip`，不再自写样式。

详见 `CubeSchema.md`（口径已由后端算，前端只渲染）。
