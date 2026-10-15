# -*- coding: utf-8 -*-
"""巡检告警通用通道（C5）：阈值筛选 + 去重，出口 = ``outbox`` kind='alert'。

背景缺口（2026-10-15 裁决）：``robot_outbox`` 的 ``_KINDS`` 里已有
``target_remind`` / ``calendar_watch`` 等告警类 kind，但
``gateway/delivery.py`` 的 ``_GROUP_KINDS`` 未同步登记——这些线入队后被
判 ``unknown_outbox_kind`` 打成死信，告警发不出去（channel_daily 曾踩过
同型坑，见 commit b6ad963）。本模块把「巡检告警」收敛为一个通用出口：

* 任何巡检线把「发现」归一为 :class:`Finding`（``code``/``severity``/``text``）；
* :func:`select` 按阈值只保留达线发现（默认 ``warning``，低于阈值静默）；
* :func:`emit` 生成 markdown 并 **入队到运维告警群**（``region='ops'``，
  kind ``alert``），入队即由 gateway 投递；
* **去重由 outbox 唯一键承载**（``region:kind:business_date[:suffix]``）：
  :func:`dedupe_suffix` 把 ``alert_cooldown_hours`` 折算成窗口后缀，
  并把 ``service_id`` 编进后缀——同一业务日多条巡检线互不顶掉彼此。

频率/阈值的真源是**管道注册表 params**（``alert_threshold`` /
``alert_cooldown_hours``），由 ops-web「告警设置」写 Nacos，本模块只读
调用方传入的 ``params``（不做 SQL、不读注册表），便于单测。
"""

from dataclasses import dataclass

#: 出口 kind（须同时在 ``outbox_repository._KINDS`` 与
#: ``gateway/delivery._GROUP_KINDS`` 登记——两处漏一处即死信）。
ALERT_KIND = "alert"

#: 投递区域（运维告警群）：region 真值见 ``docker/integration/regions.seed.json``，
#: robotCode/openConversationId 由运维在 Nacos ``region-ops.yaml`` 定点覆盖。
ALERT_REGION = "ops"

#: 严重度（升序即重要性升序）：阈值取其中之一，达线及以上才入队。
SEVERITY_ORDER = ("info", "warning", "critical")
DEFAULT_THRESHOLD = "warning"
DEFAULT_COOLDOWN_HOURS = 24

#: 注册表 params 键（ops-web「告警设置」写入）。
PARAM_THRESHOLD = "alert_threshold"
PARAM_COOLDOWN_HOURS = "alert_cooldown_hours"

#: 会向运维告警群发巡检告警的服务线（ops-web 据此显示「告警设置」入口；
#: 新增告警线时在此登记，避免前端入口散落）。
ALERT_SERVICES = frozenset({
    "channel-missing-check",   # 渠道门店到齐（同步门禁 fail-closed，不达不发）
    "calendar-watch",          # 工作日历覆盖看门（每月 25 日）
    "target-remind",           # 月末目标核对提醒（每月 28 日）
    "freshness-check",         # L3 数据新鲜度看门狗（A3）
})


@dataclass(frozen=True)
class Finding:
    """一条巡检发现（机器可读 ``code`` + 人话 ``text``）。"""

    code: str
    severity: str
    text: str


def severity_rank(severity):
    """严重度序号（越大越重）；未知值返回 ``-1``（视为不达线）。"""
    if isinstance(severity, str) and severity.strip().lower() in SEVERITY_ORDER:
        return SEVERITY_ORDER.index(severity.strip().lower())
    return -1


def normalize_threshold(value, *, default=DEFAULT_THRESHOLD):
    """阈值参数规范化：非法/缺省一律回落 ``default``（配置错不静默失效）。"""
    if isinstance(value, str) and value.strip().lower() in SEVERITY_ORDER:
        return value.strip().lower()
    return default


def normalize_cooldown_hours(value, *, default=DEFAULT_COOLDOWN_HOURS):
    """冷却小时数规范化：非正/非数/布尔一律回落 ``default``。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value if value >= 1 else default
    if isinstance(value, str):
        try:
            return normalize_cooldown_hours(float(value.strip()), default=default)
        except ValueError:
            return default
    return default


def resolve_params(params):
    """注册表 ``params`` → ``(threshold, cooldown_hours)``。"""
    source = params if isinstance(params, dict) else {}
    return (
        normalize_threshold(source.get(PARAM_THRESHOLD)),
        normalize_cooldown_hours(source.get(PARAM_COOLDOWN_HOURS)),
    )


def select(findings, *, threshold=DEFAULT_THRESHOLD):
    """按阈值筛选：保留 ``severity >= threshold`` 的发现（保序、按 code 去重）。"""
    floor = severity_rank(normalize_threshold(threshold))
    picked = []
    seen = set()
    for finding in findings or ():
        if not isinstance(finding, Finding):
            continue
        if finding.code in seen:
            continue
        if severity_rank(finding.severity) >= floor:
            seen.add(finding.code)
            picked.append(finding)
    return tuple(picked)


def cooldown_suffix(now, cooldown_hours):
    """冷却窗口后缀：同窗口内重复告警被 outbox 唯一键吞掉。

    冷却 ``>= 24h``（或非法/缺省）→ 每天最多一条（返回 ``None``，交给
    ``region:kind:business_date`` 天然按天去重）；``< 24h`` → 以 ``now``
    的时/分切窗口，返回 ``c<h>h<bucket>``。
    """
    if isinstance(cooldown_hours, bool) or not isinstance(cooldown_hours, (int, float)):
        return None
    if cooldown_hours < 1 or cooldown_hours >= 24:
        return None
    window_minutes = max(1, int(round(cooldown_hours * 60)))
    bucket = (now.hour * 60 + now.minute) // window_minutes
    label = int(cooldown_hours) if float(cooldown_hours).is_integer() else cooldown_hours
    return f"c{label}h{bucket}"


def dedupe_suffix(service_id, now, cooldown_hours, *, override=None):
    """去重键后缀：``<service_id>:<窗口|manual-...|daily>``。

    把 ``service_id`` 编进后缀，避免同一业务日多条巡检线共用一个
    ``region:kind:business_date`` 键互相顶掉；``override``（``--force``
    手动触发）优先，绕开冷却强制重发。
    """
    tail = override or cooldown_suffix(now, cooldown_hours) or "daily"
    return f"{service_id}:{tail}"


def build_message(findings, *, service_id, business_date, threshold, cooldown_hours):
    """告警正文（markdown）：分级发现列表 + 处置指引。返回 ``(title, body_md)``。"""
    title = f"巡检告警 · {service_id}"
    lines = [
        f"### {title}",
        "",
        f"- 业务日：{business_date.isoformat()}",
        f"- 阈值：{threshold}（此级别及以上才入队）",
        f"- 冷却：{cooldown_hours} 小时（窗口内重复只发一条）",
        "",
    ]
    for finding in findings:
        lines.append(
            f"- **{finding.severity.upper()}** [{finding.code}] {finding.text}"
        )
    lines.append("")
    lines.append(
        "处置：请按上述发现排查对应数据管线；本告警由巡检线自动发出，"
        "问题消失后自然静默（无需人工确认）。"
    )
    return title, "\n".join(lines)


def emit(outbox, *, service_id, findings, business_date, now, params=None,
         region=ALERT_REGION, override_suffix=None):
    """按 ``params`` 阈值筛选并入队；返回状态字符串。

    * ``"no_findings"`` —— 无发现（全部健康，静默）；
    * ``"below_threshold"`` —— 有发现但均低于阈值（静默）；
    * ``"enqueued"`` —— 已入队（gateway 待投递）；
    * ``"already_sent"`` —— 同去重键已存在（窗口内重复，静默）。
    """
    findings = tuple(findings or ())
    if not findings:
        return "no_findings"
    threshold, cooldown_hours = resolve_params(params)
    picked = select(findings, threshold=threshold)
    if not picked:
        return "below_threshold"
    title, body_md = build_message(
        picked,
        service_id=service_id,
        business_date=business_date,
        threshold=threshold,
        cooldown_hours=cooldown_hours,
    )
    enqueued = outbox.enqueue(
        region=region,
        kind=ALERT_KIND,
        business_date=business_date,
        title=title,
        body_md=body_md,
        created_at=now,
        dedupe_suffix=dedupe_suffix(
            service_id, now, cooldown_hours, override=override_suffix
        ),
    )
    return "enqueued" if enqueued else "already_sent"


# ---------------------------------------------------------------------------
# 巡检线读取：从管道注册表取本线 params（阈值/频率真源）
# ---------------------------------------------------------------------------

def fetch_service_params(service_id, *, config_source=None):
    """读本线在注册表的 ``params``（fail-soft：读失败返回 ``{}``）。

    巡检线在 cron 通道里跑，注册表抖动不应让整条线停摆——params 读不到
    就按默认阈值/冷却走（告警仍能发出，只是用了默认口径）。
    """
    if not service_id:
        return {}
    try:
        if config_source is None:
            from common.public_data.pipeline_config import build_config_source
            config_source = build_config_source()
        config = config_source.get_pipeline(service_id)
        params = getattr(config, "params", None)
        return dict(params) if isinstance(params, dict) else {}
    except Exception:
        return {}
