"""test_ops_web_app 的 core 域测试；断言自原文件等价迁移。"""

from tests.common.ops_web_test_support import *


class FactoryTests(unittest.TestCase):
    def test_session_secret_is_mandatory(self):
        with self.assertRaises(ValueError):
            create_app(settings=_fake_settings(), session_secret=None,
                       viewer_resolver=_StaticViewerResolver())


class HealthzTests(unittest.TestCase):
    def test_healthz_reports_ok_with_db(self):
        client = TestClient(_app(viewer=_admin_viewer()))

        response = client.get("/healthz")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok", "database": "ok"}, response.json())

    def test_healthz_reports_unhealthy_without_db(self):
        client = TestClient(
            _app(viewer=_admin_viewer(), connector_error=RuntimeError("mart down"))
        )

        response = client.get("/healthz")

        self.assertEqual(503, response.status_code)
        self.assertEqual({"status": "unhealthy"}, response.json())


class CapabilityModelTests(unittest.TestCase):
    """D4（2026-10-14）：能力×范围闸的纯函数语义。"""

    def test_admin_passes_every_capability(self):
        for capability in ALL_CAPABILITIES:
            self.assertTrue(capability_allows(_admin_viewer(), capability))

    def test_denied_viewer_fails_closed(self):
        viewer = authz.denied_viewer("u-x")
        for capability in ALL_CAPABILITIES:
            self.assertFalse(capability_allows(viewer, capability))
            self.assertFalse(capability_has_scoped(viewer, capability))

    def test_region_scope_only_matches_its_region(self):
        viewer = authz.Viewer(
            userid="u-other", name="名册", scopes=frozenset({"roster:other"}),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        self.assertTrue(capability_allows(viewer, "roster", "other"))
        self.assertFalse(capability_allows(viewer, "roster", "hq"))
        # 区域级键不隐式授予全局能力
        self.assertFalse(capability_allows(viewer, "roster"))
        self.assertTrue(capability_has_scoped(viewer, "roster"))
        self.assertFalse(capability_has_scoped(viewer, "org"))

    def test_global_capability_covers_all_regions(self):
        viewer = authz.Viewer(
            userid="u-r", name="名册", scopes=frozenset({"roster"}),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        self.assertTrue(capability_allows(viewer, "roster", "hq"))
        self.assertTrue(capability_allows(viewer, "roster", "other"))


class MeApiTests(unittest.TestCase):
    """/api/me（D1 SPA 启动契约）：能力＋可见区域自述。"""

    def test_requires_session(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        self.assertEqual(401, client.get("/api/me").status_code)

    def test_non_admitted_is_forbidden(self):
        client = TestClient(_app(viewer=authz.denied_viewer("u-x")))
        _login(client, "u-x")
        self.assertEqual(403, client.get("/api/me").status_code)

    def test_admin_reports_all_capabilities_and_regions(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        data = client.get("/api/me").json()
        self.assertTrue(data["is_admin"])
        self.assertEqual(list(ALL_CAPABILITIES), data["capabilities"])
        self.assertEqual({}, data["scoped_capabilities"])
        self.assertIn("other", data["regions"])   # SCOPES 兜底含 hq/other

    def test_scoped_viewer_reports_region_only(self):
        viewer = authz.Viewer(
            userid="u-other", name="名册", scopes=frozenset({"roster:other"}),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        client = TestClient(_app(viewer=viewer))
        _login(client, "u-other")
        data = client.get("/api/me").json()
        self.assertFalse(data["is_admin"])
        self.assertEqual([], data["capabilities"])
        self.assertEqual({"roster": ["other"]}, data["scoped_capabilities"])
        self.assertEqual(["other"], data["regions"])

    def test_scoped_org_viewer_reports_region_only(self):
        """C5 闭环修复（2026-10-15）：``org:<region>`` 也进 scoped_capabilities，
        前端据此放行 /org 并按授权区域渲染（此前守卫只看全局 org → 进不去）。"""
        viewer = authz.Viewer(
            userid="u-org", name="组织", scopes=frozenset({"org:other"}),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        client = TestClient(_app(viewer=viewer))
        _login(client, "u-org")
        data = client.get("/api/me").json()
        self.assertFalse(data["is_admin"])
        self.assertEqual([], data["capabilities"])
        self.assertEqual({"org": ["other"]}, data["scoped_capabilities"])


class RegionScopeGateTests(unittest.TestCase):
    """D4 区域分权（§7 验收第 6 条）：roster:<region> 只能改本区域。"""

    def _scoped_client(self, scope):
        viewer = authz.Viewer(
            userid="u-other", name="名册", scopes=frozenset({f"roster:{scope}"}),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        client = TestClient(_app(viewer=viewer))
        _login(client, "u-other")
        return client

    def test_targets_read_is_region_scoped(self):
        client = self._scoped_client("other")
        with mock.patch.object(
                report_roster, "fetch_person_targets",
                lambda conn, scope, ym: {"李树军": 93.0}):
            ok = client.get("/api/roster/targets", params={"scope": "other"})
            bad = client.get("/api/roster/targets", params={"scope": "hq"})
        self.assertEqual(200, ok.status_code)
        self.assertEqual("other", ok.json()["scope"])
        self.assertEqual(93.0, ok.json()["targets"][0]["monthly_target"])
        self.assertEqual(403, bad.status_code)

    def test_region_targets_write_is_region_scoped(self):
        client = self._scoped_client("other")
        with mock.patch.object(
                report_roster, "upsert_report_target",
                lambda conn, entry, *, actor: None):
            ok = client.post("/api/roster/region-targets", json={
                "scope": "other",
                "rows": [{"person_name": "李树军", "monthly_target": 93}],
            })
            bad = client.post("/api/roster/region-targets", json={
                "scope": "hq",
                "rows": [{"person_name": "谢坚钰", "monthly_target": 219.5}],
            })
        self.assertEqual(200, ok.status_code)
        self.assertEqual(403, bad.status_code)

    def test_scoped_viewer_cannot_touch_grants(self):
        """G4：受限 viewer（仅 `roster:other`）无授权写面/读面（fail-closed）。"""
        client = self._scoped_client("other")
        payload = {
            "user_id": "u-x", "grant_type": "scope", "grant_key": "roster:hq",
        }
        self.assertEqual(403, client.get("/api/grants").status_code)
        self.assertEqual(403, client.post("/api/grants", json=payload).status_code)
        self.assertEqual(
            403, client.post("/api/grants/delete", json=payload).status_code
        )

    def test_roster_read_filters_to_authorized_regions(self):
        store = _RosterStore()
        store.rows = [
            _roster_row(id=1, scope="other", entity_type="person",
                        entity_key="线下运营中心", person_name="李树军"),
            _roster_row(id=2, scope="hq", entity_type="person",
                        entity_key="总经办", person_name="谢坚钰"),
        ]
        client = self._scoped_client("other")
        with store.patch_fetch(), mock.patch.object(
                report_roster, "fetch_person_targets",
                lambda conn, scope, ym: {}):
            data = client.get("/api/roster").json()
        self.assertEqual(["other"], [r["region_key"] for r in data["regions"]])
        self.assertEqual({"other"}, {row["scope"] for row in data["roster"]})

    def test_plain_member_cannot_touch_roster_targets(self):
        client = TestClient(_app(viewer=_plain_member_viewer()))
        _login(client, "u-plain")
        self.assertEqual(403, client.get(
            "/api/roster/targets", params={"scope": "other"}).status_code)
        self.assertEqual(403, client.post(
            "/api/roster/region-targets",
            json={"scope": "other", "rows": []}).status_code)


class RegionsApiTests(unittest.TestCase):
    """/api/regions（D2 真源读）。"""

    def test_requires_session(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        self.assertEqual(401, client.get("/api/regions").status_code)

    def test_forbidden_without_org_or_roster(self):
        client = TestClient(_app(viewer=_plain_member_viewer()))
        _login(client, "u-plain")
        self.assertEqual(403, client.get("/api/regions").status_code)

    def test_org_capability_reads_catalog(self):
        viewer = authz.Viewer(
            userid="u-org", name="组织", scopes=frozenset({"org"}),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        client = TestClient(_app(viewer=viewer))
        _login(client, "u-org")
        data = client.get("/api/regions").json()
        self.assertEqual("ok", data["status"])
        self.assertIn("regions", data)
        self.assertIn("depts", data)

    def test_regions_carry_derived_leaderboard_url(self):
        """榜单页派生生效值透出（2026-10-26 URL 基址 env 化配套）：
        库存列为空时 broadcast 带 leaderboard_url_derived（基址模板派生），
        抽屉编辑块据此标注「留空=自动派生」，空字段不再被误读为丢失。"""
        catalog = {
            "regions": [
                {"region_key": "hangzhou",
                 "broadcast": {"leaderboard_url": None}},
                {"region_key": "junpin",
                 "broadcast": {"leaderboard_url": "http://custom/lb"}},
                {"region_key": "nobc"},   # 无 broadcast 子对象不炸
            ],
            "depts": [], "region_depts": {},
        }
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        with mock.patch.object(
                org_catalog, "fetch_catalog", lambda conn: catalog), \
                mock.patch.dict(
                    "os.environ",
                    {"PUBLIC_PAGES_BASE": "https://dt.yyhlzj.com"}):
            data = client.get("/api/regions").json()
        by_key = {r["region_key"]: r for r in data["regions"]}
        self.assertEqual(
            "https://dt.yyhlzj.com/hangzhou.html",
            by_key["hangzhou"]["broadcast"]["leaderboard_url_derived"])
        # env 已配时派生恒赢（库存自定义值不再生效，derived 如实透出）
        self.assertEqual(
            "https://dt.yyhlzj.com/junpin.html",
            by_key["junpin"]["broadcast"]["leaderboard_url_derived"])
        self.assertNotIn("broadcast", by_key["nobc"])


class SpaJsonApiContractTests(unittest.TestCase):
    """全 SPA 单壳契约（2026-10-14 D1 全量迁移）。

    三组契约：
    * 旧 HTML 页路由（``/`` ``/org`` … ``/calendar``）一律返回同一 SPA 单壳；
    * 无会话时所有数据面 ``/api/*`` GET 一律 401（fail-closed），壳路由不设闸；
    * catch-all：未知非 API 路径回壳 200，未知 ``api/`` ``auth/`` 前缀 → 404。
    """

    #: 旧 SSR 页路由 → 现均回同一单壳。
    _SHELL_PAGES = (
        "/", "/org", "/members", "/member-grants", "/grants", "/audit",
        "/records", "/pipelines", "/pipelines/audit", "/pipelines/history",
        "/roster", "/recipients", "/calendar", "/phones",
    )

    #: 数据面 GET 读面（无会话一律 fail-closed）。
    _READ_APIS = (
        "/api/me", "/api/org/overview", "/api/regions",
        "/api/records", "/api/grants", "/api/audit",
        "/api/pipelines", "/api/pipelines/audit", "/api/pipelines/history",
        "/api/recipients", "/api/roster", "/api/roster/targets",
        "/api/roster/qudao", "/api/calendar",
        "/api/phones", "/api/phones/bindings",
        "/api/pipelines/recipient-override?service_id=robot-hangzhou",
    )

    def test_legacy_page_routes_all_serve_the_same_shell(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        for path in self._SHELL_PAGES:
            with self.subTest(path=path):
                assert_spa_shell(client.get(path, follow_redirects=False))

    def test_no_session_api_reads_are_fail_closed(self):
        """壳路由不设闸；数据面 deny-by-default，无会话一律 401。"""
        client = TestClient(_app())
        for path in self._READ_APIS:
            with self.subTest(path=path):
                self.assertEqual(401, client.get(path).status_code)

    def test_admin_session_json_reads_answer_ok(self):
        admin = TestClient(_app(viewer=_admin_viewer()))
        _login(admin)

        me = admin.get("/api/me").json()
        self.assertEqual("ok", me["status"])
        self.assertTrue(me["is_admin"])
        self.assertIn("capabilities", me)

        for path, key in (
            ("/api/regions", "regions"),
            ("/api/roster", "roster"),
            ("/api/roster/qudao", "stores"),
            ("/api/audit", "rows"),
            ("/api/grants", "grants"),
            ("/api/pipelines/audit", "rows"),
            ("/api/pipelines/history", "rows"),
            ("/api/calendar", "days"),
            ("/api/phones", "phones"),
            ("/api/phones/bindings", "bindings"),
        ):
            with self.subTest(path=path):
                data = admin.get(path).json()
                self.assertEqual("ok", data["status"])
                self.assertIn(key, data)

        with mock.patch("common.ops_web.app._collect_records", lambda conn: []):
            records = admin.get("/api/records").json()
        self.assertEqual("ok", records["status"])
        self.assertEqual(8, len(records["sources"]))

        pipelines = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(pipelines)
        pl = pipelines.get("/api/pipelines").json()
        self.assertEqual("ok", pl["status"])
        self.assertIn("groups", pl)

    def test_catch_all_unknown_page_serves_shell(self):
        client = TestClient(_app())
        assert_spa_shell(client.get("/deep/unknown/page", follow_redirects=False))

    def test_catch_all_unknown_api_prefix_returns_404(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        for path in ("/api/no-such-endpoint", "/auth/no-such-token"):
            with self.subTest(path=path):
                resp = client.get(path)
                self.assertEqual(404, resp.status_code)
                self.assertEqual("not_found", resp.json()["detail"])
