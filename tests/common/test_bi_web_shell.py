"""bi-web ``/d/{id}`` shell picker tests (V1 2026-09-16; V2 2026-09-17).

Shell pick order (three tiers, decided per request): the original-design
shell ``web/bi.html`` wins first; the bi-react ``dist`` build remains a
second rollback layer until P3 removes it; ``web/index.html`` (V0) is the
final fallback.  ``BI_WEB_SHELL=legacy`` forces V0 without a restart.
The legacy shell is never deleted -- these tests pin every tier.
"""

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from common.bi_web import app as app_module
from common.bi_web.app import create_app
from common.bi_web.cards import Card
from common.bi_web.config import StaticDashboardSource

_REACT_MARKER = "<!-- bi-react-dist-shell -->"
_LEGACY_INDEX = Path(app_module.__file__).parent / "web" / "index.html"
_BI_INDEX = Path(app_module.__file__).parent / "web" / "bi.html"


def _stub_connector():
    @contextmanager
    def connector():
        yield SimpleNamespace()

    return connector


def _stub_registry():
    def run(connection, params):  # pragma: no cover - not exercised here
        return {"chart": "scalar", "value": 0, "unit": "元"}

    return {
        "kpi_offline_mtd": Card(
            card_id="kpi_offline_mtd",
            chart="scalar",
            run=run,
            params_schema={},
        )
    }


def _build_app():
    return create_app(
        settings=SimpleNamespace(),
        dashboard_source=StaticDashboardSource({
            "l1-cockpit": {
                "title": "首页驾驶舱",
                "enabled": True,
                "refresh_seconds": 86400,
                "cards": [
                    {"card": "kpi_offline_mtd", "title": "t", "span": 4},
                ],
            }
        }),
        registry=_stub_registry(),
        db_connector=_stub_connector(),
    )


class ShellPickerTests(unittest.TestCase):
    """Unit-level: _shell_index_html() honours the three tiers + env switch."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dist = Path(self._tmp.name)
        patcher = patch.object(app_module, "_REACT_DIST_DIR", self.dist)
        patcher.start()
        self.addCleanup(patcher.stop)
        # A stray BI_WEB_SHELL from the host env must not leak into cases.
        env_patcher = patch.dict(os.environ, {}, clear=False)
        env_patcher.start()
        os.environ.pop("BI_WEB_SHELL", None)
        self.addCleanup(env_patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_dist_index(self):
        (self.dist / "index.html").write_text(_REACT_MARKER, encoding="utf-8")

    def _without_bi_html(self):
        """Patch ``_WEB_DIR`` to an empty dir, simulating a missing bi.html."""
        empty_web = Path(self._tmp.name) / "web-empty"
        empty_web.mkdir(exist_ok=True)
        return patch.object(app_module, "_WEB_DIR", empty_web), empty_web

    def test_default_prefers_bi_html(self):
        self.assertEqual(
            app_module._WEB_DIR / "bi.html", app_module._shell_index_html()
        )

    def test_bi_html_wins_over_dist(self):
        self._write_dist_index()
        self.assertEqual(
            app_module._WEB_DIR / "bi.html", app_module._shell_index_html()
        )

    def test_dist_wins_only_when_bi_html_missing(self):
        # dist 是第二回退层：bi.html 缺席（P3 前的部署窗口）时才轮到他。
        self._write_dist_index()
        web_patcher, _ = self._without_bi_html()
        with web_patcher:
            self.assertEqual(self.dist / "index.html", app_module._shell_index_html())

    def test_missing_bi_and_dist_falls_back_to_legacy(self):
        web_patcher, empty_web = self._without_bi_html()
        with web_patcher:
            self.assertEqual(
                empty_web / "index.html", app_module._shell_index_html()
            )

    def test_legacy_env_forces_rollback_even_with_dist(self):
        self._write_dist_index()
        with patch.dict(os.environ, {"BI_WEB_SHELL": "legacy"}):
            self.assertEqual(
                app_module._WEB_DIR / "index.html", app_module._shell_index_html()
            )

    def test_legacy_env_is_case_insensitive(self):
        self._write_dist_index()
        with patch.dict(os.environ, {"BI_WEB_SHELL": " Legacy "}):
            self.assertEqual(
                app_module._WEB_DIR / "index.html", app_module._shell_index_html()
            )

    def test_unknown_env_value_does_not_force_legacy(self):
        self._write_dist_index()
        with patch.dict(os.environ, {"BI_WEB_SHELL": "react"}):
            self.assertEqual(
                app_module._WEB_DIR / "bi.html", app_module._shell_index_html()
            )


class ShellRouteTests(unittest.TestCase):
    """Route-level: /d/{id} serves whichever shell the picker selects."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dist = Path(self._tmp.name)
        (self.dist / "index.html").write_text(_REACT_MARKER, encoding="utf-8")
        patcher = patch.object(app_module, "_REACT_DIST_DIR", self.dist)
        patcher.start()
        self.addCleanup(patcher.stop)
        env_patcher = patch.dict(os.environ, {}, clear=False)
        env_patcher.start()
        os.environ.pop("BI_WEB_SHELL", None)
        self.addCleanup(env_patcher.stop)
        self.client = TestClient(_build_app())

    def tearDown(self):
        self._tmp.cleanup()

    def test_dashboard_route_serves_bi_html(self):
        # dist 在场也不再优先：bi.html 是 /d/{id} 的默认壳（V2, 2026-09-17）。
        response = self.client.get("/d/l1-cockpit")
        self.assertEqual(200, response.status_code)
        self.assertNotIn(_REACT_MARKER, response.text)
        self.assertEqual(_BI_INDEX.read_bytes(), response.content)

    def test_legacy_env_serves_legacy_shell(self):
        with patch.dict(os.environ, {"BI_WEB_SHELL": "legacy"}):
            response = self.client.get("/d/l1-cockpit")
        self.assertEqual(200, response.status_code)
        self.assertNotIn(_REACT_MARKER, response.text)
        self.assertEqual(_LEGACY_INDEX.read_bytes(), response.content)

    def test_missing_dist_route_still_serves_bi_html(self):
        (self.dist / "index.html").unlink()
        response = self.client.get("/d/l1-cockpit")
        self.assertEqual(200, response.status_code)
        self.assertEqual(_BI_INDEX.read_bytes(), response.content)

    def test_resolve_chain_still_guards_the_route(self):
        response = self.client.get("/d/no-such-dashboard")
        self.assertEqual(404, response.status_code)
        self.assertEqual("not_found", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
