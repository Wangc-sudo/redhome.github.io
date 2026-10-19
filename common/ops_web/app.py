"""ops-web 应用装配：权限管理（grant 增删 + 审计流水 + 批量区域开通）
与定时任务管理（管道列表 / 开关 / 新增 / 立即运行一次 / 执行流水）。

访问控制双闸（铁律 8）：

* 网络层：compose 仅回环绑定（127.0.0.1:18100），不对局域网暴露；
* 应用层：除 ``/auth/*`` 外的每个路由都要求 session 身份且
  ``viewer.is_admin``——ops-web 不是面向全员的服务。

输出纪律与 bi-web 一致：错误体只用泛化词汇（unauthorized / forbidden /
unavailable / bad_request），异常只记一条带类名的 WARNING；页面 HTML 的
每一处插值都过 ``html.escape``（管理页同样无反射面）。

写路径（铁律 3 的另一半）：``bi_authz_grant`` 的 INSERT/DELETE 与定时
任务管理的 Nacos 发布 / run-request 写入只存在于本模块，且每次变更与
同事务的一行 audit 同生共死（run_request 行的 requested_by 即触发审计）。
"""

import hmac
import html
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from common.bi_web import auth, authz
from common.public_data import (
    bi_authz,
    calendar_store,
    channel_target,
    data_fix,
    fleet_audit,
    ops_control,
    org_assignment_override,
    org_catalog,
    recipient_override,
    report_roster,
)
from common.daily_robot.inspection_alert import ALERT_SERVICES
from common.public_data.channel_target import CHANNELS
from common.public_data.pipeline_config import PipelineConfig
from common.public_data.pipeline_taxonomy import (
    CATEGORY_FAMILY,
    SUPPORTED_CATEGORIES,
    resolve_category,
)
from common.public_data.scheduler import has_command

_LOGGER = logging.getLogger(__name__)


class ErrorDetail:
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    UNAVAILABLE = "unavailable"
    BAD_REQUEST = "bad_request"
    CONFLICT = "conflict"


def load_settings():
    from common.public_data.settings import Settings
    return Settings.from_environment()


def connect(database_settings):
    from common.public_data.db import connect as _connect
    return _connect(database_settings)


def _mart_connector(settings):
    @contextmanager
    def connector():
        connection = connect(settings.mart_database)
        try:
            yield connection
        finally:
            connection.close()

    return connector


def _esc(value):
    return html.escape("" if value is None else str(value))


#: 北京时间（全库时间列约定存 UTC，展示层统一转北京时）。
_BJT = timezone(timedelta(hours=8))


def _current_year_month():
    """当前年月（北京时间，``YYYY-MM``）——区域月目标的键。"""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m")


def _shift_year_month(year_month, months):
    """``YYYY-MM`` 平移 *months* 个月（名册页「下月」导航）。"""
    year, month = (int(part) for part in year_month.split("-"))
    index = year * 12 + (month - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _fmt_time(value):
    """UTC 存储时间 → 北京时间字符串；None/认不出的原样返回。
    入参兼容 pymysql 返回的 datetime 与字符串两种形态；naive 一律按 UTC
    解读（与 sync_runs/robot_outbox 等表的存储约定一致）。
    """
    if value is None or value == "":
        return value
    dt = value if isinstance(value, datetime) else None
    if dt is None:
        text = str(value).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_BJT).strftime("%Y-%m-%d %H:%M:%S")


# -- 展示层中文标签（底层值不动：审计/状态存量英文值仅在渲染时映射） --------
_RUN_STATUS_LABEL = {
    "finished": "成功",
    "failed": "失败",
    "rejected": "已拒绝",
    "pending": "等待调度",
    "launched": "运行中",
    "running": "运行中",
}
_TRIGGER_LABEL = {"cron": "定时", "run_once": "手动"}
_ACTION_LABEL = {
    "add": "新增", "enable": "启用", "disable": "停用",
    "delete": "删除", "seed": "种子导入", "publish": "发布快照",
    "update": "修正", "schedule": "改定时", "carry": "结转", "deps": "改依赖",
    "params": "改告警参数", "realign": "归位",
    "upsert": "改判", "reset": "恢复规则", "import": "法定导入",
    "reset_month": "月度恢复",
    "target": "月目标",
    "grant": "授权", "revoke": "收回",
}

#: 授权类型中文标签（成员与授权 / 授权现状 / 审计流水 三页共用）。
_GRANT_TYPE_LABEL = {"scope": "看板页面", "region": "区域数据"}

#: 名册 scope / entity_type 中文标签（填报名册页分组与表单）。
_SCOPE_LABEL = {
    "qudao": "渠道门店",
    "hangzhou": "杭州",
    "shaoxing": "绍兴",
    "junpin": "君品雅院",
    "vanke": "万科&大莲花&团购",
    "offline_all": "线下整体",
    "dining": "餐饮/部门",
    "hq": "总部",
    "other": "其他",
}
_ENTITY_LABEL = {"store": "门店", "person": "人员", "dept": "部门"}

#: 名册角色中文标签（v3：负责人/代填报人；仅 store 类型有 deputy）。
_ROLE_LABEL = {"owner": "负责人", "deputy": "代填报人"}

#: 能力（capability）枚举（2026-10-14 D4 能力×范围权限模型）：一个用户对某
#: 业务面的操作权 = ``<capability>``（全局）或 ``<capability>:<region>``（限该
#: 区域）。由 bi_authz ``grant_type='scope'`` 承载（``_SCOPE_KEY_RE`` 已放行
#: ``<capability>:<region>`` 形态），admin 恒持有全部能力。
CAPABILITY_ORG = "org"          # 组织编排（归属/区域/部门）
CAPABILITY_ROSTER = "roster"    # 填报名册（资格 + 区域/个人月目标）
CAPABILITY_PIPELINES = "pipelines"  # 定时任务（注册/开关/立即运行/收件人覆盖）
CAPABILITY_AUDIT = "audit"      # 操作记录（聚合审计读面）
CAPABILITY_CALENDAR = "calendar"  # 播报日历（可编辑的播报日集合）
CAPABILITY_DATAFIX = "datafix"  # 数据修正（类型化修数：预览/落库，全程审计）

#: 全部能力枚举（/api/me 逐项回报；SPA 菜单与路由守卫据此渲染）。
ALL_CAPABILITIES = (
    CAPABILITY_ORG, CAPABILITY_ROSTER, CAPABILITY_PIPELINES,
    CAPABILITY_AUDIT, CAPABILITY_CALENDAR, CAPABILITY_DATAFIX,
)


def capability_allows(viewer, capability, region=None):
    """纯函数：*viewer* 是否持有 *capability*（可选限定到 *region*）。

    admin 恒过；全局键 ``<capability>`` 覆盖全部区域；区域级键
    ``<capability>:<region>`` 只在 *region* 匹配时命中。未准入/fail-closed
    viewer 一律 False。
    """
    if viewer is None or not viewer.admitted:
        return False
    if viewer.is_admin:
        return True
    scopes = viewer.scopes
    if capability in scopes:
        return True
    if region is not None and f"{capability}:{region}" in scopes:
        return True
    return False


def capability_has_scoped(viewer, capability):
    """纯函数：viewer 是否持有 *capability* 的**任一**区域级授权
    （``<capability>:<region>``）——用于「列表页」这类无单区域上下文的闸。"""
    if viewer is None or not viewer.admitted:
        return False
    prefix = f"{capability}:"
    return any(scope.startswith(prefix) for scope in viewer.scopes)


def scoped_region_keys(viewer, capability):
    """纯函数：viewer 在 *capability* 上被授权的区域键列表（已排序、去重）。

    只认 ``<capability>:<region>`` 形态（全局 ``<capability>`` 不经此路，
    由 :func:`capability_allows` 自行判全量）。未准入 → 空（fail-closed）。"""
    if viewer is None or not viewer.admitted:
        return []
    prefix = f"{capability}:"
    return sorted({
        scope.split(":", 1)[1]
        for scope in viewer.scopes
        if scope.startswith(prefix) and ":" in scope
    })


def visible_region_scopes(viewer, scopes):
    """*scopes*（区域键列表）中该 viewer 可操作（或其区域级授权覆盖）的子集。

    admin / 全局 ``roster`` → 全量；仅持 ``roster:<r>`` → 只留授权区域。
    未准入 → 空（fail-closed）。"""
    return visible_scopes(viewer, CAPABILITY_ROSTER, scopes)


def visible_scopes(viewer, capability, scopes):
    """通用版 :func:`visible_region_scopes`（*capability* 任意）。

    2026-10-15 闭环修复：组织编排（org）等能力也要支持区域级授权，
    否则「授权页可授 ``org:<region>``」与「接口只认全局 org」自相矛盾——
    用户会拿到一条永远用不上的授权。"""
    return [s for s in scopes if capability_allows(viewer, capability, s)]


def _region_scope_pairs(connection):
    """区域注册表（``dim_org_region``）→ 有序 ``[(region_key, label)]``。

    真源读；注册表为空（迁移未应用 / DB 抖动）→ 回退代码镜像
    :data:`report_roster.SCOPES`（fail-soft，不拖垮名册页）。顺序即
    ``sort_order``——与组织编排树同序。
    """
    try:
        regions = org_catalog.fetch_catalog(connection).get("regions") or []
    except Exception:
        regions = []
    pairs = []
    for row in regions:
        key = str(row.get("region_key") or "").strip()
        if key and all(key != existing for existing, _ in pairs):
            pairs.append((key, row.get("label") or _label(_SCOPE_LABEL, key)))
    if not pairs:
        pairs = [(scope, _label(_SCOPE_LABEL, scope))
                 for scope in report_roster.SCOPES]
    return pairs


def _fact_region_keys(connection):
    """行级事实表（``fact_daily_report_offline``）的 region 值域（B5）。

    授权页「暂无行级数据」标注的真源：键在此集合内 = 授权可裁剪出数据；
    ``offline_all`` 由调用方另行豁免（B5 聚合授权键，oa 卡展开为三区
    并集，事实表本无该行属预期）。
    """
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT DISTINCT `region` FROM `fact_daily_report_offline`")
        rows = cursor.fetchall()
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()
    keys = set()
    for row in rows:
        value = row.get("region") if isinstance(row, dict) else row[0]
        if value:
            keys.add(str(value))
    return keys


def _find_person_roster_entry(connection, scope, person_name):
    """该 scope 下该人**启用中**的 ``person`` 型名册条目（无则 ``None``）。

    供归属搬迁（``/api/org/move``）双侧名册同步定位旧/新条目；scope 非
    名册业务域或人名为空 → ``None``（fail-soft，不抛）。
    """
    if not person_name or scope not in report_roster.SCOPES:
        return None
    for row in report_roster.fetch_roster(connection, scope):
        if (row.get("entity_type") == "person"
                and row.get("person_name") == person_name
                and int(row.get("enabled") or 0) == 1):
            return row
    return None

#: 「口径收口」（P5，2026-10-12，设计稿 §3）：名册 / 覆盖 / 授权三条边界裁决
#: 的唯一权威表述，只在「收件人总览」页完整渲染一次。
_RECIPIENT_RULES_HTML = (
    "<details><summary>口径说明：名册 / 覆盖 / 授权各管什么"
    "（点开看三层边界）</summary>"
    "<p class=\"hint\">有效 @ 名单 =（在职成员 ∩ 名册）∪ 任务级覆盖 − 移除。"
    "三层各管一件事，互不越界：</p>"
    "<table>"
    "<tr><th>层</th><th>管什么</th><th>粒度</th><th>波及面</th><th>去哪改</th></tr>"
    "<tr><td>名册</td><td>业务资格（谁有资格填报·被催）</td>"
    "<td>scope（区域/渠道）</td><td>该范围<b>所有</b>任务</td>"
    "<td><a href=\"/roster\">填报名册</a></td></tr>"
    "<tr><td>覆盖</td><td>单条任务的 @ 名单（include/exclude）</td>"
    "<td>service_id（单条线）</td>"
    "<td><b>仅</b>该条任务；不改正文、不改资格</td>"
    "<td><a href=\"/pipelines\">定时任务页</a>抽屉</td></tr>"
    "<tr><td>授权</td><td>系统访问权（看板 / 区域 / 名册管理权）</td>"
    "<td>看板 / 区域 / roster</td>"
    "<td>与「谁能被催办」<b>无关</b>，约 1 分钟生效</td>"
    "<td><a href=\"/grants\">授权现状</a></td></tr>"
    "</table>"
    "<p class=\"hint\">一句话：名册定「资格」，成员定「在职」，"
    "覆盖定「这条线的例外」。</p>"
    "</details>"
)


def _label(mapping, value):
    """注册值 → 中文标签；未注册的原样返回（渲染处仍过 _esc）。"""
    if value is None:
        return value
    return mapping.get(value, value)


#: 「操作记录」页的来源枚举（顺序即下拉顺序）。
_RECORD_SOURCES = ("授权", "定时任务", "填报名册", "播报日历", "收件人覆盖",
                   "定时运行", "数据修正")

#: 收件人覆盖 mode / target_type 中文标签（展示层）。
_MODE_LABEL = {"include": "追加", "exclude": "移除"}

#: 数据修正 fix_type 中文标签（展示层）。
_FIX_TYPE_LABEL = {
    "offline_person_region_realign": "线下人员归属归位",
}
_TARGET_TYPE_LABEL = {"user_id": "账号", "name": "姓名"}


def _audit_get(row, keys):
    """审计行取值：dict 按 key，序列按位置（与各页 unpack 顺序一致）。"""
    if isinstance(row, dict):
        return row.get
    return lambda key, r=row, ks=keys: r[ks.index(key)]


def _collect_records(connection):
    """5 处审计（授权/任务/名册/日历/收件人覆盖）+ 定时运行流水 → 统一行。

    每行形状 (时间, 来源, 操作人, 动作, 对象, 详情[, user_id])，供「操作记录」页
    归一展示（任务3 2026-10-12：运维改完一处即可查到全部变更）。第 7 位仅
    「授权」来源带（2026-10-15 闭环修复）：该页回源深链要 ``/grants?user=``
    的账号过滤，此前被丢在元组外，深链恒空过滤。
    """
    records = []

    for row in bi_authz.fetch_audit_rows(connection):
        get = _audit_get(row, ("actor", "action", "user_id", "grant_type",
                               "grant_key", "note", "created_at"))
        records.append((
            get("created_at"), "授权", get("actor") or "",
            _label(_ACTION_LABEL, get("action")),
            get("grant_key") or "",
            " ".join(x for x in (_label(_GRANT_TYPE_LABEL, get("grant_type")),
                                 get("note")) if x),
            get("user_id") or "",
        ))

    for row in ops_control.fetch_pipeline_audit(connection):
        get = _audit_get(row, ("actor", "action", "service_id", "detail",
                               "created_at"))
        records.append((
            get("created_at"), "定时任务", get("actor") or "",
            _label(_ACTION_LABEL, get("action")),
            get("service_id") or "", get("detail") or "",
        ))

    for row in report_roster.fetch_roster_audit(connection, limit=200):
        get = _audit_get(row, ("actor", "action", "scope", "entity_key",
                               "person_name", "detail", "created_at"))
        records.append((
            get("created_at"), "填报名册", get("actor") or "",
            _label(_ACTION_LABEL, get("action")),
            " ".join(x for x in (_label(_SCOPE_LABEL, get("scope")),
                                 get("entity_key")) if x),
            " ".join(x for x in (get("person_name"), get("detail")) if x),
        ))

    for row in calendar_store.fetch_recent_audits(connection, limit=200):
        get = _audit_get(row, ("actor", "action", "business_date", "year",
                               "month", "detail", "created_at"))
        biz, yr, mon = get("business_date"), get("year"), get("month")
        when = (biz.isoformat() if isinstance(biz, date)
                else (f"{yr}-{int(mon):02d}" if yr and mon else "—"))
        records.append((
            get("created_at"), "播报日历", get("actor") or "",
            _label(_ACTION_LABEL, get("action")), when, get("detail") or "",
        ))

    # 覆盖审计是本期新表：未迁移/读失败只跳过该来源，不整页 503
    # （与 /pipelines 覆盖列的 fail-soft 口径一致）。
    try:
        override_audits = recipient_override.fetch_recent_audits(
            connection, limit=200)
    except Exception as exc:
        _LOGGER.warning("ops-web recipient override audit load failed: %s",
                        type(exc).__name__)
        override_audits = ()
    for row in override_audits:
        get = _audit_get(row, ("actor", "action", "service_id", "mode",
                               "target_type", "target_value", "detail",
                               "created_at"))
        target = " ".join(x for x in (
            _label(_MODE_LABEL, get("mode")),
            _label(_TARGET_TYPE_LABEL, get("target_type")),
            get("target_value")) if x)
        records.append((
            get("created_at"), "收件人覆盖", get("actor") or "",
            _label(_ACTION_LABEL, get("action")),
            " ".join(x for x in (get("service_id"), target) if x),
            get("detail") or "",
        ))

    # 数据修正审计（第七源，2026-10-16）：fail-soft 同覆盖审计
    # （未迁移/读失败只跳过该来源，不拖垮整页）。
    try:
        data_fix_audits = data_fix.fetch_recent_audits(connection, limit=200)
    except Exception as exc:
        _LOGGER.warning("ops-web data fix audit load failed: %s",
                        type(exc).__name__)
        data_fix_audits = ()
    for row in data_fix_audits:
        get = _audit_get(row, ("actor", "action", "fix_type", "target",
                               "detail", "row_count", "created_at"))
        records.append((
            get("created_at"), "数据修正", get("actor") or "",
            _label(_ACTION_LABEL, get("action")),
            " ".join(x for x in (
                _label(_FIX_TYPE_LABEL, get("fix_type")), get("target")) if x),
            get("detail") or "",
        ))

    for row in ops_control.fetch_run_history(connection):
        get = _audit_get(row, ("id", "service_id", "trigger_type", "status",
                               "exit_code", "note", "started_at",
                               "finished_at"))
        exit_code = get("exit_code")
        records.append((
            get("started_at"), "定时运行", "系统",
            _label(_RUN_STATUS_LABEL, get("status")),
            get("service_id") or "",
            " ".join(x for x in (
                _label(_TRIGGER_LABEL, get("trigger_type")),
                f"退出码 {exit_code}" if exit_code is not None else "",
                get("note")) if x),
        ))

    records.sort(key=lambda r: _fmt_time(r[0]), reverse=True)
    return records


#: 服务标识 → 中文说明（策展映射，展示层；注册表 description 真源不动）。
#: 未收录的 robot-/pages- 家族按后缀动态生成，其余回退注册表原文。
_SERVICE_LABELS = {
    "roll-manifest": "滚动源清单的 WDT 采集窗口（每轮同步前先把窗口往前推）",
    "sync-dingtalk": "钉钉 AI 表 → raw_dingtalk 同步",
    "sync-wdt": "旺店通读接口 → raw_wdt 同步",
    "project-mart": "raw → mart_ops 投影重建（参数化修复工具，不参与定时调度）",
    "extract-mart": "raw_dingtalk → mart_ops 提取（事实表 + 日历维表）",
    "sync-channel-sales": "渠道日销 T+1 补采（钉钉 AI 表渠道日销 + 月目标）",
    "extract-channel": "渠道日销 T+1 提取（门店事实表 + 目标，点名册依赖）",
    "channel-missing-check": "渠道门店到齐校验与催办（同步失败自动跳过不发）",
    "channel-daily-qudao": "渠道日报播报（已并入渠道榜单页，2026-09-23 停单独播报）",
    "pages-qudao-t1": "渠道榜单页 T+1 重算",
    "offline-daily-summary": "线下整体每日汇总（板块 + 日环比 + 月累计）",
    "offline-weekly-summary": "线下整体每周汇总（上周总量 + 周环比 + 排名）",
    "offline-monthly-summary": "线下整体每月汇总（上月总量 + 月环比 + 达成率排名）",
    "target-remind": "月目标核对提醒（每月 28 日 → 有目标的区域群，附名册页入口）",
    "target-rollover": "月目标自动结转（每月 1 日把上月目标幂等结转到当月，人工已录入不覆盖）",
    "calendar-watch": "播报日历覆盖看门（每月 25 日查当月/下月日历，零行缺口 → 线下整体群告警）",
    "dingtalk-gateway": "钉钉网关（outbox 投递 + 互动回调，常驻）",
    "kb-sync-products": "知识库商品同步（AI 表「产品资料」→ kb_products，小时级）",
    "kb-gateway": "知识库问答机器人（客服群，独立 Stream 应用，常驻）",
    "fleet-audit": "舰队月检（每月 2 日四节体检快照 → 治理抽屉，不发群播报）",
    "bi-web": "BI 看板（L1/L2，只读 mart_ops，常驻）",
    "scheduler": "调度器（按注册表定时触发各管道，自身无定时，常驻）",
    "ops-web": "运维台（权限管理 + 定时任务管理，常驻）",
}

_CRON_DOW = {
    "0": "日", "1": "一", "2": "二", "3": "三",
    "4": "四", "5": "五", "6": "六", "7": "日",
}

#: 区域 → 中文名（服务名/描述展示用；与 regions 注册表 display 对齐）。
_REGION_LABELS = {
    "hangzhou": "杭州",
    "vanke": "万科&大莲花&团购",
    "shaoxing": "绍兴",
    "junpin": "君品雅院",
    "qudao": "渠道",
    "offline_all": "线下整体",
}

#: 「组织编排」SPA 专用区域中文名（_REGION_LABELS 补 hq/other；
#: 不改 _REGION_LABELS 本身，避免影响服务名/描述既有展示）。
_ORG_REGION_LABELS = dict(
    _REGION_LABELS, hq="总部", other="其他",
)

#: 区域展示顺序（种子优先级风格：常用业务区在前，兜底区在后）。
_ORG_REGION_ORDER = (
    "hangzhou", "shaoxing", "vanke", "junpin", "qudao", "offline_all",
    "hq", "other",
)

#: 组织编排 SPA 构建产物（Vite 产物提交入库：生产运行时不装 node、不构建）。
_ORG_SPA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "static")
_ORG_SPA_INDEX = os.path.join(_ORG_SPA_DIR, "dist", "index.html")


def _org_region_label(region):
    """区域键 → 中文名（未知键原样返回，空值回「未归类」）。"""
    if not region:
        return "未归类"
    return _ORG_REGION_LABELS.get(region, region)


def _build_org_overview(members, overrides, catalog=None):
    """在职成员 + 有效覆盖 + 组织目录 → 组织编排视图（regions / pool）。

    *members* 是 ``(user_id, name, region, dept_name)`` 元组
    （``bi_authz.fetch_active_members`` 的产出）；*overrides* 是
    ``{user_id: row}``（只含启用行；读失败时传空 → 回种子口径）；
    *catalog* 是 :func:`org_catalog.fetch_catalog` 的产出（登记的区域/部门，
    缺省空 → 完全等价旧行为）。

    有效归属：覆盖优先、其次种子派生；覆盖 ``region`` 为空 → 「未归类」池。
    部门分组键取 ``dept_id or dept_name``（基名单只有部门名，覆盖带 id）。
    目录登记的区域/部门即使**没有成员**也渲染（可作为拖拽归属目标）。
    """
    catalog = catalog or {}
    catalog_regions = {
        str(row.get("region_key") or "").strip(): row
        for row in catalog.get("regions") or []
        if str(row.get("region_key") or "").strip()
    }
    catalog_region_labels = {
        key: (row.get("label") or key) for key, row in catalog_regions.items()
    }

    grouped = {}        # region -> {dept_key: {dept_id, dept_name, members}}
    for user_id, name, region, dept_name in members:
        override = overrides.get(user_id)
        dept_id = None
        if override is not None:
            region = override["region"]
            dept_id = override["dept_id"]
            dept_name = override["dept_name"] or dept_name
        entry = {
            "user_id": user_id, "name": name,
            "dept_id": dept_id, "dept_name": dept_name or "",
            "region": region or None,
        }
        if not region:
            grouped.setdefault(None, []).append(entry)
            continue
        depts = grouped.setdefault(region, {})
        key = dept_id or dept_name or ""
        group = depts.get(key)
        if group is None:
            group = {"dept_id": dept_id, "dept_name": dept_name or "",
                     "sort_order": 0, "members": []}
            depts[key] = group
        group["members"].append(entry)

    # 目录登记（消费侧）：区域/部门即使无成员也播种进分组树。
    for row in catalog.get("depts") or []:
        region = str(row.get("region_key") or "").strip()
        if not region:
            continue
        dept_id = row.get("dept_id")
        dept_name = row.get("dept_name") or ""
        key = dept_id or dept_name or ""
        if not key:
            continue
        depts = grouped.setdefault(region, {})
        group = depts.get(key)
        if group is None:
            group = {"dept_id": dept_id, "dept_name": dept_name,
                     "sort_order": 0, "members": []}
            depts[key] = group
        group["sort_order"] = int(row.get("sort_order") or 0)
        if dept_name and not group["dept_name"]:
            group["dept_name"] = dept_name

    pool = grouped.pop(None, [])
    pool.sort(key=lambda item: (item["name"], item["user_id"]))

    known = list(_ORG_REGION_ORDER)
    catalog_order = sorted(
        (key for key in catalog_regions if key not in known),
        key=lambda key: (int(catalog_regions[key].get("sort_order") or 0), key),
    )
    catalog_extra = set(catalog_order)
    extra = sorted(
        key for key in grouped
        if key not in known and key not in catalog_extra
    )
    regions = []
    for key in known + catalog_order + extra:
        depts = grouped.get(key) or {}
        if key not in catalog_regions and not depts:
            continue
        groups = sorted(
            depts.values(),
            key=lambda g: (g["sort_order"], g["dept_name"],
                           g["dept_id"] or ""),
        )
        for group in groups:
            group["members"].sort(
                key=lambda item: (item["name"], item["user_id"]))
        regions.append({
            "key": key,
            "label": catalog_region_labels.get(key) or _org_region_label(key),
            "depts": groups,
        })
    return {"status": "ok", "regions": regions, "pool": pool}

#: 服务标识 → 中文名（策展映射，展示层；robot-/pages- 家族按区域动态生成）。
_SERVICE_NAMES = {
    "roll-manifest": "滚动清单",
    "sync-dingtalk": "钉钉 AI 表同步",
    "sync-wdt": "旺店通同步",
    "project-mart": "投影重建（修复工具）",
    "extract-mart": "提取加工",
    "sync-channel-sales": "渠道日销补采",
    "extract-channel": "渠道日销提取",
    "channel-missing-check": "渠道到齐校验催办",
    "channel-daily-qudao": "渠道日报播报",
    "pages-qudao-t1": "渠道榜单 T+1 重算",
    "offline-daily-summary": "线下每日汇总",
    "offline-weekly-summary": "线下每周汇总",
    "offline-monthly-summary": "线下每月汇总",
    "target-remind": "月目标核对提醒",
    "target-rollover": "月目标自动结转",
    "dingtalk-gateway": "钉钉网关",
    "kb-sync-products": "知识库商品同步",
    "kb-gateway": "知识库问答机器人",
    "fleet-audit": "舰队月检",
    "bi-web": "BI 看板",
    "scheduler": "调度器",
    "ops-web": "运维台",
}


def _service_name(service_id):
    """服务中文名：策展映射优先，robot-/pages-/leaderboard- 家族按区域生成。"""
    name = _SERVICE_NAMES.get(service_id)
    if name is not None:
        return name
    for prefix, tpl in (
        ("robot-check-", "「{region}」催办未填+DING"),
        ("robot-", "「{region}」填报提醒"),
        ("pages-", "「{region}」榜单页"),
        ("leaderboard-", "「{region}」榜单播报"),
    ):
        if service_id.startswith(prefix):
            region = service_id[len(prefix):]
            return tpl.format(region=_REGION_LABELS.get(region, region))
    return service_id


def _service_name_cell(service_id):
    """服务标识列：中文名为主、原始 id 小字备查；无中文名只显示 id。"""
    name = _service_name(service_id)
    if name == service_id:
        return _esc(service_id)
    return f"{_esc(name)}<span class=\"hint\">（{_esc(service_id)}）</span>"


def _service_label(service_id, description):
    """服务中文说明：策展映射优先，家族动态生成，兜底注册表原文。"""
    label = _SERVICE_LABELS.get(service_id)
    if label is not None:
        return label
    for prefix, tpl in (
        ("robot-check-", "「{region}」催办未填人 + DING（报数核对 → 群内 @ + DING）"),
        ("robot-", "「{region}」填报提醒（报数汇总 → 群内提醒）"),
        ("pages-", "「{region}」榜单页生成"),
        ("leaderboard-", "「{region}」榜单群播报（销售完成率榜 → 群）"),
    ):
        if service_id.startswith(prefix):
            region = service_id[len(prefix):]
            return tpl.format(region=_REGION_LABELS.get(region, region))
    return description


#: 功能分类已沉为注册表真源（PipelineConfig.category，七值与推导逻辑
#: 见 common.public_data.pipeline_taxonomy）；本页只保留展示层：
#: 真源优先、未配置的存量条目按 service_id 推导兜底。

#: 无 family 前缀但绑定区域的服务（「管理群」列用；pages-qudao-t1 的
#: pages- 后缀是 qudao-t1 不是区域，必须显式登记）。
_SERVICE_REGIONS = {
    "channel-daily-qudao": "qudao",
    "channel-missing-check": "qudao",
    "pages-qudao-t1": "qudao",
    "offline-daily-summary": "offline_all",
    "offline-weekly-summary": "offline_all",
    "offline-monthly-summary": "offline_all",
}

def _category_label(config):
    """功能列：注册表 category 真源优先，未配置按 service_id 推导兜底。"""
    return resolve_category(config.service_id, config.category) or "—"


def _service_region(service_id):
    """服务绑定区域：显式映射 → 家族后缀 → None（应用线/平台）。"""
    region = _SERVICE_REGIONS.get(service_id)
    if region is not None:
        return region
    for prefix, _ in CATEGORY_FAMILY:
        if service_id.startswith(prefix):
            return service_id[len(prefix):]
    return None



def _scope_backlinks(configs):
    """名册 scope → 被该范围名册影响的催办任务标识（P3，2026-10-12）。

    由 ``service_id → 区域`` 反查（复用 :func:`_service_region` 口径）：
    ``robot-<区域>`` / ``robot-check-<区域>`` → 区域；``channel-missing-check``
    → ``qudao``。只收「催办」类任务（与定时任务页「收受影响人」列同一取值域），
    无绑定区域的平台/应用线不计。返回 ``{scope: [service_id, ...]}``，各
    scope 内按标识排序。
    """
    by_scope: dict = {}
    for config in configs:
        service_id = config.service_id
        if resolve_category(service_id, config.category) != "催办":
            continue
        region = _service_region(service_id)
        if region is None:
            continue
        by_scope.setdefault(region, []).append(service_id)
    for ids in by_scope.values():
        ids.sort()
    return by_scope


#: 定时任务页「按管理群」视角的分组顺序（2026-10-12 运维裁决）：城市区域
#: 在前、跨区域线（渠道/线下整体）居中，无区域（平台/无群）恒置末位；
#: 未登记区域排在已知区域之后。分组键为区域 key（非中文 label）。
_GROUP_ORDER = tuple(_REGION_LABELS)


def _pipeline_group_title(region):
    """群视角分组头文案：区域 → 「<区域中文名>」群；None → 平台 / 无群。"""
    if region is None:
        return "平台 / 无群"
    return f"「{_REGION_LABELS.get(region, region)}」群"



def _names_by_dept_html(effective, display, dept_by_uid):
    """有效名单的人类展示：跨多部门时按「部门：姓名、…；部门：…」归组。

    单部门（或无部门）→ 维持原「、」连接（少噪声）；名称逐个 ``_esc``，
    避免整串二次转义。*effective* 为有效 user_id 序（去重保序）。
    """
    def _name_of(uid):
        return display.get(uid) or (uid[1:] if uid.startswith("?") else uid)

    dept_map = dept_by_uid or {}
    order, groups = [], {}
    for uid in effective:
        dept = dept_map.get(uid) or ""
        if dept not in groups:
            groups[dept] = []
            order.append(dept)
        groups[dept].append(_name_of(uid))
    if len(order) <= 1:
        return "、".join(_esc(n) for n in groups[order[0]]) if order else ""
    return "；".join(
        (f"{_esc(dept)}：" if dept else "未分部门：")
        + "、".join(_esc(n) for n in groups[dept])
        for dept in order
    )


def _targets_html(base, overrides, uid_to_name, scope_label=None,
                  store_count=None, dept_by_uid=None):
    """有效名单 + 口径徽标（2026-10-12 方案 B）。

    *base* 为基名单 ``[(user_id, name)]``，*overrides* 为该线启用覆盖行；
    有效名单 = ``base ∪ include − exclude``（与机器人侧同一
    :func:`recipient_override.apply_recipient_overrides`）。有覆盖时补
    ``（+N 覆盖／−M 移除）`` 徽标；口径徽标计数按有效名单。
    *dept_by_uid*（2026-10-13）为该区域有效成员的 ``{user_id: dept_name}``，
    跨多部门时按部门归组展示（与「组织编排」同构）。
    """
    effective = recipient_override.apply_recipient_overrides(base, overrides)
    display = dict(uid_to_name or {})
    for row in overrides:   # include 姓名型：写入快照即展示名
        if (str(row.get("mode") or "") == "include"
                and str(row.get("target_type") or "") == "name"
                and row.get("resolved_user_id")):
            display.setdefault(row["resolved_user_id"], row["target_value"])
    names = [
        display.get(uid) or (uid[1:] if uid.startswith("?") else uid)
        for uid in effective
    ]
    if not names:
        return "<span class=\"hint\">覆盖后无可@人</span>"
    base_ids = [uid for uid, _ in base]
    base_set, eff_set = set(base_ids), set(effective)
    added = [uid for uid in effective if uid not in base_set]
    removed = [uid for uid in base_ids if uid not in eff_set]
    meta = f"{len(names)} 人"
    meta += f" · {store_count} 店" if store_count is not None \
        else f" · {scope_label}"
    badge = ""
    if added or removed:
        bits = []
        if added:
            bits.append(f"+{len(added)} 覆盖")
        if removed:
            bits.append(f"−{len(removed)} 移除")
        badge = f"<span class=\"hint\">（{'／'.join(bits)}）</span>"
    return (_names_by_dept_html(effective, display, dept_by_uid)
            + f"<span class=\"hint\">（{meta}）</span>{badge}")


def _recipient_entry_html(service_id):
    """「收件人」入口：点开该线的任务级覆盖抽屉（方案 B，2026-10-12）。

    service_id 受注册表约束（字母数字/._:-）；此处再 ``_esc`` 兜底防注入。
    """
    return (f' <a href="#" class="recipient-entry" title="任务级收件人覆盖" '
            f"onclick=\"openRecipientOverride('{_esc(service_id)}');"
            f'return false">收件人…</a>')


def _effective_members_by_region(members, overrides):
    """在职成员 ∘ 组织归属覆盖 → ``{region: [(uid, name, dept_name)]}``。

    与「组织编排」页 :func:`_build_org_overview` 同一口径（2026-10-13 P6
    联动）：覆盖优先、其次种子派生；覆盖 ``region`` 为空（移出全部区域 /
    未归类）不进任何区域视图。*members* 为
    :func:`bi_authz.fetch_active_members` 的 ``(uid, name, region, dept_name)``；
    *overrides* 为 :func:`org_assignment_override.fetch_overrides` 的
    ``{user_id: row}``。各组按 ``(name, uid)`` 排序。
    """
    overrides = overrides or {}
    buckets = {}
    for uid, name, region, dept_name in members:
        override = overrides.get(uid)
        if override is not None:
            region = override["region"]
            dept_name = override["dept_name"] or dept_name
        if not region:
            continue
        buckets.setdefault(region, []).append((uid, name, dept_name or ""))
    for rows in buckets.values():
        rows.sort(key=lambda item: (item[1], item[0]))
    return buckets


def _recipient_base(service_id, *, members_by_region, roster_by_region,
                    qudao_owners, uid_to_name=None):
    """基名单口径（不含任务级覆盖）——展示列与抽屉 API 共用同一来源。

    与机器人侧一致（2026-10-07 裁决「催办绑定名册」）：

    * ``robot-<区域>``（填报提醒）/ ``robot-check-<区域>``（催办+DING）：
      该区域**有效**成员（在职 ∘ 组织归属覆盖）∩ 名册启用在册人员
      （``kind='roster'``）；名册无启用记录（fail-open）→ 区域有效成员
      全员（``kind='fallback'``），与机器人 ``_unfilled`` 同真源；
    * 渠道线催办（``channel-missing-check``）：名册 ``qudao/store/启用/
      role=owner`` 去重后的人名（``kind='qudao'``）；名册未登记时机器人回退
      月目标表 owners_json，此处如实标注不复制回退链；
    * 其余服务不可计算（``kind='none'``）。

    *members_by_region* 为 :func:`_effective_members_by_region` 的
    ``{region: [(uid, name, dept_name)]}``（2026-10-13 三元组）。返回
    ``{"kind", "pairs": [(user_id, name)], "region", "store_count",
    "dept_by_uid"}``；*pairs* 不含覆盖，有效名单由调用方再叠加
    :func:`recipient_override.apply_recipient_overrides`；*dept_by_uid* 供
    展示层按部门归组。
    """
    if service_id.startswith("robot-"):
        # _service_region 兼容 robot-check- 长者前缀（2026-10-07 拆分）。
        region = _service_region(service_id)
        rows = members_by_region.get(region) or []
        pairs = [(uid, name) for uid, name, _dept in rows]
        dept_by_uid = {uid: dept for uid, _name, dept in rows if dept}
        roster = roster_by_region.get(region)
        if roster is not None:
            base = [(uid, name) for uid, name in pairs if name in roster]
            return {"kind": "roster", "pairs": base, "region": region,
                    "store_count": None, "dept_by_uid": dept_by_uid}
        return {"kind": "fallback", "pairs": list(pairs), "region": region,
                "store_count": None, "dept_by_uid": dept_by_uid}
    if _service_region(service_id) == "qudao":
        owners = qudao_owners or {}
        names = sorted({name for bucket in owners.values() for name in bucket})
        name_to_uid = {}
        for uid, name in (uid_to_name or {}).items():
            name_to_uid.setdefault(name, uid)
        base = [(name_to_uid.get(name) or ("?" + name), name)
                for name in names]
        return {"kind": "qudao", "pairs": base, "region": None,
                "store_count": len(owners), "dept_by_uid": {}}
    return {"kind": "none", "pairs": [], "region": None, "store_count": None,
            "dept_by_uid": {}}


def _base_source_label(info):
    """基名单来源的人话（抽屉 API 的 ``base_source``）。"""
    region = info.get("region")
    if info.get("kind") == "roster":
        return f"名册（{region}）∩ 在职成员"
    if info.get("kind") == "fallback":
        return f"{region} 在职填报人（该区域名册未启用，全员兜底）"
    if info.get("kind") == "qudao":
        return "店铺负责人（名册 qudao·owner）"
    return ""


def _recipient_explain(service_id, *, members_by_region, roster_by_region,
                       qudao_owners, uid_to_name=None, overrides=(),
                       base_pairs=()):
    """「为什么不在名单」归因（P2，2026-10-12；纯展示口径，不新增写面）。

    对每条服务线给出「相关但未生效」的人，按原因分组、每组一句人话，空组
    不出现。取值域（对齐设计稿 §6 P2）：

    * ``roster_only``：在名册、通讯录在职成员表无 → 查通讯录在职状态；
    * ``member_only``：在职、不在名册 → 去「填报名册」登记；
    * ``excluded``：在基名单、被本任务 exclude 移除 → 被本任务移除。

    仅 ``robot-*`` 有「名册 ∩ 在职」语义，故前两组只对区域线产出（fail-open
    无名册时也不产出）；渠道线基名单直接来自名册 owner、无区域在职交集，
    只产出 ``excluded``。返回 ``[{"key", "reason", "items": [...]}, ...]``。
    """
    groups = []
    if service_id.startswith("robot-"):
        region = _service_region(service_id)
        members = members_by_region.get(region) or []
        member_names = {name for _uid, name, _dept in members}
        uid_of_name = {}
        for uid, name, _dept in members:
            uid_of_name.setdefault(name, uid)
        roster = roster_by_region.get(region)
        if roster is not None:
            roster_only = sorted(n for n in roster if n not in member_names)
            member_only = sorted(n for n in member_names if n not in roster)
            if roster_only:
                groups.append({
                    "key": "roster_only",
                    "reason": "名册有、通讯录在职成员表无：请到「成员与授权」"
                              "核查通讯录在职状态",
                    "items": [{"user_id": "", "name": n}
                              for n in roster_only],
                })
            if member_only:
                groups.append({
                    "key": "member_only",
                    "reason": "在职但不在名册：请到「填报名册」登记，"
                              "未登记不会被催办",
                    "items": [{"user_id": uid_of_name.get(n, ""), "name": n}
                              for n in member_only],
                })
    # 被本任务 exclude 从基名单移除的人（区域线/渠道线通用）。
    base_pairs = list(base_pairs or [])
    if base_pairs:
        effective = recipient_override.apply_recipient_overrides(
            base_pairs, overrides)
        eff_set = set(effective)
        display = dict(uid_to_name or {})
        for uid, name in base_pairs:
            display.setdefault(uid, name)
        removed = [(uid, display.get(uid) or uid)
                   for uid, _name in base_pairs if uid not in eff_set]
        if removed:
            groups.append({
                "key": "excluded",
                "reason": "在基名单、被本任务移除（移除类覆盖）",
                "items": [{"user_id": uid, "name": name}
                          for uid, name in removed],
            })
    return groups


def _load_recipient_context(connection, service_id):
    """装载该线 base/explain 所需的只读上下文（两口径共用，单次装载）。

    返回 ``{"members_by_region", "roster_by_region", "qudao_owners",
    "uid_to_name"}``；不可计算的线（应用/平台）返回全空。区域线的
    *members_by_region* 为**有效**成员（在职 ∘ 组织归属覆盖，三元组
    ``(uid, name, dept_name)``，2026-10-13 联动组织编排）；覆盖表读失败
    fail-soft 回退种子口径。
    """
    members_by_region, roster_by_region = {}, {}
    qudao_owners, uid_to_name = None, {}
    if service_id.startswith("robot-"):
        region = _service_region(service_id)
        members = bi_authz.fetch_active_members(connection)
        try:
            overrides = org_assignment_override.fetch_overrides(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org assignment override load failed: %s",
                type(exc).__name__)
            overrides = {}
        members_by_region = _effective_members_by_region(members, overrides)
        for uid, name, _reg, _dept in members:
            uid_to_name.setdefault(uid, name)
        roster_by_region[region] = report_roster.fetch_scope_person_names(
            connection, region)
    elif _service_region(service_id) == "qudao":
        qudao_owners = report_roster.fetch_store_owner_map(
            connection, "qudao", roles=("owner",))
    return {
        "members_by_region": members_by_region,
        "roster_by_region": roster_by_region,
        "qudao_owners": qudao_owners,
        "uid_to_name": uid_to_name,
    }


def _reminder_targets_cell(service_id, *, members_by_region, roster_by_region,
                           qudao_owners, overrides=(), uid_to_name=None):
    """收受影响人列（仅催办行调用；其余行由调用方给 —）。

    基名单由 :func:`_recipient_base` 计算，列与抽屉 API 同一口径不漂移。
    2026-10-12 方案 B：叠加任务级收件人覆盖（include/exclude），只改 @ 名单；
    展示与机器人在触发时按同一 merge 口径，不承诺与某次运行逐位一致。
    """
    entry = _recipient_entry_html(service_id)
    info = _recipient_base(
        service_id, members_by_region=members_by_region,
        roster_by_region=roster_by_region, qudao_owners=qudao_owners,
        uid_to_name=uid_to_name)
    if info["kind"] == "none":
        return "<span class=\"hint\">—</span>"
    return _effective_names_html(info, overrides, uid_to_name) + entry


def _effective_names_html(info, overrides, uid_to_name):
    """有效 @ 名单单元格（``base ∪ include − exclude``），不含覆盖抽屉入口。

    *info* 为 :func:`_recipient_base` 结果。列与抽屉 API 同一口径；收件人总览
    页（只读）复用此渲染，``/pipelines`` 列再由 :func:`_reminder_targets_cell`
    追加抽屉入口。``none`` 口径返回 ``—``。
    """
    kind, base = info["kind"], info["pairs"]
    dept_by_uid = info.get("dept_by_uid")
    if kind == "roster":
        if not base:
            return "<span class=\"hint\">名册在册但通讯录无匹配成员</span>"
        return _targets_html(base, overrides, uid_to_name, scope_label="名册",
                             dept_by_uid=dept_by_uid)
    if kind == "fallback":
        if not base:
            return "<span class=\"hint\">该区域无在册填报人</span>"
        return _targets_html(base, overrides, uid_to_name, scope_label="全员兜底",
                             dept_by_uid=dept_by_uid)
    if kind == "qudao":
        if not base:
            return ("<span class=\"hint\">名册未登记店铺负责人"
                    "（机器人自动回退读门店月目标表）</span>")
        return _targets_html(base, overrides, uid_to_name,
                             store_count=info["store_count"],
                             dept_by_uid=dept_by_uid)
    return "<span class=\"hint\">—</span>"


def _recipients_scope_html(scope, task_ids, *, members_by_region,
                           roster_by_region, qudao_owners, uid_to_name,
                           overrides_by_service):
    """收件人总览页单个 scope 区块（只读，P4，2026-10-12）。

    先给「在职 ∩ 名册」基名单（同 :func:`_recipient_base` 口径，来源标注复用
    :func:`_base_source_label`），再逐条催办任务列出叠加任务级覆盖后的有效 @
    名单（``base ∪ include − exclude``，与 :func:`_effective_names_html` 同源）。
    本页只读、无写面：覆盖调整入口深链到定时任务页对应行抽屉
    （``/pipelines?service=<id>``）。范围无绑定催办任务时给一句人话。
    """
    scope_label = _label(_SCOPE_LABEL, scope)
    rep_service = ("channel-missing-check" if scope == "qudao"
                   else f"robot-{scope}")
    info = _recipient_base(
        rep_service, members_by_region=members_by_region,
        roster_by_region=roster_by_region, qudao_owners=qudao_owners,
        uid_to_name=uid_to_name)
    parts = [f"<h2>{_esc(scope_label)}"
             f"<span class=\"hint\">（{len(task_ids)} 条催办任务）</span></h2>"]
    base_names = "、".join(name for _uid, name in info["pairs"])
    base_cell = (
        f"{_esc(base_names)}<span class=\"hint\">"
        f"（{len(info['pairs'])} 人）</span>"
        if base_names else "<span class=\"hint\">（空）</span>"
    )
    parts.append(
        "<p>基名单（在职 ∩ 名册；展示口径，非某次运行快照）"
        f"<span class=\"hint\">{_esc(_base_source_label(info))}</span>："
        f"{base_cell}"
        "　<a href=\"/roster\">改本范围名册</a>"
        "　<a href=\"/member-grants\">查成员在职</a></p>"
    )
    if not task_ids:
        parts.append("<p class=\"hint\">本范围暂无绑定的催办任务。</p>")
        return "".join(parts)
    rows = []
    for service_id in task_ids:
        effective = _effective_names_html(
            info, overrides_by_service.get(service_id, ()), uid_to_name)
        rows.append(
            f"<tr><td>{_service_name_cell(service_id)}</td>"
            f"<td>{effective}</td>"
            f"<td><a href=\"/pipelines?service={_esc(service_id)}\">去调整</a>"
            "</td></tr>"
        )
    parts.append(
        "<table><tr><th>催办任务</th>"
        "<th>有效 @ 名单（基名单 ∪ 覆盖）</th><th>调整</th></tr>"
        + "".join(rows) + "</table>"
    )
    return "".join(parts)


def _cron_zh(schedule):
    """常见 cron 的人性化中文（仅展示；不认识的形态返回 None → 原文显示）。

    支持 ``;`` 分隔的**多段**（播报时间多时点，2026-10-13）：逐段中文后以
    「；」连接；任一段不可识别则整条回退原文。单段即退化形态。
    """
    if not schedule:
        return None
    segments = [seg.strip() for seg in schedule.split(";") if seg.strip()]
    if not segments:
        return None
    parts = []
    for segment in segments:
        zh = _cron_zh_single(segment)
        if zh is None:
            return None
        parts.append(zh)
    return "；".join(parts)


def _cron_zh_single(schedule):
    """单段 5 段 cron 的中文（:func:`_cron_zh` 的段实现）。

    覆盖注册表全部形态：每天（多）时点、每周 X、每月 D 日、每 N 分钟、
    每小时第 M 分；其余（含月份字段）一律回退原文。
    """
    parts = schedule.split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    if month != "*":
        return None

    def _times():
        if not minute.isdigit():
            return None
        hours = hour.split(",")
        if not all(h.isdigit() for h in hours):
            return None
        return "、".join(f"{int(h):02d}:{int(minute):02d}" for h in hours)

    if dom == "*" and dow == "*":
        if minute.startswith("*/") and hour == "*" and minute[2:].isdigit():
            return f"每 {int(minute[2:])} 分钟"
        if hour == "*" and minute.isdigit():
            return f"每小时第 {int(minute)} 分"
        times = _times()
        return f"每天 {times}" if times else None
    if dom == "*" and dow != "*":
        times = _times()
        # 工作日区间（定时构建器「工作日」预设与存量注册表常见形态）
        if dow == "1-5" and times:
            return f"工作日（周一至周五）{times}"
        days = dow.split(",")
        if times and all(d in _CRON_DOW for d in days):
            return "每周" + "、周".join(_CRON_DOW[d] for d in days) + f" {times}"
        return None
    if dom != "*" and dow == "*" and dom.isdigit():
        times = _times()
        return f"每月 {int(dom)} 日 {times}" if times else None
    return None


def _schedule_cell(schedule):
    """定时规则列：中文人性化为主、原表达式作小字备查；无定时=常驻。"""
    if not schedule:
        return "<span class=\"hint\">常驻</span>"
    zh = _cron_zh(schedule)
    if zh is None:
        return _esc(schedule)
    return f"{_esc(zh)}<span class=\"hint\">（{_esc(schedule)}）</span>"


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------


def create_app(*, settings, session_secret, db_connector=None, auth_client=None,
               viewer_resolver=None, session_secure=False,
               browser_login_token=None,
               fleet_source=None, config_source=None,
               config_publisher=None, config_deleter=None,
               corp_id=None, agent_id=None) -> FastAPI:
    """装配 ops-web；``session_secret`` 必填（ops-web 没有开放模式）。

    依赖全部可注入，测试不需要真实库与网络：``db_connector`` 喂
    FakeConnection，``viewer_resolver`` 喂静态 Viewer，``auth_client``
    喂 fake 交换客户端。定时任务管理面另有三注入：``fleet_source``
    （零参 callable → service_id 元组）、``config_source``（注册表读）、
    ``config_publisher``（注册表写，None = 写 API 降级 503）。
    ``browser_login_token`` 非空时开放浏览器登录通道（2026-10-14，
    为无钉钉环境的运维/验收提供入口），缺省关闭。
    """
    if not session_secret:
        raise ValueError("ops-web requires a session secret")
    if db_connector is None:
        db_connector = _mart_connector(settings)
    if viewer_resolver is None:
        viewer_resolver = authz.ViewerResolver(db_connector)

    app = FastAPI(title="ops-web")

    # 组织编排 SPA 静态托管（Vite 产物提交入库；目录不存在则跳过挂载，
    # 避免无 dist 的旧检出直接启动失败）。
    if os.path.isdir(_ORG_SPA_DIR):
        app.mount("/static", StaticFiles(directory=_ORG_SPA_DIR), name="static")

    def _resolve_request_viewer(request: Request) -> authz.Viewer:
        """session → Viewer（各闸共用的解析段；未登录 401/302，解析异常 403）。"""
        raw = request.cookies.get(auth.SESSION_COOKIE)
        userid = (
            auth.resolve_session_userid(raw, session_secret) if raw else None
        )
        if userid is None:
            accept = request.headers.get("accept", "")
            if "application/json" in accept or request.url.path.startswith("/api/"):
                raise HTTPException(status_code=401, detail=ErrorDetail.UNAUTHORIZED)
            raise HTTPException(
                status_code=302, headers={"Location": "/auth/entry?reason=login"}
            )
        try:
            return viewer_resolver.resolve(userid)
        except Exception as exc:
            # 权限链 fail-closed（铁律 2）：解析异常 = 拒绝。
            _LOGGER.warning("ops-web viewer resolve failed: %s", type(exc).__name__)
            raise HTTPException(status_code=403, detail=ErrorDetail.FORBIDDEN)

    def _forbidden(request: Request):
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=403, detail=ErrorDetail.FORBIDDEN)
        raise HTTPException(
            status_code=302,
            headers={"Location": "/auth/entry?reason=forbidden"},
        )

    def require_admin(request: Request) -> authz.Viewer:
        """应用层闸：session 有效 + admin；其余一律提示页/403。"""
        viewer = _resolve_request_viewer(request)
        if not viewer.is_admin:
            _forbidden(request)
        request.state.viewer = viewer
        return viewer

    def require_capability(request: Request, capability, region=None
                           ) -> authz.Viewer:
        """能力闸（2026-10-14 D4 能力×范围）：admin 或持 ``<capability>`` /
        ``<capability>:<region>`` 授权；其余提示页 / 403。"""
        viewer = _resolve_request_viewer(request)
        if not capability_allows(viewer, capability, region):
            _forbidden(request)
        request.state.viewer = viewer
        return viewer

    # -- 认证路由（与 bi-web 同一套实现，铁律 7）----------------------------
    @app.post("/auth/dingtalk")
    async def auth_dingtalk(request: Request):
        if auth_client is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            payload = await request.json()
        except Exception:
            payload = None
        auth_code = payload.get("authCode") if isinstance(payload, dict) else None
        if not isinstance(auth_code, str) or not auth_code:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            userid = auth_client.exchange_auth_code(auth_code)
        except auth.AuthError as exc:
            _LOGGER.warning("ops-web auth exchange failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        _LOGGER.info("ops-web login: %s", auth.mask_userid(userid))
        response = JSONResponse({"status": "ok"})
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.issue_session(userid, session_secret),
            max_age=auth.SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=session_secure,
        )
        return response

    @app.get("/auth/entry")
    def auth_entry():
        return FileResponse(auth.AUTH_ENTRY_PAGE)

    @app.get("/auth/login-options")
    def auth_login_options():
        """入口页据此决定是否渲染「浏览器登录」表单（2026-10-14）。

        bi-web 无本路由 → 404 → 表单保持隐藏，共享入口页对 bi-web 零变化。
        """
        return JSONResponse(
            {
                "dingtalk": auth_client is not None,
                "browser": bool(browser_login_token),
            }
        )

    @app.post("/auth/browser-login")
    async def auth_browser_login(request: Request):
        """浏览器登录（2026-10-14）：运维口令 + 钉钉账号 → 同一枚 session。

        未配口令 = 通道未开放（503）；口令或账号任一不合规一律 401（不区分
        分支，防探测）。**签发会话 ≠ 获得权限**：账号可见性仍由
        ``ViewerResolver`` 逐请求对 ``bi_authz`` 校验（fail-closed）。
        """
        if not browser_login_token:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            payload = await request.json()
        except Exception:
            payload = None
        user_id = payload.get("user_id") if isinstance(payload, dict) else None
        token = payload.get("token") if isinstance(payload, dict) else None
        if (
            not isinstance(user_id, str)
            or not user_id.strip()
            or not isinstance(token, str)
            or not token
        ):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not hmac.compare_digest(token, browser_login_token):
            _LOGGER.warning("ops-web browser login rejected")
            raise HTTPException(status_code=401, detail=ErrorDetail.UNAUTHORIZED)
        user_id = user_id.strip()
        _LOGGER.info("ops-web browser login: %s", auth.mask_userid(user_id))
        response = JSONResponse({"status": "ok"})
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.issue_session(user_id, session_secret),
            max_age=auth.SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=session_secure,
        )
        return response

    @app.get("/auth/logout")
    def auth_logout():
        response = RedirectResponse("/auth/entry?reason=loggedout")
        response.delete_cookie(auth.SESSION_COOKIE)
        return response

    @app.get("/auth/jsapi-config")
    def auth_jsapi_config(url: str = ""):
        # dd.config 签名原料（与 bi-web 同款，设计稿 §5.3）：agentId/corpId +
        # 随机 nonceStr/timeStamp + 对当前页 URL 的 SHA1 签名。与 /auth/dingtalk
        # 同属认证路由；url 仅参与签名、不回显，scheme 非 http/https 直接 400。
        if auth_client is None or not corp_id or not agent_id:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        nonce = auth.make_nonce()
        timestamp = int(time.time() * 1000)
        try:
            ticket = auth_client.jsapi_ticket()
        except auth.AuthError as exc:
            _LOGGER.warning(
                "ops-web jsapi ticket fetch failed: %s", type(exc).__name__
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse(
            {
                "agentId": agent_id,
                "corpId": corp_id,
                "timeStamp": timestamp,
                "nonceStr": nonce,
                "signature": auth.build_jsapi_signature(
                    ticket, nonce, timestamp, url
                ),
            }
        )

    @app.get("/healthz")
    def healthz():
        # 顶层运维端点（与 bi-web 同款）：活性与 mart 连通性（SELECT 1）。
        try:
            with db_connector() as connection:
                cursor = connection.cursor()
                try:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                finally:
                    cursor.close()
        except Exception as exc:
            _LOGGER.warning(
                "ops-web healthz mart probe failed: %s", type(exc).__name__
            )
            return JSONResponse({"status": "unhealthy"}, status_code=503)
        return JSONResponse({"status": "ok", "database": "ok"})

    # -- 能力×范围自述（SPA 启动拉取：驱动路由守卫与菜单渲染）---------------
    @app.get("/api/me")
    def api_me(request: Request):
        """当前会话的能力×范围视图（2026-10-14 D4；SPA 启动时调用）。

        返回：全局能力列表 *capabilities*、区域级能力映射
        *scoped_capabilities*、可操作区域 *regions*、全量区域 *all_regions*。
        未准入 → 403（deny-by-default）。
        """
        viewer = _resolve_request_viewer(request)
        if not viewer.admitted:
            _forbidden(request)
        request.state.viewer = viewer
        try:
            with db_connector() as connection:
                registry = [key for key, _ in _region_scope_pairs(connection)]
        except Exception as exc:
            _LOGGER.warning("ops-web me region read failed: %s", type(exc).__name__)
            registry = list(report_roster.SCOPES)
        capabilities = []
        scoped = {}
        for capability in ALL_CAPABILITIES:
            if capability_allows(viewer, capability):
                capabilities.append(capability)
                continue
            keys = scoped_region_keys(viewer, capability)
            if keys:
                scoped[capability] = keys
        return JSONResponse({
            "status": "ok",
            "userid": viewer.userid,
            "name": viewer.name,
            "is_admin": viewer.is_admin,
            "capabilities": capabilities,
            "scoped_capabilities": scoped,
            "regions": visible_region_scopes(viewer, registry),
            "all_regions": registry,
        })

    # -- 页面 ----------------------------------------------------------------
    # 「成员与授权」页已停用（2026-10-13）：在职成员展示与归属管理迁至
    # 「组织编排」SPA（/members）。路由保留防书签 404，页面引导跳转。
    @app.get("/member-grants")
    def members_page():
        """旧书签兼容：与 /grants 同壳（Router 渲染只读授权页）。"""
        return _spa_shell()

    # -- 全 SPA 单壳（Vue3 + Router；2026-10-14 D1 全量迁移）------------------
    def _spa_shell():
        """SPA 单壳：返回 Vite 产物 index.html。

        不设应用层能力闸：未登录 / 无权限由前端启动 ``/api/me`` → 401/403
        → 跳登录；数据 API 仍 fail-closed（deny-by-default）。产物缺失 → 503。
        """
        if not os.path.isfile(_ORG_SPA_INDEX):
            _LOGGER.warning("ops-web SPA build missing: %s", _ORG_SPA_INDEX)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return FileResponse(
            _ORG_SPA_INDEX, media_type="text/html",
            headers={"Cache-Control": "no-cache"})

    @app.get("/")
    def spa_home_page():
        """登录后首页 = SPA（Router 渲染总览）。"""
        return _spa_shell()

    @app.get("/org")
    def spa_org_page():
        """组织编排页壳（Router 渲染）。"""
        return _spa_shell()

    @app.get("/members")
    def members_spa_page():
        """旧书签兼容：与 /、/org 同壳。"""
        return _spa_shell()

    @app.get("/api/org/overview")
    def org_overview(request: Request):
        """组织树视图：在职成员 ∘ 覆盖 → regions / pool + 覆盖行 + 审计。

        成员读故障 = 503（组织树无从展示）；覆盖读故障 = fail-soft
        （只丢覆盖，回种子口径，不拖垮整页）。
        """
        viewer = _resolve_request_viewer(request)
        if not (capability_allows(viewer, CAPABILITY_ORG)
                or capability_has_scoped(viewer, CAPABILITY_ORG)):
            _forbidden(request)
        request.state.viewer = viewer
        try:
            with db_connector() as connection:
                members = bi_authz.fetch_active_members(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org members load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        rows = []
        try:
            with db_connector() as connection:
                rows = org_assignment_override.fetch_all_overrides(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org override read failed: %s", type(exc).__name__)
        overrides = {row["user_id"]: row for row in rows if row["enabled"]}
        try:
            with db_connector() as connection:
                catalog = org_catalog.fetch_catalog(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org catalog read failed: %s", type(exc).__name__)
            catalog = {"regions": [], "depts": []}
        overview = _build_org_overview(members, overrides, catalog)
        if not capability_allows(viewer, CAPABILITY_ORG):
            # 区域级 org 授权（2026-10-15 闭环修复）：只回授权区域，且不带
            # 「未归类」池与跨区覆盖行——pool 成员无区域可归因，scoped 视角
            # 无权看；否则 org:<r> 授权就等于全量可见，等于没授权。
            allowed = set(scoped_region_keys(viewer, CAPABILITY_ORG))
            overview["regions"] = [
                region for region in overview["regions"]
                if region["key"] in allowed
            ]
            overview["pool"] = []
            rows = [row for row in rows if row.get("region") in allowed]
        overview["overrides"] = [
            dict(row, updated_at=_fmt_time(row.get("updated_at")),
                 region_label=_org_region_label(row.get("region")))
            for row in rows
        ]
        return JSONResponse(overview)

    @app.post("/api/org/assign")
    async def org_assign(request: Request):
        """新增/更新一条归属覆盖（``region`` 为 null = 移出全部区域）。"""
        viewer = _resolve_request_viewer(request)
        request.state.viewer = viewer
        payload = await _json_body(request)
        user_id = payload.get("user_id")
        region = payload.get("region")
        dept_id = payload.get("dept_id")
        dept_name = payload.get("dept_name")
        name = payload.get("name")
        note = payload.get("note") or ""
        if not isinstance(user_id, str) or not user_id.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if region is not None and not isinstance(region, str):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        for value in (dept_id, dept_name, name, note):
            if value is not None and not isinstance(value, str):
                raise HTTPException(
                    status_code=400, detail=ErrorDetail.BAD_REQUEST)
        # 区域级 org 授权（2026-10-15 闭环修复）：``region=None``（移出全部
        # 区域）是跨区动作，只认全局 org；``region=<r>`` 则全局 org 或
        # ``org:<r>`` 均可——与 /api/org/move 的区域校验同一口径。
        if not capability_allows(viewer, CAPABILITY_ORG, region):
            _forbidden(request)
        try:
            with db_connector() as connection:
                org_assignment_override.upsert_assignment(
                    connection, user_id, region, dept_id=dept_id,
                    dept_name=dept_name, name=name, actor=viewer.userid,
                    note=note)
        except org_assignment_override.OrgAssignmentOverrideError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org assign failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/move")
    async def org_move(request: Request):
        """归属搬迁（**原子复合写**，2026-10-15 闭环画布）。

        一次事务内完成：覆盖 upsert（权威表 ``dim_org_assignment_override``）
        + 双侧 ``person`` 型名册同步 + 审计，避免**半移动态**——名册是
        fail-open 白名单，若人已进新区而旧区名册残留 / 新区无名册，会静默
        剥夺或搞错该成员**报数资格**与**当月目标归属**。

        ``to_region=null`` → 移入未归类池。**仅在目标区已存在启用 person
        名册体系**（fail-open 白名单已生效）且该人不在册时才补录；否则不
        动——避免给原本无名册的区凭空造白名单、把 fail-open 区整体翻成
        强制在册。
        """
        from common.public_data.db import transaction

        viewer = _resolve_request_viewer(request)
        if not (capability_allows(viewer, CAPABILITY_ORG)
                or capability_has_scoped(viewer, CAPABILITY_ORG)):
            _forbidden(request)
        request.state.viewer = viewer
        payload = await _json_body(request)
        user_id = payload.get("user_id")
        to_region = payload.get("to_region")
        person_name = payload.get("person_name")
        dept_name = payload.get("dept_name")
        from_region = payload.get("from_region")
        role = payload.get("role") or "owner"
        note = payload.get("note") or ""
        if not isinstance(user_id, str) or not user_id.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if to_region is not None and (
                not isinstance(to_region, str) or not to_region.strip()):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        for value in (person_name, dept_name, from_region, note):
            if value is not None and not isinstance(value, str):
                raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if from_region is not None and not from_region.strip():
            from_region = None
        if to_region is not None and not capability_allows(
                viewer, CAPABILITY_ORG, to_region):
            _forbidden(request)
        # 区域真源校验：目标区须在注册表已登记（注册表空 → fail-soft）。
        try:
            with db_connector() as connection:
                registered = org_catalog.fetch_region_keys(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org move region read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if to_region is not None and registered and to_region not in registered:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)

        from_removed = False
        to_added = False
        try:
            with db_connector() as connection:
                with transaction(connection):
                    org_assignment_override.apply_assignment(
                        connection, user_id, to_region,
                        dept_id=None, dept_name=dept_name, name=person_name,
                        actor=viewer.userid, note=note)
                    # 旧区：删该人启用中的 person 型名册（仅在册才动）。
                    if from_region and from_region != to_region and person_name:
                        hit = _find_person_roster_entry(
                            connection, from_region, person_name)
                        if hit is not None:
                            report_roster.apply_roster_delete(
                                connection, hit["id"], actor=viewer.userid)
                            from_removed = True
                    # 新区：仅当该区已启用 person 名册体系且该人不在册才补录。
                    if to_region and to_region != from_region and person_name:
                        existing = _find_person_roster_entry(
                            connection, to_region, person_name)
                        if existing is None and (
                                report_roster.fetch_scope_person_names(
                                    connection, to_region) is not None):
                            entry = report_roster.validate_roster_fields(
                                to_region, "person",
                                dept_name or _org_region_label(to_region),
                                person_name, role=role, note=note)
                            report_roster.apply_roster_entry(
                                connection, entry, actor=viewer.userid)
                            to_added = True
        except (report_roster.ReportRosterError,
                org_assignment_override.OrgAssignmentOverrideError):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning("ops-web org move failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({
            "status": "ok", "moved": True,
            "roster": {"from_removed": from_removed, "to_added": to_added},
            "audit": True,
        })

    @app.post("/api/org/region/add")
    async def org_region_add(request: Request):
        """登记一个区域（消费侧组织目录；即使无成员也进树）。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        region_key = payload.get("region_key")
        label = payload.get("label")
        sort_order = payload.get("sort_order")
        if not isinstance(region_key, str) or not region_key.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(label, str) or not label.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if sort_order is not None and (
                isinstance(sort_order, bool) or not isinstance(sort_order, int)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                org_catalog.upsert_region(
                    connection, region_key, label,
                    sort_order=sort_order or 0, actor=viewer.userid)
        except org_catalog.OrgCatalogError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org region add failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/dept/add")
    async def org_dept_add(request: Request):
        """登记一个区域下的部门（消费侧组织目录；即使无成员也进树）。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        region_key = payload.get("region_key")
        dept_name = payload.get("dept_name")
        dept_id = payload.get("dept_id")
        sort_order = payload.get("sort_order")
        if not isinstance(region_key, str) or not region_key.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(dept_name, str) or not dept_name.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if dept_id is not None and not isinstance(dept_id, str):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if sort_order is not None and (
                isinstance(sort_order, bool) or not isinstance(sort_order, int)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                org_catalog.upsert_dept(
                    connection, region_key, dept_name, dept_id=dept_id,
                    sort_order=sort_order or 0, actor=viewer.userid)
        except org_catalog.OrgCatalogError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org dept add failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/region/disable")
    async def org_region_disable(request: Request):
        """停用一个登记区域（软删：enabled=0）。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        region_key = payload.get("region_key")
        enabled = payload.get("enabled", False)
        if not isinstance(region_key, str) or not region_key.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                org_catalog.set_catalog_enabled(
                    connection, "region", region_key=region_key,
                    enabled=enabled, actor=viewer.userid)
        except org_catalog.OrgCatalogError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org region disable failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/region/delete")
    async def org_region_delete(request: Request):
        """物理删除一个登记区域（级联删除其下部门）。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        region_key = payload.get("region_key")
        if not isinstance(region_key, str) or not region_key.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                org_catalog.delete_catalog_entry(
                    connection, "region", region_key=region_key,
                    actor=viewer.userid)
        except org_catalog.OrgCatalogError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org region delete failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/dept/disable")
    async def org_dept_disable(request: Request):
        """停用一个登记部门（软删：enabled=0）。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        region_key = payload.get("region_key")
        dept_name = payload.get("dept_name")
        enabled = payload.get("enabled", False)
        if not isinstance(region_key, str) or not region_key.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(dept_name, str) or not dept_name.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                org_catalog.set_catalog_enabled(
                    connection, "dept", region_key=region_key,
                    dept_name=dept_name, enabled=enabled, actor=viewer.userid)
        except org_catalog.OrgCatalogError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org dept disable failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/dept/delete")
    async def org_dept_delete(request: Request):
        """物理删除一个登记部门。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        payload = await _json_body(request)
        region_key = payload.get("region_key")
        dept_name = payload.get("dept_name")
        if not isinstance(region_key, str) or not region_key.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(dept_name, str) or not dept_name.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                org_catalog.delete_catalog_entry(
                    connection, "dept", region_key=region_key,
                    dept_name=dept_name, actor=viewer.userid)
        except org_catalog.OrgCatalogError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org dept delete failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    def _org_override_region(user_id):
        """被操作覆盖行所属区域（无该行 → None）；读故障 503（收敛语义）。

        区域级 org 授权（2026-10-15 闭环修复）用：toggle/delete 只带 user_id，
        必须回读该行区域才能判定是否落在授权范围内。
        """
        try:
            with db_connector() as connection:
                rows = org_assignment_override.fetch_all_overrides(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org override read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        for row in rows:
            if row.get("user_id") == user_id:
                return row.get("region")
        return None

    @app.post("/api/org/toggle")
    async def org_toggle(request: Request):
        """停用/启用一条归属覆盖（按天然键 user_id）。"""
        viewer = _resolve_request_viewer(request)
        request.state.viewer = viewer
        payload = await _json_body(request)
        user_id = payload.get("user_id")
        enabled = payload.get("enabled")
        if (not isinstance(user_id, str) or not user_id.strip()
                or not isinstance(enabled, bool)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not capability_allows(viewer, CAPABILITY_ORG):
            if not capability_allows(
                    viewer, CAPABILITY_ORG, _org_override_region(user_id)):
                _forbidden(request)
        try:
            with db_connector() as connection:
                hit = org_assignment_override.set_assignment_enabled(
                    connection, user_id, enabled, actor=viewer.userid)
        except org_assignment_override.OrgAssignmentOverrideError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org toggle failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not hit:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/delete")
    async def org_delete(request: Request):
        """删除一条归属覆盖（审计留存被删行内容）。"""
        viewer = _resolve_request_viewer(request)
        request.state.viewer = viewer
        payload = await _json_body(request)
        user_id = payload.get("user_id")
        if not isinstance(user_id, str) or not user_id.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not capability_allows(viewer, CAPABILITY_ORG):
            if not capability_allows(
                    viewer, CAPABILITY_ORG, _org_override_region(user_id)):
                _forbidden(request)
        try:
            with db_connector() as connection:
                hit = org_assignment_override.delete_assignment(
                    connection, user_id, actor=viewer.userid)
        except org_assignment_override.OrgAssignmentOverrideError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org delete failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not hit:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/org/reset")
    async def org_reset(request: Request):
        """回退总开关：清除全部归属覆盖（回种子口径）。"""
        viewer = require_capability(request, CAPABILITY_ORG)
        await _json_body(request)
        try:
            with db_connector() as connection:
                deleted = org_assignment_override.reset_assignments(
                    connection, actor=viewer.userid)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web org reset failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "deleted": deleted})

    # -- 区域注册表真源读（D2，2026-10-14）-----------------------------------
    @app.get("/api/regions")
    def api_regions(request: Request):
        """区域/部门目录真源读（``dim_org_region`` / ``dim_org_dept``，启用行）。

        组织编排（``org``）与填报名册（``roster``）两域共用；持有二者任一
        能力（全局或区域级）即可读。目录读失败 → 503（收敛失败语义）。
        """
        viewer = _resolve_request_viewer(request)
        if not (capability_allows(viewer, CAPABILITY_ORG)
                or capability_has_scoped(viewer, CAPABILITY_ORG)
                or capability_allows(viewer, CAPABILITY_ROSTER)
                or capability_has_scoped(viewer, CAPABILITY_ROSTER)):
            _forbidden(request)
        request.state.viewer = viewer
        try:
            with db_connector() as connection:
                catalog = org_catalog.fetch_catalog(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web regions load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({
            "status": "ok",
            "regions": catalog.get("regions") or [],
            "depts": catalog.get("depts") or [],
        })

    # 「授权现状」页已停用（2026-10-13）：授权管理不再通过本页自助操作。
    # 路由保留防书签 404，页面引导联系管理员。
    @app.get("/grants")
    def grants_page():
        """授权现状页壳（Router 渲染只读授权 + 审计；写面按 2026-10-13 裁决关闭）。"""
        return _spa_shell()

    @app.get("/audit")
    def audit_page():
        """授权审计页壳（Router 渲染）。"""
        return _spa_shell()

    @app.get("/records")
    def records_page():
        """操作记录页壳（Router 渲染）。"""
        return _spa_shell()

    @app.get("/api/records")
    def api_records(request: Request):
        """六源聚合审计读面（JSON；/records 页数据源，2026-10-14 D1）。

        查询：``source``（来源枚举）、``actor``（操作人，子串）、``action``
        （动作，子串）、``from``/``to``（``YYYY-MM-DD``，含端点）。时间
        倒序，最多 200 条。
        """
        require_capability(request, CAPABILITY_AUDIT)
        source_q = (request.query_params.get("source") or "").strip()
        actor_q = (request.query_params.get("actor") or "").strip()
        action_q = (request.query_params.get("action") or "").strip()
        from_q = (request.query_params.get("from") or "").strip()
        to_q = (request.query_params.get("to") or "").strip()
        if source_q not in _RECORD_SOURCES:
            source_q = ""
        try:
            with db_connector() as connection:
                rows = _collect_records(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web records load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if source_q:
            rows = [r for r in rows if r[1] == source_q]
        if actor_q:
            rows = [r for r in rows if actor_q in (r[2] or "")]
        if action_q:
            rows = [r for r in rows if action_q in (r[3] or "")]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", from_q):
            rows = [r for r in rows if _fmt_time(r[0])[:10] >= from_q]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", to_q):
            rows = [r for r in rows if _fmt_time(r[0])[:10] <= to_q]
        records = [
            {"created_at": _fmt_time(r[0]), "source": r[1], "actor": r[2],
             "action": r[3], "target": r[4], "detail": r[5],
             "user_id": r[6] if len(r) > 6 else ""}
            for r in rows[:200]
        ]
        return JSONResponse({
            "status": "ok", "sources": list(_RECORD_SOURCES),
            "records": records,
        })

    # -- 授权只读面（JSON；/grants 页数据源，2026-10-14 D1）------------------
    @app.get("/api/grants")
    def api_grants(request: Request):
        require_admin(request)
        try:
            with db_connector() as connection:
                grants = bi_authz.fetch_all_grants(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web grants load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        # B5「暂无行级数据」标注（fail-soft：探针失败仅缺徽标，不拖垮读面）；
        # offline_all 豁免——B5 聚合授权键，oa 卡展开为三区并集有数据语义。
        empty_row_regions = []
        try:
            with db_connector() as connection:
                vocab = {key for key, _ in _region_scope_pairs(connection)}
                empty_row_regions = sorted(
                    vocab - _fact_region_keys(connection) - {"offline_all"}
                )
        except Exception as exc:
            _LOGGER.warning(
                "ops-web empty-row region probe failed: %s", type(exc).__name__
            )
        rows = []
        for row in grants:
            if isinstance(row, dict):
                get = row.get
            else:
                get = lambda key, r=row: r[(
                    "user_id", "grant_type", "grant_key", "note",
                    "updated_by", "updated_at").index(key)]
            grant_type = get("grant_type")
            rows.append({
                "user_id": get("user_id"),
                "grant_type": grant_type,
                "grant_type_label": _label(_GRANT_TYPE_LABEL, grant_type),
                "grant_key": get("grant_key"),
                "note": get("note"),
                "created_at": _fmt_time(get("updated_at")),
            })
        return JSONResponse({
            "status": "ok",
            "grants": rows,
            "empty_row_regions": empty_row_regions,
        })

    @app.get("/api/audit")
    def api_audit(request: Request):
        require_admin(request)
        try:
            with db_connector() as connection:
                entries = bi_authz.fetch_audit_rows(connection, limit=200)
        except Exception as exc:
            _LOGGER.warning("ops-web audit load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        rows = []
        for row in entries:
            if isinstance(row, dict):
                values = (row.get("actor"), row.get("action"), row.get("user_id"),
                          row.get("grant_type"), row.get("grant_key"),
                          row.get("note"), row.get("created_at"))
            else:
                values = row
            actor, action, user_id, grant_type, grant_key, note, created_at = values
            rows.append({
                "created_at": _fmt_time(created_at),
                "actor": actor,
                "action": action,
                "action_label": _label(_ACTION_LABEL, action),
                "user_id": user_id,
                "grant_type": grant_type,
                "grant_type_label": _label(_GRANT_TYPE_LABEL, grant_type),
                "grant_key": grant_key,
                "note": note,
            })
        return JSONResponse({"status": "ok", "rows": rows})

    # -- 写 API（事务内两写：grant + audit；actor 来自 session）--------------
    @app.post("/api/grants")
    async def grant_create(request: Request):
        viewer = require_admin(request)
        payload = await _json_body(request)
        try:
            record = bi_authz.validate_grant_fields(
                payload.get("user_id"),
                payload.get("grant_type"),
                payload.get("grant_key"),
                payload.get("note", ""),
            )
        except bi_authz.BiAuthzError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                bi_authz.upsert_grant(connection, record, actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning("ops-web grant write failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/grants/delete")
    async def grant_delete(request: Request):
        viewer = require_admin(request)
        payload = await _json_body(request)
        user_id = payload.get("user_id")
        grant_type = payload.get("grant_type")
        grant_key = payload.get("grant_key")
        if (not isinstance(user_id, str) or not user_id
                or grant_type not in bi_authz.GRANT_TYPES
                or not isinstance(grant_key, str) or not grant_key):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                deleted = bi_authz.delete_grant(
                    connection, user_id, grant_type, grant_key,
                    actor=viewer.userid,
                )
        except Exception as exc:
            _LOGGER.warning("ops-web grant delete failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "deleted": deleted})

    @app.post("/api/grants/region")
    async def grant_region(request: Request):
        """按区域一键开通：候选名单读 dim_robot_member，逐行 upsert + audit。"""
        viewer = require_admin(request)
        payload = await _json_body(request)
        region = payload.get("region")
        if not isinstance(region, str) or not region.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        region = region.strip()
        try:
            with db_connector() as connection:
                members = bi_authz.fetch_active_members(connection)
                written = 0
                for user_id, _name, member_region, _dept in members:
                    if member_region != region:
                        continue
                    bi_authz.upsert_grant(
                        connection,
                        bi_authz.GrantRecord(
                            user_id, "region", region, f"按区域批量开通（{region}）"
                        ),
                        actor=viewer.userid,
                    )
                    written += 1
        except Exception as exc:
            _LOGGER.warning("ops-web region grant failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "written": written})

    # -- 定时任务管理（管道注册表 + 立即运行一次）----------------------------

    def _fleet():
        """舰队清单；未接线或读取失败 -> 503（页面与写 API 共用）。"""
        if fleet_source is None or config_source is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            return tuple(fleet_source())
        except Exception as exc:
            _LOGGER.warning("ops-web fleet load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)

    def _pipelines_view():
        """定时任务视图装配（JSON；/api/pipelines 数据源，2026-10-14 D1）。

        注册表（Nacos）为真源：单条畸形按默认配置兜底不拖垮整页；最近运行取
        ``ops_control.fetch_recent_requests``（id DESC 首见即最新）；催办行
        「收受影响人」fail-soft（名册/通讯录读失败只该列为空口径）。
        返回 ``(groups, group_order, supported_categories)``。
        """
        service_ids = _fleet()
        configs = []
        for service_id in service_ids:
            try:
                configs.append(config_source.get_pipeline(service_id))
            except Exception as exc:
                _LOGGER.warning(
                    "ops-web pipeline config parse failed: %s", type(exc).__name__)
                configs.append(PipelineConfig(service_id))
        try:
            with db_connector() as connection:
                recent = ops_control.fetch_recent_requests(connection, limit=100)
        except Exception as exc:
            _LOGGER.warning("ops-web run requests load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        latest = {}
        for row in recent:  # fetch 按 id DESC，首见即最新
            sid = row.get("service_id") if isinstance(row, dict) else row[1]
            if sid not in latest:
                latest[sid] = row
        # 收受影响人（催办行）：附属信息 fail-soft——名册/通讯录读取故障只影响
        # 本列，不拖垮整页。
        reminder_cells = {}
        reminder_ids = [
            config.service_id for config in configs
            if resolve_category(config.service_id, config.category) == "催办"
        ]
        if reminder_ids:
            members_by_region = {}
            roster_by_region = {}
            uid_to_name = {}
            overrides_by_service = {}
            qudao_owners = None
            try:
                with db_connector() as connection:
                    robot_regions = {
                        _service_region(sid) for sid in reminder_ids
                        if sid.startswith("robot-")
                    }
                    if robot_regions:
                        members = bi_authz.fetch_active_members(connection)
                        try:
                            org_overrides = \
                                org_assignment_override.fetch_overrides(
                                    connection)
                        except Exception as exc:
                            _LOGGER.warning(
                                "ops-web org assignment override load "
                                "failed: %s", type(exc).__name__)
                            org_overrides = {}
                        members_by_region = _effective_members_by_region(
                            members, org_overrides)
                        for uid, name, _reg, _dept in members:
                            uid_to_name.setdefault(uid, name)
                        for region in robot_regions:
                            roster_by_region[region] = \
                                report_roster.fetch_scope_person_names(
                                    connection, region)
                    if any(not sid.startswith("robot-")
                           and _service_region(sid) == "qudao"
                           for sid in reminder_ids):
                        qudao_owners = report_roster.fetch_store_owner_map(
                            connection, "qudao", roles=("owner",))
                    for sid in reminder_ids:
                        try:
                            overrides_by_service[sid] = \
                                recipient_override.fetch_overrides(
                                    connection, sid)
                        except Exception as exc:
                            _LOGGER.warning(
                                "ops-web recipient override load failed: %s",
                                type(exc).__name__)
                            overrides_by_service[sid] = ()
                for sid in reminder_ids:
                    reminder_cells[sid] = _reminder_targets_cell(
                        sid, members_by_region=members_by_region,
                        roster_by_region=roster_by_region,
                        qudao_owners=qudao_owners,
                        overrides=overrides_by_service.get(sid, ()),
                        uid_to_name=uid_to_name)
            except Exception as exc:
                _LOGGER.warning(
                    "ops-web reminder targets load failed: %s",
                    type(exc).__name__)
                for sid in reminder_ids:
                    reminder_cells[sid] = (
                        "<span class=\"hint\">读取失败</span>")
        buckets = {}
        for config in configs:
            request_row = latest.get(config.service_id)
            latest_out = None
            if request_row is not None:
                if isinstance(request_row, dict):
                    get = request_row.get
                else:
                    get = lambda key, r=request_row: r[
                        ("id", "service_id", "requested_by", "status", "note",
                         "exit_code", "created_at", "finished_at").index(key)]
                latest_out = {
                    "status": get("status"),
                    "status_label": _label(_RUN_STATUS_LABEL, get("status")),
                    "requested_by": get("requested_by"),
                    "created_at": _fmt_time(get("created_at")),
                }
            category = resolve_category(config.service_id, config.category)
            buckets.setdefault(_service_region(config.service_id), []).append({
                "service_id": config.service_id,
                "service_name": _service_name(config.service_id),
                "service_label": _service_label(
                    config.service_id, config.description),
                "category_label": _category_label(config),
                "is_reminder": category == "催办",
                "reminder_html": reminder_cells.get(config.service_id),
                "enabled": bool(config.enabled),
                "schedule": config.schedule or "",
                "schedule_zh": _cron_zh(config.schedule) or "",
                "depends_on": list(config.depends_on or []),
                "params": dict(config.params or {}),
                "alertable": config.service_id in ALERT_SERVICES,
                "description": config.description or "",
                "disabled_reason": config.disabled_reason or "",
                "review_after": config.review_after or "",
                "has_command": bool(has_command(config.service_id)),
                "latest": latest_out,
            })
        # 分组顺序：城市区域 → 跨区域线 → 未登记区域 → 平台/无群（恒置末位）。
        ordered = [r for r in _GROUP_ORDER if buckets.get(r)]
        ordered += sorted(
            r for r in buckets
            if r is not None and r not in _GROUP_ORDER and buckets[r])
        if buckets.get(None):
            ordered.append(None)
        groups = [
            {"region": region,
             "title": _pipeline_group_title(region),
             "count": len(buckets[region]),
             "services": buckets[region]}
            for region in ordered
        ]
        return groups, list(_GROUP_ORDER), list(SUPPORTED_CATEGORIES)

    @app.get("/api/pipelines")
    def api_pipelines(request: Request):
        """定时任务读面（JSON；替代 SSR /pipelines 页数据，2026-10-14 D1）。"""
        require_capability(request, CAPABILITY_PIPELINES)
        groups, group_order, supported = _pipelines_view()
        return JSONResponse({
            "status": "ok",
            "groups": groups,
            "group_order": group_order,
            "supported_categories": supported,
        })

    @app.get("/pipelines")
    def pipelines_page():
        """定时任务页壳（Router 渲染）。"""
        return _spa_shell()

    @app.get("/api/pipelines/audit")
    def api_pipelines_audit(request: Request):
        """定时任务审计读面（JSON；替代 SSR /pipelines/audit 页，2026-10-14 D1）。"""
        require_capability(request, CAPABILITY_PIPELINES)
        try:
            with db_connector() as connection:
                entries = ops_control.fetch_pipeline_audit(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline audit load failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        rows = []
        for entry in entries:
            if isinstance(entry, dict):
                values = (entry.get("actor"), entry.get("action"),
                          entry.get("service_id"), entry.get("detail"),
                          entry.get("created_at"))
            else:
                values = entry
            actor, action, service_id, detail, created_at = values
            rows.append({
                "created_at": _fmt_time(created_at),
                "actor": actor,
                "action": action,
                "action_label": _label(_ACTION_LABEL, action),
                "service_id": service_id,
                "service_name": _service_name(service_id),
                "detail": detail,
            })
        return JSONResponse({"status": "ok", "rows": rows})

    @app.get("/pipelines/audit")
    def pipeline_audit_page():
        """任务审计页壳（Router 渲染）。"""
        return _spa_shell()

    @app.get("/api/pipelines/history")
    def api_pipelines_history(request: Request):
        """定时任务执行流水读面（JSON；替代 SSR /pipelines/history 页）。"""
        require_capability(request, CAPABILITY_PIPELINES)
        service_filter = (request.query_params.get("service_id") or "").strip()
        try:
            with db_connector() as connection:
                entries = ops_control.fetch_run_history(
                    connection, service_id=service_filter or None)
        except ops_control.OpsControlError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except Exception as exc:
            _LOGGER.warning("ops-web run history load failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        history_keys = ("id", "service_id", "trigger_type", "status",
                        "exit_code", "note", "started_at", "finished_at")
        rows = []
        for row in entries:
            if isinstance(row, dict):
                get = row.get
            else:
                get = lambda key, r=row: r[history_keys.index(key)]
            rows.append({
                "service_id": get("service_id"),
                "service_name": _service_name(get("service_id")),
                "trigger_type": get("trigger_type"),
                "trigger_label": _label(_TRIGGER_LABEL, get("trigger_type")),
                "status": get("status"),
                "status_label": _label(_RUN_STATUS_LABEL, get("status")),
                "exit_code": get("exit_code"),
                "started_at": _fmt_time(get("started_at")),
                "finished_at": _fmt_time(get("finished_at")),
                "note": get("note"),
            })
        return JSONResponse({
            "status": "ok", "service_id": service_filter, "rows": rows})

    @app.get("/pipelines/history")
    def pipeline_history_page():
        """执行流水页壳（Router 渲染）。"""
        return _spa_shell()

    @app.post("/api/pipelines/toggle")
    async def pipeline_toggle(request: Request):
        """启停（2026-10-17 治理 P0）：停用必须带原因（reason），可选复查日
        （review_after，YYYY-MM-DD）；启用自动清空治理字段。重复停用同态
        请求=治理元数据补录（enabled 不变、字段更新，审计照常）。"""
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        if config_publisher is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        enabled = payload.get("enabled")
        if (not ops_control.SERVICE_ID_RE.match(service_id or "")
                or not isinstance(enabled, bool)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if service_id not in _fleet():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        reason = (payload.get("reason") or "").strip()
        review_after = (payload.get("review_after") or "").strip()
        if not enabled and not reason:
            # 停用不带原因=没有上下文的死配置（治理门禁）
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if review_after:
            try:
                from datetime import date as _date
                _date.fromisoformat(review_after)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config = config_source.get_pipeline(service_id)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            config_publisher(replace(
                config, enabled=enabled,
                disabled_reason="" if enabled else reason,
                review_after="" if enabled else review_after,
            ))
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline publish failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        detail = config.schedule or ""
        if not enabled:
            detail = f"{reason}"
            if review_after:
                detail += f"（复查 {review_after}）"
        try:
            with db_connector() as connection:
                ops_control.insert_pipeline_audit(
                    connection, viewer.userid,
                    "enable" if enabled else "disable", service_id,
                    detail=detail,
                )
        except Exception as exc:
            # 发布已生效而审计没落库：变更存在但流水缺失，必须显眼。
            _LOGGER.error(
                "ops-web pipeline audit write failed AFTER publish: %s",
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/pipelines/archive")
    async def pipeline_archive(request: Request):
        """归档（2026-10-19 治理 P2 裁决）：Nacos 摘除条目 + 审计留痕。

        前置门禁：**只允许归档已停用的服务**（先停用后归档，防误删活线）。
        归档语义 = Nacos 摘除 + seed 删条目（代码 PR）+ git 历史即档案；
        响应带 ``seed_registered`` 提示——seed 里的条目不在本端点职责内，
        须同 PR 删除，否则下次 ``publish-pipelines --if-missing`` 会复活。
        """
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        if config_deleter is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        if not ops_control.SERVICE_ID_RE.match(service_id or ""):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if service_id not in _fleet():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config = config_source.get_pipeline(service_id)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if config.enabled:
            # 活线不可归档：先走停用（带原因）再归档
            raise HTTPException(status_code=409, detail=ErrorDetail.CONFLICT)
        seed_path = (os.environ.get("PUBLIC_DATA_PIPELINE_SEED") or "").strip()
        seed_registered = False
        if seed_path:
            try:
                from common.public_data.pipeline_config import load_seed
                seed_registered = service_id in load_seed(seed_path)
            except Exception:
                _LOGGER.warning("ops-web seed probe failed: archive anyway")
        try:
            config_deleter(service_id)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline archive failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            with db_connector() as connection:
                ops_control.insert_pipeline_audit(
                    connection, viewer.userid, "archive", service_id,
                    detail=config.disabled_reason or "",
                )
        except Exception as exc:
            # 删除已生效而审计没落库：变更存在但流水缺失，必须显眼。
            _LOGGER.error(
                "ops-web pipeline audit write failed AFTER archive: %s",
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok",
                             "seed_registered": seed_registered})

    @app.post("/api/pipelines/update")
    async def pipeline_schedule_update(request: Request):
        """修改既有线的播报时间（read-modify-write，只换 cron 的分/时两段）。

        与 toggle 同型：读注册表 → 改 → 整条回写 → 审计同生共死。请求体
        ``{service_id, times:[{hour, minute}, ...]}``（1..3 个 HH:MM 时点，
        任意分 0-59）；保留原 cron 的日/月/周（取首段尾段），因此「工作日
        18:00」「每月 1 日」这类形态不被抹平成每天；多时点回写为 ``;`` 分隔
        的多段 cron（调度器按并集触发）。
        """
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        if config_publisher is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        times = payload.get("times")
        if (not ops_control.SERVICE_ID_RE.match(service_id or "")
                or not isinstance(times, list)
                or not 1 <= len(times) <= 3):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        clean = []
        for item in times:
            if not isinstance(item, dict):
                raise HTTPException(status_code=400,
                                    detail=ErrorDetail.BAD_REQUEST)
            hour, minute = item.get("hour"), item.get("minute")
            if (not isinstance(hour, int) or isinstance(hour, bool)
                    or not isinstance(minute, int) or isinstance(minute, bool)
                    or not 0 <= hour <= 23 or not 0 <= minute <= 59):
                raise HTTPException(status_code=400,
                                    detail=ErrorDetail.BAD_REQUEST)
            clean.append((hour, minute))
        clean = sorted(set(clean))
        if service_id not in _fleet():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config = config_source.get_pipeline(service_id)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        # 尾段取现有 cron 首段(分 时 日 月 周)的第 2/3/4 字段，保留 日/月/周；
        # 非 5 段（空/常驻/异常）回落每天，避免拼出非法表达式。
        fields = (config.schedule or "").split(";")[0].split()
        if len(fields) == 5:
            tail = (fields[2], fields[3], fields[4])
        else:
            tail = ("*", "*", "*")
        new_cron = ";".join(
            f"{minute} {hour} {tail[0]} {tail[1]} {tail[2]}"
            for hour, minute in clean
        )
        try:
            # 只借它做 croniter 校验；返回值会丢 sources/reads，不用于发布。
            ops_control.validate_pipeline_fields(
                service_id, config.kind, new_cron,
                enabled=config.enabled, description=config.description,
                depends_on=config.depends_on, category=config.category,
            )
        except ops_control.OpsControlError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config_publisher(replace(config, schedule=new_cron))
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline publish failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            with db_connector() as connection:
                ops_control.insert_pipeline_audit(
                    connection, viewer.userid, "schedule", service_id,
                    detail=new_cron,
                )
        except Exception as exc:
            _LOGGER.error(
                "ops-web pipeline audit write failed AFTER publish: %s",
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "schedule": new_cron})

    @app.post("/api/pipelines/params")
    async def pipeline_params_update(request: Request):
        """修改既有线的线级参数 ``params``（read-modify-write + 审计）。

        巡检告警线的阈值/冷却即落此：请求体 ``{service_id, params:{...}}``，
        ``params`` 传 ``{}`` 即清空。只覆盖 ``params`` 字段，其余运行态
        （enabled/schedule/description…）原样保留，与改定时同型。
        """
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        if config_publisher is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        if not ops_control.SERVICE_ID_RE.match(service_id or ""):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if service_id not in _fleet():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        # 必须显式给 dict（清空传 {}）；缺键/None/非映射一律 400，
        # 避免客户端漏传把既有 params 静默清空。
        raw_params = payload.get("params")
        if not isinstance(raw_params, dict):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            params = ops_control.validate_pipeline_params(raw_params)
        except ops_control.OpsControlError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config = config_source.get_pipeline(service_id)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            config_publisher(replace(config, params=params))
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline publish failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        detail = "; ".join(
            f"{key}={value}" for key, value in sorted(params.items())
        ) or "cleared"
        try:
            with db_connector() as connection:
                ops_control.insert_pipeline_audit(
                    connection, viewer.userid, "params", service_id,
                    detail=detail,
                )
        except Exception as exc:
            _LOGGER.error(
                "ops-web pipeline audit write failed AFTER publish: %s",
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "params": params})

    @app.post("/api/pipelines/deps")
    async def pipeline_deps_update(request: Request):
        """修改既有线的依赖 ``depends_on``（画布拉线：模块口→模块口）。

        请求体 ``{service_id, depends_on:[<service_id>, ...]}``——全量覆盖
        该线的依赖集（画布侧做增量加减后整体提交）；依赖必须都在舰队内且
        不等于自身；重复自动去重（保序）。read-modify-write + 审计，
        与改定时/改参数同型。
        """
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        if config_publisher is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        depends_on = payload.get("depends_on")
        if (not ops_control.SERVICE_ID_RE.match(service_id or "")
                or not isinstance(depends_on, list)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        fleet = set(_fleet())
        if service_id not in fleet:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        clean = []
        for dep in depends_on:
            if (not isinstance(dep, str)
                    or not ops_control.SERVICE_ID_RE.match(dep)
                    or dep not in fleet
                    or dep == service_id):
                raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
            if dep not in clean:
                clean.append(dep)
        try:
            config = config_source.get_pipeline(service_id)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            # 借校验器过一遍（croniter 等全字段），返回值不用于发布。
            ops_control.validate_pipeline_fields(
                service_id, config.kind, config.schedule,
                enabled=config.enabled, description=config.description,
                depends_on=clean, category=config.category,
            )
        except ops_control.OpsControlError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config_publisher(replace(config, depends_on=clean))
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline publish failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            with db_connector() as connection:
                ops_control.insert_pipeline_audit(
                    connection, viewer.userid, "deps", service_id,
                    detail=",".join(clean) or "cleared",
                )
        except Exception as exc:
            _LOGGER.error(
                "ops-web pipeline audit write failed AFTER publish: %s",
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "depends_on": clean})

    @app.get("/api/pipelines/health")
    def api_pipelines_health(request: Request):
        """舰队健康读面（治理抽屉用）：每服务最近一次真实 cron 运行。

        真源 ``pd_ops_run_history``（调度器落表）；注意与
        ``pd_ops_run_request``（手动「运行一次」请求）区分——治理的僵尸
        检测必须用前者，2026-10-17 P1 误报事故即混用所致。
        返回 ``{service_id: {"started_at", "status"}}``。
        """
        require_capability(request, CAPABILITY_PIPELINES)
        try:
            with db_connector() as connection:
                rows = ops_control.fetch_run_history(connection, limit=500)
        except Exception as exc:
            _LOGGER.warning("ops-web run history load failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        latest = {}
        for row in rows:  # id DESC，首见即最新
            get = (row.get if isinstance(row, dict)
                   else None)
            if get is None:
                sid, status, started = row[1], row[3], row[6]
            else:
                sid, status, started = (
                    get("service_id"), get("status"), get("started_at"))
            if sid not in latest:
                latest[sid] = {
                    "started_at": _fmt_time(started),
                    "status": status,
                }
        return JSONResponse({"status": "ok", "latest": latest})

    @app.get("/api/pipelines/fleet-audit-latest")
    def api_pipelines_fleet_audit_latest(request: Request):
        """最近一次舰队月检快照（治理抽屉「上次月检快照」节数据源）。

        真源 ``pd_ops_fleet_audit``（fleet-audit 管道每月 2 日落一行）；
        从未跑过 → ``snapshot=None``（前端显示「未跑过」而非报错）。
        """
        require_capability(request, CAPABILITY_PIPELINES)
        try:
            with db_connector() as connection:
                snapshot = fleet_audit.fetch_latest_snapshot(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web fleet-audit snapshot load failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "snapshot": snapshot})

    # -- 数据修正（类型化修数通道，2026-10-16：画布拉线之外的写面收口）-----

    @app.post("/api/data-fix/realign")
    async def data_fix_realign(request: Request):
        """线下事实行人员归属归位（preview/apply 一体）。

        请求体 ``{person_name, from_region, to_region, apply?}``：
        ``apply`` 缺省 false = 预览（命中行数/金额/日期范围，零写入）；
        true = 同事务 UPDATE + 审计。区域必须都在区域注册表内且不相同；
        零命中不落审计（空操作不算修正）。
        """
        viewer = require_capability(request, CAPABILITY_DATAFIX)
        payload = await _json_body(request)
        person_name = (payload.get("person_name") or "").strip()
        from_region = (payload.get("from_region") or "").strip()
        to_region = (payload.get("to_region") or "").strip()
        apply = bool(payload.get("apply"))
        if not person_name or not from_region or not to_region:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if from_region == to_region:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                known = set(org_catalog.fetch_region_keys(connection))
                if from_region not in known or to_region not in known:
                    raise HTTPException(
                        status_code=400, detail=ErrorDetail.BAD_REQUEST)
                if apply:
                    stats = data_fix.realign_offline_person_region(
                        connection, person_name=person_name,
                        from_region=from_region, to_region=to_region,
                        actor=viewer.userid)
                else:
                    stats = data_fix.preview_person_region_realign(
                        connection, person_name=person_name,
                        from_region=from_region, to_region=to_region)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning("ops-web data fix failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "applied": apply, **stats})

    @app.get("/api/data-fix/audits")
    def api_data_fix_audits(request: Request):
        """数据修正审计读面（JSON，created_at 倒序最多 50 条）。"""
        require_capability(request, CAPABILITY_DATAFIX)
        try:
            with db_connector() as connection:
                rows = data_fix.fetch_recent_audits(connection, limit=50)
        except Exception as exc:
            _LOGGER.warning("ops-web data fix audit load failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        get = lambda row: _audit_get(row, ("actor", "action", "fix_type",
                                           "target", "detail", "row_count",
                                           "created_at"))
        return JSONResponse({
            "status": "ok",
            "audits": [{
                "created_at": _fmt_time(get(row)("created_at")),
                "actor": get(row)("actor"),
                "action": get(row)("action"),
                "action_label": _label(_ACTION_LABEL, get(row)("action")),
                "fix_type": get(row)("fix_type"),
                "fix_type_label": _label(_FIX_TYPE_LABEL, get(row)("fix_type")),
                "target": get(row)("target"),
                "detail": get(row)("detail"),
                "row_count": get(row)("row_count"),
            } for row in rows],
        })

    @app.post("/api/pipelines/add")
    async def pipeline_add(request: Request):
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        if config_publisher is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        payload = await _json_body(request)
        depends_on = payload.get("depends_on") or []
        if not isinstance(depends_on, list):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config = ops_control.validate_pipeline_fields(
                payload.get("service_id"),
                payload.get("kind"),
                payload.get("schedule"),
                enabled=payload.get("enabled", True),
                description=payload.get("description", ""),
                depends_on=depends_on,
                category=payload.get("category") or None,
                params=payload.get("params"),
            )
        except ops_control.OpsControlError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if config.service_id in _fleet():
            # 已存在的线走「操作」列开关；重复新增会静默覆盖 Nacos 条目。
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config_publisher(config)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline publish failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            with db_connector() as connection:
                ops_control.insert_pipeline_audit(
                    connection, viewer.userid, "add", config.service_id,
                    detail=config.schedule or "",
                )
        except Exception as exc:
            _LOGGER.error(
                "ops-web pipeline audit write failed AFTER publish: %s",
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/pipelines/run-once")
    async def pipeline_run_once(request: Request):
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        if not ops_control.SERVICE_ID_RE.match(service_id or ""):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not has_command(service_id):
            # 无命令模板的 id 触发不了（C 方案：允许注册但不允许空跑）。
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                if ops_control.has_pending_request(connection, service_id):
                    raise HTTPException(
                        status_code=400, detail=ErrorDetail.BAD_REQUEST
                    )
                request_id = ops_control.insert_run_request(
                    connection, service_id, viewer.userid,
                )
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning("ops-web run-once write failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "request_id": request_id})

    # -- 填报名册（dim_report_roster，ops-web 唯一写方）---------------------

    @app.get("/roster")
    def roster_page():
        """填报名册页壳（Router 渲染）。"""
        return _spa_shell()

    # -- 收件人总览（P4，2026-10-12；只读，无写面）--------------------------

    @app.get("/api/recipients")
    def api_recipients(request: Request):
        """收件人总览读面（JSON；替代 SSR /recipients 页数据，2026-10-14 D1）。

        只读展示口径；舰队清单/只读上下文均 fail-soft（单块故障只缺该块）。
        """
        require_admin(request)
        configs = []
        scope_pairs = [(scope, _label(_SCOPE_LABEL, scope))
                       for scope in report_roster.SCOPES]
        try:
            if fleet_source is not None and config_source is not None:
                for service_id in _fleet():
                    try:
                        configs.append(config_source.get_pipeline(service_id))
                    except Exception as exc:
                        _LOGGER.warning(
                            "ops-web recipients config parse failed: %s",
                            type(exc).__name__)
                        configs.append(PipelineConfig(service_id))
        except Exception as exc:
            _LOGGER.warning("ops-web recipients fleet load failed: %s",
                            type(exc).__name__)
            configs = []
        scope_tasks = _scope_backlinks(configs)
        reminder_ids = sorted({sid for ids in scope_tasks.values()
                               for sid in ids})
        overrides_by_service = {}
        context = None
        context_error = False
        try:
            with db_connector() as connection:
                members = bi_authz.fetch_active_members(connection)
                try:
                    org_overrides = org_assignment_override.fetch_overrides(
                        connection)
                except Exception as exc:
                    _LOGGER.warning(
                        "ops-web org assignment override load failed: %s",
                        type(exc).__name__)
                    org_overrides = {}
                members_by_region = _effective_members_by_region(
                    members, org_overrides)
                uid_to_name = {}
                for uid, name, _reg, _dept in members:
                    uid_to_name.setdefault(uid, name)
                scope_pairs = _region_scope_pairs(connection)
                roster_by_region = {
                    scope: report_roster.fetch_scope_person_names(
                        connection, scope)
                    for scope, _label_ in scope_pairs if scope != "qudao"
                }
                qudao_owners = report_roster.fetch_store_owner_map(
                    connection, "qudao", roles=("owner",))
                for service_id in reminder_ids:
                    try:
                        overrides_by_service[service_id] = \
                            recipient_override.fetch_overrides(
                                connection, service_id)
                    except Exception as exc:
                        _LOGGER.warning(
                            "ops-web recipients override load failed: %s",
                            type(exc).__name__)
                        overrides_by_service[service_id] = ()
            context = {
                "members_by_region": members_by_region,
                "roster_by_region": roster_by_region,
                "qudao_owners": qudao_owners,
                "uid_to_name": uid_to_name,
            }
        except Exception as exc:
            _LOGGER.warning("ops-web recipients context load failed: %s",
                            type(exc).__name__)
            context_error = True
        sections = []
        for scope, scope_label in scope_pairs:
            if context is None:
                sections.append({
                    "scope": scope, "label": scope_label,
                    "html": "<p class=\"hint\">名单读取失败，请稍后重试。</p>"})
                continue
            try:
                html = _recipients_scope_html(
                    scope, scope_tasks.get(scope) or [],
                    overrides_by_service=overrides_by_service, **context)
            except Exception as exc:
                _LOGGER.warning("ops-web recipients scope failed: %s",
                                type(exc).__name__)
                html = "<p class=\"hint\">读取失败，请稍后重试。</p>"
            sections.append({"scope": scope, "label": scope_label, "html": html})
        return JSONResponse({
            "status": "ok",
            "rules_html": _RECIPIENT_RULES_HTML,
            "sections": sections,
            "context_error": context_error,
        })

    @app.get("/recipients")
    def recipients_page():
        """收件人总览页壳（Router 渲染）。"""
        return _spa_shell()

    # -- 填报名册读面（JSON，2026-10-14 D1：替代 SSR 页数据）----------------
    @app.get("/api/roster")
    def api_roster(request: Request):
        """名册 + 区域个人月目标读面。

        ``?month=YYYY-MM`` 指定目标月份（缺省当月）。仅返回 viewer 授权
        （或全局）区域的记录与目标（D4 区域分权）；qudao 渠道门店目标
        不在本 API（另一存储、另一概念）。
        """
        viewer = _resolve_request_viewer(request)
        if not (capability_allows(viewer, CAPABILITY_ROSTER)
                or capability_has_scoped(viewer, CAPABILITY_ROSTER)):
            _forbidden(request)
        request.state.viewer = viewer
        current_month = _current_year_month()
        selected_month = (request.query_params.get("month") or current_month)
        if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", selected_month):
            selected_month = current_month
        try:
            with db_connector() as connection:
                entries = report_roster.fetch_roster(connection)
                audits = report_roster.fetch_roster_audit(connection, limit=20)
                scope_pairs = _region_scope_pairs(connection)
                scopes = [key for key, _ in scope_pairs]
                visible_scopes = visible_region_scopes(viewer, scopes)
                targets = {
                    scope: report_roster.fetch_person_targets(
                        connection, scope, selected_month)
                    for scope in visible_scopes if scope != "qudao"
                }
        except Exception as exc:
            _LOGGER.warning("ops-web roster load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        labels = dict(scope_pairs)
        allowed = set(visible_scopes)
        # updated_at / created_at 是 DB DATETIME（pymysql 返回 datetime 对象），
        # 不经 _fmt_time 归一直接进 JSONResponse 会 json.dumps 失败 → 整接口
        # 500、/roster 页数据面全挂（2026-10-14 带会话线上实测）。出口统一为
        # 北京时间字符串，与 records / overrides 同口径。
        roster = [
            dict(row, scope_label=labels.get(row.get("scope"))
                 or _label(_SCOPE_LABEL, row.get("scope")),
                 updated_at=_fmt_time(row.get("updated_at")))
            for row in entries if row.get("scope") in allowed
        ]
        audits = [
            dict(row, created_at=_fmt_time(row.get("created_at")))
            for row in audits
        ]
        return JSONResponse({
            "status": "ok",
            "month": selected_month,
            "regions": [
                {"region_key": key, "label": label}
                for key, label in scope_pairs if key in allowed
            ],
            "roster": roster,
            "targets": targets,
            "audits": audits,
        })

    @app.get("/api/roster/targets")
    def api_roster_targets(request: Request):
        """某区域当月个人月目标读面（``dim_report_target``，万元）。

        ``?scope=<region>&month=YYYY-MM``（缺省当月）。区域分权：仅该
        scope 的全局 / 区域级 roster 授权可读；qudao 走门店目标表不在此。
        """
        viewer = _resolve_request_viewer(request)
        scope = (request.query_params.get("scope") or "").strip()
        if not capability_allows(viewer, CAPABILITY_ROSTER, scope):
            _forbidden(request)
        request.state.viewer = viewer
        if not scope or scope == "qudao":
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        month = (request.query_params.get("month") or _current_year_month())
        if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
            month = _current_year_month()
        try:
            with db_connector() as connection:
                registered = org_catalog.fetch_region_keys(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web roster region read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if registered and scope not in registered:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                targets = report_roster.fetch_person_targets(
                    connection, scope, month)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web roster targets load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({
            "status": "ok", "scope": scope, "month": month,
            "targets": [
                {"person_name": name, "monthly_target": value}
                for name, value in sorted(targets.items())
            ],
        })

    @app.get("/api/roster/qudao")
    def api_roster_qudao(request: Request):
        """渠道门店一体读面（JSON；替代 SSR 名册页 qudao 段，2026-10-14 D1）。

        门店目标走 raw 通道（单位元，与区域个人月目标「万元」不同口径）；
        含负责人/代填报人名册链接、店铺编号与渠道。
        """
        viewer = _resolve_request_viewer(request)
        if not capability_allows(viewer, CAPABILITY_ROSTER, "qudao"):
            _forbidden(request)
        request.state.viewer = viewer
        try:
            with db_connector() as connection:
                entries = report_roster.fetch_roster(connection)
                targets = channel_target.fetch_store_targets(connection)
                numbers = report_roster.fetch_store_numbers(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web roster qudao load failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        rows = [row for row in entries if row.get("scope") == "qudao"]
        store_rows = {}
        for row in rows:
            store_rows.setdefault(row["entity_key"], []).append(row)
        all_stores = sorted(set(store_rows) | set(targets.keys()))
        stores = []
        for store in all_stores:
            channel, target = targets.get(
                store, (numbers.get(store, ("", None))[0], None))
            links = store_rows.get(store, [])
            stores.append({
                "store": store,
                "channel": channel or "",
                "target": target,
                "store_no": numbers.get(store, (None, None))[1],
                "owners": sorted(r["person_name"] for r in links
                                 if r.get("role") != "deputy"),
                "deputies": sorted(r["person_name"] for r in links
                                   if r.get("role") == "deputy"),
                "links": [dict(r, updated_at=_fmt_time(r.get("updated_at")))
                          for r in sorted(
                              links,
                              key=lambda r: (
                                  1 if r.get("role") == "deputy" else 0,
                                  r["person_name"], int(r["id"])))],
            })
        return JSONResponse({"status": "ok", "stores": stores})

    @app.post("/api/roster/add")
    async def roster_add(request: Request):
        viewer = _resolve_request_viewer(request)
        payload = await _json_body(request)
        aliases = payload.get("aliases") or []
        if not isinstance(aliases, list):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        scope = payload.get("scope")
        # 区域分权（D4）：全局 roster 或该 scope 的区域级授权。
        if not capability_allows(viewer, CAPABILITY_ROSTER, scope):
            _forbidden(request)
        try:
            entry = report_roster.validate_roster_fields(
                scope,
                payload.get("entity_type"),
                payload.get("entity_key"),
                payload.get("person_name"),
                aliases=aliases,
                note=payload.get("note", ""),
                role=payload.get("role") or "owner",
            )
        except report_roster.ReportRosterError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                registered = org_catalog.fetch_region_keys(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web roster region read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        # 区域真源校验（D2）：scope 必须是注册表已登记区域；注册表空 → fail-soft。
        if registered and scope not in registered:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                report_roster.upsert_roster_entry(
                    connection, entry, actor=viewer.userid
                )
        except Exception as exc:
            _LOGGER.warning("ops-web roster add failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/roster/toggle")
    async def roster_toggle(request: Request):
        viewer = require_capability(request, CAPABILITY_ROSTER)
        payload = await _json_body(request)
        roster_id = payload.get("id")
        enabled = payload.get("enabled")
        if (isinstance(roster_id, bool) or not isinstance(roster_id, int)
                or not isinstance(enabled, bool)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                hit = report_roster.set_roster_enabled(
                    connection, roster_id, enabled, actor=viewer.userid
                )
        except Exception as exc:
            _LOGGER.warning("ops-web roster toggle failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not hit:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/roster/delete")
    async def roster_delete(request: Request):
        viewer = require_capability(request, CAPABILITY_ROSTER)
        payload = await _json_body(request)
        roster_id = payload.get("id")
        if isinstance(roster_id, bool) or not isinstance(roster_id, int):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                hit = report_roster.delete_roster_entry(
                    connection, roster_id, actor=viewer.userid
                )
        except Exception as exc:
            _LOGGER.warning("ops-web roster delete failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not hit:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/roster/region-targets")
    async def roster_region_targets(request: Request):
        """保存某区域当月个人月目标（dim_report_target，「所有数据入库」）：
        逐条校验+upsert+审计；空白（null）= 清除该人当月目标。"""
        viewer = _resolve_request_viewer(request)
        payload = await _json_body(request)
        scope = payload.get("scope")
        rows = payload.get("rows")
        # 区域分权（D4）：全局 roster 或该 scope 的区域级授权。
        if not capability_allows(viewer, CAPABILITY_ROSTER, scope):
            _forbidden(request)
        if (not isinstance(scope, str)
                or scope not in report_roster.SCOPES
                or scope == "qudao" or not isinstance(rows, list)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                registered = org_catalog.fetch_region_keys(connection)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web roster region read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        # 区域真源校验（D2）：scope 必须是注册表已登记区域；注册表空 → fail-soft。
        if registered and scope not in registered:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        # 目标月份（2026-10-09 起可指定下月提前录入；缺省当月，非法回退当月）
        year_month = payload.get("year_month") or _current_year_month()
        if not isinstance(year_month, str) or not re.fullmatch(
                r"20\d{2}-(0[1-9]|1[0-2])", year_month):
            year_month = _current_year_month()
        try:
            entries = [
                report_roster.validate_target_fields(
                    scope,
                    (row or {}).get("person_name") if isinstance(row, dict)
                    else None,
                    year_month,
                    (row or {}).get("monthly_target")
                    if isinstance(row, dict) else None,
                )
                for row in rows
            ]
        except report_roster.ReportRosterError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                for entry in entries:
                    report_roster.upsert_report_target(
                        connection, entry, actor=viewer.userid
                    )
        except Exception as exc:
            _LOGGER.warning(
                "ops-web region targets save failed: %s", type(exc).__name__
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "saved": len(entries)})

    @app.post("/api/roster/publish-snapshot")
    async def roster_publish_snapshot(request: Request):
        """发布月度目标快照（S2）：目标覆盖 raw + 名册投影 owners + 触发提取。"""
        viewer = require_admin(request)
        payload = await _json_body(request)
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        # 渠道缺省时从名册编号表反查（页面只对有把握的店带 channel）。
        try:
            with db_connector() as connection:
                numbers = report_roster.fetch_store_numbers(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web publish preflight failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, dict) and not (row.get("channel") or "").strip():
                store = row.get("store_name")
                row["channel"] = numbers.get(store, ("", None))[0]
        try:
            target_rows = channel_target.validate_target_rows(rows)
        except channel_target.ChannelTargetError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as mart_connection:
                raw_connection = connect(settings.dingtalk_database)
                try:
                    deleted, inserted, missing = channel_target.publish_snapshot(
                        raw_connection, target_rows, actor=viewer.userid,
                        roster_connection=mart_connection,
                    )
                finally:
                    raw_connection.close()
                request_id = ops_control.insert_run_request(
                    mart_connection, "extract-mart", viewer.userid
                )
        except Exception as exc:
            _LOGGER.warning("ops-web publish snapshot failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({
            "status": "ok", "deleted": deleted, "inserted": inserted,
            "missing_owners": missing, "extract_request_id": request_id,
        })

    # -- 播报日历（dim_calendar_override DB 裁决层，2026-10-16 纯播报口径：
    # 默认每天播报，点日期改 播报/休息，不碰 git/CLI）---------------------

    def _calendar_seed_path():
        seed_path = getattr(settings, "calendar_seed_path", None)
        if seed_path is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return seed_path

    @app.get("/calendar")
    def calendar_page():
        """播报日历页壳（Router 渲染）。"""
        return _spa_shell()

    @app.get("/api/calendar")
    def api_calendar(request: Request):
        """播报日历读面（JSON；替代 SSR /calendar 页数据，2026-10-14 D1）。"""
        require_capability(request, CAPABILITY_CALENDAR)
        month = (request.query_params.get("month") or "").strip()
        if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
            month = _current_year_month()
        year, mon = (int(part) for part in month.split("-"))
        try:
            with db_connector() as connection:
                days = calendar_store.fetch_month_days(connection, year, mon)
                audits = calendar_store.fetch_recent_audits(connection, limit=20)
        except Exception as exc:
            _LOGGER.warning("ops-web calendar load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        cells = {day.day: (day, is_workday, source, note)
                 for day, is_workday, source, note in days}
        today = datetime.now(_BJT).date()
        import calendar as _pycal
        out_days = []
        for day_no in range(1, _pycal.monthrange(year, mon)[1] + 1):
            info = cells.get(day_no)
            if info is None:
                out_days.append({"day": day_no, "is_workday": None,
                                 "source": None, "note": "", "is_today": False})
                continue
            day, is_workday, source, note = info
            out_days.append({
                "day": day_no,
                "is_workday": bool(is_workday),
                "source": source,
                "note": note or "",
                "is_today": day == today,
            })
        audit_rows = []
        for entry in audits:
            if isinstance(entry, dict):
                get = entry.get
            else:
                get = lambda key, r=entry: r[(
                    "actor", "action", "business_date", "year", "month",
                    "detail", "created_at").index(key)]
            biz_date = get("business_date")
            yr, mon2 = get("year"), get("month")
            when = biz_date.isoformat() if isinstance(biz_date, date) else (
                f"{yr}-{int(mon2):02d}" if yr and mon2 else "—")
            audit_rows.append({
                "created_at": _fmt_time(get("created_at")),
                "actor": get("actor"),
                "action": get("action"),
                "action_label": _label(_ACTION_LABEL, get("action")),
                "when": when,
                "detail": get("detail"),
            })
        return JSONResponse({
            "status": "ok",
            "month": month,
            "year": year,
            "month_no": mon,
            "first_weekday": date(year, mon, 1).weekday(),
            "days": out_days,
            "audits": audit_rows,
            "prev_month": _shift_year_month(month, -1),
            "next_month": _shift_year_month(month, 1),
            "today_iso": today.isoformat(),
        })

    @app.post("/api/calendar/toggle")
    async def calendar_toggle(request: Request):
        """点日期改 上班/休息：写 manual 覆盖 + 即时重写当月 + 审计。"""
        viewer = require_capability(request, CAPABILITY_CALENDAR)
        payload = await _json_body(request)
        note = payload.get("note") or ""
        is_workday = payload.get("is_workday")
        try:
            day = date.fromisoformat(str(payload.get("date")))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(is_workday, bool) \
                or not isinstance(note, str) or len(note) > 255:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                calendar_store.upsert_override(
                    connection, day, is_workday,
                    actor=viewer.userid, note=note,
                )
                calendar_store.rewrite_month(
                    connection, _calendar_seed_path(), day.year, day.month,
                )
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web calendar toggle failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/calendar/reset-month")
    async def calendar_reset_month(request: Request):
        """本月恢复规则：清除 manual 覆盖（回到全播报基线）+ 即时重写。"""
        viewer = require_capability(request, CAPABILITY_CALENDAR)
        payload = await _json_body(request)
        month = str(payload.get("month") or "")
        if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        year, mon = (int(part) for part in month.split("-"))
        try:
            with db_connector() as connection:
                deleted = calendar_store.reset_month_overrides(
                    connection, year, mon, actor=viewer.userid
                )
                calendar_store.rewrite_month(
                    connection, _calendar_seed_path(), year, mon
                )
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web calendar reset failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "deleted": deleted})

    # ------------------------------------------------------------------
    # 任务级收件人覆盖（方案 B，2026-10-12）：只改 @ 名单，不改消息正文
    # ------------------------------------------------------------------
    _service_id_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

    @app.get("/api/pipelines/recipient-override")
    async def recipient_override_get(request: Request):
        """某条服务线的覆盖行（含停用）+ 基名单 +「为什么不在名单」归因。

        抽屉据此展示「有效名单 = 基名单 ∪ include − exclude」三层来源
        （方案 A，2026-10-12；归因为 P2 展示口径）。
        """
        require_capability(request, CAPABILITY_PIPELINES)
        service_id = (request.query_params.get("service_id") or "").strip()
        if not _service_id_re.match(service_id):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                rows = recipient_override.fetch_service_overrides(
                    connection, service_id)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient override read failed: %s",
                type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        # updated_at 是 DB DATETIME（pymysql 返回 datetime 对象），直接进
        # JSONResponse 会 json.dumps 失败 → 500（2026-10-12 线上实测：抽屉报
        # 「读取异常，请稍后重试」）。出口统一用 _fmt_time 归一为北京时间字符串，
        # 与页面其它时间展示口径一致。
        overrides = [
            dict(row, updated_at=_fmt_time(row.get("updated_at")))
            for row in rows
        ]
        # 基名单/归因是附属信息：读取故障只让抽屉少「来源」与「口径说明」
        # 区块，不给整页 503（与 /pipelines 列 fail-soft 同口径）。一次装载
        # 上下文，base 与 explain 共用，避免重复读库。
        base, base_source, explain = [], "", []
        try:
            with db_connector() as connection:
                context = _load_recipient_context(connection, service_id)
            info = _recipient_base(service_id, **context)
            base = [{"user_id": uid, "name": name}
                    for uid, name in info["pairs"]]
            base_source = _base_source_label(info)
            explain = _recipient_explain(
                service_id, base_pairs=info["pairs"], overrides=overrides,
                **context)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient base load failed: %s", type(exc).__name__)
        return JSONResponse(
            {"status": "ok", "service_id": service_id, "base": base,
             "base_source": base_source, "explain": explain,
             "overrides": overrides})

    @app.post("/api/pipelines/recipient-override/add")
    async def recipient_override_add(request: Request):
        """新增/重启用一条覆盖：先解析姓名快照，再落库（同事务审计）。"""
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        mode = payload.get("mode")
        target_type = payload.get("target_type")
        target_value = payload.get("target_value")
        note = payload.get("note") or ""
        if (not isinstance(service_id, str)
                or not _service_id_re.match(service_id.strip())
                or mode not in recipient_override.MODES
                or target_type not in recipient_override.TARGET_TYPES
                or not isinstance(target_value, str) or not target_value.strip()
                or not isinstance(note, str)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        unresolved = []
        resolved = None
        try:
            with db_connector() as connection:
                resolved_rows, unresolved = recipient_override.resolve_targets(
                    connection,
                    [{"target_type": target_type,
                      "target_value": target_value}],
                )
                resolved = resolved_rows[0].get("resolved_user_id")
                recipient_override.upsert_override(
                    connection, service_id.strip(), mode, target_type,
                    target_value, actor=viewer.userid, note=note,
                    resolved_user_id=resolved,
                )
        except recipient_override.RecipientOverrideError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient override add failed: %s",
                type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse(
            {"status": "ok", "resolved_user_id": resolved,
             "unresolved": unresolved})

    @app.post("/api/pipelines/recipient-override/toggle")
    async def recipient_override_toggle(request: Request):
        """停用/启用一条覆盖（按主键 id）。"""
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        payload = await _json_body(request)
        override_id = payload.get("id")
        enabled = payload.get("enabled")
        if (isinstance(override_id, bool) or not isinstance(override_id, int)
                or not isinstance(enabled, bool)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                hit = recipient_override.set_override_enabled(
                    connection, override_id, enabled, actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient override toggle failed: %s",
                type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not hit:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/pipelines/recipient-override/delete")
    async def recipient_override_delete(request: Request):
        """删除一条覆盖（审计留存被删行内容）。"""
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        payload = await _json_body(request)
        override_id = payload.get("id")
        if isinstance(override_id, bool) or not isinstance(override_id, int):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                hit = recipient_override.delete_override(
                    connection, override_id, actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient override delete failed: %s",
                type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not hit:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/pipelines/recipient-override/reset")
    async def recipient_override_reset(request: Request):
        """回退总开关：清空某条服务线全部覆盖（回名册口径）。"""
        viewer = require_capability(request, CAPABILITY_PIPELINES)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        if not isinstance(service_id, str) \
                or not _service_id_re.match(service_id.strip()):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                deleted = recipient_override.reset_service_overrides(
                    connection, service_id.strip(), actor=viewer.userid)
        except recipient_override.RecipientOverrideError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient override reset failed: %s",
                type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "deleted": deleted})

    # -- SPA 单壳 catch-all（注册在最后：未匹配 GET 返回壳；API 等前缀 404）-
    @app.get("/{path:path}")
    def spa_fallback(request: Request, path: str):
        if path.startswith(("api/", "auth/", "static/", "healthz")):
            raise HTTPException(status_code=404, detail="not_found")
        return _spa_shell()

    return app


async def _json_body(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
    return payload


def serve(application):
    import uvicorn
    uvicorn.run(application, host="0.0.0.0", port=8080)


def main():
    """从环境装配并启动；启动失败只打印一行安全消息（同 bi-web 纪律）。"""
    try:
        from common.public_data.pipeline_config import (
            build_config_deleter,
            build_config_publisher,
            build_config_source,
        )
        from common.public_data.scheduler import build_fleet_source

        settings = load_settings()
        session_secret = os.environ.get("BI_WEB_SESSION_SECRET") or None
        app_key = (os.environ.get("BI_DINGTALK_APPKEY") or "").strip()
        app_secret = (os.environ.get("BI_DINGTALK_APPSECRET") or "").strip()
        auth_client = (
            auth.DingTalkAuthClient(app_key, app_secret)
            if session_secret and app_key and app_secret
            else None
        )
        session_secure = os.environ.get(
            "BI_WEB_SESSION_SECURE", ""
        ).strip().lower() in ("1", "true", "yes")
        # 浏览器登录通道（2026-10-14）：非空才开放；凭据与其余 BI_* 同纪律
        # ——宿主机 /opt/dops/ops-web.env 注入，不入库、不入 repo。
        browser_login_token = (
            os.environ.get("OPS_WEB_BROWSER_TOKEN") or ""
        ).strip() or None
        corp_id = (os.environ.get("BI_DINGTALK_CORPID") or "").strip() or None
        agent_id = (os.environ.get("BI_DINGTALK_AGENTID") or "").strip() or None
        config_source = build_config_source()
        try:
            fleet_source = build_fleet_source()
        except Exception:
            # 舰队清单地基（seed）缺失不拖垮权限管理面：定时任务页 503。
            _LOGGER.warning("ops-web fleet source unavailable at startup")
            fleet_source = None
        application = create_app(
            settings=settings,
            session_secret=session_secret,
            auth_client=auth_client,
            session_secure=session_secure,
            browser_login_token=browser_login_token,
            fleet_source=fleet_source,
            config_source=config_source,
            config_publisher=build_config_publisher(),
            config_deleter=build_config_deleter(),
            corp_id=corp_id,
            agent_id=agent_id,
        )
    except Exception as exc:
        print(f"ops-web startup failed: invalid configuration ({type(exc).__name__})")
        sys.exit(1)
    serve(application)


if __name__ == "__main__":  # pragma: no cover
    main()
