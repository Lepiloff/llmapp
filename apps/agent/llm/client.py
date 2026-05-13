"""LLM provider abstraction.

The pipeline never instantiates a concrete provider directly. It calls
``llm.complete(...)`` against the abstract ``LLMProvider`` interface,
and the caller (typically the Django bridge in ``apps.agent.tasks``)
decides which implementation to pass in based on env config.

This split exists because:

1. **Testing.** Phase 1 must be fully exercisable without network
   access. ``MockLLMProvider`` returns canned ``MergeSet`` / ``EnrichedDraft``
   fixtures the pipeline can validate against.
2. **Vendor neutrality.** Anthropic prompt-caching and OpenAI JSON
   schema response_format are different wire protocols; the pipeline
   should not care.
3. **Extraction.** When the agent moves to its own service, providers
   migrate together with the abstract base. Switching SDKs is a single
   subclass change.

Concrete Anthropic/OpenAI providers are deliberately NOT implemented
in this file yet — they're Phase 1b once the mock-driven tests are
green. Their stubs are present so factory wiring works end-to-end
without API keys.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from pydantic import BaseModel

from apps.agent.pipeline.taxonomy import TaxonomySnapshot

SchemaT = TypeVar("SchemaT", bound=BaseModel)


# ---------------------------------------------------------------------------
# Call-level metadata — the provider returns the parsed model plus this.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LLMCallMetadata:
    """Per-call accounting. Persisted to LLMCallLog by the Django bridge."""

    provider: str
    model: str
    prompt_version: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    is_mock: bool = False


@dataclass
class LLMResponse:
    """Bundle of parsed Pydantic output + the call metadata."""

    data: BaseModel
    meta: LLMCallMetadata


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------
class LLMProvider(ABC):
    """A provider returns structured output for ``schema`` on every call.

    Implementations are responsible for:
      * Wire-protocol differences (Anthropic tool-use, OpenAI JSON schema
        response_format, etc.) — invisible to callers.
      * Retry with exponential backoff on 429 / 5xx.
      * Computing ``LLMCallMetadata.cost_usd`` from token counts at
        request time (so changes in provider pricing are picked up
        without code redeploy — read pricing from env or per-instance
        config).
    """

    name: str = "abstract"

    @abstractmethod
    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        schema: type[SchemaT],
        taxonomy: TaxonomySnapshot | None = None,
        prompt_version: str = "",
    ) -> LLMResponse:
        """Return ``LLMResponse(data=<schema instance>, meta=<accounting>)``.

        ``taxonomy`` is informational for providers that want to embed
        allowed slugs as constraints (e.g. tool-use enum fields). The
        mock provider uses it to assert callers passed it (a common bug
        that would make slugs hallucinate).
        """


# ---------------------------------------------------------------------------
# Stubs for real providers — wired up in Phase 1b
# ---------------------------------------------------------------------------
class AnthropicProvider(LLMProvider):
    """Anthropic Claude via the official SDK. **Not implemented yet.**

    Kept here so ``apps.agent.llm.client.build_provider`` can dispatch
    on ``AGENT_LLM_PROVIDER_PRIMARY`` and surface a clear error
    message until the real wire path lands.
    """

    name = "anthropic"

    def __init__(self, *, model: str, api_key: str) -> None:
        self.model = model
        self.api_key = api_key

    def complete(self, **kwargs) -> LLMResponse:  # pragma: no cover - placeholder
        raise NotImplementedError(
            "AnthropicProvider is a Phase 1b deliverable. "
            "Set AGENT_LLM_PROVIDER_PRIMARY=mock for the Phase 1 mock-only path."
        )


class OpenAIProvider(LLMProvider):
    """OpenAI via the official SDK. **Not implemented yet.** See AnthropicProvider."""

    name = "openai"

    def __init__(self, *, model: str, api_key: str) -> None:
        self.model = model
        self.api_key = api_key

    def complete(self, **kwargs) -> LLMResponse:  # pragma: no cover - placeholder
        raise NotImplementedError(
            "OpenAIProvider is a Phase 1b deliverable. "
            "Set AGENT_LLM_PROVIDER_PRIMARY=mock for the Phase 1 mock-only path."
        )


# ---------------------------------------------------------------------------
# MockLLMProvider — drives all Phase 1 tests
# ---------------------------------------------------------------------------
MockResponder = Callable[[dict], BaseModel]


@dataclass
class MockLLMProvider(LLMProvider):
    """Test double. Returns a caller-supplied response for each call.

    Use the ``responder`` argument when the test wants to vary the
    response by inputs; use ``responses_queue`` for fixed-sequence
    tests where order matters and you don't want to re-implement the
    decision logic.

    Tokens / cost / latency default to zero so unit tests don't fight
    against accounting noise. The Django bridge writes ``is_mock=True``
    into ``LLMCallLog`` so monthly cost dashboards can exclude them.
    """

    name: str = "mock"
    model: str = "mock-model"
    responder: MockResponder | None = None
    responses_queue: list[BaseModel] = field(default_factory=list)
    call_log: list[dict] = field(default_factory=list)

    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        schema: type[SchemaT],
        taxonomy: TaxonomySnapshot | None = None,
        prompt_version: str = "",
    ) -> LLMResponse:
        # Record the call so tests can assert on what was sent.
        self.call_log.append(
            {
                "system": system,
                "messages": messages,
                "schema": schema.__name__,
                "taxonomy_present": taxonomy is not None,
                "prompt_version": prompt_version,
            }
        )

        if self.responder is not None:
            data = self.responder(
                {"system": system, "messages": messages, "schema": schema}
            )
        elif self.responses_queue:
            data = self.responses_queue.pop(0)
        else:
            raise RuntimeError(
                "MockLLMProvider has no responder and no queued responses. "
                "Configure one before invoking the pipeline."
            )

        if not isinstance(data, schema):
            raise TypeError(
                f"MockLLMProvider returned {type(data).__name__}, "
                f"expected {schema.__name__}. "
                "Mock fixtures must match the schema the caller requested."
            )

        meta = LLMCallMetadata(
            provider=self.name,
            model=self.model,
            prompt_version=prompt_version,
            is_mock=True,
            latency_ms=0,
        )
        return LLMResponse(data=data, meta=meta)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def build_provider(
    role: str,
    *,
    settings_module=None,
) -> LLMProvider:
    """Construct the configured provider for ``role`` ∈ {"primary", "cheap"}.

    Pulls config from Django settings lazily so the pure-Python pipeline
    stays importable without django.setup() — callers in the Django
    bridge invoke this; tests pass ``MockLLMProvider`` directly.

    Raises ``ImproperlyConfigured`` if a real provider is requested but
    its API key is empty — much better than a 401 surface from inside
    a worker task at runtime.
    """
    from django.conf import settings as django_settings
    from django.core.exceptions import ImproperlyConfigured

    settings = settings_module or django_settings

    if role == "primary":
        provider_key = settings.AGENT_LLM_PROVIDER_PRIMARY
        model = settings.AGENT_LLM_MODEL_PRIMARY
    elif role == "cheap":
        provider_key = settings.AGENT_LLM_PROVIDER_CHEAP
        model = settings.AGENT_LLM_MODEL_CHEAP
    else:
        raise ValueError(f"Unknown LLM role: {role!r}")

    provider_key = (provider_key or "mock").lower()

    if provider_key == "mock":
        return MockLLMProvider(model=model or "mock-model")

    if provider_key == "anthropic":
        if not settings.ANTHROPIC_API_KEY:
            raise ImproperlyConfigured(
                "AGENT_LLM_PROVIDER_*=anthropic requires ANTHROPIC_API_KEY."
            )
        return AnthropicProvider(model=model, api_key=settings.ANTHROPIC_API_KEY)

    if provider_key == "openai":
        if not settings.OPENAI_API_KEY:
            raise ImproperlyConfigured(
                "AGENT_LLM_PROVIDER_*=openai requires OPENAI_API_KEY."
            )
        return OpenAIProvider(model=model, api_key=settings.OPENAI_API_KEY)

    raise ImproperlyConfigured(f"Unknown LLM provider key: {provider_key!r}")


# ---------------------------------------------------------------------------
# Timer helper for real providers (Phase 1b will use this)
# ---------------------------------------------------------------------------
def now_ms() -> int:
    return int(time.monotonic() * 1000)
