"""Integration tests for the Phase 4 re-actualization orchestrator.

Covered:
* Happy path — fresh diff → NeedsReviewQueueEntry written, audit chain
  intact (AgentRun + EnrichmentTask + LLMCallLog).
* Empty diff — Source.last_enriched_at advances, but no queue entry.
* No re-actualizable source — task marked SKIPPED, no LLM call.
* Beat task gated by AGENT_REACTUALIZATION_ENABLED feature flag.
* Selector picks overdue apps in NULLS-FIRST order.
"""
from __future__ import annotations

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.agent.llm.client import MockLLMProvider
from apps.agent.llm.schemas import (
    CategoryProposal,
    EnrichedDraft,
    ListingTypeProposal,
)
from apps.agent.models import (
    AgentRun,
    EnrichmentTask,
    LLMCallLog,
    NeedsReviewQueueEntry,
)
from apps.agent.persist import (
    pending_reactualization_app_ids,
    pick_primary_active_source,
)
from apps.agent.pipeline.fetch import FetchResult
from apps.agent.tasks import reactualize_apps_batch, run_reactualize_app
from apps.catalog.models import (
    App,
    Category,
    Capability,
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
    Category.objects.get_or_create(
        slug="productivity", defaults={"name": "Productivity"}
    )
    Capability.objects.get_or_create(
        key="open_source", defaults={"label": "Open source"}
    )
    ListingType.objects.get_or_create(
        slug="mcp-server", defaults={"name": "MCP Server"}
    )


@pytest.fixture
def published_app(taxonomy_rows) -> App:
    app = App.objects.create(
        name="LiveMCP",
        slug="livemcp",
        short_description="Old short description",
        long_description="Old long description.",
        repo_url="https://github.com/example/livemcp",
        status=App.AppStatus.PUBLISHED,
        editorial_review_status=App.EditorialReviewStatus.REVIEWED,
        platform_verification_status=App.PlatformVerificationStatus.NOT_LISTED,
        developer_claim_status=App.DeveloperClaimStatus.UNCLAIMED,
        launch_status=App.LaunchStatus.LIVE,
        pricing_model=App.PricingModel.FREE,
    )
    app.platforms.add(Platform.objects.get(slug="mcp"))
    app.categories.add(Category.objects.get(slug="developer-tools"))
    app.listing_types.add(ListingType.objects.get(slug="mcp-server"))
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.GITHUB_MCP,
        external_id=f"github_mcp:{app.slug}",
        source_url="https://github.com/example/livemcp",
        is_primary=True,
        fetched_at=timezone.now(),
    )
    return app


def _enriched(**overrides) -> EnrichedDraft:
    base = {
        "name": "LiveMCP",
        "short_description": "Old short description",
        "long_description": "Old long description.",
        "developer_name": "",
        "developer_url": "",
        "official_page_url": "",
        "install_url": "",
        "repo_url": "https://github.com/example/livemcp",
        "listing_types": [ListingTypeProposal(slug="mcp-server", confidence=0.95)],
        "categories": [CategoryProposal(slug="developer-tools", confidence=0.9)],
        "capabilities": {},
        "use_cases": [],
        "launch_status": "live",
        "pricing_model": "free",
        "proposed_verdict": "",
        "scope_summary": "",
    }
    base.update(overrides)
    return EnrichedDraft(**base)


def _fetcher_returning(text: str = "fresh readme"):
    def _f(url: str) -> FetchResult:
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            content_type="text/plain",
            text=text,
            raw_payload={"url": url, "status_code": 200},
        )
    return _f


def test_run_reactualize_app_queues_diff_and_advances_timestamp(
    published_app,
) -> None:
    fresh = _enriched(short_description="Brand-new tagline after upstream rewrite")
    llm = MockLLMProvider(responses_queue=[fresh])

    outcome = run_reactualize_app(
        published_app.pk, llm=llm, fetcher=_fetcher_returning(),
    )

    assert outcome.persist is not None
    assert outcome.persist.queue_entry_id is not None
    entry = NeedsReviewQueueEntry.objects.get(pk=outcome.persist.queue_entry_id)
    assert entry.kind == NeedsReviewQueueEntry.Kind.REACTUALIZED
    assert any(
        fd["field"] == "short_description"
        for fd in entry.payload["fields"]
    )
    source = Source.objects.get(app=published_app)
    assert source.last_enriched_at is not None
    # Audit chain is wired end-to-end.
    assert AgentRun.objects.filter(pk=outcome.run_id).exists()
    task = EnrichmentTask.objects.get(pk=outcome.task_id)
    assert task.status == EnrichmentTask.Status.PERSISTED
    assert LLMCallLog.objects.filter(task=task).exists()


def test_run_reactualize_app_with_empty_diff_writes_no_queue_entry(
    published_app,
) -> None:
    """If the LLM proposes exactly what's already in the catalog, the
    editor doesn't get a noise entry — but the source timestamp still
    advances so the cadence window resets."""
    fresh = _enriched()  # matches catalog exactly
    llm = MockLLMProvider(responses_queue=[fresh])

    outcome = run_reactualize_app(
        published_app.pk, llm=llm, fetcher=_fetcher_returning(),
    )

    assert outcome.persist is not None
    assert outcome.persist.queue_entry_id is None
    assert outcome.persist.is_empty is True
    assert not NeedsReviewQueueEntry.objects.filter(
        kind=NeedsReviewQueueEntry.Kind.REACTUALIZED
    ).exists()
    Source.objects.get(app=published_app).last_enriched_at  # not None
    assert Source.objects.get(app=published_app).last_enriched_at is not None


def test_run_reactualize_app_skips_when_no_active_source(
    taxonomy_rows,
) -> None:
    """Apps without an active re-actualizable Source are skipped without
    spending an LLM call. Manual-curated cards stay editor-only."""
    app = App.objects.create(
        name="Manual app",
        slug="manual-app",
        status=App.AppStatus.PUBLISHED,
        editorial_review_status=App.EditorialReviewStatus.REVIEWED,
    )
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.MANUAL,
        external_id="manual:manual-app",
    )

    llm = MockLLMProvider(responses_queue=[])  # would crash if called
    outcome = run_reactualize_app(app.pk, llm=llm, fetcher=_fetcher_returning())

    assert outcome.skipped_reason == "no_active_reactualizable_source"
    assert outcome.persist is None
    task = EnrichmentTask.objects.get(pk=outcome.task_id)
    assert task.status == EnrichmentTask.Status.SKIPPED
    assert not LLMCallLog.objects.filter(task=task).exists()


def test_run_reactualize_app_skips_when_source_has_no_url(
    taxonomy_rows,
) -> None:
    """Sources without an explicit URL or a payload-derived URL can't
    be re-fetched — skip cleanly rather than hand the fetcher an empty
    string."""
    app = App.objects.create(
        name="Stub",
        slug="stub",
        status=App.AppStatus.PUBLISHED,
        editorial_review_status=App.EditorialReviewStatus.REVIEWED,
    )
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.AGENT_ENRICH,
        external_id="agent-enrich:stub",
        source_url="",
        payload={},
    )

    llm = MockLLMProvider(responses_queue=[])
    outcome = run_reactualize_app(app.pk, llm=llm, fetcher=_fetcher_returning())

    assert outcome.skipped_reason == "source_has_no_url"


@override_settings(AGENT_REACTUALIZATION_ENABLED=False)
def test_batch_no_ops_when_flag_disabled(published_app) -> None:
    result = reactualize_apps_batch()
    assert result == {"skipped": "reactualization_disabled"}
    assert not AgentRun.objects.filter(
        source_type="agent_reactualize_batch"
    ).exists()


@override_settings(AGENT_REACTUALIZATION_ENABLED=True)
def test_batch_dry_run_bypasses_flag(taxonomy_rows) -> None:
    """Manual `--dry-run` probes must work even when the flag is off —
    they're how operators tune the prompt before flipping the switch."""
    with override_settings(AGENT_REACTUALIZATION_ENABLED=False):
        result = reactualize_apps_batch(dry_run=True)
    # No apps to process, but the run row still exists with DRY_RUN.
    assert result == {"processed": 0, "queued": 0, "skipped": 0, "failed": 0}
    run = AgentRun.objects.get(source_type="agent_reactualize_batch")
    assert run.status == AgentRun.Status.DRY_RUN


def test_pending_reactualization_picks_overdue_apps_nulls_first(
    published_app,
) -> None:
    """Selector ordering: never-enriched cards drain ahead of long-tail."""
    other_app = App.objects.create(
        name="Older",
        slug="older",
        status=App.AppStatus.PUBLISHED,
        editorial_review_status=App.EditorialReviewStatus.REVIEWED,
    )
    Source.objects.create(
        app=other_app,
        source_type=Source.SourceType.GITHUB_MCP,
        external_id="github_mcp:older",
        source_url="https://github.com/example/older",
        last_enriched_at=timezone.now() - timezone.timedelta(days=60),
        fetched_at=timezone.now(),
    )

    ids = pending_reactualization_app_ids(interval_days=30, limit=10)
    # published_app's source has NULL last_enriched_at → first.
    assert ids[0] == published_app.pk
    assert other_app.pk in ids


def test_pick_primary_active_source_prefers_primary_then_most_recent(
    published_app,
) -> None:
    Source.objects.create(
        app=published_app,
        source_type=Source.SourceType.AGENT_ENRICH,
        external_id="agent-enrich:livemcp",
        source_url="https://example/agent",
        is_primary=False,
        fetched_at=timezone.now(),
    )

    picked = pick_primary_active_source(published_app.pk)
    # is_primary=True wins, even though the other row is more recent.
    assert picked.source_type == Source.SourceType.GITHUB_MCP
