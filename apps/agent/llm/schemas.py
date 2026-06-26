"""Pydantic models for LLM-pipeline structured I/O.

These are the *contract* between the pipeline (pure Python) and any
LLM provider — every provider returns the same Pydantic shape regardless
of whether it's Anthropic tool-use, OpenAI JSON schema response_format,
or the in-process MockLLMProvider.

Two principles drive the field choices:

1. **Evidence-first.** Anything that affects user-facing trust signals
   (capabilities yes/no, categories, listing types) carries an
   ``evidence`` quote. The merge layer downgrades anything without
   evidence to ``unknown`` / discards it. Hallucinations get filtered
   out at the data boundary, not at admin review time.
2. **Confidence-gated.** Categories and listing-type assignments carry
   numeric confidence. The merge layer applies a configurable floor
   (default 0.7). Less-confident proposals still travel through the
   pipeline — they show up in ``NeedsReviewQueueEntry`` for the editor.

These models intentionally do NOT include Django types — they live in
the pure-Python pipeline and migrate as-is when the agent is extracted
to its own service.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Capability value with evidence
# ---------------------------------------------------------------------------
CapabilityValue = Literal["yes", "no", "unknown"]


class CapabilityProposal(BaseModel):
    """LLM's call on a single capability for an App.

    ``evidence`` is required (possibly empty string) so the merge layer
    can apply the "no evidence ⇒ unknown" guardrail without ever seeing
    ``None``. The validator below enforces that ``yes``/``no`` answers
    actually carry text.
    """

    model_config = ConfigDict(extra="forbid")

    value: CapabilityValue
    evidence: str = Field(
        default="",
        description=(
            "Short quote (≤ 280 chars) from the source supporting a yes/no"
            " answer. Required for yes/no; ignored when value=unknown."
        ),
    )
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0,
        description="0..1 — defensive when LLM expresses doubt.",
    )

    @field_validator("evidence")
    @classmethod
    def _trim_evidence(cls, v: str) -> str:
        return (v or "").strip()[:280]


class CategoryProposal(BaseModel):
    """LLM's suggestion of one category slug + confidence."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    confidence: float = Field(..., ge=0.0, le=1.0)


class ListingTypeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    confidence: float = Field(..., ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# EnrichedDraft — output of enrich_new_app (Phase 3+; here for completeness)
# ---------------------------------------------------------------------------
class EnrichedDraft(BaseModel):
    """Full structured draft for a *new* candidate URL.

    Used by Phase 3+ discovery sources. In Phase 1 we only consume the
    subset relevant for existing-draft enrichment via :class:`MergeSet`.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    short_description: str = ""
    long_description: str = ""
    developer_name: str = ""
    developer_url: str = ""
    official_page_url: str = ""
    install_url: str = ""
    repo_url: str = ""

    listing_types: list[ListingTypeProposal] = Field(default_factory=list)
    categories: list[CategoryProposal] = Field(default_factory=list)
    capabilities: dict[str, CapabilityProposal] = Field(default_factory=dict)
    use_cases: list[str] = Field(default_factory=list)

    launch_status: Literal["live", "beta", "waitlist", "deprecated"] = "live"
    pricing_model: Literal["free", "paid", "freemium", "unknown"] = "unknown"

    proposed_verdict: str = Field(
        default="",
        description=(
            "Editor's one-liner candidate. Never written to App.verdict;"
            " stored in Source.payload for editorial review."
        ),
    )
    scope_summary: str = ""


# ---------------------------------------------------------------------------
# DiscoveryDecision — cheap classifier for Phase 3 source candidates
# ---------------------------------------------------------------------------
class DiscoveryDecision(BaseModel):
    """YES/NO decision for whether a discovered URL is catalog-worthy."""

    model_config = ConfigDict(extra="forbid")

    is_relevant: bool
    canonical_url: str
    reason: str
    confidence: float = Field(..., ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# MergeSet — output of enrich_existing_draft (Phase 1 target)
# ---------------------------------------------------------------------------
class MergeSet(BaseModel):
    """A delta the agent proposes for an *existing* DRAFT App.

    The merge layer (apps.agent.pipeline.merge) is the only place that
    decides which of these fields are applied automatically vs routed
    to the review queue. The LLM is allowed to propose anything; the
    merge policy enforces "never overwrite editorial intent".
    """

    model_config = ConfigDict(extra="forbid")

    # Text fields — applied only if the current value is empty.
    short_description: str | None = None
    long_description: str | None = None
    developer_name: str | None = None
    developer_url: str | None = None
    official_page_url: str | None = None
    install_url: str | None = None
    repo_url: str | None = None

    # Taxonomy — additive only (never removes existing assignments).
    add_listing_types: list[ListingTypeProposal] = Field(default_factory=list)
    add_categories: list[CategoryProposal] = Field(default_factory=list)
    add_use_cases: list[str] = Field(default_factory=list)

    # Capabilities — yes/no only fills slots currently equal to ``unknown``.
    capabilities: dict[str, CapabilityProposal] = Field(default_factory=dict)

    # Editorial proposals — NEVER applied to App fields automatically.
    # Routed to NeedsReviewQueueEntry.payload for the editor to act on.
    proposed_verdict: str = ""
    proposed_launch_status: Literal["live", "beta", "waitlist", "deprecated"] | None = None
    proposed_pricing_model: Literal["free", "paid", "freemium", "unknown"] | None = None
    proposed_scope_summary: str = ""

    # The LLM's own free-form explanation. Stored in Source.payload for
    # audit; never user-facing.
    rationale: str = ""


# ---------------------------------------------------------------------------
# AppSnapshot — input to enrich_existing_draft (pure-Python view of an App)
# ---------------------------------------------------------------------------
class AppSnapshot(BaseModel):
    """Frozen view of an existing App + its related taxonomy at one point.

    Built by ``apps.agent.persist.build_app_snapshot``. Crossing the
    pipeline ↔ Django boundary as a serializable Pydantic model keeps
    the pure-Python core importable without django.setup() and makes
    fixtures trivial to write.
    """

    model_config = ConfigDict(extra="forbid")

    app_id: int
    slug: str
    name: str

    short_description: str = ""
    long_description: str = ""
    developer_name: str = ""
    developer_url: str = ""
    official_page_url: str = ""
    install_url: str = ""
    repo_url: str = ""

    status: str
    editorial_review_status: str
    platform_verification_status: str
    developer_claim_status: str
    launch_status: str
    pricing_model: str

    verdict: str = ""

    platform_slugs: tuple[str, ...] = Field(default_factory=tuple)
    listing_type_slugs: tuple[str, ...] = Field(default_factory=tuple)
    category_slugs: tuple[str, ...] = Field(default_factory=tuple)
    use_case_slugs: tuple[str, ...] = Field(default_factory=tuple)

    # capability_key -> current value (yes/no/unknown). Present even for
    # capabilities the App has no row for — defaulted to ``unknown`` by
    # the snapshot builder so the merge layer has a uniform view.
    capabilities: dict[str, str] = Field(default_factory=dict)
