"""End-to-end pipeline test (pure-Python core, no DB).

Drives ``enrich_existing_draft`` with a ``MockLLMProvider``: confirms
prompt → LLM → validate → merge chains correctly and the LLM call is
recorded with ``is_mock=True``.
"""
from __future__ import annotations

import pytest

from apps.agent.llm.client import MockLLMProvider
from apps.agent.llm.schemas import (
    AppSnapshot,
    CapabilityProposal,
    CategoryProposal,
    MergeSet,
)
from apps.agent.pipeline.enrich import enrich_existing_draft
from apps.agent.pipeline.taxonomy import TaxonomySnapshot


@pytest.fixture
def taxonomy() -> TaxonomySnapshot:
    return TaxonomySnapshot(
        platform_slugs=("mcp",),
        category_slugs=("developer-tools",),
        capability_keys=("open_source", "write_actions"),
        listing_type_slugs=("mcp-server",),
    )


@pytest.fixture
def snapshot() -> AppSnapshot:
    return AppSnapshot(
        app_id=42,
        slug="example-mcp",
        name="ExampleMCP",
        short_description="",
        long_description="",
        developer_name="",
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
        capabilities={"open_source": "unknown", "write_actions": "unknown"},
    )


def test_happy_path_runs_prompt_then_validates_then_merges(
    snapshot, taxonomy
) -> None:
    llm = MockLLMProvider(
        responses_queue=[
            MergeSet(
                short_description="Fills the gap",
                add_categories=[
                    CategoryProposal(slug="developer-tools", confidence=0.95)
                ],
                capabilities={
                    "open_source": CapabilityProposal(
                        value="yes", evidence="GitHub README"
                    ),
                },
            )
        ]
    )

    result = enrich_existing_draft(snapshot, taxonomy, llm)

    assert result.call_meta.is_mock is True
    assert result.call_meta.provider == "mock"

    # Field plan filled
    field_names = [u.field for u in result.outcome.plan.field_updates]
    assert "short_description" in field_names

    # Category plan filled
    assert result.outcome.plan.add_categories == ["developer-tools"]

    # Capability plan filled
    assert any(
        u.key == "open_source" and u.value == "yes"
        for u in result.outcome.plan.capability_updates
    )

    # No editorial proposals → queue should be empty.
    assert result.outcome.queue.is_empty()


def test_evidenceless_capability_yes_is_silently_downgraded(
    snapshot, taxonomy
) -> None:
    llm = MockLLMProvider(
        responses_queue=[
            MergeSet(
                capabilities={
                    "write_actions": CapabilityProposal(value="yes", evidence="")
                }
            )
        ]
    )

    result = enrich_existing_draft(snapshot, taxonomy, llm)

    # Validation report flags it.
    assert "write_actions" in result.validation.dropped_capabilities_no_evidence
    # And the merge plan no longer writes yes/no — value is unknown now.
    assert result.outcome.plan.capability_updates == []


def test_unknown_taxonomy_slugs_are_dropped(snapshot, taxonomy) -> None:
    llm = MockLLMProvider(
        responses_queue=[
            MergeSet(
                add_categories=[
                    CategoryProposal(slug="not-in-taxonomy", confidence=0.99)
                ],
                capabilities={
                    "no_such_capability": CapabilityProposal(
                        value="yes", evidence="x"
                    ),
                },
            )
        ]
    )

    result = enrich_existing_draft(snapshot, taxonomy, llm)

    assert result.outcome.plan.add_categories == []
    assert result.outcome.plan.capability_updates == []
    assert "not-in-taxonomy" in result.validation.dropped_categories_unknown_slug
    assert (
        "no_such_capability"
        in result.validation.dropped_capabilities_unknown_key
    )


def test_proposed_editorial_fields_route_to_queue_only(
    snapshot, taxonomy
) -> None:
    llm = MockLLMProvider(
        responses_queue=[
            MergeSet(
                proposed_verdict="Pick this if you want GitHub MCP",
                proposed_launch_status="beta",
                proposed_pricing_model="freemium",
            )
        ]
    )

    result = enrich_existing_draft(snapshot, taxonomy, llm)

    assert result.outcome.plan.is_empty()
    assert result.outcome.queue.proposed_verdict.startswith("Pick this")
    assert result.outcome.queue.proposed_launch_status == "beta"
    assert result.outcome.queue.proposed_pricing_model == "freemium"


def test_prompt_includes_taxonomy_context(snapshot, taxonomy) -> None:
    llm = MockLLMProvider(responses_queue=[MergeSet()])
    enrich_existing_draft(snapshot, taxonomy, llm)

    assert len(llm.call_log) == 1
    call = llm.call_log[0]
    assert call["taxonomy_present"] is True
    assert call["prompt_version"].startswith("enrich-existing-")
    body = call["messages"][0]["content"]
    assert "developer-tools" in body
    assert "mcp-server" in body
    assert "open_source" in body
