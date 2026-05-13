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
    assert response.meta.cost_usd == pytest.approx(0.0075)
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


def test_build_provider_requires_model_for_real_provider() -> None:
    settings = SimpleNamespace(
        AGENT_LLM_PROVIDER_PRIMARY="openai",
        AGENT_LLM_MODEL_PRIMARY="",
        AGENT_LLM_PROVIDER_CHEAP="mock",
        AGENT_LLM_MODEL_CHEAP="",
        OPENAI_API_KEY="test-key",
        ANTHROPIC_API_KEY="",
        AGENT_OPENAI_INPUT_COST_PER_1M_TOKENS=0,
        AGENT_OPENAI_OUTPUT_COST_PER_1M_TOKENS=0,
    )

    with pytest.raises(ImproperlyConfigured, match="AGENT_LLM_MODEL"):
        build_provider("primary", settings_module=settings)


def test_cost_helpers_tolerate_missing_usage_details() -> None:
    assert _extract_cached_tokens(None) == 0
    assert _extract_cached_tokens(SimpleNamespace(prompt_tokens_details={})) == 0
    assert _estimate_cost_usd(
        input_tokens=1000,
        output_tokens=1000,
        input_cost_per_1m_tokens=1.0,
        output_cost_per_1m_tokens=2.0,
    ) == pytest.approx(0.003)


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
