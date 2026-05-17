"""Production settings hardening regressions."""
from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture
def _restore_prod_module():
    """Make sure each test re-imports config.settings.prod cleanly."""
    saved = sys.modules.pop("config.settings.prod", None)
    yield
    sys.modules.pop("config.settings.prod", None)
    if saved is not None:
        sys.modules["config.settings.prod"] = saved


def _import_prod():
    return importlib.import_module("config.settings.prod")


def test_prod_refuses_empty_secret_key(monkeypatch, _restore_prod_module):
    """`SECRET_KEY=` (empty string in env) must not boot prod."""
    from django.core.exceptions import ImproperlyConfigured

    monkeypatch.setenv("SECRET_KEY", "")
    monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "")

    with pytest.raises(ImproperlyConfigured) as excinfo:
        _import_prod()
    assert "missing" in str(excinfo.value).lower() or "insecure" in str(excinfo.value).lower()


def test_prod_refuses_insecure_default_secret_key(monkeypatch, _restore_prod_module):
    from django.core.exceptions import ImproperlyConfigured

    monkeypatch.setenv("SECRET_KEY", "insecure-dev-key-change-me")
    monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "")

    with pytest.raises(ImproperlyConfigured) as excinfo:
        _import_prod()
    assert "insecure" in str(excinfo.value).lower()


def test_prod_boots_with_real_secret_key(monkeypatch, _restore_prod_module):
    monkeypatch.setenv("SECRET_KEY", "x" * 64)
    monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://example.com")

    module = _import_prod()
    assert module.SECRET_KEY == "x" * 64
    assert module.DEBUG is False
