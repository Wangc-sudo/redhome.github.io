# -*- coding: utf-8 -*-
"""
common — 数字中台公共能力层
============================
跨业务复用的能力，不含业务逻辑：
- dingtalk       钉钉开放平台统一客户端（token/AI表格/群消息/webhook）
- calendar_utils 工作日历判定（休息日/工作日/早报口径）
- gateway        钉钉网关（Stream 收数 + outbox 投递，唯一外发通道）
- wdt            旺店通旗舰版 ERP 客户端

业务模块只依赖本包，不互相依赖。旧 broadcaster 已随双轨收口删除，
群播报统一走 gateway/outbox。
"""
