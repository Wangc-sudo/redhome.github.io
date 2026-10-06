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
import logging
import os
import sys
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import replace

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from common.bi_web import auth, authz
from common.public_data import bi_authz, ops_control
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
        "<a href=\"/auth/logout\">退出</a>"
        f"<span class=\"who\">{who}</span></header><main>"
        + "".join(sections)
        + f"</main><script>{_PAGE_JS}</script></body></html>"
    )


def _esc(value):
    return html.escape("" if value is None else str(value))


# -- 展示层中文标签（底层值不动：审计/状态存量英文值仅在渲染时映射） --------
_KIND_LABEL = {"apps": "应用线", "business": "业务线"}
_RUN_STATUS_LABEL = {
    "finished": "成功",
    "failed": "失败",
    "rejected": "已拒绝",
    "pending": "等待调度",
    "launched": "运行中",
}
_ACTION_LABEL = {"add": "新增", "enable": "启用", "disable": "停用"}


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
            return tpl.format(region=service_id[len(prefix):])
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
                    f"{_esc(get('created_at'))}）</span>"
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
                f"<tr><td>{_esc(config.service_id)}</td>"
                f"<td>{_esc(config.kind)}</td>"
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
                f"<tr><td>{_esc(created_at)}</td><td>{_esc(actor)}</td>"
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
