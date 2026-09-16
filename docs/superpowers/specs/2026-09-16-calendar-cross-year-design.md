# dim_calendar 跨年扩年立项设计（2025 全年 + 2026 全年）

日期：2026-09-16 · 作者：data-agent（D 线）· 状态：设计草案，**本期不改生产 seed**（`docker/integration/calendar.seed.json` 保持原样）
阻塞解除对象：需求 ⑥（季度经营预实）、⑬（月度销售同比）——roadmap P2「跨月日历」公共前置项。

---

## 1. 现状与问题

- `calendar.seed.json` 仅含 2026-09 一个月（version 1，规则制：基准周日休 + 大休周六 + 节假日 − 调休）。
- `dim_calendar` 由 `extract_mart._extract_calendar` 物化：遍历 seed 的 months，`calendar_utils.generate_rest_days` 推导休息日，`replace_dim_calendar` **整体替换**（DELETE 全表 + INSERT）。
- 消费面：`fetch_workdays(year, month)` 按月区间查 `is_workday=1`；缺口径卡（shortfall）用月工作日三件套；历史月查询拿不到工作日 → `required_daily`/severity 退化。
- 季度聚合需要 2026 全年逐月；同比⑬ 需要 2025 全年基期月。当前 seed 两者都不具备。

## 2. 目标范围

seed months 扩至 **2025-01 ~ 2026-12 共 24 个月**。结构不变（version 仍为 1：months[] 每项 `{year, month, bigRestSaturdays, holidays, makeupWorkdays, _说明}`）。

### 2.1 数据来源（节假日/调休）

| 数据 | 来源 | 说明 |
|---|---|---|
| 法定节假日 + 调休上班日 | **国务院办公厅《关于部分节假日安排的通知》**（每年底发布次年安排） | 2025 年安排：国办发明电〔2024〕12 号（2024-11 发布）；2026 年安排：2025-11 发布。转录进 seed 时逐日核对官方原文，不靠二手日历网站 |
| 大小休（大休周六清单） | **HR/行政的公司排班规则**（spec §10 有业务背书：钉钉排班不适用本业务组） | 2025 历史月各月大休周六为**业务输入**，需 HR 确认后转录；<待填：HR 确认 2025-01~2025-12 各月 bigRestSaturdays> |
| 已锁定的锚点 | 仓内既有事实 | 2026-09：`bigRestSaturdays=[19], holidays=[25,26,27], makeupWorkdays=[20]`（calendar.seed.json + golden 夹具 24/13/11 钉死）；春节错位：2025 春节 1/29 vs 2026 春节 2/17（需求对照 §1 ⑬） |

### 2.2 历史月 is_workday 如何定（回补方案）

2025 是**历史年**，is_workday 只用于同比基期月的工作日计数与页面提示，不回算任何已发生业务。规则与现行完全一致：

1. 基准：每周日休（小休周六上班）；
2. 大休周六：HR 确认的当月 bigRestSaturdays 补休；
3. 法定节假日：按国务院 2025 年通知转录 `holidays`；
4. 调休上班：按通知转录 `makeupWorkdays`，从休息集合剔除。

全部经 `generate_rest_days(year, month, ...)` 推导，**绝不手工列 restDays**（seed `_说明` 既定原则：种子只记意图，渲染不可能与规则漂移）。2026 年 1-8 月同理回补（已过月），10-12 月按通知预排。

### 2.3 转录质量护栏

- 每月条目 `_说明` 字段做自证（照 2026-09 先例：「基准周日休 + 大休周六X + 法定假Y − 调休Z ⇒ [推导结果]」）；
- 新增 seed 级单测：24 个月 × `generate_rest_days` 结果与条目 `_说明` 自证一致；2026-09 逐位等于现值（防回归）；
- 转录人对照国务院通知原文双人复核；HR 对 2025 大休周六清单书面确认。

## 3. seed 版本化策略

- **结构版本不变**：version 保持 1（months 元素结构无变化，`load_calendar_seed` 无需改代码）。扩年是**内容演进**不是 schema 演进。
- 变更载体 = `calendar.seed.json` 的一次 git 提交（24 个月条目）；diff 即审计轨迹。
- `source` 保持 `local`；每月 `_说明` 标注依据（「国务院 2025 年节假日通知 + HR 大小休确认 2026-XX-XX」）。
- 既有「跨年未更新应告警」约定延续：每年 11-12 月国务院发布次年安排后，HR 更新 seed；seed 缺下一自然年月份 → 提取层/巡检告警（<待填：告警接在哪条巡检链路上，建议随 extract-mart 摘要输出缺月 warning>）。

## 4. 提取层兼容评估（`_extract_calendar` 整体替换语义）

| 检查项 | 结论 |
|---|---|
| 整体替换 vs 扩年 | ✅ **天然兼容**。`replace_dim_calendar` 是全表 DELETE+INSERT，seed 加月 = 下次 `extract-mart` 后表中多出 2025/2026 其余月份；无迁移、无残留行风险（被移除的月份必须消失正是该语义的设计意图） |
| `_extract_calendar` 代码 | ✅ 零改动：遍历 seed months 推导，24 个月与 1 个月同路径 |
| `load_calendar_seed` 校验 | ✅ 零改动：year 无范围限制，重复 (year,month) 有显式报错 |
| run 摘要体量 | ✅ record_ids 从 30 → 731 条 isoformat 字符串，`_save_summary` 无截断风险（摘要表 TEXT 列，<待填：确认 sync_run_datasets 摘要列长度上限>） |
| 消费方 | ✅ `fetch_workdays` 按月区间查询，扩年后历史月自然返回工作日集合；shortfall 历史月 `remaining_workdays=None` 逻辑不受影响 |
| golden 护栏 | ✅ `calendar_real_dim_calendar_2026_09`（24/13/11）要求 2026-09 条目不动力；扩年提交必须先跑通该夹具 |

**结论：扩年 = 只改 seed 内容 + 补 seed 单测，生产代码零改动。**

## 5. 落地步骤（下期执行，本期不实施）

1. HR 确认 2025 全年 + 2026 全年各月 bigRestSaturdays；数据组按国务院通知转录 holidays/makeupWorkdays；
2. 更新 `calendar.seed.json`（24 个月，含逐月 `_说明` 自证），version 仍为 1；
3. 新增 seed 自证单测 + 跑通 `calendar_real_dim_calendar_2026_09` 护栏与全量 unittest；
4. 测试环境 `extract-mart` 验证 `dim_calendar` 731 行（365+366），抽查 2025-01/2025-10（国庆）/2026-02（春节）；
5. ⑬ 同比卡启用仍需 2025 基数经 `generic` 模板首填（manual-report-consumption.md §3.4）。

## 6. 风险

| 风险 | 处置 |
|---|---|
| 2025 大休周六 HR 记忆不准 | 与 2025 已发薪考勤/排班表交叉验证；不确定月份标 `_说明` 存疑并由 HR 签认 |
| 国务院通知转录错日 | 双人复核 + `_说明` 自证单测；春节/国庆重点抽查 |
| 扩年后 2026-09 被无意改动 | golden 夹具钉死，提交即炸 |
