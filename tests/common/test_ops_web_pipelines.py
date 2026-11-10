"""test_ops_web_app 的 pipelines 域测试；断言自原文件等价迁移。"""

from tests.common.ops_web_test_support import *


class PipelinePageTests(unittest.TestCase):
    """定时任务面（2026-10-14 全 SPA 迁移）。

    旧 SSR 页的纯呈现细节（表单 id / 内联 onclick / 视图 tab / 弹窗挂载点 /
    行内列序 / 交叉导航链接）已迁至 Vue 页（AppShell + 定时任务页），相应
    用例删除；数据与权限断言改钉 JSON 读面 /api/pipelines（+ audit/history）。
    """

    def _pick(self, data, service_id):
        """从 /api/pipelines 的 groups 中取指定 service 行。"""
        for group in data["groups"]:
            for service in group["services"]:
                if service["service_id"] == service_id:
                    return service
        self.fail(f"/api/pipelines 未含 {service_id}")

    def test_pipelines_api_lists_fleet_with_schedule_and_latest(self):
        store = _PipelineStore()
        store.recent = [{
            "service_id": "robot-hangzhou", "requested_by": "王城",
            "status": "finished", "note": None, "exit_code": 0,
            "created_at": "2026-09-30 10:00:00", "finished_at": "2026-09-30 10:01:00",
        }]
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_recent():
            data = client.get("/api/pipelines").json()

        self.assertEqual("ok", data["status"])
        row = self._pick(data, "robot-hangzhou")
        self.assertEqual("0 2 * * *", row["schedule"])       # 原表达式备查
        self.assertEqual("每天 02:00", row["schedule_zh"])   # cron 人性化
        self.assertIn("填报提醒", row["service_label"])       # robot- 家族中文说明
        self.assertTrue(row["enabled"])
        self.assertEqual("成功", row["latest"]["status_label"])  # finished 中文化
        self.assertEqual("2026-09-30 18:00:00",
                         row["latest"]["created_at"])            # UTC → 北京时

    def test_pipelines_api_exposes_params_and_alertable(self):
        """巡检线读面携带 params 与 alertable，供前端「告警设置」入口。"""
        client = TestClient(_fleet_app(
            viewer=_admin_viewer(),
            fleet=("channel-missing-check", "robot-hangzhou"),
            configs={
                "channel-missing-check": {
                    "schedule": "40 10 * * *", "enabled": True,
                    "params": {"alert_threshold": "critical",
                               "alert_cooldown_hours": 6},
                },
                "robot-hangzhou": {"schedule": "0 2 * * *", "enabled": True},
            },
        ))
        _login(client)
        data = client.get("/api/pipelines").json()

        alert = self._pick(data, "channel-missing-check")
        self.assertTrue(alert["alertable"])
        self.assertEqual({"alert_threshold": "critical",
                          "alert_cooldown_hours": 6}, alert["params"])
        plain = self._pick(data, "robot-hangzhou")
        self.assertFalse(plain["alertable"])
        self.assertEqual({}, plain["params"])

    def test_pipelines_api_group_and_function_classification(self):
        """群视角分组：家族后缀/显式区域映射、跨区域线与平台线归组。"""
        fleet = (
            "leaderboard-hangzhou",   # 家族：播报 + 「杭州」群
            "pages-qudao-t1",         # 显式区域：页面 + 「渠道」群（后缀非区域）
            "channel-missing-check",  # 显式：催办 + 「渠道」群
            "sync-wdt",               # 应用线：同步 + 无群
            "dingtalk-gateway",       # 平台线：钉钉 + 无群
        )
        client = TestClient(_pipeline_app(viewer=_admin_viewer(), fleet=fleet))
        _login(client)
        data = client.get("/api/pipelines").json()

        by_title = {g["title"]: g["count"] for g in data["groups"]}
        self.assertEqual(1, by_title["「杭州」群"])    # leaderboard- 区域映射
        self.assertEqual(2, by_title["「渠道」群"])    # 两条渠道线同群聚组
        self.assertEqual(2, by_title["平台 / 无群"])   # sync-wdt + dingtalk-gateway
        self.assertIn("榜单群播报",
                      self._pick(data, "leaderboard-hangzhou")["service_label"])

    def test_pipelines_api_group_order_places_platform_last(self):
        """群视角：按区域分组、平台/无群恒置末位、任一行不丢。"""
        fleet = (
            "robot-hangzhou",        # 「杭州」群
            "channel-missing-check",  # 「渠道」群（跨区域线单独成组）
            "sync-wdt",               # 平台 / 无群
            "dingtalk-gateway",       # 平台 / 无群
        )
        client = TestClient(_pipeline_app(viewer=_admin_viewer(), fleet=fleet))
        _login(client)
        data = client.get("/api/pipelines").json()

        regions = [g["region"] for g in data["groups"]]
        self.assertLess(regions.index("hangzhou"), regions.index("qudao"))
        self.assertLess(regions.index("qudao"), regions.index(None))
        ids = [s["service_id"] for g in data["groups"] for s in g["services"]]
        for sid in fleet:          # 每行都在（不得因分组丢行）
            self.assertIn(sid, ids)

    def test_pipelines_api_reminder_targets(self):
        """收受影响人列：robot=名册在册人员（绑定）；渠道到齐=名册店铺负责人。"""
        connection = _Connection({
            "dim_robot_member": [
                ("u1", "张三", "hangzhou", "门店一部"),
                ("u2", "李四", "hangzhou", "门店二部"),
                ("u3", "王五", "shaoxing", "项目部"),
            ],
            "entity_type` = 'person'": [("张三",)],   # 名册：hangzhou 只登记张三
            "entity_type` = 'store'": [
                ("天猫-A店", "赵六"),
                ("京东-B店", "钱七"),
                ("京东-B店", "赵六"),
            ],
        })
        fleet = ("robot-hangzhou", "channel-missing-check", "sync-wdt")
        client = TestClient(_pipeline_app(
            viewer=_admin_viewer(), fleet=fleet, connection=connection))
        _login(client)
        data = client.get("/api/pipelines").json()

        robot = self._pick(data, "robot-hangzhou")["reminder_html"]
        self.assertIn("张三", robot)
        self.assertIn("（1 人 · 名册）", robot)   # robot- 名册绑定：只列在册的张三
        self.assertNotIn("李四", robot)           # 非在册成员不收影响
        self.assertNotIn("王五", robot)           # 无 robot-shaoxing 行不展示
        self.assertNotIn("读取失败", robot)
        qudao = self._pick(data, "channel-missing-check")["reminder_html"]
        self.assertIn("赵六、钱七", qudao)        # 渠道名册负责人（去重排序）
        self.assertIn("（2 人 · 2 店）", qudao)

    def test_pipelines_api_reminder_targets_with_overrides(self):
        """任务级覆盖（方案 B）：有效名单 = 名册 ± include/exclude，带徽标。"""
        connection = _Connection({
            "dim_robot_member": [
                ("u1", "张三", "hangzhou", "门店一部"),
                ("u2", "李四", "hangzhou", "门店二部"),
            ],
            "entity_type` = 'person'": [("张三",), ("李四",)],
            "dim_recipient_override": [
                {"id": 1, "service_id": "robot-hangzhou", "mode": "exclude",
                 "target_type": "name", "target_value": "李四",
                 "resolved_user_id": None, "enabled": 1, "note": None,
                 "updated_by": "ops", "updated_at": None},
                {"id": 2, "service_id": "robot-hangzhou", "mode": "include",
                 "target_type": "user_id", "target_value": "u9",
                 "resolved_user_id": "u9", "enabled": 1, "note": None,
                 "updated_by": "ops", "updated_at": None},
            ],
        })
        client = TestClient(_pipeline_app(
            viewer=_admin_viewer(), fleet=("robot-hangzhou",),
            connection=connection))
        _login(client)
        data = client.get("/api/pipelines").json()
        html = self._pick(data, "robot-hangzhou")["reminder_html"]

        self.assertIn("张三", html)
        self.assertNotIn("李四", html)      # exclude 按姓名移除
        self.assertIn("u9", html)           # include 追加（未知 id 回显）
        self.assertIn("（2 人 · 名册）", html)
        self.assertIn("+1 覆盖", html)
        self.assertIn("−1 移除", html)

    def test_pipelines_api_reminder_override_read_fail_soft(self):
        """覆盖表未迁移/读失败 → 回退名册口径，本列不整体「读取失败」。"""
        connection = _Connection({
            "dim_robot_member": [("u1", "张三", "hangzhou", "门店一部")],
        })
        client = TestClient(_pipeline_app(
            viewer=_admin_viewer(), fleet=("robot-hangzhou",),
            connection=connection))
        _login(client)
        with mock.patch.object(recipient_override, "fetch_overrides",
                               side_effect=RuntimeError("no table")):
            data = client.get("/api/pipelines").json()
        html = self._pick(data, "robot-hangzhou")["reminder_html"]
        self.assertIn("张三", html)
        self.assertNotIn("读取失败", html)

    def test_pipelines_api_reminder_targets_fail_open_all_members(self):
        """名册无记录 → 区域在册填报人全员兜底（与机器人 fail-open 同语义）；
        跨多部门时按部门归组展示（2026-10-13 联动组织编排）。"""
        connection = _Connection({
            "dim_robot_member": [
                ("u1", "张三", "hangzhou", "门店一部"),
                ("u2", "李四", "hangzhou", "门店二部"),
            ],
        })
        client = TestClient(_pipeline_app(
            viewer=_admin_viewer(), fleet=("robot-hangzhou",),
            connection=connection))
        _login(client)
        data = client.get("/api/pipelines").json()
        html = self._pick(data, "robot-hangzhou")["reminder_html"]

        self.assertIn("门店一部：张三", html)   # 按部门归组（非旧的「、」连接）
        self.assertIn("门店二部：李四", html)
        self.assertIn("（2 人 · 全员兜底）", html)

    def test_pipelines_api_reminder_targets_empty_roster_hint(self):
        """渠道名册未登记 → 如实标注机器人自动回退读门店月目标表。"""
        client = TestClient(_pipeline_app(
            viewer=_admin_viewer(), fleet=("channel-missing-check",)))
        _login(client)
        data = client.get("/api/pipelines").json()
        html = self._pick(data, "channel-missing-check")["reminder_html"]
        self.assertIn("自动回退读门店月目标表", html)

    def test_pipelines_api_reminder_targets_fail_soft(self):
        """收受影响人读取故障 → 本列「读取失败」，整页不拖垮。"""
        def _boom(conn):
            raise RuntimeError("db down")
        client = TestClient(_pipeline_app(
            viewer=_admin_viewer(), fleet=("robot-hangzhou",)))
        _login(client)
        with mock.patch.object(bi_authz, "fetch_active_members", _boom):
            response = client.get("/api/pipelines")
        self.assertEqual(200, response.status_code)
        html = self._pick(response.json(), "robot-hangzhou")["reminder_html"]
        self.assertIn("读取失败", html)

    def test_pipelines_api_category_truth_overrides_derivation(self):
        """category 真源优先：注册表配 category=催办 时，即便 service_id 推导为
        「同步」，该行仍按催办装载（收受影响人列走催办取值域）。"""
        configs = {"sync-wdt": {"schedule": "0 2 * * *", "category": "催办"}}
        app = create_app(
            settings=_fake_settings(),
            session_secret=SECRET,
            db_connector=_connector(),
            viewer_resolver=_StaticViewerResolver(_admin_viewer()),
            fleet_source=lambda: ("sync-wdt",),
            config_source=StaticConfigSource(configs),
            config_publisher=None,
        )
        client = TestClient(app)
        _login(client)
        seen = []

        def _spy(conn, service_id):
            seen.append(service_id)
            return ()

        with mock.patch.object(recipient_override, "fetch_overrides", _spy):
            data = client.get("/api/pipelines").json()

        self.assertEqual(["sync-wdt"], seen)   # 真源 = 催办 → 催办装载被触发
        self.assertTrue(self._pick(data, "sync-wdt")["is_reminder"])

    def test_pipelines_api_weekday_cron_is_humanized(self):
        """工作日区间 cron（1-5）人性化：与构建器「工作日」预设同形。"""
        configs = {"sync-wdt": {"schedule": "0 18 * * 1-5"}}
        app = create_app(
            settings=_fake_settings(),
            session_secret=SECRET,
            db_connector=_connector(),
            viewer_resolver=_StaticViewerResolver(_admin_viewer()),
            fleet_source=lambda: ("sync-wdt",),
            config_source=StaticConfigSource(configs),
            config_publisher=None,
        )
        client = TestClient(app)
        _login(client)
        data = client.get("/api/pipelines").json()
        row = self._pick(data, "sync-wdt")
        self.assertEqual("工作日（周一至周五）18:00", row["schedule_zh"])
        self.assertEqual("0 18 * * 1-5", row["schedule"])   # 原表达式备查

    def test_pipeline_audit_api_lists_entries(self):
        store = _PipelineStore()
        store.audit_rows = [
            ("admin", "add", "robot-x", "0 2 * * *", "2026-09-30 10:00:00"),
            ("admin", "schedule", "robot-hangzhou", "30 20 * * 1-5",
             "2026-10-12 10:00:00"),
        ]
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_audit_rows():
            rows = client.get("/api/pipelines/audit").json()["rows"]

        self.assertIn("robot-x", [r["service_id"] for r in rows])
        self.assertEqual("新增", rows[0]["action_label"])    # action=add 中文化
        self.assertEqual("改定时", rows[1]["action_label"])  # action=schedule 中文化
        self.assertEqual("30 20 * * 1-5", rows[1]["detail"])

    def test_action_label_covers_target(self):
        """G1：名册月目标保存动作中文化（原回退英文原文 target）。"""
        from common.ops_web.app import _ACTION_LABEL, _label
        self.assertEqual("月目标", _label(_ACTION_LABEL, "target"))

    def test_pipeline_history_api_renders_entries(self):
        store = _PipelineStore()
        store.history_rows = [
            {"id": 2, "service_id": "robot-hangzhou", "trigger_type": "cron",
             "status": "failed", "exit_code": None,
             "note": "调度锁被占用或运行异常，未产生退出码",
             "started_at": "2026-10-07 02:00:01", "finished_at": "2026-10-07 02:00:02"},
            {"id": 1, "service_id": "sync-wdt", "trigger_type": "run_once",
             "status": "finished", "exit_code": 0, "note": None,
             "started_at": "2026-10-07 01:00:00", "finished_at": "2026-10-07 01:05:00"},
        ]
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_history_rows():
            data = client.get("/api/pipelines/history").json()

        by_id = {r["service_id"]: r for r in data["rows"]}
        self.assertEqual({"robot-hangzhou", "sync-wdt"}, set(by_id))
        self.assertEqual("定时", by_id["robot-hangzhou"]["trigger_label"])   # cron
        self.assertEqual("手动", by_id["sync-wdt"]["trigger_label"])          # run_once
        self.assertEqual("失败", by_id["robot-hangzhou"]["status_label"])
        self.assertEqual("成功", by_id["sync-wdt"]["status_label"])

    def test_pipeline_history_api_filters_by_service_id(self):
        store = _PipelineStore()
        store.history_rows = [
            {"id": 1, "service_id": "sync-wdt", "trigger_type": "cron",
             "status": "finished", "exit_code": 0, "note": None,
             "started_at": "2026-10-07 01:00:00", "finished_at": "2026-10-07 01:05:00"},
            {"id": 2, "service_id": "robot-hangzhou", "trigger_type": "cron",
             "status": "finished", "exit_code": 0, "note": None,
             "started_at": "2026-10-07 02:00:00", "finished_at": "2026-10-07 02:01:00"},
        ]
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_history_rows():
            data = client.get(
                "/api/pipelines/history", params={"service_id": "sync-wdt"}
            ).json()

        self.assertEqual("sync-wdt", data["service_id"])
        self.assertEqual(["sync-wdt"],
                         [r["service_id"] for r in data["rows"]])


class ZhLabelTests(unittest.TestCase):
    """中文化 helper：cron 人性化与服务中文说明（纯函数，无 IO）。"""

    def test_cron_zh_daily_multi_hours(self):
        from common.ops_web.app import _cron_zh
        self.assertEqual("每天 01:55、06:55、13:55、15:55",
                         _cron_zh("55 1,6,13,15 * * *"))
        self.assertEqual("每天 02:00", _cron_zh("0 2 * * *"))

    def test_cron_zh_weekly_and_monthly(self):
        from common.ops_web.app import _cron_zh
        self.assertEqual("每周一 09:30", _cron_zh("30 9 * * 1"))
        self.assertEqual("每月 1 日 10:00", _cron_zh("0 10 1 * *"))

    def test_cron_zh_minutely_hourly_and_fallback(self):
        from common.ops_web.app import _cron_zh
        self.assertEqual("每 5 分钟", _cron_zh("*/5 * * * *"))
        self.assertEqual("每小时第 30 分", _cron_zh("30 * * * *"))
        # 多段（播报时间多时点，2026-10-13）：逐段中文、以「；」连接
        self.assertEqual("每天 08:00；每天 12:30",
                         _cron_zh("0 8 * * *;30 12 * * *"))
        self.assertEqual("每天 02:00", _cron_zh("0 2 * * *;"))   # 空段忽略
        self.assertIsNone(_cron_zh("0 8 * * *;0 0 1 1 *"))  # 任一段不可识别
        self.assertIsNone(_cron_zh("0 0 1 1 *"))     # 带月份字段 → 回退
        self.assertIsNone(_cron_zh("not a cron"))

    def test_schedule_cell_keeps_raw_as_hint(self):
        from common.ops_web.app import _schedule_cell
        cell = _schedule_cell("55 1,6,13,15 * * *")
        self.assertIn("每天 01:55", cell)
        self.assertIn("55 1,6,13,15 * * *", cell)
        self.assertIn("常驻", _schedule_cell(None))
        self.assertEqual("weird cron", _schedule_cell("weird cron"))

    def test_service_label_curated_family_and_fallback(self):
        from common.ops_web.app import _service_label
        self.assertIn("滚动源清单", _service_label("roll-manifest", "rolls..."))
        self.assertEqual("「杭州」填报提醒（报数汇总 → 群内提醒）",
                         _service_label("robot-hangzhou", "hangzhou daily..."))
        self.assertEqual("「杭州」催办未填人 + DING（报数核对 → 群内 @ + DING）",
                         _service_label("robot-check-hangzhou", "hangzhou..."))
        self.assertEqual("「万科&大莲花&团购」榜单页生成",
                         _service_label("pages-vanke", "vanke leaderboard..."))
        self.assertEqual("原文保留", _service_label("mystery", "原文保留"))

    def test_service_name_curated_family_and_fallback(self):
        from common.ops_web.app import _service_name, _service_name_cell
        self.assertEqual("滚动清单", _service_name("roll-manifest"))
        self.assertEqual("「杭州」填报提醒", _service_name("robot-hangzhou"))
        self.assertEqual("「杭州」催办未填+DING",
                         _service_name("robot-check-hangzhou"))
        self.assertEqual("「绍兴」榜单页", _service_name("pages-shaoxing"))
        self.assertEqual("mystery", _service_name("mystery"))
        # 有中文名：中文为主、id 小字备查；无中文名：只显示 id
        cell = _service_name_cell("robot-hangzhou")
        self.assertIn("「杭州」填报提醒", cell)
        self.assertIn("（robot-hangzhou）", cell)
        self.assertEqual("mystery", _service_name_cell("mystery"))

    def test_service_region_robot_check_maps_region(self):
        """robot-check-<region> 管理群区域解析（家族长者优先于 robot-）。"""
        from common.ops_web.app import _service_region, _pipeline_group_title
        self.assertEqual("hangzhou", _service_region("robot-check-hangzhou"))
        self.assertEqual("hangzhou", _service_region("robot-hangzhou"))
        self.assertIn("「杭州」群", _pipeline_group_title("hangzhou"))

    def test_fmt_time_converts_utc_to_beijing(self):
        from datetime import datetime, timezone
        from common.ops_web.app import _fmt_time
        # 字符串（naive 按 UTC 解读）
        self.assertEqual("2026-09-30 18:00:00",
                         _fmt_time("2026-09-30 10:00:00"))
        self.assertEqual("2026-10-06 19:13:03",
                         _fmt_time("2026-10-06 11:13:03.465847"))
        # datetime 对象（naive / 带 tz 都支持）
        self.assertEqual("2026-09-30 18:00:00",
                         _fmt_time(datetime(2026, 9, 30, 10, 0, 0)))
        self.assertEqual("2026-09-30 18:00:00",
                         _fmt_time(datetime(2026, 9, 30, 10, 0, 0,
                                            tzinfo=timezone.utc)))
        # None / 认不出的原样
        self.assertIsNone(_fmt_time(None))
        self.assertEqual("not-a-time", _fmt_time("not-a-time"))


class PipelineToggleTests(unittest.TestCase):
    def test_toggle_publishes_flipped_enabled_and_audits(self):
        store = _PipelineStore()
        publisher = _RecordingPublisher()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(), publisher=publisher))
        _login(client)
        with store.patch_audit():
            response = client.post(
                "/api/pipelines/toggle",
                json={"service_id": "robot-hangzhou", "enabled": False,
                      "reason": "合并进页面面板", "review_after": "2026-11-01"},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(publisher.published))
        self.assertEqual("robot-hangzhou", publisher.published[0].service_id)
        self.assertEqual(False, publisher.published[0].enabled)
        # 治理字段随停用落库（2026-10-17 P0）
        self.assertEqual("合并进页面面板", publisher.published[0].disabled_reason)
        self.assertEqual("2026-11-01", publisher.published[0].review_after)
        self.assertEqual(
            [(ADMIN_USERID, "disable", "robot-hangzhou",
              "合并进页面面板（复查 2026-11-01）")],
            store.audit,
        )

    def test_toggle_enable_clears_governance_fields(self):
        store = _PipelineStore()
        publisher = _RecordingPublisher()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(), publisher=publisher))
        _login(client)
        with store.patch_audit():
            response = client.post(
                "/api/pipelines/toggle",
                json={"service_id": "robot-hangzhou", "enabled": True},
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual("", publisher.published[0].disabled_reason)
        self.assertEqual("", publisher.published[0].review_after)

    def test_toggle_disable_requires_reason(self):
        """治理门禁：停用不带原因 400（2026-10-17 P0）。同态停用（enabled
        不变）视为治理元数据补录，同样需要原因。"""
        client = TestClient(_pipeline_app(viewer=_admin_viewer(),
                                          publisher=_RecordingPublisher()))
        _login(client)
        for payload in (
            {"service_id": "robot-hangzhou", "enabled": False},
            {"service_id": "robot-hangzhou", "enabled": False, "reason": "  "},
            {"service_id": "robot-hangzhou", "enabled": False,
             "reason": "x", "review_after": "11-01"},   # 非 ISO 日期
        ):
            self.assertEqual(
                400,
                client.post("/api/pipelines/toggle", json=payload).status_code,
                payload,
            )

    def test_toggle_requires_admin(self):
        store = _PipelineStore()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(),
                                          publisher=_RecordingPublisher()))
        self.assertEqual(
            401, client.post("/api/pipelines/toggle",
                             json={"service_id": "robot-hangzhou",
                                   "enabled": True}).status_code
        )

    def test_toggle_503_when_no_publisher(self):
        store = _PipelineStore()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(), publisher=None))
        _login(client)
        self.assertEqual(
            503, client.post("/api/pipelines/toggle",
                             json={"service_id": "robot-hangzhou",
                                   "enabled": True}).status_code
        )

    def test_toggle_rejects_bad_payload_and_unknown_service(self):
        store = _PipelineStore()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(),
                                          publisher=_RecordingPublisher()))
        _login(client)
        for payload in (
            {"service_id": "BAD ID", "enabled": True},   # 非法 id
            {"service_id": "robot-hangzhou", "enabled": "yes"},  # enabled 非 bool
            {"service_id": "nope", "enabled": True},      # 不在舰队
        ):
            self.assertEqual(
                400,
                client.post("/api/pipelines/toggle", json=payload).status_code,
                payload,
            )


class PipelineArchiveTests(unittest.TestCase):
    """归档端点（2026-10-19 治理 P2 裁决）：Nacos 摘除+审计留痕，
    git 历史即档案；只允许归档已停用服务。"""

    _DISABLED = {"sync-runner": {"schedule": "0 2 * * *", "enabled": False,
                                 "disabled_reason": "legacy 被双跳取代"}}

    def _app(self, deleter=None, fleet=("sync-runner",)):
        return _pipeline_app(viewer=_admin_viewer(), fleet=fleet,
                             configs=dict(self._DISABLED),
                             deleter=deleter if deleter is not None
                             else _RecordingDeleter())

    def test_archive_disabled_service_deletes_and_audits(self):
        store = _PipelineStore()
        deleter = _RecordingDeleter()
        client = TestClient(self._app(deleter=deleter))
        _login(client)
        with store.patch_audit():
            response = client.post("/api/pipelines/archive",
                                   json={"service_id": "sync-runner"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(["sync-runner"], deleter.deleted)
        self.assertEqual(1, len(store.audit))
        actor, action, sid, detail = store.audit[0]
        self.assertEqual((ADMIN_USERID, "archive", "sync-runner"),
                         (actor, action, sid))
        self.assertIn("legacy", detail)

    def test_archive_enabled_service_conflict(self):
        """活线不可归档：409，先停用（带原因）再归档。"""
        deleter = _RecordingDeleter()
        client = TestClient(_pipeline_app(
            viewer=_admin_viewer(), deleter=deleter))  # 缺省 enabled=True
        _login(client)
        response = client.post("/api/pipelines/archive",
                               json={"service_id": "robot-hangzhou"})
        self.assertEqual(409, response.status_code)
        self.assertEqual([], deleter.deleted)

    def test_archive_rejects_unknown_and_bad_id(self):
        client = TestClient(self._app())
        _login(client)
        for payload in (
            {"service_id": "nope"},        # 不在舰队
            {"service_id": "BAD ID"},      # 非法 id
            {},                            # 缺字段
        ):
            self.assertEqual(
                400, client.post("/api/pipelines/archive",
                                 json=payload).status_code, payload)

    def test_archive_503_when_no_deleter(self):
        client = TestClient(_pipeline_app(
            viewer=_admin_viewer(), fleet=("sync-runner",),
            configs=dict(self._DISABLED), deleter=None))
        _login(client)
        # deleter=None 即「未接线」：端点降级 503
        self.assertEqual(503, client.post(
            "/api/pipelines/archive",
            json={"service_id": "sync-runner"}).status_code)

    def test_archive_requires_admin(self):
        client = TestClient(self._app())
        self.assertEqual(401, client.post(
            "/api/pipelines/archive",
            json={"service_id": "sync-runner"}).status_code)


class PipelineSlaTests(unittest.TestCase):
    """SLA 迟到读面（2026-10-19 治理 P2 尾巴，用户裁决「不要播报」）。"""

    def test_sla_endpoint_returns_violations(self):
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        with mock.patch.object(
                fleet_audit, "collect_sla_status",
                lambda *a, **k: (
                    [{"service_id": "extract-mart", "deadline": "17:45"}],
                    "18:00")):
            data = client.get("/api/pipelines/sla").json()
        self.assertEqual("ok", data["status"])
        self.assertEqual("18:00", data["checked_at"])
        self.assertEqual(
            [{"service_id": "extract-mart", "deadline": "17:45"}],
            data["violations"],
        )

    def test_sla_endpoint_requires_admin_and_degrades(self):
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        self.assertEqual(401, client.get("/api/pipelines/sla").status_code)
        _login(client)
        with mock.patch.object(
                fleet_audit, "collect_sla_status",
                lambda *a, **k: (_ for _ in ()).throw(RuntimeError())):
            self.assertEqual(
                503, client.get("/api/pipelines/sla").status_code)


class PipelineAddTests(unittest.TestCase):
    def test_add_validates_publishes_and_audits(self):
        store = _PipelineStore()
        publisher = _RecordingPublisher()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(), publisher=publisher))
        _login(client)
        with store.patch_audit():
            response = client.post(
                "/api/pipelines/add",
                json={"service_id": "robot-x", "kind": "apps",
                      "schedule": "0 2 * * *", "enabled": True,
                      "depends_on": [], "description": "demo"},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual("robot-x", publisher.published[0].service_id)
        self.assertEqual(
            [(ADMIN_USERID, "add", "robot-x", "0 2 * * *")], store.audit
        )

    def test_add_publishes_category_and_rejects_unknown(self):
        store = _PipelineStore()
        publisher = _RecordingPublisher()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(), publisher=publisher))
        _login(client)
        with store.patch_audit():
            ok = client.post(
                "/api/pipelines/add",
                json={"service_id": "robot-x", "kind": "business",
                      "category": "催办", "schedule": "0 2 * * *"},
            )
            bad = client.post(
                "/api/pipelines/add",
                json={"service_id": "robot-y", "kind": "business",
                      "category": "别的", "schedule": "0 2 * * *"},
            )

        self.assertEqual(200, ok.status_code)
        self.assertEqual("催办", publisher.published[0].category)
        self.assertEqual(400, bad.status_code)
        self.assertEqual(1, len(publisher.published))  # 非法 category 不落注册表

    def test_add_rejects_invalid_fields(self):
        store = _PipelineStore()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(),
                                          publisher=_RecordingPublisher()))
        _login(client)
        for payload in (
            {"service_id": "BAD ID", "kind": "apps", "schedule": "0 2 * * *"},
            {"service_id": "robot-x", "kind": "nope", "schedule": "0 2 * * *"},
            {"service_id": "robot-x", "kind": "apps", "schedule": "not a cron"},
            {"service_id": "robot-x", "kind": "apps",
             "schedule": "0 2 * * *", "depends_on": ["robot-x"]},
        ):
            self.assertEqual(
                400, client.post("/api/pipelines/add", json=payload).status_code,
                payload,
            )

    def test_add_rejects_duplicate_service(self):
        store = _PipelineStore()
        client = TestClient(_pipeline_app(viewer=_admin_viewer(),
                                          publisher=_RecordingPublisher()))
        _login(client)
        self.assertEqual(
            400, client.post("/api/pipelines/add",
                             json={"service_id": "robot-hangzhou", "kind": "apps",
                                   "schedule": "0 2 * * *"}).status_code
        )


class PipelineScheduleUpdateTests(unittest.TestCase):
    """改播报时间（2026-10-13）：read-modify-write，只换 cron 分/时多段。

    请求体 ``{service_id, times:[{hour,minute}, ...]}``（1..3 个时点、任意
    分 0-59、去重升序）；日/月/周形态（工作日、每月 N 日）取自现有首段保留，
    且发布用原配置 replace（不得丢 sources/reads）。
    """

    def _client(self, *, configs=None, publisher=None, config_source=None,
                fleet=("robot-hangzhou",), login=True):
        app = create_app(
            settings=_fake_settings(),
            session_secret=SECRET,
            db_connector=_connector(),
            viewer_resolver=_StaticViewerResolver(_admin_viewer()),
            fleet_source=lambda: fleet,
            config_source=(config_source if config_source is not None
                           else StaticConfigSource(
                               configs or {"robot-hangzhou":
                                           {"schedule": "0 18 * * 1-5"}})),
            config_publisher=publisher,
        )
        client = TestClient(app)
        if login:
            _login(client)
        return client

    def test_update_rewrites_time_and_preserves_weekday(self):
        store = _PipelineStore()
        publisher = _RecordingPublisher()
        client = self._client(publisher=publisher)
        with store.patch_audit():
            response = client.post(
                "/api/pipelines/update",
                json={"service_id": "robot-hangzhou",
                      "times": [{"hour": 20, "minute": 30}]},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual("30 20 * * 1-5", response.json()["schedule"])
        self.assertEqual("30 20 * * 1-5", publisher.published[0].schedule)
        self.assertEqual(True, publisher.published[0].enabled)
        self.assertEqual(
            [(ADMIN_USERID, "schedule", "robot-hangzhou", "30 20 * * 1-5")],
            store.audit,
        )

    def test_update_accepts_multiple_times_sorted_and_deduped(self):
        """多时点：去重、按 (hour,minute) 升序，';' 连接多段 cron。"""
        store = _PipelineStore()
        publisher = _RecordingPublisher()
        client = self._client(
            configs={"robot-hangzhou": {"schedule": "0 2 * * *", "enabled": True}},
            publisher=publisher)
        with store.patch_audit():
            response = client.post(
                "/api/pipelines/update",
                json={"service_id": "robot-hangzhou",
                      "times": [{"hour": 18, "minute": 0},
                                {"hour": 8, "minute": 0},
                                {"hour": 12, "minute": 30}]},
            )

        self.assertEqual(200, response.status_code)
        expected = "0 8 * * *;30 12 * * *;0 18 * * *"
        self.assertEqual(expected, response.json()["schedule"])
        self.assertEqual(expected, publisher.published[0].schedule)
        self.assertEqual(
            [(ADMIN_USERID, "schedule", "robot-hangzhou", expected)],
            store.audit,
        )

    def test_update_accepts_any_minute(self):
        """分 0-59 全量合法（旧实现仅 00/30；minute=15 现应成功）。"""
        publisher = _RecordingPublisher()
        client = self._client(publisher=publisher)
        response = client.post(
            "/api/pipelines/update",
            json={"service_id": "robot-hangzhou",
                  "times": [{"hour": 1, "minute": 15}]},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("15 1 * * 1-5", response.json()["schedule"])

    def test_update_falls_back_to_daily_for_non_cron_schedule(self):
        publisher = _RecordingPublisher()
        client = self._client(
            configs={"robot-hangzhou": {"schedule": None, "enabled": True}},
            publisher=publisher,
        )
        response = client.post(
            "/api/pipelines/update",
            json={"service_id": "robot-hangzhou",
                  "times": [{"hour": 7, "minute": 0}]},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("0 7 * * *", response.json()["schedule"])
        self.assertEqual("0 7 * * *", publisher.published[0].schedule)

    def test_update_preserves_sources_and_reads(self):
        publisher = _RecordingPublisher()
        client = self._client(
            configs={"robot-hangzhou": {"schedule": "0 2 * * *", "enabled": True,
                                        "sources": ["dingtalk"],
                                        "reads": ["dim_x"]}},
            publisher=publisher,
        )
        response = client.post(
            "/api/pipelines/update",
            json={"service_id": "robot-hangzhou",
                  "times": [{"hour": 9, "minute": 30}]},
        )

        self.assertEqual(200, response.status_code)
        published = publisher.published[0]
        self.assertEqual(("dingtalk",), published.sources)
        self.assertEqual(("dim_x",), published.reads)
        self.assertEqual("30 9 * * *", published.schedule)

    def test_update_rejects_bad_payload_and_unknown_service(self):
        client = self._client(publisher=_RecordingPublisher())
        three = [{"hour": 1, "minute": 0}, {"hour": 2, "minute": 0},
                 {"hour": 3, "minute": 0}]
        for payload in (
            {"service_id": "BAD ID", "times": three},             # 非法 id
            {"service_id": "nope", "times": three},               # 不在舰队
            {"service_id": "robot-hangzhou"},                     # 缺 times
            {"service_id": "robot-hangzhou", "times": None},      # 非 list
            {"service_id": "robot-hangzhou", "times": []},        # 空
            {"service_id": "robot-hangzhou",                      # 超 3 时点
             "times": [{"hour": h, "minute": 0} for h in (1, 2, 3, 4)]},
            {"service_id": "robot-hangzhou", "times": [1]},       # 项非 dict
            {"service_id": "robot-hangzhou",                      # 时越界
             "times": [{"hour": 24, "minute": 0}]},
            {"service_id": "robot-hangzhou",
             "times": [{"hour": -1, "minute": 0}]},
            {"service_id": "robot-hangzhou",                      # 分越界
             "times": [{"hour": 1, "minute": 60}]},
            {"service_id": "robot-hangzhou",                      # 非 int
             "times": [{"hour": "1", "minute": 0}]},
            {"service_id": "robot-hangzhou",                      # bool
             "times": [{"hour": True, "minute": 0}]},
            {"service_id": "robot-hangzhou",                      # bool 分
             "times": [{"hour": 1, "minute": True}]},
        ):
            self.assertEqual(
                400, client.post("/api/pipelines/update", json=payload).status_code,
                payload,
            )

    def test_update_requires_admin(self):
        client = self._client(publisher=_RecordingPublisher(), login=False)
        self.assertEqual(
            401, client.post("/api/pipelines/update",
                             json={"service_id": "robot-hangzhou",
                                   "times": [{"hour": 1, "minute": 0}]}
                             ).status_code
        )

    def test_update_503_when_no_publisher(self):
        client = self._client(publisher=None)
        self.assertEqual(
            503, client.post("/api/pipelines/update",
                             json={"service_id": "robot-hangzhou",
                                   "times": [{"hour": 1, "minute": 0}]}
                             ).status_code
        )

    def test_update_503_when_read_or_publish_fails(self):
        # 读注册表失败
        read_fail = self._client(config_source=_BrokenConfigSource(),
                                 publisher=_RecordingPublisher())
        self.assertEqual(
            503, read_fail.post("/api/pipelines/update",
                                json={"service_id": "robot-hangzhou",
                                      "times": [{"hour": 1, "minute": 0}]}
                                ).status_code
        )
        # 发布失败
        publish_fail = self._client(
            publisher=_RecordingPublisher(error=RuntimeError("publish down")))
        self.assertEqual(
            503, publish_fail.post("/api/pipelines/update",
                                   json={"service_id": "robot-hangzhou",
                                         "times": [{"hour": 1, "minute": 0}]}
                                   ).status_code
        )

    def test_params_rewrites_and_audits_preserving_runtime_state(self):
        store = _PipelineStore()
        publisher = _RecordingPublisher()
        client = self._client(publisher=publisher)
        params = {"alert_threshold": "critical", "alert_cooldown_hours": 6}
        with store.patch_audit():
            response = client.post(
                "/api/pipelines/params",
                json={"service_id": "robot-hangzhou", "params": params},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual(params, response.json()["params"])
        published = publisher.published[0]
        self.assertEqual(params, published.params)
        # 只覆盖 params，其余运行态（schedule/enabled）原样保留。
        self.assertEqual("0 18 * * 1-5", published.schedule)
        self.assertEqual(True, published.enabled)
        self.assertEqual("params", store.audit[0][1])
        self.assertEqual("robot-hangzhou", store.audit[0][2])

    def test_params_empty_dict_clears_existing(self):
        publisher = _RecordingPublisher()
        client = self._client(
            configs={"robot-hangzhou": {"schedule": "0 18 * * 1-5",
                                        "enabled": True,
                                        "params": {"alert_threshold": "warning"}}},
            publisher=publisher,
        )
        response = client.post(
            "/api/pipelines/params",
            json={"service_id": "robot-hangzhou", "params": {}},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual({}, publisher.published[0].params)

    def test_params_rejects_bad_payload_and_unknown_service(self):
        client = self._client(publisher=_RecordingPublisher())
        for payload in (
            {"service_id": "BAD ID", "params": {}},                  # 非法 id
            {"service_id": "nope", "params": {}},                    # 不在舰队
            {"service_id": "robot-hangzhou"},                        # 缺 params
            {"service_id": "robot-hangzhou", "params": None},        # 非 dict
            {"service_id": "robot-hangzhou", "params": {"": "x"}},   # 空键
            {"service_id": "robot-hangzhou", "params": {"k": [1]}},  # 非标量
            {"service_id": "robot-hangzhou", "params": {"k": "x" * 201}},
            {"service_id": "robot-hangzhou",                         # 超 16 项
             "params": {"k%d" % i: 1 for i in range(20)}},
        ):
            self.assertEqual(
                400, client.post("/api/pipelines/params", json=payload).status_code,
                payload,
            )

    def test_params_requires_admin(self):
        client = self._client(publisher=_RecordingPublisher(), login=False)
        self.assertEqual(
            401, client.post("/api/pipelines/params",
                             json={"service_id": "robot-hangzhou",
                                   "params": {}}).status_code
        )

    # -- /api/pipelines/deps（画布拉线改依赖，2026-10-16）-------------------

    def test_deps_rejects_self_and_unknown_and_bad_payload(self):
        client = self._client(
            fleet=("robot-hangzhou", "pages-hangzhou"),
            publisher=_RecordingPublisher(),
        )
        for payload in (
            {"service_id": "robot-hangzhou", "depends_on": "pages-hangzhou"},  # 非 list
            {"service_id": "BAD ID", "depends_on": []},                        # 非法 id
            {"service_id": "nope", "depends_on": []},                          # 不在舰队
            {"service_id": "robot-hangzhou",
             "depends_on": ["robot-hangzhou"]},                                # 自依赖
            {"service_id": "robot-hangzhou",
             "depends_on": ["robot-shaoxing"]},                                # 依赖不在舰队
            {"service_id": "robot-hangzhou", "depends_on": [123]},             # 非字符串
        ):
            self.assertEqual(
                400, client.post("/api/pipelines/deps", json=payload).status_code,
                payload,
            )

    def test_deps_rewrites_and_audits_happy_path(self):
        store = _PipelineStore()
        publisher = _RecordingPublisher()
        client = self._client(
            configs={"robot-hangzhou": {"schedule": "0 18 * * 1-5",
                                        "enabled": True},
                     "pages-hangzhou": {"schedule": "30 8 * * *",
                                        "enabled": True}},
            fleet=("robot-hangzhou", "pages-hangzhou"),
            publisher=publisher,
        )
        with store.patch_audit():
            response = client.post(
                "/api/pipelines/deps",
                json={"service_id": "pages-hangzhou",
                      "depends_on": ["robot-hangzhou", "robot-hangzhou"]},
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual(["robot-hangzhou"], response.json()["depends_on"])
        published = publisher.published[0]
        self.assertEqual(["robot-hangzhou"], published.depends_on)
        # 其余运行态原样保留
        self.assertEqual("30 8 * * *", published.schedule)
        self.assertEqual(True, published.enabled)
        self.assertEqual("deps", store.audit[0][1])
        self.assertEqual("pages-hangzhou", store.audit[0][2])

    def test_deps_requires_login_and_publisher(self):
        no_login = self._client(publisher=_RecordingPublisher(), login=False)
        self.assertEqual(
            401, no_login.post("/api/pipelines/deps",
                               json={"service_id": "robot-hangzhou",
                                     "depends_on": []}).status_code)
        no_pub = self._client(publisher=None)
        self.assertEqual(
            503, no_pub.post("/api/pipelines/deps",
                             json={"service_id": "robot-hangzhou",
                                   "depends_on": []}).status_code)

    def test_params_503_when_no_publisher(self):
        client = self._client(publisher=None)
        self.assertEqual(
            503, client.post("/api/pipelines/params",
                             json={"service_id": "robot-hangzhou",
                                   "params": {}}).status_code
        )


class PipelinesHealthTests(unittest.TestCase):
    """舰队健康读面（治理 P1）：每服务最近一次真实 cron 运行。"""

    def test_health_returns_latest_per_service(self):
        client = TestClient(_pipeline_app(viewer=_admin_viewer(),
                                          publisher=_RecordingPublisher()))
        _login(client)
        with mock.patch(
            "common.ops_web.app.ops_control.fetch_run_history",
            return_value=[
                # id DESC 序：首见即最新
                {"service_id": "robot-hangzhou", "status": "finished",
                 "started_at": "2026-10-17 08:30:00"},
                {"service_id": "robot-hangzhou", "status": "failed",
                 "started_at": "2026-10-16 08:30:00"},
                {"service_id": "extract-mart", "status": "finished",
                 "started_at": "2026-10-17 07:00:00"},
            ],
        ):
            resp = client.get("/api/pipelines/health")
        self.assertEqual(200, resp.status_code)
        latest = resp.json()["latest"]
        self.assertEqual("finished", latest["robot-hangzhou"]["status"])
        # started_at 存 UTC，读面 _fmt_time 转北京时间（+8h）
        self.assertEqual("2026-10-17 16:30:00",
                         latest["robot-hangzhou"]["started_at"])
        self.assertIn("extract-mart", latest)

    def test_health_requires_login(self):
        client = TestClient(_pipeline_app(viewer=_admin_viewer(),
                                          publisher=_RecordingPublisher()))
        self.assertEqual(401, client.get("/api/pipelines/health").status_code)


class PipelineRunOnceTests(unittest.TestCase):
    def test_run_once_inserts_request_with_actor(self):
        store = _PipelineStore()
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        with store.patch_pending(), store.patch_insert():
            response = client.post(
                "/api/pipelines/run-once", json={"service_id": "robot-hangzhou"}
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual(42, response.json()["request_id"])
        self.assertEqual([("robot-hangzhou", ADMIN_USERID)], store.inserted)

    def test_run_once_rejects_without_command_template(self):
        store = _PipelineStore()
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        # bi-web 无调度命令模板（C 方案：允许注册但不允许空跑）
        self.assertEqual(
            400, client.post("/api/pipelines/run-once",
                             json={"service_id": "bi-web"}).status_code
        )

    def test_run_once_rejects_bad_id_and_duplicate_pending(self):
        store = _PipelineStore()
        store.pending = True
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        self.assertEqual(
            400, client.post("/api/pipelines/run-once",
                             json={"service_id": "BAD ID"}).status_code
        )
        with store.patch_pending():  # has_pending_request -> True
            self.assertEqual(
                400, client.post("/api/pipelines/run-once",
                                 json={"service_id": "robot-hangzhou"}).status_code
            )


class RecipientsOverviewTests(unittest.TestCase):
    """收件人总览页（P4，2026-10-12）：只读、按区域/scope 一屏展示
    「在职 ∩ 名册 ∪/∖ 覆盖」的层层结果，按区块 fail-soft（不整页 503）。"""

    _FLEET = ("robot-hangzhou", "robot-check-hangzhou",
              "channel-missing-check", "sync-wdt")

    def _client(self, *, fleet=None, configs=None):
        fleet = self._FLEET if fleet is None else fleet
        configs = configs if configs is not None else {
            service_id: {} for service_id in fleet}
        client = TestClient(_fleet_app(
            viewer=_admin_viewer(), fleet=tuple(fleet), configs=configs))
        _login(client)
        return client

    def test_overview_api_groups_by_scope_base_and_effective_with_badge(self):
        def _roster_names(conn, scope=None):
            return frozenset({"张三", "王五"}) if scope == "hangzhou" else None

        def _overrides(conn, sid):
            if sid != "robot-hangzhou":
                return []
            return [{
                "id": 1, "service_id": "robot-hangzhou", "mode": "include",
                "target_type": "user_id", "target_value": "u9",
                "resolved_user_id": "u9", "enabled": 1, "note": None,
                "updated_at": None,
            }]

        owners = {"天猫-A店": {"张三"}, "京东-B店": {"李四"}}
        with mock.patch.object(bi_authz, "fetch_active_members",
                               lambda conn: list(_MEMBERS)), \
                mock.patch.object(report_roster, "fetch_scope_person_names",
                                  _roster_names), \
                mock.patch.object(report_roster, "fetch_store_owner_map",
                                  lambda conn, scope, roles=None: owners), \
                mock.patch.object(recipient_override, "fetch_overrides",
                                  _overrides):
            response = self._client().get("/api/recipients")

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual("ok", data["status"])
        self.assertFalse(data["context_error"])
        scopes = {s["scope"]: s for s in data["sections"]}
        html = "".join(s["html"] for s in data["sections"])
        self.assertEqual("杭州", scopes["hangzhou"]["label"])
        # 杭州：基名单 = 在职(张三/王五) ∩ 名册(张三/王五)，来源人话。
        self.assertIn("名册（hangzhou）∩ 在职成员", html)
        self.assertIn("张三、王五", html)
        self.assertIn("（2 条催办任务）", html)
        # 有效名单叠加覆盖（+1 覆盖），任务深链回定时任务页抽屉。
        self.assertIn("+1 覆盖", html)
        self.assertIn("u9", html)
        self.assertIn('href="/pipelines?service=robot-hangzhou"', html)
        self.assertIn('href="/pipelines?service=robot-check-hangzhou"', html)
        # 渠道：基名单 = 名册 qudao·owner，带门店数徽标。
        self.assertIn("店铺负责人（名册 qudao·owner）", html)
        self.assertIn("张三、李四", html)
        self.assertIn("2 店", html)
        self.assertIn('href="/pipelines?service=channel-missing-check"', html)
        # 非催办任务不进总览（与 P3 反查同一取值域）。
        self.assertNotIn('href="/pipelines?service=sync-wdt"', html)
        # 空 scope 给一句人话，不留空。
        self.assertIn("本范围暂无绑定的催办任务。", html)

    def test_overview_api_fail_soft_when_context_load_fails(self):
        """只读上下文装载故障：各区块给提示，读面仍 200（不整页 503）。"""
        def _boom(conn):
            raise RuntimeError("members down")

        with mock.patch.object(bi_authz, "fetch_active_members", _boom):
            response = self._client().get("/api/recipients")

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertTrue(data["context_error"])
        html = "".join(s["html"] for s in data["sections"])
        self.assertIn("名单读取失败，请稍后重试。", html)

    def test_overview_api_renders_without_fleet(self):
        """无舰队清单：仍按 scope 展示基名单，只是无催办任务行。"""
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        response = client.get("/api/recipients")
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertTrue(data["sections"])
        html = "".join(s["html"] for s in data["sections"])
        self.assertIn("本范围暂无绑定的催办任务。", html)
        self.assertNotIn('href="/pipelines?service=', html)

    def test_overview_api_requires_admin(self):
        client = TestClient(_fleet_app(viewer=_admin_viewer()))
        assert_spa_shell(client.get("/recipients", follow_redirects=False))
        self.assertEqual(401, client.get("/api/recipients").status_code)


class RecipientRulesTests(unittest.TestCase):
    """口径收口（P5，2026-10-12，设计稿 §3）：名册 / 覆盖 / 授权三条边界
    只在「收件人总览」完整渲染一处。

    2026-10-14 全 SPA 迁移：权威口径区改由 /api/recipients 的 ``rules_html``
    下发；SSR 名册页的共用指针（``_RECIPIENT_RULES_LINK``）随旧页下线而不再
    渲染，故删除 `test_roster_page_shares_pointer`。
    """

    def _admin_client(self):
        client = TestClient(_app(viewer=_admin_viewer()))
        _login(client)
        return client

    def test_overview_api_renders_canonical_boundary_block(self):
        body = self._admin_client().get("/api/recipients").json()["rules_html"]
        self.assertIn("口径说明：名册 / 覆盖 / 授权各管什么", body)
        self.assertIn("（在职成员 ∩ 名册）∪ 任务级覆盖 − 移除", body)
        # 三条边界的关键裁决点都在权威区里（与设计稿 §3 逐条对应）
        self.assertIn("业务资格", body)                 # 名册 = 资格
        self.assertIn("scope（区域/渠道）", body)
        self.assertIn("service_id（单条线）", body)      # 覆盖 = 单条任务
        self.assertIn("不改正文、不改资格", body)
        self.assertIn("与「谁能被催办」<b>无关</b>", body)  # 授权与催办无关
        self.assertIn("名册定「资格」", body)
        # 三个入口深链
        self.assertIn('href="/roster"', body)
        self.assertIn('href="/pipelines"', body)
        self.assertIn('href="/grants"', body)

    def test_decommissioned_pages_do_not_carry_pointer(self):
        """「成员与授权」「授权现状」已停用（2026-10-13），不再渲染口径指针。"""
        pointer = "名册定资格、覆盖定单条例外、授权定系统访问（与催办无关）"
        members = self._admin_client().get("/member-grants").text
        grants = self._admin_client().get("/grants").text
        for page, body in (("成员与授权", members), ("授权现状", grants)):
            with self.subTest(page=page):
                self.assertNotIn(pointer, body)


class RecipientOverrideApiTests(unittest.TestCase):
    """任务级收件人覆盖写 API（方案 B，2026-10-12）。"""

    def _client(self):
        client = TestClient(_pipeline_app(viewer=_admin_viewer()))
        _login(client)
        return client

    def test_get_returns_overrides_and_rejects_bad_service(self):
        rows = [{
            "id": 1, "service_id": "robot-hangzhou", "mode": "include",
            "target_type": "user_id", "target_value": "u9",
            "resolved_user_id": "u9", "enabled": 1, "note": None,
            "updated_by": "ops", "updated_at": None,
        }]
        with mock.patch.object(recipient_override, "fetch_service_overrides",
                               lambda conn, sid: list(rows)):
            response = self._client().get(
                "/api/pipelines/recipient-override",
                params={"service_id": "robot-hangzhou"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(rows, response.json()["overrides"])
        self.assertEqual(400, self._client().get(
            "/api/pipelines/recipient-override").status_code)

    def test_get_serializes_datetime_updated_at(self):
        """线上实测：updated_at 为 datetime 时 JSONResponse 序列化失败 → 500，
        抽屉显示「读取异常，请稍后重试」。出口须归一为字符串（2026-10-12
        hotfix，方案 A P0）。"""
        rows = [{
            "id": 7, "service_id": "robot-hangzhou", "mode": "exclude",
            "target_type": "name", "target_value": "李四",
            "resolved_user_id": None, "enabled": 1, "note": None,
            "updated_by": "ops",
            "updated_at": datetime(2026, 10, 12, 8, 30, 0),
        }]
        with mock.patch.object(recipient_override, "fetch_service_overrides",
                               lambda conn, sid: list(rows)):
            response = self._client().get(
                "/api/pipelines/recipient-override",
                params={"service_id": "robot-hangzhou"})
        self.assertEqual(200, response.status_code)
        overrides = response.json()["overrides"]
        self.assertEqual(1, len(overrides))
        self.assertIsInstance(overrides[0]["updated_at"], str)
        self.assertEqual("李四", overrides[0]["target_value"])

    def test_get_includes_base_and_source_from_roster(self):
        """P1（2026-10-12）：抽屉 API 附带「本线基名单」——robot 线取该区域
        在职成员 ∩ 名册启用在册人员，来源标注人话（与列表列同源口径）。"""
        with mock.patch.object(recipient_override, "fetch_service_overrides",
                               lambda conn, sid: []), \
                mock.patch.object(bi_authz, "fetch_active_members",
                                  lambda conn: list(_MEMBERS)), \
                mock.patch.object(report_roster, "fetch_scope_person_names",
                                  lambda conn, scope=None:
                                  frozenset({"张三", "王五"})):
            response = self._client().get(
                "/api/pipelines/recipient-override",
                params={"service_id": "robot-hangzhou"})
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(
            [{"user_id": "u-zhang", "name": "张三"},
             {"user_id": "u-wang", "name": "王五"}], data["base"])
        self.assertEqual("名册（hangzhou）∩ 在职成员", data["base_source"])

    def test_get_returns_qudao_base(self):
        """渠道线基名单 = 名册 qudao·owner 去重人名；无 uid 映射时以 ?名 占位。"""
        owners = {"天猫-A店": {"张三"}, "京东-B店": {"李四", "张三"}}
        with mock.patch.object(recipient_override, "fetch_service_overrides",
                               lambda conn, sid: []), \
                mock.patch.object(report_roster, "fetch_store_owner_map",
                                  lambda conn, scope, roles=None: owners):
            response = self._client().get(
                "/api/pipelines/recipient-override",
                params={"service_id": "channel-missing-check"})
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(
            [{"user_id": "?张三", "name": "张三"},
             {"user_id": "?李四", "name": "李四"}], data["base"])
        self.assertEqual("店铺负责人（名册 qudao·owner）", data["base_source"])

    def test_get_base_fail_soft_keeps_overrides(self):
        """基名单是附属信息：装载故障只让抽屉少「来源」区块，不给整页 503。"""
        def _boom(conn, scope=None):
            raise RuntimeError("roster down")

        rows = [{"id": 1, "service_id": "robot-hangzhou", "mode": "include",
                 "target_type": "user_id", "target_value": "u9",
                 "resolved_user_id": "u9", "enabled": 1, "note": None,
                 "updated_by": "ops", "updated_at": None}]
        with mock.patch.object(recipient_override, "fetch_service_overrides",
                               lambda conn, sid: list(rows)), \
                mock.patch.object(bi_authz, "fetch_active_members",
                                  lambda conn: list(_MEMBERS)), \
                mock.patch.object(report_roster, "fetch_scope_person_names",
                                  _boom):
            response = self._client().get(
                "/api/pipelines/recipient-override",
                params={"service_id": "robot-hangzhou"})
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual([], data["base"])
        self.assertEqual("", data["base_source"])
        self.assertEqual([], data["explain"])
        self.assertEqual(rows, data["overrides"])

    def test_get_includes_explain_for_robot(self):
        """P2（2026-10-12）：抽屉附带「为什么不在名单」三组归因——名册有但
        不在职、在职但不在名册、被本任务 exclude 移除（展示口径，不新增写面）。"""
        rows = [{"id": 3, "service_id": "robot-hangzhou", "mode": "exclude",
                 "target_type": "user_id", "target_value": "u-zhang",
                 "resolved_user_id": "u-zhang", "enabled": 1, "note": None,
                 "updated_by": "ops", "updated_at": None}]
        with mock.patch.object(recipient_override, "fetch_service_overrides",
                               lambda conn, sid: list(rows)), \
                mock.patch.object(bi_authz, "fetch_active_members",
                                  lambda conn: list(_MEMBERS)), \
                mock.patch.object(report_roster, "fetch_scope_person_names",
                                  lambda conn, scope=None:
                                  frozenset({"张三", "赵六"})):
            response = self._client().get(
                "/api/pipelines/recipient-override",
                params={"service_id": "robot-hangzhou"})
        self.assertEqual(200, response.status_code)
        data = response.json()
        # 基名单 = 在职(hangzhou: 张三/王五) ∩ 名册(张三/赵六) = 张三
        self.assertEqual([{"user_id": "u-zhang", "name": "张三"}], data["base"])
        groups = {g["key"]: g for g in data["explain"]}
        self.assertEqual(["赵六"],
                         [i["name"] for i in groups["roster_only"]["items"]])
        self.assertEqual(["王五"],
                         [i["name"] for i in groups["member_only"]["items"]])
        self.assertEqual(["张三"],
                         [i["name"] for i in groups["excluded"]["items"]])
        self.assertIn("成员与授权", groups["roster_only"]["reason"])
        self.assertIn("填报名册", groups["member_only"]["reason"])

    def test_get_qudao_explain_has_no_member_groups(self):
        """渠道线基名单直接来自名册 owner、无区域在职交集：explain 不产出
        roster_only/member_only（否则会把全部 owner 误报为「不在职」），
        只随覆盖产出 excluded。"""
        owners = {"天猫-A店": {"张三"}, "京东-B店": {"李四"}}
        rows = [{"id": 5, "service_id": "channel-missing-check",
                 "mode": "exclude", "target_type": "name",
                 "target_value": "张三", "resolved_user_id": None,
                 "enabled": 1, "note": None, "updated_by": "ops",
                 "updated_at": None}]
        with mock.patch.object(recipient_override, "fetch_service_overrides",
                               lambda conn, sid: list(rows)), \
                mock.patch.object(report_roster, "fetch_store_owner_map",
                                  lambda conn, scope, roles=None: owners):
            response = self._client().get(
                "/api/pipelines/recipient-override",
                params={"service_id": "channel-missing-check"})
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(["excluded"], [g["key"] for g in data["explain"]])
        self.assertEqual(["张三"],
                         [i["name"] for i in data["explain"][0]["items"]])

    def test_add_resolves_snapshot_and_reports_unresolved(self):
        seen = {}

        def fake_upsert(conn, service_id, mode, target_type, target_value,
                        *, actor, note="", resolved_user_id=None):
            seen.update(service_id=service_id, mode=mode,
                        target_type=target_type, target_value=target_value,
                        actor=actor, note=note, resolved=resolved_user_id)

        with mock.patch.object(
                recipient_override, "resolve_targets",
                lambda conn, rows: (
                    [{"target_type": "name", "target_value": "张三",
                      "resolved_user_id": "u-zhang"}], ["李四"])), \
                mock.patch.object(recipient_override, "upsert_override",
                                  fake_upsert):
            response = self._client().post(
                "/api/pipelines/recipient-override/add",
                json={"service_id": "robot-hangzhou", "mode": "include",
                      "target_type": "name", "target_value": "张三",
                      "note": "补人"})

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual("u-zhang", data["resolved_user_id"])
        self.assertEqual(["李四"], data["unresolved"])
        self.assertEqual(ADMIN_USERID, seen["actor"])
        self.assertEqual("u-zhang", seen["resolved"])
        self.assertEqual("补人", seen["note"])

    def test_add_rejects_invalid_payload(self):
        client = self._client()
        for payload in (
            {"service_id": "robot-hangzhou", "mode": "nope",
             "target_type": "name", "target_value": "张三"},
            {"service_id": "robot-hangzhou", "mode": "include",
             "target_type": "alien", "target_value": "张三"},
            {"service_id": "robot-hangzhou", "mode": "include",
             "target_type": "name", "target_value": "  "},
            {"service_id": "bad id!", "mode": "include",
             "target_type": "name", "target_value": "张三"},
        ):
            self.assertEqual(400, client.post(
                "/api/pipelines/recipient-override/add",
                json=payload).status_code, payload)

    def test_toggle_delete_reset(self):
        client = self._client()
        with mock.patch.object(recipient_override, "set_override_enabled",
                               lambda conn, oid, enabled, *, actor: True):
            self.assertEqual(200, client.post(
                "/api/pipelines/recipient-override/toggle",
                json={"id": 3, "enabled": False}).status_code)
        with mock.patch.object(recipient_override, "set_override_enabled",
                               lambda conn, oid, enabled, *, actor: False):
            self.assertEqual(400, client.post(
                "/api/pipelines/recipient-override/toggle",
                json={"id": 3, "enabled": True}).status_code)
        with mock.patch.object(recipient_override, "delete_override",
                               lambda conn, oid, *, actor: True):
            self.assertEqual(200, client.post(
                "/api/pipelines/recipient-override/delete",
                json={"id": 4}).status_code)
        with mock.patch.object(recipient_override, "reset_service_overrides",
                               lambda conn, sid, *, actor: 5):
            response = client.post(
                "/api/pipelines/recipient-override/reset",
                json={"service_id": "robot-hangzhou"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(5, response.json()["deleted"])
        # 非法载荷 → 400（bool 不算 int；service_id 非法）
        self.assertEqual(400, client.post(
            "/api/pipelines/recipient-override/toggle",
            json={"id": True, "enabled": True}).status_code)
        self.assertEqual(400, client.post(
            "/api/pipelines/recipient-override/reset",
            json={"service_id": "bad id!"}).status_code)

    def test_add_db_error_returns_503(self):
        def boom(*args, **kwargs):
            raise RuntimeError("db down")

        with mock.patch.object(recipient_override, "resolve_targets", boom):
            response = self._client().post(
                "/api/pipelines/recipient-override/add",
                json={"service_id": "robot-hangzhou", "mode": "include",
                      "target_type": "user_id", "target_value": "u9"})
        self.assertEqual(503, response.status_code)
