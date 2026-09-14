# bi-web 阶段 B：L2 分析页三页 + L1 日环比卡设计

状态：待确认（评审稿；确认后走 writing-plans 出实施计划）

## 1. 背景与范围

阶段 A（L1 驾驶舱五卡 + 认证 + compose + 合约测试）已落地。阶段 B 在**零管线改动、零 compose 改动、零新表**的前提下扩展展示层：

- 新增 3 个 L2 分析页：`l2-region`（区域下钻）、`l2-channel`（渠道明细）、`l2-people`（人员榜）
- L1 增补模块 ⑭：线下/电商日环比卡两张（需求表中唯一「数据已就绪但未排期」的模块）
- 新机制：配置驱动的筛选器（region/channel/month）、bar 点击下钻、跨页导航、table 图表类型、scalar 载荷扩展

明确出界：品牌维度卡（B1，等 476 个无品牌 SKU 分类规则）、商品动销仪表盘（B2，等 extract 聚合架构）、C 类人工报表通道、D 类缺源模块、线下渠道字典（E 类）。

**定位声明**：本批「区域→部门→人员」与「渠道→店铺」两条下钻链是**数据现实决定的过渡形态**——线下数据长这样（日报机器人既有口径）。维度字典正式的「销售分析页」下钻链（板块→业务单元→品牌→渠道→SKU）需待 B1+B2+E 类字典齐备后另批实现；人员榜亦非需求表独立模块，属既有口径的看板化呈现。本批三页不应被当作正式下钻链验收。

## 2. 模块编号对照（勘误）

以《BI看板颗粒度需求表》现行编号为准（文件 2026-09-09 后未改）：

| 需求表编号 | 模块 | 阶段 B 覆盖 |
|---|---|---|
| ① | 板块日销（板块，日） | A 已上线线下+电商；B 增补电商渠道×店铺下钻、线下区域×部门下钻 |
| ② | 销售结构下钻（单元×品牌×渠道×SKU，日） | B 覆盖「渠道×店铺」子集；业务单元/品牌/SKU → B1+B2 |
| ⑫ | 年度目标达成（公司+板块，日累计） | A 已上线；品牌树 → B1 |
| ⑭ | 日环比（板块，日） | **本批新增 L1 两卡** |
| ③⑦⑧ | 库存/效期/仓储（B2） | 不在本批 |
| ④⑤⑥⑪⑬ | C 类人工报表 | 不在本批 |
| ⑨ | D 类缺真源 | 不在本批 |
| ⑩ | B1 fin 五表投影 | 不在本批 |

历史文档曾把「电商渠道日销」标为 ③，系编号误植：需求表 ③ 为库存补货预警（B2）。阶段 A 实际交付 = ①（线下+电商两板块）+ ⑫ + 电商渠道粒度加成。

## 3. 页面与卡片

### 3.1 L1 增补：⑭ 日环比卡

| 卡片 | 图表 | 口径 |
|---|---|---|
| kpi_offline_dod | scalar 扩展 | 当日=Σsales_amount（business_date=表内最新数据日且 ≤CURDATE()，排除合计行）；prev=前一**自然日**（非前一数据日，环比基期按需求表）；delta_pct=(value−prev)/prev；近 7 自然日逐日值 |
| kpi_channel_dod | scalar 扩展 | 同口径，源 fact_channel_daily_sales，无合计行问题 |

scalar 载荷新增可选字段（向后兼容，无则渲染不变）：

```json
{
  "chart": "scalar",
  "value": 86000.0,
  "date": "2026-09-11",
  "prev": 79000.0,
  "delta_pct": 0.0886,
  "trend7": [{"date": "2026-09-05", "value": 70000.0}, {"date": "2026-09-06", "value": null}]
}
```

渲染：大数字+数据日；次行「前一日 X 万 · 环比 +8.9%」，**升绿降红**（±20% 阈值标红属需求表 IT 确认项，待业务定后再加）；卡内 ECharts 迷你趋势（7 日，缺数日断线）。seed 布局：两卡 span 6+6 置顶（「每天开板先看环比」），原有五卡不动。⑭ 末级的「下钻到业务单元×品牌」不在本批——区域/渠道趋势线已给日粒度穿透。

### 3.2 l2-region 区域下钻（fact_daily_report_offline）

| 卡片 | 图表 | 参数 | 口径 |
|---|---|---|---|
| kpi_region_mtd | scalar+进度条 | region, month | 选中区域当月 Σsales（截断未来+排除合计）；月目标=**Σ各人员 MAX(monthly_target)**（按人取 MAX 再求和，规避 melt 重复行）；进度=Σsales÷月目标 |
| trend_region_daily | line | region, month | 复用 L1 卡激活参数：有 region 单系列、无则杭州/绍兴双系列 |
| bar_department_mtd | bar | region, month | 选中区域当月按部门 Σsales 降序（杭州约 8 组/绍兴 5 组） |

筛选器：region、month。

### 3.3 l2-channel 渠道明细（fact_channel_daily_sales，7 渠道/32 店）

| 卡片 | 图表 | 参数 | 口径 |
|---|---|---|---|
| bar_channel_mtd | bar（可点击） | month | 复用 L1 卡+month；点击渠道条→设置 channel 参数（下钻入口，见 §5） |
| trend_channel_daily | line | month | 当月按日 Σsales 分渠道 7 系列 |
| table_channel_mtd | table | month | 渠道对比：销售额/推广费/ROI/店铺数；**ROI=Σsales÷Σpromo**（非行级均值）；推广费缺失→「—」（直播 48/48、京东 12/12 为 null） |
| table_store_mtd | table | channel, month | 店铺排行：Σsales 降序；无 channel 参数=全部 32 店含渠道列，有=该渠道店铺（≤10 行，无分页） |

筛选器：channel、month。

### 3.4 l2-people 人员榜（复用 common/daily_robot/mart_leaderboard）

| 卡片 | 图表 | 参数 | 口径 |
|---|---|---|---|
| kpi_people_count / kpi_people_completed / kpi_people_rate | scalar | region, month | 参与人数 / Σ完成额 / 总达成率=Σcompleted÷Σtarget——三卡由**同一次 mart_collect 调用**派生，不加查询 |
| table_people_leaderboard | table | region, month | 复用现有口径（含未完成缺口列），排序 (-rate, -completed, -target)；无 region 参数=杭州(28)+绍兴(14) 两榜合并 |

筛选器：region、month。

## 4. 筛选机制（配置驱动）

`DashboardConfig` 新增可选字段（seed 与 Nacos 同构，走同一 fail-open 链）：

```yaml
l2-region:
  title: "区域下钻"
  nav_order: 10
  filters:
    - {param: region, source: regions, label: "区域"}
    - {param: month,  source: months,  label: "月份"}
```

- 三个 source 由服务端查维表渲染下拉：`regions`=fact_daily_report_offline DISTINCT region；`channels`=fact_channel_daily_sales DISTINCT channel；`months`=两张事实表 DISTINCT 月 ∪ 当前月，降序（只出现有数据的月份，数据积累后自动出新选项）
- JS：下拉变更→URL 查询串更新（replaceState 不刷页）→全部卡片带参重拉；轮询沿用当前 URL 参数
- **双闸校验**：params_schema 白名单（未知参数 400，阶段 A 既有机制本批激活）+ 值合法性（region/channel/month 必须在对应维表集合内，否则 400）；SQL 全参数化
- 缺省语义：无 month→当前月；无 region/channel→卡片各自默认口径（全区域/全渠道）
- **L1 驾驶舱不加筛选器**，总览页保持无参

## 5. 下钻与导航

- **图表点击下钻**：placement 新增可选 `on_click: {param: channel}`——bar_channel_mtd 点击渠道条→JS 设 channel 参数并同步下拉框。通用机制，渠道语义不写死在 JS；校验规则：on_click.param 必须出现在该页 filters 中，否则启动即报错
- **跨页导航**：base.html 顶部导航列出全部 enabled 看板，按新字段 `nav_order` 升序（l1-cockpit=0，三 L2 页=10/20/30），当前页高亮；看板 disable 后自动从导航消失

## 6. table 图表类型与 scalar 扩展

table 载荷（首次实现渲染分支，纯 DOM `<table>`，不引入前端框架）：

```json
{
  "chart": "table",
  "columns": [
    {"key": "rank", "title": "排名"},
    {"key": "sales", "title": "本月销售额", "format": "wan"}
  ],
  "rows": [{"rank": 1, "store": "天猫官方旗舰店", "sales": 1234567.89}]
}
```

- format 三种：`wan`（÷10000 千分位+「万」，与 scalar 同一格式化助手）/ `percent`（×100 保留 1 位小数+%，null→「—」）/ `ratio`（2 位小数，null→「—」）；缺省原样输出
- 「排名」列值由服务端计算含于 rows；空结果→「暂无数据」占位行

## 7. 口径细则（沿用+新增）

沿用阶段 A：business_date≤CURDATE() 截断、合计行排除（`responsible_person NOT LIKE '%合计%'`）、金额单位元/前端格式化万、⑫ 分母 760,210,000。

新增：

1. **区域月目标**=Σ各人员 MAX(monthly_target)（§3.2，绝不跨行 SUM 目标）
2. **ROI**=Σsales÷Σpromo，缺失渠道显示「—」（§3.3）
3. **环比基期**=前一自然日；prev 缺失或为 0→delta_pct 为 null 显示「—」
4. **人员榜总达成率**=Σcompleted÷Σtarget（非个人率平均）
5. 近 7 日窗口按自然日取值，缺数日 value=null 断线呈现

## 8. 错误处理

全部沿用阶段 A 语义：单页 503 不影响其他页、fail-open 配置链、错误响应无 SQL 无 traceback、`/api/` no-store。新增两条：非法参数值→400 安全文案；无数据月份/渠道组合→空结果而非报错。

## 9. 测试

- **口径单测**：新 SQL 在 test-runner 对 mart_ops_test 真实执行；ROI/区域月目标/dod 三处人工对拍；leaderboard 对拍 mart_collect 直调结果
- **配置校验**：seed 引用未知 filter source / 同页重复 param / on_click.param 不在 filters / 非法 nav_order→启动即报错（与未知 card 同级防护）
- **参数校验**：未知参数 400、非法 month/region/channel 400、合法参数过滤生效（TestClient 集成覆盖）
- **合约测试零改动**（compose 不动）
- **浏览器冒烟**：四页渲染、筛选变更→URL+数据联动、bar 点击→channel 下钻、table 渲染、导航互达

## 10. 估算与阶段边界

机制（filters/table/导航/下钻/scalar 扩展）3–4 人天 + 三页口径卡 2–3 人天 + ⑭ 卡 0.5 人天 + 测试冒烟 1–1.5 人天 → **6–9 人天**。

后续批次：B1（dim_product 投影+品牌维度+⑩ fin 五表）、B2（商品动销+库存+仓储，先定 extract 聚合架构）、C 类人工报表、阶段 C（免登/行级权限/Nacos listener）。

## 11. 开放点

1. ±20% 波动预警阈值（模块①⑭共同 IT 确认项）——本批先方向色（升绿降红），阈值确认后加标红
2. 周末/节假日环比口径提示——本批靠趋势图日期轴人工结合日历；是否自动标注周末待 dim_calendar 字段盘点后定
