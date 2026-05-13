"""Merge policy — the never-overwrite-editorial-intent contract.

These tests are pure Python and table-driven. They are intentionally
the densest tests in Phase 1: the merge policy is the safety boundary
between hallucinations / drift and the catalog's editorial state.
"""
from __future__ import annotations

import pytest

from apps.agent.llm.schemas import (
    AppSnapshot,
    CapabilityProposal,
    CategoryProposal,
    ListingTypeProposal,
    MergeSet,
)
from apps.agent.pipeline.merge import compute_merge


def _snap(**overrides) -> AppSnapshot:
    """Build a baseline DRAFT snapshot; overrides patch specific fields."""
    base = dict(
        app_id=1,
        slug="example",
        name="Example",
        short_description="",
        long_description="",
        developer_name="",
        official_page_url="",
        install_url="",
        repo_url="",
        status="draft",
        editorial_review_status="unreviewed",
        platform_verification_status="official",
        developer_claim_status="unclaimed",
        launch_status="live",
        pricing_model="unknown",
        verdict="",
        platform_slugs=("mcp",),
        listing_type_slugs=(),
        category_slugs=(),
        use_case_slugs=(),
        capabilities={
            "read_data": "unknown",
            "write_actions": "unknown",
            "open_source": "unknown",
        },
    )
    base.update(overrides)
    return AppSnapshot(**base)


# ---------------------------------------------------------------------------
# Text fields
# ---------------------------------------------------------------------------
def test_empty_text_field_is_filled_by_plan() -> None:
    snap = _snap()
    merge = MergeSet(short_description="A fresh description")
    outcome = compute_merge(snap, merge)

    assert any(
        u.field == "short_description" and u.new_value == "A fresh description"
        for u in outcome.plan.field_updates
    )
    assert outcome.queue.skipped_field_updates == []


def test_non_empty_text_field_is_NEVER_overwritten() -> None:
    snap = _snap(short_description="Editor's hand-written description")
    merge = MergeSet(short_description="LLM proposal")
    outcome = compute_merge(snap, merge)

    assert not any(
        u.field == "short_description" for u in outcome.plan.field_updates
    ), "MERGE POLICY VIOLATED: short_description was overwritten"
    assert any(
        u.field == "short_description" and u.new_value == "LLM proposal"
        for u in outcome.queue.skipped_field_updates
    )


def test_identical_text_proposal_is_silent_noop() -> None:
    """When the LLM repeats what the editor already wrote, don't pester."""
    snap = _snap(short_description="Same text")
    merge = MergeSet(short_description="Same text")
    outcome = compute_merge(snap, merge)

    assert outcome.plan.field_updates == []
    assert outcome.queue.skipped_field_updates == []


def test_whitespace_only_proposals_are_ignored() -> None:
    snap = _snap()
    merge = MergeSet(short_description="   \n  ")
    outcome = compute_merge(snap, merge)
    assert outcome.plan.field_updates == []


def test_none_proposals_are_ignored() -> None:
    snap = _snap()
    merge = MergeSet()  # all field defaults are None
    outcome = compute_merge(snap, merge)
    assert outcome.plan.is_empty()
    assert outcome.queue.is_empty()


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------
def test_yes_with_evidence_fills_unknown_slot() -> None:
    snap = _snap()
    merge = MergeSet(
        capabilities={
            "open_source": CapabilityProposal(value="yes", evidence="GitHub URL")
        }
    )
    outcome = compute_merge(snap, merge)
    assert len(outcome.plan.capability_updates) == 1
    upd = outcome.plan.capability_updates[0]
    assert upd.key == "open_source"
    assert upd.value == "yes"
    assert upd.evidence == "GitHub URL"


def test_yes_never_overwrites_existing_no() -> None:
    snap = _snap(capabilities={
        "write_actions": "no",
        "read_data": "unknown",
        "open_source": "unknown",
    })
    merge = MergeSet(
        capabilities={
            "write_actions": CapabilityProposal(value="yes", evidence="cite")
        }
    )
    outcome = compute_merge(snap, merge)
    # Editor said NO; LLM cannot flip it silently.
    assert outcome.plan.capability_updates == []
    # But the disagreement IS surfaced for review.
    assert len(outcome.queue.skipped_capability_updates) == 1
    skipped = outcome.queue.skipped_capability_updates[0]
    assert skipped.key == "write_actions"
    assert skipped.value == "yes"


def test_unknown_proposal_is_noop_even_over_unknown() -> None:
    snap = _snap()
    merge = MergeSet(
        capabilities={
            "open_source": CapabilityProposal(value="unknown", evidence="")
        }
    )
    outcome = compute_merge(snap, merge)
    assert outcome.plan.capability_updates == []
    assert outcome.queue.skipped_capability_updates == []


def test_yes_same_as_existing_yes_is_noop() -> None:
    snap = _snap(capabilities={
        "open_source": "yes",
        "read_data": "unknown",
        "write_actions": "unknown",
    })
    merge = MergeSet(
        capabilities={
            "open_source": CapabilityProposal(value="yes", evidence="x")
        }
    )
    outcome = compute_merge(snap, merge)
    assert outcome.plan.capability_updates == []
    assert outcome.queue.skipped_capability_updates == []


# ---------------------------------------------------------------------------
# Categories / listing types / use-cases — additive only
# ---------------------------------------------------------------------------
def test_category_is_added_when_missing() -> None:
    snap = _snap()
    merge = MergeSet(
        add_categories=[CategoryProposal(slug="developer-tools", confidence=0.9)]
    )
    outcome = compute_merge(snap, merge)
    assert outcome.plan.add_categories == ["developer-tools"]


def test_existing_category_is_not_re_added() -> None:
    snap = _snap(category_slugs=("developer-tools",))
    merge = MergeSet(
        add_categories=[CategoryProposal(slug="developer-tools", confidence=0.9)]
    )
    outcome = compute_merge(snap, merge)
    assert outcome.plan.add_categories == []


def test_listing_type_is_additive_only() -> None:
    snap = _snap(listing_type_slugs=("mcp-server",))
    merge = MergeSet(
        add_listing_types=[
            ListingTypeProposal(slug="mcp-server", confidence=0.99),
            ListingTypeProposal(slug="claude-connector", confidence=0.85),
        ]
    )
    outcome = compute_merge(snap, merge)
    assert outcome.plan.add_listing_types == ["claude-connector"]


def test_use_case_dedup_by_slugified_title() -> None:
    snap = _snap(use_case_slugs=("turn-notes-into-slides",))
    merge = MergeSet(
        add_use_cases=["Turn Notes Into Slides", "Summarize PDF"]
    )
    outcome = compute_merge(snap, merge)
    # Phase 1's merge uses a best-effort slugifier (lowercase + dashes).
    assert outcome.plan.add_use_cases == ["Summarize PDF"]


# ---------------------------------------------------------------------------
# Editorial proposals — never applied, always queued
# ---------------------------------------------------------------------------
def test_proposed_verdict_is_queued_never_applied() -> None:
    snap = _snap()
    merge = MergeSet(proposed_verdict="Best for AI agents with a Postgres backend")
    outcome = compute_merge(snap, merge)

    # No plan touches verdict — there are no plan.field_updates at all
    # because verdict is not in _SAFE_TEXT_FIELDS.
    assert outcome.plan.is_empty()
    assert outcome.queue.proposed_verdict.startswith("Best for")


def test_proposed_launch_status_is_queued_never_applied() -> None:
    snap = _snap(launch_status="live")
    merge = MergeSet(proposed_launch_status="deprecated")
    outcome = compute_merge(snap, merge)
    assert outcome.plan.is_empty()
    assert outcome.queue.proposed_launch_status == "deprecated"


def test_proposed_pricing_is_queued_never_applied() -> None:
    snap = _snap(pricing_model="unknown")
    merge = MergeSet(proposed_pricing_model="freemium")
    outcome = compute_merge(snap, merge)
    assert outcome.plan.is_empty()
    assert outcome.queue.proposed_pricing_model == "freemium"


# ---------------------------------------------------------------------------
# Hard invariant: forbidden fields never appear in plan
# ---------------------------------------------------------------------------
def test_merge_never_proposes_writes_to_status_fields() -> None:
    """The merge policy must not, under any circumstances, propose
    writes to App.status / editorial_review_status / verdict /
    platform_verification_status / developer_claim_status.
    """
    snap = _snap()
    # Build a merge with every kind of input the LLM could send.
    merge = MergeSet(
        short_description="x",
        long_description="y",
        developer_name="Acme",
        official_page_url="https://example.com",
        install_url="https://example.com/install",
        repo_url="https://github.com/x/y",
        proposed_verdict="should never be applied",
        proposed_launch_status="deprecated",
        proposed_pricing_model="paid",
        capabilities={
            "open_source": CapabilityProposal(value="yes", evidence="GH"),
        },
        add_categories=[CategoryProposal(slug="x", confidence=0.99)],
    )
    outcome = compute_merge(snap, merge)
    forbidden = {
        "status",
        "editorial_review_status",
        "platform_verification_status",
        "developer_claim_status",
        "verdict",
    }
    for u in outcome.plan.field_updates:
        assert u.field not in forbidden, (
            f"MERGE INVARIANT VIOLATED: plan wants to write {u.field!r}"
        )
