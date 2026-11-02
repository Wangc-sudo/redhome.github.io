---
name: daily_sales
description: "查询某区域某日销售：销售额、日环比、月目标完成率"
tags: [business]
order: 4
export: true
snapshot: daily_report_offline
parameters:
  type: object
  properties:
    scope:
      type: string
      description: 区域标识（如 hangzhou）
    date:
      type: string
      description: YYYY-MM-DD，缺省为最近一天
  required: [scope]
---
线下区域日聚合（agg_offline_daily）：销售额、日环比、周环比、月目标完成率。
scope 用 DB DISTINCT 动态白名单，非法值回合法清单。
