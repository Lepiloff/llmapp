"""Regression: MCP Registry fetch failures surface in Sentry.

Without this, a persistent 404 (or transport outage) on
registry.modelcontextprotocol.io would silently zero out the ingest
counters every day and operators wouldn't notice.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest
import requests

from apps.sources.mcp_registry import MCPRegistrySource, _report_to_sentry


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


def test_report_to_sentry_captures_with_stable_fingerprint(monkeypatch) -> None:
    fake_sdk = MagicMock()
    fake_sdk.new_scope.return_value.__enter__ = lambda self: fake_sdk._scope
    fake_sdk.new_scope.return_value.__exit__ = lambda self, *_: False
    fake_sdk._scope = MagicMock()
    fake_sdk._scope.fingerprint = None

    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)

    exc = requests.HTTPError("404 not found")
    exc.response = _FakeResponse(404)

    _report_to_sentry(exc, base_url="https://registry.example/v1", cursor=None)

    # Fingerprint includes the constant key + the status, so all 404s
    # group into one Sentry issue across daily retries.
    assert fake_sdk._scope.fingerprint == ["mcp-registry-unreachable", "404"]
    fake_sdk._scope.set_tag.assert_any_call("component", "mcp_registry_ingest")
    fake_sdk._scope.set_tag.assert_any_call("http_status", "404")
    fake_sdk.capture_message.assert_called_once()
    args, kwargs = fake_sdk.capture_message.call_args
    assert "404" in args[0]
    assert kwargs == {"level": "warning"}


def test_report_to_sentry_is_safe_without_sdk(monkeypatch) -> None:
    """No sentry_sdk in venv (e.g. minimal CI) → silent no-op."""
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)
    # Force import to fail by removing the module entirely.
    monkeypatch.delitem(sys.modules, "sentry_sdk", raising=False)

    # Simulate ImportError on `import sentry_sdk`:
    import builtins
    real_import = builtins.__import__

    def _no_sentry(name, *args, **kwargs):
        if name == "sentry_sdk":
            raise ImportError
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_sentry)
    # Should not raise — function is a no-op when sdk missing.
    _report_to_sentry(
        requests.ConnectionError("network down"),
        base_url="https://registry.example/v1",
        cursor="abc",
    )


def test_iter_drafts_reports_404_to_sentry(monkeypatch) -> None:
    """End-to-end: an HTTP failure inside iter_drafts triggers sentry capture."""
    fake_sdk = MagicMock()
    fake_sdk.new_scope.return_value.__enter__ = lambda self: fake_sdk._scope
    fake_sdk.new_scope.return_value.__exit__ = lambda self, *_: False
    fake_sdk._scope = MagicMock()

    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)

    source = MCPRegistrySource()

    def _exploding_get(*args, **kwargs):
        exc = requests.HTTPError("404 not found")
        exc.response = _FakeResponse(404)
        raise exc

    source.http.get = _exploding_get  # type: ignore[attr-defined]

    drafts = list(source.iter_drafts())
    assert drafts == []
    fake_sdk.capture_message.assert_called_once()
    args, _ = fake_sdk.capture_message.call_args
    assert "MCP Registry fetch failed" in args[0]
