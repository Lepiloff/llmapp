from __future__ import annotations

import pytest

from apps.catalog.models import App
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
