# BI 推进 · 任务派发提示词（2026-09-16）

> 用途：今天早上三份设计全部停在草案、零代码落地（诊断见本文 §0）。
> 本文把**能立刻开工的**与**等外部/等裁决的**分开，可直接复制派发。
>
> - **§A** = 可复制执行提示词（人工报表消费 ④⑤ 页落地，不依赖任何外部确认）
> - **§B** = 等外部解锁（HR / 财务），附催办提问模板
> - **§C** = 等 main 裁决（seed 编排、资金安全页）
> - **§D** = 事实锚点表（符号 / 路径 / 行号，防止执行时找错对象）

---

## §0 为什么写这份文档

| 检查项 | 结果 |
|---|---|
| `common/bi_web/**` 搜 `manual_report` / `fact_fin_` | **0 命中**，三份设计一行代码没落地 |
| `docker/integration/bi.seed.yaml` | 停在 09-15 13:27 |
| `docker/integration/calendar.seed.json` | 停在 09-11（仍单月 2026-09） |
| `common/bi_web/cards.py` | 停在 09-15 13:27，新卡 0 注册 |
| `docs/superpowers/plans/` | **09-16 无 plan**，最新停在 09-14 |

三份 spec 都写完了（契约、缺口评估、护栏齐全），但 spec → plan → 落地这条链断在中间：
**设计完 ≠ 实现完**，spec 里的代码块是**签名 + docstring**，没有函数体。

---

## §A 可复制执行提示词（人工报表消费 ④⑤ 页）

> 复制以下整段给执行 agent。范围只到「代码 + 测试」，**不动 seed 编排**（见 §C）。

````text
你是 bi-backend-agent。任务：把《fact_manual_report 消费接口草案》落地为可运行的
BI 卡片，覆盖需求 ④（电商月度）与 ⑤（餐饮月度）。

【契约来源，先读】
docs/superpowers/specs/2026-09-16-manual-report-consumption.md
（§2 数据契约、§3.2-3.4 接口签名、§4 投影侧结论、§5 转派事项）
本任务即该文件 §5 的第 1、2、5 条；第 3 条按下方「参数方案」执行；第 4 条（⑬ 同比）
本批不做。

【范围边界】
- 只改：common/bi_web/{queries.py,derived.py,cards.py} + tests/common/test_bi_web_{queries,cards,derived}.py
  + tests/fixtures/derived_golden.json（追加）
- 不改：app.py、config.py、docker/**、common/public_data/**、frontend/**
- 不碰生产库/生产数据；所有 SQL 只读 mart

【铁律（违反即返工）】
1. SQL 只取事实，派生一律走 derived 纯函数 —— 同 shortfall_facts 的既定形态，
   SQL 里不出现除法、不出现 CASE WHEN metric= 的列展开。
2. metric 名绝不写死：模板加一列指标，卡片 SQL 不该跟着改（§2.3 明令）。
   窄行取回后由 Python 侧按 dimension_value × metric 展开。
3. 金额单位一律 _UNIT = "元"；Decimal → float 只在 run_* 边界做，低层保持 Decimal。
4. value 为 NULL = 选填未填，**绝不替换成 0**，前端显示「—」。
5. 不做 CURDATE 截断（人工报表无未来预填行）；不做「合计」过滤（总计行不入库）。
6. 参数一律 %s 绑定。

【参数方案：dataset 不做 URL 参数，固化成独立卡片 id】
理由：新增 filter source 要同时改 config.KNOWN_FILTER_SOURCES 与
app._FILTER_SOURCE_QUERIES，而 app 层本批冻结（queries.py run_kpi_shortfall 注释明示
「app 层本批冻结，留给下一批增量」）。因此：
- 出两张卡：table_manual_ecommerce_monthly（dataset=ecommerce_monthly，dim_scope=channel）
            table_manual_restaurant_monthly（dataset=restaurant_monthly，dim_scope=store）
- month 参数复用既有 "months" source（不新增 source）。
- ⚠️ 风险点，必须先验证：month_options 的选项来自业务事实表 DISTINCT 月，
  **不含人工报表独有的月份**。若 fact_manual_report 的月份不在该集合内，
  用户选该月会被 app 层值域校验判 400。
  处置：先查测试/集成库确认月份是否重叠；重叠 → 接 month 参数；
  不重叠 → 该卡 params_schema 留空（默认当前月），并在 PROGRESS 记录原因。
  **不要为了让月份可选而新增 filter source。**

【分步任务】
T1 queries.py
  - 加 SQL 常量 _MANUAL_REPORT_ROWS_SQL（WHERE dataset = %s AND period_start = %s）
  - 实现 manual_report_rows(connection, *, dataset, period_start) -> list
  - 实现 pivot_manual_rows(rows, *, dim_scope) -> list（缺失指标 = 键不存在，不是 0）
  - 期间推导复用 month_bounds（queries.py:283），不在卡片层重写
T2 queries.py
  - 实现 run_table_manual_monthly(connection, params) -> dict
    载荷形状照 run_table_channel_mtd（queries.py:1192）的返回结构
    {"chart":"table","unit":_UNIT,"month":...,"dataset":...,
     "columns":[...],"rows":[...]}
T3 derived.py
  - 增 margin(part, whole) 纯函数：分母 0/None/缺失 → None（不静默补 0）
  - 风格对齐现有 shortfall / rate / mom（derived.py:94/121/132）
T4 tests/fixtures/derived_golden.json（追加式，不改既有条目）
  - margin 用例：正常 / 分母 0 / None / 负值，逐位期望值手写 + 标注 source
T5 cards.py
  - _CARDS 注册两张新卡，chart="table"，params_schema 按 T「参数方案」结论
T6 守门断言同步（roadmap §3 裁决 #1：**只准追加，不准放弱**）
  tests/common/test_bi_web_cards.py：
    STAGE_B_CARD_IDS（+2）、EXPECTED_CHARTS（+2）、EXPECTED_RUN_FUNCTIONS（+2）、
    EXPECTED_PARAMS_SCHEMA（+2），并把 `self.assertEqual(23, len(REGISTRY))` 改为 25。
    ⚠️ `set(STAGE_B_CARD_IDS) == set(REGISTRY)` 原样保留，不许弱化。
  tests/common/test_bi_web_queries.py：
    StaticSqlTests._QUERY_FUNCTIONS 追加新查询函数（若形态符合「全静态 SQL」）。
  全部测试用 FakeConnection / 内存假数据，不连 MySQL。

【验收 · 完成定义】
1. python -m unittest discover -s tests -t . -k bi_web -v  → 全绿
   （基线 327 例 + 新增；数字以改动前实测为准，先跑一次记录基线）
2. 守门断言 set(STAGE_B_CARD_IDS)==set(REGISTRY) 存在且通过，len(REGISTRY) 23 → 25
3. 新增卡片 SQL 一律只读 mart；StaticSqlTests 覆盖新查询函数
4. derived_golden.json 既有条目零改动（diff 只有新增）

【汇报要求】
- 在 frontend/bi-react/PROGRESS.md 之外，另建
  docs/superpowers/plans/2026-09-16-manual-report-consumption-plan.md 记录：
  基线测试数、最终测试数、T「参数方案」最终选了哪条分支及依据、遇到的契约偏差。
- 若发现 spec 与代码事实不符：**前端适配并登记，不改后端既有语义**，
  参照 bi-react/PROGRESS.md §5 的做法列差异清单。
````

---

## §B 等外部解锁（今天催，但代码侧不空等）

### B1 · HR/行政：2025 全年大休周六清单
- 卡住对象：`dim_calendar` 跨年扩年 → 需求 ⑥（季度预实）、⑬（月度同比）的公共前置
- 出处：`specs/2026-09-16-calendar-cross-year-design.md` §2.1 标 `<待填>`
- 催办话术（可直接发）：

```text
需要 HR/行政确认 2025-01 ~ 2025-12 每个月的「大休周六」具体日期
（公司大小休规则：周日休 + 大休周六休，小休周六上班）。

用途：BI 看板算月度目标缺口与「所需日均」，需要每月工作日数。
2026-09 我们已锁定为 24 个工作日（大休周六 19 号、中秋 25-27 休、20 号调休上班），
现在要把同样口径回补到 2025 全年，用于月度同比。

如果某个月记不准确，请直接标注「存疑」，我们会在数据里留痕并请签认，
不要凭印象给一个确定值 —— 错的日历会让缺口金额系统性偏差。

建议对照物：2025 年已发薪的考勤/排班表。
```

### B2 · 财务：应收/预付超期标准
- 卡住对象：资金安全页（需求 ⑩）**唯一阻塞项**
- 出处：`specs/2026-09-16-fund-safety-draft.md` §5
- 当前按行业默认 `>60 天` 先行，确认后只改阈值常量，不动 SQL 与派生函数
- 催办话术（可直接发）：

```text
需要财务确认两个口径（当前 BI 暂按行业默认 >60 天实现，确认后改常量即可）：
1. 应收账款「超期」的判定天数是多少天？（默认 60 天）
   另外：账龄表 raw 侧只有 0-30 天、31-60 天两桶，>60 天这桶我们打算用
   「期末余额 − 0-30 − 31-60」差额推导，这个口径是否认可？
2. 预付账款/未到票的挂账超期天数？（默认同为 60 天）

另需（不阻塞，可后补）：账账相符、合作状态的「正常」枚举值清单。
```

> 注：B1/B2 未回前，**§A 照常执行** —— ④⑤ 页不依赖这两项。

---

## §C 等 main 裁决（我未擅自处理）

| # | 事项 | 出处 | 为何要你拍板 |
|---|---|---|---|
| C1 | `l2-fund-safety` seed 编排写入 `docker/integration/bi.seed.yaml` | fund-safety §4「待 main 裁决后追加」 | 动集成编排 + 需重建镜像才生效，会短暂中断后端联调 |
| C2 | 人工报表两张新卡编排进哪个看板页（新建页 vs 并入现有 l2-*） | consumption spec 未定义 | 产品归属 |
| C3 | ⑬ 同比卡启用时点 | 依赖 2025 基数首填 + 日历跨年 | 双重前置 |

---

## §D 事实锚点表（执行时对照，防止找错对象）

| 对象 | 位置 |
|---|---|
| `_UNIT = "元"` | `common/bi_web/queries.py:40` |
| `month_bounds()` | `common/bi_web/queries.py:283` |
| 表格卡返回结构范本 `run_table_channel_mtd` | `common/bi_web/queries.py:1192` |
| 「只读事实」函数范本 `shortfall_facts` | `common/bi_web/queries.py:1356` |
| 派生纯函数 `shortfall` / `rate` / `mom` | `common/bi_web/derived.py:94 / 121 / 132` |
| 派生对拍夹具 | `tests/fixtures/derived_golden.json` |
| `_CARDS` 注册表 | `common/bi_web/cards.py:70` |
| 注册时校验（chart / filter source） | `common/bi_web/cards.py:62-67` |
| `KNOWN_FILTER_SOURCES` | `common/bi_web/config.py:58`（regions/channels/months/brands/sku_channels） |
| `_FILTER_SOURCE_QUERIES` | `common/bi_web/app.py:186` |
| 守门：卡片 id 集合 + 计数 | `tests/common/test_bi_web_cards.py:178-180`（`23 == len(REGISTRY)`） |
| 守门：chart / run / params_schema | `tests/common/test_bi_web_cards.py:72 / 99 / 126` |
| `StaticSqlTests._QUERY_FUNCTIONS` | `tests/common/test_bi_web_queries.py:269-281` |
| 人工报表模板（dataset 名来源） | `docker/integration/manual-import-templates/ecommerce_monthly.yaml`、`restaurant_monthly.yaml` |
| 模板占位前缀（`<待填>` 模板会拒绝导入） | `common/public_data/manual_import/template.py:41` |

---

## §E 一句话总结

**能立刻动**：§A（`manual-report` ④⑤ 落地，零外部依赖）。
**今天该催**：§B（HR 排班 / 财务超期标准）。
**等你拍板**：§C（seed 编排两处 + 同比卡时点）。
