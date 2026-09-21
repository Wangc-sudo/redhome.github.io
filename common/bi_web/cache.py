"""Card payload cache for bi-web (2026-09-14 cache spec, stage 2).

Redis 数据缓存 + 冷热分层，一律 fail-open：

* **可选增强，不是硬依赖**（设计 D2）：``PUBLIC_DATA_REDIS_URL`` 缺省为空
  → 进程内缓存兜底（语义等价单容器部署）；配置了才连，且连接是惰性的，
  Redis 不可达只表现为缓存 miss，绝不把 5xx 带给卡片路由。
* **冷热分层 = 缓存分层，不是存储分层**（设计 D1）：窗口含当天（无
  ``month`` 或 ``month`` == 当前月）= 热，短 TTL；``month`` 非当前月
  （已封月或未来月，结果不变）= 冷，长 TTL，键随月翻转。
* **键规范**（设计 D5）：``biweb:card:{card_id}:{规范化参数}:{周期类别}``；
  参数按名排序拼接（顺序无关），杜绝键爆炸。
* **防惊群**：同一键并发 miss 只算一次（per-key 锁，双检查）；``compute``
  抛错时异常传播且不缓存坏值。
* 缓存只存在于服务端：HTTP 层的 ``Cache-Control: no-store`` 响应头由
  路由保留，与本模块无关。

输出纪律与 :mod:`common.bi_web.app` 一致：后端故障静默吞掉（不记日志、
不带异常文本），因为 miss 的代价只是一次 mart 直查。

可观测（P2）：``get_or_compute`` 在基类累计 hit/miss，两个后端各自累计
error 与后端调用耗时；汇总经 :meth:`CardCache.metrics_snapshot` 只读暴露
（诊断端点消费），快照只含计数与后端名，绝不携带 DSN、键样本或载荷。
"""

import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

#: 热 TTL：对齐页面 ``refresh_seconds`` 缺省（设计阶段 2）。
HOT_TTL_SECONDS = 300.0

#: 冷 TTL：封月数据理论上不变，一天兜底（设计阶段 2）。
COLD_TTL_SECONDS = 86400.0

#: 键前缀（设计 D5）。
_KEY_PREFIX = "biweb:card"

#: 环境开关：非空 → Redis 后端，否则进程内后端。
_REDIS_URL_ENV = "PUBLIC_DATA_REDIS_URL"


# ---------------------------------------------------------------------------
# Key construction and the hot/cold split
# ---------------------------------------------------------------------------

def normalize_params(params: Dict[str, str]) -> str:
    """参数规范化：按名排序拼接，同一参数集与顺序无关（杜绝键爆炸）。"""
    return "&".join(f"{name}={params[name]}" for name in sorted(params))


def period_class(params: Dict[str, str], *, now: Optional[datetime] = None,
                 hot_ttl: float = HOT_TTL_SECONDS,
                 cold_ttl: float = COLD_TTL_SECONDS) -> Tuple[str, float]:
    """周期类别 + TTL：``("cur", 热 TTL)`` 或 ``(month, 冷 TTL)``。

    无 ``month`` 或 ``month`` == 当前月 → 热（窗口含当天，随 extract
    同步在变）。任何非当前月 → 冷、键带月：封月结果不变；未来月结果
    为空且不变，与冷数据同性质，且跨入该月后类别翻转为 ``cur``，键
    自然轮换，绝不永久陈旧。

    粒度（2026-09-18 批次 A）：``gran == "day"`` 一律热（日序列随
    T+1 入仓在变，冷 TTL 内不刷新会丢当天数据）；``gran`` 为
    week/month 时窗口终点 = ``month`` 参数（截止月），落在当前月 →
    热，否则冷。
    """
    current = (datetime.now() if now is None else now).strftime("%Y-%m")
    month = params.get("month") or None
    gran = params.get("gran") or None
    if gran == "day":
        return "cur", hot_ttl
    if month is None or month == current:
        return "cur", hot_ttl
    return month, cold_ttl


def card_cache_key(card_id: str, params: Dict[str, str], *,
                   now: Optional[datetime] = None,
                   hot_ttl: float = HOT_TTL_SECONDS,
                   cold_ttl: float = COLD_TTL_SECONDS) -> Tuple[str, float]:
    """缓存键 + TTL：``biweb:card:{card_id}:{规范化参数}:{周期类别}``。"""
    period, ttl = period_class(params, now=now, hot_ttl=hot_ttl, cold_ttl=cold_ttl)
    return f"{_KEY_PREFIX}:{card_id}:{normalize_params(params)}:{period}", ttl


# ---------------------------------------------------------------------------
# Metrics (P2): hit/miss at the read-through layer, error + latency per backend
# ---------------------------------------------------------------------------

class CacheMetrics:
    """线程安全计数器：hit / miss / error / 后端调用耗时。

    只暴露聚合快照（计数、命中率、耗时汇总），不持有键、载荷、URL——
    诊断端点可以原样返回快照而不触碰泄露边界。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._errors = 0
        self._latency_calls = 0
        self._latency_total_seconds = 0.0
        self._latency_max_seconds = 0.0

    def record_hit(self) -> None:
        with self._lock:
            self._hits += 1

    def record_miss(self) -> None:
        with self._lock:
            self._misses += 1

    def record_error(self) -> None:
        with self._lock:
            self._errors += 1

    def record_backend_call(self, elapsed_seconds: float) -> None:
        with self._lock:
            self._latency_calls += 1
            self._latency_total_seconds += elapsed_seconds
            if elapsed_seconds > self._latency_max_seconds:
                self._latency_max_seconds = elapsed_seconds

    def snapshot(self) -> Dict[str, Any]:
        """只读聚合：计数、命中率（无请求时为 ``None``）、耗时毫秒汇总。"""
        with self._lock:
            hits, misses, errors = self._hits, self._misses, self._errors
            calls = self._latency_calls
            total_ms = self._latency_total_seconds * 1000.0
            max_ms = self._latency_max_seconds * 1000.0
        lookups = hits + misses
        return {
            "hits": hits,
            "misses": misses,
            "errors": errors,
            "hit_rate": (hits / lookups) if lookups else None,
            "backend_latency": {
                "calls": calls,
                "total_ms": total_ms,
                "max_ms": max_ms,
                "avg_ms": (total_ms / calls) if calls else None,
            },
        }


# ---------------------------------------------------------------------------
# The cache contract and its backends
# ---------------------------------------------------------------------------

class CardCache:
    """Contract: ``get`` / ``set`` / ``close``，外加共享的读穿防惊群。

    后端只承诺两件事：``get`` miss 返回 ``None``；``set`` 失败不向调用
    方传播（fail-open）。``get_or_compute`` 在基类用 per-key 锁实现，
    两个后端自动获得「同一键并发 miss 只算一次」的语义；hit/miss 也
    在这一层计数，后端经 ``_metrics`` 上报 error 与调用耗时。
    """

    #: 诊断快照里的后端名（绝不携带 DSN）。
    backend_name = "unknown"

    def __init__(self) -> None:
        self._key_locks: Dict[str, threading.Lock] = {}
        self._key_locks_guard = threading.Lock()
        self._metrics = CacheMetrics()

    def get(self, key):  # pragma: no cover - interface
        """命中返回载荷，miss（含后端故障）返回 ``None``。"""
        raise NotImplementedError

    def set(self, key, payload, ttl):  # pragma: no cover - interface
        """写入载荷；失败静默（fail-open，绝不拖垮卡片路由）。"""
        raise NotImplementedError

    def close(self):
        """释放后端资源（缺省无资源）。"""

    def metrics_snapshot(self) -> Dict[str, Any]:
        """只读诊断快照：后端名 + 计数聚合（无 DSN、无键、无载荷）。"""
        snapshot = self._metrics.snapshot()
        return {"backend": self.backend_name, **snapshot}

    def _key_lock(self, key):
        with self._key_locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def get_or_compute(self, key: str, ttl: float,
                       compute: Callable[[], Any]) -> Any:
        """读穿：命中直返；miss 时在 per-key 锁内双检查后只算一次。

        ``compute`` 抛错 → 异常原样传播且不缓存坏值，下一次调用重试。
        hit/miss 按「外层一次查询」计数：锁内双检查命中视为 hit。
        """
        payload = self.get(key)
        if payload is not None:
            self._metrics.record_hit()
            return payload
        self._metrics.record_miss()
        with self._key_lock(key):
            payload = self.get(key)
            if payload is not None:
                self._metrics.record_hit()
                return payload
            payload = compute()
            self.set(key, payload, ttl)
            return payload


class InProcessCardCache(CardCache):
    """缺省后端：进程内 TTL 字典（线程安全）。

    语义等价单容器部署的现状：没有 Redis 时卡片仍在同一进程内去重。
    """

    backend_name = "in_process"

    def __init__(self, *, monotonic: Optional[Callable[[], float]] = None) -> None:
        super().__init__()
        self._monotonic = time.monotonic if monotonic is None else monotonic
        self._entries: Dict[str, Tuple[float, Any]] = {}
        self._guard = threading.Lock()

    def get(self, key):
        started = time.monotonic()
        try:
            with self._guard:
                entry = self._entries.get(key)
                if entry is None:
                    return None
                expires_at, payload = entry
                if self._monotonic() >= expires_at:
                    self._entries.pop(key, None)
                    return None
                return payload
        except Exception:
            self._metrics.record_error()
            return None
        finally:
            self._metrics.record_backend_call(time.monotonic() - started)

    def set(self, key, payload, ttl):
        started = time.monotonic()
        try:
            with self._guard:
                self._entries[key] = (self._monotonic() + ttl, payload)
        except Exception:
            self._metrics.record_error()
        finally:
            self._metrics.record_backend_call(time.monotonic() - started)

    def close(self):
        with self._guard:
            self._entries.clear()


class RedisCardCache(CardCache):
    """Redis 后端（fail-open）：任何异常 → 视为 miss；set 失败静默。

    连接是惰性的：构造不连，首次读写才经 ``redis.Redis.from_url``
    建立，所以 Redis 不可达从启动到运行都只是 miss，绝不 503。
    载荷以 JSON 存储（``run_*`` 已转 float，与 HTTP 响应同一编码面）。
    """

    backend_name = "redis"

    def __init__(self, url: str, *, client: Any = None) -> None:
        super().__init__()
        self._url = url
        self._client = client  # injectable for tests

    def _redis(self):
        if self._client is None:
            import redis
            self._client = redis.Redis.from_url(self._url)
        return self._client

    def get(self, key):
        started = time.monotonic()
        try:
            raw = self._redis().get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception:
            self._metrics.record_error()
            return None
        finally:
            self._metrics.record_backend_call(time.monotonic() - started)

    def set(self, key, payload, ttl):
        started = time.monotonic()
        try:
            self._redis().set(key, json.dumps(payload), ex=max(1, int(ttl)))
        except Exception:
            self._metrics.record_error()
        finally:
            self._metrics.record_backend_call(time.monotonic() - started)

    def close(self):
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def build_card_cache(environ: Optional[Dict[str, str]] = None) -> "CardCache":
    """按环境选后端：``PUBLIC_DATA_REDIS_URL`` 非空 → Redis，否则进程内。"""
    env = os.environ if environ is None else environ
    url = (env.get(_REDIS_URL_ENV) or "").strip()
    if url:
        return RedisCardCache(url)
    return InProcessCardCache()
