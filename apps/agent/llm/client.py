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

Anthropic is still deferred, but OpenAI is wired in Phase 1b through
the official SDK's Pydantic structured-output helper.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict

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


class LLMProviderError(RuntimeError):
    """Base class for provider failures surfaced to the orchestrator."""


class LLMProviderRefusal(LLMProviderError):
    """Raised when the model refuses a structured-output request."""


class _OpenAIListingTypeProposal(BaseModel):
    """Wire-only OpenAI schema.

    OpenAI strict structured outputs require every object property to be
    listed in ``required``. The internal ``MergeSet`` intentionally uses
    Python defaults for ergonomic tests/merge logic, so we keep this
    schema separate and convert back after parsing.
    """

    model_config = ConfigDict(extra="forbid")

    slug: str
    confidence: float


class _OpenAICategoryProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    confidence: float


class _OpenAICapabilityProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value: Literal["yes", "no", "unknown"]
    evidence: str
    confidence: float


class _OpenAIMergeSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    short_description: str | None
    long_description: str | None
    developer_name: str | None
    developer_url: str | None
    official_page_url: str | None
    install_url: str | None
    repo_url: str | None
    add_listing_types: list[_OpenAIListingTypeProposal]
    add_categories: list[_OpenAICategoryProposal]
    add_use_cases: list[str]
    capabilities: list[_OpenAICapabilityProposal]
    proposed_verdict: str
    proposed_launch_status: Literal["live", "beta", "waitlist", "deprecated"] | None
    proposed_pricing_model: Literal["free", "paid", "freemium", "unknown"] | None
    proposed_scope_summary: str
    rationale: str


class _OpenAIEnrichedDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    short_description: str
    long_description: str
    developer_name: str
    developer_url: str
    official_page_url: str
    install_url: str
    repo_url: str
    listing_types: list[_OpenAIListingTypeProposal]
    categories: list[_OpenAICategoryProposal]
    capabilities: list[_OpenAICapabilityProposal]
    use_cases: list[str]
    launch_status: Literal["live", "beta", "waitlist", "deprecated"]
    pricing_model: Literal["free", "paid", "freemium", "unknown"]
    proposed_verdict: str
    scope_summary: str


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
    """OpenAI via the official SDK and Pydantic structured outputs."""

    name = "openai"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        client=None,
        input_cost_per_1m_tokens: float = 0.0,
        cached_input_cost_per_1m_tokens: float = 0.0,
        output_cost_per_1m_tokens: float = 0.0,
        timeout: float | None = None,
        max_retries: int = 3,
    ) -> None:
        if not model:
            raise ValueError("OpenAIProvider requires a non-empty model.")
        self.model = model
        self.input_cost_per_1m_tokens = input_cost_per_1m_tokens
        self.cached_input_cost_per_1m_tokens = cached_input_cost_per_1m_tokens
        self.output_cost_per_1m_tokens = output_cost_per_1m_tokens
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        if client is not None:
            self.client = client
        else:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - depends on env
                raise RuntimeError(
                    "OpenAIProvider requires the 'openai' package. "
                    "Install project dependencies before using "
                    "AGENT_LLM_PROVIDER_PRIMARY=openai."
                ) from exc
            kwargs = {"api_key": api_key}
            if timeout is not None:
                kwargs["timeout"] = timeout
            self.client = OpenAI(**kwargs)

    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        schema: type[SchemaT],
        taxonomy: TaxonomySnapshot | None = None,
        prompt_version: str = "",
    ) -> LLMResponse:
        """Call OpenAI and return a parsed Pydantic model.

        Uses ``chat.completions.parse`` because it accepts a Pydantic
        ``response_format`` directly and returns ``message.parsed``.
        """
        started = now_ms()
        provider_schema = _openai_wire_schema_for(schema)
        completion = self._parse_with_retry(
            model=self.model,
            messages=[{"role": "system", "content": system}, *messages],
            response_format=provider_schema,
        )
        latency_ms = max(0, now_ms() - started)

        choice = completion.choices[0]
        message = choice.message
        refusal = getattr(message, "refusal", None)
        if refusal:
            raise LLMProviderRefusal(f"OpenAI refused structured output: {refusal}")

        parsed = getattr(message, "parsed", None)
        if not isinstance(parsed, provider_schema):
            raise LLMProviderError(
                f"OpenAI returned no parsed {provider_schema.__name__} payload."
            )
        data = _coerce_openai_output(parsed, target_schema=schema)

        usage = getattr(completion, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        cached_tokens = _extract_cached_tokens(usage)
        cost_usd = _estimate_cost_usd(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            input_cost_per_1m_tokens=self.input_cost_per_1m_tokens,
            output_cost_per_1m_tokens=self.output_cost_per_1m_tokens,
            cached_input_cost_per_1m_tokens=self.cached_input_cost_per_1m_tokens,
        )

        return LLMResponse(
            data=data,
            meta=LLMCallMetadata(
                provider=self.name,
                model=self.model,
                prompt_version=prompt_version,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                is_mock=False,
            ),
        )

    def _parse_with_retry(self, **kwargs):
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return self.client.chat.completions.parse(**kwargs)
            except Exception as exc:  # pragma: no cover - concrete SDK classes vary
                last_exc = exc
                if attempt >= self.max_retries or not _is_retryable_openai_error(exc):
                    raise LLMProviderError(
                        f"OpenAI request failed: {_safe_provider_error(exc)}"
                    ) from exc
                time.sleep(min(2 ** (attempt - 1), 8))
        assert last_exc is not None
        raise LLMProviderError(
            f"OpenAI request failed: {_safe_provider_error(last_exc)}"
        ) from last_exc


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
# ---------------------------------------------------------------------------
# Provider registry — keyed by ``AGENT_LLM_PROVIDER_*`` env value.
# Each entry is a builder taking (settings, role, model, cost_prefix) and
# returning a configured LLMProvider instance.
#
# Adding a new provider (Bedrock / Vertex / Mistral-API / ...) is a single
# new entry below + the concrete LLMProvider subclass — no surgery on the
# if-elif tree.
# ---------------------------------------------------------------------------
def _build_mock(settings, role, model, cost_prefix) -> LLMProvider:
    return MockLLMProvider(model=model or "mock-model")


def _build_anthropic(settings, role, model, cost_prefix) -> LLMProvider:
    from django.core.exceptions import ImproperlyConfigured

    if not model:
        raise ImproperlyConfigured(
            "AGENT_LLM_PROVIDER_*=anthropic requires AGENT_LLM_MODEL_*."
        )
    if not settings.ANTHROPIC_API_KEY:
        raise ImproperlyConfigured(
            "AGENT_LLM_PROVIDER_*=anthropic requires ANTHROPIC_API_KEY."
        )
    return AnthropicProvider(model=model, api_key=settings.ANTHROPIC_API_KEY)


def _build_openai(settings, role, model, cost_prefix) -> LLMProvider:
    from django.core.exceptions import ImproperlyConfigured

    if not model:
        raise ImproperlyConfigured(
            "AGENT_LLM_PROVIDER_*=openai requires AGENT_LLM_MODEL_*."
        )
    if not settings.OPENAI_API_KEY:
        raise ImproperlyConfigured(
            "AGENT_LLM_PROVIDER_*=openai requires OPENAI_API_KEY."
        )
    return OpenAIProvider(
        model=model,
        api_key=settings.OPENAI_API_KEY,
        input_cost_per_1m_tokens=float(
            getattr(settings, f"{cost_prefix}_INPUT_COST_PER_1M_TOKENS", 0) or 0
        ),
        cached_input_cost_per_1m_tokens=float(
            getattr(settings, f"{cost_prefix}_CACHED_COST_PER_1M_TOKENS", 0) or 0
        ),
        output_cost_per_1m_tokens=float(
            getattr(settings, f"{cost_prefix}_OUTPUT_COST_PER_1M_TOKENS", 0) or 0
        ),
        timeout=float(
            getattr(settings, "AGENT_OPENAI_TIMEOUT_SECONDS", 90.0) or 90.0
        ),
    )


# Map of provider-key → builder. Lower-cased; ``register_provider`` allows
# third-party providers (or test doubles) to plug in at runtime without
# editing this module.
_PROVIDER_REGISTRY: dict[str, Callable[..., LLMProvider]] = {
    "mock": _build_mock,
    "anthropic": _build_anthropic,
    "openai": _build_openai,
}


def register_provider(key: str, builder: Callable[..., LLMProvider]) -> None:
    """Register a new provider builder under ``key``.

    Builders receive ``(settings, role, model, cost_prefix)`` — same
    contract as the in-tree entries. Idempotent: re-registering an
    existing key replaces the previous builder.
    """
    _PROVIDER_REGISTRY[key.lower()] = builder


def _role_config(settings, role: str) -> tuple[str, str, str]:
    """Resolve (provider_key, model, cost_prefix) for ``role``."""
    if role == "primary":
        return (
            settings.AGENT_LLM_PROVIDER_PRIMARY,
            settings.AGENT_LLM_MODEL_PRIMARY,
            "AGENT_OPENAI_PRIMARY",
        )
    if role == "cheap":
        return (
            settings.AGENT_LLM_PROVIDER_CHEAP,
            settings.AGENT_LLM_MODEL_CHEAP,
            "AGENT_OPENAI_CHEAP",
        )
    raise ValueError(f"Unknown LLM role: {role!r}")


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
    a worker task at runtime. Unknown ``AGENT_LLM_PROVIDER_*`` values
    raise too, so a typo doesn't silently fall through to mock.
    """
    from django.conf import settings as django_settings
    from django.core.exceptions import ImproperlyConfigured

    settings = settings_module or django_settings
    provider_key, model, cost_prefix = _role_config(settings, role)
    provider_key = (provider_key or "mock").lower()

    builder = _PROVIDER_REGISTRY.get(provider_key)
    if builder is None:
        raise ImproperlyConfigured(
            f"Unknown LLM provider key: {provider_key!r}. "
            f"Known: {sorted(_PROVIDER_REGISTRY)}."
        )
    return builder(settings, role, model, cost_prefix)


# ---------------------------------------------------------------------------
# Timer helper for real providers (Phase 1b will use this)
# ---------------------------------------------------------------------------
def now_ms() -> int:
    return int(time.monotonic() * 1000)


def _extract_cached_tokens(usage) -> int:
    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None:
        return 0
    if isinstance(details, dict):
        return int(details.get("cached_tokens") or 0)
    return int(getattr(details, "cached_tokens", 0) or 0)


def _estimate_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    input_cost_per_1m_tokens: float,
    output_cost_per_1m_tokens: float,
    cached_input_cost_per_1m_tokens: float = 0.0,
) -> float:
    """Compute per-call cost, accounting for prompt-cache discounts.

    OpenAI reports ``prompt_tokens`` as the total (cached + non-cached);
    the cached subset is in ``prompt_tokens_details.cached_tokens`` and
    is billed at ~10% of the standard input price. We subtract cached
    from billable input so the row reflects the actual invoice.
    """
    cached = max(0, min(cached_tokens, input_tokens))
    non_cached_input = max(0, input_tokens - cached)
    return (
        non_cached_input / 1_000_000 * input_cost_per_1m_tokens
        + cached / 1_000_000 * cached_input_cost_per_1m_tokens
        + output_tokens / 1_000_000 * output_cost_per_1m_tokens
    )


def _is_retryable_openai_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code in {408, 409, 429, 500, 502, 503, 504}:
        return True
    name = exc.__class__.__name__.lower()
    return any(
        marker in name
        for marker in (
            "timeout",
            "ratelimit",
            "rate_limit",
            "apierror",
            "api_error",
            "apiconnection",
            "api_connection",
        )
    )


def _safe_provider_error(exc: Exception) -> str:
    status_code = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    error_type = exc.__class__.__name__
    parts = [error_type]
    if status_code is not None:
        parts.append(f"status={status_code}")
    if code:
        parts.append(f"code={code}")
    return " ".join(parts)


def _openai_wire_schema_for(schema: type[SchemaT]) -> type[BaseModel]:
    if schema.__name__ == "MergeSet":
        return _OpenAIMergeSet
    if schema.__name__ == "EnrichedDraft":
        return _OpenAIEnrichedDraft
    return schema


def _coerce_openai_output(parsed: BaseModel, *, target_schema: type[SchemaT]) -> SchemaT:
    if isinstance(parsed, target_schema):
        return parsed
    if target_schema.__name__ == "MergeSet" and isinstance(parsed, _OpenAIMergeSet):
        payload = parsed.model_dump()
        payload["capabilities"] = {
            item["key"]: {
                "value": item["value"],
                "evidence": item["evidence"],
                "confidence": item["confidence"],
            }
            for item in payload.pop("capabilities")
        }
        return target_schema.model_validate(payload)
    if target_schema.__name__ == "EnrichedDraft" and isinstance(parsed, _OpenAIEnrichedDraft):
        payload = parsed.model_dump()
        payload["capabilities"] = {
            item["key"]: {
                "value": item["value"],
                "evidence": item["evidence"],
                "confidence": item["confidence"],
            }
            for item in payload.pop("capabilities")
        }
        return target_schema.model_validate(payload)
    raise LLMProviderError(
        f"Cannot coerce OpenAI {type(parsed).__name__} to {target_schema.__name__}."
    )
