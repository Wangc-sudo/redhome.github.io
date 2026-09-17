# bi-react · 进度与缺陷台账

> 主线 C 第一批增量（契约适配 + 首页打通）。日期：2026-09-15。
> 上游：`bi-ui/CubeSchema.md`（数据口径）、`bi-ui/VISUALIZATION.md`、`ARCHITECTURE.md`、`README.md` 铁律。

> **✅ 已与后端对拍一致 @ `tests/fixtures/derived_golden.json`（2026-09-15）**
> 对拍字段：**shortfall / rate / required_daily，24 条派生用例全部逐位一致**，冲突台账已清空。
> 范围说明（不是"部分一致"的粉饰，是职责边界）：`severity` 与**日历推导**已移出前端比对范围——前者由后端
> `common/bi_web/derived.py` 产出、前端分支按裁决删除；后者真相源是 `dim_calendar`，前端零推算。
> 这两项在测试里是**显式移除 + 挑明原因**（含"若有人把 severityOf 加回来会变红"的守卫断言），不是 skip。

---

## 0. 本轮结论

| 项 | 结果 |
|---|---|
| `npm install` | ✅ 成功（86 包，19s；后续补 `react-resizable` / `vitest` / `@types/node` / `jsdom` / `@testing-library/react`） |
| `npm run typecheck` | ✅ 0 error（`tsc -p tsconfig.json && tsc -p tsconfig.node.json`，含测试文件） |
| `npm run build` | ✅ 成功；已做依赖分包（见 §6-3）：主包 **26.08 kB / gzip 10.18**，react 133.97/43.16、grid 80.63/22.46、echarts 1059.73/352.45 |
| `npm run dev` | ✅ 起来并验证：`:18090/` → 200，`:18090/api/v1/dashboards` 与 `:18090/api/v1/d/l1-cockpit/cards/kpi_offline_mtd` 经代理返回与直连 18080 **一致**的 JSON；验证后已关闭 |
| `npx vitest run` | ✅ **76 passed / 4 skipped**（adapter 12 + 派生卡 12 + derive 15 + golden 对拍 34 + 日历用例 skip 4 + App 冒烟 3） |
| 与原生页数值比对 | ⚠️ 静态比对做不到：`http://127.0.0.1:18080/d/l1-cockpit` 只有 1196 字节的 JS 壳，数值全靠前端再拉 `/api/v1`，抓不到渲染后数字。改为**同源比对**：React 版消费的正是同一个 `/api/v1/d/{id}/cards/{card}`，代理转发返回与直连一致，故数值同源 |

---

## 0.1 集成批次：接住 `kpi_shortfall` / `anomaly_top`（后端已产出口径）

**结论：两张卡已归一、已渲染、已断言；未改任何渲染器的业务逻辑。**

| 项 | 结果 |
|---|---|
| `npm run typecheck` | ✅ 0 error |
| `npm run build` | ✅ 718 modules / 8.0s |
| `npx vitest run` | ✅ **39 passed**（adapter 12 + 派生卡 10 + derive 14 + App 冒烟 3） |
| 真机 payload 校验 | ✅ 直连测试库 `mart_ops_test`（127.0.0.1:13306）跑 `queries.run_kpi_shortfall/run_anomaly_top` 抓到真实响应，据此写 fixture（临时探针脚本用完即删，未改后端代码） |

### 改动（全部在适配层 / 展示层，渲染器无算式）
- 新增 `src/data/severity.ts`：`SEVERITY_DOMAIN` + `alertOf()`（code → {label, reason} 查表）。独立成文件是因为**后端已自己产出 severity**，这份映射在 `derive.ts` 被删之后仍要保留；`derive.ts` 改为复用它，消灭第二份文案表。
- `adapter/cardToCube.ts`：
  - 新增 `backendDerivedOf()` —— 行里有 `severity` 即视为「后端已算」，只做字段搬运（shortfall / rate / required_daily / severity），**不重算**；`deriveCube` 按 rowKey 已存在即跳过，故不会出现「后端 p2、前端显示 p0」。
  - 行主键优先取 `name` 列（anomaly_top 首列是 `rank`，不能拿名次当主键）。
  - table 透传 `unit` / `as_of`；新增 `percent` / `severity` 两种 format；派生列打 `derived:true` 标记。
- `TableCard.tsx`：`format==='severity'` 列渲染 `AlertChip`（色+文字，色盲友好）；有独立告警列时首列不再重复挂 chip；比率列不参与合计。
- `mock/fixtures.ts`：两张卡按**真机形状**落 fixture（grain=person，带「部门」列；含一条 target=null 的不可算行）。
- `types.ts`：TablePayload 补 `unit/as_of/month/grain/limit/severity_domain`。

### 单测（`src/data/adapter/derivedCards.test.ts`，10 例）
- 列序/格式/as_of/unit 按后端；行主键 = 姓名（rank 列不抢占）。
- **反例钉死**：造一行 `done=0` 的行（derive.ts 判 `p0`），后端给 `p2` → 断言最终是 `p2`。
- 后端浮点数原样透传：`requiredDaily === 42916.666666666664`（本地按 22 工作日重算是 46818.18）。
- 无目标行 → `missing=true`、不臆造告警；`anomaly_top` 名次列 + 派生按 name 索引。
- 回归：没有 severity 的表（table_people_mtd）仍走 `derive.ts`（p0）。

### ⚠️ 待裁决 / 待操作（未擅自处理）
1. **18080 需要重建，不是重启**：它是容器 `public-data-integration-bi-web`（`docker-compose.integration.yml`，build context=仓库根，**无 mount**，代码与 `bi.seed.yaml` 都烤在镜像里）。当前镜像仍是 9 张卡的旧注册表：`/d/l1-cockpit/cards/kpi_shortfall` 返回 `{"detail":"not_found"}`。生效命令：
   `docker compose -f docker-compose.integration.yml --profile bi-web up -d --build bi-web`
   —— 会重建镜像并短暂中断，可能打断 metrics-semantics 正在做的后端联调，**所以我没有擅自执行**，等你一句话（只重启无用）。
2. **工作日口径差异（留给 golden 对拍）**：真机 payload 里 `elapsed_workdays=13` + `remaining_workdays=11` → 后端月内 **24 个工作日**；`derive.ts` 按周一至周五算得 **22**（2026-09 实际 22）。severity 不受影响（后端算），但 `required_daily` 会差（1030000/24=42916.67 vs /22=46818.18）。按 CubeSchema.md 裁判，等 `derived_golden.json` 落地后一起定，我**不会**先改 TS 去迁就。
3. **grain 默认是 person 不是 region**：真机 `kpi_shortfall` 默认 `grain=person`（列含「部门」），前端不假设粒度，按 columns 渲染。

---

## 0.2 跨线口径对拍：`derive.ts` ←→ `tests/fixtures/derived_golden.json`（24 派生 + 4 日历）

驱动测试：`src/data/deriveGolden.test.ts`（34 例 + 4 条 skip；B 于本轮新增第 4 条日历用例
`calendar_real_dim_calendar_2026_09`（24/13/11，与真机一致），自动落入 skip 组，未改一行测试代码）。

**最终结果（2026-09-15 二次裁决后）：24 条派生用例在 shortfall / rate / required_daily 上全部逐位一致，冲突台账 = 空。**

| # | 用例 | 字段 | 归宿 |
|---|---|---|---|
| ① | `missing_target_everything_none` | severity | **不再适用**：前端 severity 分支已删，该字段移出比对范围（不是 skip，是前端不再有第二份实现） |
| ② | `missing_done_counts_as_no_sales` | shortfall / rate | **已一致 ✅**：team-lead 改判「后端 SQL 是 `COALESCE(SUM(d.done),0)` 且左表由 target 驱动 ⇒ done 缺失 = 真的没开单」→ 改 TS 认 golden（`shortfallOf`/`rateOf` 的 done 缺失按 0 计） |
| ③ | `missing_calendar_p1_undecidable_falls_to_p2` | required_daily | **已一致 ✅**：删掉本地推算后 TS 为 `null`，与 golden 一致 |
| ④ | `missing_remaining_workdays_degrades_to_p2` | severity | **不再适用**：同 ①（p1 阈值属告警判定，随分支一起删除） |

- **severity 移出比对范围的处理方式**（team-lead 要求"移除而非 skip"）：`FIELDS` 只留 3 个派生字段；另加两条守卫——`derive.ts` 不得再导出 `severityOf / P1_COEFF / P0_MIN_ELAPSED_WORKDAYS`（加回来即变红），且 golden 里 24 条 severity 期望**仍在**（Python 侧继续对拍，只是前端不再有对应实现）。
- **日历用例 4 条：显式 `describe.skip`**，注明「前端不实现：日历由后端 `dim_calendar` 提供」；skip 组用 `it.each(calendar_cases)` 遍历 ⇒ 新增用例**自动纳入**（已验证：B 的 `calendar_real_dim_calendar_2026_09` = 24/13/11 进来后无需改本文件）；夹具自检也用 `≥3` 的包容断言。另留断言钉住 `derive.ts` 不再导出 `workdaysInMonth/elapsedWorkdays/remainingWorkdays`。
- **mom（§2.4）**：6 条 mom 用例的 3 个派生字段全部一致；`mom` 本身 out of scope（环比由后端 ScalarCard `delta_pct` 产出），已钉住「derive.ts 不实现 momOf」，将来若补会变红提醒补对拍。
- 测试写法：24 条走真断言；冲突台账**保留空清单 + 两条守卫**（"冲突集合 == 已登记清单"、"每条登记冲突仍真实存在"）——将来任一侧漂移都会变红，强制重走裁决，不会悄悄粉刷成全绿。
- 现状：`typecheck` ✅ 0 error / `vitest` ✅ **73 passed + 4 skipped** / `build` ✅。

---

## 0.3 三项裁决落地：工作日 / severity 分支删除 / done 缺失按 0

### A. 工作日：以 `dim_calendar` 为准，前端零推算

| 改动 | 位置 | 说明 |
|---|---|---|
| 删除 `workdaysInMonth` / `elapsedWorkdays` / `remainingWorkdays` | `src/data/derive.ts` | 周一至周五那套兜底彻底移除；改由 `WORKDAY_KEYS` 声明「只认后端字段」 |
| `requiredDaily(target, totalWorkdays)` | 同上 | 去掉 `ref` 兜底参数；总工作日 null/≤0 → **返回 null**，不再回落 22 |
| `deriveRow(row)` 读行内 `total_workdays` | 同上 | 取不到 = 不可算，UI 显示「—」（§5 数据缺陷显式化） |

### B. severity 分支：查证后删除（team-lead 改判二）

**查证结论：后端注册表里没有任何一张卡依赖前端算 severity。**

| 查证对象 | 结果 |
|---|---|
| l1-cockpit 旧 9 张卡 | 唯一的表卡 `table_channel_mtd` 列是 `sales/promo/roi/stores`，**没有 target/done**；`kpi_annual_progress` 有 target 但它是 scalar（不进派生） |
| 全注册表里带 target 的表卡 | 只有 `table_people_leaderboard`（`rank/name/dept/completed/target/rate/unfilled`）——**后端不下发 severity 列**，且「已完成」列叫 `completed`，derive.ts 本就不认 |
| 告警需求落在哪 | `kpi_shortfall` / `anomaly_top`：后端 `derived.py` 已产出 severity，经 `backendDerivedOf` **原样透传** |

⇒ 删除 `severityOf` / `SeverityInput` / `P1_COEFF` / `P0_MIN_ELAPSED_WORKDAYS`，`deriveRow` 不再产出 `alert`；删除处留注释指向 `common/bi_web/derived.py`。
`data/severity.ts`（code → chip 查表）**保留**——后端 severity 仍需要它来出 chip。

### C. done 缺失按 0（team-lead 改判一）

`shortfallOf` / `rateOf` 的 done 缺失按 0 计（后端 `COALESCE(SUM(d.done),0)` + 左表由 target 驱动 ⇒
能出现在结果集就说明有目标、窗口内无销单 = 0）。**但 `deriveRow` 额外加了一道闸**：
行里必须存在 `done` / `sales` 列才派生，**没有该列（如人员榜的 `completed`）绝不派生成 0**——
否则「没有这个字段」会被误当成「完成 0」，凭空造出一堆缺口。

### D. 顺带对齐：mock 的 l2-people 用回真实卡

`table_people_mtd`（mock 自造 id）→ `table_people_leaderboard`，列结构改成后端真实结构
（`rank/name/dept/completed/target/rate/unfilled`）。原来自造 id 是前端告警唯一的"用武之地"，
现在与真机一致：这张卡后端不产 severity，前端也不自造。冒烟测试改为断言「l2-people 不得出现告警 chip」。

---

## 0.4 bi-web 重建前后对比（放行前提：证明是纯新增、无回归）

**命令**：`docker compose -f docker-compose.integration.yml --profile bi-web up -d --build bi-web`

| 项 | 重建前 | 重建后 | 判定 |
|---|---|---|---|
| `l1-cockpit` 卡数 | 9 | **11** | ✅ 新增 `kpi_shortfall`、`anomaly_top`（插在 `pie_sku_mtd` 之后） |
| 卡清单 | kpi_offline_mtd, kpi_channel_mtd, kpi_annual_progress, trend_region_daily, bar_channel_mtd, table_channel_mtd, pie_sku_mtd, kpi_offline_dod, kpi_channel_dod | 前 7 项不变 + **anomaly_top, kpi_shortfall** + kpi_offline_dod, kpi_channel_dod | ✅ 纯新增，原有卡顺序未变 |
| `kpi_offline_mtd` | `{"chart":"scalar","value":4534023.0,"unit":"元"}` | **逐字节相同** | ✅ 无回归 |
| `trend_region_daily` | 12 个日期 × 2 条 series（杭州/绍兴） | **逐字节相同** | ✅ 无回归 |
| `kpi_shortfall` 真实载荷 | — | 后端已带 `shortfall/rate/required_daily/severity/remaining_workdays:11/elapsed_workdays:13`；`required_daily=1030000/24=42916.67` 反证 **24 工作日** | ✅ 印证裁决 |

结论：重建是**纯新增**，两张旧卡零回归。

---

## 0.5 `has_fact`（后端 2026-09-15 新增）：前端不接语义，只守两条不变量

后端在 `run_kpi_shortfall` / `run_anomaly_top` 每行加了布尔 `has_fact`，登记为第 5 类缺陷
（`docs/derived-metrics.md`）。**前端不为它新增任何判定分支**——语义归后端，前端只守住：

| 后端形态 | 前端行为 | 断言位置 |
|---|---|---|
| 给了 severity（含 has_fact=false 仍判 p0） | 原样透传 chip 与数值，**绝不改判** | `derivedCards.test.ts` ① |
| 没给 severity（指标全 null） | 交 `derive.ts`，done 列存在即按 0 ⇒ 缺口 = 目标（偏严） | ② |

**⚠️ 已上报 team-lead 的一处自相矛盾**（后端消息 vs golden，前端不替它选）：
- metrics-semantics 消息说 has_fact=false ⇒ 全 null、「—」、**不进 p0**；
- 同一份 golden 的 `row_cases.row_no_fact_with_target_is_also_p0` 却期望
  `shortfall=240 / rate=0 / severity=p0`（"LEFT JOIN 右表为空 = 挂零的另一名字"）。

⇒ 两边我都没动，只把当前行为钉成测试：**任一侧改口径都会变红**，强制重走裁决。
另：`row_cases`（行级组装对拍）尚未挂进 `deriveGolden.test.ts`——它测的是后端行组装，
前端没有对应实现（前端只做字段搬运），挂它需要一个"喂 fact dict → 断言整行"的假实现，
等 team-lead 决定是否值得。

---

## 0.6 收尾批（2026-09-16）：has_fact 诊断角标上线 + derive.ts 整文件删除

| 项 | 结果 |
|---|---|
| `npm run typecheck` | ✅ 0 error（前置修复 2 处既有损坏：`useDashboard.ts` 注释块外残留 2 行、`States.tsx` 重复 import） |
| `npm run build` | ✅ 成功；主包 26.08 → **25.44 kB**（gzip 10.12，derive.ts 移除）；echarts vendor 告警照旧（策略不变） |
| `npx vitest run` | ✅ **39 passed / 4 skipped**（adapter 12 + 派生卡 12 + golden 守卫 8 + 角标 4 + App 冒烟 3；skip 仍为原 4 条日历用例，无新增） |

### A. has_fact 诊断角标（roadmap P0 最后一项，仅渲染不参与判定）
- `TableCard.tsx`：行内 `has_fact === false` → 在 name 列（无 name 列退回首列，anomaly_top 首列是 rank 也挂对人）
  渲染中性「挂零」角标，复用 bi-ui `.badge-defect`；title 注明「不影响告警判定与数值」。**告警 chip 与数值零改动。**
- `fixtures.ts`：kpi_shortfall / anomaly_top 行补真机字段 `has_fact`（真机 42 人全 true）；
  另加 1 条「演示·挂零」行（has_fact=false + 后端 p0，形状按 golden `row_no_fact_with_target_is_also_p0`，注释标明非真机）。
- 新增 `TableCard.hasFact.test.tsx`（4 例）：只挂 false 行 / p0 chip 色·文案原样 / 无 severity 列只挂角标不造 chip / rank 首列时角标仍挂 name 列。

### B. derive.ts 整文件删除（roadmap P3；删除条件已满足）
- 条件核验：后端 `common/bi_web/derived.py` 就绪且 golden 对拍一致（§0 既有结论）；
  `VITE_DERIVE=0` 实测：仅 8 条「断言前端补派生」用例失败（即删除对象），后端透传 / golden / 冒烟全绿 ⇒ 关闭路径可行。
- 删除：`src/data/derive.ts`、`src/data/derive.test.ts`；`VITE_DERIVE` 从 `vite-env.d.ts` / `.env.example` 一并移除。
- `cardToCube.ts`：摘掉 `deriveCube` 注入；`backendDerivedOf` 语义改为「无 severity = 该行无派生，前端不补」。
- 测试改造（防回归语义保持）：
  - `deriveGolden.test.ts` → **文件级守卫**：① derive.ts/.test.ts 不存在；② src 无任何文件 import derive 模块（含动态）；
    ③ src 无任何文件重新导出 `workdaysInMonth/elapsedWorkdays/remainingWorkdays/severityOf/P1_COEFF/P0_MIN_ELAPSED_WORKDAYS/shortfallOf/rateOf/requiredDaily/deriveRow/deriveCube/momOf`（裁决 #3/#5 钉死清单全覆盖）；
    ④ golden 夹具完整（24 派生 + ≥3 日历 + severity/mom 期望仍在，逐位对拍职责移交 Python 侧）。日历 skip 组保留（仍 4 条，无新增 skip）。
  - `derivedCards.test.ts`：「后端 p2 / 前端 p0 以后端为准」反例**保持绿**；has_fact ② 与回归组改写为
    「无 severity ⇒ 前端零补算，渲染「—」+ 挂零角标」；矛盾待裁决的旧注释按 roadmap 作废声明更新。
  - `cardToCube.test.ts`：派生注入用例改写为「无 severity ⇒ 无 derived，行字段原样透传」。
- 行为影响面核验：生产卡片无一受影响 —— kpi_shortfall / anomaly_top 逐行带 severity（后端透传不变）；
  table_people_leaderboard（completed 列）与 table_channel_mtd（无 target）在 derive.ts 时代本就不产派生。

---

## 0.7 V1 发布（2026-09-16）：前后端拉齐 · 14 页全集上线

> 执行依据：`指南/前后端拉齐V1-执行提示词.md`（T1–T7）。多 agent 协同：backend-cards / frontend-adapter / preview-fixer + main 集成。

| 项 | 结果 |
|---|---|
| 卡片注册表 | **25 → 36**：资金安全 5 真卡（`fin_derived.py` 纯函数 + `fin_derived_golden.json` 逐位对拍，超期 >60 天默认「待财务确认」）+ ⑪ 体验馆克隆（`showroom_monthly`）+ 5 张 0 占位结构卡（不查库，`rows:[]` + 卡级 `has_fact:false`，列结构按需求表一次到位） |
| 同比率 null 规则 | `derived.yoy_rate`（除零/无基数 → null → 前端「—」，绝不 0%/-100%）+ golden `yoy_cases` 5 条（追加式，既有条目逐字未动） |
| seed 编排 | `bi.seed.yaml` 5 → **14 页**；全页 `refresh_seconds=86400`（T+1）；占位 5 页标题带「（待接入）」 |
| 前端 | 卡级占位态（p0 chip「应接入未接入」+「待接入」角标 + 列头照常 + 空态文案）；页脚统一「数据口径 T+1」；FilterBar 空候选禁用态；`/d/{id}` 路径直访初始路由兜底（hash 机制不变）；轮询源确认：仅 useCube 一条后端驱动链 |
| preview.html | 文案级修复 6 项（删"每 5 分钟自动刷新"、3 处单位 bug、效期→库龄、首页撤体验馆日销 KPI、品牌树"其中"标注、11 页上线状态标注），JS 逻辑未动 |
| 壳替换 | `app.py` `_shell_index_html()`：dist 存在即 serve React 壳；`BI_WEB_SHELL=legacy` 或删 dist 一键回退 V0；旧 `web/` 保留未删；`.dockerignore` 放行 dist、排除 node_modules |
| 测试 | Python **1080 OK**（本地 skipped=36；Docker 容器内 skipped=11，集成测试真实跑通）· 新增 42 例（后端 33 + 壳 9）；vitest **54 passed / 4 skipped**（新增 15 例，skip 无新增）；守门断言 25→36 同步追加、集合相等断言原样 |
| 冒烟 | 14 页 API/壳/载荷逐项可达；占位页挂零语义正确；资金安全真卡端到端绿；旧卡零回归 |

**不做清单（按提示词 §6）**：上云、播报 P0、WDT 入 mart、建新表等均未触碰。
**回退预案**：seed git 回滚；`BI_WEB_SHELL=legacy` 切回旧壳；占位页下线 = seed 删条目（未建表，无数据残留）。

---

## 0.8 资金趋势卡 SQL 下推 + 主体参数化（2026-09-17 P1+P2）

> 执行依据：`指南/资金趋势卡-SQL下推与参数化-执行提示词.md`（§3 P1 必做、§4 P2 按「主体会变」裁决执行）。

**为什么改**：4 张硬编码分屏卡（习水村/杭易/平澜路/民酒汇）把 300 行全表拉回 Python 过滤累加，4 卡 = 4 次全表 + 4 遍 Decimal 循环；且公司主体会变（新增/改名/下线），硬编码卡每变一次就要改代码。

| 项 | 结果 |
|---|---|
| SQL 下推（P1） | `_FIN_STORE_FUNDS_ENTITY_SQL` 改为 `WHERE company_entity = %s + GROUP BY month, channel + SUM(balance)`，值一律 `%s` 绑定；零填充（缺月补 0）不可下推，留 Python |
| 主体参数化（P2） | 4 张分屏卡 → 1 张 `trend_fin_store_funds_entity`（`params_schema={"entity": "entities"}`）+ 新增 `entities` 筛选源（`entity_options`：DISTINCT company_entity）；entity 空 = 全主体按渠道汇总（走不带 WHERE 的 `_FIN_STORE_FUNDS_CHANNEL_SQL`，绝不传恒真通配值） |
| 注册表 | **40 → 37**；`trend_fin_store_funds`（38 线原卡）保留未删（仅页面不挂载） |
| 筛选源对拍 | `config.KNOWN_FILTER_SOURCES` 与 `app._FILTER_SOURCE_QUERIES` 同时 += `entities`（`FilterSourceParityTests` 钉死） |
| seed 编排 | `l2-fund-safety` 挂页面级筛选（entity / entities / 公司主体），卡片 8 → 5，span `(4, 12, 12, 6, 6)` |
| 守门测试 | `test_bi_web_cards`（清单/映射/params_schema/37）、`test_bi_web_config`（filters/卡片/span）、`test_bi_web_app`（页面桩、`filter_params["l2-fund-safety"]=["entity"]`、dates 结构断言）六处+三处同步；新增 `FinEntityTrendTests` 4 例（%s 绑定形态 / 空 entity 无 WHERE / 渠道名清洗+零填充 / entity_options 形态） |
| 测试结论 | Docker 容器内 4 模块 **305 OK（skipped=1）**，无 FAILED/ERROR |
| 前端 | **零改动**：`FilterBar` 由 `def.filters` + `/api/v1/options/entities` 驱动，下拉自动出现；`card_cache_key` 已含参数，切主体各自命中缓存 |
| 冒烟 | `filters` 含 entity ✓；`options/entities` 返回 4 主体 ✓；无 entity → 全主体 7 条渠道线 × 8 月 ✓；`entity=习水村` → 6 条线、渠道名正常中文 ✓；非法 entity → **400 bad_request**（值域闸，非 500）✓ |

**遗留**：`region`（地域）映射规则未定，本批继续用 `company_entity`；口径确认后在 mart 投影层补 `dim_store` 或加列（提示词 §7 不做清单）。

---

## 0.9 原稿转正（2026-09-17 P0–P2）：原稿静态壳接管 /d/{id}，React 进入下线倒计时

> 决策（老板拍板）：`preview.html` 原稿视觉更好，取消 React + Vite 架构；原稿已单独发布保留。
> 执行依据：`指南/BI看板原稿转正-任务书.md` + `指南/BI看板原稿转正-多agent执行提示词.md`；
> 多 agent 协同：shell-forger（A 三件套）/ data-adapter（B 数据层）/ shell-switch（C 壳切换）+ main 集成。

| 项 | 结果 |
|---|---|
| 新壳 | `common/bi_web/web/bi.html`(35) + `bi.css`(255，原稿 token 一字未动) + `bi.js`(405，零依赖 SVG 图表库+卡片构造器，零演示数据) + `bi-data.js`(338，API 适配层：dashboards→definition→options→逐卡取数，WeakMap 竞态防护) |
| 壳选择序 | `app.py::_shell_index_html()`：**bi.html → dist（P3 前回退层）→ legacy**；`BI_WEB_SHELL=legacy` 一键回 V0；逐请求决策无需重启 |
| 契约映射 | 5 种 payload → 原稿组件：scalar→CardKpi（进度条/环比/trend7 迷你图）、line/bar/pie→SVG 图、table→CardTable（7 种 format + severity chip + `has_fact:false` 占位「应接入未接入」）；非法筛选值 → 卡内错误态（API 400），整页不挂 |
| 守门测试 | `test_bi_web_app`（壳断言+静态资源 8 例）、`test_bi_web_shell`（三级选择序全向钉死，main 集成时补同步——C 只跑了 app 模块，全量暴露后补齐） |
| 测试 | 全量 **1117 OK（skipped=11）**（`--build` 重建纪律） |
| 冒烟 | 14 页逐页 200；壳接线 bi.css/bi.js/bi-data.js 全 200；资金安全页 entity 下拉 4 选项、切主体生效、非法值卡内错误态；占位 5 页占位态正确 |
| 回退演练 | 一次性容器 `BI_WEB_SHELL=legacy` 起 18082：legacy 壳（dashboard.js）确认可用，演练后销毁 |
| 排障记录 | nginx 缓存旧 bi-web IP → 502（`docker restart nginx` 即恢复，今后重建 bi-web 后须同步重启 nginx）；`outputs/bi-preview/server.py`（9/14 起）长期占用 127.0.0.1:18081，直连口以 8088/容器内为准 |

**React 拆除（P3 / D 批）**：观察 3-7 天无回退诉求后，按 `多agent执行提示词.md` §7 独立派发：归档本文件 → 删 bi-react 目录 → 删 dist 检测与 `/assets` → 删 CI frontend job。
**本仓库（bi-react）自今日起冻结**：只读档案，不再接受功能改动。

---

## 1. 已修开工必修缺陷

| # | 位置 | 问题 | 处理 |
|---|---|---|---|
| 1 | `components/layout/renderWidget.tsx:13` | 用不存在的 `w.id`（`WidgetDef` 字段是 `i`），TS 必失败 | 文件整体删除 |
| 2 | `renderWidget.tsx:12-13` | 普通函数里调 `useCube`，且在 `DashboardGrid` 的 `map` 中被调用 → 违反 Rules of Hooks，N 卡状态串台 | 改为独立组件 `components/layout/WidgetCard.tsx`，每卡自己订阅共享缓存 |
| 3 | `components/layout/DashboardGrid.tsx:14` | localStorage 只写不读，刷新必回默认布局 | 初始化 `readStored()` + `mergeLayout()`（按 `i` 对齐，只覆盖用户拖过的卡）；新增「恢复默认布局」按钮 |
| 4 | `data/useCube.ts` | 每卡一条 30s 轮询 → 7 卡 = 7 倍请求 | 改为**看板级共享缓存**：`Map<key, Entry>` + 引用计数，同 key 并发只发一次请求、共用一条轮询链；周期取后端 `refresh_seconds`（实测 300s）；最后一个订阅者离开时 `AbortController.abort()` + 清定时器 + 释放缓存 |
| 5 | `App.tsx` | 当前页存 zustand，刷新回 overview、URL 不可分享 | 改 hash 路由 `#/d/{id}`（`router/useHashRoute.ts`），侧边栏用真实 `<a href>`，刷新/分享都能还原 |
| 6 | `vite.config.ts` | proxy 未转发 Bearer，`BI_WEB_TOKEN` 一开就 401 | `loadEnv` 读取 `BI_WEB_TOKEN`/`VITE_BI_WEB_TOKEN` 注入 `Authorization: Bearer …`；新增 `.env.example` |
| 7 | `package.json` | 未声明 `react-resizable`，但 `main.tsx:4` import 其 CSS | 补 `react-resizable ^3.0.5` |
| 8 | `tsconfig*.json` | `tsc -b --noEmit` 报 TS6310（引用项目不能 disable emit），改造前连基线都编译不过 | 去掉 project references，`typecheck` 改为两个 `-p --noEmit`；`build` 改为 `npm run typecheck && vite build` |
| 9 | `vite.config.ts` 缺 `process` 类型 | TS2580 | 补 `@types/node` |

---

## 2. 新增文件

```
src/data/types.ts                 后端契约类型（dashboards / cards / 5 种 payload）
src/data/errors.ts                BiWebError + 统一错误语义（与后端 HTTPException 对齐）
src/data/biWebClient.ts           四端点客户端（唯一 fetch 出口）+ VITE_MOCK 开关
src/data/adapter/cardToCube.ts    payload → CubeSchema 归一（按 chart 分派）+ formatCell
src/data/derive.ts                ⚠️ 过渡派生层（登记的临时债，feature flag）
src/data/mock/fixtures.ts         5 个看板 fixture（真实 payload 结构，经同一 adapter 归一）
src/data/useDashboard.ts          useDashboardList / useDashboardDef / useFilterOptions
src/router/useHashRoute.ts        hash 路由 #/d/{id}
src/utils/format.ts               金额缩放 / 百分比 / 涨跌 class（只格式化，不算业务）
src/components/layout/WidgetCard.tsx       单卡组件（取代 renderWidget）
src/components/layout/DashboardView.tsx    看板视图：定义 → 栅格 → 卡片
src/components/layout/FilterBar.tsx        后端 filters + /options 驱动的筛选条
src/components/renderers/PieCard.tsx       饼/环图渲染器
src/styles/bi-ui/{tokens,base,components}.css   设计系统落库（从 ../../bi-ui 拷贝，只读）
src/styles/app.css                仅应用外壳布局（sidebar 让位 / 栅格 / 拖拽把手）
src/vite-env.d.ts                 import.meta.env 类型
src/data/adapter/cardToCube.test.ts        适配层单测（12）
src/data/derive.test.ts                    派生层单测（14）
src/__tests__/app.smoke.test.tsx           App 渲染冒烟（2，走 mock 后端）
.env.example / .gitignore / vitest.config.ts
```

## 3. 改动文件

`App.tsx`（hash 路由）、`components/layout/DashboardGrid.tsx`（读写 localStorage + span 排布）、
`Sidebar.tsx`（`<a href>` + bi-ui 类名）、`components/common/States.tsx`（BiWebError 文案）、
`components/renderers/{ScalarCard,ChartCard,TableCard,AnomalyList,AlertChip,ConclusionBar}.tsx`
（全部改用 bi-ui 真实类名 `.card/.card-title/.kpi-value/.delta/.progress/.data-table/.alert-chip/.conclusion-bar/.anomaly-card`）、
`dashboards/registry.ts`（收敛为导航兜底）、`hooks/useDrill.ts`（默认看板改 l1-cockpit）、
`types/cube.ts`（补 `chart/unit/rowKeys/columns/format`）、`data/useCube.ts`（重写）、`styles/index.css`（相对路径）。

## 4. 删除文件

- `src/components/layout/renderWidget.tsx` —— hook 违规，已被 `WidgetCard` 取代
- `src/data/cubeClient.ts` —— 按**不存在的契约** `/api/v1/d/{path}` 实现，留着必被误用
- `src/data/derive.ts` + `src/data/derive.test.ts` —— 过渡派生层（2026-09-16 按路线图 P3 删除；后端 derived.py 为唯一真相源，防回归守卫见 deriveGolden.test.ts）

---

## 5. 与后端契约的差异清单（未改后端，全部前端适配）

| # | 差异 | 前端适配方式 | 建议后端（B 线） |
|---|---|---|---|
| D1 | `ARCHITECTURE.md` 假设 `GET /api/v1/d/{path}` 直接返回 CubeSchema；**后端无此端点**（实测 404） | 新增 `adapter/cardToCube.ts`：按 `chart` 把卡片 payload 归一成 CubeSchema，渲染器只看 CubeSchema | 若后端直接产 CubeSchema，删 adapter 即可，组件不动 |
| D2 | 后端以 `cards[]` 驱动看板，原 `registry.ts` 静态 widget 定义会与之打架 | `registry.ts` 收敛为**导航兜底**，布局/卡片/筛选一律取后端 `cards[]` | — |
| D3 | 后端无 `overview` 看板（只有 l1-cockpit / l2-region / l2-channel / l2-product / l2-people） | 默认路由 = 列表第一个（l1-cockpit）；V2 门户页归属待产品确认 | 需要门户页则补一个 dashboard id |
| D4 | 卡片 payload 是**可视化结构**（`dates/series`、`categories/values`、`items`），不是二维表 | adapter 归一：line→(date, series…)、bar→(category, value)、table→(columns, rows)、pie→(name, value)、scalar→单行+derived | — |
| D5 | ~~后端**不产出** CubeSchema §2 的派生指标~~ **已闭合（2026-09-16）**：后端 `derived.py` 产出 shortfall / severity / required_daily / 工作日口径 | ~~`data/derive.ts` 过渡补齐~~ derive.ts 已整文件删除；前端零算式，`backendDerivedOf` 只做字段搬运 | — |
| D6 | 后端卡**不带高度**，只有 `span`（12 栅格宽） | 前端按 chart 给默认高度（scalar 2 / line·bar·pie 5 / table 7，rowHeight 64） | 建议补 `min_h`/`h`，前端即透传 |
| D7 | `scalar` 卡的 `target/rate` 只有部分卡有；日环比卡给 `prev/delta_pct/trend7` | adapter 有则透传进 `derived`，没有就不臆造 | — |
| D8 | 筛选项候选值走 `/api/v1/options/{source}`，卡的 `params` 之外的键一律 400 | `pickParams()` 只带声明过的键；FilterBar 每个 select 独立取 options | — |
| D9 | `unit` 目前只见「元」，原始值动辄 7 位 | 前端只做展示缩放（≥1万→万，≥1亿→亿），原值不变（CubeSchema §6） | 建议后端统一给 `unit` + 原值 |
| D10 | 后端无结论条 / 异常清单卡 | `ConclusionBar` / `AnomalyList` 已实现但**未接线**（见 TODO） | 需要 L1「结论条 + 缺口 TOP」则补对应卡 |

---

## 6. 剩余 TODO

1. ~~**后端补派生口径** → 置 `VITE_DERIVE=0` → 删除 `src/data/derive.ts` 与 `derive.test.ts`~~：**已完成（2026-09-16，见 §0.6-B）**。
2. **结论条 / 异常清单接线**：后端给出 `conclusion` / `anomaly` 型卡后，在 `WidgetCard` 的 switch 里接上（组件已就绪）。
3. ~~**echarts 分包**~~：**已完成**（2026-09-15）。`vite.config.ts` 的 `build.rollupOptions.output.manualChunks`
   把 `react/react-dom`、`echarts/echarts-for-react`、`react-grid-layout/react-resizable` 切成三块，
   **业务主包只剩 26.08 kB（gzip 10.18）**；echarts 1,059.73 kB（gzip 352.45）独立成 vendor 块，
   低频变更 ⇒ 改业务代码不会让用户重下 1MB。
   注：① 未动任何渲染逻辑；② 图表卡是静态 import，首屏仍会拉 echarts 块 —— 分包收益是**缓存/并行**，
   不是首屏体积；要再砍首屏需把图表卡改 `React.lazy`（未做，待批）；
   ③ 构建仍有一条 `>500 kB` 告警，指向 echarts vendor 块 —— **未上调 `chunkSizeWarningLimit`**，
   以免掩盖业务包变大的信号。
4. **下钻 / 图表联动**：`hooks/useDrill.ts` 目前只承担筛选状态；`hierarchies` 下钻与事件总线 `emit/on` 待后端支持 `grain` 参数后再接。
5. **端侧性能优化**：`react-grid-layout` 的 `WidthProvider` 在窄屏首帧会有一次 1280px 宽度抖动，可按需加 `measureBeforeMount`。
6. **错误上报**：`ErrorBoundary.componentDidCatch` 仍是 `console.error`，等监控后端确定后接上报。
7. ~~工作日节假日日历~~：已按裁决改为**只认后端 `dim_calendar`**，`derive.ts` 不再本地推算（见 §0.3）。
8. **日历只 seed 了 2026-09**（team-lead 转达）：跨月 / 历史月查询会因无日历行而退 p2，挡住季度预实与月度同比。这是后端待办，非本线解决；写 mock / fixture 时**不要假设历史月有日历**。

---

## 7. 纪律自检

- ✅ 未改 `common/**`、`docker/**`、`tests/**`、`frontend/bi-ui/**`（bi-ui 三件套为**拷贝进** `src/styles/bi-ui/`，源文件零改动）
- ✅ 渲染器内无 `shortfall / severity / required_daily / 环比` 任何算式（过渡例外 `src/data/derive.ts` 已于 2026-09-16 整文件删除；前端**零**派生实现，`severity.ts` 仅为 code→chip 查表）
- ✅ 组件不直接 `fetch`，一律经 `data/biWebClient.ts`
- ✅ 未改任何后端 Python；契约不符一律前端适配并记入 §5
