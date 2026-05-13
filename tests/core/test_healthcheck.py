"""Core healthcheck regressions."""
from __future__ import annotations

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
