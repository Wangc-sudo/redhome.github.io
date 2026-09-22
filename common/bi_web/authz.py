"""BI 授权骨架：Viewer 解析 + 准入门 + 页面级 scope 判定（设计稿 §4）。

语义（铁律 1/2，违反即返工）：

* **deny-by-default**——``bi_authz_grant`` 无任何记录的用户
  ``admitted=False``：认证通过也只见「未授权」提示页。admin
  （``grant_type='admin'``）恒准入、恒全 scope。
* **fail-closed**——grant 表读失败、``dim_robot_member`` 读失败、
  记录形态损坏：一律按「未准入 / 无 scope」处理，绝不退化为全员可见。
* admin 自举不以 ``dim_robot_member`` 在册为前提（Q3：总部子树同步前
  admin 不在快照属预期）——admin grant 命中即准入，在职校验只约束
  非 admin 用户。

行级集合（``allowed_regions``）本批只算好不注入任何卡片查询（M3）。
"""

import threading
import time
from dataclasses import dataclass

from common.public_data import bi_authz

#: Viewer 解析结果的缓存窗口（设计稿 §4.3：每请求解析，TTL 60s）。
VIEWER_TTL_SECONDS = 60.0

#: admin 隐式持有的页面 scope（设计稿 §4.3：scopes ∪ ({'fin'} if admin)）。
_ADMIN_IMPLICIT_SCOPES = frozenset({"fin"})


@dataclass(frozen=True)
class Viewer:
    """一个已认证用户的授权视图。

    ``admitted=False`` 即准入门拒绝（deny-by-default / 离职 / 读失败
    的 fail-closed 出口都汇聚到这一个标志）；``allowed_regions`` 本批
    只算好，注入查询层是 M3。
    """

    userid: str
    name: str
    scopes: frozenset
    allowed_regions: frozenset
    is_admin: bool
    admitted: bool


#: 准入门拒绝时的统一 Viewer（fail-closed 的唯一出口形态）。
def denied_viewer(userid):
    return Viewer(
        userid=userid if isinstance(userid, str) else "",
        name="",
        scopes=frozenset(),
        allowed_regions=frozenset(),
        is_admin=False,
        admitted=False,
    )


def build_viewer(userid, grant_rows, member_status):
    """纯函数：grant 记录 + 在职状态 → Viewer（无 IO，全部可离线单测）。

    *grant_rows* 是 ``(grant_type, grant_key)`` 序列；*member_status* 是
    ``(name, is_active)`` 或 ``None``（不在册）。任何形态损坏都按
    fail-closed 出口（denied_viewer）处理。
    """
    if not isinstance(userid, str) or not userid:
        return denied_viewer(userid)
    try:
        rows = [
            (grant_type, grant_key)
            for grant_type, grant_key in grant_rows
            if isinstance(grant_type, str)
            and isinstance(grant_key, str)
            and grant_type in bi_authz.GRANT_TYPES
            and grant_key
        ]
    except (TypeError, ValueError):
        return denied_viewer(userid)

    # admin 恒准入（自举语义：grant 表有 admin 记录即准入，不问在册）。
    if ("admin", bi_authz.ADMIN_GRANT_KEY) in rows:
        name = member_status[0] if member_status else ""
        return Viewer(
            userid=userid,
            name=name,
            scopes=frozenset(_ADMIN_IMPLICIT_SCOPES),
            allowed_regions=frozenset(),
            is_admin=True,
            admitted=True,
        )

    # 非 admin：在职才继续（设计稿 §4.2 is_active=0 自动失效）；
    # 不在册视同未授权（deny-by-default 延伸到组织架构外的人）。
    if not member_status or not member_status[1]:
        return denied_viewer(userid)
    if not rows:
        return denied_viewer(userid)

    scopes = frozenset(key for kind, key in rows if kind == "scope")
    regions = frozenset(key for kind, key in rows if kind == "region")
    return Viewer(
        userid=userid,
        name=member_status[0],
        scopes=scopes,
        allowed_regions=regions,
        is_admin=False,
        admitted=True,
    )


class ViewerResolver:
    """每请求解析 Viewer，TTL 缓存 60s（线程安全，fail-closed）。

    一次解析 = 两次只读查询（grant + 在职状态）。**任何**存储异常都
    不缓存、不放大：直接回答 denied_viewer——权限链 fail-closed（铁律
    2）与配置链 fail-open 的分界就在这一类里。
    """

    def __init__(self, db_connector, *, ttl_seconds=VIEWER_TTL_SECONDS,
                 monotonic=None):
        self._db_connector = db_connector
        self._ttl_seconds = ttl_seconds
        self._monotonic = time.monotonic if monotonic is None else monotonic
        self._lock = threading.Lock()
        self._cache = {}
        self.failures = 0  # 只读计数，供 /diagnostics 级别的观测（聚合，无 PII）

    def resolve(self, userid):
        if not isinstance(userid, str) or not userid:
            return denied_viewer(userid)
        with self._lock:
            now = self._monotonic()
            cached = self._cache.get(userid)
            if cached is not None and now - cached[0] < self._ttl_seconds:
                return cached[1]
        viewer = self._fetch(userid)
        if viewer.admitted:
            # 只缓存准入结果：失败不缓存，下一请求立即重试（与
            # NacosDashboardSource「corrupt 不缓存」同款纪律）。
            with self._lock:
                self._cache[userid] = (self._monotonic(), viewer)
        return viewer

    def _fetch(self, userid):
        try:
            with self._db_connector() as connection:
                grant_rows = bi_authz.fetch_grants(connection, userid)
                member_status = bi_authz.fetch_member_status(connection, userid)
        except Exception:
            self.failures += 1
            return denied_viewer(userid)
        try:
            return build_viewer(userid, grant_rows, member_status)
        except Exception:
            self.failures += 1
            return denied_viewer(userid)


# ---------------------------------------------------------------------------
# 页面级判定（路由守卫与导航过滤共用的纯函数）
# ---------------------------------------------------------------------------

def scope_allows(viewer, required_scope):
    """页面级：无声明 = 已准入即可见；有声明 = 需对应 scope（admin 恒过）。

    ``required_scope`` 形态损坏（非字符串）按 fail-closed 处理——
    视同声明了一个不存在的 scope，只有 admin 能进。
    """
    if viewer is None or not viewer.admitted:
        return False
    if viewer.is_admin:
        return True
    if required_scope is None:
        return True
    if not isinstance(required_scope, str) or not required_scope:
        return False  # 声明损坏：fail-closed
    return required_scope in viewer.scopes
