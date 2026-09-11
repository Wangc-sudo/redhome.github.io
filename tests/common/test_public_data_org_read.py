"""Tests for the contact-directory gateway, seed, and member collector."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from common.public_data.org_read import (
    OrgReadError,
    OrgReadGateway,
    collect_members,
    load_org_seed,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_PATH = _REPO_ROOT / "docker" / "integration" / "org.seed.json"


def _seed_file(test_case, document):
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    with handle:
        handle.write(
            document if isinstance(document, str)
            else json.dumps(document, ensure_ascii=False)
        )
    test_case.addCleanup(os.unlink, handle.name)
    return handle.name


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

class OrgReadGatewayTests(unittest.TestCase):

    def _gateway(self, handler):
        calls = []

        def fake(url, method=None, body=None, headers=None, timeout=None):
            calls.append({"url": url, "method": method, "body": body})
            return handler(url, body)

        return OrgReadGateway("k", "s", request_json=fake), calls

    def test_listsub_posts_to_the_oapi_endpoint_with_token(self):
        def handler(url, body):
            return {
                "errcode": 0,
                "result": [{"dept_id": 2, "name": "子部门"}],
            }

        gateway, calls = self._gateway(handler)
        result = gateway.list_sub_departments(1)

        self.assertEqual(result, [(2, "子部门")])
        self.assertEqual(len(calls), 1)
        self.assertIn("/topapi/v2/department/listsub", calls[0]["url"])
        self.assertIn("access_token=test-access-token", calls[0]["url"])
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["body"], {"dept_id": 1})

    def test_listsub_rejects_nonzero_errcode(self):
        gateway, _ = self._gateway(
            lambda url, body: {"errcode": 40035, "errmsg": "secret-detail"}
        )
        with self.assertRaises(OrgReadError) as ctx:
            gateway.list_sub_departments(1)
        # 响应体细节不得进入异常消息（非泄露约定）。
        self.assertNotIn("secret-detail", str(ctx.exception))

    def test_listsub_rejects_non_list_result(self):
        gateway, _ = self._gateway(
            lambda url, body: {"errcode": 0, "result": {"not": "a list"}}
        )
        with self.assertRaises(OrgReadError):
            gateway.list_sub_departments(1)

    def test_listid_paginates_until_has_more_is_false(self):
        pages = {
            0: {"errcode": 0, "result": {
                "list": ["u1", "u2"], "has_more": True, "next_cursor": 7,
            }},
            7: {"errcode": 0, "result": {
                "list": ["u3"], "has_more": False,
            }},
        }
        gateway, calls = self._gateway(
            lambda url, body: pages[body["cursor"]]
        )
        self.assertEqual(gateway.list_user_ids(5), ["u1", "u2", "u3"])
        self.assertEqual([c["body"]["cursor"] for c in calls], [0, 7])
        self.assertIn("/topapi/user/listid", calls[0]["url"])

    def test_listid_rejects_missing_next_cursor(self):
        gateway, _ = self._gateway(
            lambda url, body: {"errcode": 0, "result": {
                "list": ["u1"], "has_more": True,
            }}
        )
        with self.assertRaises(OrgReadError):
            gateway.list_user_ids(5)

    def test_listid_rejects_non_string_user_id(self):
        gateway, _ = self._gateway(
            lambda url, body: {"errcode": 0, "result": {
                "list": [123], "has_more": False,
            }}
        )
        with self.assertRaises(OrgReadError):
            gateway.list_user_ids(5)

    def test_get_user_returns_the_detail_dict(self):
        def handler(url, body):
            self.assertEqual(body, {"userid": "u1", "language": "zh_CN"})
            return {"errcode": 0, "result": {"userid": "u1", "name": "张三"}}

        gateway, calls = self._gateway(handler)
        self.assertEqual(gateway.get_user("u1"), {"userid": "u1", "name": "张三"})
        self.assertIn("/topapi/v2/user/get", calls[0]["url"])

    def test_http_failure_is_non_leaking(self):
        def boom(url, body):
            raise OSError("connection to secret-host refused")

        gateway, _ = self._gateway(boom)
        with self.assertRaises(OrgReadError) as ctx:
            gateway.list_sub_departments(1)
        self.assertNotIn("secret-host", str(ctx.exception))

    def test_dept_id_accepts_digit_strings(self):
        gateway, calls = self._gateway(
            lambda url, body: {"errcode": 0, "result": []}
        )
        gateway.list_sub_departments("1050135497")
        self.assertEqual(calls[0]["body"], {"dept_id": 1050135497})

    def test_dept_id_rejects_garbage(self):
        gateway, _ = self._gateway(
            lambda url, body: {"errcode": 0, "result": []}
        )
        for bad in ("1;DROP", None, 1.5, True):
            with self.assertRaises(OrgReadError):
                gateway.list_sub_departments(bad)


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

class ShippedOrgSeedTests(unittest.TestCase):
    """真正发版的种子：区域顺序即优先级，兜底的 other 必须在最后。"""

    def test_shipped_seed_regions_in_priority_order(self):
        regions = load_org_seed(_SEED_PATH)
        self.assertEqual(
            [region for region, _ in regions],
            ["hangzhou", "shaoxing", "other"],
        )

    def test_shipped_seed_dept_ids_match_the_robot_config(self):
        regions = dict(load_org_seed(_SEED_PATH))
        self.assertEqual(
            regions["hangzhou"],
            (1049728636, 1049886665, 1050078449, 1050062512),
        )
        self.assertEqual(
            regions["shaoxing"], (1050416052, 1049663672, 1050408252),
        )
        # other 含根部门 1050135497，递归展开即全公司——靠顺序兜底。
        self.assertIn(1050135497, regions["other"])


class LoadOrgSeedTests(unittest.TestCase):

    def test_valid_seed_preserves_document_order(self):
        path = _seed_file(self, {
            "version": 1,
            "regions": {"a": [1, 2], "b": [3]},
        })
        self.assertEqual(load_org_seed(path), (("a", (1, 2)), ("b", (3,))))

    def test_rejects_wrong_version(self):
        path = _seed_file(self, {"version": 2, "regions": {"a": [1]}})
        with self.assertRaises(OrgReadError):
            load_org_seed(path)

    def test_rejects_empty_regions(self):
        path = _seed_file(self, {"version": 1, "regions": {}})
        with self.assertRaises(OrgReadError):
            load_org_seed(path)

    def test_rejects_non_integer_dept_id(self):
        path = _seed_file(self, {"version": 1, "regions": {"a": ["1"]}})
        with self.assertRaises(OrgReadError):
            load_org_seed(path)

    def test_rejects_duplicate_dept_id_within_a_region(self):
        path = _seed_file(self, {"version": 1, "regions": {"a": [1, 1]}})
        with self.assertRaises(OrgReadError):
            load_org_seed(path)

    def test_rejects_a_missing_file(self):
        with self.assertRaises(OrgReadError):
            load_org_seed(_REPO_ROOT / "docker" / "integration" / "nope.json")

    def test_rejects_malformed_json(self):
        with self.assertRaises(OrgReadError):
            load_org_seed(_seed_file(self, "{not json"))


# ---------------------------------------------------------------------------
# collect_members
# ---------------------------------------------------------------------------

class _FakeGateway:
    """In-memory directory: subtree + member listings + user details."""

    def __init__(self, *, sub_depts=None, users=None, details=None):
        self._sub_depts = sub_depts or {}
        self._users = users or {}
        self._details = details or {}
        self.listsub_calls = []
        self.listid_calls = []

    def list_sub_departments(self, dept_id):
        self.listsub_calls.append(dept_id)
        return list(self._sub_depts.get(dept_id, []))

    def list_user_ids(self, dept_id):
        self.listid_calls.append(dept_id)
        return list(self._users.get(dept_id, []))

    def get_user(self, user_id):
        return dict(self._details[user_id])


class CollectMembersTests(unittest.TestCase):

    def _gateway(self):
        return _FakeGateway(
            sub_depts={1: [(2, "子部门")], 2: []},
            users={1: ["u1"], 2: ["u1", "u2"]},
            details={
                "u1": {"userid": "u1", "name": "张三"},
                "u2": {"userid": "u2", "name": "李四"},
            },
        )

    def test_expands_subtree_and_dedupes_members(self):
        records = collect_members(self._gateway(), (("r", (1,)),))
        self.assertEqual([r.user_id for r in records], ["u1", "u2"])
        by_id = {r.user_id: r for r in records}
        self.assertEqual(by_id["u1"].dept_id, 1)
        self.assertIsNone(by_id["u1"].dept_name)
        self.assertEqual(by_id["u2"].dept_id, 2)
        self.assertEqual(by_id["u2"].dept_name, "子部门")

    def test_records_carry_region_and_payload(self):
        records = collect_members(self._gateway(), (("hangzhou", (1,)),))
        self.assertTrue(all(r.region == "hangzhou" for r in records))
        self.assertEqual(records[0].payload, {"userid": "u1", "name": "张三"})

    def test_each_dept_is_expanded_once(self):
        gateway = _FakeGateway(
            sub_depts={1: [(2, "b"), (3, "c")], 2: [(3, "c")], 3: []},
            users={1: ["u1"]},
            details={"u1": {"userid": "u1", "name": "张三"}},
        )
        collect_members(gateway, (("r", (1,)),))
        self.assertEqual(sorted(gateway.listsub_calls), [1, 2, 3])

    def test_first_region_wins_on_overlap(self):
        gateway = _FakeGateway(
            sub_depts={1: [], 2: []},
            users={1: ["u1"], 2: ["u1", "u2"]},
            details={
                "u1": {"userid": "u1", "name": "张三"},
                "u2": {"userid": "u2", "name": "李四"},
            },
        )
        records = collect_members(gateway, (
            ("hangzhou", (1,)),
            ("other", (2,)),
        ))
        by_id = {r.user_id: r for r in records}
        # u1 同时被两区展开到，归最早的 hangzhou；u2 只在 other。
        self.assertEqual(by_id["u1"].region, "hangzhou")
        self.assertEqual(by_id["u2"].region, "other")

    def test_zero_members_raises(self):
        gateway = _FakeGateway(users={1: []})
        with self.assertRaisesRegex(OrgReadError, "zero members"):
            collect_members(gateway, (("r", (1,)),))

    def test_member_without_a_name_raises(self):
        gateway = _FakeGateway(
            users={1: ["u1"]},
            details={"u1": {"userid": "u1"}},
        )
        with self.assertRaises(OrgReadError):
            collect_members(gateway, (("r", (1,)),))

    def test_tree_deeper_than_the_limit_raises(self):
        # 无限增长的子树：若无深度上限会失控，有上限则显式失败。
        gateway = _FakeGateway(
            sub_depts={n: [(n + 1, f"d{n + 1}")] for n in range(1, 10)},
            users={1: ["u1"]},
            details={"u1": {"userid": "u1", "name": "张三"}},
        )
        with self.assertRaisesRegex(OrgReadError, "too deep"):
            collect_members(gateway, (("r", (1,)),), max_depth=3)


if __name__ == "__main__":
    unittest.main()
