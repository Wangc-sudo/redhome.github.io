"""Offline API tests for the ops-web data-quality control surface."""

import copy
import unittest
import warnings
from contextlib import contextmanager
from unittest import mock

warnings.filterwarnings(
    "ignore", message="Using `httpx` with `starlette.testclient` is deprecated"
)

from fastapi.testclient import TestClient

from common.bi_web import auth, authz
from common.ops_web.app import create_app
from common.public_data.cleaning import rule_version
from common.public_data.data_quality import (
    DataQualityError,
    DataQualityNotFound,
    SourceBatch,
)
from tests.common.test_bi_web_app import _fake_settings
from tests.common.test_ops_web_app import _StaticViewerResolver, _connector


SECRET = "ops-test-secret"
DATASET = "channel_daily_sales"
RUN_ID = "10000000-0000-0000-0000-000000000001"


def _viewer(userid="u-viewer", *, scopes=(), admin=False, admitted=True):
    return authz.Viewer(
        userid=userid,
        name=userid,
        scopes=frozenset(scopes),
        allowed_regions=frozenset(),
        is_admin=admin,
        admitted=admitted,
    )


def _writer():
    return _viewer("u-datafix", scopes={"datafix"})


def _rule(**changes):
    rule = {
        "id": 1,
        "dataset": DATASET,
        "rule_code": "CHANNEL_REQUIRED",
        "rule_type": "required",
        "field_name": "channel",
        "params": {},
        "severity": "error",
        "enabled": 1,
        "version": 1,
    }
    rule.update(changes)
    return rule


class _Repository:
    def __init__(self, *, rules=None, runs=None, issues=None, overview=None):
        self.rules = copy.deepcopy(rules if rules is not None else [_rule()])
        self.runs = copy.deepcopy(runs or [])
        self.issues = copy.deepcopy(issues or [])
        self.overview_items = copy.deepcopy(overview or [])
        self.created_runs = []
        self.snapshots = {}
        self.snapshot_reads = []
        self.inserted_issues = []
        self.finished_runs = []
        self.created_rules = []
        self.updated_rules = []
        self.resolutions = []
        self.overrides = []
        self.issue_queries = []
        self.run_queries = []
        self.sql_count = 0
        self.db_ms = 0

    @property
    def control_writes(self):
        return self.created_runs + self.inserted_issues + self.finished_runs

    def new_id(self):
        return RUN_ID

    def list_rules(self, dataset=None, *, enabled_only=False):
        return copy.deepcopy([
            rule for rule in self.rules
            if (dataset is None or rule["dataset"] == dataset)
            and (not enabled_only or rule.get("enabled", 1))
        ])

    def list_overrides(self, dataset):
        return {}

    def create_run(self, **kwargs):
        self.created_runs.append(kwargs)
        rules = self.list_rules(kwargs["dataset"], enabled_only=True)
        self.snapshots[RUN_ID] = {
            "run_id": RUN_ID,
            "dataset": kwargs["dataset"],
            "rule_version": rule_version(rules),
            "rules": rules,
            "overrides": {},
        }
        return RUN_ID

    def get_run_snapshot(self, run_id):
        self.snapshot_reads.append(run_id)
        if run_id not in self.snapshots:
            raise DataQualityNotFound("rerun snapshot not found")
        return copy.deepcopy(self.snapshots[run_id])

    def insert_issues(self, run_id, issues):
        self.inserted_issues.extend(issues)

    def finish_run(self, run_id, **kwargs):
        self.finished_runs.append(kwargs)

    def overview(self):
        return {"items": copy.deepcopy(self.overview_items), "datasets": [DATASET]}

    def create_rule(self, rule, *, actor):
        self.created_rules.append((copy.deepcopy(rule), actor))
        stored = {**rule, "id": 99, "version": 1}
        self.rules.append(stored)
        return {"rule": copy.deepcopy(stored), "audit_id": "audit-rule-create"}

    def update_rule(self, rule_id, changes, *, actor):
        self.updated_rules.append((rule_id, copy.deepcopy(changes), actor))
        current = next((rule for rule in self.rules if rule["id"] == rule_id), None)
        if current is None:
            raise DataQualityNotFound("rule not found")
        current.update(changes)
        current["version"] = int(current.get("version") or 1) + 1
        return {"rule": copy.deepcopy(current), "audit_id": "audit-rule-update"}

    def list_issues(self, dataset, *, status=None, rule_code=None, cursor_id=None, limit=50):
        if status is not None and status not in {"open", "confirmed", "ignored", "resolved"}:
            raise DataQualityError("invalid issue status")
        self.issue_queries.append({
            "dataset": dataset, "status": status, "rule_code": rule_code,
            "cursor_id": cursor_id, "limit": limit,
        })
        return {
            "items": copy.deepcopy(self.issues), "dataset": dataset,
            "next_cursor": None,
        }

    def resolve_issue(self, issue_id, *, status, reason, actor):
        self.resolutions.append((issue_id, status, reason, actor))
        return {
            "issue_id": issue_id, "status": status, "audit_id": "audit-resolution",
            "dataset": DATASET,
        }

    def put_override(self, **kwargs):
        self.overrides.append(kwargs)
        return {
            "override_id": "override-1", "audit_id": "audit-override",
            "dataset": kwargs["dataset"], "source_record_id": kwargs["source_record_id"],
            "field_name": kwargs["field_name"], "active": 1,
        }

    def list_runs(self, *, dataset=None, mode=None, cursor_id=None, limit=50):
        self.run_queries.append({
            "dataset": dataset, "mode": mode, "cursor_id": cursor_id, "limit": limit,
        })
        rows = [
            row for row in self.runs
            if (dataset is None or row["dataset"] == dataset)
            and (mode is None or row["mode"] == mode)
        ]
        return {"items": copy.deepcopy(rows[:limit]), "next_cursor": None}

    def get_run(self, run_id):
        row = next((row for row in self.runs if row["run_id"] == run_id), None)
        if row is None:
            raise DataQualityNotFound("run not found")
        return copy.deepcopy(row)


class _Source:
    def __init__(self, batches=()):
        self.batches = list(batches)
        self.sql_count = 1
        self.db_ms = 2
        self.channel_business_dates = set()

    def iter_batches(self, dataset, **kwargs):
        yield from self.batches


def _app(repository, viewer, source=None, *, auth_disabled=False):
    source = source or _Source()

    @contextmanager
    def source_connector():
        yield source

    return create_app(
        settings=_fake_settings(),
        session_secret=SECRET,
        db_connector=_connector(),
        viewer_resolver=_StaticViewerResolver(viewer),
        data_quality_source_connector=source_connector,
        auth_disabled=auth_disabled,
    )


def _login(client, viewer):
    client.cookies.set(
        auth.SESSION_COOKIE, auth.issue_session(viewer.userid, SECRET)
    )


def _patch_repository(repository):
    return mock.patch(
        "common.ops_web.data_quality.DataQualityRepository", return_value=repository
    )


class DataQualityPermissionTests(unittest.TestCase):
    def test_unauthenticated_read_is_401_and_admitted_viewer_can_read(self):
        repository = _Repository()
        viewer = _viewer()
        with _patch_repository(repository):
            client = TestClient(_app(repository, viewer))
            self.assertEqual(401, client.get("/api/data-quality/overview").status_code)
            _login(client, viewer)
            self.assertEqual(200, client.get("/api/data-quality/overview").status_code)
            rules = client.get("/api/data-quality/rules", params={"dataset": DATASET})
            self.assertEqual(200, rules.status_code)
            self.assertEqual(1, rules.json()["count"])

    def test_plain_viewer_cannot_write_and_unadmitted_viewer_cannot_read(self):
        repository = _Repository()
        viewer = _viewer()
        with _patch_repository(repository):
            client = TestClient(_app(repository, viewer))
            _login(client, viewer)
            response = client.post("/api/data-quality/reruns", json={"dataset": DATASET})
            self.assertEqual(403, response.status_code)
            self.assertEqual({"detail": "forbidden"}, response.json())
            self.assertEqual([], repository.created_runs)

        denied = _viewer("u-denied", admitted=False)
        with _patch_repository(repository):
            client = TestClient(_app(repository, denied))
            _login(client, denied)
            self.assertEqual(403, client.get("/api/data-quality/overview").status_code)

    def test_auth_disabled_still_requires_session_for_quality_writes(self):
        repository = _Repository()
        with _patch_repository(repository):
            client = TestClient(_app(repository, _viewer(), auth_disabled=True))
            self.assertEqual(200, client.get("/api/data-quality/overview").status_code)
            response = client.post("/api/data-quality/reruns", json={"dataset": DATASET})
            self.assertEqual(401, response.status_code)
            self.assertEqual([], repository.created_runs)

    def test_datafix_capability_and_admin_can_write(self):
        for viewer in (_writer(), _viewer("u-admin", admin=True)):
            with self.subTest(viewer=viewer.userid):
                repository = _Repository()
                with _patch_repository(repository):
                    client = TestClient(_app(repository, viewer))
                    _login(client, viewer)
                    response = client.post(
                        "/api/data-quality/reruns",
                        json={
                            "dataset": DATASET,
                            "window_start": "2026-10-26",
                            "window_end": "2026-10-26",
                        },
                    )
                    self.assertEqual(200, response.status_code)
                    self.assertEqual("pending", response.json()["status"])
                    self.assertEqual(viewer.userid, repository.created_runs[0]["requested_by"])


class DataQualityPreviewTests(unittest.TestCase):
    def test_real_runner_previews_proposed_rule_without_control_writes(self):
        repository = _Repository(rules=[])
        source = _Source([
            SourceBatch([{"source_record_id": "raw-1"}], ["raw-1"], 10),
        ])
        viewer = _viewer()
        proposed = _rule(id=None, version=1)
        with _patch_repository(repository):
            client = TestClient(_app(repository, viewer, source))
            _login(client, viewer)
            response = client.post(
                "/api/data-quality/preview",
                json={"dataset": DATASET, "rule": proposed},
            )

        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual("preview", body["mode"])
        self.assertTrue(body["dry_run"])
        self.assertNotIn("run_id", body)
        self.assertEqual(1, body["records_read"])
        self.assertEqual(1, body["records_quarantined"])
        self.assertEqual(1, body["issue_count"])
        self.assertEqual("required", body["samples"][0]["reason"])
        self.assertEqual([], repository.control_writes)


class DataQualityValidationTests(unittest.TestCase):
    def setUp(self):
        self.repository = _Repository()
        self.viewer = _writer()
        self.patch = _patch_repository(self.repository)
        self.patch.start()
        self.client = TestClient(_app(self.repository, self.viewer))
        _login(self.client, self.viewer)

    def tearDown(self):
        self.patch.stop()

    def test_unsupported_dataset_is_400(self):
        response = self.client.post(
            "/api/data-quality/preview", json={"dataset": "not_supported"}
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual({"detail": "bad_request"}, response.json())

    def test_missing_incomplete_or_reversed_window_is_400(self):
        payloads = [
            {"dataset": DATASET},
            {"dataset": DATASET, "window_start": "2026-10-27"},
            {"dataset": DATASET, "window_start": "2026-10-27", "window_end": "2026-10-26"},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                response = self.client.post("/api/data-quality/reruns", json=payload)
                self.assertEqual(400, response.status_code)
        self.assertEqual([], self.repository.created_runs)

    def test_rule_outside_whitelist_is_400(self):
        response = self.client.post(
            "/api/data-quality/preview",
            json={"dataset": DATASET, "rule": _rule(rule_type="executable")},
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual([], self.repository.control_writes)

    def test_invalid_cursor_and_limit_are_400(self):
        for params in ({"cursor": "abc"}, {"cursor": "0"}, {"limit": "0"}, {"limit": "201"}):
            with self.subTest(params=params):
                response = self.client.get(
                    f"/api/data-quality/datasets/{DATASET}/issues", params=params
                )
                self.assertEqual(400, response.status_code)
        self.assertEqual([], self.repository.issue_queries)

    def test_stale_rule_version_is_409_and_does_not_enqueue(self):
        response = self.client.post(
            "/api/data-quality/reruns",
            json={
                "dataset": DATASET, "rule_version": "0" * 64,
                "window_start": "2026-10-26", "window_end": "2026-10-26",
            },
        )
        self.assertEqual(409, response.status_code)
        self.assertEqual({"detail": "conflict"}, response.json())
        self.assertEqual([], self.repository.created_runs)
        self.assertEqual([], self.repository.snapshot_reads)

    def test_null_rule_change_is_400_and_does_not_write(self):
        for payload in ({"rule_type": None}, {"severity": None}, {"enabled": None}):
            with self.subTest(payload=payload):
                response = self.client.put("/api/data-quality/rules/1", json=payload)
                self.assertEqual(400, response.status_code)
        self.assertEqual([], self.repository.updated_rules)


class DataQualityRerunTests(unittest.TestCase):
    def test_both_dry_run_modes_only_create_pending_rerun_requests(self):
        viewer = _writer()
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                repository = _Repository()
                with _patch_repository(repository), mock.patch(
                    "common.ops_web.data_quality.DataQualityRunner"
                ) as runner:
                    client = TestClient(_app(repository, viewer))
                    _login(client, viewer)
                    response = client.post(
                        "/api/data-quality/reruns",
                        json={
                            "dataset": DATASET, "dry_run": dry_run,
                            "window_start": "2026-10-26", "window_end": "2026-10-26",
                        },
                    )
                self.assertEqual(200, response.status_code, response.text)
                body = response.json()
                self.assertEqual(RUN_ID, body["run_id"])
                self.assertEqual("pending", body["status"])
                self.assertEqual("rerun", body["mode"])
                self.assertEqual(dry_run, body["dry_run"])
                self.assertEqual(
                    rule_version(repository.list_rules(DATASET, enabled_only=True)),
                    body["rule_version"],
                )
                self.assertEqual(0, body["counts"]["records_written"])
                self.assertNotIn("audit_id", body)
                self.assertEqual(1, len(repository.created_runs))
                created = repository.created_runs[0]
                self.assertEqual("pending", created["status"])
                self.assertEqual("rerun", created["mode"])
                self.assertEqual(dry_run, created["dry_run"])
                self.assertEqual([RUN_ID], repository.snapshot_reads)
                runner.assert_not_called()

    def test_rerun_response_uses_stored_snapshot_version(self):
        repository = _Repository()
        original_create = repository.create_run

        def create_with_concurrent_rule_change(**kwargs):
            repository.rules[0]["severity"] = "warning"
            return original_create(**kwargs)

        repository.create_run = create_with_concurrent_rule_change
        viewer = _writer()
        with _patch_repository(repository):
            client = TestClient(_app(repository, viewer))
            _login(client, viewer)
            response = client.post("/api/data-quality/reruns", json={
                "dataset": DATASET,
                "window_start": "2026-10-26", "window_end": "2026-10-26",
            })

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(
            rule_version(repository.list_rules(DATASET, enabled_only=True)),
            response.json()["rule_version"],
        )
        self.assertNotEqual(
            repository.created_runs[0]["rule_version"], response.json()["rule_version"],
        )

    def test_rerun_list_and_detail_expose_counts_diff_and_boolean_dry_run(self):
        row = {
            "run_id": RUN_ID, "dataset": DATASET, "mode": "rerun",
            "status": "finished", "dry_run": 1, "diff_json": '{"changed": 2}',
            "records_read": 3, "records_written": 0,
        }
        repository = _Repository(runs=[row])
        viewer = _viewer()
        with _patch_repository(repository):
            client = TestClient(_app(repository, viewer))
            _login(client, viewer)
            listed = client.get("/api/data-quality/reruns", params={"dataset": DATASET})
            detail = client.get(f"/api/data-quality/reruns/{RUN_ID}")

        self.assertEqual(200, listed.status_code)
        item = listed.json()["items"][0]
        self.assertIs(True, item["dry_run"])
        self.assertEqual({"changed": 2}, item["diff"])
        self.assertEqual(3, item["counts"]["records_read"])
        self.assertEqual(0, item["counts"]["records_written"])
        self.assertEqual(RUN_ID, detail.json()["run_id"])

    def test_non_rerun_run_is_not_returned_by_rerun_detail(self):
        repository = _Repository(runs=[{
            "run_id": RUN_ID, "dataset": DATASET, "mode": "check", "status": "finished",
        }])
        viewer = _viewer()
        with _patch_repository(repository):
            client = TestClient(_app(repository, viewer))
            _login(client, viewer)
            response = client.get(f"/api/data-quality/reruns/{RUN_ID}")
        self.assertEqual(404, response.status_code)
        self.assertEqual({"detail": "not_found"}, response.json())


class DataQualityWriteWiringTests(unittest.TestCase):
    def test_rule_issue_and_override_writes_use_authenticated_actor(self):
        repository = _Repository(rules=[_rule(id=99)])
        viewer = _writer()
        rule_payload = _rule()
        rule_payload.pop("id")
        rule_payload.pop("version")
        with _patch_repository(repository):
            client = TestClient(_app(repository, viewer))
            _login(client, viewer)
            created = client.post("/api/data-quality/rules", json=rule_payload)
            updated = client.put("/api/data-quality/rules/99", json={"enabled": False})
            resolved = client.post(
                "/api/data-quality/issues/7/resolution",
                json={"status": "confirmed", "reason": "verified"},
            )
            override = client.post("/api/data-quality/overrides", json={
                "dataset": DATASET, "source_record_id": "raw-1", "field_name": "channel",
                "value": "抖音", "reason": "verified mapping",
            })

        self.assertEqual(200, created.status_code, created.text)
        self.assertEqual("audit-rule-create", created.json()["audit_id"])
        self.assertEqual(200, updated.status_code, updated.text)
        self.assertEqual("audit-rule-update", updated.json()["audit_id"])
        self.assertEqual(200, resolved.status_code, resolved.text)
        self.assertEqual("confirmed", resolved.json()["status"])
        self.assertEqual(200, override.status_code, override.text)
        self.assertEqual(1, override.json()["active"])
        self.assertEqual(viewer.userid, repository.created_rules[0][1])
        self.assertEqual(viewer.userid, repository.updated_rules[0][2])
        self.assertEqual(viewer.userid, repository.resolutions[0][3])
        self.assertEqual(viewer.userid, repository.overrides[0]["actor"])


if __name__ == "__main__":
    unittest.main()
