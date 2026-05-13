"""Validator guardrails — the second line of defense against hallucinations."""
from __future__ import annotations

import pytest

from apps.agent.llm.schemas import (
    CapabilityProposal,
    CategoryProposal,
    EnrichedDraft,
    ListingTypeProposal,
    MergeSet,
)
from apps.agent.pipeline.taxonomy import TaxonomySnapshot
from apps.agent.pipeline.validate import validate_enriched_draft, validate_merge_set


@pytest.fixture
def taxonomy() -> TaxonomySnapshot:
    return TaxonomySnapshot(
        platform_slugs=("chatgpt", "claude", "mcp"),
        category_slugs=("productivity", "developer-tools"),
        capability_keys=("read_data", "write_actions", "open_source"),
        listing_type_slugs=("mcp-server", "claude-connector"),
    )


def test_capability_without_evidence_is_downgraded_to_unknown(taxonomy) -> None:
    merge = MergeSet(
        capabilities={"write_actions": CapabilityProposal(value="yes", evidence="")}
    )
    sanitized, report = validate_merge_set(merge, taxonomy)
    assert sanitized.capabilities["write_actions"].value == "unknown"
    assert "write_actions" in report.dropped_capabilities_no_evidence


def test_capability_with_evidence_survives(taxonomy) -> None:
    merge = MergeSet(
        capabilities={
            "open_source": CapabilityProposal(value="yes", evidence="GitHub URL in README"),
        }
    )
    sanitized, report = validate_merge_set(merge, taxonomy)
    assert sanitized.capabilities["open_source"].value == "yes"
    assert not report.dropped_capabilities_no_evidence


def test_capability_with_unknown_key_is_dropped(taxonomy) -> None:
    merge = MergeSet(
        capabilities={"bogus_key": CapabilityProposal(value="yes", evidence="x")}
    )
    sanitized, report = validate_merge_set(merge, taxonomy)
    assert "bogus_key" not in sanitized.capabilities
    assert "bogus_key" in report.dropped_capabilities_unknown_key


def test_unknown_category_slug_is_dropped(taxonomy) -> None:
    merge = MergeSet(
        add_categories=[CategoryProposal(slug="not-a-real-cat", confidence=0.95)]
    )
    sanitized, report = validate_merge_set(merge, taxonomy)
    assert sanitized.add_categories == []
    assert "not-a-real-cat" in report.dropped_categories_unknown_slug


def test_low_confidence_category_is_dropped(taxonomy) -> None:
    merge = MergeSet(
        add_categories=[CategoryProposal(slug="productivity", confidence=0.5)]
    )
    sanitized, report = validate_merge_set(merge, taxonomy)
    assert sanitized.add_categories == []
    assert "productivity" in report.dropped_categories_low_confidence


def test_confidence_floor_is_configurable(taxonomy) -> None:
    merge = MergeSet(
        add_categories=[CategoryProposal(slug="productivity", confidence=0.5)]
    )
    sanitized, _ = validate_merge_set(merge, taxonomy, confidence_floor=0.4)
    assert sanitized.add_categories[0].slug == "productivity"


def test_unknown_listing_type_is_dropped(taxonomy) -> None:
    merge = MergeSet(
        add_listing_types=[ListingTypeProposal(slug="invented", confidence=0.95)]
    )
    sanitized, report = validate_merge_set(merge, taxonomy)
    assert sanitized.add_listing_types == []
    assert "invented" in report.dropped_listing_types_unknown_slug


def test_invalid_url_is_dropped(taxonomy) -> None:
    merge = MergeSet(
        official_page_url="not a url",
        repo_url="https://github.com/x/y",
    )
    sanitized, report = validate_merge_set(merge, taxonomy)
    assert sanitized.official_page_url is None
    assert sanitized.repo_url == "https://github.com/x/y"
    assert ("official_page_url", "not a url") in report.dropped_urls_invalid


def test_validator_never_mutates_input(taxonomy) -> None:
    original = MergeSet(
        capabilities={"open_source": CapabilityProposal(value="yes", evidence="")}
    )
    snapshot = original.model_copy(deep=True)
    validate_merge_set(original, taxonomy)
    assert original == snapshot


def test_validate_enriched_draft_applies_same_guardrails(taxonomy) -> None:
    draft = EnrichedDraft(
        name="Acme MCP",
        official_page_url="not-a-url",
        listing_types=[
            ListingTypeProposal(slug="mcp-server", confidence=0.9),
            ListingTypeProposal(slug="unknown", confidence=0.9),
        ],
        categories=[
            CategoryProposal(slug="developer-tools", confidence=0.9),
            CategoryProposal(slug="productivity", confidence=0.2),
        ],
        capabilities={
            "open_source": CapabilityProposal(value="yes", evidence=""),
            "unknown_cap": CapabilityProposal(value="yes", evidence="Source"),
        },
    )

    sanitized, report = validate_enriched_draft(draft, taxonomy)

    assert sanitized.official_page_url == ""
    assert [lt.slug for lt in sanitized.listing_types] == ["mcp-server"]
    assert [cat.slug for cat in sanitized.categories] == ["developer-tools"]
    assert sanitized.capabilities["open_source"].value == "unknown"
    assert "unknown_cap" not in sanitized.capabilities
    assert report.dropped_urls_invalid == [("official_page_url", "not-a-url")]
