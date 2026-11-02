---
name: sku_sales
description: "按单品/SKU 查询销售：销售额与件数合计 + TOP N 单品（占比）。问「哪款卖得好」「各款销量」「某商品/系列（如金质）卖了多少」用它；商品销售额的同比=本期与去年同期各调一次再对比；问店铺/渠道汇总用 channel_sales/channel_yoy"
tags: [business]
order: 7
export: true
snapshot: wdt_order_line_fact
parameters:
  type: object
  properties:
    keyword:
      type: string
      description: 商品名或规格编码，留空为全部单品
    start_date:
      type: string
      description: 开始日期 YYYY-MM-DD，需与 end_date 同时提供
    end_date:
      type: string
      description: 结束日期 YYYY-MM-DD（含当天），缺省为本月 1 日至今
    channel:
      type: string
      description: 渠道名（如京东），留空为全部
    shop_name:
      type: string
      description: 店铺名（模糊），留空为全部
    top_n:
      type: integer
      description: 返回前 N 个单品，缺省 10，上限 30
---
SKU 维度销售（fact_order_line 按 spec_no 聚合）。
命中多规格时逐规格列出并标注 candidates，不合并。
