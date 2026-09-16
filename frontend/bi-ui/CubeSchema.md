# BI 统一取数契约（CubeSchema）

> 配套：`bi-ui/` 三件套（tokens / base / components）。
> 原则来源：《前端与 BI》精读 —— *"围绕数据计算模型确定一套固定的接口格式，取数不依赖组件，所有组件对标准数据都有对应的展现。"*
> 决策来源：`design-system-unification-plan.md` §0.5 + 2026-09-15 dodo 拍板：派生指标后端统一算，前端只渲染。

---

## 0. 为什么要有这份契约

V1（bi-web 通用渲染器）、V2（DIGITAL OPS 门户）、异常驱动看板（L1 驾驶舱）现在**各自算一遍**缺口/告警/环比，导致"皮肤统一、含义分裂"。本契约把口径收口到**后端**，三前端只消费同一份结果。

**铁律**：前端 **不得** 计算 `缺口 / 进度差 / 告警级别 / 环比` 任何一项。一旦需要，说明该指标还不在 CubeSchema 里，应回到后端补，而不是在前端补丁。

---

## 1. 数据集形态（三前端唯一消费格式）

```jsonc
{
  "meta": {
    "dashboard": "l2-region",          // 看板标识（V1 pathname 即此值）
    "title": "大区经营总览",
    "as_of": "2026-09-04",             // 数据截至（业务日期）
    "fetched_at": "2026-09-15T11:00:00+08:00", // 读取时间
    "grain": "person",                 // person | dept | channel | product
    "unit": "wan"                      // 金额单位：wan(万) / yuan / yi(亿)
  },
  "schema": {                          // 列定义，驱动表格/图表无感知渲染
    "dimensions": ["dept", "name", "store", "channel"],
    "measures": [
      {"key": "target",    "label": "目标",   "type": "money", "unit": "wan"},
      {"key": "done",      "label": "已完成", "type": "money", "unit": "wan"},
      {"key": "shortfall", "label": "缺口",   "type": "money", "unit": "wan", "derived": true},
      {"key": "rate",      "label": "完成率", "type": "percent", "derived": true},
      {"key": "severity",  "label": "告警",   "type": "enum",   "derived": true,
       "domain": ["p0","p1","p2","ok"]},
      {"key": "mom",       "label": "环比",   "type": "delta",  "derived": true}
    ],
    "hierarchies": [                   // 层系：下钻 = 切换 grain，组件无感知
      {"from": "dept", "to": "person"},
      {"from": "channel", "to": "person"}
    ]
  },
  "rows": [ /* 二维表：每行一条记录，字段齐全；缺失按 §5 规则标注 */ ]
}
```

---

## 2. 派生指标口径（**后端计算，前端禁用**）

### 2.1 缺口 shortfall（默认排序键，降序）
```
shortfall = target_should − done
```
- `target_should` = 当期目标总额（**不是** `目标 × 时间进度`）。缺口表达的是"距离目标还差多少"，与日历无关，月内恒定。
- ❌ 旧错误口径：`should = 目标 × 时间进度` 会导致缺口随时间机械变小、月初误判"没人落后"。
- ✅ 排序只用 `shortfall`，与 BI 优化方案 `_logic_test.js:69` TOP10 口径一致。

### 2.2 完成率 rate
```
rate = done / target        (封顶不超 100% 显示，但原值保留用于告警)
```

### 2.3 四级告警 severity（**锚绝对缺口 / 所需日销，不锚相对进度差**）
> 修复 BI 优化方案 ❌1：原方案用 `gap = rate − 时间进度`，会随月内时间机械升级。本契约改为锚**绝对口径**。

| 级别 | 规则（满足任一即定级，取最高） | 色 token | 处理节奏 |
|---|---|---|---|
| 🔴 **p0** | `done == 0` **且** 已过 ≥ 2 个工作日 | `--severity-p0 #b42318` | 今日 |
| 🟠 **p1** | `shortfall ≥ required_daily × 剩余工作日 × 0.5`（即按当前日销，缺口需 > 半月产能才能补齐） | `--severity-p1 #bc4c00` | 本周内 |
| 🟡 **p2** | `shortfall > 0` 且未达 p0/p1 | `--severity-p2 #9a6700` | 观察 |
| 🟢 **ok** | `shortfall ≤ 0`（已达标/超额） | `--severity-ok #1a7f37` | 不占首屏 |

其中：
```
剩余工作日  = 当月总工作日 − 已过工作日
required_daily = target / 当月总工作日        // 为达成本月目标所需的日均
```
- 这样"今日需跟进谁"在月内**稳定**，不会第 20 天自动全员升级。
- p0 的"≥2 工作日"边界以**工作日**计（排除周末），与 BI 优化方案 §2.3 文案对齐但修正了 `levelOf` 只判 `done===0` 的漏洞（见 §6 待清项）。

### 2.4 环比 mom（涨红跌绿，国内惯例）
```
mom = (本期值 − 上期值) / |上期值|      // 方向 + 幅度
direction: up(涨) | down(跌) | flat(持平)
```
- 前端渲染：`.delta.up`=红 `--up`，`.delta.down`=绿 `--down`，`.delta.flat`=灰。
- 注意：**健康度（完成率是否达标）用 `--severity-*` 语义色，环比方向用 `--up/--down`**。同一数字不混用两套色（见 tokens.css 注释）。

---

## 3. 层系与下钻（组件无感知）

- L1 驾驶舱 `grain=person` 汇总；点击部门 → 请求 `?grain=person&dept=×`，后端按 hierarchy 切分，**前端图表/表格组件不变**。
- V2 门户跳 V1 详情，仅换 `dashboard` 标识 + query，不换组件库。

---

## 4. 字段规范速查

| 字段 | 类型 | 单位 | 来源 | 备注 |
|---|---|---|---|---|
| `target` | money | wan | 业务系统 | 当期目标 |
| `done` | money | wan | 业务系统 | 当期已完成 |
| `shortfall` | money | wan | **派生** | §2.1 |
| `rate` | percent | — | **派生** | §2.2 |
| `severity` | enum | — | **派生** | §2.3，域 p0/p1/p2/ok |
| `mom` | delta | — | **派生** | §2.4，含 direction |
| `forecast_month_end` | percent | — | 业务系统 | 见 §5 低置信标注 |
| `store_count` | int | — | 业务系统 | 0 → 标"待补" |

---

## 5. 数据缺陷显式化（前端按 flag 渲染，不静默留空）

| 情况 | 字段值 | 前端展示 | token |
|---|---|---|---|
| 线性外推预测（样本 < 7 天） | `forecast_month_end` + `low_conf:true` | 标签改「按当前日均推算」+ 灰标「样本 N 天·低置信」 | `.badge-lowconf` |
| 无推广费（ROI 不可算） | `roi: null` | 显示 `—`，悬停「未投放/未取数」，**不参与 ROI 排序** | — |
| 店铺数 = 0 | `store_count: 0` | 显示「待补」橙标，**不计入店铺均值** | `.badge-tobefilled` |
| 未填（值为天数） | `unfilled_days` | 列名「未填天数」，`>0` 加数据质量角标 | `.badge-defect` |

---

## 6. 待清项（P1，后端落地时一并处理）

- ⚠️ p0 边界：当前 `levelOf` 仅判 `done===0`，需补"≥2 工作日"判定（BI 优化方案 §2.3 文案已写但实现未兑现）。
- ⚠️ `store_count` 均值剔除逻辑须后端在聚合时处理，前端不二次计算。
- ⚠️ `unit` 缩放（≥1万→「13.2万」/ ≥1亿→「1.2亿」）建议后端输出原始值 + `unit`，前端只做格式化（异常驱动 §3.3）。

---

## 7. 对比度核验（WCAG AA，白底）

| token | hex | 对比度 | 结论 |
|---|---|---|---|
| `--severity-p0` | #b42318 | ≥ 4.5:1 | ✅ |
| `--severity-p1` | #bc4c00 | ≥ 4.5:1 | ✅ |
| `--severity-p2` | #9a6700 | ≥ 5:1 | ✅ |
| `--severity-ok` | #1a7f37 | ≥ 4.5:1 | ✅ |
| `--up`(=p0) | #b42318 | ≥ 4.5:1 | ✅ |
| `--down` | #067647 | ≥ 4.5:1 | ✅ |
| `--text` | #1f2933 | ≥ 13:1 | ✅ |

> 所有告警 **必须** 配文字/图标（`.alert-chip` 已强制），不靠颜色单独传意（异常驱动 §3.4）。
