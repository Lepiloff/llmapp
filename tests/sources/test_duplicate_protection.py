from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command

from apps.agent.models import NeedsReviewQueueEntry
from apps.catalog.models import (
    App,
    AppCapability,
    AppPlatform,
    Capability,
    Category,
    ListingType,
    Platform,
)
from apps.sources.base import AppDraft
from apps.sources.models import DuplicateCandidate, Source
from apps.sources.upsert import upsert_app_from_draft

pytestmark = pytest.mark.django_db


def _existing_app(**kwargs) -> App:
    defaults = {
        "name": "Acme Search",
        "slug": "acme-search",
        "short_description": "Search across Acme workspaces",
        "developer_name": "Acme",
        "developer_url": "https://acme.example",
        "official_page_url": "https://acme.example/apps/search",
        "install_url": "",
        "repo_url": "",
        "status": App.AppStatus.DRAFT,
    }
    defaults.update(kwargs)
    return App.objects.create(**defaults)


def _draft(**kwargs) -> AppDraft:
    defaults = {
        "name": "Acme Search",
        "slug_hint": "acme-search",
        "short_description": "Search across Acme workspaces",
        "developer_name": "Acme",
        "developer_url": "https://acme.example",
        "official_page_url": "https://mcpapp.net/app/acme-search",
        "external_id": "acme-search-chatgpt",
        "raw_payload": {"source": "test"},
    }
    defaults.update(kwargs)
    return AppDraft(**defaults)


def test_exact_normalized_url_attaches_source_without_new_app() -> None:
    existing = _existing_app(
        install_url="https://www.acme.example/apps/search/?utm=old",
    )
    draft = _draft(
        name="Acme Search",
        slug_hint="acme-search-chatgpt",
        install_url="https://acme.example/apps/search/",
        external_id="chatgpt-acme-search",
    )

    outcome = upsert_app_from_draft(draft, Source.SourceType.CHATGPT_UNOFFICIAL)

    assert outcome == "skipped"
    assert App.objects.count() == 1
    source = Source.objects.get(
        source_type=Source.SourceType.CHATGPT_UNOFFICIAL,
        external_id="chatgpt-acme-search",
    )
    assert source.app == existing
    assert DuplicateCandidate.objects.count() == 0


def test_github_repo_identity_attaches_cross_source_match() -> None:
    existing = _existing_app(
        name="Acme MCP",
        slug="acme-mcp",
        repo_url="https://github.com/Acme/acme-mcp.git",
    )
    draft = _draft(
        name="Acme MCP Server",
        slug_hint="acme-mcp-server",
        repo_url="https://github.com/acme/acme-mcp/tree/main",
        external_id="github-acme-mcp",
    )

    outcome = upsert_app_from_draft(draft, Source.SourceType.GITHUB_MCP)

    assert outcome == "skipped"
    assert App.objects.count() == 1
    assert Source.objects.get(
        source_type=Source.SourceType.GITHUB_MCP,
        external_id="github-acme-mcp",
    ).app == existing


def test_mcp_registry_same_repo_creates_separate_apps() -> None:
    Platform.objects.get_or_create(
        slug="mcp", defaults={"name": "MCP", "public_path": "mcp-servers"}
    )
    first = _draft(
        name="Acme Alpha MCP",
        slug_hint="acme-alpha-mcp",
        repo_url="https://github.com/acme/mcp-monorepo",
        official_page_url="https://github.com/acme/mcp-monorepo",
        external_id="com.acme/alpha",
        platforms=["mcp"],
        listing_types=["mcp-server"],
    )
    second = _draft(
        name="Acme Beta MCP",
        slug_hint="acme-beta-mcp",
        repo_url="https://github.com/acme/mcp-monorepo",
        official_page_url="https://github.com/acme/mcp-monorepo",
        external_id="com.acme/beta",
        platforms=["mcp"],
        listing_types=["mcp-server"],
    )

    assert upsert_app_from_draft(first, Source.SourceType.MCP_REGISTRY) == "new"
    assert upsert_app_from_draft(second, Source.SourceType.MCP_REGISTRY) == "new"

    assert App.objects.count() == 2
    assert Source.objects.get(
        source_type=Source.SourceType.MCP_REGISTRY,
        external_id="com.acme/alpha",
    ).app != Source.objects.get(
        source_type=Source.SourceType.MCP_REGISTRY,
        external_id="com.acme/beta",
    ).app


def test_weak_domain_name_match_creates_review_candidate() -> None:
    existing = _existing_app()
    draft = _draft(
        name="Acme Search AI",
        slug_hint="acme-search-ai",
        developer_url="",
        official_page_url="https://acme.example/chatgpt/search",
        external_id="weak-acme-search",
    )

    outcome = upsert_app_from_draft(draft, Source.SourceType.CHATGPT_UNOFFICIAL)

    assert outcome == "new"
    assert App.objects.count() == 2
    new_app = App.objects.get(slug="acme-search-ai")
    candidate = DuplicateCandidate.objects.get(app=new_app)
    assert candidate.candidate_app == existing
    assert candidate.status == DuplicateCandidate.Status.PENDING
    assert candidate.match_reason == "shared_domain_similar_name"
    assert candidate.source == Source.objects.get(app=new_app)


def test_directory_domain_name_match_does_not_create_review_candidate() -> None:
    _existing_app(
        name="Airtable",
        slug="airtable",
        developer_url="",
        official_page_url="https://claude.com/connectors/airtable",
    )
    draft = _draft(
        name="Airwallex",
        slug_hint="airwallex",
        developer_url="",
        official_page_url="https://claude.com/connectors/airwallex",
        external_id="claude-airwallex",
    )

    outcome = upsert_app_from_draft(draft, Source.SourceType.CLAUDE_CONNECTORS)

    assert outcome == "new"
    assert App.objects.count() == 2
    assert DuplicateCandidate.objects.count() == 0


def test_dismiss_directory_duplicate_candidates_command_dry_run_then_apply() -> None:
    airtable = _existing_app(
        name="Airtable",
        slug="airtable",
        official_page_url="https://claude.com/connectors/airtable",
    )
    airwallex = _existing_app(
        name="Airwallex",
        slug="airwallex",
        official_page_url="https://claude.com/connectors/airwallex",
    )
    source = Source.objects.create(
        app=airwallex,
        source_type=Source.SourceType.CLAUDE_CONNECTORS,
        external_id="claude:airwallex",
    )
    weak = DuplicateCandidate.objects.create(
        app=airwallex,
        candidate_app=airtable,
        source=source,
        match_reason="shared_domain_similar_name",
        score=0.7,
        evidence={"domains": ["claude.com"], "name_similarity": 0.7},
    )
    strong_name = DuplicateCandidate.objects.create(
        app=_existing_app(name="Atlassian Rovo", slug="atlassian-rovo"),
        candidate_app=_existing_app(name="Atlassian Rovo", slug="atlassian"),
        match_reason="shared_domain_similar_name",
        score=0.98,
        evidence={"domains": ["claude.com"], "name_similarity": 0.98},
    )

    out = StringIO()
    call_command(
        "dismiss_directory_duplicate_candidates",
        "--limit=10",
        "--indent=0",
        stdout=out,
    )
    dry_run = json.loads(out.getvalue())
    weak.refresh_from_db()
    assert dry_run["would_dismiss"] == 1
    assert dry_run["dismissed"] == 0
    assert weak.status == DuplicateCandidate.Status.PENDING

    out = StringIO()
    call_command(
        "dismiss_directory_duplicate_candidates",
        "--limit=10",
        "--apply",
        "--indent=0",
        stdout=out,
    )
    applied = json.loads(out.getvalue())
    weak.refresh_from_db()
    strong_name.refresh_from_db()
    assert applied["would_dismiss"] == 1
    assert applied["dismissed"] == 1
    assert weak.status == DuplicateCandidate.Status.DISMISSED
    assert weak.resolved_at is not None
    assert strong_name.status == DuplicateCandidate.Status.PENDING


def test_merge_cross_platform_duplicates_command_dry_run_then_apply() -> None:
    chatgpt, _ = Platform.objects.get_or_create(
        slug="chatgpt",
        defaults={"name": "ChatGPT", "public_path": "chatgpt-apps"},
    )
    claude, _ = Platform.objects.get_or_create(
        slug="claude",
        defaults={"name": "Claude", "public_path": "claude-connectors"},
    )
    chatgpt_listing = ListingType.objects.create(
        slug="chatgpt-app",
        name="ChatGPT App",
    )
    claude_listing = ListingType.objects.create(
        slug="claude-connector",
        name="Claude Connector",
    )
    category = Category.objects.create(slug="productivity", name="Productivity")
    capability = Capability.objects.create(key="read-data", label="Read data")
    canonical = _existing_app(
        name="Atlassian Rovo",
        slug="atlassian-rovo",
        short_description="Read/write Jira and Confluence",
        official_page_url=(
            "https://chatgpt.com/apps/atlassian-rovo/"
            "connector_692de805e3ec8191834719067174a384"
        ),
        install_url=(
            "https://chatgpt.com/apps/atlassian-rovo/"
            "connector_692de805e3ec8191834719067174a384"
        ),
    )
    canonical.platforms.add(chatgpt, through_defaults={"official_directory_url": canonical.install_url})
    canonical.platforms.add(claude)
    canonical.listing_types.add(chatgpt_listing)
    Source.objects.create(
        app=canonical,
        source_type=Source.SourceType.CHATGPT_UNOFFICIAL,
        external_id="mcpapp-chatgpt:atlassian-rovo",
        source_url=canonical.install_url,
        is_primary=True,
    )
    duplicate = _existing_app(
        name="Atlassian Rovo",
        slug="atlassian",
        short_description="Access Jira and Confluence from Claude",
        official_page_url="https://claude.com/connectors/atlassian",
        install_url="",
        platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
    )
    duplicate.platforms.add(
        claude,
        through_defaults={
            "official_directory_url": "https://claude.com/connectors/atlassian",
            "scope_summary": "Access Jira and Confluence from Claude.",
        },
    )
    duplicate.listing_types.add(claude_listing)
    duplicate.categories.add(category)
    AppCapability.objects.create(
        app=duplicate,
        capability=capability,
        value=AppCapability.CapabilityValue.YES,
        note="Can read Jira and Confluence data.",
    )
    claude_source = Source.objects.create(
        app=duplicate,
        source_type=Source.SourceType.CLAUDE_CONNECTORS,
        external_id="claude:atlassian",
        source_url="https://claude.com/connectors/atlassian",
        is_primary=True,
    )
    review = NeedsReviewQueueEntry.objects.create(
        app=duplicate,
        kind=NeedsReviewQueueEntry.Kind.ENRICHED,
        payload={"proposed_scope_summary": "Access Jira and Confluence."},
    )
    pending = DuplicateCandidate.objects.create(
        app=canonical,
        candidate_app=duplicate,
        source=claude_source,
        match_reason="similar_name",
        score=1.0,
        evidence={"name_similarity": 1.0},
    )

    out = StringIO()
    call_command(
        "merge_cross_platform_duplicates",
        "--limit=10",
        "--indent=0",
        stdout=out,
    )
    dry_run = json.loads(out.getvalue())
    pending.refresh_from_db()
    assert dry_run["would_merge"] == 1
    assert dry_run["merged"] == 0
    assert pending.status == DuplicateCandidate.Status.PENDING
    assert Source.objects.get(pk=claude_source.pk).app == duplicate

    out = StringIO()
    call_command(
        "merge_cross_platform_duplicates",
        "--limit=10",
        "--apply",
        "--indent=0",
        stdout=out,
    )
    applied = json.loads(out.getvalue())
    pending.refresh_from_db()
    duplicate.refresh_from_db()
    claude_link = AppPlatform.objects.get(app=canonical, platform=claude)

    assert applied["would_merge"] == 1
    assert applied["merged"] == 1
    assert pending.status == DuplicateCandidate.Status.CONFIRMED
    assert pending.resolved_at is not None
    assert duplicate.status == App.AppStatus.HIDDEN
    assert not duplicate.is_indexable
    assert Source.objects.get(pk=claude_source.pk).app == canonical
    assert Source.objects.get(pk=claude_source.pk).is_primary is False
    assert claude_link.official_directory_url == "https://claude.com/connectors/atlassian"
    assert claude_link.scope_summary == "Access Jira and Confluence from Claude."
    assert canonical.listing_types.filter(slug="claude-connector").exists()
    assert canonical.categories.filter(slug="productivity").exists()
    assert AppCapability.objects.get(app=canonical, capability=capability).value == "yes"
    assert NeedsReviewQueueEntry.objects.get(pk=review.pk).app == canonical


def test_merge_cross_platform_duplicates_blocks_mcp_by_default() -> None:
    Platform.objects.get_or_create(
        slug="mcp",
        defaults={"name": "MCP", "public_path": "mcp-servers"},
    )
    first = _existing_app(name="Acme Tools", slug="acme-tools")
    first.platforms.add(Platform.objects.get(slug="mcp"))
    second = _existing_app(name="Acme Tools", slug="acme-tools-2")
    DuplicateCandidate.objects.create(
        app=first,
        candidate_app=second,
        match_reason="similar_name",
        score=1.0,
        evidence={"name_similarity": 1.0},
    )

    out = StringIO()
    call_command(
        "merge_cross_platform_duplicates",
        "--limit=10",
        "--indent=0",
        stdout=out,
    )
    dry_run = json.loads(out.getvalue())

    assert dry_run["would_merge"] == 0
    assert dry_run["blocker_counts"] == {"mcp_requires_include_mcp": 1}
