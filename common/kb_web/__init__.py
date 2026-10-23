# -*- coding: utf-8 -*-
"""小易 Web 对话前端的后端面（2026-10-23 方案 V1）。

模块划分：
- sessions.py  Web 会话存储（Redis 主、进程内兜底；7 天滑动 TTL）
- acl.py       业务数据角色白名单（kb_web_acl 表，默认只答商品/手册）
- service.py   问答服务（复用 kb_qa 的 _answer 纯函数主链路）
- routes.py    FastAPI 路由组（挂 bi-web /kb/api/*）
"""
