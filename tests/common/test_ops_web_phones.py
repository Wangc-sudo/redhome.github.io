"""test_ops_web_app 的 phones 域测试；断言自原文件等价迁移。"""

from tests.common.ops_web_test_support import *


class PhoneRegistryApiTests(unittest.TestCase):
    """号码台账端点（2026-10-26 收尾）：读面 capability 闸 + 五个写面。

    模块函数一律 mock（真源 SQL 形态由 test_phone_registry.py 钉住），
    这里只钉端点的闸、载荷校验与 actor 透传。
    """

    def _client(self, viewer=None):
        v = viewer or _admin_viewer()
        client = TestClient(_app(viewer=v))
        _login(client, v.userid)
        return client

    def test_reads_require_phone_capability(self):
        client = TestClient(_app(viewer=_plain_member_viewer()))
        _login(client, "u-plain")
        self.assertEqual(403, client.get("/api/phones").status_code)
        self.assertEqual(403, client.get("/api/phones/bindings").status_code)

    def test_list_returns_summary_and_rows(self):
        client = self._client()
        with mock.patch.object(
                phone_registry, "fetch_phone_list",
                lambda conn, keyword="", unclaimed_only=False: [
                    {"phone_no": "13300001111", "phone_type": "主卡",
                     "actual_user": "张三", "needs_claim": 0, "enabled": 1,
                     "updated_at": None},
                ]), mock.patch.object(
                phone_registry, "summarize_registry",
                lambda conn: {"total": 71, "unclaimed": 8, "dead": 2,
                              "by_user": [("张三", 3)]}):
            data = client.get("/api/phones").json()
        self.assertEqual("ok", data["status"])
        self.assertEqual({"total": 71, "unclaimed": 8, "dead": 2},
                         data["summary"])
        self.assertEqual("13300001111", data["phones"][0]["phone_no"])

    def test_bindings_returns_rows(self):
        client = self._client()
        with mock.patch.object(
                phone_registry, "fetch_bindings",
                lambda conn, keyword="": [
                    {"id": 5, "phone_no": "13300001111",
                     "channel_category": "快手",
                     "platform_account": "习水村酒类专营店",
                     "note": None, "enabled": 1, "updated_by": "ops",
                     "updated_at": None, "actual_user": "张三",
                     "physical_device": "序号① 蓝色手机", "status": "正常"},
                ]):
            data = client.get(
                "/api/phones/bindings", params={"keyword": "习水村"}).json()
        self.assertEqual("ok", data["status"])
        self.assertEqual("习水村酒类专营店",
                         data["bindings"][0]["platform_account"])

    def test_claim_ok_and_audit_actor(self):
        client = self._client()
        with mock.patch.object(
                phone_registry, "set_phone_user", return_value=1) as patched:
            response = client.post("/api/phones/claim", json={
                "phone_no": "13300001111", "actual_user": "张三"})
        self.assertEqual(200, response.status_code)
        args, kwargs = patched.call_args
        self.assertEqual("13300001111", args[1])
        self.assertEqual("张三", args[2])
        self.assertEqual(ADMIN_USERID, kwargs["actor"])

    def test_claim_rejects_bad_payload_and_unknown_phone(self):
        client = self._client()
        self.assertEqual(400, client.post(
            "/api/phones/claim", json={"phone_no": "13300001111"}).status_code)
        with mock.patch.object(phone_registry, "set_phone_user",
                               lambda conn, phone, user, *, actor: 0):
            self.assertEqual(400, client.post("/api/phones/claim", json={
                "phone_no": "13300001111", "actual_user": "张三"}).status_code)

    def test_save_validates_and_writes(self):
        client = self._client()
        with mock.patch.object(phone_registry, "upsert_phone_number",
                               lambda conn, record, *, actor: None):
            ok = client.post("/api/phones/save", json={
                "phone_no": "13300001111", "phone_type": "主卡",
                "actual_user": "张三"})
            bad = client.post("/api/phones/save", json={"phone_no": "  "})
        self.assertEqual(200, ok.status_code)
        self.assertEqual(400, bad.status_code)

    def test_toggle_and_binding_guards(self):
        client = self._client()
        with mock.patch.object(phone_registry, "set_phone_enabled",
                               lambda conn, phone, enabled, *, actor: 1):
            self.assertEqual(200, client.post("/api/phones/toggle", json={
                "phone_no": "13300001111", "enabled": False}).status_code)
        with mock.patch.object(phone_registry, "set_binding_enabled",
                               lambda conn, bid, enabled, *, actor: 1):
            self.assertEqual(200, client.post(
                "/api/phones/binding/toggle",
                json={"id": 5, "enabled": False}).status_code)
            # 非法主键（0 / 负 / 非整）一律 400
            self.assertEqual(400, client.post(
                "/api/phones/binding/toggle",
                json={"id": 0, "enabled": True}).status_code)
        with mock.patch.object(phone_registry, "upsert_phone_binding",
                               lambda conn, *a, **k: None):
            self.assertEqual(200, client.post(
                "/api/phones/binding/save", json={
                    "phone_no": "13300001111", "channel_category": "快手",
                    "platform_account": "习水村酒类专营店"}).status_code)

    def test_writes_require_phone_capability(self):
        client = TestClient(_app(viewer=_plain_member_viewer()))
        _login(client, "u-plain")
        for path, payload in (
            ("/api/phones/claim",
             {"phone_no": "13300001111", "actual_user": "张三"}),
            ("/api/phones/save", {"phone_no": "13300001111"}),
            ("/api/phones/toggle",
             {"phone_no": "13300001111", "enabled": False}),
            ("/api/phones/binding/save", {"phone_no": "13300001111"}),
            ("/api/phones/binding/toggle", {"id": 1, "enabled": False}),
        ):
            with self.subTest(path=path):
                self.assertEqual(403, client.post(path, json=payload)
                                 .status_code)
