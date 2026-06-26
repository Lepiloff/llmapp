"""Source abstractions shared by every ingest backend.

`AppDraft` is the contract: every source produces normalized drafts; the
upsert layer (`apps.sources.upsert`) is the only code that knows about the
ORM. This split lets us swap data sources without touching catalog code.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass
class AppDraft:
    """In-memory normalized record. Not yet persisted.

    Mirrors docs/architecture.md § 9.1. Source backends populate as many
    fields as they can; ``platform_metadata`` carries type-specific blobs
    (e.g. MCP transport, ChatGPT marketplace id) without bloating the
    public schema.
    """

    name: str
    slug_hint: str
    short_description: str = ""
    long_description: str = ""
    developer_name: str = ""
    developer_url: str = ""
    official_page_url: str = ""
    install_url: str = ""
    repo_url: str = ""
    platforms: list[str] = field(default_factory=list)
    listing_types: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    capabilities: dict[str, str] = field(default_factory=dict)
    capability_evidence: dict[str, str] = field(default_factory=dict)
    use_cases: list[str] = field(default_factory=list)
    pricing_model: str = "unknown"
    launch_status: str = "live"
    external_id: str = ""
    raw_payload: dict = field(default_factory=dict)

    # Per-AppPlatform fields
    platform_metadata: dict = field(default_factory=dict)
    official_directory_url: str = ""
    supported_plans: list[str] = field(default_factory=list)
    region_availability: str = "unknown"
    scope_summary: str = ""


class BaseSource:
    """Iterate normalized drafts. Concrete sources override `iter_drafts`."""

    source_type: str

    def iter_drafts(self) -> Iterable[AppDraft]:  # pragma: no cover - interface
        raise NotImplementedError
