---
name: search_products
description: "查询商品资料：箱规/尺寸/重量/材质/条码/礼袋规格等参数，或按条件筛选商品"
tags: [knowledge]
order: 1
parameters:
  type: object
  properties:
    keyword:
      type: string
      description: 商品名或筛选关键词
  required: [keyword]
---
商品参数检索（真源 kb_products，AI 表同步）。
适用：箱规/尺寸/重量/材质/条码/礼袋规格，或按条件筛选商品。
统计类问题（有多少种商品）用 product_stats，不要用本工具反复检索。
