"""Tests for the provider registry in ``apps.agent.llm.client``."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.agent.llm.client import (
    LLMProvider,
    LLMResponse,
    LLMCallMetadata,
    MockLLMProvider,
    _PROVIDER_REGISTRY,
    build_provider,
    register_provider,
)


@pytest.fixture
def _isolated_registry():
    """Restore the registry after each test that mutates it."""
    snapshot = dict(_PROVIDER_REGISTRY)
    yield
    _PROVIDER_REGISTRY.clear()
    _PROVIDER_REGISTRY.update(snapshot)


def _settings(provider="mock", model="m"):
    return SimpleNamespace(
        AGENT_LLM_PROVIDER_PRIMARY=provider,
        AGENT_LLM_PROVIDER_CHEAP=provider,
        AGENT_LLM_MODEL_PRIMARY=model,
        AGENT_LLM_MODEL_CHEAP=model,
        OPENAI_API_KEY="sk-x",
        ANTHROPIC_API_KEY="sk-a",
    )


def test_unknown_provider_raises_improperly_configured() -> None:
    settings = _settings(provider="bedrock")
    with pytest.raises(ImproperlyConfigured) as excinfo:
        build_provider("primary", settings_module=settings)
    assert "Unknown LLM provider" in str(excinfo.value)
    # Error message lists known keys so operators can spot a typo.
    assert "openai" in str(excinfo.value)


def test_unknown_role_raises_value_error() -> None:
    settings = _settings()
    with pytest.raises(ValueError):
        build_provider("strange", settings_module=settings)


def test_register_provider_adds_new_key(_isolated_registry) -> None:
    class _FakeProvider(LLMProvider):
        name = "bedrock"

        def __init__(self, *, model: str):
            self.model = model

        def complete(self, **kwargs):  # pragma: no cover - never invoked
            raise NotImplementedError

    called_with: dict = {}

    def _build(settings, role, model, cost_prefix):
        called_with.update(
            {"role": role, "model": model, "cost_prefix": cost_prefix}
        )
        return _FakeProvider(model=model or "default")

    register_provider("bedrock", _build)

    settings = _settings(provider="bedrock", model="anthropic.claude-3-sonnet")
    provider = build_provider("primary", settings_module=settings)

    assert isinstance(provider, _FakeProvider)
    assert provider.model == "anthropic.claude-3-sonnet"
    assert called_with == {
        "role": "primary",
        "model": "anthropic.claude-3-sonnet",
        "cost_prefix": "AGENT_OPENAI_PRIMARY",
    }


def test_register_provider_is_case_insensitive(_isolated_registry) -> None:
    register_provider("BEDROCK", lambda *a, **k: MockLLMProvider(model="x"))
    settings = _settings(provider="bedrock", model="x")
    provider = build_provider("primary", settings_module=settings)
    assert isinstance(provider, MockLLMProvider)


def test_re_registering_replaces_builder(_isolated_registry) -> None:
    first = lambda *a, **k: MockLLMProvider(model="first")
    second = lambda *a, **k: MockLLMProvider(model="second")
    register_provider("custom", first)
    register_provider("custom", second)

    settings = _settings(provider="custom", model="ignored")
    provider = build_provider("primary", settings_module=settings)
    assert provider.model == "second"


def test_empty_provider_key_defaults_to_mock() -> None:
    """Empty string in env (AGENT_LLM_PROVIDER_PRIMARY=) falls back to mock,
    matching the pre-registry behaviour so dev/CI boots cleanly."""
    settings = _settings(provider="", model="")
    provider = build_provider("primary", settings_module=settings)
    assert isinstance(provider, MockLLMProvider)
