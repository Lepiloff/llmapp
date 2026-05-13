"""Pydantic schema invariants — the contract between LLM and pipeline.

Tests in this file are pure Python (no DB). They guard:

* Forbidden extra keys (``extra="forbid"``) — silent typo fields would
  bypass the merge layer's safety.
* Evidence trimming for capabilities.
* Confidence bounds [0, 1] for proposal types.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from apps.agent.llm.schemas import (
    AppSnapshot,
    CapabilityProposal,
    CategoryProposal,
    EnrichedDraft,
    ListingTypeProposal,
    MergeSet,
)


def test_capability_proposal_trims_long_evidence() -> None:
    long = "x" * 500
    prop = CapabilityProposal(value="yes", evidence=long)
    assert len(prop.evidence) == 280


def test_capability_proposal_strips_whitespace_evidence() -> None:
    prop = CapabilityProposal(value="yes", evidence="   short   ")
    assert prop.evidence == "short"


def test_capability_proposal_rejects_invalid_value() -> None:
    with pytest.raises(ValidationError):
        CapabilityProposal(value="maybe")  # type: ignore[arg-type]


def test_capability_proposal_clamps_confidence() -> None:
    with pytest.raises(ValidationError):
        CapabilityProposal(value="yes", evidence="x", confidence=1.5)
    with pytest.raises(ValidationError):
        CapabilityProposal(value="yes", evidence="x", confidence=-0.1)


def test_category_proposal_requires_confidence() -> None:
    with pytest.raises(ValidationError):
        CategoryProposal(slug="productivity")  # type: ignore[call-arg]


def test_listing_type_proposal_requires_confidence() -> None:
    with pytest.raises(ValidationError):
        ListingTypeProposal(slug="mcp-server")  # type: ignore[call-arg]


def test_merge_set_forbids_extra_keys() -> None:
    """A typo in a field name must NOT silently get accepted."""
    with pytest.raises(ValidationError):
        MergeSet(short_description="x", typo_field="oops")  # type: ignore[call-arg]


def test_enriched_draft_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError):
        EnrichedDraft(name="X", typo_field=1)  # type: ignore[call-arg]


def test_merge_set_proposed_launch_status_choices() -> None:
    with pytest.raises(ValidationError):
        MergeSet(proposed_launch_status="archived")  # type: ignore[arg-type]


def test_app_snapshot_carries_all_taxonomy_axes() -> None:
    snap = AppSnapshot(
        app_id=1,
        slug="x",
        name="X",
        status="draft",
        editorial_review_status="unreviewed",
        platform_verification_status="official",
        developer_claim_status="unclaimed",
        launch_status="live",
        pricing_model="unknown",
        platform_slugs=("mcp",),
        category_slugs=(),
        listing_type_slugs=("mcp-server",),
        use_case_slugs=(),
        capabilities={"open_source": "yes"},
    )
    assert snap.platform_slugs == ("mcp",)
    assert snap.capabilities == {"open_source": "yes"}
