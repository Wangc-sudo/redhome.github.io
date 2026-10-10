"""管道功能分类（category）——注册表真源 + service_id 推导兜底。

七值分类（2026-10-07 裁决：分类沉为后端真源，不再只是 ops-web 展示层推导）：

    数据线：同步 / 加工
    业务线：播报 / 催办 / 页面
    应用类：钉钉 / 平台

``PipelineConfig.category`` 是持久化真源（Nacos ``PIPELINES`` 组条目可选
字段）；未配置的存量条目由 :func:`derive_category` 按 service_id 推导兜底
（显式映射 → 家族前缀），与 2026-10-07 之前 ops-web 页面推导逻辑一致，
回填迁移完成前读路径零行为变化。
"""

SUPPORTED_CATEGORIES = ("同步", "加工", "播报", "催办", "页面", "钉钉", "平台")

#: category → 三表归类（定时任务页分表）。
CATEGORY_CLASS = {
    "同步": "数据线",
    "加工": "数据线",
    "播报": "业务线",
    "催办": "业务线",
    "页面": "业务线",
    "钉钉": "应用类",
    "平台": "应用类",
}

#: 三表渲染顺序。
CLASS_ORDER = ("应用类", "业务线", "数据线")

#: 无 family 前缀的服务显式登记（与 seed/注册表现存 service_id 对齐）。
SERVICE_CATEGORIES = {
    "roll-manifest": "同步",
    "sync-dingtalk": "同步",
    "sync-wdt": "同步",
    "sync-runner": "同步",
    "sync-channel-sales": "同步",
    "kb-sync-products": "同步",
    "kb-gateway": "钉钉",
    "project-mart": "加工",
    "extract-mart": "加工",
    "extract-channel": "加工",
    "channel-missing-check": "催办",
    "channel-daily-qudao": "播报",
    "offline-daily-summary": "播报",
    "offline-weekly-summary": "播报",
    "offline-monthly-summary": "播报",
    "target-rollover": "加工",
    "target-remind": "播报",
    "dingtalk-gateway": "钉钉",
    "bi-web": "平台",
    "scheduler": "平台",
    "ops-web": "平台",
}

#: 家族前缀归类（新区域服务注册即自动归类，无需改代码）。
#: robot-check- 必须排在 robot- 前（同前缀，长者优先；2026-10-07
#: robot 一拆二：robot-=填报提醒、robot-check-=催办未填人+DING）。
CATEGORY_FAMILY = (
    ("robot-check-", "催办"),
    ("robot-", "催办"),
    ("pages-", "页面"),
    ("leaderboard-", "播报"),
)


def derive_category(service_id):
    """按 service_id 推导 category：显式映射 → 家族前缀 → None（未归类）。"""
    category = SERVICE_CATEGORIES.get(service_id)
    if category is not None:
        return category
    for prefix, family_category in CATEGORY_FAMILY:
        if service_id.startswith(prefix):
            return family_category
    return None


def resolve_category(service_id, configured=None):
    """读路径统一入口：注册表真源优先，未配置按 service_id 推导兜底。

    返回值仍可能为 None（既未配置又推导不出），调用方按「未归类」处理。
    """
    if configured:
        return configured
    return derive_category(service_id)
