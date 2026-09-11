"""钉钉通讯录只读网关（spec §10「组织成员：走 API 直连（B 方案）」）。

只读三个**已实测开通**的接口，网关不含任何写方法：

* ``topapi/v2/department/listsub`` —— 某部门的直接子部门
* ``topapi/user/listid``          —— 某部门直属成员的 userId（cursor 分页）
* ``topapi/v2/user/get``          —— 单个成员的姓名等详情

区域归属规则不在网关里，而在 :func:`collect_members`：给定网关即纯函数，
所以测试不需要 HTTP 或凭据。所有异常消息都不得包含 URL、请求体或响应体。
"""

import time
import urllib.parse
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

from common.dingtalk.client import _http_json


_TOKEN_CACHE_SECONDS = 100 * 60

_OAPI_BASE = "https://oapi.dingtalk.com"
_LISTSUB_PATH = "/topapi/v2/department/listsub"
_LISTID_PATH = "/topapi/user/listid"
_USER_GET_PATH = "/topapi/v2/user/get"

#: ``listid`` 的单页上限（钉钉文档允许 1..100）。
_DEFAULT_PAGE_SIZE = 100
#: 通讯录递归展开的层数上限——超过即视为数据异常，绝不死循环。
_MAX_TREE_DEPTH = 20


class OrgReadError(RuntimeError):
    """通讯录读取失败（消息不得包含凭据、URL、请求体或响应体）。"""


@dataclass(frozen=True)
class OrgMemberRecord:
    """一名成员在一次展开中的归属事实。"""

    user_id: str
    name: str
    region: str
    dept_id: int
    dept_name: str | None
    payload: dict


class OrgReadGateway:
    """钉钉通讯录的只读 API 面。

    与 :class:`DingTalkReadGateway` 同样的注入约定：``request_json`` 为
    ``None`` 时走真实 HTTP 与真实 token，否则走注入的 fake 与固定测试
    token——生产代码路径与测试代码路径仅差这一个可替换点。
    """

    def __init__(self, app_key, app_secret, request_json=None):
        self._app_key = app_key
        self._app_secret = app_secret
        self._request_json = _http_json if request_json is None else request_json
        self._token_provider = (
            self._request_access_token
            if request_json is None
            else self._test_access_token
        )
        self._token = None
        self._token_timestamp = 0.0

    # ------------------------------------------------------------------
    # Public API surface（只读，三个端点一一对应）
    # ------------------------------------------------------------------

    def list_sub_departments(self, dept_id):
        """返回 *dept_id* 的直接子部门 ``[(dept_id, name|None), ...]``。"""
        result = self._oapi_result(
            _LISTSUB_PATH, {"dept_id": self._validate_dept_id(dept_id)}
        )
        if not isinstance(result, list):
            raise OrgReadError("org read failed")
        departments = []
        for item in result:
            if not isinstance(item, Mapping):
                raise OrgReadError("org read failed")
            sub_id = item.get("dept_id")
            if not isinstance(sub_id, int) or isinstance(sub_id, bool):
                raise OrgReadError("org read failed")
            name = item.get("name")
            departments.append(
                (sub_id, name if isinstance(name, str) and name else None)
            )
        return departments

    def list_user_ids(self, dept_id, *, page_size=_DEFAULT_PAGE_SIZE, max_pages=100):
        """返回 *dept_id* 直属成员的 userId 列表（自动翻完 cursor 分页）。"""
        dept_id = self._validate_dept_id(dept_id)
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise OrgReadError("org read failed")

        user_ids = []
        cursor = 0
        page = 0
        while True:
            page += 1
            result = self._oapi_result(
                _LISTID_PATH,
                {"dept_id": dept_id, "cursor": cursor, "size": page_size},
            )
            if not isinstance(result, Mapping):
                raise OrgReadError("org read failed")
            batch = result.get("list")
            if not isinstance(batch, list):
                raise OrgReadError("org read failed")
            for user_id in batch:
                if not isinstance(user_id, str) or not user_id:
                    raise OrgReadError("org read failed")
            user_ids.extend(batch)

            if result.get("has_more") is not True:
                return user_ids
            if page >= max_pages:
                raise OrgReadError("org read pagination incomplete")
            next_cursor = result.get("next_cursor")
            if not isinstance(next_cursor, int) or isinstance(next_cursor, bool):
                raise OrgReadError("org read failed")
            cursor = next_cursor

    def get_user(self, user_id):
        """返回 *user_id* 的详情字典（含 ``name``）。"""
        if not isinstance(user_id, str) or not user_id:
            raise OrgReadError("org read failed")
        result = self._oapi_result(
            _USER_GET_PATH, {"userid": user_id, "language": "zh_CN"}
        )
        if not isinstance(result, Mapping):
            raise OrgReadError("org read failed")
        return dict(result)

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _oapi_result(self, path, body):
        url = (
            f"{_OAPI_BASE}{path}?access_token="
            + urllib.parse.quote(self._get_access_token(), safe="")
        )
        try:
            response = self._request_json(url, method="POST", body=body)
        except Exception:
            raise OrgReadError("org read failed") from None
        if not isinstance(response, Mapping):
            raise OrgReadError("org read failed")
        if response.get("errcode") != 0:
            raise OrgReadError("org read failed")
        return response.get("result")

    def _get_access_token(self):
        if (
            self._token is not None
            and time.time() - self._token_timestamp < _TOKEN_CACHE_SECONDS
        ):
            return self._token
        token = self._token_provider()
        if not isinstance(token, str) or not token:
            raise OrgReadError("org read token failed")
        self._token = token
        self._token_timestamp = time.time()
        return token

    def _request_access_token(self):
        try:
            response = self._request_json(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                method="POST",
                body={"appKey": self._app_key, "appSecret": self._app_secret},
            )
        except Exception:
            raise OrgReadError("org read token failed") from None
        if not isinstance(response, Mapping):
            raise OrgReadError("org read token failed")
        return response.get("accessToken")

    @staticmethod
    def _test_access_token():
        return "test-access-token"

    @staticmethod
    def _validate_dept_id(dept_id):
        if isinstance(dept_id, bool):
            raise OrgReadError("org read failed")
        if isinstance(dept_id, int):
            return dept_id
        if isinstance(dept_id, str) and dept_id.isdigit():
            return int(dept_id)
        raise OrgReadError("org read failed")


# ---------------------------------------------------------------------------
# 区域 → 部门 种子（版本受控，非凭据；sync 与 extract 两线都可读）
# ---------------------------------------------------------------------------

def load_org_seed(path):
    """解析版本受控的「区域 → 顶层部门 ID」种子。

    形态::

        {"version": 1,
         "regions": {"hangzhou": [1049728636], "shaoxing": [...], "other": [...]}}

    返回 ``((region, (dept_id, ...)), ...)``，**保持文档顺序**——顺序即区域
    优先级（见 :func:`collect_members`）。部门 ID 是递归展开的*起点*：子
    部门自动纳入，新设部门不再需要手工补登记（spec §10 的父子混列隐患）。
    """
    import json
    from pathlib import Path

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OrgReadError("org seed is not a readable JSON file") from exc
    if not isinstance(raw, dict):
        raise OrgReadError("org seed must be a JSON object")
    if raw.get("version") != 1:
        raise OrgReadError("org seed version must be 1")

    regions = raw.get("regions")
    if not isinstance(regions, dict) or not regions:
        raise OrgReadError("org seed regions must be a non-empty object")

    result = []
    for region, dept_ids in regions.items():
        if not isinstance(region, str) or not region:
            raise OrgReadError("org seed region names must be non-empty strings")
        if not isinstance(dept_ids, list) or not dept_ids:
            raise OrgReadError(f"org seed region {region!r} needs a non-empty dept list")
        seen = set()
        parsed = []
        for dept_id in dept_ids:
            if not isinstance(dept_id, int) or isinstance(dept_id, bool) or dept_id <= 0:
                raise OrgReadError(
                    f"org seed region {region!r} dept ids must be positive integers"
                )
            if dept_id in seen:
                raise OrgReadError(f"org seed region {region!r} repeats dept id")
            seen.add(dept_id)
            parsed.append(dept_id)
        result.append((region, tuple(parsed)))
    return tuple(result)


# ---------------------------------------------------------------------------
# 部门子树 → 成员（给定网关即纯函数）
# ---------------------------------------------------------------------------

def collect_members(gateway, regions, *, max_depth=_MAX_TREE_DEPTH):
    """把每个区域的部门子树展开为成员记录。

    *regions* 是 ``(region, dept_ids)`` 对，**顺序即优先级**：一名成员同时
    出现在多个区域的展开结果里时，归最早的区域（兜底区域 ``other`` 应排在
    最后——它通常含根部门，展开即全公司）。

    显式失败、绝不静默降级：
      * 成员详情缺 ``name`` → :class:`OrgReadError`；
      * 展开结果为零成员 → :class:`OrgReadError`（真实目录不会为空）；
      * 部门树超过 *max_depth* 层 → :class:`OrgReadError`（视为数据异常）。
    """
    records = []
    claimed = set()

    for region, dept_ids in regions:
        dept_names = {}
        queue = deque((dept_id, None) for dept_id in dept_ids)
        seen_depts = set()
        depth = 0

        while queue:
            depth += 1
            if depth > max_depth:
                raise OrgReadError("org department tree is too deep")
            next_queue = deque()
            for dept_id, known_name in queue:
                if dept_id in seen_depts:
                    continue
                seen_depts.add(dept_id)
                dept_name = known_name or dept_names.get(dept_id)

                for user_id in gateway.list_user_ids(dept_id):
                    if user_id in claimed:
                        continue
                    claimed.add(user_id)
                    user = gateway.get_user(user_id)
                    member_name = user.get("name")
                    if not isinstance(member_name, str) or not member_name:
                        raise OrgReadError("org member has no name")
                    records.append(OrgMemberRecord(
                        user_id=user_id,
                        name=member_name,
                        region=region,
                        dept_id=dept_id,
                        dept_name=dept_name,
                        payload=dict(user),
                    ))

                for sub_id, sub_name in gateway.list_sub_departments(dept_id):
                    if sub_name:
                        dept_names.setdefault(sub_id, sub_name)
                    next_queue.append((sub_id, sub_name))
            queue = next_queue

    if not records:
        raise OrgReadError("org directory returned zero members")
    return tuple(records)
