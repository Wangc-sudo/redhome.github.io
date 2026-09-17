# 人工报表消费 ④⑤ 落地记录（2026-09-16）

执行依据：`docs/superpowers/plans/2026-09-16-manual-report-consumption-prompt.md` §A
契约来源：`docs/superpowers/specs/2026-09-16-manual-report-consumption.md`（§5 第 1、2、5 条；第 3 条按下文「参数方案」执行；第 4 条 ⑬ 本批不做）

## 1. 测试基线与终态

| 项 | 数字 |
|---|---|
| 基线（改动前实测） | 359 例全绿（23 跳过 = 集成测试，需 Docker MySQL） |
| 终态（改动后） | **375 例全绿**（+16，23 跳过不变） |
| 验收命令 | `python -m unittest discover -s tests -t . -k bi_web` |
| lint | 六个改动文件 0 诊断 |

新增 16 例分布：`test_bi_web_queries.py` +11（SQL 形态 2、事实层 1、pivot 3、run 载荷 5），`test_bi_web_derived.py` +5（MarginTests 4 + golden margin_cases 消费 1），`test_bi_web_cards.py` 守门断言扩展（计数 23→25，集合相等断言原样保留，未新增方法）。

## 2. 参数方案最终分支：**params_schema 留空**（保守分支）

判定过程：

1. **尝试验证**：本地无集成 MySQL 运行（`docker ps` 只有无关容器），无法实查月份集合。
2. **证据侧查**：`fact_manual_report` 在全仓**无任何 seed**（`bi.seed.yaml` 不种子事实表，该表只由 `import-manual` CLI 写入）→ 任何当前环境里人工报表月份集合为空，「重叠」无法得到正面确认。
3. **按 roadmap §3 裁决 #1（只准追加、不准放弱）选边**：
   - 接 month 参数 = 赌「重叠」。赌错就要撤参数（放弱），且人工报表独有月份（餐饮无系统采集源，可能补录业务事实表没有的月份）会被 app 层值域校验判 400；
   - 留空 = 零 400 风险，month 缺省当前月。日后确认重叠后**追加** `{"month": "months"}` 即可，属追加式改动（`EXPECTED_PARAMS_SCHEMA` +2 键、卡片注册加 schema），代码路径已就绪（`run_*` 已读 `params.get("month")`）。
4. **红线遵守**：没有为让月份可选而新增 filter source（未动 `config.KNOWN_FILTER_SOURCES` / `app._FILTER_SOURCE_QUERIES`）。

**待办（留给下一批）**：集成库导入首批人工月报后，实查 `fact_manual_report` 月份是否 ⊆ `month_options`（两业务事实表 DISTINCT 月 ∪ 当前月）；是 → 两张卡追加 month 参数。

## 3. 契约偏差清单（前端适配并登记，不改后端既有语义）

| # | spec/prompt 原文 | 实际落地 | 理由 |
|---|---|---|---|
| 1 | prompt T2：实现 `run_table_manual_monthly(connection, params)`（单函数） | 拆为 `run_table_manual_ecommerce_monthly` / `run_table_manual_restaurant_monthly` 两个薄封装 + 共享核心 `_manual_monthly_payload` | dataset 已裁决**不做 URL 参数**（params_schema 不含 dataset），而 app 层只按 params_schema 传参——单函数拿不到 dataset。卡片注册表按 card_id 绑定各自 run 函数，与既有形态一致 |
| 2 | spec §3.5.4：新增 `_MANUAL_*` SQL 常量同步追加 `StaticSqlTests` 断言 | `manual_report_rows` 带 `%s` 绑定，**不符「全静态」形态**，不进 `_QUERY_FUNCTIONS`；改以同等强度的 `ManualReportSqlShapeTests` 钉带参形态（FROM 表、两 `%s` 占位、参数元组、无 CURDATE/合计/CASE） | prompt T6 原文即带条件「若形态符合全静态 SQL」——不符合，故走带参形态测试 |
| 3 | spec §3.4 载荷示例含 `month`/`dataset` 键 | 完全一致；另补 `unit: "元"`（与既有表卡一致，供前端万换算） | spec §3.1 明令金额一律元 |
| 4 | golden 夹具「既有条目零改动」 | cases/calendar_cases/row_cases 全部条目逐字未动；唯一非纯新增是 `meta.input_contract` 的 `row_cases` 描述行尾补逗号（JSON 语法必需）+ 新增 `margin_cases` 说明与 5 条用例 | 追加式 |

## 4. 铁律自查

| 铁律 | 落点 |
|---|---|
| SQL 只取事实、派生走 derived | `_MANUAL_REPORT_ROWS_SQL` 无除法/无 CASE 展开；毛利率走 `derived.margin`，SQL 形态测试钉死 |
| metric 不写死 | 窄行取回 + `pivot_manual_rows` Python 侧展开；列由当月实际 metric 集合驱动，未知指标键名兜底（有测试） |
| `_UNIT = "元"`、float 只在 run 边界 | pivot 保持 Decimal；`_manual_monthly_payload` 出载荷前转 float（有 isinstance 断言） |
| NULL 不补 0 | 事实层/pivot/载荷三层各有 None 断言；`margin(None, …) → None` |
| 不做 CURDATE 截断、不做合计过滤 | SQL 形态测试 `assertNotIn("CURDATE()")` / `assertNotIn("合计")` |
| 参数 %s 绑定 | `(dataset, period_start)` 元组断言 |

## 5. 未做事项（按 prompt 边界）

- ⑬ 同比卡（§C3，等日历跨年 + 2025 基数）；
- seed 编排（§C1/C2，等 main 裁决——两张新卡进哪个看板页由 `bi.seed.yaml`/Nacos 侧决定，代码注册表不含页面归属）；
- `showroom_monthly`（⑪，模板待克隆，`_MANUAL_MONTHLY_DATASETS` 加一行即可扩展）。
