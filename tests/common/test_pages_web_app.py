"""pages-web（榜单页请求时渲染）离线单测。

钉死降级链与路由安全闸（2026-10-13 cron 解耦）：
* 当前页 TTL 缓存读穿（renderer 只算一次）、成功即双写 /output；
* 渲染失败 → 磁盘 last-good（200）→ 磁盘也无 → 503（nginx @static 兜）；
* 存档页磁盘快路径优先，缺失才渲染并写存档；
* 页面名正则安全闸：未知 region / 路径穿越 / 非法日期一律 404。

全部 TestClient + fake connector/renderer/cache：不联网、不碰凭据。
"""

import unittest
import warnings
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

warnings.filterwarnings(
    "ignore", message="Using `httpx` with `starlette.testclient` is deprecated"
)

from fastapi.testclient import TestClient

from common.bi_web.cache import InProcessCardCache
from common.pages_web.app import create_app

_HANGZHOU = SimpleNamespace(region="hangzhou", display="杭州")


class _Cursor:
    def __init__(self, fail=False):
        self._fail = fail

    def execute(self, sql, params=None):
        if self._fail:
            raise RuntimeError("db down")

    def fetchone(self):
        return (1,)

    def close(self):
        pass


class _Connection:
    def __init__(self, fail=False):
        self._fail = fail

    def cursor(self):
        return _Cursor(fail=self._fail)

    def close(self):
        pass


def _connector(fail=False):
    @contextmanager
    def connector():
        if fail:
            raise RuntimeError("connect failed")
        yield _Connection(fail=fail)

    return connector


def _renderer(html="<html>完成率榜单</html>", error=None):
    """fake renderer：模拟 render_leaderboard_page 的写盘行为并计数。"""
    calls = []

    def renderer(conn, *, region, cfg, business_date, now, output):
        calls.append((region, business_date, Path(output)))
        if error is not None:
            raise error
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(html, encoding="utf-8")
        return html, "people=1"

    return renderer, calls


def _client(tmp_path, *, renderer=None, connector=None, cache=None):
    settings = SimpleNamespace(region_seed_path=Path("seed.json"))
    app = create_app(
        settings=settings,
        db_connector=connector if connector is not None else _connector(),
        cache=cache if cache is not None else InProcessCardCache(),
        renderer=renderer,
        output_dir=tmp_path,
    )
    return TestClient(app)


class PagesWebPageTests(unittest.TestCase):
    def setUp(self):
        self._regions = patch(
            "common.daily_robot.mart_cli.load_region_configs",
            return_value={"hangzhou": _HANGZHOU},
        )
        self._regions.start()
        self.addCleanup(self._regions.stop)

    def test_current_page_renders_and_hits_ttl_cache(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            renderer, calls = _renderer()
            with _client(Path(tmp), renderer=renderer) as client:
                first = client.get("/hangzhou.html")
                second = client.get("/hangzhou.html")

            self.assertEqual(200, first.status_code)
            self.assertEqual("<html>完成率榜单</html>", first.text)
            self.assertEqual("no-store", first.headers["cache-control"])
            # TTL 缓存读穿：第二次请求不重算
            self.assertEqual(1, len(calls))
            # 渲染成功即落盘（当前页 last-good 持续保温）
            self.assertEqual(
                "<html>完成率榜单</html>",
                (Path(tmp) / "hangzhou.html").read_text(encoding="utf-8"),
            )

    def test_render_failure_falls_back_to_last_good(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            last_good = Path(tmp) / "hangzhou.html"
            last_good.write_text("<html>昨天的榜</html>", encoding="utf-8")
            renderer, _calls = _renderer(error=RuntimeError("mart down"))
            with _client(Path(tmp), renderer=renderer) as client:
                response = client.get("/hangzhou.html")

            self.assertEqual(200, response.status_code)
            self.assertEqual("<html>昨天的榜</html>", response.text)

    def test_render_failure_without_file_is_503(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            renderer, _calls = _renderer(error=RuntimeError("mart down"))
            with _client(Path(tmp), renderer=renderer) as client:
                response = client.get("/hangzhou.html")

            self.assertEqual(503, response.status_code)

    def test_unknown_region_is_404(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            renderer, calls = _renderer()
            with _client(Path(tmp), renderer=renderer) as client:
                response = client.get("/mystery.html")

            self.assertEqual(404, response.status_code)
            self.assertEqual([], calls)

    def test_path_traversal_and_junk_are_rejected(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            renderer, _calls = _renderer()
            with _client(Path(tmp), renderer=renderer) as client:
                for path in (
                    "/..%2Fapp.env",
                    "/..\\app.env",
                    "/hangzhou.html.extra",
                    "/UPPER.html",
                    "/hangzhou.html/../../etc/passwd",
                ):
                    self.assertEqual(404, client.get(path).status_code, path)

    def test_archive_page_served_from_disk_without_render(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "hangzhou-2026-10-09.html"
            archive.write_text("<html>存档</html>", encoding="utf-8")
            renderer, calls = _renderer()
            with _client(Path(tmp), renderer=renderer) as client:
                response = client.get("/hangzhou-2026-10-09.html")

            self.assertEqual(200, response.status_code)
            self.assertEqual("<html>存档</html>", response.text)
            self.assertEqual([], calls)

    def test_missing_archive_page_renders_and_writes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            renderer, calls = _renderer(html="<html>补档</html>")
            with _client(Path(tmp), renderer=renderer) as client:
                response = client.get("/hangzhou-2026-10-09.html")

            self.assertEqual(200, response.status_code)
            self.assertEqual("<html>补档</html>", response.text)
            self.assertEqual(1, len(calls))
            self.assertEqual(
                "<html>补档</html>",
                (Path(tmp) / "hangzhou-2026-10-09.html").read_text(
                    encoding="utf-8"
                ),
            )

    def test_archive_render_failure_is_503(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            renderer, _calls = _renderer(error=RuntimeError("mart down"))
            with _client(Path(tmp), renderer=renderer) as client:
                response = client.get("/hangzhou-2026-10-09.html")

            self.assertEqual(503, response.status_code)

    def test_archive_with_impossible_date_is_404(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            renderer, _calls = _renderer()
            with _client(Path(tmp), renderer=renderer) as client:
                response = client.get("/hangzhou-2026-13-99.html")

            self.assertEqual(404, response.status_code)


class PagesWebHealthzTests(unittest.TestCase):
    def test_healthz_ok_and_unhealthy(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(region_seed_path=None)
            with _client(Path(tmp), connector=_connector()) as client:
                response = client.get("/healthz")
            self.assertEqual(200, response.status_code)
            self.assertEqual("ok", response.json()["status"])

            with _client(Path(tmp), connector=_connector(fail=True)) as client:
                response = client.get("/healthz")
            self.assertEqual(503, response.status_code)
            self.assertEqual("unhealthy", response.json()["status"])

    def test_missing_region_seed_is_503(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            renderer, _calls = _renderer()
            settings = SimpleNamespace(region_seed_path=None)
            app = create_app(
                settings=settings,
                db_connector=_connector(),
                cache=InProcessCardCache(),
                renderer=renderer,
                output_dir=Path(tmp),
            )
            with TestClient(app) as client:
                response = client.get("/hangzhou.html")

            self.assertEqual(503, response.status_code)


if __name__ == "__main__":
    unittest.main()
