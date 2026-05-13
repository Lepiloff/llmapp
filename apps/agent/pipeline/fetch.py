"""Fetch primitives for Phase 3 enrichment."""
from __future__ import annotations

from dataclasses import dataclass, field

import requests


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

    Full robots/rate-limit enforcement is the larger Phase 3/5 fetcher
    work. This helper is intentionally small and test-injectable so
    `run_enrich_new_app` can be validated without network access.
    """
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
