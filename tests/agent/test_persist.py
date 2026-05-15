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
    EnrichedDraft,
    ListingTypeProposal,
    MergeSet,
)
from apps.agent.llm.client import MockLLMProvider
from apps.agent.models import NeedsReviewQueueEntry
from apps.agent.persist import (
    AppNotEligibleError,
    _derive_platforms,
    _enriched_to_app_draft,
    apply_merge_set,
    assert_app_is_eligible,
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
    """A DRAFT App with an MCP_REGISTRY Source — the Phase 1 happy-path target.

    Carrying the Source row in the fixture (instead of relying on
    ``--allow-non-mcp``) means every test exercises the real production
    eligibility path. The dedicated negative-path test
    ``test_assert_app_is_eligible_rejects_non_mcp_draft`` covers the
    case where no eligible source exists.
    """
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
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.MCP_REGISTRY,
        external_id=f"mcp-registry:{app.slug}",
        is_primary=True,
    )
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


# ---------------------------------------------------------------------------
# Phase 1 follow-up regressions (review High #1, #2, #3 + Low/Medium)
# ---------------------------------------------------------------------------
def test_assert_app_is_eligible_rejects_published(draft_app) -> None:
    """``assert_app_is_eligible`` is the fast-fail before any LLM token spend."""
    App.objects.filter(pk=draft_app.pk).update(status=App.AppStatus.PUBLISHED)
    with pytest.raises(AppNotEligibleError) as excinfo:
        assert_app_is_eligible(draft_app.pk)
    assert excinfo.value.app_id == draft_app.pk
    assert excinfo.value.status == App.AppStatus.PUBLISHED


def test_assert_app_is_eligible_rejects_missing() -> None:
    with pytest.raises(AppNotEligibleError):
        assert_app_is_eligible(99999999)


def test_assert_app_is_eligible_rejects_non_mcp_draft(taxonomy_rows) -> None:
    """Phase 1 is strictly MCP-registry-sourced DRAFT cards.

    A DRAFT manually entered by an editor (no Source, or Source of type
    other than MCP_REGISTRY) must be rejected by the eligibility check.
    """
    app = App.objects.create(
        name="ManuallyEntered",
        slug="manual",
        short_description="",
        status=App.AppStatus.DRAFT,
    )
    # No Source row at all → ineligible.
    with pytest.raises(AppNotEligibleError) as excinfo:
        assert_app_is_eligible(app.pk)
    assert "MCP Registry Source" in str(excinfo.value)
    assert "--allow-non-mcp" in str(excinfo.value)

    # Even a non-MCP Source (e.g. SUBMISSION) is still ineligible.
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.SUBMISSION,
        external_id=f"submission:{app.pk}",
    )
    with pytest.raises(AppNotEligibleError):
        assert_app_is_eligible(app.pk)


def test_assert_app_is_eligible_allows_non_mcp_when_overridden(
    taxonomy_rows,
) -> None:
    """``--allow-non-mcp`` (operator escape hatch) bypasses the source check."""
    app = App.objects.create(
        name="ManuallyEntered",
        slug="manual-2",
        short_description="",
        status=App.AppStatus.DRAFT,
    )
    # Status still DRAFT — that's a hard requirement no override touches.
    assert_app_is_eligible(app.pk, allow_non_mcp=True)


def test_allow_non_mcp_override_still_requires_draft_status(
    taxonomy_rows,
) -> None:
    """The override widens the source-type filter only, NOT the status guard.

    PUBLISHED apps remain off-limits even with --allow-non-mcp.
    """
    app = App.objects.create(
        name="Published",
        slug="published",
        short_description="x",
        status=App.AppStatus.PUBLISHED,
    )
    with pytest.raises(AppNotEligibleError) as excinfo:
        assert_app_is_eligible(app.pk, allow_non_mcp=True)
    assert excinfo.value.status == App.AppStatus.PUBLISHED


def test_pending_enrichment_app_ids_filters_by_mcp_source(
    taxonomy_rows,
) -> None:
    """Selector returns only DRAFT apps with an MCP_REGISTRY Source.

    Manual/submission DRAFT cards must NOT be picked up by batch.
    """
    from apps.agent.persist import pending_enrichment_app_ids

    mcp_app = App.objects.create(
        name="FromRegistry",
        slug="mcp-app",
        short_description="",
        status=App.AppStatus.DRAFT,
    )
    Source.objects.create(
        app=mcp_app,
        source_type=Source.SourceType.MCP_REGISTRY,
        external_id="mcp-registry:from-registry",
    )

    manual_app = App.objects.create(
        name="ManualEntry",
        slug="manual-3",
        short_description="",
        status=App.AppStatus.DRAFT,
    )
    Source.objects.create(
        app=manual_app,
        source_type=Source.SourceType.MANUAL,
        external_id="manual:from-editor",
    )

    pending = list(pending_enrichment_app_ids())
    assert mcp_app.pk in pending
    assert manual_app.pk not in pending


def test_apply_merge_set_refuses_published_under_lock(draft_app) -> None:
    """Defense-in-depth: even if the upstream guard is bypassed, the persist
    layer must still refuse to write against a non-DRAFT row."""
    # Build a valid result against the snapshot...
    result = _run_pipeline(
        draft_app,
        MergeSet(
            short_description="Should NOT land",
            capabilities={
                "open_source": CapabilityProposal(value="yes", evidence="GH")
            },
        ),
    )
    # ...then publish the App behind our back (simulating a race).
    App.objects.filter(pk=draft_app.pk).update(status=App.AppStatus.PUBLISHED)

    with pytest.raises(AppNotEligibleError):
        apply_merge_set(draft_app.pk, result)

    draft_app.refresh_from_db()
    assert draft_app.status == App.AppStatus.PUBLISHED
    assert draft_app.short_description == ""
    # No audit Source row should exist either — the entire transaction
    # rolled back.
    assert not Source.objects.filter(
        app=draft_app, external_id=f"agent-enrich:{draft_app.pk}"
    ).exists()
    assert not AppCapability.objects.filter(
        app=draft_app, capability__key="open_source"
    ).exists()


def test_field_race_editor_wins_between_snapshot_and_apply(draft_app) -> None:
    """The classic stale-snapshot scenario.

    1. snapshot built with short_description=''
    2. Pipeline returns plan including a field_update for short_description
    3. *Editor writes the field via admin* — committed before our transaction
    4. apply_merge_set must NOT overwrite the editor's value
    """
    result = _run_pipeline(
        draft_app, MergeSet(short_description="Agent proposal that loses the race")
    )
    # Plan really does have the field update — guarantee the test is
    # exercising the race, not a no-op plan.
    assert any(
        u.field == "short_description" for u in result.outcome.plan.field_updates
    )

    # Simulate editor write committed between snapshot build and apply.
    App.objects.filter(pk=draft_app.pk).update(
        short_description="Editor wrote this between snapshot and apply"
    )

    persisted = apply_merge_set(draft_app.pk, result)

    draft_app.refresh_from_db()
    assert (
        draft_app.short_description
        == "Editor wrote this between snapshot and apply"
    ), "RACE: agent overwrote editor's freshly-committed value"
    assert "short_description" not in persisted.fields_written


def test_capability_race_editor_wins_between_snapshot_and_apply(draft_app) -> None:
    """Same race scenario, capability side.

    1. snapshot built with open_source=UNKNOWN
    2. Pipeline returns plan with capability_update {open_source: yes}
    3. Editor sets open_source=NO via admin before apply
    4. apply_merge_set must NOT flip NO → YES
    """
    result = _run_pipeline(
        draft_app,
        MergeSet(
            capabilities={
                "open_source": CapabilityProposal(value="yes", evidence="GH README")
            }
        ),
    )
    assert any(
        u.key == "open_source" for u in result.outcome.plan.capability_updates
    )

    # Editor's NO call lands before apply commits.
    open_source = Capability.objects.get(key="open_source")
    AppCapability.objects.create(
        app=draft_app,
        capability=open_source,
        value=AppCapability.CapabilityValue.NO,
    )

    persisted = apply_merge_set(draft_app.pk, result)

    cap = AppCapability.objects.get(app=draft_app, capability=open_source)
    assert cap.value == AppCapability.CapabilityValue.NO, (
        "RACE: agent flipped editor's NO to YES"
    )
    assert "open_source" not in persisted.capabilities_written


def test_audit_source_uses_agent_enrich_source_type(draft_app) -> None:
    """Distinguish agent provenance from manual entry (review Low/Medium)."""
    result = _run_pipeline(draft_app, MergeSet(short_description="x"))
    apply_merge_set(draft_app.pk, result)

    src = Source.objects.get(
        app=draft_app, external_id=f"agent-enrich:{draft_app.pk}"
    )
    assert src.source_type == Source.SourceType.AGENT_ENRICH


def test_search_vector_refresh_is_scheduled_on_field_update(
    draft_app, monkeypatch
) -> None:
    """``App.objects.update()`` skips post_save → search vector signal won't
    fire. The persist layer must schedule the refresh manually."""
    captured: list = []

    # Capture every on_commit callback registered while applying.
    real_on_commit = __import__(
        "django.db.transaction", fromlist=["on_commit"]
    ).on_commit

    def capture_on_commit(callback, using=None, robust=False):
        captured.append(callback)
        return real_on_commit(callback, using=using, robust=robust)

    monkeypatch.setattr(
        "django.db.transaction.on_commit", capture_on_commit
    )
    # Also patch where it's bound inside persist.py.
    from apps.agent import persist as persist_module
    monkeypatch.setattr(persist_module.transaction, "on_commit", capture_on_commit)

    # Stub the actual celery task so the captured callback doesn't try
    # to hit the broker / DB rebuild in tests.
    from apps.search import tasks as search_tasks
    invoked: list[int] = []

    def fake_delay(app_id):
        invoked.append(app_id)

    monkeypatch.setattr(search_tasks.refresh_search_vector_task, "delay", fake_delay)

    result = _run_pipeline(draft_app, MergeSet(short_description="Filled by agent"))
    apply_merge_set(draft_app.pk, result)

    # At least one on_commit callback was scheduled.
    assert len(captured) >= 1, "no search-vector refresh was scheduled"
    # Manually run the captured callbacks (test transaction rolls back
    # so the real on_commit hooks never fire).
    for cb in captured:
        cb()
    assert draft_app.pk in invoked


def test_no_field_updates_means_no_fields_written(draft_app) -> None:
    """When the LLM proposes no field updates, ``PersistResult.fields_written``
    is empty — the gate that decides whether the agent schedules its own
    refresh.

    (We don't assert on the underlying ``transaction.on_commit`` calls
    directly: ``AppCapability.objects.create`` fires ``post_save``,
    which independently schedules a refresh via the catalog signals.
    That's correct behaviour, not the agent's responsibility.)
    """
    result = _run_pipeline(
        draft_app,
        MergeSet(
            capabilities={
                "open_source": CapabilityProposal(value="yes", evidence="GH README")
            }
        ),
    )
    persisted = apply_merge_set(draft_app.pk, result)
    assert persisted.fields_written == []


def test_search_refresh_scheduled_on_category_addition(
    draft_app, monkeypatch
) -> None:
    """Adding a category through the agent path must trigger the
    ``m2m_changed`` → ``_schedule_refresh`` → ``refresh_search_vector_task``
    chain. The category name is part of ``App.search_index_text``, so a
    missing refresh would silently drop the new category from search."""
    from apps.catalog import signals as catalog_signals

    refresh_calls: list[int] = []
    real_schedule = catalog_signals._schedule_refresh

    def capture(app_id: int) -> None:
        refresh_calls.append(app_id)
        real_schedule(app_id)

    monkeypatch.setattr(catalog_signals, "_schedule_refresh", capture)

    result = _run_pipeline(
        draft_app,
        MergeSet(
            add_categories=[CategoryProposal(slug="developer-tools", confidence=0.99)]
        ),
    )
    apply_merge_set(draft_app.pk, result)

    # The signal handler binds to the module's _schedule_refresh by name
    # at call time, so the monkeypatch is picked up. m2m_changed fires
    # once per add() invocation.
    assert draft_app.pk in refresh_calls


def test_search_refresh_scheduled_on_use_case_addition(
    draft_app, monkeypatch
) -> None:
    """Same invariant as categories: use_cases.add() must reach the
    signal handler so the new use-case slug lands in ``search_index_text``."""
    from apps.catalog import signals as catalog_signals

    refresh_calls: list[int] = []
    real_schedule = catalog_signals._schedule_refresh

    def capture(app_id: int) -> None:
        refresh_calls.append(app_id)
        real_schedule(app_id)

    monkeypatch.setattr(catalog_signals, "_schedule_refresh", capture)

    result = _run_pipeline(
        draft_app,
        MergeSet(add_use_cases=["Turn notes into slides"]),
    )
    apply_merge_set(draft_app.pk, result)

    assert draft_app.pk in refresh_calls


# ---------------------------------------------------------------------------
# F5 — listing-type → platform inference for agent-discovered drafts.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("listing_slugs", "expected"),
    [
        (["mcp-server"], ["mcp"]),
        (["chatgpt-app"], ["chatgpt"]),
        (["claude-connector"], ["claude"]),
        (["interactive-claude-app"], ["claude"]),
        (["gemini-app"], ["gemini"]),
        (["enterprise-agent"], ["enterprise"]),
        # Two Claude-family listings collapse to a single Platform row.
        (["claude-connector", "interactive-claude-app"], ["claude"]),
        # Multiple distinct listing types yield multiple platforms in
        # encounter order (deterministic for snapshot assertions).
        (
            ["mcp-server", "chatgpt-app", "gemini-app"],
            ["mcp", "chatgpt", "gemini"],
        ),
        # Unknown listing type — Trigger.dev #12 case — yields no platform
        # so the publish-checklist blocks until an editor sets one.
        (["agent-platform"], []),
        ([], []),
    ],
)
def test_derive_platforms(listing_slugs, expected) -> None:
    assert _derive_platforms(listing_slugs) == expected


def test_enriched_to_app_draft_propagates_platforms_from_listing_types() -> None:
    """The pure-Python translation must carry derived platforms onto AppDraft
    so ``upsert_app_from_draft`` can attach AppPlatform rows on first persist.
    Regression: F5 previously hard-coded ``mcp-server → mcp`` and dropped
    every other listing-type's platform, blocking publish on Trigger.dev #12
    plus any future non-MCP discovery target."""
    enriched = EnrichedDraft(
        name="Example ChatGPT App",
        short_description="x",
        listing_types=[
            ListingTypeProposal(slug="chatgpt-app", confidence=0.95),
        ],
    )
    draft = _enriched_to_app_draft(
        enriched, external_id="ext-1", raw_payload={}
    )
    assert draft.platforms == ["chatgpt"]
    assert draft.listing_types == ["chatgpt-app"]
