# -*- coding: utf-8 -*-
"""
common — 数字中台公共能力层
============================
跨业务复用的能力，不含业务逻辑：
- dingtalk       钉钉开放平台统一客户端（token/AI表格/群消息/webhook）
- calendar_utils 工作日历判定（休息日/工作日/早报口径）
- broadcaster    群播报器（Markdown 群发/@人/DING 命令生成）
- wdt            旺店通旗舰版 ERP 客户端

业务模块（数字化/钉钉/*）只依赖本包，不互相依赖。
"""
