"""Phase 1 hard-invariant test for ``apply_merge_set``.

Confirms the boundary the merge layer + persist layer jointly enforce:

* ``App.status`` stays ``DRAFT`` no matter what the LLM proposed.
* ``App.editorial_review_status`` stays ``UNREVIEWED``.
* ``App.platform_verification_status`` and ``App.developer_claim_status``
  are untouched.
* ``App.verdict`` is **never** written by the agent; the LLM's
  proposal lives only in ``Source.payload`` and
  ``NeedsReviewQueueEntry.payload``.
* Safe text fields fill empty slots; existing values are never
  overwritten by the persist layer (even if a hypothetical merge plan
  asked for it).
* Capabilities flip from ``unknown`` to ``yes/no`` with evidence as a
  ``note``; existing ``yes/no`` are left alone.
* Source row carrying the audit trail is upserted.
"""
from __future__ import annotations

import pytest

from apps.agent.llm.schemas import (
    CapabilityProposal,
    CategoryProposal,
    ListingTypeProposal,
    MergeSet,
)
from apps.agent.llm.client import MockLLMProvider
from apps.agent.models import NeedsReviewQueueEntry
from apps.agent.persist import (
    apply_merge_set,
    build_app_snapshot,
    build_taxonomy_snapshot,
)
from apps.agent.pipeline.enrich import enrich_existing_draft
from apps.catalog.models import (
    App,
    AppCapability,
    AppCategory,
    Capability,
    Category,
    ListingType,
    Platform,
)
from apps.sources.models import Source


pytestmark = pytest.mark.django_db


@pytest.fixture
def taxonomy_rows():
    Platform.objects.get_or_create(
        slug="mcp", defaults={"name": "MCP", "public_path": "mcp-servers"}
    )
    Category.objects.get_or_create(
        slug="developer-tools", defaults={"name": "Developer Tools"}
    )
    Capability.objects.get_or_create(
        key="open_source", defaults={"label": "Open source"}
    )
    Capability.objects.get_or_create(
        key="write_actions", defaults={"label": "Can take actions"}
    )
    ListingType.objects.get_or_create(
        slug="mcp-server", defaults={"name": "MCP Server"}
    )


@pytest.fixture
def draft_app(taxonomy_rows) -> App:
    app = App.objects.create(
        name="ExampleMCP",
        slug="examplemcp",
        short_description="",
        status=App.AppStatus.DRAFT,
        platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
        editorial_review_status=App.EditorialReviewStatus.UNREVIEWED,
        developer_claim_status=App.DeveloperClaimStatus.UNCLAIMED,
        launch_status=App.LaunchStatus.LIVE,
        pricing_model=App.PricingModel.UNKNOWN,
    )
    app.platforms.add(Platform.objects.get(slug="mcp"))
    return app


def _llm_with(merge: MergeSet) -> MockLLMProvider:
    return MockLLMProvider(responses_queue=[merge])


def _run_pipeline(app: App, merge: MergeSet):
    taxonomy = build_taxonomy_snapshot()
    snapshot = build_app_snapshot(app.pk)
    llm = _llm_with(merge)
    return enrich_existing_draft(snapshot, taxonomy, llm)


# ---------------------------------------------------------------------------
# Hard invariants
# ---------------------------------------------------------------------------
def test_status_fields_never_change(draft_app) -> None:
    """No matter what the LLM proposes, status / editorial / verdict don't move."""
    result = _run_pipeline(
        draft_app,
        MergeSet(
            short_description="Real summary",
            proposed_verdict="Best for ...",
            proposed_launch_status="deprecated",
            proposed_pricing_model="paid",
            capabilities={
                "open_source": CapabilityProposal(
                    value="yes", evidence="GitHub URL"
                )
            },
            add_categories=[CategoryProposal(slug="developer-tools", confidence=0.9)],
        ),
    )
    apply_merge_set(draft_app.pk, result)

    draft_app.refresh_from_db()
    assert draft_app.status == App.AppStatus.DRAFT
    assert draft_app.editorial_review_status == App.EditorialReviewStatus.UNREVIEWED
    assert draft_app.platform_verification_status == App.PlatformVerificationStatus.OFFICIAL
    assert draft_app.developer_claim_status == App.DeveloperClaimStatus.UNCLAIMED
    assert draft_app.verdict == ""  # NEVER written by the agent
    # launch_status / pricing_model also stay where they were.
    assert draft_app.launch_status == App.LaunchStatus.LIVE
    assert draft_app.pricing_model == App.PricingModel.UNKNOWN


def test_empty_text_field_is_filled(draft_app) -> None:
    result = _run_pipeline(
        draft_app, MergeSet(short_description="LLM-proposed summary")
    )
    apply_merge_set(draft_app.pk, result)
    draft_app.refresh_from_db()
    assert draft_app.short_description == "LLM-proposed summary"


def test_existing_text_field_is_NEVER_overwritten(draft_app) -> None:
    draft_app.short_description = "Editor wrote this"
    draft_app.save(update_fields=["short_description"])

    result = _run_pipeline(
        draft_app, MergeSet(short_description="Agent proposal")
    )
    apply_merge_set(draft_app.pk, result)

    draft_app.refresh_from_db()
    assert draft_app.short_description == "Editor wrote this"
    # The disagreement should appear in the review queue.
    assert NeedsReviewQueueEntry.objects.filter(app=draft_app).count() == 1


def test_capability_yes_writes_evidence_note(draft_app) -> None:
    result = _run_pipeline(
        draft_app,
        MergeSet(
            capabilities={
                "open_source": CapabilityProposal(
                    value="yes", evidence="https://github.com/foo/bar"
                )
            }
        ),
    )
    apply_merge_set(draft_app.pk, result)

    cap = AppCapability.objects.get(
        app=draft_app, capability__key="open_source"
    )
    assert cap.value == AppCapability.CapabilityValue.YES
    assert cap.note.startswith("https://github.com/")


def test_existing_capability_yes_is_NEVER_flipped(draft_app) -> None:
    open_source = Capability.objects.get(key="open_source")
    AppCapability.objects.create(
        app=draft_app,
        capability=open_source,
        value=AppCapability.CapabilityValue.NO,
    )

    result = _run_pipeline(
        draft_app,
        MergeSet(
            capabilities={
                "open_source": CapabilityProposal(value="yes", evidence="GH")
            }
        ),
    )
    apply_merge_set(draft_app.pk, result)

    cap = AppCapability.objects.get(app=draft_app, capability=open_source)
    assert cap.value == AppCapability.CapabilityValue.NO  # still no
    queue = NeedsReviewQueueEntry.objects.get(app=draft_app)
    assert any(
        c["key"] == "open_source" and c["value"] == "yes"
        for c in queue.payload["skipped_capability_updates"]
    )


def test_category_is_added_when_missing(draft_app) -> None:
    result = _run_pipeline(
        draft_app,
        MergeSet(
            add_categories=[CategoryProposal(slug="developer-tools", confidence=0.95)]
        ),
    )
    apply_merge_set(draft_app.pk, result)

    cats = list(draft_app.categories.values_list("slug", flat=True))
    assert "developer-tools" in cats


def test_listing_type_additive(draft_app) -> None:
    result = _run_pipeline(
        draft_app,
        MergeSet(
            add_listing_types=[
                ListingTypeProposal(slug="mcp-server", confidence=0.99)
            ]
        ),
    )
    apply_merge_set(draft_app.pk, result)

    types = list(draft_app.listing_types.values_list("slug", flat=True))
    assert "mcp-server" in types


def test_source_row_records_audit_trail(draft_app) -> None:
    result = _run_pipeline(
        draft_app, MergeSet(short_description="Filled")
    )
    apply_merge_set(draft_app.pk, result)

    src = Source.objects.get(
        app=draft_app, external_id=f"agent-enrich:{draft_app.pk}"
    )
    payload = src.payload
    assert payload["llm"]["is_mock"] is True
    assert payload["llm"]["provider"] == "mock"
    assert "outcome" in payload
    assert "validation" in payload
    assert "enriched_at" in payload


def test_queue_entry_carries_evidence_for_editor(draft_app) -> None:
    """Editor must see WHY the agent proposed each thing it queued."""
    result = _run_pipeline(
        draft_app,
        MergeSet(
            proposed_verdict="Pick this for X",
            proposed_launch_status="beta",
        ),
    )
    apply_merge_set(draft_app.pk, result)

    entry = NeedsReviewQueueEntry.objects.get(app=draft_app)
    assert entry.kind == NeedsReviewQueueEntry.Kind.ENRICHED
    assert entry.payload["proposed_verdict"] == "Pick this for X"
    assert entry.payload["proposed_launch_status"] == "beta"


def test_apply_is_atomic_on_inner_failure(draft_app, monkeypatch) -> None:
    """If queue write fails mid-apply, App / capability writes must roll back."""
    result = _run_pipeline(
        draft_app,
        MergeSet(
            short_description="Will be rolled back",
            capabilities={
                "open_source": CapabilityProposal(value="yes", evidence="GH")
            },
        ),
    )

    # Patch the very last step inside apply_merge_set to blow up.
    from apps.agent import persist

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(persist, "_maybe_queue_review", boom)

    with pytest.raises(RuntimeError):
        apply_merge_set(draft_app.pk, result)

    draft_app.refresh_from_db()
    assert draft_app.short_description == ""  # rollback
    assert not AppCapability.objects.filter(
        app=draft_app, capability__key="open_source"
    ).exists()
