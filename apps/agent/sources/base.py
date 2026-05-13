"""Shared discovery-source primitives."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DiscoveryCandidate:
    """A URL or repository that may become a catalog draft."""

    external_id: str
    url: str
    title: str
    summary: str = ""
    source_name: str = ""
    raw_payload: dict = field(default_factory=dict)

    def llm_context(self) -> str:
        return (
            f"title: {self.title}\n"
            f"url: {self.url}\n"
            f"source: {self.source_name}\n"
            f"summary: {self.summary}\n"
        )
