---
name: query_sql
description: "受控 SQL 查询业务库（只读 SELECT，白名单表，自动 LIMIT，大表必须带日期过滤）。用于其他工具答不了的订单/出库/退款/库存/人员目标类问题。"
tags: [business]
order: 10
export: true
parameters:
  type: object
  properties:
    sql:
      type: string
      description: 单条 SELECT 语句（MySQL 方言）
  required: [sql]
---
受控 SQL（六层护栏：SELECT-only / 表白名单 / 强制 LIMIT / EXPLAIN 预检 /
MAX_EXECUTION_TIME / 结果截断），handler 集中在 executor，本文件只是文档。

日期过滤铁律（2026-11-02 用户截图事故）：
- 必须写范围谓词 trade_time>='YYYY-MM-DD' AND trade_time<'YYYY-MM-DD'；
- 禁止 DATE(trade_time) 包裹列——函数包裹让索引失效变全表扫描，
  被 EXPLAIN 护栏拒绝（实测范围写法 21 天聚合 0.2s，DATE() 写法估算 70 万行）；
- 出库表 consign_time 同理。
- 被护栏拒绝或超时时自行修正写法或缩小范围重试，不要把「分段跑」推给用户。
