"""test_ops_web_app 的 data_fix 域测试；断言自原文件等价迁移。"""

from tests.common.ops_web_test_support import *


class DataFixApiTests(unittest.TestCase):
    """数据修正端点（2026-10-16）：preview/apply + 审计读面。"""

    def _app(self, connection=None):
        return create_app(
            settings=_fake_settings(), session_secret=SECRET,
            db_connector=_connector(connection or _Connection()),
            viewer_resolver=_StaticViewerResolver(_admin_viewer()),
        )

    def test_realign_preview_is_read_only(self):
        client = TestClient(self._app())
        _login(client)
        with mock.patch(
            "common.ops_web.app.org_catalog.fetch_region_keys",
            return_value=("shaoxing", "other"),
        ), mock.patch(
            "common.ops_web.app.data_fix.preview_person_region_realign",
            return_value={"person_name": "李树军", "from_region": "shaoxing",
                          "to_region": "other", "row_count": 20,
                          "sales_amount": 159230.0, "date_from": "2026-09-01",
                          "date_to": "2026-09-23"},
        ) as preview:
            resp = client.post("/api/data-fix/realign", json={
                "person_name": "李树军", "from_region": "shaoxing",
                "to_region": "other"})
        self.assertEqual(200, resp.status_code)
        body = resp.json()
        self.assertEqual(False, body["applied"])
        self.assertEqual(20, body["row_count"])
        preview.assert_called_once()

    def test_realign_apply_calls_writer_with_actor(self):
        client = TestClient(self._app())
        _login(client)
        with mock.patch(
            "common.ops_web.app.org_catalog.fetch_region_keys",
            return_value=("shaoxing", "other"),
        ), mock.patch(
            "common.ops_web.app.data_fix.realign_offline_person_region",
            return_value={"person_name": "李树军", "from_region": "shaoxing",
                          "to_region": "other", "row_count": 20,
                          "sales_amount": 159230.0, "date_from": "2026-09-01",
                          "date_to": "2026-09-23"},
        ) as writer:
            resp = client.post("/api/data-fix/realign", json={
                "person_name": "李树军", "from_region": "shaoxing",
                "to_region": "other", "apply": True})
        self.assertEqual(200, resp.status_code)
        self.assertEqual(True, resp.json()["applied"])
        self.assertEqual(ADMIN_USERID, writer.call_args.kwargs["actor"])

    def test_realign_rejects_bad_payload_and_unknown_region(self):
        client = TestClient(self._app())
        _login(client)
        with mock.patch(
            "common.ops_web.app.org_catalog.fetch_region_keys",
            return_value=("shaoxing",),
        ):
            for payload in (
                {},                                                    # 全缺
                {"person_name": "", "from_region": "a", "to_region": "b"},
                {"person_name": "甲", "from_region": "a", "to_region": "a"},  # 同区
                {"person_name": "甲", "from_region": "a", "to_region": "b"},  # 未注册
            ):
                self.assertEqual(
                    400, client.post("/api/data-fix/realign",
                                     json=payload).status_code, payload)

    def test_realign_requires_login(self):
        client = TestClient(self._app())
        self.assertEqual(
            401, client.post("/api/data-fix/realign", json={
                "person_name": "甲", "from_region": "a",
                "to_region": "b"}).status_code)
        self.assertEqual(
            401, client.get("/api/data-fix/audits").status_code)

    def test_audits_endpoint_returns_rows(self):
        client = TestClient(self._app())
        _login(client)
        with mock.patch(
            "common.ops_web.app.data_fix.fetch_recent_audits",
            return_value=[{"actor": "admin", "action": "realign",
                           "fix_type": "offline_person_region_realign",
                           "target": "李树军", "detail": "shaoxing→other",
                           "row_count": 20,
                           "created_at": "2026-10-16 10:00:00"}],
        ):
            resp = client.get("/api/data-fix/audits")
        self.assertEqual(200, resp.status_code)
        row = resp.json()["audits"][0]
        self.assertEqual("归位", row["action_label"])
        self.assertEqual("线下人员归属归位", row["fix_type_label"])
        self.assertEqual(20, row["row_count"])
