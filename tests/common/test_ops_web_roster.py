"""test_ops_web_app 的 roster 域测试；断言自原文件等价迁移。"""

from tests.common.ops_web_test_support import *


class RosterPageTests(unittest.TestCase):
    """填报名册面（2026-10-14 全 SPA 迁移）。

    SSR 名册页呈现（负责人/代填报人分列、状态下拉、scope 反向索引深链、
    空范围提示）已迁至 Vue 页，相应纯呈现用例删除；数据/权限断言改钉
    JSON 读面 /api/roster（渠道门店明细另见 RosterSortTests 的 qudao 读面）。
    """

    def test_roster_api_groups_by_scope_with_chinese_labels(self):
        store = _RosterStore()
        store.rows = [_roster_row()]
        store.audit_rows = [{
            "actor": "admin", "action": "add", "scope": "qudao",
            "entity_type": "store", "entity_key": "JD购喝",
            "person_name": "饶佳君", "detail": "共管",
            "created_at": "2026-10-06 11:00:00",
        }]
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_fetch(), store.patch_audit():
            data = client.get("/api/roster").json()

        self.assertEqual("ok", data["status"])
        labels = {r["region_key"]: r["label"] for r in data["regions"]}
        self.assertEqual("渠道门店", labels.get("qudao"))    # scope 中文化
        row = next(r for r in data["roster"] if r["entity_key"] == "JD购喝")
        self.assertEqual("饶佳君", row["person_name"])
        self.assertEqual("渠道门店", row["scope_label"])
        self.assertIn("小饶", row["aliases"])                # aliases JSON 原样透出
        self.assertEqual(["admin"], [a["actor"] for a in data["audits"]])

    def test_roster_api_normalizes_datetime_fields(self):
        """updated_at / created_at 为 datetime 时须归一（2026-10-14 线上 500 回归）。

        pymysql 返回 datetime 对象，未过 _fmt_time 直接进 JSONResponse 会
        json.dumps 失败 → 整接口 500、/roster 页数据面全挂。
        """
        import datetime as _dt

        store = _RosterStore()
        store.rows = [_roster_row(updated_at=_dt.datetime(2026, 10, 6, 3, 0, 0))]
        store.audit_rows = [{
            "actor": "admin", "action": "target", "scope": "other",
            "entity_type": "person", "entity_key": "other",
            "person_name": "谢坚钰", "detail": "219.5",
            "created_at": _dt.datetime(2026, 10, 6, 3, 0, 0),
        }]
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_fetch(), store.patch_audit():
            resp = client.get("/api/roster")

        self.assertEqual(200, resp.status_code)
        data = resp.json()
        # UTC 03:00 → 北京 11:00，证明确实走了 _fmt_time（而非原样透传）。
        row = next(r for r in data["roster"] if r["entity_key"] == "JD购喝")
        self.assertEqual("2026-10-06 11:00:00", row["updated_at"])
        self.assertEqual("2026-10-06 11:00:00", data["audits"][0]["created_at"])

    def test_roster_api_requires_admin(self):
        # 壳路由放行（前端 Router 守卫）；数据面 fail-closed。
        client = TestClient(_app(viewer=_admin_viewer()))
        assert_spa_shell(client.get("/roster", follow_redirects=False))
        self.assertEqual(401, client.get("/api/roster").status_code)

        member = TestClient(_app(viewer=_plain_member_viewer()))
        _login(member, "u-plain")
        self.assertEqual(403, member.get("/api/roster").status_code)

    def test_roster_shell_renders_without_fleet(self):
        """无舰队清单（fleet_source=None）时 /roster 壳路由照常返回。"""
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        assert_spa_shell(client.get("/roster", follow_redirects=False))


class RosterApiTests(unittest.TestCase):
    def test_add_validates_and_upserts_with_actor(self):
        store = _RosterStore()
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_upsert():
            response = client.post("/api/roster/add", json={
                "scope": "qudao", "entity_type": "store",
                "entity_key": "JD购喝", "person_name": "饶佳君",
                "aliases": ["小饶"], "note": "共管",
            })

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(store.upserted))
        entry, actor = store.upserted[0]
        self.assertEqual("JD购喝", entry.entity_key)
        self.assertEqual(("小饶",), entry.aliases)
        self.assertEqual(ADMIN_USERID, actor)

    def test_add_rejects_invalid_fields(self):
        store = _RosterStore()
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        for payload in (
            {"scope": "nope", "entity_type": "store",
             "entity_key": "店", "person_name": "张三"},
            {"scope": "qudao", "entity_type": "alien",
             "entity_key": "店", "person_name": "张三"},
            {"scope": "qudao", "entity_type": "store",
             "entity_key": "店", "person_name": "  "},
            {"scope": "qudao", "entity_type": "store",
             "entity_key": "店", "person_name": "张三", "aliases": "not-a-list"},
            {"scope": "qudao", "entity_type": "store",
             "entity_key": "店", "person_name": "张三", "role": "boss"},
        ):
            self.assertEqual(
                400, client.post("/api/roster/add", json=payload).status_code,
                payload,
            )
        self.assertEqual([], store.upserted)

    def test_add_with_deputy_role(self):
        # v3 角色：代填报人经表单 role 字段入库（2026-10-07 运维裁决）
        store = _RosterStore()
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_upsert():
            response = client.post("/api/roster/add", json={
                "scope": "qudao", "entity_type": "store",
                "entity_key": "TM习酒旗舰店", "person_name": "卢雅莹",
                "role": "deputy",
            })

        self.assertEqual(200, response.status_code)
        entry, _actor = store.upserted[0]
        self.assertEqual("deputy", entry.role)

    def test_toggle_and_delete_with_hit_and_miss(self):
        store = _RosterStore()
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)

        with store.patch_toggle():
            self.assertEqual(200, client.post(
                "/api/roster/toggle", json={"id": 5, "enabled": False},
            ).status_code)
        self.assertEqual([(5, False, ADMIN_USERID)], store.toggled)

        with store.patch_delete():
            self.assertEqual(200, client.post(
                "/api/roster/delete", json={"id": 7},
            ).status_code)
        self.assertEqual([(7, ADMIN_USERID)], store.deleted)

        # 未命中 → 400；非法载荷 → 400
        store.hit = False
        with store.patch_toggle():
            self.assertEqual(400, client.post(
                "/api/roster/toggle", json={"id": 99, "enabled": True},
            ).status_code)
        self.assertEqual(400, client.post(
            "/api/roster/toggle", json={"id": "x", "enabled": True},
        ).status_code)
        self.assertEqual(400, client.post(
            "/api/roster/delete", json={"id": True},
        ).status_code)

    def test_apis_require_admin_session(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        self.assertEqual(401, client.post(
            "/api/roster/add", json={"scope": "qudao", "entity_type": "store",
                                     "entity_key": "店", "person_name": "张三"},
        ).status_code)


class RosterManageGateTests(unittest.TestCase):
    """名册新增/删除/状态修改绑定「名册管理」授权（scope=roster，
    2026-10-07 运维裁决）；admin 隐式持有。"""

    def test_manager_can_view_and_write(self):
        viewer = _roster_manager_viewer()
        store = _RosterStore()
        client = TestClient(_app(viewer=viewer))
        _login(client, userid="u-manager")
        with store.patch_fetch(), store.patch_audit():
            self.assertEqual(200, client.get("/roster").status_code)
        with store.patch_upsert():
            self.assertEqual(200, client.post("/api/roster/add", json={
                "scope": "qudao", "entity_type": "store",
                "entity_key": "JD购喝", "person_name": "饶佳君",
            }).status_code)
        with store.patch_toggle():
            self.assertEqual(200, client.post(
                "/api/roster/toggle", json={"id": 1, "enabled": False},
            ).status_code)
        with store.patch_delete():
            self.assertEqual(200, client.post(
                "/api/roster/delete", json={"id": 1},
            ).status_code)
        entry, actor = store.upserted[0]
        self.assertEqual("u-manager", actor)

    def test_plain_member_forbidden(self):
        viewer = _plain_member_viewer()
        store = _RosterStore()
        client = TestClient(_app(viewer=viewer))
        _login(client, userid="u-plain")
        # 壳路由放行（前端 Router 守卫）；数据面 fail-closed。
        assert_spa_shell(client.get("/roster", follow_redirects=False))
        self.assertEqual(403, client.get("/api/roster").status_code)
        for path, payload in (
            ("/api/roster/add", {"scope": "qudao", "entity_type": "store",
                                 "entity_key": "店", "person_name": "张三"}),
            ("/api/roster/toggle", {"id": 1, "enabled": False}),
            ("/api/roster/delete", {"id": 1}),
        ):
            self.assertEqual(
                403, client.post(path, json=payload).status_code, path,
            )
        self.assertEqual([], store.upserted)

    def test_admin_remains_implicit_manager(self):
        store = _RosterStore()
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_delete():
            self.assertEqual(200, client.post(
                "/api/roster/delete", json={"id": 1},
            ).status_code)


class RosterSortTests(unittest.TestCase):
    """渠道门店读面 /api/roster/qudao（2026-10-14 全 SPA 迁移）。

    SSR 名册页的排序（``?sort=`` 链接）、渠道 rowspan 聚合与列序等呈现细节
    已迁至 Vue 页，相应纯呈现用例删除；数据面（负责人/代填报人拆分与排序、
    channel/store_no/门店目标元口径）改钉 JSON 读面。
    """

    def _rows(self):
        return [
            _roster_row(id=1, entity_key="JD购喝", person_name="娄灿斌",
                        aliases=None, note=""),
            _roster_row(id=2, entity_key="TM旗舰A", person_name="钟甜",
                        aliases=None, note=""),
            _roster_row(id=3, entity_key="TM旗舰B", person_name="周嘉炜",
                        aliases=None, note=""),
            _roster_row(id=4, entity_key="TM旗舰A", person_name="张瑾萱",
                        aliases=None, note="", role="deputy"),
        ]

    def _qudao(self, store):
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        targets = {
            "JD购喝": ("京东", 1500000),
            "TM旗舰A": ("天猫", 3000000),
            "TM旗舰B": ("天猫", 2000000),
        }
        numbers = {
            "JD购喝": ("京东", 1),
            "TM旗舰A": ("天猫", 1),
            "TM旗舰B": ("天猫", 2),
        }
        with store.patch_fetch(), store.patch_audit(), \
             mock.patch.object(channel_target, "fetch_store_targets",
                               lambda conn: targets), \
             mock.patch.object(report_roster, "fetch_store_numbers",
                               lambda conn: numbers):
            return client.get("/api/roster/qudao").json()

    def test_qudao_api_splits_roles_and_reports_channel_target_number(self):
        store = _RosterStore()
        store.rows = self._rows()
        data = self._qudao(store)

        self.assertEqual("ok", data["status"])
        by_store = {s["store"]: s for s in data["stores"]}
        self.assertEqual({"JD购喝", "TM旗舰A", "TM旗舰B"}, set(by_store))
        tm = by_store["TM旗舰A"]
        self.assertEqual("天猫", tm["channel"])
        self.assertEqual(1, tm["store_no"])
        self.assertEqual(3000000, tm["target"])       # 门店目标（元口径）
        self.assertEqual(["钟甜"], tm["owners"])       # 负责人
        self.assertEqual(["张瑾萱"], tm["deputies"])   # 代填报人分列
        # 链接序：负责人在前、代填在后（读面确定性）
        self.assertEqual(["钟甜", "张瑾萱"],
                         [link["person_name"] for link in tm["links"]])
        jd = by_store["JD购喝"]
        self.assertEqual("京东", jd["channel"])
        self.assertEqual(1500000, jd["target"])
        self.assertEqual(["娄灿斌"], jd["owners"])
        self.assertEqual([], jd["deputies"])
        self.assertEqual(2, by_store["TM旗舰B"]["store_no"])

    def test_qudao_api_multi_owner_store_lists_all_owners_sorted(self):
        store = _RosterStore()
        store.rows = self._rows() + [
            _roster_row(id=5, entity_key="PDD共管店", person_name="侯仙姚",
                        aliases=None, note=""),
            _roster_row(id=6, entity_key="PDD共管店", person_name="杨美聪",
                        aliases=None, note=""),
        ]
        data = self._qudao(store)
        pdd = {s["store"]: s for s in data["stores"]}["PDD共管店"]
        # 多负责人全在 owners（升序）、deputies 为空
        self.assertEqual(["侯仙姚", "杨美聪"], pdd["owners"])
        self.assertEqual([], pdd["deputies"])


class RosterRegionTemplateTests(unittest.TestCase):
    """区域名册 + 个人月目标读面 /api/roster（2026-10-14 全 SPA 迁移）。

    SSR 名册页的区域表模板（统一表头、rowspan 合并、可编辑目标输入框、
    月份选择器、状态下拉）已迁至 Vue 页，相关呈现用例删除；数据面
    （区域个人月目标「万元」口径 + 名册行）改钉 JSON 读面。
    """

    def test_roster_api_reports_region_person_targets_in_wan(self):
        store = _RosterStore()
        store.rows = [
            _roster_row(id=11, scope="shaoxing", entity_type="person",
                        entity_key="绍兴项目部", person_name="潘良峰",
                        aliases=None, note=""),
            _roster_row(id=12, scope="shaoxing", entity_type="person",
                        entity_key="绍兴项目部", person_name="洪强",
                        aliases=None, note="", enabled=0),
            _roster_row(id=13, scope="shaoxing", entity_type="person",
                        entity_key="诸暨项目部", person_name="周亚平",
                        aliases=None, note=""),
        ]
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_fetch(), store.patch_audit(), \
             mock.patch.object(
                 report_roster, "fetch_person_targets",
                 lambda conn, scope, ym: (
                     {"潘良峰": 383000.0, "洪强": 351000.0}
                     if scope == "shaoxing" else {})):
            data = client.get("/api/roster").json()

        # 个人月目标「万元」口径（停用的洪强仍挂在其名字键下）
        self.assertEqual({"潘良峰": 383000.0, "洪强": 351000.0},
                         data["targets"]["shaoxing"])
        rows = {r["person_name"]: r for r in data["roster"]
                if r["scope"] == "shaoxing"}
        self.assertEqual({"潘良峰", "洪强", "周亚平"}, set(rows))
        self.assertEqual("绍兴", rows["潘良峰"]["scope_label"])
        self.assertEqual("person", rows["潘良峰"]["entity_type"])

    def test_roster_api_reports_completed_by_scope(self):
        """画布完成率分子（2026-10-26「播报回显完成百分比上面板」）：
        当月完成额按名册**在册 person 型**人名归集到 scope（代填落群区域
        也归本人——人名匹配）；停用行与店铺型不计；读失败 fail-soft。"""
        store = _RosterStore()
        store.rows = [
            _roster_row(id=11, scope="shaoxing", entity_type="person",
                        entity_key="绍兴项目部", person_name="潘良峰",
                        aliases=None, note=""),
            _roster_row(id=12, scope="shaoxing", entity_type="person",
                        entity_key="绍兴项目部", person_name="洪强",
                        aliases=None, note="", enabled=0),      # 停用不计
            _roster_row(id=13, scope="qudao", entity_type="store",
                        entity_key="PDD店", person_name="饶佳君",
                        aliases=None, note=""),                  # 店铺型不计
        ]
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_fetch(), store.patch_audit(), mock.patch.object(
                report_roster, "fetch_month_completed_by_person",
                lambda conn, ym: {"潘良峰": 120000.0, "洪强": 999.0}):
            data = client.get("/api/roster").json()
        self.assertEqual({"shaoxing": 120000.0}, data["completed"])

    def test_roster_api_completed_read_fails_soft(self):
        """完成额读面异常（表未迁移等）→ completed={} 兜底，名册读面不拖垮。"""
        store = _RosterStore()
        store.rows = [_roster_row()]
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)

        def _boom(conn, ym):
            raise RuntimeError("table missing")

        with store.patch_fetch(), store.patch_audit(), mock.patch.object(
                report_roster, "fetch_month_completed_by_person", _boom):
            data = client.get("/api/roster").json()
        self.assertEqual("ok", data["status"])
        self.assertEqual({}, data["completed"])


class RegionTargetsApiTests(unittest.TestCase):
    """区域月目标保存 API（dim_report_target 入库，2026-10-07）。"""

    def test_save_validates_and_upserts_with_actor(self):
        saved = []
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with mock.patch.object(
                report_roster, "upsert_report_target",
                lambda conn, entry, *, actor: saved.append((entry, actor))):
            response = client.post("/api/roster/region-targets", json={
                "scope": "shaoxing",
                "rows": [
                    {"person_name": "潘良峰", "monthly_target": 383000},
                    {"person_name": "周亚平", "monthly_target": None},
                ],
            })

        self.assertEqual(200, response.status_code)
        self.assertEqual(2, response.json()["saved"])
        self.assertEqual(2, len(saved))
        entry, actor = saved[0]
        self.assertEqual(("shaoxing", "潘良峰", 383000.0),
                         (entry.scope, entry.person_name,
                          entry.monthly_target))
        self.assertEqual(ADMIN_USERID, actor)
        self.assertIsNone(saved[1][0].monthly_target)  # 空白 = 清除

    def test_save_accepts_explicit_year_month(self):
        """2026-10-09 月份切换：可指定下月提前录入（结转管道不覆盖人工键）。"""
        saved = []
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with mock.patch.object(
                report_roster, "upsert_report_target",
                lambda conn, entry, *, actor: saved.append(entry)):
            response = client.post("/api/roster/region-targets", json={
                "scope": "hangzhou",
                "year_month": "2099-11",
                "rows": [{"person_name": "余发兴", "monthly_target": 822000}],
            })

        self.assertEqual(200, response.status_code)
        self.assertEqual("2099-11", saved[0].year_month)

    def test_save_rejects_invalid_payload_and_requires_manager(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        for payload in (
            {"scope": "qudao", "rows": []},                 # qudao 不走本表
            {"scope": "nope", "rows": []},
            {"scope": "shaoxing", "rows": "not-a-list"},
            {"scope": "shaoxing",
             "rows": [{"person_name": "潘良峰", "monthly_target": -1}]},
            {"scope": "shaoxing", "rows": [{"person_name": "  "}]},
        ):
            self.assertEqual(
                400, client.post("/api/roster/region-targets",
                                 json=payload).status_code, payload,
            )

        plain = TestClient(_app(viewer=_plain_member_viewer()))
        _login(plain, userid="u-plain")
        self.assertEqual(403, plain.post("/api/roster/region-targets", json={
            "scope": "shaoxing", "rows": [],
        }).status_code)


class RosterPublishTests(unittest.TestCase):
    """发布月度快照 API（S2）：校验 + 写 raw + 触发 extract run-request。"""

    def test_publish_writes_and_triggers_extract(self):
        published = []
        requests = []

        class _RawConn:
            def close(self):
                pass

        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with mock.patch.object(
                channel_target, "publish_snapshot",
                lambda raw_conn, rows, *, actor, roster_connection=None:
                published.append((rows, actor, roster_connection)) or (2, 3, ["新店"])), \
             mock.patch.object(
                ops_control, "insert_run_request",
                lambda conn, sid, by: requests.append((sid, by)) or 88), \
             mock.patch.object(
                report_roster, "fetch_store_numbers", lambda conn: {}), \
             mock.patch("common.ops_web.app.connect", lambda _ds: _RawConn()):
            response = client.post("/api/roster/publish-snapshot", json={
                "rows": [{"store_name": "JD购喝", "channel": "京东",
                          "monthly_target": 1500000}],
            })

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual((2, 3), (body["deleted"], body["inserted"]))
        self.assertEqual(["新店"], body["missing_owners"])
        self.assertEqual(88, body["extract_request_id"])
        self.assertEqual([("extract-mart", ADMIN_USERID)], requests)
        self.assertEqual(ADMIN_USERID, published[0][1])
        self.assertIsNotNone(published[0][2])  # roster_connection 走 mart

    def test_publish_rejects_bad_payload(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with mock.patch.object(report_roster, "fetch_store_numbers",
                               lambda conn: {}):
            self.assertEqual(400, client.post(
                "/api/roster/publish-snapshot", json={"rows": "x"}).status_code)
            self.assertEqual(400, client.post(
                "/api/roster/publish-snapshot",
                json={"rows": [{"store_name": "店", "channel": "商超",
                                "monthly_target": 1}]}).status_code)

    def test_publish_requires_admin_session(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        self.assertEqual(401, client.post(
            "/api/roster/publish-snapshot", json={"rows": []}).status_code)
