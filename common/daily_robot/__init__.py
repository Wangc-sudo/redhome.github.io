# -*- coding: utf-8 -*-
"""daily_robot — 日报机器人（新链路单轨）。

旧双轨（core/listener/broadcaster 直读写 AI 表）已随阶段 4 收口退役：
报数走 gateway/report_intake（Stream 直落 mart_ops），提醒/催办/榜单走
mart_cli + mart_tasks + outbox，投递统一由 dingtalk-gateway 执行。

本包不再做任何包级 re-export——消费方一律显式 import 子模块
（mart_cli / mart_tasks / mart_leaderboard / offline_summary /
channel_daily / leaderboard），避免 import 包时被动加载旧链路依赖。
"""
