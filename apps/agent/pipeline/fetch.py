"""Fetch primitives for Phase 3 enrichment."""
from __future__ import annotations

from dataclasses import dataclass, field

import requests

from apps.agent.pipeline.rate_limit import get_default_limiter


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status_code: int
    content_type: str
    text: str
    raw_payload: dict = field(default_factory=dict)

    def llm_context(self) -> str:
        return (
            f"url: {self.url}\n"
            f"final_url: {self.final_url}\n"
            f"status_code: {self.status_code}\n"
            f"content_type: {self.content_type}\n"
            f"text:\n{self.text[:12000]}\n"
        )


def fetch_url_text(url: str, *, timeout: float = 20.0) -> FetchResult:
    """Fetch URL text for enrichment.

    Per-domain throttle is enforced by ``DomainRateLimiter`` configured
    from ``AGENT_RATE_LIMIT_RPS_PER_DOMAIN`` (defaults to 1.0 RPS). The
    helper is still test-injectable: tests should pass their own
    ``fetcher`` to ``run_enrich_new_app`` rather than mocking
    ``requests`` here, so the throttle isn't exercised under unit tests.
    """
    get_default_limiter().acquire(url)
    resp = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": "LLMAppMarket-Agent/1.0 (+https://llmappmarket.com/bots)"},
    )
    resp.raise_for_status()
    return FetchResult(
        url=url,
        final_url=resp.url,
        status_code=resp.status_code,
        content_type=resp.headers.get("content-type", ""),
        text=resp.text,
        raw_payload={
            "url": url,
            "final_url": resp.url,
            "status_code": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
        },
    )
