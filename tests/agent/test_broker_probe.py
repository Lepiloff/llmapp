"""Tests for the host-venv broker auto-fallback.

The probe behavior to pin:

* ``broker_host_port`` parses common redis URL shapes and rejects
  ones we can't probe.
* ``is_broker_reachable`` returns False on DNS failure / connection
  refused without crashing.
* ``ensure_eager_if_broker_unreachable`` flips Celery to eager mode
  and writes a warning when the probe fails, AND is a no-op when the
  broker is reachable or eager is already on.
"""
from __future__ import annotations

import socket
from io import StringIO
from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.agent.management._broker_probe import (
    broker_host_port,
    ensure_eager_if_broker_unreachable,
    is_broker_reachable,
)


def test_broker_host_port_parses_redis_url() -> None:
    assert broker_host_port("redis://redis:6379/0") == ("redis", 6379)
    assert broker_host_port("redis://localhost:6380/1") == ("localhost", 6380)
    # Default port when omitted.
    assert broker_host_port("redis://redis/0") == ("redis", 6379)
    assert broker_host_port("rediss://secured-host/2") == ("secured-host", 6380)


def test_broker_host_port_rejects_unknown_shapes() -> None:
    assert broker_host_port("amqp://rabbit:5672//") is None
    assert broker_host_port("not-a-url") is None
    assert broker_host_port("redis:///0") is None  # no host


@override_settings(CELERY_BROKER_URL="redis://nonexistent-host-xyz123:6379/0")
def test_is_broker_reachable_returns_false_on_dns_failure() -> None:
    # A guaranteed-bogus hostname — DNS lookup must fail fast.
    assert is_broker_reachable(timeout=0.5) is False


@override_settings(CELERY_BROKER_URL="redis://127.0.0.1:1/0")
def test_is_broker_reachable_returns_false_on_connection_refused() -> None:
    # Port 1 is reserved; nothing listens. socket.connect raises
    # ConnectionRefusedError which OSError catches.
    assert is_broker_reachable(timeout=0.5) is False


@override_settings(
    CELERY_BROKER_URL="redis://anywhere/0",
    CELERY_TASK_ALWAYS_EAGER=False,
)
def test_ensure_eager_writes_warning_and_flips_when_unreachable() -> None:
    from django.conf import settings as django_settings
    err = StringIO()

    with patch(
        "apps.agent.management._broker_probe.is_broker_reachable",
        return_value=False,
    ):
        switched = ensure_eager_if_broker_unreachable(err)

    assert switched is True
    assert django_settings.CELERY_TASK_ALWAYS_EAGER is True
    assert "auto-enabling" in err.getvalue()
    assert "docker compose exec" in err.getvalue()


@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_ensure_eager_no_op_when_broker_reachable() -> None:
    from django.conf import settings as django_settings
    err = StringIO()

    with patch(
        "apps.agent.management._broker_probe.is_broker_reachable",
        return_value=True,
    ):
        switched = ensure_eager_if_broker_unreachable(err)

    assert switched is False
    assert django_settings.CELERY_TASK_ALWAYS_EAGER is False
    assert err.getvalue() == ""


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
def test_ensure_eager_no_op_when_eager_already_on() -> None:
    err = StringIO()

    # Even with broker unreachable, we don't re-warn — eager is the
    # current mode, nothing to change.
    with patch(
        "apps.agent.management._broker_probe.is_broker_reachable",
        return_value=False,
    ):
        switched = ensure_eager_if_broker_unreachable(err)

    assert switched is False
    assert err.getvalue() == ""
