"""Per-domain rate limiter unit tests.

Covers the pure-Python in-memory limiter and the Django-bridge
registration path. The Redis-backed limiter is tested in
``tests/agent/test_rate_limit_redis.py`` against a mocked client so
the suite stays infra-free.
"""
from __future__ import annotations

import pytest

from apps.agent.pipeline import rate_limit
from apps.agent.pipeline.rate_limit import (
    DomainRateLimiter,
    InMemoryDomainRateLimiter,
    NoopRateLimiter,
)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_noop_limiter_never_sleeps() -> None:
    limiter = NoopRateLimiter()
    assert limiter.acquire("https://example.com") == 0.0


def test_no_throttle_when_rate_zero() -> None:
    clock = _FakeClock()
    limiter = InMemoryDomainRateLimiter(0, clock=clock.time, sleep=clock.sleep)

    waited = limiter.acquire("https://example.com/a")
    assert waited == 0.0
    assert clock.sleeps == []


def test_first_request_runs_immediately() -> None:
    clock = _FakeClock()
    limiter = InMemoryDomainRateLimiter(1.0, clock=clock.time, sleep=clock.sleep)

    waited = limiter.acquire("https://example.com/a")
    assert waited == 0.0
    assert clock.sleeps == []


def test_second_request_to_same_host_waits_one_interval() -> None:
    clock = _FakeClock()
    limiter = InMemoryDomainRateLimiter(2.0, clock=clock.time, sleep=clock.sleep)

    limiter.acquire("https://example.com/a")
    waited = limiter.acquire("https://example.com/b")
    # 2 RPS → 0.5s interval between calls.
    assert waited == pytest.approx(0.5)
    assert clock.sleeps == [pytest.approx(0.5)]


def test_different_hosts_dont_share_bucket() -> None:
    clock = _FakeClock()
    limiter = InMemoryDomainRateLimiter(1.0, clock=clock.time, sleep=clock.sleep)

    limiter.acquire("https://github.com/a")
    waited = limiter.acquire("https://example.com/x")
    assert waited == 0.0


def test_hostname_case_insensitive() -> None:
    clock = _FakeClock()
    limiter = InMemoryDomainRateLimiter(1.0, clock=clock.time, sleep=clock.sleep)

    limiter.acquire("https://GitHub.com/a")
    waited = limiter.acquire("https://github.com/b")
    assert waited > 0


def test_non_http_url_is_noop() -> None:
    clock = _FakeClock()
    limiter = InMemoryDomainRateLimiter(1.0, clock=clock.time, sleep=clock.sleep)

    assert limiter.acquire("") == 0.0
    assert limiter.acquire("not a url") == 0.0


def test_domain_rate_limiter_alias_points_at_in_memory() -> None:
    """Existing code/tests that imported the historic alias still work."""
    assert DomainRateLimiter is InMemoryDomainRateLimiter


@pytest.fixture(autouse=True)
def _reset_global_limiter():
    rate_limit.reset_default_limiter()
    yield
    rate_limit.reset_default_limiter()


def test_default_limiter_starts_as_noop() -> None:
    """No Django bridge ran → pipeline gets the safe Noop default."""
    assert isinstance(rate_limit.get_default_limiter(), NoopRateLimiter)


def test_set_default_limiter_installs_instance() -> None:
    clock = _FakeClock()
    real = InMemoryDomainRateLimiter(2.0, clock=clock.time, sleep=clock.sleep)
    rate_limit.set_default_limiter(real)
    assert rate_limit.get_default_limiter() is real
