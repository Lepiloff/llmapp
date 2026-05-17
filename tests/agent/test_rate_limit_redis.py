"""Tests for the Redis-backed cross-process rate limiter.

The Redis client is mocked — we exercise the Lua-script call shape
and verify the fallback behaviour, not Redis itself.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from django.test import override_settings

from apps.agent.pipeline import rate_limit
from apps.agent.pipeline.rate_limit import (
    InMemoryDomainRateLimiter,
    NoopRateLimiter,
)
from apps.agent.rate_limit_redis import (
    RedisDomainRateLimiter,
    build_limiter_from_settings,
)


class _ScriptedClient:
    """Captures `register_script(...)` and lets each test stub it out."""

    def __init__(self, response: str = "0", *, raises: Exception | None = None) -> None:
        self.response = response
        self.raises = raises
        self.calls: list[dict] = []
        self.script = self._invoke

    def register_script(self, _lua_source: str):
        return self.script

    def _invoke(self, keys, args):
        if self.raises:
            raise self.raises
        self.calls.append({"keys": keys, "args": args})
        return self.response


@pytest.fixture(autouse=True)
def _reset_default_limiter():
    rate_limit.reset_default_limiter()
    yield
    rate_limit.reset_default_limiter()


# ---------------------------------------------------------------------------
# RedisDomainRateLimiter behaviour
# ---------------------------------------------------------------------------
def test_acquire_invokes_lua_with_expected_args() -> None:
    sleeps: list[float] = []
    client = _ScriptedClient(response="0")

    limiter = RedisDomainRateLimiter(
        rate_per_second=2.0,
        redis_client=client,
        clock=lambda: 1000.0,
        sleep=sleeps.append,
    )
    waited = limiter.acquire("https://github.com/a")

    assert waited == 0.0
    assert sleeps == []
    call = client.calls[0]
    assert call["keys"] == ["agent:rate_limit:github.com"]
    # args: now, interval, ttl
    assert call["args"] == [1000.0, pytest.approx(0.5), RedisDomainRateLimiter._TTL_SECONDS]


def test_acquire_sleeps_when_lua_returns_positive_wait() -> None:
    sleeps: list[float] = []
    client = _ScriptedClient(response="0.75")
    limiter = RedisDomainRateLimiter(
        rate_per_second=1.0,
        redis_client=client,
        clock=lambda: 0.0,
        sleep=sleeps.append,
    )
    waited = limiter.acquire("https://example.com/x")
    assert waited == pytest.approx(0.75)
    assert sleeps == [pytest.approx(0.75)]


def test_zero_rate_short_circuits_without_redis_call() -> None:
    client = _ScriptedClient(response="0")
    limiter = RedisDomainRateLimiter(
        rate_per_second=0,
        redis_client=client,
        clock=lambda: 0.0,
        sleep=lambda _s: None,
    )
    assert limiter.acquire("https://github.com/a") == 0.0
    assert client.calls == []


def test_redis_failure_degrades_to_no_throttle() -> None:
    """A Redis outage must not break outbound fetches; we log + skip."""
    sleeps: list[float] = []
    client = _ScriptedClient(raises=ConnectionError("broker down"))
    limiter = RedisDomainRateLimiter(
        rate_per_second=1.0,
        redis_client=client,
        clock=lambda: 0.0,
        sleep=sleeps.append,
    )
    assert limiter.acquire("https://github.com/a") == 0.0
    assert sleeps == []


# ---------------------------------------------------------------------------
# build_limiter_from_settings — the Django-bridge factory
# ---------------------------------------------------------------------------
@override_settings(AGENT_RATE_LIMIT_RPS_PER_DOMAIN=0)
def test_factory_returns_noop_when_rate_is_zero() -> None:
    assert isinstance(build_limiter_from_settings(), NoopRateLimiter)


@override_settings(AGENT_RATE_LIMIT_RPS_PER_DOMAIN=1.0)
def test_factory_falls_back_to_in_memory_when_redis_unreachable(monkeypatch) -> None:
    """Boot must succeed even if Redis is down at startup."""
    def _fail(_alias):
        raise ConnectionError("redis offline")

    monkeypatch.setattr(
        "django_redis.get_redis_connection", _fail, raising=False
    )
    # Force the import path inside build_limiter_from_settings to use
    # our mock. The function imports `from django_redis import
    # get_redis_connection` at call-time.
    import django_redis
    monkeypatch.setattr(django_redis, "get_redis_connection", _fail)

    limiter = build_limiter_from_settings()
    assert isinstance(limiter, InMemoryDomainRateLimiter)


@override_settings(AGENT_RATE_LIMIT_RPS_PER_DOMAIN=2.5)
def test_factory_returns_redis_limiter_when_ping_ok(monkeypatch) -> None:
    fake_client = MagicMock()
    fake_client.ping.return_value = True
    fake_client.register_script.return_value = MagicMock()

    import django_redis
    monkeypatch.setattr(django_redis, "get_redis_connection", lambda _a: fake_client)

    limiter = build_limiter_from_settings()
    assert isinstance(limiter, RedisDomainRateLimiter)
    assert limiter.rate == 2.5
    fake_client.ping.assert_called_once()
