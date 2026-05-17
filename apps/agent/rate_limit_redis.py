"""Redis-backed per-domain rate limiter — cross-process correct.

Why this exists: ``InMemoryDomainRateLimiter`` is process-local, so a
Celery worker running with ``--concurrency=2`` (or multiple worker
containers) effectively doubles the per-domain RPS the operator
configured via ``AGENT_RATE_LIMIT_RPS_PER_DOMAIN``. This module
provides a Redis-shared bucket implementation that all worker
processes coordinate on, so the configured RPS holds end-to-end.

Mechanism: a tiny Lua script reads the persisted "next allowed
timestamp" for a host, computes the wait time, advances the timestamp
atomically, and returns the wait so the caller sleeps locally. Lua
guarantees atomicity across concurrent SETs without a separate lock.

Lives in the Django layer (not under ``apps.agent.pipeline.*``)
because it depends on Redis configuration that the pipeline layer
must stay free of.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from apps.agent.pipeline.rate_limit import RateLimiter, host_key

logger = logging.getLogger(__name__)


_LUA_RATE_LIMIT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local current = redis.call('GET', key)
local next_at
if current then
    next_at = tonumber(current)
else
    next_at = 0
end
local wait = next_at - now
if wait < 0 then wait = 0 end
local new_next = next_at
if now > new_next then new_next = now end
new_next = new_next + interval
redis.call('SET', key, tostring(new_next), 'EX', ttl)
return tostring(wait)
"""


class RedisDomainRateLimiter:
    """Cross-process rate limiter coordinated via Redis.

    Constructed from a redis-py-compatible client. ``register_script``
    pre-loads the Lua so the EVAL roundtrip is a single op once primed.
    Wall-clock (``time.time``) is used intentionally — all worker
    processes see the same Redis timeline, so wall-time agreement is
    what matters across machines.

    Failure mode: if Redis throws (down, partitioned), we log and
    return 0.0 — degrades to no throttle for that call rather than
    failing the whole fetch. Operators see the warning in logs and
    Sentry breadcrumbs.
    """

    KEY_PREFIX = "agent:rate_limit:"
    # SET TTL well above max interval so idle hosts expire eventually
    # without losing in-flight throttling for active ones.
    _TTL_SECONDS = 60

    def __init__(
        self,
        rate_per_second: float,
        redis_client,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = max(0.0, float(rate_per_second))
        self._clock = clock
        self._sleep = sleep
        self._redis = redis_client
        self._script = redis_client.register_script(_LUA_RATE_LIMIT)

    def acquire(self, url: str) -> float:
        if self.rate <= 0:
            return 0.0
        host = host_key(url)
        if not host:
            return 0.0
        interval = 1.0 / self.rate
        key = f"{self.KEY_PREFIX}{host}"
        try:
            raw = self._script(
                keys=[key],
                args=[self._clock(), interval, self._TTL_SECONDS],
            )
        except Exception:
            logger.warning(
                "rate_limit_redis_failure",
                extra={"host": host},
                exc_info=True,
            )
            return 0.0
        wait = float(raw)
        if wait > 0:
            self._sleep(wait)
        return wait


def build_limiter_from_settings() -> RateLimiter:
    """Construct a configured limiter at Django startup.

    Returns:
      * ``NoopRateLimiter`` if RPS ≤ 0 (no enforcement).
      * ``RedisDomainRateLimiter`` if Redis is reachable.
      * ``InMemoryDomainRateLimiter`` as a soft fallback when Redis
        can't be reached — logs a loud warning so operators notice the
        degraded guarantee.
    """
    from django.conf import settings
    from apps.agent.pipeline.rate_limit import (
        InMemoryDomainRateLimiter,
        NoopRateLimiter,
    )

    rps = float(
        getattr(settings, "AGENT_RATE_LIMIT_RPS_PER_DOMAIN", 1.0) or 0.0
    )
    if rps <= 0:
        return NoopRateLimiter()

    try:
        from django_redis import get_redis_connection

        client = get_redis_connection("default")
        client.ping()
        return RedisDomainRateLimiter(rate_per_second=rps, redis_client=client)
    except Exception:
        logger.warning(
            "rate_limit_redis_unavailable_falling_back_to_in_memory",
            exc_info=True,
        )
        return InMemoryDomainRateLimiter(rate_per_second=rps)
