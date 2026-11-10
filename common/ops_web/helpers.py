"""ops-web 公共依赖、权限策略与展示辅助。"""

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
from fastapi.encoders import jsonable_encoder
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
    ops_credential,
    ops_token,
    org_assignment_override,
    org_catalog,
    org_proxy_grant,
    phone_registry,
    recipient_override,
    report_roster,
)
from common.daily_robot.inspection_alert import ALERT_SERVICES
from common.ops_web.data_quality import register_data_quality_routes
from common.ops_web.freshness import register_freshness_routes
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
    NOT_FOUND = "not_found"


def load_settings():
    from common.public_data.settings import Settings
    return Settings.from_environment()


def connect(database_settings):
    from common.public_data.db import connect as _connect
    return _connect(database_settings)


def _database_connector(database_settings):
    @contextmanager
    def connector():
        connection = connect(database_settings)
        try:
            yield connection
        finally:
            connection.close()

    return connector


def _mart_connector(settings):
    return _database_connector(settings.mart_database)


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
CAPABILITY_PHONE = "phone"      # 号码台账（号码↔持卡人↔平台账号）

#: 全部能力枚举（/api/me 逐项回报；SPA 菜单与路由守卫据此渲染）。
ALL_CAPABILITIES = (
    CAPABILITY_ORG, CAPABILITY_ROSTER, CAPABILITY_PIPELINES,
    CAPABILITY_AUDIT, CAPABILITY_CALENDAR, CAPABILITY_DATAFIX,
    CAPABILITY_PHONE,
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
        # 测试区（2026-10-19 改制为通用功能模块「播报试投」）不是业务
        # 区域：不进名册/目标页区域列表（它只做播报改投目标）。
        if key == "test":
            continue
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
                   "定时运行", "数据修正", "号码台账")

#: 号码台账审计动作中文标签（专用：upsert 在名册语境译「改判」，在此译
#: 「新增/更新」）。
_PHONE_ACTION_LABEL = {
    "upsert": "新增/更新", "seed": "全量重放", "claim": "认领",
    "enable": "启用", "disable": "停用", "binding_save": "绑定新增/更新",
}

#: 收件人覆盖 mode / target_type 中文标签（展示层）。
_MODE_LABEL = {"include": "追加", "exclude": "移除"}

#: 数据修正 fix_type 中文标签（展示层）。
_FIX_TYPE_LABEL = {
    "offline_person_region_realign": "线下人员归属归位",
}
_TARGET_TYPE_LABEL = {"user_id": "账号", "name": "姓名"}





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

    # 同名部门归并（2026-10-26 裂行修复）：覆盖成员的组带 dept_id、
    # 基名单成员的组只有名字——「分组键取 dept_id or dept_name」会让同
    # 一个部门裂成两行（id 键一行、名字键一行，画布出现两个同名部门）。
    # 按 dept_name 把同名组合并：优先保留带 dept_id 的组（id 与目录
    # sort_order 随之保留）；两组同带 id = 登记脏数据，同样合并（同区域
    # 同名两行本身即误导，展示归一）。
    for region_key, depts in grouped.items():
        if not isinstance(depts, dict):
            continue
        by_name = {}
        for key in list(depts):
            group = depts[key]
            name = group["dept_name"]
            if not name:
                continue
            hit = by_name.get(name)
            if hit is None:
                by_name[name] = key
                continue
            keep_key = hit if depts[hit]["dept_id"] else key
            drop_key = key if keep_key == hit else hit
            keep, drop = depts[keep_key], depts[drop_key]
            keep["members"].extend(drop["members"])
            if not keep["dept_id"] and drop["dept_id"]:
                keep["dept_id"] = drop["dept_id"]
            del depts[drop_key]
            by_name[name] = keep_key

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
            # 播报改投目标（2026-10-19 测试区；None=不改投），画布抽屉开关
            "relay_to": (catalog_regions.get(key) or {}).get("relay_to"),
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


def _broadcast_task_type(service_id):
    """service_id → 播报任务类型（播报编排板的行轴，2026-10-21）。

    按 service_id 家族归一（去掉区域后缀），让同一任务类型跨区域聚成一行。
    """
    if service_id.startswith("robot-check-"):
        return "催办未填人"
    if service_id.startswith("robot-"):
        return "填报提醒"
    if service_id.startswith("leaderboard-"):
        return "完成率榜单"
    if service_id == "offline-daily-summary":
        return "每日汇总日报"
    if service_id.startswith("channel-missing"):
        return "渠道门店校验"
    if service_id.startswith("channel-daily"):
        return "渠道日报"
    return "其他"



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


#: 认证关闭时**读面**的直通 Viewer（2026-10-26 读写分离裁决：查看面匿名
#: 开放、写操作仍要登录会话+授权——SG 全放行的现实下「网内信任」前提
#: 不成立，写面不能裸奔）。审计 actor 记 anon-ops；写操作走正常会话
#: 解析后记真实 userid。回开全闸 = 摘掉 OPS_WEB_AUTH_DISABLED 环境变量。
_OPEN_VIEWER = authz.Viewer(
    userid="anon-ops", name="匿名只读",
    scopes=frozenset(), allowed_regions=frozenset(),
    is_admin=True, admitted=True,
)

async def _json_body(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
    return payload

__all__ = tuple(name for name in globals() if not name.startswith("__"))
