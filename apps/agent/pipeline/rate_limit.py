"""Per-domain rate limiting for the outbound fetchers.

This module is **pure Python** — no Django imports — so the pipeline
layer stays portable to the future autonomous-agent service. The
Django bridge (`apps.agent.apps.AgentConfig.ready`) registers a
process-appropriate implementation at startup via
``set_default_limiter()``.

Two implementations ship here:

* :class:`NoopRateLimiter` — never sleeps. Default until something
  registers a real limiter. Useful for tests that don't care.
* :class:`InMemoryDomainRateLimiter` — thread-safe token bucket
  keyed by URL hostname. Adequate when there is exactly one worker
  process; **not** adequate under ``--concurrency=2`` (each child
  process keeps its own bucket → actual rate doubles).

The Redis-backed cross-process implementation lives at
``apps.agent.rate_limit_redis`` (Django layer — it can import
``django_redis``). The pipeline never imports it directly.

Contract: ``acquire(url)`` blocks until the next request to ``url``'s
host is allowed and returns the slept duration (useful for tests /
logging). Rate of zero or non-HTTP URLs short-circuit to a no-op.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Protocol
from urllib.parse import urlparse


def host_key(url: str) -> str:
    """Lowercased hostname for ``url``; empty string on parse failure."""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


class RateLimiter(Protocol):
    """Minimal interface fetchers depend on."""

    def acquire(self, url: str) -> float: ...


class NoopRateLimiter:
    """Zero-overhead no-op. Returned by ``get_default_limiter()`` until
    the Django bridge registers a real limiter at app startup."""

    def acquire(self, url: str) -> float:
        return 0.0


class _Bucket:
    __slots__ = ("next_at", "lock")

    def __init__(self) -> None:
        self.next_at = 0.0
        self.lock = threading.Lock()


class InMemoryDomainRateLimiter:
    """Thread-safe token-bucket throttle keyed by URL hostname.

    Process-local: two Celery child processes each carry their own
    state, so the effective per-domain RPS is ``rate_per_second ×
    worker_count``. Use the Redis-backed limiter
    (:class:`apps.agent.rate_limit_redis.RedisDomainRateLimiter`)
    when running with ``--concurrency > 1`` or multiple workers.
    """

    def __init__(
        self,
        rate_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = max(0.0, float(rate_per_second))
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, _Bucket] = {}
        self._registry_lock = threading.Lock()

    def acquire(self, url: str) -> float:
        if self.rate <= 0:
            return 0.0
        host = host_key(url)
        if not host:
            return 0.0
        bucket = self._bucket_for(host)
        interval = 1.0 / self.rate
        with bucket.lock:
            now = self._clock()
            wait = bucket.next_at - now
            if wait > 0:
                self._sleep(wait)
                now = now + wait
            else:
                wait = 0.0
            bucket.next_at = max(now, bucket.next_at) + interval
        return wait

    def _bucket_for(self, host: str) -> _Bucket:
        bucket = self._buckets.get(host)
        if bucket is not None:
            return bucket
        with self._registry_lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                bucket = _Bucket()
                self._buckets[host] = bucket
        return bucket


# Backwards-compat alias for existing callsites / tests.
DomainRateLimiter = InMemoryDomainRateLimiter


# ---------------------------------------------------------------------------
# Process-wide registration. Pipeline callers grab the limiter via
# ``get_default_limiter()``; the Django bridge installs a real one in
# ``AgentConfig.ready()``.
# ---------------------------------------------------------------------------
_DEFAULT_LIMITER: RateLimiter = NoopRateLimiter()
_DEFAULT_LIMITER_LOCK = threading.Lock()


def get_default_limiter() -> RateLimiter:
    return _DEFAULT_LIMITER


def set_default_limiter(limiter: RateLimiter) -> None:
    """Install ``limiter`` as the process-wide default. Used by the
    Django bridge at startup and by tests that want a concrete
    limiter without going through settings."""
    global _DEFAULT_LIMITER
    with _DEFAULT_LIMITER_LOCK:
        _DEFAULT_LIMITER = limiter


def reset_default_limiter() -> None:
    """Test helper — restore the no-op default."""
    set_default_limiter(NoopRateLimiter())
