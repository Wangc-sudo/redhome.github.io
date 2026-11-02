---
name: channel_yoy
description: "按 DB 订单行计算渠道销售同比：本期与去年同期均查 fact_order_line.paid_amount，支持按店铺拆分；缺省日期为本月 1 日截至今天"
tags: [business]
order: 6
export: true
snapshot: channel_daily_sales
parameters:
  type: object
  properties:
    channel:
      type: string
      description: 渠道名（如京东），留空为全部渠道
    start_date:
      type: string
      description: 本期开始日期 YYYY-MM-DD，需与 end_date 同时提供
    end_date:
      type: string
      description: 本期结束日期 YYYY-MM-DD（含当天），需与 start_date 同时提供
    group_by:
      type: string
      enum: [channel, store]
      description: channel 按渠道汇总；store 按店铺拆分
    shop_name:
      type: string
      description: 只看某家店（模糊匹配店铺名），留空为全部店铺
---
渠道同比：本期与去年同期同查 DB 订单行 + dim_channel_map 映射，两期同源同口径。
has_previous_data=false 时必须明示去年同期缺数，禁止改用 channel_sales 或静默改成环比。
