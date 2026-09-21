"""ops-web 应用装配：权限管理（grant 增删 + 审计流水 + 批量区域开通）。

访问控制双闸（铁律 8）：

* 网络层：compose 仅回环绑定（127.0.0.1:18100），不对局域网暴露；
* 应用层：除 ``/auth/*`` 外的每个路由都要求 session 身份且
  ``viewer.is_admin``——ops-web 不是面向全员的服务。

输出纪律与 bi-web 一致：错误体只用泛化词汇（unauthorized / forbidden /
unavailable / bad_request），异常只记一条带类名的 WARNING；页面 HTML 的
每一处插值都过 ``html.escape``（管理页同样无反射面）。

写路径（铁律 3 的另一半）：``bi_authz_grant`` 的 INSERT/DELETE 只存在于
本模块，且每次变更与同事务的一行 audit 同生共死。
"""

import html
import logging
import os
import sys
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from common.bi_web import auth, authz
from common.public_data import bi_authz

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
        "<a href=\"/audit\">审计流水</a><a href=\"/auth/logout\">退出</a>"
        f"<span class=\"who\">{who}</span></header><main>"
        + "".join(sections)
        + f"</main><script>{_PAGE_JS}</script></body></html>"
    )


def _esc(value):
    return html.escape("" if value is None else str(value))


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------

def create_app(*, settings, session_secret, db_connector=None, auth_client=None,
               viewer_resolver=None, session_secure=False) -> FastAPI:
    """装配 ops-web；``session_secret`` 必填（ops-web 没有开放模式）。

    依赖全部可注入，测试不需要真实库与网络：``db_connector`` 喂
    FakeConnection，``viewer_resolver`` 喂静态 Viewer，``auth_client``
    喂 fake 交换客户端。
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
        application = create_app(
            settings=settings,
            session_secret=session_secret,
            auth_client=auth_client,
            session_secure=session_secure,
        )
    except Exception as exc:
        print(f"ops-web startup failed: invalid configuration ({type(exc).__name__})")
        sys.exit(1)
    serve(application)


if __name__ == "__main__":  # pragma: no cover
    main()
