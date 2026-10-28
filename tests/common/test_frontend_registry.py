"""前端台账结构、路由与冻结约束的离线校验。"""

import json
import re
import unittest
from pathlib import Path
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "frontend" / "apps.registry.json"
NGINX_PATH = REPO_ROOT / "docker" / "cloud" / "nginx-pages.conf"
BI_SEED_PATH = REPO_ROOT / "docker" / "integration" / "bi.seed.yaml"
REGIONS_SEED_PATH = REPO_ROOT / "docker" / "integration" / "regions.seed.json"

REQUIRED_FIELDS = {
    "id",
    "name",
    "owner",
    "users",
    "criticality",
    "status",
    "change_policy",
    "public_url",
    "legacy_urls",
    "source_path",
    "dist_path",
    "runtime_service",
    "auth_mode",
    "api_dependencies",
    "health_check",
    "replacement_url",
}
FROZEN_APPS = {
    "bi-hangzhou": "https://dt.yyhlzj.com/hangzhou.html",
    "bi-shaoxing": "https://dt.yyhlzj.com/shaoxing.html",
}


class FrontendRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        cls.apps = cls.registry["applications"]
        cls.by_id = {app["id"]: app for app in cls.apps}
        cls.nginx = NGINX_PATH.read_text(encoding="utf-8")
        cls.bi_seed = BI_SEED_PATH.read_text(encoding="utf-8")
        cls.regions_seed = json.loads(REGIONS_SEED_PATH.read_text(encoding="utf-8"))

    def test_schema_and_repository_paths(self):
        self.assertEqual(1, self.registry["schema_version"])
        self.assertTrue(self.apps)
        for app in self.apps:
            with self.subTest(app=app.get("id")):
                self.assertFalse(REQUIRED_FIELDS - app.keys())
                for field in (
                    "id",
                    "name",
                    "owner",
                    "users",
                    "runtime_service",
                    "auth_mode",
                ):
                    self.assertIsInstance(app[field], str)
                    self.assertTrue(app[field].strip(), field)
                self.assertIn(app["status"], {"active", "protected", "deprecated", "archived"})
                self.assertIn(app["criticality"], {"P0", "P1", "P2"})
                self.assertIn(app["change_policy"], {"frozen", "governed"})
                self.assertIsInstance(app["legacy_urls"], list)
                self.assertIsInstance(app["api_dependencies"], list)
                self.assertTrue(app["api_dependencies"])
                self.assertIsInstance(app["source_path"], list)
                self.assertTrue(app["source_path"])
                for relative in app["source_path"]:
                    self.assertTrue((REPO_ROOT / relative).exists(), relative)
                if app["dist_path"] is not None:
                    self.assertTrue((REPO_ROOT / app["dist_path"] / "index.html").is_file())
                self.assertIsInstance(app["health_check"], dict)
                self.assertIn("kind", app["health_check"])
                self.assertIn("expected_access", app["health_check"])
                if app["status"] == "deprecated":
                    self.assertTrue(app["replacement_url"])

    def test_primary_and_legacy_urls_are_unique_and_safe(self):
        primary_urls = [app["public_url"] for app in self.apps]
        legacy_urls = [url for app in self.apps for url in app["legacy_urls"]]
        self.assertEqual(len(primary_urls), len(set(primary_urls)))
        self.assertEqual(len(legacy_urls), len(set(legacy_urls)))
        self.assertEqual(len(self.apps), len(self.by_id))
        for url in primary_urls + legacy_urls:
            with self.subTest(url=url):
                parsed = urlsplit(url)
                self.assertIn(parsed.scheme, {"http", "https"})
                self.assertTrue(parsed.hostname)
                self.assertIsNone(parsed.username)
                self.assertIsNone(parsed.password)

    def test_frozen_bi_scope_is_explicit(self):
        actual_frozen = {app["id"] for app in self.apps if app["change_policy"] == "frozen"}
        self.assertEqual(set(FROZEN_APPS), actual_frozen)
        for app_id, url in FROZEN_APPS.items():
            with self.subTest(app=app_id):
                app = self.by_id[app_id]
                self.assertEqual(url, app["public_url"])
                self.assertEqual("protected", app["status"])
                self.assertEqual("P0", app["criticality"])
                self.assertEqual("public", app["auth_mode"])
                self.assertNotIn("access_review", app)
                self.assertIn("禁止任何直接或间接改动", app["notes"])

    def test_dashboard_seed_and_exact_nginx_routes_exist(self):
        for app in self.apps:
            health = app["health_check"]
            if health["kind"] != "dashboard":
                continue
            with self.subTest(app=app["id"]):
                dashboard_id = health["dashboard_id"]
                self.assertRegex(self.bi_seed, rf"(?m)^{re.escape(dashboard_id)}:\s*$")
                self.assertIn(dashboard_id, app["api_dependencies"][0])
                path = urlsplit(app["public_url"]).path
                self.assertTrue(f"location = {path}" in self.nginx, path)
                expected_proxy = f"proxy_pass http://127.0.0.1:18080/d/{dashboard_id};"
                self.assertTrue(expected_proxy in self.nginx, expected_proxy)

    def test_leaderboard_regions_and_dynamic_route_exist(self):
        self.assertTrue(
            'location ~ "^/[a-z0-9_]+(-\\d{4}-\\d{2}-\\d{2})?\\.html$"' in self.nginx,
            "missing dynamic leaderboard route",
        )
        self.assertTrue(
            "proxy_pass http://127.0.0.1:18090;" in self.nginx,
            "missing pages-web host bridge",
        )
        for app in self.apps:
            health = app["health_check"]
            if health["kind"] != "leaderboard":
                continue
            with self.subTest(app=app["id"]):
                region = health["region"]
                self.assertIn(region, self.regions_seed["regions"])
                self.assertEqual(f"/{region}.html", urlsplit(app["public_url"]).path)
                self.assertEqual(f"/{region}.html", app["api_dependencies"][0])

    def test_kb_and_legacy_redirect_routes_exist(self):
        self.assertEqual("/kb/", urlsplit(self.by_id["kb-web"]["public_url"]).path)
        self.assertTrue("location /kb/" in self.nginx, "missing /kb/ location")
        self.assertTrue(
            "proxy_pass http://127.0.0.1:18080;" in self.nginx,
            "missing bi-web host bridge",
        )
        self.assertTrue("listen 8300;" in self.nginx, "missing legacy listener")
        self.assertTrue(
            "return 301 http://dt.yyhlzj.com$request_uri;" in self.nginx,
            "missing legacy domain redirect",
        )
        for app in self.apps:
            for legacy_url in app["legacy_urls"]:
                with self.subTest(app=app["id"], legacy_url=legacy_url):
                    legacy = urlsplit(legacy_url)
                    primary = urlsplit(app["public_url"])
                    self.assertEqual(8300, legacy.port)
                    self.assertEqual("203.205.91.241", legacy.hostname)
                    self.assertEqual(primary.path, legacy.path)
                    self.assertEqual("dt.yyhlzj.com", primary.hostname)

    def test_channel_is_public_and_ops_health_requires_login(self):
        qudao = self.by_id["bi-qudao"]
        self.assertEqual("public", qudao["auth_mode"])
        self.assertEqual("public", qudao["health_check"]["expected_access"])
        self.assertNotIn("access_review", qudao)
        ops = self.by_id["bi-ops-health"]
        self.assertEqual("login", ops["auth_mode"])
        self.assertEqual("login", ops["health_check"]["expected_access"])
        self.assertNotIn("access_review", ops)

    def test_ops_web_has_existing_spa_entry_and_dist(self):
        app = self.by_id["ops-web"]
        router = (REPO_ROOT / "common" / "ops_web" / "web" / "src" / "router.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("path: '/'", router)
        self.assertIn("component: FlowPage", router)
        self.assertTrue((REPO_ROOT / app["dist_path"] / "index.html").is_file())


if __name__ == "__main__":
    unittest.main()
