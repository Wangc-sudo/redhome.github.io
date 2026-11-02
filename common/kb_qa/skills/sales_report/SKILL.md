---
name: sales_report
description: "运营周报/月报素材一次拿全：期间总量+环比+渠道分布+TOP店铺+TOP单品+库存三态。问「做个运营周报/月报」「本期经营情况怎么样」用它，不要拆成多次 query_sql"
tags: [business]
order: 11
export: false
snapshot: wdt_order_line_fact
parameters:
  type: object
  properties:
    month:
      type: string
      description: 目标月份 YYYY-MM（如 2026-10），与 start_date/end_date 互斥
    start_date:
      type: string
      description: 开始日期 YYYY-MM-DD，需与 end_date 同时提供
    end_date:
      type: string
      description: 结束日期 YYYY-MM-DD（含当天），缺省为本月 1 日至今
    top_n:
      type: integer
      description: 店铺/单品榜条数，缺省 10，上限 30
---
运营报表（fact_order_line 预定义聚合，一次拿全五段素材）。
环比：整月报表=上一自然月，否则=紧邻等长上一段；同比另用 channel_yoy/sku_sales。
库存三态为辅信息，缺快照时该段为空、不拖垮整份报表。
