# BI 看板：现状评估 · 本轮交付 · 路线图

日期：2026-09-15 · 范围：`digital-ops`（后端 + `frontend/bi-react` + `frontend/bi-ui`）

---

## 0. 一句话结论

三条主线并行落地：**人工报表有了进库通道**、**派生口径回到后端**、**React 应用从"编译不过"变成"跑得起来且与后端口径一致"**。
最大收获不是代码量，而是清掉了两个隐性事故：**大小休日历差点被"修正"成标准双休**，**挂零差点被 has_fact 粉饰成无数据**。

---

## 1. 现状评估（本轮开始前）

| 项 | 状态 |
|---|---|
| 后端三层管线（raw → mart_ops → bi-web 只读 mart） | 已有，健康 |
| 看板能力 | 5 页 / 23 张卡，`/api/v1/**` 只读契约稳定 |
| 人工报表（毛利、费用、预算、2025 基数） | **无进库通道** —— 五个月报页的共同瓶颈 |
| 派生指标（缺口、告警分级、日均要求） | **后端全无**，全仓 `shortfall` 0 命中，只有 rate / dod |
| React 应用 `bi-react` | 19 文件脚手架，**未 install、未构建、未运行**，且 `tsc -b` 报 TS6310 连基线都编译不过 |
| 设计系统 `bi-ui` | 已存在，但此前只活在会话临时目录（无 git），**且被 React 侧复制成了第二套 token** |

---

## 2. 本轮交付

### 主线 A · 数据供应链（人工报表导入通道）
- `common/public_data/manual_import/`：`template / loader / validate / repository / projector / service / schema`
- 模板：`ecommerce_monthly`、`restaurant_monthly`、`generic`（支持 month/quarter/year，含"待填占位"守卫）
- CLI：`import-manual --template --file --period [--apply]`，**默认 dry-run**
- 落库：raw 存通用窄行（不按模板动态建表，避免迁移漂移）→ 投影 `mart_ops.fact_manual_report` 窄表
- 幂等：同文件 sha256 去重；同 dataset+period 换文件先删后插
- 校验：类型/必填/枚举/重复/合计对账/极端值，全部进结构化报告（行号+字段+code），有 error 整批拒绝
- 测试 67 例，全程内存 fake，不连 MySQL

### 主线 B · 口径与契约（派生指标后端化）
- `common/bi_web/derived.py`：`shortfall / required_daily / rate / mom / severity(p0|p1|p2|ok)` + 日历三件套
- SQL：`run_kpi_shortfall`、`run_anomaly_top`（SQL 只取事实，派生一律调 derived）
- 卡片注册 + `l1-cockpit` 编排 9 → 11 张（原 9 张顺序与数值零回归）
- `tests/fixtures/derived_golden.json`：**24 条派生 + 4 条日历 + 3 条行级用例**，每条带 `source` 指向 CubeSchema 条款，期望值手写

### 主线 C · 产品与前端（跑通 + 契约适配）
- 修 9 项开工缺陷：`w.id`/Hook 违规/看板级共享缓存 + AbortController/hash 路由/代理鉴权/缺依赖…
- 新增：`biWebClient`、按 chart 分派的 `cardToCube` adapter、`derive.ts`（临时）、mock fixture、hash 路由、bi-ui 落库
- **删掉**前端 severity 分支与三个本地工作日函数（后端已产，多一份真相不如没有）
- 验证：`typecheck 0 error`、`build OK`、`vitest 73 passed / 4 skipped`；dev 代理与直连 18080 字节一致

---

## 3. 关键裁决（含两次改判）

| # | 争议 | 裁断 | 依据 |
|---|---|---|---|
| 1 | 新增卡撞钉死测试断言 | **批准追加式更新**，不回退 | 守门断言 `set(IDS)==set(REGISTRY)` 原样保留；追加可以，放弱不行 |
| 2 | 工作日 24（后端）vs 22（前端） | **24 正确，不改日历** | 业务为大小休（周六默认上班）+ 中秋 + 调休；spec §10 有业务背书。改成 22 会让 `required_daily` 系统性偏高 9% |
| 3 | `has_fact=false` 是否降级不报警 | **撤销降级**（改判） | 后端"有目标无销单"=`has_fact=false`，恰是最该报警的挂零；真机 42 人全 true、无实例。字段保留作诊断，不参与 severity |
| 4 | 无目标时 severity 返回 ok 还是 null | **null** | "无目标=达标"是危险语义，宁可现在动类型 |
| 5 | 前端是否本地推算工作日 | **禁止**，缺失显示"—" | 前端无从得知大小休与年度调休；已加断言钉死"不得再导出本地日历函数" |

> 改判说明：#3 我最初支持引入 `has_fact` 降级，被"左表 target 驱动 ⇒ done=0 即真挂零"的论证驳倒后撤回；但落地版本又把同一批人降级，属自相矛盾，再次改判。两次都由数据而非职位决定。
>
> **作废声明**：`frontend/bi-react/PROGRESS.md` 中"后端 has_fact 语义与 golden `row_no_fact_with_target_is_also_p0` 相矛盾、待裁决"的记载**已作废**。矛盾源于改判前的陈旧消息；最终行为以 golden 为准：`has_fact=false` 仍按 done=0 判 p0。前端当时"两侧都不动、只把行为钉成测试"的处理与最终裁决方向一致（偏严），无需返工。

---

## 4. 跨线一致性

- **golden 对拍**：24 条派生用例在 `shortfall / rate / required_daily` 上**逐位一致**，冲突台账清空。
- **护栏用例**：`has_fact=true/false` 两种情况下 `done=0 & elapsed≥2` 期望值**必须都是 p0**——谁改成"—"，两条一起炸。
- **日历护栏**：`calendar_real_dim_calendar_2026_09` 把真机 24/13/11 锁进夹具，任何"修正成 22"都会被拦。

---

## 5. 路线图

### P0 · 收尾
- [x] echarts 分包：**主包 1302KB → 26KB**（echarts 1036KB / react 131KB / grid 79KB 各自成块，只切依赖未动渲染逻辑）
- [ ] 前端接 `has_fact` 诊断角标（仅渲染，不参与判定）

### P1 · 解锁五个月报页（需求 ④⑤⑥⑪⑬）
- [ ] 业务提供表头 → 照 `generic.yaml` 填列映射即可，**零代码**
- [ ] 补 `quarter`/`year` 期间的实际模板
- [ ] BI 侧卡片 SQL 消费 `fact_manual_report`

### P2 · 口径与数据缺口
- [ ] **跨月日历**（当前 seed 仅 2026-09）：阻塞季度预实与月度同比 —— 需单独立项
- [ ] 渠道 grain 月目标（现无 `monthly_target` 列，等 `dim_target` 落渠道粒度）
- [ ] 第 5 类数据缺陷（离职/未启用）**暂缓定性**：需独立人员状态信号，不得用 JOIN 空值倒推
- [ ] 指标 owner 落位（现为 `@TODO-OWNER` 占位）

### P3 · 产品
- [ ] `derive.ts` 整文件删除（后端派生口径已就绪 ✅，等待前端确认可关闭）
- [ ] 下钻/联动（待后端支持 grain 参数）
- [ ] `ConclusionBar` / `AnomalyList` 接线（组件已实现，待后端补 `conclusion`/`anomaly` 卡）

---

## 6. 风险登记

| 风险 | 影响 | 处置 |
|---|---|---|
| 日历仅覆盖 2026-09 | 季度/同比模块上线即失效 | 单独立项，已知且已文档化 |
| `bi-react` 此前无任何 CI 门禁 | 回归无人拦截 | ✅ 已补 typecheck/build/vitest 三道 |
| 人工报表依赖业务给表头 | 阻塞五个需求页 | 通道已就绪，等输入即可 |
| 派生口径双端实现（临时） | 漂移风险 | ✅ golden 夹具对拍一致，可安全删除前端临时实现 |
