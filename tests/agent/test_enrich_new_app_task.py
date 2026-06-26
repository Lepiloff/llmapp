from __future__ import annotations

import pytest

from apps.agent.llm.client import MockLLMProvider
from apps.agent.llm.schemas import (
    CapabilityProposal,
    CategoryProposal,
    EnrichedDraft,
    ListingTypeProposal,
)
from apps.agent.models import AgentRun, EnrichmentTask, LLMCallLog
from apps.agent.pipeline.fetch import FetchResult
from apps.agent.tasks import run_enrich_new_app
from apps.catalog.models import App, AppCapability, Capability, Category, ListingType, Platform
from apps.sources.models import Source

pytestmark = pytest.mark.django_db


@pytest.fixture
def taxonomy_rows():
    Platform.objects.create(slug="mcp", name="MCP", public_path="mcp-servers")
    ListingType.objects.create(slug="mcp-server", name="MCP Server")
    Category.objects.create(slug="developer-tools", name="Developer Tools")
    Capability.objects.create(key="open_source", label="Open source")


def _fetcher(url: str) -> FetchResult:
    return FetchResult(
        url=url,
        final_url=url,
        status_code=200,
        content_type="text/markdown",
        text="# Acme MCP\nOpen source MCP server for Acme.",
        raw_payload={"fixture": True},
    )


def _llm() -> MockLLMProvider:
    return MockLLMProvider(
        responses_queue=[
            EnrichedDraft(
                name="Acme MCP",
                short_description="Open source MCP server for Acme.",
                long_description="Acme MCP lets assistants connect to Acme.",
                developer_name="Acme",
                official_page_url="https://github.com/acme/acme-mcp",
                repo_url="https://github.com/acme/acme-mcp",
                listing_types=[
                    ListingTypeProposal(slug="mcp-server", confidence=0.95)
                ],
                categories=[
                    CategoryProposal(slug="developer-tools", confidence=0.9)
                ],
                capabilities={
                    "open_source": CapabilityProposal(
                        value="yes",
                        evidence="Open source MCP server",
                        confidence=0.95,
                    )
                },
                proposed_verdict="Useful for Acme users.",
                scope_summary="Reads Acme data.",
            )
        ]
    )


def test_run_enrich_new_app_dry_run_records_audit_without_app(taxonomy_rows) -> None:
    outcome = run_enrich_new_app(
        "https://github.com/acme/acme-mcp",
        source_type=Source.SourceType.GITHUB_MCP,
        external_id="github:acme/acme-mcp",
        llm=_llm(),
        fetcher=_fetcher,
        dry_run=True,
    )

    assert outcome.dry_run is True
    assert outcome.persist is None
    assert not App.objects.exists()
    run = AgentRun.objects.get(pk=outcome.run_id)
    assert run.status == AgentRun.Status.DRY_RUN
    task = EnrichmentTask.objects.get(pk=outcome.task_id)
    assert task.status == EnrichmentTask.Status.DRY_RUN
    assert task.diff_summary["sanitized_draft"]["name"] == "Acme MCP"
    assert LLMCallLog.objects.filter(task=task, prompt_version="enrich-new-v1.0").exists()


def test_run_enrich_new_app_apply_creates_draft_and_source(taxonomy_rows) -> None:
    outcome = run_enrich_new_app(
        "https://github.com/acme/acme-mcp",
        source_type=Source.SourceType.GITHUB_MCP,
        external_id="github:acme/acme-mcp",
        llm=_llm(),
        fetcher=_fetcher,
        dry_run=False,
    )

    assert outcome.persist is not None
    assert outcome.persist.outcome == "new"
    app = App.objects.get(pk=outcome.persist.app_id)
    assert app.status == App.AppStatus.DRAFT
    assert app.editorial_review_status == App.EditorialReviewStatus.UNREVIEWED
    assert app.verdict == ""
    assert app.platforms.filter(slug="mcp").exists()
    assert app.categories.filter(slug="developer-tools").exists()
    cap = AppCapability.objects.get(app=app, capability__key="open_source")
    assert cap.value == AppCapability.CapabilityValue.YES
    source = Source.objects.get(app=app, source_type=Source.SourceType.GITHUB_MCP)
    assert source.payload["proposed_verdict"] == "Useful for Acme users."
    assert "agent_enrichment" in source.payload
    task = EnrichmentTask.objects.get(pk=outcome.task_id)
    assert task.app == app
    assert task.status == EnrichmentTask.Status.PERSISTED
