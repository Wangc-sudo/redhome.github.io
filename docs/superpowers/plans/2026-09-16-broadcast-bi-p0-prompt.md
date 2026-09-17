# 播报 × BI 统一 · P0 派发提示词（2026-09-16）

> 用途：把「渠道日报」播报的达成率口径统一到共享口径，并给这个**生产已跑一个月、目前零单测**的脚本建立第一道离线回归。
>
> - **§0** = 为什么这段值得单独发一批（诊断事实）
> - **§A** = 可复制执行提示词（本批唯一要派发的内容）
> - **§B** = **明确不动**（生产风险 > 收益）
> - **§C** = 本批之后的阻塞与归属
> - **§D** = 事实锚点表（文件 / 行号，防止改错对象）
> - **§E** = 一句话总结

---

## §0 为什么写这份提示词

| 检查项 | 结果 |
|---|---|
| `daily_scheduler.py:44` 任务表映射 | 「渠道日报」→ **`run_today.py`**（11:00，周日不播） |
| `main.py` + `channel_report.py` | 另一条旧链，**不被调度器调用**，但仍存在于仓库——同一播报两套实现 |
| `run_today.py` 单测 | **0 个**（全仓库无 `test_*ChannelDaily*`，`agg_targets` / `build_md` 无任何覆盖） |
| 达成率计算 | `build_md` 自算 `m / t * 100`（`run_today.py:190`、`:208`），**未走** `common.metrics.achievement_rate` |
| 月目标聚合 | `agg_targets`（`run_today.py:164-173`）用 `out[ch] += tgt` —— **跨行 SUM 目标** |
| ROI | `run_today.py:202` 自算 `sales / promo`、未走共享纯函数；且受 `agg_day:142` 的 `or 0.0` 污染——推广费缺失在取数层就被抹成 0.0，缺失与 0 无法区分 |

**核心问题**：《BI建设指南》§4.2 已经把「月目标 = Σ各主体 MAX(monthly_target)，**绝不跨行 SUM**（melt 陷阱）」和「达成率 = Σcompleted ÷ Σtarget」写成铁律，但群里每天播的那张表一条都没走。这不是"未来会漂移"，是**现在就可能在错**——如果目标表里一个渠道有多行（按店铺/负责人），目标被翻倍，达成率系统性偏低，而没人发现。

---

## §A 可复制执行提示词（渠道日报 P0）

> 复制以下整段给执行 agent。

````text
你是 broadcast-agent。任务：把「渠道日报」播报（11:00，钉钉渠道日报表群）的达成率口径
统一到共享口径，并为该脚本建立第一批离线单测。本批是《播报 × BI 统一设计稿》§6 的 P0。

【契约来源，先读】
1. docs/superpowers/specs/2026-09-16-broadcast-bi-unification-design.md
   （§2 三条口径分裂证据、§4 结合对照表、§5 裁决记录、§6 P0）
2. docs/BI建设指南.md §4.2 核心指标口径（月目标 melt 陷阱 / 达成率 / ROI）
3. docs/bi-roadmap-2026-09-15.md §5 P2（渠道 grain 月目标缺口的归属）
4. 本文件（docs/superpowers/plans/2026-09-16-broadcast-bi-p0-prompt.md）§0 与 §D 锚点表

【范围边界】
只改：
  - 数字化/钉钉/渠道日报机器人/run_today.py（内部重构，文案不改）
  - common/metrics/daily_report.py（追加聚合纯函数）+ common/metrics/__init__.py（导出）
  - tests/common/ 新增离线单测；tests/fixtures/ 追加 fixture（新增，不改既有）
不改（详见本文件 §B）：
  - daily_scheduler.py（时刻表 / TASKS / 幂等 state）
  - stock_alert.py / hot_items_monitor.py / purchase_alert.py / order_risk_alert.py
  - config.json / 凭据文件 / 生产库 / frontend/** / docker/**
  - main.py + channel_report.py 这条旧链（本批只做差异登记，不合并）

【先确认的三件事，动手前必须回答】
Q1 生产主体：**确认跑的是 run_today.py**（daily_scheduler.py:44），不是 main.py 那条。
Q2 目标表形态：钉钉 AI 表「渠道销售目标达成率9」里，**一个渠道是否可能有多行**？
    - 若一个渠道恒为一行 → 本次切换对播报数字**无影响**，可放心推进；
    - 若一个渠道存在多行（按店铺 / 负责人） → 切换**会改变播报数字**（目标不再被翻倍，
      达成率会上升）。这种情况：**先停下来向用户报备差异数值，批准后再发**，不要在
      无人知情的情况下让群里数字跳变。
Q3 是否已有 dry-run 条件：`run_today.py --dry` 需要 config.json 凭据；**凭据缺失就用
   fixture 构造 baseline**，不要因为没有凭据就跳过回归。

【铁律（违反即返工）】
1. 达成率只能由 `common.metrics.achievement_rate` 产出（target 缺失 / <=0 → None）。
   **None 显示「--」，绝不替换成 0 或 0.0%**——「无目标 = 达标」是危险语义，已由
   bi-roadmap §3 裁决 #4 定论。
2. 月目标聚合语义：按目标明细主体取 **MAX 再相加**（Σ主体 MAX），**绝不跨行 SUM**。
3. ROI = Σsales ÷ Σpromo（不是行级均值）；promo 缺失或 <=0 → 「--」；
   **缺失（None）与 0 必须区分**，不许 `or 0.0` 抹平。
4. 取数与渲染分离：`build_md` 只接收结构化 dict，**内部不许做业务算术**（`wan()` 格式化
   除外）。所有口径以纯函数形式落地，不得藏在 markdown 拼接里。
5. 新增口径一律配离线单测：**不联网、不碰凭据**（手写 records fixture / FakeConnection）。
6. 生成的 markdown **文本结构逐字保留**：标题格式、表头七列、脚注「数据来源：」句式。
   只有"数字本身"允许因口径修正而变化，排版与措辞不许顺手改。

【分步任务】
T0 · baseline（没有 baseline 的回归等于没回归）
  - 有凭据：对同一业务日分别记录改造前 `--dry` 的完整 markdown，存为 fixture。
  - 无凭据：用手写 fixture 走一遍现有 `build_md`，记录输出作为 baseline。
  - 同时跑一次 `python -m unittest discover -s tests -t .` 记录**测试基线数字**。

T1 · 口径下沉（common/metrics/daily_report.py）
  - 新增纯函数 `sum_targets_by_subject(rows, *, group_key, subject_key, target_key) -> dict`：
    组内按主体取 MAX 后相加（缺失 None/非数 → 跳过；结果为空组 → 键不存在，不补 0）。
  - 已有 `achievement_rate`（daily_report.py:48）直接复用，**不许重写一份**。
  - `common/metrics/__init__.py` 的 import 与 `__all__` 同步导出新函数。
  - 风格对齐现有纯函数写法（无 IO、可单测、docstring 写明边界语义）。

T2 · run_today.py 改造
  - `agg_targets`（run_today.py:164）改为调用 T1 的 `sum_targets_by_subject`，
    保留原返回形态 `{渠道: 目标}`，让下游零感知。
  - `build_md`（run_today.py:182）：达成率全部走 `achievement_rate`；
    ROI 抽成模块级纯函数 `roi(sales, promo)` 走「Σ÷Σ」语义 + None 区分。
  - **例外许可（铁律 3 的必要前置，不属于"顺手改"）**：`agg_day`（run_today.py:142）
    的 `promo += to_num(...) or 0.0` 把推广费缺失在取数层抹成 0.0，导致 `roi` 永远无法
    区分缺失与 0。允许将 promo 聚合改为 None-aware（有任一非缺失值则求和、全缺失则
    None）。**注意这会让"全缺失"渠道的推广费/ROI 从 0 变为 --**，是 Q2 之外第二个
    可能改变播报数字的来源——验收与汇报口径必须覆盖它。
  - **配套授权（头部合计）**：`agg_day` 改 None-aware 后，`build_md` 的
    `total_promo = sum(v["promo"] ...)`（run_today.py:184）遇 None 会直接 TypeError。
    头部推广费合计同步按 None-aware 求和（跳过 None；全渠道全缺失 → None，经 `wan()`
    显示「--」）。行级推广费走 `wan(promo)` 对 None 已显示「--」，无需特判。
  - **本月累计 / 环比 / 销售额聚合逻辑保持不变**（不是本批议题）——即使看着别扭也不要顺手改。

T3 · 可测性（不改变 CLI 行为）
  - 确保 `import run_today` 无副作用、不联网（`main()` 只在 `__main__` 下执行）。
  - 注意脚本顶部的 `sys.path` 注入（run_today.py:25-28）在 import 上下文中要能正常工作；
    若测试环境不便于导入脚本模块，可按仓库既有做法把纯函数再往 `common/` 提一层，
    **但务必在汇报里说明这一步的取舍**。

T4 · 离线单测（tests/common/test_channel_daily_broadcast.py，新建）
  - `sum_targets_by_subject`：单主体单行 / 同主体多行（验证取 MAX 不翻倍，这是本案核心）/ 
    跨主体相加 / 目标为 0 / 目标为 None / 空行列表。
  - `roi`：正常 / promo=0 / promo=None / sales=0（四种必须分别断言，不许合并成一个用例）。
  - `build_md` 端到端：喂 fixture → 与 T0 的 baseline 比对；
    并单独钉死一条：**target 缺失那一列的达成率必须是「--」，不是 0.0%**。
  - 用例不联网、不读 config.json、不依赖当前日期（日期从入参传入）。

T5 · 双实现差异登记 + 已知缺陷登记（只读调查，不改代码）
  - 对比 `main.py + channel_report.py` 与 `run_today.py`：字段识别方式、MTD 聚合算法、
    推广费与 ROI 的处理、推送目标解析、错误处理。
  - **登记头部合计达成率的分母缺陷**（run_today.py:185-190）：`target_total` 只含
    有目标的渠道，`mtd_total` 却含全部渠道销售额——一旦有渠道没填目标，头部达成率
    系统性偏高。本批**只登记不修**（修了数字就变），纯重构会原样保留该语义。
  - 结论写进 plan，**不合并、不删除**——合并涉及业务确认，归后续批次。

【验收 · 完成定义】
1. 新增单测全绿：`python -m unittest discover -s tests -t . -v`（数字以 T0 基线为准做对比）。
2. 既有测试**零回归**：bi_web / daily_robot / calendar 相关用例数字与报错状态同基线。
3. 达成率 None → 「--」有独立断言钉死；target 缺失不产生 0.0%。
4. 若 Q2 结论为「一渠道一行」且无全缺失推广费的渠道：改造前后 markdown **逐字一致**
   （证明纯重构无副作用）。若为「多行」或存在推广费全缺失渠道：改造前后差异**逐项列出**
   并说明每处差异的原因（目标不再翻倍 / 缺失不再显示 0），两条都不能含糊。
5. `daily_scheduler.py` 无 diff（用 diff 自证）。

【汇报要求】
新建 docs/superpowers/plans/2026-09-16-broadcast-bi-p0-plan.md，至少记录：
  - T0 的测试基线数字与最终数字；
  - Q2 的实际结论（目标表行数与分组方式，附观察证据）+ 是否影响播报数字；
  - `agg_day` promo None-aware 改造是否触发播报数字变化（哪些渠道、从什么变成什么）；
  - 头部合计达成率分母缺陷的登记结论与建议处理批次；
  - T3 是否动了模块归属及理由；
  - T5 的双实现差异清单；
  - 剩余硬编码项清单（`MONTH` / `BASE_ID` / `CHANNELS` 各自的风险与建议处理批次）。
若发现 spec 与本文件描述和代码事实不符：**以代码为准，在 plan 里登记偏差**，不要为了让文档
好看去改背后的语义。
````

---

## §B 明确不动（本批）

| 对象 | 为什么不动 |
|---|---|
| `daily_scheduler.py` 五个时刻 + TASKS + `.schedule_state.json` 幂等 | 生产在跑；动它的风险远大于收益。时刻表是业务确认过的（2026-09-03） |
| `stock_alert.py` 去重状态机 + 去重键 | 同上；且它的口径裁决刚落地，改动要跟 P1 的 WDT 入 DB 一起做，避免改两遍 |
| `hot_items_monitor.py` / `purchase_alert.py` / `order_risk_alert.py` | 等 P1 / P2，本批不碰 |
| `channel_report.py` 达成的率 | 旧链，未被调用；本批只登记差异，合并需业务确认 |
| `MONTH`（本批不要求改，`BASE_ID` / `CHANNELS` 明确不动） | `MONTH` 是跨月手改的雷，但改成自动推导需要 fixture 覆盖多个月份，**让 agent 在 T3 后顺手评估**；`BASE_ID` / `CHANNELS` 属业务配置，改了要确认 |
| seed 编排 / Nacos | 归 §C，编排迁移放到阶段 C（裁决 #3 暂缓） |

---

## §C 本批之后的阻塞与归属

| # | 事项 | 阻塞 | 归属批次 |
|---|---|---|---|
| C1 | 渠道月目标进 mart（`fact_channel_daily_sales` 有 557 行日销，但 `dim_target` 只有人员月粒度） | 需目标粒度/口径确认 | P1 后单独立项 |
| C2 | `stock_alert` / `hot_items_monitor` 取数层切 mart | WDT stock 数据集入 DB（roadmap B2） | P1，口径已裁决（spec §5.1），无待决策项 |
| C3 | 播报是否改 Nacos 编排 + `broadcast` 字段 | 与免登/权限同批 | P2 / 阶段 C |
| C4 | `main.py` + `channel_report.py` 旧链去留 | 需业务确认哪份为准 | 随 C1（渠道月目标进 mart）立项时一并裁决，**不无限挂起** |

---

## §D 事实锚点表

| 对象 | 位置 |
|---|---|
| 生产播报主体（CPU 的真脚本） | `数字化/钉钉/渠道日报机器人/run_today.py` |
| 调度映射「渠道日报 → run_today.py」 | `数字化/钉钉/渠道日报机器人/daily_scheduler.py:44` |
| 五个播报时刻表 | `daily_scheduler.py:41-47` |
| **跨行 SUM 目标（melt 陷阱嫌疑）** | `run_today.py:164-173` `agg_targets` |
| 自算达成率（未走共享口径） | `run_today.py:190`（合计行）、`:208`（分渠道行） |
| 自算 ROI | `run_today.py:202` |
| 推广费缺失被抹成 0（铁律 3 的取数层障碍） | `run_today.py:142` `agg_day` 的 `or 0.0` |
| 头部达成率分母缺陷（只登记不修） | `run_today.py:185-190` |
| 硬编码 `BASE_ID` / `CHANNELS` / `MONTH` | `run_today.py:36-38` |
| 渲染入口 `build_md` | `run_today.py:182-214` |
| **共享达成率（唯一真源，target<=0→None）** | `common/metrics/daily_report.py:48` |
| 共享口径导出清单 | `common/metrics/__init__.py:3-29` |
| BI 侧相同口径的事实表 | `fact_channel_daily_sales`（557 行，`common/bi_web/queries.py`） |
| 落差：渠道月目标在 mart 无对应 | `docs/bi-roadmap-2026-09-15.md` §5 P2 |
| 派生纯函数风格范本 | `common/bi_web/derived.py:94 / 121 / 132` |
| golden 夹具写法范本 | `tests/fixtures/derived_golden.json` |
| 旧执行链（未被调度器调用） | `main.py:21` `import channel_report as cr` |

---

## §E 一句话总结

**本批只干一件事**：让群里每天播的「渠道达成率」和看板算的是同一个数——把目标的聚合语义从「跨行 SUM」改成「Σ主体 MAX」，放弃各脚本自算、改调 `achievement_rate`，并给这个零覆盖的生产脚本装上第一道离线回归。

**不干**：时刻表、库存/热卖品/采购/风控四个播报、Nacos 编排、旧链合并——全部留给后续批次。
