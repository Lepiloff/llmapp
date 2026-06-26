from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.test import override_settings

from apps.agent.llm.client import MockLLMProvider
from apps.agent.llm.schemas import (
    CapabilityProposal,
    CategoryProposal,
    DiscoveryDecision,
    EnrichedDraft,
    ListingTypeProposal,
)
from apps.agent.models import AgentRun, EnrichmentTask, LLMCallLog
from apps.agent.pipeline.fetch import FetchResult
from apps.agent.sources.base import DiscoveryCandidate
from apps.agent.tasks import _run_discovery_batch, enrich_pending_drafts_batch
from apps.catalog.models import App, Capability, Category, ListingType, Platform
from apps.sources.models import Source

pytestmark = pytest.mark.django_db


def _candidate() -> DiscoveryCandidate:
    return DiscoveryCandidate(
        external_id="github:acme/acme-mcp",
        url="https://github.com/acme/acme-mcp",
        title="acme/acme-mcp",
        summary="Acme MCP server",
        source_name="github_mcp",
        raw_payload={
            "full_name": "acme/acme-mcp",
            "html_url": "https://github.com/acme/acme-mcp",
            "description": "Acme MCP server",
        },
    )


def _llm(is_relevant: bool = True) -> MockLLMProvider:
    return MockLLMProvider(
        responses_queue=[
            DiscoveryDecision(
                is_relevant=is_relevant,
                canonical_url="https://github.com/acme/acme-mcp",
                reason="MCP server repository",
                confidence=0.95,
            )
        ]
    )


def _enrich_llm() -> MockLLMProvider:
    return MockLLMProvider(
        responses_queue=[
            EnrichedDraft(
                name="Acme MCP",
                short_description="Open source MCP server for Acme.",
                official_page_url="https://github.com/acme/acme-mcp",
                repo_url="https://github.com/acme/acme-mcp",
                listing_types=[ListingTypeProposal(slug="mcp-server", confidence=0.95)],
                categories=[CategoryProposal(slug="developer-tools", confidence=0.9)],
                capabilities={
                    "open_source": CapabilityProposal(
                        value="yes",
                        evidence="Open source MCP server",
                        confidence=0.95,
                    )
                },
                proposed_verdict="Useful for Acme users.",
            )
        ]
    )


def _fetcher(url: str) -> FetchResult:
    return FetchResult(
        url=url,
        final_url=url,
        status_code=200,
        content_type="text/markdown",
        text="# Acme MCP\nOpen source MCP server for Acme.",
        raw_payload={"fixture": True},
    )


def test_discovery_dry_run_records_audit_rows_without_persisting() -> None:
    result = _run_discovery_batch(
        source_flag="github_mcp",
        source_label=Source.SourceType.GITHUB_MCP,
        candidates=[_candidate()],
        llm=_llm(),
        dry_run=True,
        persist_github_drafts=True,
    )

    assert result["seen"] == 1
    assert result["relevant"] == 1
    assert result["persisted"] == 0
    run = AgentRun.objects.get(source_type=Source.SourceType.GITHUB_MCP)
    assert run.status == AgentRun.Status.DRY_RUN
    task = EnrichmentTask.objects.get(run=run)
    assert task.status == EnrichmentTask.Status.DRY_RUN
    assert task.app_id is None
    assert task.diff_summary["candidate"]["external_id"] == "github:acme/acme-mcp"
    assert LLMCallLog.objects.filter(task=task, is_mock=True).count() == 1
    assert not App.objects.filter(slug="acme-mcp").exists()


def test_discovery_skips_non_relevant_candidate() -> None:
    result = _run_discovery_batch(
        source_flag="rss",
        source_label=Source.SourceType.RSS_DISCOVERY,
        candidates=[_candidate()],
        llm=_llm(is_relevant=False),
        dry_run=True,
    )

    assert result["seen"] == 1
    assert result["relevant"] == 0
    task = EnrichmentTask.objects.get()
    assert task.status == EnrichmentTask.Status.SKIPPED


@override_settings(AGENT_SOURCES_ENABLED=["github_mcp"])
def test_github_discovery_apply_persists_minimal_draft() -> None:
    Platform.objects.create(slug="mcp", name="MCP", public_path="mcp-servers")
    ListingType.objects.create(slug="mcp-server", name="MCP Server")
    Capability.objects.create(key="open_source", label="Open source")

    result = _run_discovery_batch(
        source_flag="github_mcp",
        source_label=Source.SourceType.GITHUB_MCP,
        candidates=[_candidate()],
        llm=_llm(),
        dry_run=False,
        persist_github_drafts=True,
    )

    assert result["persisted"] == 1
    app = App.objects.get(slug="acme-mcp")
    assert app.status == App.AppStatus.DRAFT
    assert app.repo_url == "https://github.com/acme/acme-mcp"
    assert app.platforms.filter(slug="mcp").exists()
    assert Source.objects.filter(
        app=app,
        source_type=Source.SourceType.GITHUB_MCP,
        external_id="github:acme/acme-mcp",
    ).exists()


def test_discovery_apply_is_feature_flag_guarded() -> None:
    result = _run_discovery_batch(
        source_flag="github_mcp",
        source_label=Source.SourceType.GITHUB_MCP,
        candidates=[_candidate()],
        llm=_llm(),
        dry_run=False,
        persist_github_drafts=True,
    )

    assert result == {"skipped": "source_disabled", "source": "github_mcp"}
    assert not AgentRun.objects.exists()


def test_pending_enrichment_apply_is_feature_flag_guarded() -> None:
    result = enrich_pending_drafts_batch()

    assert result == {"skipped": "source_disabled", "source": "enrich_pending"}
    assert not AgentRun.objects.exists()


@override_settings(
    AGENT_SOURCES_ENABLED=["enrich_pending"],
    AGENT_ENRICH_PENDING_SOURCE_TYPES=["gemini_extensions"],
)
def test_pending_enrichment_batch_uses_source_type_allowlist(monkeypatch) -> None:
    mcp_app = App.objects.create(
        name="MCP Pending",
        slug="mcp-pending",
        status=App.AppStatus.DRAFT,
    )
    Source.objects.create(
        app=mcp_app,
        source_type=Source.SourceType.MCP_REGISTRY,
        external_id="mcp-registry:mcp-pending",
    )
    gemini_app = App.objects.create(
        name="Gemini Pending",
        slug="gemini-pending",
        status=App.AppStatus.DRAFT,
    )
    Source.objects.create(
        app=gemini_app,
        source_type=Source.SourceType.GEMINI_EXTENSIONS,
        external_id="gemini:gemini-pending",
    )

    from apps.agent import tasks as agent_tasks

    processed: list[int] = []

    def fake_run_enrich_existing_draft(app_id, *, dry_run, run):
        processed.append(app_id)
        return SimpleNamespace(
            result=SimpleNamespace(
                call_meta=SimpleNamespace(cost_usd=0),
            ),
            persist=SimpleNamespace(queue_entry_id=None),
        )

    monkeypatch.setattr(
        agent_tasks,
        "run_enrich_existing_draft",
        fake_run_enrich_existing_draft,
    )

    result = enrich_pending_drafts_batch(limit=10)

    assert processed == [gemini_app.pk]
    assert result == {
        "processed": 1,
        "failed": 0,
        "queued": 0,
        "source_types": ["gemini_extensions"],
    }


@override_settings(AGENT_SOURCES_ENABLED=["github_mcp"])
def test_discovery_apply_can_run_full_new_app_enrichment() -> None:
    Platform.objects.create(slug="mcp", name="MCP", public_path="mcp-servers")
    ListingType.objects.create(slug="mcp-server", name="MCP Server")
    Category.objects.create(slug="developer-tools", name="Developer Tools")
    Capability.objects.create(key="open_source", label="Open source")

    result = _run_discovery_batch(
        source_flag="github_mcp",
        source_label=Source.SourceType.GITHUB_MCP,
        candidates=[_candidate()],
        llm=_llm(),
        dry_run=False,
        enrich_relevant=True,
        enrich_llm=_enrich_llm(),
        fetcher=_fetcher,
    )

    assert result["seen"] == 1
    assert result["relevant"] == 1
    assert result["persisted"] == 1
    app = App.objects.get(slug="acme-mcp")
    assert app.short_description == "Open source MCP server for Acme."
    assert app.verdict == ""
    tasks = list(EnrichmentTask.objects.order_by("id"))
    assert [task.status for task in tasks] == [
        EnrichmentTask.Status.PENDING,
        EnrichmentTask.Status.PERSISTED,
    ]
    assert LLMCallLog.objects.count() == 2
