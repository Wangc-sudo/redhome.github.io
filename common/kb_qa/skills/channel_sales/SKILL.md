---
name: channel_sales
description: "按渠道汇总某日销售额（可指定单一渠道）"
tags: [business]
order: 5
export: true
snapshot: channel_daily_sales
parameters:
  type: object
  properties:
    channel:
      type: string
      description: 渠道名，留空为全部渠道
    date:
      type: string
      description: YYYY-MM-DD，缺省为最近一天
---
AI 表格日销口径（fact_channel_daily_sales）。
上游预置未来空行，工具内已强制 business_date<=CURDATE()。
同比/去年同期问题不要用本工具，走 channel_yoy。
