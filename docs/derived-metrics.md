# 派生指标清单（CubeSchema §2）

口径真相源：`frontend/bi-ui/CubeSchema.md`（§2 派生口径 / §2.3 四级告警 / §5 数据缺陷显式化）。
**铁律：派生指标一律后端算，前端只渲染。**

> ✅ **状态（2026-09-15）**：`common/bi_web/derived.py` 已交付，与前端 golden 夹具对拍一致。
> 前端 `frontend/bi-react/src/data/derive.ts` 为临时过渡实现，可通过 `VITE_DERIVE=0` 关闭。

## 1. 派生清单

| 指标 | 公式（CubeSchema 条款） | 不可算时 | Owner |
|---|---|---|---|
| `shortfall` | `目标总额 − 已完成`（绝对口径，**不乘**时间进度）§2.1 | 无目标 → `None` | `@TODO-OWNER`（BI 后端） |
| `rate` | `done ÷ target` §2.2 | 目标缺失/≤0 → `None` | `@TODO-OWNER` |
| `required_daily` | `月目标 ÷ 当月总工作日` §2.3 | 目标或日历缺失 → `None` | `@TODO-OWNER` |
| `severity` | 四级：p0/p1/p2/ok §2.3 | 缺口不可算 → `None` | `@TODO-OWNER` |
| `mom` | `(本期 − 上期) ÷ |上期|` + `direction` §2.4 | 上期缺失/为 0 → `None` | `@TODO-OWNER` |

四级告警（`Derived.severity`，取最高）：

- **p0**：`done == 0` 且已过 ≥ 2 个工作日 → 今日
- **p1**：`shortfall ≥ required_daily × 剩余工作日 × 0.5` → 本周内
- **p2**：`shortfall > 0` 且未达 p0/p1 → 观察
- **ok**：`shortfall ≤ 0`

护栏：日历/目标缺失时 p1 不可判，退到 p2，**不因数据缺陷把人标红**；
已闭月（`month` 取历史月）的「剩余产能」无意义 → `remaining_workdays=None`。

## 2. 目标取值纪律

月目标 = Σ各人员 `MAX(monthly_target)`，**绝不跨行 SUM**（melt 陷阱，与
`common/metrics/daily_report.py` 同口径）。金额单位统一 **元**（卡片 `unit: 元`），
前端按 `unit` 格式化为万，后端不在 SQL 做换算。

## 3. 后端出口（card id）

| card_id | 内容 | 参数白名单 | 落位 |
|---|---|---|---|
| `kpi_shortfall` | 人员粒度 target/done/shortfall/rate/required_daily/severity | `region` `month` | l1-cockpit（span 6） |
| `anomaly_top` | 同一口径的缺口 TOP 10（服务端名次 `rank`） | `month` | l1-cockpit（span 6） |

SQL 只取事实（`queries.shortfall_facts`），派生组装在 `queries.shortfall_rows`
里调用 `derived` —— 这是「口径后端化」的可测边界：SQL 文本里不碰任何派生列。

## 4. 前端如何消费

1. 取数走既有的 `GET /api/v1/d/{dashboard}/cards/{card_id}`（`Cache-Control: no-store`），
   行已按 `shortfall` 降序排好（§2.1 默认排序键），缺口不可算的行排在末尾。
2. 渲染： columns（`schema`）驱动表格/列表；`severity` 映射 `--severity-*` token，
   `mom.direction` 映射 `--up/--down`；`None` 一律渲染「—」并按 §5 加数据缺陷角标。
3. **前端临时实现的 `derive.ts` 由前端主线删除**（本文件不越界改动 `frontend/**`）；
   删之前请勿重复计算缺口/告警/环比 —— 需要新指标就回后端补。
4. 暂未开放 URL 上的 `grain` 参数：新增筛选 source 要动 `app._FILTER_SOURCE_QUERIES`
   与 `config.KNOWN_FILTER_SOURCES`，留给下一批增量（`shortfall_facts` 已支持 `grain=region`）。

## 5. 日历的已知限制与第 5 类数据缺陷

- **工作日真相源 = `dim_calendar`**（大小休 + 法定假 + 年度调休，**不是**周一至周五）：
  `calendar.seed.json` → `calendar_utils.generate_rest_days` → `extract_mart._extract_calendar`
  （逐日落 `is_workday`、整体替换）；2026-09 由此得 24 个工作日。
- **前端不得本地推算工作日**：只消费后端下发的 `total/elapsed/remaining_workdays`，
  缺失显示「—」（大小休轮换与年度调休，前端无从得知）。
- **已知限制**：seed 现仅覆盖 2026-09 ⇒ 跨月/历史月无日历行 → `required_daily`、
  `remaining_workdays` 为 `None`，severity 自动降级 p2（已实现，不误报）。该限制**阻塞**
  季度预实、月度同比等依赖历史月的模块，需单独立项补「跨月日历」。
- **`has_fact` 是纯诊断字段**，不参与任何派生判定：False = 本月截至今日无销单行，
  而左表由 target 驱动 ⇒ 它就是挂零的另一名字。真正的**第 5 类缺陷「无事实行」暂缓定性
  —— 待定**：需独立的人员状态信号（离职/未启用/数据源未接入），现有模型没有该字段，
  **不得用 LEFT JOIN 空值倒推**代替。真机实测 2026-09 的 42 人全部 `has_fact=true`。

## 6. 测试

`python -m unittest discover -s tests -t . -k bi_web_derived -v`
—— 覆盖 p0/p1/p2/ok 四边界（含 p0 的 ≥2 工作日、p1 的 0.5 系数）、`mom` 的
`prev=0`/`None`、无目标返回 `None`，以及回归用例「同一组数据在月初/月中得到同一 severity」。
**双端对拍夹具**：`tests/fixtures/derived_golden.json`（24 条派生 + 4 条日历 + 3 条行级
`row_cases`；期望值手写且标注 `source` = CubeSchema.md 条款）—— Python 与前端 TS 跑同一
份 JSON，两边都绿才算口径统一。
