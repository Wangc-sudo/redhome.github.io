"""test_ops_web_app 的 audit 域测试；断言自原文件等价迁移。"""

from tests.common.ops_web_test_support import *


class GrantWriteApiTests(unittest.TestCase):
    def test_grant_writes_record_and_audit_with_session_actor(self):
        connection = _Connection()
        client = TestClient(_app(viewer=_admin_viewer(), connection=connection))
        _login(client)

        response = client.post("/api/grants", json={
            "user_id": "u-zhang", "grant_type": "scope",
            "grant_key": "fin", "note": "财务页",
        })

        self.assertEqual(200, response.status_code)
        executed = connection.cursor_instance.executed
        sql = "\n".join(q for q, _ in executed)
        self.assertIn("INSERT INTO `bi_authz_grant`", sql)
        self.assertIn("INSERT INTO `bi_authz_grant_audit`", sql)
        audit_params = [p for q, p in executed if "audit" in q][0]
        self.assertEqual(ADMIN_USERID, audit_params[0])
        self.assertEqual(1, connection.commits)

    def test_region_scoped_scope_key_is_accepted(self):
        """G4 D4：`scope='roster:other'` 区域级键经 _SCOPE_KEY_RE 放行并落 audit。"""
        connection = _Connection()
        client = TestClient(_app(viewer=_admin_viewer(), connection=connection))
        _login(client)

        response = client.post("/api/grants", json={
            "user_id": "u-other", "grant_type": "scope",
            "grant_key": "roster:other", "note": "其他区域名册",
        })

        self.assertEqual(200, response.status_code)
        executed = connection.cursor_instance.executed
        sql = "\n".join(q for q, _ in executed)
        self.assertIn("INSERT INTO `bi_authz_grant`", sql)
        self.assertIn("INSERT INTO `bi_authz_grant_audit`", sql)
        grant_params = [
            p for q, p in executed if q.startswith("INSERT INTO `bi_authz_grant` ")
        ]
        self.assertIn("roster:other", grant_params[0])

    def test_grant_rejects_bad_fields(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)

        for payload in (
            {"user_id": "", "grant_type": "scope", "grant_key": "fin"},
            {"user_id": "u", "grant_type": "superuser", "grant_key": "x"},
            {"user_id": "u", "grant_type": "admin", "grant_key": "all"},
            {"user_id": "u", "grant_type": "scope"},
        ):
            self.assertEqual(
                400, client.post("/api/grants", json=payload).status_code,
                msg=repr(payload),
            )

    def test_delete_removes_and_audits(self):
        connection = _Connection()
        client = TestClient(_app(viewer=_admin_viewer(), connection=connection))
        _login(client)

        response = client.post("/api/grants/delete", json={
            "user_id": "u-zhang", "grant_type": "scope", "grant_key": "fin",
        })

        self.assertEqual(200, response.status_code)
        executed = connection.cursor_instance.executed
        sql = "\n".join(q for q, _ in executed)
        self.assertIn("DELETE FROM `bi_authz_grant`", sql)
        self.assertIn("revoke", [p[1] for q, p in executed if "audit" in q])

    def test_write_failure_is_a_generalized_503(self):
        client = TestClient(
            _app(viewer=_admin_viewer(), connector_error=RuntimeError("down"))
        )
        _login(client)

        response = client.post("/api/grants", json={
            "user_id": "u", "grant_type": "scope", "grant_key": "fin",
        })

        self.assertEqual(503, response.status_code)
        self.assertNotIn("down", response.text)


class RegionBatchTests(unittest.TestCase):
    def test_only_matching_region_members_are_granted_each_with_audit(self):
        connection = _Connection({"dim_robot_member": _MEMBERS})
        client = TestClient(_app(viewer=_admin_viewer(), connection=connection))
        _login(client)

        response = client.post("/api/grants/region", json={"region": "hangzhou"})

        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok", "written": 2}, response.json())
        executed = connection.cursor_instance.executed
        grant_params = [
            p for q, p in executed
            if q.startswith("INSERT INTO `bi_authz_grant` ")
        ]
        self.assertEqual(["u-zhang", "u-wang"], [p[0] for p in grant_params])
        # 批量也必须逐行落 audit（铁律：每次变更一行流水）。
        audit_params = [p for q, p in executed if "audit" in q]
        self.assertEqual(2, len(audit_params))
        self.assertEqual(
            ["u-zhang", "u-wang"], [p[2] for p in audit_params]
        )

    def test_region_requires_a_value(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)

        self.assertEqual(
            400, client.post("/api/grants/region", json={"region": " "}).status_code
        )


class GrantsAndAuditPageTests(unittest.TestCase):
    def test_grants_page_serves_spa_shell(self):
        """「授权现状」已停用（2026-10-13）；旧书签路由与 SPA 同壳。

        2026-10-14 全 SPA 迁移：停用提示（「授权管理功能已停用」+ 链接）
        已由 Vue 页承载，SSR 提示删除；数据面另钉 /api/grants。
        """
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        assert_spa_shell(client.get("/grants", follow_redirects=False))

    def test_audit_api_lists_entries_with_chinese_labels(self):
        connection = _Connection({
            "bi_authz_grant_audit`": [
                (ADMIN_USERID, "grant", "u-zhang", "scope", "fin", "",
                 "2026-09-21 10:00:00.000000"),
            ],
        })
        client = TestClient(_app(viewer=_admin_viewer(), connection=connection))
        _login(client)

        data = client.get("/api/audit").json()

        self.assertEqual("ok", data["status"])
        row = data["rows"][0]
        self.assertEqual("授权", row["action_label"])        # grant 中文化
        self.assertEqual("看板页面", row["grant_type_label"])  # scope 中文化
        self.assertEqual("u-zhang", row["user_id"])
        self.assertEqual("fin", row["grant_key"])


class GrantsEmptyRowRegionTests(unittest.TestCase):
    """B5「暂无行级数据」标注（2026-10-15 裁决）：/api/grants 附带
    ``empty_row_regions`` = 词表键 − 事实表值域 − {offline_all（聚合键）}；
    探针失败 fail-soft 为空，不拖垮读面。"""

    def _connection(self):
        return _Connection({
            "dim_org_region": [
                {"id": 1, "region_key": "hangzhou", "label": "杭州",
                 "sort_order": 0, "enabled": 1, "updated_by": None,
                 "updated_at": None},
                {"id": 2, "region_key": "shaoxing", "label": "绍兴",
                 "sort_order": 1, "enabled": 1, "updated_by": None,
                 "updated_at": None},
                {"id": 3, "region_key": "offline_all", "label": "线下整体",
                 "sort_order": 2, "enabled": 1, "updated_by": None,
                 "updated_at": None},
                {"id": 4, "region_key": "junpin", "label": "君品雅院",
                 "sort_order": 3, "enabled": 1, "updated_by": None,
                 "updated_at": None},
                {"id": 5, "region_key": "hq", "label": "总部",
                 "sort_order": 4, "enabled": 1, "updated_by": None,
                 "updated_at": None},
            ],
            "fact_daily_report_offline": [
                {"region": "hangzhou"}, {"region": "shaoxing"},
            ],
        })

    def test_grants_api_marks_regions_without_row_data(self):
        client = TestClient(_app(viewer=_admin_viewer(),
                                 connection=self._connection()))
        _login(client)

        data = client.get("/api/grants").json()

        self.assertEqual("ok", data["status"])
        # hangzhou/shaoxing 有行级数据；offline_all 聚合键豁免；
        # junpin/hq 词表有、事实无 → 标注。
        self.assertEqual(["hq", "junpin"], data["empty_row_regions"])

    def test_grants_api_probe_failure_keeps_read_ok(self):
        client = TestClient(_app(viewer=_admin_viewer(),
                                 connection=self._connection()))
        _login(client)

        with mock.patch("common.ops_web.app._fact_region_keys",
                        side_effect=RuntimeError("boom")):
            data = client.get("/api/grants").json()

        self.assertEqual("ok", data["status"])
        self.assertEqual([], data["empty_row_regions"])  # fail-soft：仅缺徽标


class RecordsPageTests(unittest.TestCase):
    """统一「操作记录」页（任务3 2026-10-12：4 处审计收口 + 运行流水）。"""

    _GRANT = [{
        "actor": ADMIN_USERID, "action": "grant", "user_id": "u-zhang",
        "grant_type": "scope", "grant_key": "fin", "note": "",
        "created_at": "2026-09-21 10:00:00.000000",
    }]
    _PIPELINE = [{
        "actor": ADMIN_USERID, "action": "toggle",
        "service_id": "robot-hangzhou", "detail": "停用",
        "created_at": "2026-09-21 11:00:00.000000",
    }]
    _ROSTER = [{
        "actor": ADMIN_USERID, "action": "add", "scope": "hangzhou",
        "entity_key": "A店", "person_name": "张三", "detail": "",
        "created_at": "2026-09-21 12:00:00.000000",
    }]
    _CALENDAR = [{
        "actor": ADMIN_USERID, "action": "upsert",
        "business_date": date(2026, 9, 21), "year": 2026, "month": 9,
        "detail": "改上班", "created_at": "2026-09-21 13:00:00.000000",
    }]
    _HISTORY = [{
        "id": 1, "service_id": "robot-hangzhou", "trigger_type": "cron",
        "status": "success", "exit_code": 0, "note": "",
        "started_at": "2026-09-21 14:00:00.000000",
        "finished_at": "2026-09-21 14:05:00.000000",
    }]
    _RECIPIENT = [{
        "actor": ADMIN_USERID, "action": "add", "service_id": "robot-hangzhou",
        "mode": "include", "target_type": "user_id", "target_value": "u9",
        "detail": "", "created_at": "2026-09-21 15:00:00.000000",
    }]
    _PHONE = [{
        "actor": ADMIN_USERID, "action": "claim", "phone_no": "13300001111",
        "detail": "认领持卡人：张三",
        "created_at": "2026-09-21 16:00:00.000000",
    }]

    @contextmanager
    def _patched(self):
        with mock.patch.object(bi_authz, "fetch_audit_rows",
                               lambda conn: list(self._GRANT)), \
             mock.patch.object(ops_control, "fetch_pipeline_audit",
                               lambda conn, limit=200: list(self._PIPELINE)), \
             mock.patch.object(report_roster, "fetch_roster_audit",
                               lambda conn, limit=200: list(self._ROSTER)), \
             mock.patch.object(calendar_store, "fetch_recent_audits",
                               lambda conn, limit=200: list(self._CALENDAR)), \
             mock.patch.object(phone_registry, "fetch_phone_audit",
                               lambda conn, limit=200: list(self._PHONE)), \
             mock.patch.object(ops_control, "fetch_run_history",
                               lambda conn, limit=200, service_id=None:
                               list(self._HISTORY)):
            yield

    def _client(self):
        client = TestClient(_app(viewer=_admin_viewer(),
                                 connection=_Connection({})))
        _login(client)
        return client

    def test_records_api_merges_all_sources(self):
        with self._patched():
            data = self._client().get("/api/records").json()
        self.assertEqual("ok", data["status"])
        self.assertEqual(["授权", "定时任务", "填报名册", "播报日历",
                          "收件人覆盖", "定时运行", "数据修正", "号码台账"],
                         data["sources"])
        sources = {r["source"] for r in data["records"]}
        for source in ("授权", "定时任务", "填报名册", "播报日历", "定时运行",
                       "号码台账"):
            self.assertIn(source, sources)
        targets = [r["target"] for r in data["records"]]
        self.assertIn("fin", targets)          # 授权对象（grant_key）
        self.assertIn("杭州 A店", targets)      # 名册对象（scope 中文化 + entity_key）
        details = [r["detail"] for r in data["records"]]
        self.assertIn("张三", details)         # 名册详情（person_name）
        actors = [r["actor"] for r in data["records"]]
        self.assertIn("系统", actors)          # 运行流水操作人

    def test_records_api_filters_by_source(self):
        with self._patched():
            data = self._client().get(
                "/api/records", params={"source": "填报名册"}).json()
        self.assertTrue(data["records"])       # 非空
        self.assertTrue(all(r["source"] == "填报名册"
                            for r in data["records"]))
        details = [r["detail"] for r in data["records"]]
        self.assertIn("张三", details)
        self.assertNotIn("fin", [r["target"] for r in data["records"]])

    # test_write_feedback_banner_present：2026-10-14 全 SPA 迁移：写入成功
    # /失败提示（id="flash" / showFlash）已由 Vue 页承载，故删。

    def test_records_api_includes_recipient_override_source(self):
        """方案 B：收件人覆盖审计并入操作记录（来源/动作/对象中文化）。"""
        with self._patched(), mock.patch.object(
                recipient_override, "fetch_recent_audits",
                lambda conn, limit=200: list(self._RECIPIENT)):
            data = self._client().get("/api/records").json()
        override = [r for r in data["records"] if r["source"] == "收件人覆盖"]
        self.assertEqual(1, len(override))
        # 对象 = service_id +（mode 中文化 + target_type 中文化 + 值）
        self.assertEqual("robot-hangzhou 追加 账号 u9", override[0]["target"])

    def test_records_api_fails_soft_when_override_audit_read_fails(self):
        """缺表（迁移未跑）或读异常：只跳过该来源，不整页 503。

        #62 上线后曾实测踩到：deploy 不跑迁移，dim_recipient_override_audit
        缺失时 /records 整页 503。此用例钉住 fail-soft 口径——其余来源照常
        展示，且不因该来源读失败而出现「读取失败」。
        """
        def _boom(*_args, **_kwargs):
            raise RuntimeError("table missing")

        with self._patched(), mock.patch.object(
                recipient_override, "fetch_recent_audits", _boom):
            response = self._client().get("/api/records")
        self.assertEqual(200, response.status_code)
        data = response.json()
        sources = {r["source"] for r in data["records"]}
        self.assertIn("授权", sources)         # 其余来源仍正常展示
        self.assertIn("定时运行", sources)
        self.assertNotIn("读取失败", [r["detail"] for r in data["records"]])


class RecordsApiTests(unittest.TestCase):
    """/api/records（D1 操作记录读面）。"""

    def test_requires_audit_capability(self):
        client = TestClient(_app(viewer=_plain_member_viewer()))
        _login(client, "u-plain")
        self.assertEqual(403, client.get("/api/records").status_code)

    def test_returns_collected_records(self):
        viewer = authz.Viewer(
            userid="u-audit", name="审计", scopes=frozenset({"audit"}),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        client = TestClient(_app(viewer=viewer))
        _login(client, "u-audit")
        with mock.patch(
                "common.ops_web.app._collect_records",
                lambda conn: [(datetime(2026, 10, 14, 1, 0, 0), "填报名册",
                               "u-other", "新增", "其他 李树军", "月目标=93")]):
            data = client.get("/api/records").json()
        self.assertEqual("ok", data["status"])
        self.assertEqual(1, len(data["records"]))
        self.assertEqual("填报名册", data["records"][0]["source"])
        self.assertIn("填报名册", data["sources"])

    def test_authorization_records_carry_user_id(self):
        """D1 闭环修复（2026-10-15）：授权来源行下发 ``user_id``，供「操作记录」
        页回源深链 ``/grants?user=`` 过滤——此前该列被丢在元组外，深链恒空过滤。"""
        viewer = authz.Viewer(
            userid="u-audit", name="审计", scopes=frozenset({"audit"}),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        client = TestClient(_app(viewer=viewer))
        _login(client, "u-audit")
        with mock.patch(
                "common.ops_web.app._collect_records",
                lambda conn: [(datetime(2026, 10, 15, 1, 0, 0), "授权",
                               "admin", "新增授权", "roster:other", "备注",
                               "u-other")]):
            data = client.get("/api/records").json()
        self.assertEqual("u-other", data["records"][0]["user_id"])

    def test_non_authorization_records_default_user_id_empty(self):
        """非授权来源（6 元组）不误填 user_id，保持空串。"""
        viewer = authz.Viewer(
            userid="u-audit", name="审计", scopes=frozenset({"audit"}),
            allowed_regions=frozenset(), is_admin=False, admitted=True,
        )
        client = TestClient(_app(viewer=viewer))
        _login(client, "u-audit")
        with mock.patch(
                "common.ops_web.app._collect_records",
                lambda conn: [(datetime(2026, 10, 15, 1, 0, 0), "定时任务",
                               "admin", "启用", "kb-gateway", "")]):
            data = client.get("/api/records").json()
        self.assertEqual("", data["records"][0]["user_id"])
