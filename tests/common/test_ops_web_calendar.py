"""test_ops_web_app 的 calendar 域测试；断言自原文件等价迁移。"""

from tests.common.ops_web_test_support import *


class CalendarPageTests(unittest.TestCase):
    """播报日历页（DB 裁决层操作面：非技术同学用；2026-10-16 纯播报口径）。"""

    def _calendar_app(self, connection=None):
        settings = SimpleNamespace(
            **vars(_fake_settings()), calendar_seed_path="seed.json")
        return create_app(
            settings=settings, session_secret=SECRET,
            db_connector=_connector(connection),
            viewer_resolver=_StaticViewerResolver(_admin_viewer()),
        )


    def test_api_renders_month_grid_with_source_badges(self):
        """2026-10-14 全 SPA 迁移：日历格呈现（cal-src-* 徽标 / 内联
        calendarToggle / 导入按钮）已迁至 Vue 页，改钉 JSON 读面
        /api/calendar 的网格、来源标记与审计动作中文标签。"""
        connection = _Connection(rows_by_keyword={
            # 审计查询的表名包含 dim_calendar，必须先于月份查询命中
            "dim_calendar_override_audit": [
                {"actor": "admin", "action": "import", "business_date": None,
                 "year": 2027, "month": None, "detail": "d",
                 "created_at": "2026-10-10 08:00:00"},
            ],
            "dim_calendar": [
                {"business_date": date(2026, 10, 6), "is_workday": 0,
                 "source": "holiday_cn", "note": "国庆"},
                {"business_date": date(2026, 10, 7), "is_workday": 1,
                 "source": "manual", "note": "改上班"},
            ],
        })
        client = TestClient(self._calendar_app(connection))
        _login(client)

        data = client.get("/api/calendar", params={"month": "2026-10"}).json()

        self.assertEqual("ok", data["status"])
        self.assertEqual("2026-10", data["month"])
        self.assertEqual(31, len(data["days"]))                    # 十月 31 天
        self.assertEqual(date(2026, 10, 1).weekday(), data["first_weekday"])
        self.assertEqual("2026-09", data["prev_month"])
        self.assertEqual("2026-11", data["next_month"])
        by_day = {d["day"]: d for d in data["days"]}
        self.assertEqual("holiday_cn", by_day[6]["source"])        # 法定来源
        self.assertFalse(by_day[6]["is_workday"])
        self.assertEqual("国庆", by_day[6]["note"])
        self.assertEqual("manual", by_day[7]["source"])            # 人工裁定
        self.assertTrue(by_day[7]["is_workday"])
        self.assertEqual("法定导入", data["audits"][0]["action_label"])

    def test_page_falls_back_to_current_month_on_bad_param(self):
        client = TestClient(self._calendar_app(_Connection()))
        _login(client)
        self.assertEqual(200, client.get("/calendar?month=not-a-month").status_code)

    def test_toggle_writes_override_and_rewrites_month(self):
        with mock.patch(
            "common.ops_web.app.calendar_store.upsert_override"
        ) as upsert, mock.patch(
            "common.ops_web.app.calendar_store.rewrite_month", return_value=31
        ) as rewrite:
            client = TestClient(self._calendar_app(_Connection()))
            _login(client)
            resp = client.post("/api/calendar/toggle", json={
                "date": "2026-10-07", "is_workday": True, "note": "改上班",
            })

        self.assertEqual(200, resp.status_code)
        _, day, is_workday = upsert.call_args.args
        self.assertEqual(date(2026, 10, 7), day)
        self.assertIs(is_workday, True)
        self.assertEqual(ADMIN_USERID, upsert.call_args.kwargs["actor"])
        self.assertEqual("改上班", upsert.call_args.kwargs["note"])
        _, seed_path, year, month = rewrite.call_args.args
        self.assertEqual("seed.json", seed_path)
        self.assertEqual((2026, 10), (year, month))

    def test_toggle_rejects_bad_payload(self):
        client = TestClient(self._calendar_app(_Connection()))
        _login(client)
        for payload in (
            {"date": "not-a-date", "is_workday": True},
            {"date": "2026-10-07", "is_workday": "yes"},
            {"date": "2026-10-07"},
        ):
            self.assertEqual(
                400,
                client.post("/api/calendar/toggle", json=payload).status_code,
                msg=repr(payload),
            )

    def test_import_year_endpoint_removed(self):
        """法定节假日导入随纯播报口径退役（2026-10-16）：端点不复存在。"""
        client = TestClient(self._calendar_app(_Connection()))
        _login(client)
        resp = client.post("/api/calendar/import-year", json={"year": 2027})
        self.assertIn(resp.status_code, (404, 405))

    def test_reset_month_clears_manual_and_rewrites(self):
        with mock.patch(
            "common.ops_web.app.calendar_store.reset_month_overrides",
            return_value=2,
        ) as reset, mock.patch(
            "common.ops_web.app.calendar_store.rewrite_month", return_value=30
        ):
            client = TestClient(self._calendar_app(_Connection()))
            _login(client)
            resp = client.post(
                "/api/calendar/reset-month", json={"month": "2026-10"})

        self.assertEqual(200, resp.status_code)
        self.assertEqual(2, resp.json()["deleted"])
        _, year, month = reset.call_args.args
        self.assertEqual((2026, 10), (year, month))
        self.assertEqual(ADMIN_USERID, reset.call_args.kwargs["actor"])

    def test_reset_month_rejects_bad_month(self):
        client = TestClient(self._calendar_app(_Connection()))
        _login(client)
        self.assertEqual(
            400,
            client.post(
                "/api/calendar/reset-month", json={"month": "2026-13"}
            ).status_code,
        )
