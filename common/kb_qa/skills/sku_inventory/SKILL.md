---
name: sku_inventory
description: "查询库存快照：可用库存、在途、可售天数、库存状态（紧急/超卖/呆滞）。问「还有多少货」「哪些呆滞」用它；结果为最新快照，务必带上快照日期"
tags: [business]
order: 8
export: true
snapshot: wdt_inventory_sku_daily
parameters:
  type: object
  properties:
    keyword:
      type: string
      description: 商品名或规格编码，留空为三态分布总览 + 各态 TOP 5
    state:
      type: string
      enum: [URGENT, OVERSOLD, DEAD]
      description: 只看某一状态：紧急补货/超卖/呆滞
    top_n:
      type: integer
      description: 返回前 N 个单品，缺省 10，上限 30
---
库存日快照（fact_inventory_sku_daily）：只有最新 business_date 是当前库存。
三态口径：URGENT=可售<7 天；OVERSOLD=可用为负；DEAD=近 30 天零出库（与库存量无关）。
