"""Core healthcheck regressions."""
from __future__ import annotations

import pytest

from apps.core import healthcheck


class FakeCursor:
    def __init__(self, row):
        self.row = row
        self.sql = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql):
        self.sql = sql

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def test_pg_trgm_healthcheck_uses_similarity_function(monkeypatch) -> None:
    cursor = FakeCursor((1.0,))
    monkeypatch.setattr(healthcheck, "connection", FakeConnection(cursor))

    assert healthcheck._check_pg_trgm() is True
    assert "similarity(" in cursor.sql
    assert "%%" not in cursor.sql


def test_pg_trgm_healthcheck_fails_when_similarity_is_unavailable(monkeypatch) -> None:
    class BrokenCursor(FakeCursor):
        def execute(self, sql):
            raise RuntimeError("missing extension")

    monkeypatch.setattr(healthcheck, "connection", FakeConnection(BrokenCursor(None)))

    assert healthcheck._check_pg_trgm() is False


class _FakeCeleryControl:
    def __init__(self, replies):
        self._replies = replies
        self.last_timeout = None

    def ping(self, timeout=None):
        self.last_timeout = timeout
        return self._replies


class _FakeCeleryApp:
    def __init__(self, control):
        self.control = control


def _install_fake_celery(monkeypatch, replies):
    import sys
    import types

    control = _FakeCeleryControl(replies)
    fake_module = types.ModuleType("config.celery")
    fake_module.app = _FakeCeleryApp(control)
    monkeypatch.setitem(sys.modules, "config.celery", fake_module)
    return control


def test_celery_healthcheck_ok_when_worker_pings(monkeypatch) -> None:
    control = _install_fake_celery(
        monkeypatch, [{"celery@worker": {"ok": "pong"}}]
    )
    assert healthcheck._check_celery_worker() is True
    assert control.last_timeout == healthcheck._CELERY_PING_TIMEOUT_SECONDS


def test_celery_healthcheck_fail_when_no_worker_responds(monkeypatch) -> None:
    _install_fake_celery(monkeypatch, [])
    assert healthcheck._check_celery_worker() is False


def test_celery_healthcheck_fail_when_broker_unreachable(monkeypatch) -> None:
    import sys
    import types

    class _ExplodingControl:
        def ping(self, timeout=None):
            raise ConnectionError("broker unreachable")

    fake_module = types.ModuleType("config.celery")
    fake_module.app = _FakeCeleryApp(_ExplodingControl())
    monkeypatch.setitem(sys.modules, "config.celery", fake_module)

    assert healthcheck._check_celery_worker() is False


@pytest.mark.django_db
def test_view_returns_503_when_celery_down(monkeypatch, client) -> None:
    monkeypatch.setattr(healthcheck, "_check_db", lambda: True)
    monkeypatch.setattr(healthcheck, "_check_redis", lambda: True)
    monkeypatch.setattr(healthcheck, "_check_pg_trgm", lambda: True)
    monkeypatch.setattr(healthcheck, "_check_celery_worker", lambda: False)

    response = client.get("/health/")
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "fail"
    assert payload["checks"]["celery_worker"] is False
