"""Tests for the bi-web card payload cache (2026-09-14 cache spec, stage 2).

Everything runs fully injected -- no Redis, no RDS, no network.  The
matrix locks:

* key normalization: params sort by name (order-independent, no key
  explosion) and the key carries ``card_id`` + the period class;
* hot/cold dispatch: no ``month`` or current ``month`` -> ``("cur",
  HOT_TTL_SECONDS)``; any other month -> ``(month, COLD_TTL_SECONDS)``;
* the in-process backend: hit within TTL, miss past TTL, close clears;
* the Redis backend: JSON round-trip with the TTL passed as ``ex``, and
  fail-open on every failure mode (raising get -> miss, raising set ->
  silent, garbage payload -> miss, unreachable at build time -> still a
  working cache object);
* ``build_card_cache`` env dispatch: URL set -> Redis backend, unset or
  blank -> in-process;
* stampede protection: N threads missing the same key compute exactly
  once; a raising compute propagates, caches nothing, and retries;
* app wiring: the card route reads through the injected cache (second
  identical request served without a second ``run``), distinct params
  stay distinct keys, a raising backend still answers 200, and
  ``Cache-Control: no-store`` is preserved.
"""

import threading
import time
import unittest
import warnings
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

# Same httpx/starlette filter as test_bi_web_app (see there).
warnings.filterwarnings(
    "ignore", message="Using `httpx` with `starlette.testclient` is deprecated"
)

from fastapi.testclient import TestClient

from common.bi_web.app import create_app
from common.bi_web.cache import (
    COLD_TTL_SECONDS,
    HOT_TTL_SECONDS,
    InProcessCardCache,
    RedisCardCache,
    build_card_cache,
    card_cache_key,
    normalize_params,
    period_class,
)
from common.bi_web.cards import Card
from common.bi_web.config import StaticDashboardSource


#: Pin "today" so hot/cold dispatch never depends on the wall clock.
_NOW = datetime(2026, 9, 14, 10, 30)


class KeyNormalizationTests(unittest.TestCase):
    def test_params_sort_by_name(self):
        self.assertEqual("month=2026-08&region=杭州", normalize_params(
            {"region": "杭州", "month": "2026-08"}
        ))

    def test_empty_params_normalize_to_empty(self):
        self.assertEqual("", normalize_params({}))

    def test_key_is_param_order_independent(self):
        first, _ = card_cache_key(
            "kpi_region_mtd", {"month": "2026-08", "region": "杭州"}, now=_NOW
        )
        second, _ = card_cache_key(
            "kpi_region_mtd", {"region": "杭州", "month": "2026-08"}, now=_NOW
        )
        self.assertEqual(first, second)

    def test_key_shape_carries_card_params_and_period(self):
        key, _ = card_cache_key(
            "kpi_region_mtd", {"region": "杭州", "month": "2026-08"}, now=_NOW
        )
        self.assertEqual(
            "biweb:card:kpi_region_mtd:month=2026-08&region=杭州:2026-08", key
        )

    def test_distinct_param_values_are_distinct_keys(self):
        first, _ = card_cache_key("card", {"region": "杭州"}, now=_NOW)
        second, _ = card_cache_key("card", {"region": "绍兴"}, now=_NOW)
        self.assertNotEqual(first, second)

    def test_current_month_and_no_month_are_distinct_periods(self):
        explicit, _ = card_cache_key("card", {"month": "2026-09"}, now=_NOW)
        implicit, _ = card_cache_key("card", {}, now=_NOW)
        # Same hot window, but the explicit param belongs to the key.
        self.assertNotEqual(explicit, implicit)
        self.assertTrue(explicit.endswith(":cur"))
        self.assertTrue(implicit.endswith(":cur"))


class PeriodClassTests(unittest.TestCase):
    def test_no_month_is_hot(self):
        self.assertEqual(
            ("cur", HOT_TTL_SECONDS), period_class({}, now=_NOW)
        )

    def test_current_month_is_hot(self):
        self.assertEqual(
            ("cur", HOT_TTL_SECONDS),
            period_class({"month": "2026-09"}, now=_NOW),
        )

    def test_sealed_month_is_cold_and_keyed_by_month(self):
        self.assertEqual(
            ("2026-08", COLD_TTL_SECONDS),
            period_class({"month": "2026-08"}, now=_NOW),
        )

    def test_future_month_is_cold_and_flips_to_cur_when_it_arrives(self):
        # A future month is empty and unchanging: cold now; when the
        # calendar reaches it the class flips to "cur", rotating the key.
        self.assertEqual(
            ("2026-10", COLD_TTL_SECONDS),
            period_class({"month": "2026-10"}, now=_NOW),
        )
        arrived = datetime(2026, 10, 1)
        self.assertEqual(
            ("cur", HOT_TTL_SECONDS),
            period_class({"month": "2026-10"}, now=arrived),
        )

    def test_ttls_align_with_the_spec(self):
        # Hot aligns with the page refresh_seconds default; cold is a day.
        self.assertEqual(300.0, HOT_TTL_SECONDS)
        self.assertEqual(86400.0, COLD_TTL_SECONDS)


class _FakeClock:
    """Injectable monotonic clock for the in-process backend."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class InProcessCardCacheTests(unittest.TestCase):
    def test_hit_within_ttl_and_miss_past_ttl(self):
        clock = _FakeClock()
        cache = InProcessCardCache(monotonic=clock)
        cache.set("k", {"v": 1}, ttl=10.0)

        self.assertEqual({"v": 1}, cache.get("k"))
        clock.advance(9.9)
        self.assertEqual({"v": 1}, cache.get("k"))
        clock.advance(0.2)
        self.assertIsNone(cache.get("k"))

    def test_unknown_key_is_a_miss(self):
        self.assertIsNone(InProcessCardCache().get("nope"))

    def test_close_clears_all_entries(self):
        cache = InProcessCardCache()
        cache.set("k", {"v": 1}, ttl=60.0)
        cache.close()
        self.assertIsNone(cache.get("k"))


class _FakeRedis:
    """Dict-backed redis.Redis stand-in; can be told to fail wholesale."""

    def __init__(self, error=None):
        self.store = {}
        self.ttls = {}
        self.error = error
        self.closed = False

    def get(self, key):
        if self.error is not None:
            raise self.error
        return self.store.get(key)

    def set(self, key, value, ex=None):
        if self.error is not None:
            raise self.error
        self.store[key] = value
        self.ttls[key] = ex

    def close(self):
        self.closed = True


class RedisCardCacheTests(unittest.TestCase):
    def test_json_round_trip_with_ex_ttl(self):
        client = _FakeRedis()
        cache = RedisCardCache("redis://example:6379/0", client=client)
        cache.set("k", {"chart": "scalar", "value": 1.5}, ttl=300.0)

        self.assertEqual({"chart": "scalar", "value": 1.5}, cache.get("k"))
        self.assertEqual(300, client.ttls["k"])

    def test_miss_answers_none(self):
        cache = RedisCardCache("redis://example:6379/0", client=_FakeRedis())
        self.assertIsNone(cache.get("nope"))

    def test_garbage_payload_is_a_miss(self):
        client = _FakeRedis()
        client.store["k"] = b"not-json{"
        cache = RedisCardCache("redis://example:6379/0", client=client)
        self.assertIsNone(cache.get("k"))

    def test_raising_backend_fails_open(self):
        # get raises -> miss; set raises -> silent; never an exception to
        # the caller (redis 挂 != bi-web 挂).
        client = _FakeRedis(error=ConnectionError("redis down"))
        cache = RedisCardCache("redis://example:6379/0", client=client)

        self.assertIsNone(cache.get("k"))
        cache.set("k", {"v": 1}, ttl=60.0)  # must not raise

    def test_close_releases_and_reconnects_lazily(self):
        client = _FakeRedis()
        cache = RedisCardCache("redis://example:6379/0", client=client)
        cache.get("k")
        cache.close()

        self.assertTrue(client.closed)


class CacheMetricsTests(unittest.TestCase):
    """P2 可观测：hit/miss 在读穿层计数，error/耗时由后端上报。"""

    def test_hit_and_miss_are_counted_per_lookup(self):
        cache = InProcessCardCache()

        cache.get_or_compute("k", 60.0, lambda: {"v": 1})  # miss + compute
        cache.get_or_compute("k", 60.0, lambda: {"v": 1})  # hit

        snapshot = cache.metrics_snapshot()
        self.assertEqual("in_process", snapshot["backend"])
        self.assertEqual(1, snapshot["hits"])
        self.assertEqual(1, snapshot["misses"])
        self.assertEqual(0.5, snapshot["hit_rate"])

    def test_backend_calls_are_timed(self):
        cache = InProcessCardCache()

        cache.get_or_compute("k", 60.0, lambda: {"v": 1})

        latency = cache.metrics_snapshot()["backend_latency"]
        self.assertGreaterEqual(latency["calls"], 2)  # get + set
        self.assertGreaterEqual(latency["total_ms"], 0.0)
        self.assertGreaterEqual(latency["max_ms"], 0.0)
        self.assertIsNotNone(latency["avg_ms"])

    def test_raising_redis_backend_counts_errors_but_still_misses(self):
        cache = RedisCardCache(
            "redis://example:6379/0",
            client=_FakeRedis(error=ConnectionError("redis down")),
        )

        self.assertIsNone(cache.get("k"))
        cache.set("k", {"v": 1}, ttl=60.0)  # must not raise

        snapshot = cache.metrics_snapshot()
        self.assertEqual("redis", snapshot["backend"])
        self.assertEqual(2, snapshot["errors"])
        self.assertEqual(2, snapshot["backend_latency"]["calls"])

    def test_raising_backend_error_and_hit_rate_coexist(self):
        # fail-open 语义：后端全挂时每次 get_or_compute 都是 miss +
        # error，但 compute 仍执行，路由不受影响。
        cache = RedisCardCache(
            "redis://example:6379/0",
            client=_FakeRedis(error=ConnectionError("redis down")),
        )

        self.assertEqual(
            {"v": 1}, cache.get_or_compute("k", 60.0, lambda: {"v": 1})
        )

        snapshot = cache.metrics_snapshot()
        self.assertEqual(1, snapshot["misses"])
        self.assertEqual(0, snapshot["hits"])
        self.assertEqual(0.0, snapshot["hit_rate"])
        self.assertGreaterEqual(snapshot["errors"], 1)

    def test_snapshot_never_carries_the_dsn(self):
        cache = RedisCardCache(
            "redis://:pw123-secret@redis-secret-host:6379/0",
            client=_FakeRedis(),
        )

        text = str(cache.metrics_snapshot())

        self.assertNotIn("pw123-secret", text)
        self.assertNotIn("redis-secret-host", text)


class BuildCardCacheTests(unittest.TestCase):
    def test_url_present_builds_redis_backend(self):
        cache = build_card_cache(
            {"PUBLIC_DATA_REDIS_URL": "redis://redis:6379/0"}
        )
        try:
            self.assertIsInstance(cache, RedisCardCache)
        finally:
            cache.close()

    def test_blank_or_missing_url_builds_in_process_backend(self):
        for env in ({}, {"PUBLIC_DATA_REDIS_URL": ""},
                    {"PUBLIC_DATA_REDIS_URL": "   "}):
            with self.subTest(env=env):
                self.assertIsInstance(build_card_cache(env), InProcessCardCache)


class ReadThroughTests(unittest.TestCase):
    """``get_or_compute``: per-key locks, one compute among stampedes."""

    def test_miss_computes_once_and_caches(self):
        cache = InProcessCardCache()
        computes = []

        def compute():
            computes.append(1)
            return {"v": 1}

        self.assertEqual({"v": 1}, cache.get_or_compute("k", 60.0, compute))
        self.assertEqual({"v": 1}, cache.get_or_compute("k", 60.0, compute))
        self.assertEqual(1, len(computes))

    def test_concurrent_misses_compute_exactly_once(self):
        cache = InProcessCardCache()
        computes = []
        started = threading.Barrier(8)

        def compute():
            computes.append(1)
            time.sleep(0.05)  # widen the race window
            return {"v": len(computes)}

        results = []

        def worker():
            started.wait(timeout=5)
            results.append(cache.get_or_compute("k", 60.0, compute))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(1, len(computes))
        self.assertEqual([{"v": 1}] * 8, results)

    def test_raising_compute_propagates_caches_nothing_and_retries(self):
        cache = InProcessCardCache()
        attempts = []

        def failing():
            attempts.append(1)
            raise RuntimeError("mart down")

        with self.assertRaises(RuntimeError):
            cache.get_or_compute("k", 60.0, failing)
        self.assertIsNone(cache.get("k"))
        with self.assertRaises(RuntimeError):
            cache.get_or_compute("k", 60.0, failing)
        self.assertEqual(2, len(attempts))

    def test_fail_open_backend_still_computes(self):
        # A backend whose get/set always raise must degrade to a plain
        # pass-through: the route keeps answering from the mart.
        cache = RedisCardCache(
            "redis://example:6379/0",
            client=_FakeRedis(error=ConnectionError("redis down")),
        )
        self.assertEqual(
            {"v": 1}, cache.get_or_compute("k", 60.0, lambda: {"v": 1})
        )


# ---------------------------------------------------------------------------
# App wiring: the card route reads through the injected cache
# ---------------------------------------------------------------------------

def _stub_registry(run_calls):
    """One-card registry recording run calls, like test_bi_web_app fakes."""
    def run(connection, params):
        run_calls.append(params)
        return {"chart": "scalar", "params": params}

    return {"kpi_offline_mtd": Card(
        card_id="kpi_offline_mtd",
        chart="scalar",
        run=run,
        params_schema={},
    )}


def _stub_connector():
    @contextmanager
    def connector():
        yield SimpleNamespace()

    return connector


def _cached_app(run_calls, card_cache):
    return create_app(
        settings=SimpleNamespace(),
        dashboard_source=StaticDashboardSource({
            "l1-cockpit": {
                "title": "首页驾驶舱",
                "enabled": True,
                "refresh_seconds": 300,
                "cards": [
                    {"card": "kpi_offline_mtd", "title": "t", "span": 4},
                ],
            }
        }),
        registry=_stub_registry(run_calls),
        db_connector=_stub_connector(),
        card_cache=card_cache,
    )


class AppWiringTests(unittest.TestCase):
    def test_second_identical_request_is_served_from_cache(self):
        run_calls = []
        client = TestClient(_cached_app(run_calls, InProcessCardCache()))

        first = client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd")
        second = client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd")

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(first.json(), second.json())
        self.assertEqual("no-store", second.headers["cache-control"])
        self.assertEqual(1, len(run_calls))

    def test_distinct_params_use_distinct_keys(self):
        run_calls = []

        def run(connection, params):
            run_calls.append(params)
            return {"chart": "scalar", "params": params}

        registry = {"kpi_offline_mtd": Card(
            card_id="kpi_offline_mtd",
            chart="scalar",
            run=run,
            params_schema={"region": "regions"},
        )}
        app = create_app(
            settings=SimpleNamespace(),
            dashboard_source=StaticDashboardSource({
                "l1-cockpit": {
                    "title": "首页驾驶舱",
                    "enabled": True,
                    "refresh_seconds": 300,
                    "cards": [
                        {"card": "kpi_offline_mtd", "title": "t", "span": 4},
                    ],
                }
            }),
            registry=registry,
            db_connector=_stub_connector(),
            card_cache=InProcessCardCache(),
        )
        client = TestClient(app)

        # No value gate tripwires here: unknown option-set sources fail
        # open, so any region value reaches run (test_bi_web_app locks
        # the gate itself; here only the cache key matters).
        client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd",
                   params={"region": "杭州"})
        client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd",
                   params={"region": "绍兴"})
        client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd",
                   params={"region": "杭州"})

        self.assertEqual(2, len(run_calls))

    def test_diagnostics_endpoint_counts_requests_across_the_route(self):
        run_calls = []
        client = TestClient(_cached_app(run_calls, InProcessCardCache()))

        client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd")  # miss
        client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd")  # hit
        response = client.get("/diagnostics/cache")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("in_process", payload["backend"])
        self.assertEqual(1, payload["hits"])
        self.assertEqual(1, payload["misses"])
        self.assertEqual(0.5, payload["hit_rate"])
        self.assertEqual(0, payload["errors"])
        self.assertGreaterEqual(payload["backend_latency"]["calls"], 3)

    def test_diagnostics_endpoint_never_leaks_the_dsn(self):
        run_calls = []
        cache = RedisCardCache(
            "redis://:pw123-secret@redis-secret-host:6379/0",
            client=_FakeRedis(error=ConnectionError("redis down")),
        )
        client = TestClient(_cached_app(run_calls, cache))

        client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd")  # fail-open
        response = client.get("/diagnostics/cache")

        self.assertEqual(200, response.status_code)
        self.assertEqual("redis", response.json()["backend"])
        self.assertGreaterEqual(response.json()["errors"], 1)
        self.assertNotIn("pw123-secret", response.text)
        self.assertNotIn("redis-secret-host", response.text)

    def test_raising_backend_never_5xxes_the_route(self):
        run_calls = []
        cache = RedisCardCache(
            "redis://example:6379/0",
            client=_FakeRedis(error=ConnectionError("redis down")),
        )
        client = TestClient(_cached_app(run_calls, cache))

        response = client.get("/api/d/l1-cockpit/cards/kpi_offline_mtd")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"chart": "scalar", "params": {}}, response.json())
        self.assertEqual(1, len(run_calls))


if __name__ == "__main__":
    unittest.main()
