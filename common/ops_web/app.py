"""ops-web 应用装配：权限管理（grant 增删 + 审计流水 + 批量区域开通）
与定时任务管理（管道列表 / 开关 / 新增 / 立即运行一次）。

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

import html
import json
import logging
import os
import sys
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from common.bi_web import auth, authz
from common.public_data import bi_authz, channel_target, ops_control, report_roster
from common.public_data.pipeline_config import PipelineConfig
from common.public_data.scheduler import has_command

_LOGGER = logging.getLogger(__name__)


class ErrorDetail:
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    UNAVAILABLE = "unavailable"
    BAD_REQUEST = "bad_request"


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


# ---------------------------------------------------------------------------
# 极简服务端渲染（零模板依赖；所有插值过 html.escape）
# ---------------------------------------------------------------------------

_PAGE_STYLE = (
    "body{margin:0;background:#f5f6f8;color:#333;font-family:-apple-system,"
    "'PingFang SC','Microsoft YaHei',sans-serif}"
    "header{background:#1f3a5f;color:#fff;padding:12px 24px;display:flex;"
    "align-items:center;gap:24px}"
    "header a{color:#cfe0f5;text-decoration:none;font-size:14px}"
    "header .who{margin-left:auto;font-size:13px;color:#9fb8d8}"
    "main{padding:24px;max-width:1080px;margin:0 auto}"
    "h1{font-size:18px}h2{font-size:15px;margin-top:32px}"
    "table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}"
    "th,td{border:1px solid #e3e6ea;padding:6px 10px;text-align:left}"
    "th{background:#f0f2f5}"
    "form.inline{display:flex;gap:8px;flex-wrap:wrap;background:#fff;"
    "padding:12px;border:1px solid #e3e6ea;margin-top:12px}"
    "input,select{padding:5px 8px;font-size:13px}"
    "button{padding:6px 14px;font-size:13px;cursor:pointer}"
    ".hint{color:#888;font-size:12px}"
)

_PAGE_JS = """
async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (resp.ok) { location.reload(); return; }
  alert('操作失败（' + resp.status + '）');
}
function submitGrant(ev) {
  ev.preventDefault();
  const f = ev.target;
  postJSON('/api/grants', {
    user_id: f.user_id.value.trim(),
    grant_type: f.grant_type.value,
    grant_key: f.grant_key.value.trim(),
    note: f.note.value.trim(),
  });
}
function revokeGrant(userId, grantType, grantKey) {
  if (!confirm('确认收回该授权？')) return;
  postJSON('/api/grants/delete', {
    user_id: userId, grant_type: grantType, grant_key: grantKey,
  });
}
function grantRegion(ev) {
  ev.preventDefault();
  const region = ev.target.region.value;
  if (!confirm('确认为 ' + region + ' 区域全部在职成员开通该区域授权？')) return;
  postJSON('/api/grants/region', {region: region});
}
function togglePipeline(serviceId, enabled) {
  const action = enabled ? '启用' : '停用';
  if (!confirm('确认' + action + ' ' + serviceId + '？（下一个调度 tick 生效，≤30s）')) return;
  postJSON('/api/pipelines/toggle', {service_id: serviceId, enabled: enabled});
}
function runPipelineOnce(serviceId) {
  if (!confirm('确认立即运行一次 ' + serviceId + '？（调度器下个 tick 认领触发）')) return;
  postJSON('/api/pipelines/run-once', {service_id: serviceId});
}
function addPipeline(ev) {
  ev.preventDefault();
  const f = ev.target;
  postJSON('/api/pipelines/add', {
    service_id: f.service_id.value.trim(),
    kind: f.kind.value,
    schedule: f.schedule.value.trim(),
    enabled: f.enabled.checked,
    depends_on: f.depends_on.value.split(/[\\s,]+/).filter(Boolean),
    description: f.description.value.trim(),
  });
}
function addRoster(ev) {
  ev.preventDefault();
  const f = ev.target;
  postJSON('/api/roster/add', {
    scope: f.scope.value,
    entity_type: f.entity_type.value,
    entity_key: f.entity_key.value.trim(),
    person_name: f.person_name.value.trim(),
    aliases: f.aliases.value.split(/[\\s,，、]+/).filter(Boolean),
    note: f.note.value.trim(),
    role: f.role.value,
  });
}
function toggleRoster(id, enabled) {
  const action = enabled ? '启用' : '停用';
  if (!confirm('确认' + action + '该名册记录？')) return;
  postJSON('/api/roster/toggle', {id: id, enabled: enabled});
}
function deleteRoster(id) {
  if (!confirm('确认删除该名册记录？（审计会留存，日常建议用「停用」）')) return;
  postJSON('/api/roster/delete', {id: id});
}
async function publishSnapshot() {
  if (!confirm('确认发布月度快照？（目标按当前输入覆盖 raw 快照，负责人按名册投影，随后自动触发提取重建榜单）')) return;
  const rows = [];
  document.querySelectorAll('input.roster-target').forEach(function (el) {
    const raw = el.value.trim();
    rows.push({
      store_name: el.dataset.store,
      channel: el.dataset.channel,
      monthly_target: raw === '' ? null : Number(raw),
    });
  });
  const resp = await fetch('/api/roster/publish-snapshot', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({rows: rows}),
  });
  if (resp.ok) { location.reload(); return; }
  alert('发布失败（' + resp.status + '）');
}
"""


def _page(title, *sections, viewer_name=""):
    who = f"当前管理员：{html.escape(viewer_name)}" if viewer_name else ""
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)} · ops-web</title>"
        f"<style>{_PAGE_STYLE}</style></head><body>"
        "<header><strong>ops-web</strong>"
        "<a href=\"/\">成员与授权</a><a href=\"/grants\">授权现状</a>"
        "<a href=\"/audit\">审计流水</a>"
        "<a href=\"/pipelines\">定时任务</a>"
        "<a href=\"/pipelines/audit\">任务审计</a>"
        "<a href=\"/roster\">填报名册</a>"
        "<a href=\"/auth/logout\">退出</a>"
        f"<span class=\"who\">{who}</span></header><main>"
        + "".join(sections)
        + f"</main><script>{_PAGE_JS}</script></body></html>"
    )


def _esc(value):
    return html.escape("" if value is None else str(value))


#: 北京时间（全库时间列约定存 UTC，展示层统一转北京时）。
_BJT = timezone(timedelta(hours=8))


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
_KIND_LABEL = {"apps": "应用线", "business": "业务线"}
_RUN_STATUS_LABEL = {
    "finished": "成功",
    "failed": "失败",
    "rejected": "已拒绝",
    "pending": "等待调度",
    "launched": "运行中",
}
_ACTION_LABEL = {
    "add": "新增", "enable": "启用", "disable": "停用",
    "delete": "删除", "seed": "种子导入", "publish": "发布快照",
}

#: 名册 scope / entity_type 中文标签（填报名册页分组与表单）。
_SCOPE_LABEL = {
    "qudao": "渠道门店",
    "hangzhou": "杭州",
    "shaoxing": "绍兴",
    "junpin": "君品雅院",
    "vanke": "万科&大莲花&团购",
    "offline_all": "线下整体",
    "dining": "餐饮/部门",
}
_ENTITY_LABEL = {"store": "门店", "person": "人员", "dept": "部门"}

#: 名册角色中文标签（v3：负责人/代填报人；仅 store 类型有 deputy）。
_ROLE_LABEL = {"owner": "负责人", "deputy": "代填报人"}


def _label(mapping, value):
    """注册值 → 中文标签；未注册的原样返回（渲染处仍过 _esc）。"""
    if value is None:
        return value
    return mapping.get(value, value)


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
    "dingtalk-gateway": "钉钉网关（outbox 投递 + 互动回调，常驻）",
    "sync-runner": "旧版合并同步（双源，遗留入口）",
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
    "dingtalk-gateway": "钉钉网关",
    "sync-runner": "合并同步（旧）",
    "bi-web": "BI 看板",
    "scheduler": "调度器",
    "ops-web": "运维台",
}


def _service_name(service_id):
    """服务中文名：策展映射优先，robot-/pages- 家族按区域中文名生成。"""
    name = _SERVICE_NAMES.get(service_id)
    if name is not None:
        return name
    for prefix, tpl in (
        ("robot-", "「{region}」日报机器人"),
        ("pages-", "「{region}」榜单页"),
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
    """服务中文说明：策展映射优先，robot-/pages- 家族动态生成，兜底注册表原文。"""
    label = _SERVICE_LABELS.get(service_id)
    if label is not None:
        return label
    for prefix, tpl in (
        ("robot-", "「{region}」日报机器人（报数汇总 → 群内播报/催办）"),
        ("pages-", "「{region}」榜单页生成"),
    ):
        if service_id.startswith(prefix):
            region = service_id[len(prefix):]
            return tpl.format(region=_REGION_LABELS.get(region, region))
    return description


def _cron_zh(schedule):
    """常见 cron 的人性化中文（仅展示；不认识的形态返回 None → 原文显示）。

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
        days = dow.split(",")
        times = _times()
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


def _qudao_section(rows, targets, numbers):
    """渠道门店一体视图（S2）：目标可编辑 + 负责人名册 + 发布快照。

    行 = 店铺（名册链接 ∪ fact 目标表，编号冻结排序）；目标列输入框即
    「草稿」，点「发布月度快照」才落 raw（publishSnapshot JS 收集）。
    """
    store_rows = {}
    for row in rows:
        store_rows.setdefault(row["entity_key"], []).append(row)
    all_stores = sorted(
        set(store_rows) | set(targets.keys()),
        key=lambda store: (numbers.get(store, (None, 10 ** 6))[1] or 10 ** 6,
                           store),
    )
    body = []
    total = 0.0
    for store in all_stores:
        channel, target = targets.get(
            store, (numbers.get(store, ("", None))[0], None)
        )
        store_no = numbers.get(store, (None, None))[1]
        owner_cells = []
        store_links = sorted(
            store_rows.get(store, []),
            key=lambda row: (1 if row.get("role") == "deputy" else 0,
                             row["person_name"]),
        )
        for row in store_links:
            enabled = bool(row["enabled"])
            name_html = _esc(row["person_name"])
            if row.get("role") == "deputy":
                name_html += "<span class=\"hint\">【代填】</span>"
            if row.get("aliases"):
                try:
                    alias_text = "、".join(json.loads(row["aliases"]))
                except (ValueError, TypeError):
                    alias_text = ""
                if alias_text:
                    name_html += f"<span class=\"hint\">（{_esc(alias_text)}）</span>"
            if not enabled:
                name_html = f"<span class=\"hint\">{name_html}(停)</span>"
            toggle_label = "停用" if enabled else "启用"
            owner_cells.append(
                f"{name_html}"
                f"<button onclick=\"toggleRoster({int(row['id'])}, "
                f"{str(not enabled).lower()})\">{toggle_label}</button>"
                f"<button onclick=\"deleteRoster({int(row['id'])})\">删</button>"
            )
        target_value = "" if target is None else f"{float(target):.0f}"
        if target:
            total += float(target)
        body.append(
            f"<tr><td>{_esc(store_no) if store_no else '—'}</td>"
            f"<td>{_esc(store)}</td>"
            f"<td>{_esc(channel)}</td>"
            f"<td><input class=\"roster-target\" data-store=\"{_esc(store)}\" "
            f"data-channel=\"{_esc(channel)}\" value=\"{_esc(target_value)}\" "
            f"placeholder=\"目标（元）\"></td>"
            f"<td>{' '.join(owner_cells) or '<span class=\"hint\">无负责人在册</span>'}</td></tr>"
        )
    return (
        "<h2>渠道门店<span class=\"hint\">（qudao，目标 + 负责人一体管理；"
        "编号已冻结不随目标洗牌）</span></h2>"
        "<table><tr><th>编号</th><th>店铺</th><th>渠道</th>"
        "<th>月目标（元）</th>"
        "<th>负责人/代填报人（停用/删除即改填报权限）</th></tr>"
        + ("".join(body)
           or "<tr><td colspan=\"5\" class=\"hint\">暂无记录</td></tr>")
        + "</table>"
        f"<p>合计：<strong>{total:.0f}</strong> 元 "
        "<button onclick=\"publishSnapshot()\">发布月度快照</button>"
        "<span class=\"hint\">（负责人按名册投影写 raw 快照，自动触发提取重建榜单；"
        "改动未发布前不落库）</span></p>"
    )


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------

def create_app(*, settings, session_secret, db_connector=None, auth_client=None,
               viewer_resolver=None, session_secure=False,
               fleet_source=None, config_source=None,
               config_publisher=None, corp_id=None, agent_id=None) -> FastAPI:
    """装配 ops-web；``session_secret`` 必填（ops-web 没有开放模式）。

    依赖全部可注入，测试不需要真实库与网络：``db_connector`` 喂
    FakeConnection，``viewer_resolver`` 喂静态 Viewer，``auth_client``
    喂 fake 交换客户端。定时任务管理面另有三注入：``fleet_source``
    （零参 callable → service_id 元组）、``config_source``（注册表读）、
    ``config_publisher``（注册表写，None = 写 API 降级 503）。
    """
    if not session_secret:
        raise ValueError("ops-web requires a session secret")
    if db_connector is None:
        db_connector = _mart_connector(settings)
    if viewer_resolver is None:
        viewer_resolver = authz.ViewerResolver(db_connector)

    app = FastAPI(title="ops-web")

    def require_admin(request: Request) -> authz.Viewer:
        """应用层闸：session 有效 + admin；其余一律提示页/403。"""
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
            viewer = viewer_resolver.resolve(userid)
        except Exception as exc:
            # 权限链 fail-closed（铁律 2）：解析异常 = 拒绝。
            _LOGGER.warning("ops-web viewer resolve failed: %s", type(exc).__name__)
            raise HTTPException(status_code=403, detail=ErrorDetail.FORBIDDEN)
        if not viewer.is_admin:
            if request.url.path.startswith("/api/"):
                raise HTTPException(status_code=403, detail=ErrorDetail.FORBIDDEN)
            raise HTTPException(
                status_code=302,
                headers={"Location": "/auth/entry?reason=forbidden"},
            )
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

    # -- 页面 ----------------------------------------------------------------
    @app.get("/")
    def members_page(request: Request):
        viewer = require_admin(request)
        try:
            with db_connector() as connection:
                members = bi_authz.fetch_active_members(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web members load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        rows = "".join(
            f"<tr><td>{_esc(user_id)}</td><td>{_esc(name)}</td>"
            f"<td>{_esc(region)}</td><td>{_esc(dept_name)}</td></tr>"
            for user_id, name, region, dept_name in members
        )
        regions = sorted({region for _uid, _n, region, _d in members})
        region_options = "".join(
            f"<option value=\"{_esc(region)}\">{_esc(region)}</option>"
            for region in regions
        )
        grant_form = (
            "<h2>单人授权</h2>"
            "<form class=\"inline\" onsubmit=\"submitGrant(event)\">"
            "<input name=\"user_id\" placeholder=\"userid\" required>"
            "<select name=\"grant_type\">"
            "<option value=\"scope\">scope（页面）</option>"
            "<option value=\"region\">region（行级）</option>"
            "</select>"
            "<input name=\"grant_key\" placeholder=\"如 fin / hangzhou\" required>"
            "<input name=\"note\" placeholder=\"备注\">"
            "<button type=\"submit\">授权</button></form>"
            "<h2>按区域一键开通</h2>"
            "<form class=\"inline\" onsubmit=\"grantRegion(event)\">"
            f"<select name=\"region\">{region_options}</select>"
            "<button type=\"submit\">该区域全员开通 region 授权</button>"
            "<span class=\"hint\">候选名单即下表在职成员；逐行落审计。</span>"
            "</form>"
        )
        table = (
            "<h1>在职成员</h1>"
            "<table><tr><th>userid</th><th>姓名</th><th>区域</th><th>部门</th></tr>"
            f"{rows}</table>"
        )
        return HTMLResponse(_page("成员与授权", table, grant_form,
                                  viewer_name=viewer.name or viewer.userid))

    @app.get("/grants")
    def grants_page(request: Request):
        viewer = require_admin(request)
        try:
            with db_connector() as connection:
                grants = bi_authz.fetch_all_grants(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web grants load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        rows = []
        for row in grants:
            get = (lambda key: row.get(key)) if isinstance(row, dict) else None
            if get is not None:
                user_id, grant_type = get("user_id"), get("grant_type")
                grant_key, note = get("grant_key"), get("note")
                updated_by, updated_at = get("updated_by"), get("updated_at")
            else:
                (user_id, grant_type, grant_key, note,
                 updated_by, updated_at) = row
            rows.append(
                f"<tr><td>{_esc(user_id)}</td><td>{_esc(grant_type)}</td>"
                f"<td>{_esc(grant_key)}</td><td>{_esc(note)}</td>"
                f"<td>{_esc(updated_by)}</td><td>{_esc(updated_at)}</td>"
                f"<td><button onclick=\"revokeGrant('{_esc(user_id)}',"
                f"'{_esc(grant_type)}','{_esc(grant_key)}')\">收回</button></td></tr>"
            )
        table = (
            "<h1>授权现状</h1>"
            "<table><tr><th>userid</th><th>类型</th><th>对象</th><th>备注</th>"
            "<th>最后操作人</th><th>更新时间</th><th></th></tr>"
            + "".join(rows) + "</table>"
        )
        return HTMLResponse(_page("授权现状", table,
                                  viewer_name=viewer.name or viewer.userid))

    @app.get("/audit")
    def audit_page(request: Request):
        viewer = require_admin(request)
        try:
            with db_connector() as connection:
                entries = bi_authz.fetch_audit_rows(connection)
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
            rows.append(
                f"<tr><td>{_esc(created_at)}</td><td>{_esc(actor)}</td>"
                f"<td>{_esc(action)}</td><td>{_esc(user_id)}</td>"
                f"<td>{_esc(grant_type)}</td><td>{_esc(grant_key)}</td>"
                f"<td>{_esc(note)}</td></tr>"
            )
        table = (
            "<h1>审计流水</h1>"
            "<table><tr><th>时间</th><th>操作人</th><th>动作</th><th>userid</th>"
            "<th>类型</th><th>对象</th><th>备注</th></tr>"
            + "".join(rows) + "</table>"
        )
        return HTMLResponse(_page("审计流水", table,
                                  viewer_name=viewer.name or viewer.userid))

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

    @app.get("/pipelines")
    def pipelines_page(request: Request):
        viewer = require_admin(request)
        service_ids = _fleet()
        configs = []
        for service_id in service_ids:
            try:
                configs.append(config_source.get_pipeline(service_id))
            except Exception as exc:
                # 单条注册表项畸形不拖垮整页：按默认配置展示。
                _LOGGER.warning(
                    "ops-web pipeline config parse failed: %s", type(exc).__name__
                )
                configs.append(PipelineConfig(service_id))
        try:
            with db_connector() as connection:
                recent = ops_control.fetch_recent_requests(connection, limit=100)
        except Exception as exc:
            _LOGGER.warning("ops-web run requests load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        latest = {}
        for row in recent:  # fetch 按 id DESC，首见即最新
            service_id = row.get("service_id") if isinstance(row, dict) else row[1]
            if service_id not in latest:
                latest[service_id] = row
        rows = []
        for config in configs:
            request_row = latest.get(config.service_id)
            if request_row is None:
                request_cell = "—"
            else:
                get = (
                    (lambda key: request_row.get(key))
                    if isinstance(request_row, dict)
                    else (lambda key: request_row[
                        ("id", "service_id", "requested_by", "status", "note",
                         "exit_code", "created_at", "finished_at").index(key)])
                )
                request_cell = (
                    f"{_esc(_label(_RUN_STATUS_LABEL, get('status')))}"
                    f"<span class=\"hint\">（{_esc(get('requested_by'))} "
                    f"{_esc(_fmt_time(get('created_at')))}）</span>"
                )
            template = (
                "✓" if has_command(config.service_id)
                else "<span class=\"hint\">无模板</span>"
            )
            toggle_label = "停用" if config.enabled else "启用"
            run_button = (
                f"<button onclick=\"runPipelineOnce('{_esc(config.service_id)}')\">"
                "运行一次</button>"
                if has_command(config.service_id) else ""
            )
            rows.append(
                f"<tr><td>{_service_name_cell(config.service_id)}</td>"
                f"<td>{_esc(_label(_KIND_LABEL, config.kind))}</td>"
                f"<td>{'✓' if config.enabled else '—'}</td>"
                f"<td>{_schedule_cell(config.schedule)}</td>"
                f"<td>{_esc(', '.join(config.depends_on))}</td>"
                f"<td>{_esc(_service_label(config.service_id, config.description))}</td>"
                f"<td>{template}</td>"
                f"<td>{request_cell}</td>"
                f"<td><button onclick=\"togglePipeline("
                f"'{_esc(config.service_id)}', {str(not config.enabled).lower()})\">"
                f"{toggle_label}</button> {run_button}</td></tr>"
            )
        table = (
            "<h1>定时任务（管道注册表）</h1>"
            "<table><tr><th>服务标识</th><th>类型</th><th>启用</th>"
            "<th>定时规则</th><th>依赖</th><th>描述</th><th>模板</th>"
            "<th>最近运行</th><th>操作</th></tr>"
            + "".join(rows) + "</table>"
            "<p class=\"hint\">配置存 Nacos 注册表（PIPELINES 组），开关与新增在"
            "下一个调度轮询（≤30 秒）生效；「模板」= 调度器能否把该服务标识"
            "翻译成可执行命令。</p>"
        )
        add_form = (
            "<h2>新增定时任务</h2>"
            "<form class=\"inline\" onsubmit=\"addPipeline(event)\">"
            "<input name=\"service_id\" placeholder=\"服务标识（小写字母/数字/-/_）\" required>"
            "<select name=\"kind\">"
            "<option value=\"business\">业务线</option>"
            "<option value=\"apps\">应用线</option>"
            "</select>"
            "<input name=\"schedule\" placeholder=\"定时规则（分 时 日 月 周，如 0 18 * * * = 每天 18:00）\" required>"
            "<label><input type=\"checkbox\" name=\"enabled\" checked> 启用</label>"
            "<input name=\"depends_on\" placeholder=\"依赖的服务标识，逗号分隔（可空）\">"
            "<input name=\"description\" placeholder=\"描述\">"
            "<button type=\"submit\">新增</button></form>"
            "<p class=\"hint\">robot-&lt;区域&gt; / pages-&lt;区域&gt; "
            "家族自动按后缀解析区域，注册即可调度；其他任意标识也可注册，"
            "但需先在调度器命令表加命令模板后才能触发（本页「模板」列可"
            "自查）。已存在的标识请用「操作」列开关，不可重复新增。</p>"
        )
        return HTMLResponse(_page("定时任务", table, add_form,
                                  viewer_name=viewer.name or viewer.userid))

    @app.get("/pipelines/audit")
    def pipeline_audit_page(request: Request):
        viewer = require_admin(request)
        try:
            with db_connector() as connection:
                entries = ops_control.fetch_pipeline_audit(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline audit load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        rows = []
        for row in entries:
            if isinstance(row, dict):
                values = (row.get("actor"), row.get("action"),
                          row.get("service_id"), row.get("detail"),
                          row.get("created_at"))
            else:
                values = row
            actor, action, service_id, detail, created_at = values
            rows.append(
                f"<tr><td>{_esc(_fmt_time(created_at))}</td><td>{_esc(actor)}</td>"
                f"<td>{_esc(_label(_ACTION_LABEL, action))}</td>"
                f"<td>{_esc(service_id)}</td>"
                f"<td>{_esc(detail)}</td></tr>"
            )
        table = (
            "<h1>定时任务审计流水</h1>"
            "<table><tr><th>时间</th><th>操作人</th><th>动作</th>"
            "<th>服务标识</th><th>详情</th></tr>"
            + "".join(rows) + "</table>"
            "<p class=\"hint\">「立即运行一次」不经本表——其发起人"
            "直接落在运行请求行上（定时任务页「最近运行」列）。</p>"
        )
        return HTMLResponse(_page("任务审计", table,
                                  viewer_name=viewer.name or viewer.userid))

    @app.post("/api/pipelines/toggle")
    async def pipeline_toggle(request: Request):
        viewer = require_admin(request)
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
        try:
            config = config_source.get_pipeline(service_id)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            config_publisher(replace(config, enabled=enabled))
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline publish failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            with db_connector() as connection:
                ops_control.insert_pipeline_audit(
                    connection, viewer.userid,
                    "enable" if enabled else "disable", service_id,
                    detail=config.schedule or "",
                )
        except Exception as exc:
            # 发布已生效而审计没落库：变更存在但流水缺失，必须显眼。
            _LOGGER.error(
                "ops-web pipeline audit write failed AFTER publish: %s",
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/pipelines/add")
    async def pipeline_add(request: Request):
        viewer = require_admin(request)
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
        viewer = require_admin(request)
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
    def roster_page(request: Request):
        viewer = require_admin(request)
        try:
            with db_connector() as connection:
                entries = report_roster.fetch_roster(connection)
                audits = report_roster.fetch_roster_audit(connection, limit=20)
                qudao_targets = channel_target.fetch_store_targets(connection)
                qudao_numbers = report_roster.fetch_store_numbers(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web roster load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)

        by_scope = {scope: [] for scope in report_roster.SCOPES}
        for row in entries:
            by_scope.setdefault(row["scope"], []).append(row)

        sections = ["<h1>填报名册（谁可以填哪些店/区域）</h1>"]
        for scope in report_roster.SCOPES:
            rows = by_scope.get(scope) or []
            if scope == "qudao":
                sections.append(
                    _qudao_section(rows, qudao_targets, qudao_numbers)
                )
                continue
            scope_label = _label(_SCOPE_LABEL, scope)
            sections.append(f"<h2>{_esc(scope_label)}"
                            f"<span class=\"hint\">（{_esc(scope)}，{len(rows)} 条）</span></h2>")
            if not rows:
                sections.append("<p class=\"hint\">暂无记录"
                                + ("——该范围当前不拦截（fail-open，维持现状）。"
                                   if scope != "qudao" else
                                   "——渠道机器人回退读月目标表 owners_json。")
                                + "</p>")
                continue
            body = []
            for row in rows:
                aliases_text = ""
                if row.get("aliases"):
                    try:
                        aliases_text = "、".join(json.loads(row["aliases"]))
                    except (ValueError, TypeError):
                        aliases_text = ""
                enabled = bool(row["enabled"])
                toggle_label = "停用" if enabled else "启用"
                status_cell = "✓" if enabled else "<span class=\"hint\">已停用</span>"
                updated = _fmt_time(row.get("updated_at")) or "—"
                role_text = (
                    _label(_ROLE_LABEL, row.get("role") or "owner")
                    if row["entity_type"] == "store" else "—"
                )
                body.append(
                    f"<tr><td>{_esc(_label(_ENTITY_LABEL, row['entity_type']))}</td>"
                    f"<td>{_esc(row['entity_key'])}</td>"
                    f"<td>{_esc(row['person_name'])}</td>"
                    f"<td>{_esc(role_text)}</td>"
                    f"<td>{_esc(aliases_text)}</td>"
                    f"<td>{status_cell}</td>"
                    f"<td>{_esc(row.get('note'))}</td>"
                    f"<td><span class=\"hint\">{_esc(updated)}</span></td>"
                    f"<td><button onclick=\"toggleRoster("
                    f"{int(row['id'])}, {str(not enabled).lower()})\">{toggle_label}</button> "
                    f"<button onclick=\"deleteRoster({int(row['id'])})\">删除</button></td></tr>"
                )
            sections.append(
                "<table><tr><th>类型</th><th>对象</th><th>填报人</th>"
                "<th>角色</th><th>别名</th>"
                "<th>启用</th><th>备注</th><th>更新</th><th>操作</th></tr>"
                + "".join(body) + "</table>"
            )

        scope_options = "".join(
            f"<option value=\"{_esc(scope)}\">{_esc(_label(_SCOPE_LABEL, scope))}</option>"
            for scope in report_roster.SCOPES
        )
        entity_options = "".join(
            f"<option value=\"{_esc(entity)}\">{_esc(_label(_ENTITY_LABEL, entity))}</option>"
            for entity in report_roster.ENTITY_TYPES
        )
        add_form = (
            "<h2>新增名册记录</h2>"
            "<form class=\"inline\" onsubmit=\"addRoster(event)\">"
            f"<select name=\"scope\">{scope_options}</select>"
            f"<select name=\"entity_type\">{entity_options}</select>"
            "<input name=\"entity_key\" placeholder=\"对象（店名/部门名/区域）\" required>"
            "<input name=\"person_name\" placeholder=\"填报人姓名\" required>"
            "<select name=\"role\">"
            "<option value=\"owner\">负责人</option>"
            "<option value=\"deputy\">代填报人</option>"
            "</select>"
            "<input name=\"aliases\" placeholder=\"别名，逗号分隔（可空）\">"
            "<input name=\"note\" placeholder=\"备注（可空）\">"
            "<button type=\"submit\">新增</button></form>"
            "<p class=\"hint\">渠道门店（qudao）：管理「谁可以填哪些店」，渠道机器人"
            "实时生效；日报区域（杭州/绍兴等）：该范围一旦有人名记录即启用白名单"
            "（只准在册人员填报），无记录则不拦截；餐饮/部门先登记，机器人后续接入。"
            "「删除」会留审计，日常调整建议用「停用」。"
            "<strong>人名一律用通讯录本名（不用花名/昵称）</strong>；"
            "别名仅用于群昵称与本名不一致的匹配容错。"
            "角色：负责人占业绩归属，代填报人仅可填报不占业绩；"
            "改角色 = 用新角色重新「新增」同人同对象（覆盖生效）。</p>"
        )
        audit_rows = "".join(
            f"<tr><td>{_esc(_fmt_time(row['created_at']))}</td>"
            f"<td>{_esc(row['actor'])}</td>"
            f"<td>{_esc(_label(_ACTION_LABEL, row['action']))}</td>"
            f"<td>{_esc(_label(_SCOPE_LABEL, row['scope']))}</td>"
            f"<td>{_esc(row['entity_key'])}</td>"
            f"<td>{_esc(row['person_name'])}</td>"
            f"<td>{_esc(row.get('detail'))}</td></tr>"
            for row in audits
        )
        audit_table = (
            "<h2>名册变更流水（最近 20 条）</h2>"
            "<table><tr><th>时间</th><th>操作人</th><th>动作</th><th>范围</th>"
            "<th>对象</th><th>填报人</th><th>详情</th></tr>"
            + (audit_rows or "<tr><td colspan=\"7\" class=\"hint\">暂无变更</td></tr>")
            + "</table>"
        )
        return HTMLResponse(_page("填报名册", "".join(sections), add_form,
                                  audit_table,
                                  viewer_name=viewer.name or viewer.userid))

    @app.post("/api/roster/add")
    async def roster_add(request: Request):
        viewer = require_admin(request)
        payload = await _json_body(request)
        aliases = payload.get("aliases") or []
        if not isinstance(aliases, list):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            entry = report_roster.validate_roster_fields(
                payload.get("scope"),
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
                report_roster.upsert_roster_entry(
                    connection, entry, actor=viewer.userid
                )
        except Exception as exc:
            _LOGGER.warning("ops-web roster add failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/roster/toggle")
    async def roster_toggle(request: Request):
        viewer = require_admin(request)
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
        viewer = require_admin(request)
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
            fleet_source=fleet_source,
            config_source=config_source,
            config_publisher=build_config_publisher(),
            corp_id=corp_id,
            agent_id=agent_id,
        )
    except Exception as exc:
        print(f"ops-web startup failed: invalid configuration ({type(exc).__name__})")
        sys.exit(1)
    serve(application)


if __name__ == "__main__":  # pragma: no cover
    main()
