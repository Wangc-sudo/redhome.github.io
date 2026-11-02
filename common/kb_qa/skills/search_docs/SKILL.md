---
name: search_docs
description: "检索产品手册与业务文档（品牌工艺/卖点话术/售后流程/销售总结与规划）"
tags: [knowledge]
order: 2
parameters:
  type: object
  properties:
    query:
      type: string
      description: 检索问题
  required: [query]
---
文档检索：BM25 + 向量合并（kb_doc_chunks + rag 向量库）。
适用：产品手册、品牌工艺、卖点话术、售后流程、销售总结与规划。
