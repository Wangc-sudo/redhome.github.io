"""端到端（内存 fake）：bi-web 卡片缓存命中/失效与跨线不变量。

经 ``TestClient`` 走真实 HTTP 路由（API-first，``/api/v1/d/{dash}/cards/{card}``），
依赖全部注入：静态 dashboard 源、探针卡片注册表、fake mart 连接、可控时钟的
进程内缓存。不依赖 Docker/MySQL/Redis/Nacos。

守门断言：

* 缓存命中：同参数二次请求不再触发 mart 查询（run 只算一次）；
* 键规范：参数顺序无关；热（当月）/ 冷（封月）分层各自成键；
* 失效：热 TTL 到期重算，冷 TTL 内不失效；compute 抛错不缓存坏值；
* 错误泛化：500/401/404/503/400 响应体只有固定 detail，绝不回显
  主机名、SQL、异常原文或 URL；
* 只读 mart：默认连接器只把 ``settings.mart_database`` 交给 ``connect``，
  app 源码不引用 dingtalk/wdt 库。
"""

import inspect
import threading
import unittest
import warnings
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

warnings.filterwarnings(
    "ignore", message="Using `httpx` with `starlette.testclient` is deprecated"
)

from fastapi.testclient import TestClient

from common.bi_web import app as bi_app
from common.bi_web.cache import HOT_TTL_SECONDS, InProcessCardCache
from common.bi_web.cards import Card
from common.bi_web.config import StaticDashboardSource


_TOKEN = "probe-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_DASHBOARD_ID = "probe-dash"
_CARD_ID = "probe_card"

#: 只会出现在异常原文 / fake 主机名里的泄露标记。
_LEAK_MARKER = "mart-secret-host.internal:3306/access_token=SECRET"


class _FakeMartCursor:
    """按 SQL 路由：维度选项查询返回固定选项，其余录制。"""

    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if "DATE_FORMAT" in sql:
            self._rows = [{"month": "2026-09"}, {"month": "2026-08"}]
        elif "DISTINCT region" in sql:
            self._rows = [{"region": "hangzhou"}]
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return {"1": 1}

    def close(self):
        pass


class _FakeMartConnection:
    def __init__(self):
        self.cursor_instance = _FakeMartCursor()

    def cursor(self):
        return self.cursor_instance

    def close(self):
        pass


class _ProbeCardRun:
    """计数型卡片 run：可选先抛错 N 次（异常原文携带泄露标记）。"""

    def __init__(self, failures=0):
        self.calls = 0
        self._failures = failures

    def __call__(self, connection, params):
        self.calls += 1
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError(f"mysql gone: {_LEAK_MARKER}")
        return {"value": self.calls, "params": dict(sorted(params.items()))}


def _dashboard_source():
    return StaticDashboardSource({
        _DASHBOARD_ID: {
            "title": "探针页",
            "enabled": True,
            "refresh_seconds": 300,
            "cards": [{"card": _CARD_ID, "title": "探针卡", "span": 4}],
        }
    })


def _build(*, run, params_schema=None, gate=lambda: True):
    """组装 app：可控时钟缓存 + fake mart 连接器，返回 (client, parts)。"""
    clock = {"t": 1000.0}
    cache = InProcessCardCache(monotonic=lambda: clock["t"])
    registry = {
        _CARD_ID: Card(
            card_id=_CARD_ID,
            chart="kpi",
            run=run,
            params_schema={} if params_schema is None else params_schema,
        )
    }
    settings = SimpleNamespace(
        mart_database=SimpleNamespace(name="mart_ops_test"),
        dingtalk_database=SimpleNamespace(name="raw_dingtalk_test"),
        wdt_database=SimpleNamespace(name="raw_wdt_test"),
    )

    @contextmanager
    def connector():
        yield _FakeMartConnection()

    application = bi_app.create_app(
        settings=settings,
        dashboard_source=_dashboard_source(),
        registry=registry,
        token=_TOKEN,
        gate=gate,
        db_connector=connector,
        card_cache=cache,
    )
    return TestClient(application), SimpleNamespace(
        clock=clock, cache=cache, settings=settings,
    )


def _card_url(**params):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    url = f"/api/v1/d/{_DASHBOARD_ID}/cards/{_CARD_ID}"
    return f"{url}?{query}" if query else url


class CardCacheHitTests(unittest.TestCase):

    def test_second_identical_request_hits_the_cache(self):
        run = _ProbeCardRun()
        client, _ = _build(run=run)

        first = client.get(_card_url(), headers=_AUTH)
        second = client.get(_card_url(), headers=_AUTH)

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(1, run.calls)
        self.assertEqual("no-store", first.headers["Cache-Control"])

    def test_param_order_does_not_fragment_the_key(self):
        run = _ProbeCardRun()
        client, _ = _build(
            run=run, params_schema={"region": "regions", "month": "months"},
        )

        one = client.get(_card_url(region="hangzhou", month="2026-08"),
                         headers=_AUTH)
        # 同参数集、不同顺序 -> 同一个缓存键。
        two = client.get(_card_url(month="2026-08", region="hangzhou"),
                         headers=_AUTH)

        self.assertEqual(200, one.status_code)
        self.assertEqual(200, two.status_code)
        self.assertEqual(1, run.calls)

    def test_hot_and_cold_periods_get_distinct_keys(self):
        run = _ProbeCardRun()
        client, _ = _build(run=run, params_schema={"month": "months"})
        current_month = datetime.now().strftime("%Y-%m")

        cold = client.get(_card_url(month="2026-08"), headers=_AUTH)
        hot = client.get(_card_url(), headers=_AUTH)

        self.assertEqual(200, cold.status_code)
        self.assertEqual(200, hot.status_code)
        # 封月（冷）与当月（热）是两个键：各算一次。
        self.assertEqual(2, run.calls)
        self.assertNotEqual(current_month, "2026-08")


class CardCacheInvalidationTests(unittest.TestCase):

    def test_hot_entry_expires_after_hot_ttl(self):
        run = _ProbeCardRun()
        client, parts = _build(run=run)

        client.get(_card_url(), headers=_AUTH)
        parts.clock["t"] += HOT_TTL_SECONDS + 1
        client.get(_card_url(), headers=_AUTH)

        self.assertEqual(2, run.calls)

    def test_cold_entry_survives_hot_ttl_window(self):
        run = _ProbeCardRun()
        client, parts = _build(run=run, params_schema={"month": "months"})

        client.get(_card_url(month="2026-08"), headers=_AUTH)
        parts.clock["t"] += HOT_TTL_SECONDS * 10
        client.get(_card_url(month="2026-08"), headers=_AUTH)

        self.assertEqual(1, run.calls)

    def test_failed_compute_is_never_cached_and_retries(self):
        run = _ProbeCardRun(failures=1)
        client, _ = _build(run=run)

        failed = client.get(_card_url(), headers=_AUTH)
        retried = client.get(_card_url(), headers=_AUTH)

        self.assertEqual(500, failed.status_code)
        self.assertEqual({"detail": "card_error"}, failed.json())
        self.assertNotIn(_LEAK_MARKER, failed.text)
        self.assertNotIn("Traceback", failed.text)
        # 坏值不进缓存：下一次重算并成功。
        self.assertEqual(200, retried.status_code)
        self.assertEqual(2, run.calls)

    def test_concurrent_miss_computes_once(self):
        run = _ProbeCardRun()
        client, _ = _build(run=run)
        barrier = threading.Barrier(8)
        results = []

        def hit():
            barrier.wait(timeout=5)
            results.append(client.get(_card_url(), headers=_AUTH).status_code)

        threads = [threading.Thread(target=hit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual([200] * 8, results)
        self.assertEqual(1, run.calls)


class ErrorGeneralizationTests(unittest.TestCase):
    """错误响应泛化：固定 detail 词表，任何泄露标记都不得出现在响应体。"""

    def setUp(self):
        self.client, _ = _build(run=_ProbeCardRun())

    def _assert_generic(self, response, status, detail):
        self.assertEqual(status, response.status_code)
        self.assertEqual({"detail": detail}, response.json())
        self.assertNotIn(_LEAK_MARKER, response.text)
        self.assertNotIn("Traceback", response.text)
        self.assertNotIn("SELECT", response.text)

    def test_missing_or_wrong_bearer_is_generic_401(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            with self.subTest(headers=headers):
                self._assert_generic(
                    self.client.get(_card_url(), headers=headers),
                    401, "unauthorized",
                )

    def test_unknown_dashboard_is_generic_404(self):
        self._assert_generic(
            self.client.get("/api/v1/d/ghost/cards/x", headers=_AUTH),
            404, "not_found",
        )

    def test_unknown_card_on_known_dashboard_is_generic_404(self):
        self._assert_generic(
            self.client.get(f"/api/v1/d/{_DASHBOARD_ID}/cards/ghost",
                            headers=_AUTH),
            404, "not_found",
        )

    def test_unwhitelisted_param_is_generic_400(self):
        self._assert_generic(
            self.client.get(_card_url(evil="1); DROP TABLE mart_ops--"),
                            headers=_AUTH),
            400, "bad_request",
        )

    def test_param_value_outside_dimension_is_generic_400(self):
        client, _ = _build(
            run=_ProbeCardRun(), params_schema={"region": "regions"},
        )
        self._assert_generic(
            client.get(_card_url(region="atlantis"), headers=_AUTH),
            400, "bad_request",
        )

    def test_gate_off_is_generic_503(self):
        client, _ = _build(run=_ProbeCardRun(), gate=lambda: False)
        self._assert_generic(
            client.get(_card_url(), headers=_AUTH), 503, "unavailable",
        )


class MartOnlyConnectorTests(unittest.TestCase):
    """跨线不变量：bi-web 只读 mart_ops，raw_* 库在代码层不可达。"""

    def test_default_connector_passes_only_the_mart_database(self):
        settings = SimpleNamespace(
            mart_database=SimpleNamespace(name="mart_ops_test", marker="mart"),
            dingtalk_database=SimpleNamespace(name="raw_dingtalk_test"),
            wdt_database=SimpleNamespace(name="raw_wdt_test"),
        )
        with patch.object(bi_app, "connect", return_value=_FakeMartConnection()) as mocked:
            application = bi_app.create_app(
                settings=settings,
                dashboard_source=_dashboard_source(),
                registry={},
                gate=lambda: True,
            )
            response = TestClient(application).get("/healthz")

        self.assertEqual(200, response.status_code)
        mocked.assert_called_once_with(settings.mart_database)

    def test_app_source_never_references_raw_databases(self):
        source = inspect.getsource(bi_app)
        for forbidden in (
            "settings.dingtalk_database",
            "settings.wdt_database",
            "raw_dingtalk",
            "raw_wdt",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
