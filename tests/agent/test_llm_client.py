"""LLMProvider concrete-client tests.

These tests do not call external APIs. OpenAIProvider is driven with a
fake SDK client that mirrors the small surface we use:
``client.chat.completions.parse(...)`` returning a parsed Pydantic model.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.agent.llm.client import (
    LLMProviderError,
    LLMProviderRefusal,
    _OpenAIEnrichedDraft,
    OpenAIProvider,
    _OpenAIMergeSet,
    _estimate_cost_usd,
    _extract_cached_tokens,
    _safe_provider_error,
    build_provider,
)
from apps.agent.llm.schemas import EnrichedDraft, MergeSet


class FakeOpenAICompletions:
    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _fake_client(completions: FakeOpenAICompletions):
    return SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )


def _completion(*, parsed=None, refusal=None, usage=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(parsed=parsed, refusal=refusal)
            )
        ],
        usage=usage,
    )


def _usage(
    *,
    prompt_tokens: int = 1000,
    completion_tokens: int = 500,
    cached_tokens: int = 120,
):
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
    )


def _openai_merge(**overrides) -> _OpenAIMergeSet:
    payload = {
        "short_description": None,
        "long_description": None,
        "developer_name": None,
        "developer_url": None,
        "official_page_url": None,
        "install_url": None,
        "repo_url": None,
        "add_listing_types": [],
        "add_categories": [],
        "add_use_cases": [],
        "capabilities": [],
        "proposed_verdict": "",
        "proposed_launch_status": None,
        "proposed_pricing_model": None,
        "proposed_scope_summary": "",
        "rationale": "",
    }
    payload.update(overrides)
    return _OpenAIMergeSet(**payload)


def _openai_enriched(**overrides) -> _OpenAIEnrichedDraft:
    payload = {
        "name": "Acme MCP",
        "short_description": "Acme MCP server",
        "long_description": "",
        "developer_name": "Acme",
        "developer_url": "https://example.com",
        "official_page_url": "https://example.com/acme",
        "install_url": "",
        "repo_url": "https://github.com/acme/acme-mcp",
        "listing_types": [{"slug": "mcp-server", "confidence": 0.95}],
        "categories": [{"slug": "developer-tools", "confidence": 0.9}],
        "capabilities": [],
        "use_cases": ["connect Acme"],
        "launch_status": "live",
        "pricing_model": "unknown",
        "proposed_verdict": "Useful for Acme users.",
        "scope_summary": "Reads Acme data.",
    }
    payload.update(overrides)
    return _OpenAIEnrichedDraft(**payload)


def test_openai_provider_returns_parsed_schema_and_metadata() -> None:
    parsed = _openai_merge(short_description="Filled by OpenAI")
    completions = FakeOpenAICompletions(
        _completion(parsed=parsed, usage=_usage())
    )
    provider = OpenAIProvider(
        model="test-model",
        api_key="test-key",
        client=_fake_client(completions),
        input_cost_per_1m_tokens=2.5,
        cached_input_cost_per_1m_tokens=0.25,
        output_cost_per_1m_tokens=10.0,
    )

    response = provider.complete(
        system="system prompt",
        messages=[{"role": "user", "content": "body"}],
        schema=MergeSet,
        prompt_version="enrich-existing-v1.0",
    )

    assert isinstance(response.data, MergeSet)
    assert response.data.short_description == "Filled by OpenAI"
    assert response.meta.provider == "openai"
    assert response.meta.model == "test-model"
    assert response.meta.prompt_version == "enrich-existing-v1.0"
    assert response.meta.input_tokens == 1000
    assert response.meta.output_tokens == 500
    assert response.meta.cached_tokens == 120
    # Billable input = 1000 - 120 = 880 tokens × $2.50 / 1M  = $0.00220
    # Cached input  = 120 tokens × $0.25 / 1M                = $0.00003
    # Output        = 500 tokens × $10.00 / 1M               = $0.00500
    # Total                                                  = $0.00723
    assert response.meta.cost_usd == pytest.approx(0.00723)
    assert response.meta.is_mock is False

    call = completions.calls[0]
    assert call["model"] == "test-model"
    assert call["response_format"] is _OpenAIMergeSet
    assert call["messages"][0] == {"role": "system", "content": "system prompt"}
    assert call["messages"][1] == {"role": "user", "content": "body"}


def test_openai_provider_raises_on_refusal() -> None:
    completions = FakeOpenAICompletions(
        _completion(parsed=None, refusal="policy refusal", usage=_usage())
    )
    provider = OpenAIProvider(
        model="test-model",
        api_key="test-key",
        client=_fake_client(completions),
    )

    with pytest.raises(LLMProviderRefusal):
        provider.complete(
            system="system",
            messages=[{"role": "user", "content": "body"}],
            schema=MergeSet,
        )


def test_openai_provider_raises_when_parsed_payload_missing() -> None:
    completions = FakeOpenAICompletions(
        _completion(parsed=None, refusal=None, usage=_usage())
    )
    provider = OpenAIProvider(
        model="test-model",
        api_key="test-key",
        client=_fake_client(completions),
    )

    with pytest.raises(LLMProviderError):
        provider.complete(
            system="system",
            messages=[{"role": "user", "content": "body"}],
            schema=MergeSet,
        )


def test_openai_provider_retries_retryable_errors(monkeypatch) -> None:
    class RateLimitError(RuntimeError):
        status_code = 429

    completions = FakeOpenAICompletions(
        RateLimitError("slow down"),
        _completion(parsed=_openai_merge(short_description="After retry"), usage=_usage()),
    )
    provider = OpenAIProvider(
        model="test-model",
        api_key="test-key",
        client=_fake_client(completions),
        max_retries=2,
    )
    monkeypatch.setattr("apps.agent.llm.client.time.sleep", lambda seconds: None)

    response = provider.complete(
        system="system",
        messages=[{"role": "user", "content": "body"}],
        schema=MergeSet,
    )

    assert response.data.short_description == "After retry"
    assert len(completions.calls) == 2


def test_openai_wire_schema_converts_capability_list_to_internal_dict() -> None:
    completions = FakeOpenAICompletions(
        _completion(
            parsed=_openai_merge(
                capabilities=[
                    {
                        "key": "open_source",
                        "value": "yes",
                        "evidence": "GitHub repository",
                        "confidence": 0.9,
                    }
                ]
            ),
            usage=_usage(),
        )
    )
    provider = OpenAIProvider(
        model="test-model",
        api_key="test-key",
        client=_fake_client(completions),
    )

    response = provider.complete(
        system="system",
        messages=[{"role": "user", "content": "body"}],
        schema=MergeSet,
    )

    assert response.data.capabilities["open_source"].value == "yes"
    assert response.data.capabilities["open_source"].evidence == "GitHub repository"


def test_openai_wire_schema_converts_enriched_draft_capability_list() -> None:
    completions = FakeOpenAICompletions(
        _completion(
            parsed=_openai_enriched(
                capabilities=[
                    {
                        "key": "open_source",
                        "value": "yes",
                        "evidence": "Public GitHub repository",
                        "confidence": 0.91,
                    }
                ]
            ),
            usage=_usage(),
        )
    )
    provider = OpenAIProvider(
        model="test-model",
        api_key="test-key",
        client=_fake_client(completions),
    )

    response = provider.complete(
        system="system",
        messages=[{"role": "user", "content": "body"}],
        schema=EnrichedDraft,
    )

    assert isinstance(response.data, EnrichedDraft)
    assert response.data.capabilities["open_source"].value == "yes"
    assert completions.calls[0]["response_format"] is _OpenAIEnrichedDraft


def _settings_with_prices(**overrides) -> SimpleNamespace:
    """Build a settings stub that already includes the per-role cost knobs.

    Tests that don't care about prices can pass overrides to set the
    provider/model bits they need; the cost fields default to 0.
    """
    base = {
        "AGENT_LLM_PROVIDER_PRIMARY": "mock",
        "AGENT_LLM_MODEL_PRIMARY": "",
        "AGENT_LLM_PROVIDER_CHEAP": "mock",
        "AGENT_LLM_MODEL_CHEAP": "",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "AGENT_OPENAI_PRIMARY_INPUT_COST_PER_1M_TOKENS": 0,
        "AGENT_OPENAI_PRIMARY_CACHED_COST_PER_1M_TOKENS": 0,
        "AGENT_OPENAI_PRIMARY_OUTPUT_COST_PER_1M_TOKENS": 0,
        "AGENT_OPENAI_CHEAP_INPUT_COST_PER_1M_TOKENS": 0,
        "AGENT_OPENAI_CHEAP_CACHED_COST_PER_1M_TOKENS": 0,
        "AGENT_OPENAI_CHEAP_OUTPUT_COST_PER_1M_TOKENS": 0,
        "AGENT_OPENAI_TIMEOUT_SECONDS": 90.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_provider_requires_model_for_real_provider() -> None:
    settings = _settings_with_prices(
        AGENT_LLM_PROVIDER_PRIMARY="openai",
        AGENT_LLM_MODEL_PRIMARY="",
        OPENAI_API_KEY="test-key",
    )

    with pytest.raises(ImproperlyConfigured, match="AGENT_LLM_MODEL"):
        build_provider("primary", settings_module=settings)


def test_build_provider_uses_role_specific_openai_prices() -> None:
    """Primary and cheap roles must read from their own price keys so
    a gpt-mini primary and a gpt-nano cheap don't share one price pair.
    Regression: F2 — single global cost vars mis-billed cheap calls."""
    settings = _settings_with_prices(
        AGENT_LLM_PROVIDER_PRIMARY="openai",
        AGENT_LLM_MODEL_PRIMARY="gpt-mini",
        AGENT_LLM_PROVIDER_CHEAP="openai",
        AGENT_LLM_MODEL_CHEAP="gpt-nano",
        OPENAI_API_KEY="test-key",
        AGENT_OPENAI_PRIMARY_INPUT_COST_PER_1M_TOKENS=0.75,
        AGENT_OPENAI_PRIMARY_CACHED_COST_PER_1M_TOKENS=0.075,
        AGENT_OPENAI_PRIMARY_OUTPUT_COST_PER_1M_TOKENS=4.50,
        AGENT_OPENAI_CHEAP_INPUT_COST_PER_1M_TOKENS=0.20,
        AGENT_OPENAI_CHEAP_CACHED_COST_PER_1M_TOKENS=0.02,
        AGENT_OPENAI_CHEAP_OUTPUT_COST_PER_1M_TOKENS=1.25,
    )

    primary = build_provider("primary", settings_module=settings)
    cheap = build_provider("cheap", settings_module=settings)

    assert isinstance(primary, OpenAIProvider)
    assert isinstance(cheap, OpenAIProvider)
    assert primary.model == "gpt-mini"
    assert primary.input_cost_per_1m_tokens == 0.75
    assert primary.cached_input_cost_per_1m_tokens == 0.075
    assert primary.output_cost_per_1m_tokens == 4.50
    assert cheap.model == "gpt-nano"
    assert cheap.input_cost_per_1m_tokens == 0.20
    assert cheap.cached_input_cost_per_1m_tokens == 0.02
    assert cheap.output_cost_per_1m_tokens == 1.25


def test_build_provider_uses_openai_timeout_setting() -> None:
    settings = _settings_with_prices(
        AGENT_LLM_PROVIDER_PRIMARY="openai",
        AGENT_LLM_MODEL_PRIMARY="gpt-mini",
        OPENAI_API_KEY="test-key",
        AGENT_OPENAI_TIMEOUT_SECONDS=45.0,
    )

    provider = build_provider("primary", settings_module=settings)

    assert isinstance(provider, OpenAIProvider)
    assert provider.timeout == 45.0


def test_cost_helpers_tolerate_missing_usage_details() -> None:
    assert _extract_cached_tokens(None) == 0
    assert _extract_cached_tokens(SimpleNamespace(prompt_tokens_details={})) == 0
    # Backwards-compat: callers that don't supply cached_tokens get the
    # old "all input is non-cached" behavior.
    assert _estimate_cost_usd(
        input_tokens=1000,
        output_tokens=1000,
        input_cost_per_1m_tokens=1.0,
        output_cost_per_1m_tokens=2.0,
    ) == pytest.approx(0.003)


def test_estimate_cost_usd_discounts_cached_input() -> None:
    """Cached tokens are subtracted from billable input and billed at
    the (much cheaper) cached-input price. Without this, prompt-cache
    hits would inflate cost_usd by ~10x for cached-heavy workloads."""
    cost = _estimate_cost_usd(
        input_tokens=10_000,
        output_tokens=2_000,
        cached_tokens=8_000,
        input_cost_per_1m_tokens=0.75,
        output_cost_per_1m_tokens=4.50,
        cached_input_cost_per_1m_tokens=0.075,
    )
    # billable input = 2000 × 0.75 / 1M    = $0.0015
    # cached         = 8000 × 0.075 / 1M   = $0.0006
    # output         = 2000 × 4.50 / 1M    = $0.0090
    # total                                = $0.0111
    assert cost == pytest.approx(0.0111)


def test_estimate_cost_usd_clamps_cached_above_input() -> None:
    """Defensive: if a provider over-reports cached_tokens (> input_tokens),
    we clamp instead of producing a negative billable-input bill."""
    cost = _estimate_cost_usd(
        input_tokens=1_000,
        output_tokens=0,
        cached_tokens=99_999,
        input_cost_per_1m_tokens=10.0,
        output_cost_per_1m_tokens=0.0,
        cached_input_cost_per_1m_tokens=1.0,
    )
    # cached clamped to 1000 → billable input = 0; cost = 1000 × 1.0/1M = $0.001
    assert cost == pytest.approx(0.001)


def test_safe_provider_error_does_not_include_exception_message() -> None:
    class AuthenticationError(RuntimeError):
        status_code = 401
        code = "invalid_api_key"

    exc = AuthenticationError("Incorrect API key provided: sk-secret")
    safe = _safe_provider_error(exc)

    assert "AuthenticationError" in safe
    assert "status=401" in safe
    assert "invalid_api_key" in safe
    assert "sk-secret" not in safe
    assert "Incorrect API key" not in safe
