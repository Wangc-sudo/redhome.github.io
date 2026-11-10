"""test_ops_web_app 的 org 域测试；断言自原文件等价迁移。"""

from tests.common.ops_web_test_support import *


class MembersPageTests(unittest.TestCase):
    def test_member_grants_serves_spa_shell(self):
        """「成员与授权」已停用（2026-10-13），旧书签路由与 /grants 同壳。

        2026-10-14 全 SPA 迁移：停用引导提示（「已合并至…」+ 链接）已由
        Vue 页承载，SSR 提示删除，仅钉壳路由。
        """
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        assert_spa_shell(client.get("/member-grants", follow_redirects=False))

    def test_members_page_no_longer_reads_db(self):
        """停用后页面不读成员表，DB 故障不再 503。"""
        client = TestClient(
            _app(viewer=_admin_viewer(), connector_error=RuntimeError("down"))
        )
        _login(client)
        response = client.get("/member-grants")
        self.assertEqual(200, response.status_code)


class OrgAssignmentApiTests(unittest.TestCase):
    """组织编排 SPA 后端（2026-10-13 P6）：/members 壳 + /api/org/*。

    覆盖：/api/org/* admin 闸、overview 结构（区域/部门/池）、覆盖读
    fail-soft、成员读 503、assign/toggle/delete/reset 落库与审计、参数校验。
    /members 壳路由不设闸（2026-10-14 全 SPA 迁移）。
    """

    def _admin_client(self, connection):
        client = TestClient(_app(viewer=_admin_viewer(), connection=connection))
        _login(client)
        return client

    def _members_conn(self, rows=None, **kwargs):
        base = {"dim_robot_member": _MEMBERS}
        base.update(rows or {})
        return _OrgConn(base, **kwargs)

    # -- / 与 /members（SPA 壳）---------------------------------------------

    def test_root_serves_org_spa_shell(self):
        """B3（2026-10-13）：登录后首页 / 即「组织编排」SPA 壳（登录即见拖拽树）。"""
        import tempfile
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".html", delete=False, encoding="utf-8")
        handle.write("<!DOCTYPE html><div id=\"app\"></div>")
        handle.close()
        try:
            with mock.patch("common.ops_web.app._ORG_SPA_INDEX", handle.name):
                client = self._admin_client(_OrgConn({}))
                response = client.get("/")
            self.assertEqual(200, response.status_code)
            self.assertIn("text/html", response.headers["content-type"])
            self.assertIn("id=\"app\"", response.text)
        finally:
            import os
            os.unlink(handle.name)

    def test_members_spa_serves_shell_without_session(self):
        """2026-10-14 全 SPA 迁移：壳路由不设应用层闸（前端 Router 经
        /api/me 跳登录），未登录亦返回单壳 200；数据面 /api/org/* 仍
        fail-closed（另有 overview 闸用例覆盖）。"""
        client = TestClient(_app(viewer=_admin_viewer()))
        assert_spa_shell(client.get("/members", follow_redirects=False))

    def test_members_spa_serves_shell_for_non_admin(self):
        """同上：非 admin 亦得壳 200；页面级 forbidden 302 已随 SSR 下线。"""
        viewer = authz.Viewer(
            userid="u1", name="张三", scopes=frozenset(),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        client = TestClient(_app(viewer=viewer))
        _login(client, "u1")
        assert_spa_shell(client.get("/members", follow_redirects=False))

    def test_members_spa_serves_shell_when_built(self):
        import tempfile
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".html", delete=False, encoding="utf-8")
        handle.write("<!DOCTYPE html><div id=\"app\"></div>")
        handle.close()
        try:
            with mock.patch("common.ops_web.app._ORG_SPA_INDEX", handle.name):
                client = self._admin_client(_OrgConn({}))
                response = client.get("/members")
            self.assertEqual(200, response.status_code)
            self.assertIn("text/html", response.headers["content-type"])
            self.assertIn("id=\"app\"", response.text)
        finally:
            import os
            os.unlink(handle.name)

    def test_members_spa_503_when_build_missing(self):
        with mock.patch("common.ops_web.app._ORG_SPA_INDEX",
                        "Z:/nonexistent/ops-web/dist/index.html"):
            client = self._admin_client(_OrgConn({}))
            response = client.get("/members")
        self.assertEqual(503, response.status_code)

    # -- /api/org/overview --------------------------------------------------

    def test_overview_gate(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        self.assertEqual(401, client.get("/api/org/overview").status_code)

    def test_overview_forbidden_for_non_admin(self):
        viewer = authz.Viewer(
            userid="u1", name="张三", scopes=frozenset(),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        client = TestClient(_app(viewer=viewer))
        _login(client, "u1")
        self.assertEqual(403, client.get("/api/org/overview").status_code)

    def test_overview_groups_base_members(self):
        client = self._admin_client(self._members_conn())
        data = client.get("/api/org/overview").json()

        self.assertEqual("ok", data["status"])
        regions = {region["key"]: region for region in data["regions"]}
        hangzhou = {
            member["name"]
            for group in regions["hangzhou"]["depts"]
            for member in group["members"]
        }
        self.assertEqual({"张三", "王五"}, hangzhou)
        hq = {
            member["name"]
            for group in regions["hq"]["depts"]
            for member in group["members"]
        }
        self.assertEqual({"李四"}, hq)
        self.assertEqual("杭州", regions["hangzhou"]["label"])
        self.assertEqual("总部", regions["hq"]["label"])
        self.assertEqual([], data["pool"])
        self.assertEqual([], data["overrides"])

    def test_overview_applies_override_move_region_and_dept(self):
        client = self._admin_client(self._members_conn(
            {"dim_org_assignment_override": [_ORG_OVERRIDE_ROW]}))
        data = client.get("/api/org/overview").json()

        regions = {region["key"]: region for region in data["regions"]}
        hangzhou = {
            member["user_id"]
            for group in regions["hangzhou"]["depts"]
            for member in group["members"]
        }
        self.assertEqual({"u-zhang"}, hangzhou)          # 王五已移出杭州
        hq_groups = {group["dept_name"]: group["members"]
                     for group in regions["hq"]["depts"]}
        self.assertEqual({"u-li", "u-wang"},              # 王五移入总部
                         {m["user_id"] for m in hq_groups["总经办"]})
        self.assertEqual(1, len(data["overrides"]))
        self.assertEqual("总部", data["overrides"][0]["region_label"])

    def test_overview_override_region_null_goes_to_pool(self):
        row = dict(_ORG_OVERRIDE_ROW, user_id="u-li", name="李四",
                   region=None, dept_name=None)
        client = self._admin_client(self._members_conn(
            {"dim_org_assignment_override": [row]}))
        data = client.get("/api/org/overview").json()

        self.assertEqual(["u-li"], [m["user_id"] for m in data["pool"]])
        self.assertNotIn("hq", {region["key"] for region in data["regions"]})

    def test_overview_override_read_fails_soft(self):
        conn = self._members_conn(fail={"dim_org_assignment_override"})
        client = self._admin_client(conn)
        response = client.get("/api/org/overview")

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual([], data["overrides"])           # 覆盖读失败 = 空
        self.assertIn("hangzhou",
                      {region["key"] for region in data["regions"]})

    def test_overview_members_read_503(self):
        conn = self._members_conn(fail={"dim_robot_member"})
        client = self._admin_client(conn)
        self.assertEqual(503, client.get("/api/org/overview").status_code)

    # -- 写面 ---------------------------------------------------------------

    def test_assign_requires_admin(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        self.assertEqual(
            401, client.post("/api/org/assign",
                             json={"user_id": "u-zhang"}).status_code)

    def test_assign_writes_override_and_audit(self):
        conn = self._members_conn()
        client = self._admin_client(conn)
        response = client.post("/api/org/assign", json={
            "user_id": "u-zhang", "region": "hq", "dept_id": "d9",
            "dept_name": "总经办", "name": "张三"})

        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok"}, response.json())
        sqls = [sql for sql, _ in conn.executed]
        self.assertTrue(any(
            "INSERT INTO `dim_org_assignment_override`" in sql
            and "ON DUPLICATE KEY UPDATE" in sql for sql in sqls))
        audit = next(p for sql, p in conn.executed
                     if "dim_org_assignment_override_audit" in sql)
        self.assertEqual(ADMIN_USERID, audit[0])
        self.assertEqual("assign", audit[1])
        self.assertEqual(1, conn.commits)                 # 审计同事务

    def test_assign_null_region_moves_out(self):
        conn = self._members_conn()
        client = self._admin_client(conn)
        response = client.post(
            "/api/org/assign", json={"user_id": "u-li", "region": None})
        self.assertEqual(200, response.status_code)
        audit = next(p for sql, p in conn.executed
                     if "dim_org_assignment_override_audit" in sql)
        self.assertEqual("移出全部区域", audit[7])

    def test_assign_rejects_missing_user_id(self):
        client = self._admin_client(self._members_conn())
        response = client.post("/api/org/assign", json={"region": "hq"})
        self.assertEqual(400, response.status_code)

    def test_assign_rejects_bad_region_type(self):
        client = self._admin_client(self._members_conn())
        response = client.post(
            "/api/org/assign", json={"user_id": "u-zhang", "region": 5})
        self.assertEqual(400, response.status_code)

    def test_assign_rejects_illegal_region_key(self):
        conn = self._members_conn()
        client = self._admin_client(conn)
        response = client.post(
            "/api/org/assign", json={"user_id": "u-zhang", "region": "Bad Key"})
        self.assertEqual(400, response.status_code)
        self.assertEqual(0, conn.commits)

    def test_toggle_hit_and_miss(self):
        rows = {"dim_org_assignment_override": [_ORG_OVERRIDE_ROW]}
        hit = self._admin_client(self._members_conn(rows, rowcount=1))
        self.assertEqual(
            200, hit.post("/api/org/toggle",
                          json={"user_id": "u-wang", "enabled": False}
                          ).status_code)
        miss = self._admin_client(self._members_conn(rows, rowcount=0))
        self.assertEqual(
            400, miss.post("/api/org/toggle",
                           json={"user_id": "u-wang", "enabled": False}
                           ).status_code)

    def test_toggle_rejects_bad_body(self):
        client = self._admin_client(self._members_conn())
        self.assertEqual(
            400, client.post("/api/org/toggle",
                             json={"user_id": "u-wang", "enabled": "yes"}
                             ).status_code)

    def test_delete_hit_and_miss(self):
        hit = self._admin_client(self._members_conn(
            {"dim_org_assignment_override": [_ORG_OVERRIDE_ROW]}))
        self.assertEqual(
            200, hit.post("/api/org/delete",
                          json={"user_id": "u-wang"}).status_code)
        miss = self._admin_client(self._members_conn())
        self.assertEqual(
            400, miss.post("/api/org/delete",
                           json={"user_id": "u-wang"}).status_code)

    def test_reset_clears_all(self):
        conn = self._members_conn(rowcount=3)
        client = self._admin_client(conn)
        response = client.post("/api/org/reset", json={})
        self.assertEqual(200, response.status_code)
        self.assertEqual(3, response.json()["deleted"])
        audit = next(p for sql, p in conn.executed
                     if "dim_org_assignment_override_audit" in sql)
        self.assertEqual("reset", audit[1])

    # -- /api/org/move（归属搬迁原子复合写，2026-10-15 闭环画布）---------------

    _ROSTER_SQL_KEY = "ORDER BY `scope`, `entity_type`"
    _PERSON_NAMES_SQL_KEY = "`entity_type` = 'person' AND `enabled` = 1"
    _ROSTER_BY_ID_SQL_KEY = "WHERE `id` = %s"

    @staticmethod
    def _roster_row(row_id, scope, person_name, entity_key="门店一部"):
        return {
            "id": row_id, "scope": scope, "entity_type": "person",
            "entity_key": entity_key, "person_name": person_name,
            "aliases": None, "enabled": 1, "note": None, "channel": None,
            "store_no": None, "role": "owner", "updated_by": "ops",
            "updated_at": None,
        }

    def test_move_syncs_both_roster_sides_atomically(self):
        """hangzhou→hq：覆盖写 + 删旧区名册 + 补新区名册，单事务一次 commit。"""
        from_roster = [self._roster_row(7, "hangzhou", "王五", "门店二部")]

        def roster_router(sql, params):
            scope = params[0] if params else None
            return from_roster if scope == "hangzhou" else []

        conn = self._members_conn({
            "dim_org_region": [{"region_key": "hangzhou"},
                               {"region_key": "hq"}],
            self._ROSTER_SQL_KEY: roster_router,
            self._PERSON_NAMES_SQL_KEY: [{"person_name": "李四"}],
            self._ROSTER_BY_ID_SQL_KEY: from_roster,
        })
        response = self._admin_client(conn).post("/api/org/move", json={
            "user_id": "u-wang", "to_region": "hq", "person_name": "王五",
            "from_region": "hangzhou", "dept_name": "总经办"})

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {"status": "ok", "moved": True,
             "roster": {"from_removed": True, "to_added": True}, "audit": True},
            response.json())
        sqls = [sql for sql, _ in conn.executed]
        self.assertTrue(any("INSERT INTO `dim_report_roster`" in s for s in sqls))
        self.assertTrue(any("DELETE FROM `dim_report_roster`" in s for s in sqls))
        override_audit = next(
            p for s, p in conn.executed
            if "dim_org_assignment_override_audit" in s)
        self.assertEqual("assign", override_audit[1])
        roster_audit = [
            p for s, p in conn.executed if "dim_report_roster_audit" in s]
        self.assertEqual(2, len(roster_audit))            # 删 + 增 各一行审计
        self.assertEqual(1, conn.commits)                 # 复合写单事务一次 commit

    def test_move_to_pool_clears_region(self):
        conn = self._members_conn({
            "dim_org_region": [{"region_key": "hangzhou"}]})
        response = self._admin_client(conn).post("/api/org/move", json={
            "user_id": "u-wang", "to_region": None,
            "person_name": "王五", "from_region": "hangzhou"})

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {"status": "ok", "moved": True,
             "roster": {"from_removed": False, "to_added": False},
             "audit": True},
            response.json())
        audit = next(p for s, p in conn.executed
                     if "dim_org_assignment_override_audit" in s)
        self.assertEqual("移出全部区域", audit[7])
        self.assertFalse(any("INSERT INTO `dim_report_roster`" in s
                             for s, _ in conn.executed))

    def test_move_skips_add_when_target_has_no_roster_system(self):
        """目标区无启用 person 名册体系 → 不补录（避免凭空造白名单）。"""
        conn = self._members_conn({
            "dim_org_region": [{"region_key": "hangzhou"},
                               {"region_key": "dining"}]})
        response = self._admin_client(conn).post("/api/org/move", json={
            "user_id": "u-wang", "to_region": "dining", "person_name": "王五",
            "from_region": "hangzhou"})

        self.assertEqual(200, response.status_code)
        self.assertEqual({"from_removed": False, "to_added": False},
                         response.json()["roster"])
        self.assertFalse(any("INSERT INTO `dim_report_roster`" in s
                             for s, _ in conn.executed))

    def test_move_skips_add_when_already_in_target_roster(self):
        existing = [self._roster_row(9, "hq", "王五", "总经办")]

        def roster_router(sql, params):
            scope = params[0] if params else None
            return existing if scope == "hq" else []

        conn = self._members_conn({
            "dim_org_region": [{"region_key": "hangzhou"},
                               {"region_key": "hq"}],
            self._ROSTER_SQL_KEY: roster_router,
            self._PERSON_NAMES_SQL_KEY: [{"person_name": "王五"}],
        })
        response = self._admin_client(conn).post("/api/org/move", json={
            "user_id": "u-wang", "to_region": "hq", "person_name": "王五",
            "from_region": "hangzhou"})

        self.assertEqual(200, response.status_code)
        self.assertFalse(response.json()["roster"]["to_added"])
        self.assertFalse(any("INSERT INTO `dim_report_roster`" in s
                             for s, _ in conn.executed))

    def test_move_rejects_unregistered_target_region(self):
        conn = self._members_conn({
            "dim_org_region": [{"region_key": "hangzhou"}]})
        response = self._admin_client(conn).post("/api/org/move", json={
            "user_id": "u-wang", "to_region": "atlantis"})
        self.assertEqual(400, response.status_code)
        self.assertEqual(0, conn.commits)

    def test_move_rejects_missing_user_id(self):
        client = self._admin_client(self._members_conn())
        self.assertEqual(
            400, client.post("/api/org/move", json={"to_region": "hq"}
                             ).status_code)

    def test_move_requires_org_capability(self):
        unauth = TestClient(_app(viewer=_admin_viewer()))
        self.assertEqual(401, unauth.post(
            "/api/org/move", json={"user_id": "u-wang"}).status_code)
        viewer = authz.Viewer(
            userid="u1", name="张三", scopes=frozenset(),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        client = TestClient(_app(viewer=viewer))
        _login(client, "u1")
        self.assertEqual(403, client.post(
            "/api/org/move",
            json={"user_id": "u-wang", "to_region": "hq"}).status_code)

    # -- 组织目录（新增区域 / 部门，2026-10-13 P7）-------------------------

    def test_overview_renders_catalog_region_and_dept_without_members(self):
        conn = self._members_conn({
            "dim_org_region": [
                {"region_key": "chengdu", "label": "成都", "sort_order": 0},
            ],
            "dim_org_dept": [
                {"region_key": "hangzhou", "dept_name": "杭州销售部",
                 "dept_id": None, "sort_order": 0},
                {"region_key": "chengdu", "dept_name": "成都销售部",
                 "dept_id": None, "sort_order": 0},
            ],
        })
        data = self._admin_client(conn).get("/api/org/overview").json()
        regions = {region["key"]: region for region in data["regions"]}

        # 目录登记的新区域（无成员）也出现，label 取目录值。
        self.assertIn("chengdu", regions)
        self.assertEqual("成都", regions["chengdu"]["label"])
        chengdu = {d["dept_name"]: d for d in regions["chengdu"]["depts"]}
        self.assertEqual([], chengdu["成都销售部"]["members"])
        # 目录登记的部门（无成员）并入既有区域。
        hangzhou = {d["dept_name"]: d for d in regions["hangzhou"]["depts"]}
        self.assertIn("杭州销售部", hangzhou)
        self.assertEqual([], hangzhou["杭州销售部"]["members"])

    def test_overview_merges_same_name_dept_split_by_id_key(self):
        """裂行修复（2026-10-26）：覆盖成员的组带 dept_id、无覆盖成员的组
        只有部门名——同一部门不得裂成两行；合并归带 id 的组。"""
        conn = self._members_conn({
            "dim_robot_member": [
                ("u-zhang", "张三", "hangzhou", "门店一部"),
                ("u-chen", "陈六", "hangzhou", "门店一部"),   # 无覆盖→名字键
            ],
            "dim_org_assignment_override": [
                dict(_ORG_OVERRIDE_ROW, user_id="u-zhang", region="hangzhou",
                     dept_id="d1", dept_name="门店一部"),
            ],
        })
        data = self._admin_client(conn).get("/api/org/overview").json()
        regions = {r["key"]: r for r in data["regions"]}
        hz = regions["hangzhou"]["depts"]

        names = [d["dept_name"] for d in hz]
        self.assertEqual(1, names.count("门店一部"))          # 不裂行
        dept = next(d for d in hz if d["dept_name"] == "门店一部")
        self.assertEqual("d1", dept["dept_id"])             # 保留 id 组
        self.assertEqual({"u-zhang", "u-chen"},
                         {m["user_id"] for m in dept["members"]})

    def test_overview_catalog_read_fails_soft(self):
        conn = self._members_conn(fail={"dim_org_region"})
        response = self._admin_client(conn).get("/api/org/overview")
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertIn("hangzhou",
                      {region["key"] for region in data["regions"]})

    def test_region_add_requires_admin(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        self.assertEqual(
            401, client.post("/api/org/region/add",
                             json={"region_key": "chengdu", "label": "成都"}
                             ).status_code)

    def test_region_add_writes_catalog_and_audit(self):
        conn = self._members_conn()
        client = self._admin_client(conn)
        response = client.post("/api/org/region/add",
                               json={"region_key": "chengdu", "label": "成都"})
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok"}, response.json())
        sqls = [sql for sql, _ in conn.executed]
        self.assertTrue(any("INSERT INTO `dim_org_region`" in sql
                            and "ON DUPLICATE KEY UPDATE" in sql
                            for sql in sqls))
        audit = next(p for sql, p in conn.executed
                     if "dim_org_catalog_audit" in sql)
        self.assertEqual(ADMIN_USERID, audit[0])
        self.assertEqual("add_region", audit[1])
        self.assertEqual("chengdu", audit[3])
        self.assertEqual(1, conn.commits)

    def test_region_add_rejects_bad_body(self):
        conn = self._members_conn()
        client = self._admin_client(conn)
        self.assertEqual(
            400, client.post("/api/org/region/add",
                             json={"region_key": "Bad Key", "label": "成都"}
                             ).status_code)
        self.assertEqual(
            400, client.post("/api/org/region/add",
                             json={"region_key": "chengdu", "label": ""}
                             ).status_code)
        self.assertEqual(0, conn.commits)

    # -- /api/org/region/relay（播报改投测试区，2026-10-19 裁决）----------

    def test_region_relay_sets_and_clears_with_audit(self):
        conn = self._members_conn()
        client = self._admin_client(conn)
        response = client.post("/api/org/region/relay",
                               json={"region_key": "hangzhou", "relay": True})
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok", "region_key": "hangzhou",
                          "relay_to": "test"}, response.json())
        update = next(p for s, p in conn.executed
                      if s.lstrip().startswith("UPDATE"))
        self.assertEqual("test", update[0])          # relay_to='test'
        audit = next(p for s, p in conn.executed
                     if "dim_org_catalog_audit" in s)
        self.assertEqual("set_relay", audit[1])
        # 清除
        response = client.post("/api/org/region/relay",
                               json={"region_key": "hangzhou", "relay": False})
        self.assertEqual(200, response.status_code)
        update = next(p for s, p in reversed(conn.executed)
                      if s.lstrip().startswith("UPDATE"))
        self.assertIsNone(update[0])

    def test_region_relay_guards(self):
        conn = self._members_conn()
        client = self._admin_client(conn)
        # 测试区自身不可改投（防回环）
        self.assertEqual(400, client.post(
            "/api/org/region/relay",
            json={"region_key": "test", "relay": True}).status_code)
        # 非法载荷 / 未登记区域（UPDATE 零命中）
        self.assertEqual(400, client.post(
            "/api/org/region/relay",
            json={"region_key": "hangzhou"}).status_code)
        self.assertEqual(0, conn.commits)

    def test_region_relay_requires_org_capability(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        self.assertEqual(401, client.post(
            "/api/org/region/relay",
            json={"region_key": "hangzhou", "relay": True}).status_code)

    def test_region_scope_pairs_skip_test_region(self):
        """测试区改制为「播报试投」功能模块（2026-10-19）：不进名册/目标
        页的区域列表（它不是业务区域）。"""
        from common.ops_web.app import _region_scope_pairs
        conn = self._members_conn({"dim_org_region": [
            {"region_key": "hangzhou", "label": "杭州"},
            {"region_key": "test", "label": "测试区"},
            {"region_key": "shaoxing", "label": "绍兴"},
        ]})
        pairs = _region_scope_pairs(conn)
        keys = [key for key, _ in pairs]
        self.assertEqual(["hangzhou", "shaoxing"], keys)

    def test_dept_add_writes_catalog_and_audit(self):
        conn = self._members_conn()
        client = self._admin_client(conn)
        response = client.post(
            "/api/org/dept/add",
            json={"region_key": "hangzhou", "dept_name": "杭州销售部",
                  "dept_id": "1050062512"})
        self.assertEqual(200, response.status_code)
        sqls = [sql for sql, _ in conn.executed]
        self.assertTrue(any("INSERT INTO `dim_org_dept`" in sql
                            and "ON DUPLICATE KEY UPDATE" in sql
                            for sql in sqls))
        audit = next(p for sql, p in conn.executed
                     if "dim_org_catalog_audit" in sql)
        self.assertEqual("add_dept", audit[1])
        self.assertEqual("hangzhou", audit[3])
        self.assertEqual("1050062512", audit[4])
        self.assertEqual("杭州销售部", audit[5])

    def test_dept_add_rejects_missing_name(self):
        client = self._admin_client(self._members_conn())
        self.assertEqual(
            400, client.post("/api/org/dept/add",
                             json={"region_key": "hangzhou",
                                   "dept_name": "  "}).status_code)


class RegionScopedOrgGateTests(unittest.TestCase):
    """C5 闭环修复（2026-10-15）：``org:<region>`` 区域级授权真正生效。

    授权页允许授出 ``org:<region>``（能力×范围模型），但此前 /api/org/overview
    与 assign/toggle/delete 只认全局 ``org``——授权页能给、页面用不上，语义
    不自洽。本组钉死：只读本区域、只写本区域、越区 403、池与跨区覆盖行不泄漏；
    目录级 CRUD（区域/部门增删停用）有意保持全局 org。
    """

    def _scoped_client(self, region, connection=None):
        viewer = authz.Viewer(
            userid="u-org-scoped", name="区域组织",
            scopes=frozenset({f"org:{region}"}),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        client = TestClient(_app(viewer=viewer, connection=connection))
        _login(client, "u-org-scoped")
        return client

    def _members_conn(self, rows=None, **kwargs):
        base = {"dim_robot_member": _MEMBERS}
        base.update(rows or {})
        return _OrgConn(base, **kwargs)

    def _cross_region_override_conn(self):
        return self._members_conn({"dim_org_assignment_override": [
            dict(_ORG_OVERRIDE_ROW, user_id="u-wang", region="hq"),
            dict(_ORG_OVERRIDE_ROW, user_id="u-zhang", region="hangzhou"),
        ]})

    def test_overview_reads_only_authorized_region(self):
        client = self._scoped_client("hangzhou", self._members_conn())
        data = client.get("/api/org/overview").json()

        self.assertEqual("ok", data["status"])
        self.assertEqual(["hangzhou"], [r["key"] for r in data["regions"]])
        self.assertEqual([], data["pool"])

    def test_overview_hides_cross_region_overrides(self):
        client = self._scoped_client("hangzhou", self._cross_region_override_conn())
        data = client.get("/api/org/overview").json()

        self.assertEqual(["hangzhou"], [r["key"] for r in data["regions"]])
        # 跨区覆盖行（hq）不下发，只留本区域行
        self.assertEqual({"u-zhang"},
                         {row["user_id"] for row in data["overrides"]})

    def test_assign_inside_authorized_region_allowed(self):
        client = self._scoped_client("hangzhou", self._members_conn())
        response = client.post("/api/org/assign", json={
            "user_id": "u-li", "region": "hangzhou", "dept_id": "d1",
            "dept_name": "门店一部", "name": "李四"})

        self.assertEqual(200, response.status_code)

    def test_assign_outside_authorized_region_forbidden(self):
        client = self._scoped_client("hangzhou", self._members_conn())
        response = client.post("/api/org/assign", json={
            "user_id": "u-li", "region": "hq", "dept_id": "d9",
            "dept_name": "总经办", "name": "李四"})

        self.assertEqual(403, response.status_code)

    def test_assign_null_region_requires_global_org(self):
        """``region=null``（移出全部区域）是跨区动作，只认全局 org。"""
        client = self._scoped_client("hangzhou", self._members_conn())
        self.assertEqual(403, client.post(
            "/api/org/assign", json={"user_id": "u-li"}).status_code)

    def test_toggle_inside_region_allowed_outside_forbidden(self):
        client = self._scoped_client("hangzhou", self._cross_region_override_conn())
        ok = client.post("/api/org/toggle",
                         json={"user_id": "u-zhang", "enabled": False})
        bad = client.post("/api/org/toggle",
                          json={"user_id": "u-wang", "enabled": False})

        self.assertEqual(200, ok.status_code)
        self.assertEqual(403, bad.status_code)

    def test_delete_outside_region_forbidden(self):
        client = self._scoped_client("hangzhou", self._cross_region_override_conn())
        self.assertEqual(403, client.post(
            "/api/org/delete", json={"user_id": "u-wang"}).status_code)

    def test_catalog_crud_stays_global_org(self):
        """区域/部门增删停用影响整张登记表结构，超出单区域范围，保持全局 org。"""
        client = self._scoped_client("hangzhou", self._members_conn())
        self.assertEqual(403, client.post(
            "/api/org/region/add",
            json={"region_key": "shaoxing"}).status_code)
        self.assertEqual(403, client.post(
            "/api/org/dept/add",
            json={"region_key": "hangzhou", "dept_name": "新部门"}).status_code)

    def test_plain_member_cannot_read_overview(self):
        client = TestClient(_app(viewer=_plain_member_viewer()))
        _login(client, "u-plain")
        self.assertEqual(403, client.get("/api/org/overview").status_code)
