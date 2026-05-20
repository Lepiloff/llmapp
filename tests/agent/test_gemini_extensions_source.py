from __future__ import annotations

import pytest

from apps.agent.sources.gemini_extensions import GeminiExtensionsSource
from apps.agent.tasks import run_direct_ingest_batch
from apps.catalog.models import App, Capability, ListingType, Platform
from apps.sources.models import Source


def _entry(**overrides) -> dict:
    data = {
        "id": "obra-superpowers",
        "url": "https://github.com/obra/superpowers",
        "fullName": "obra/superpowers",
        "repoDescription": "An agentic skills framework.",
        "extensionName": "superpowers",
        "extensionVersion": "5.1.0",
        "extensionDescription": "Core skills library.",
        "hasMCP": True,
        "hasContext": True,
        "hasHooks": True,
        "hasSkills": True,
        "hasCustomCommands": False,
    }
    data.update(overrides)
    return data


def test_maps_json_entry_to_app_draft() -> None:
    source = GeminiExtensionsSource(fetch_json=lambda url: [_entry()])

    draft = list(source.iter_drafts())[0]

    assert draft.name == "superpowers"
    assert draft.developer_name == "obra"
    assert draft.repo_url == "https://github.com/obra/superpowers"
    assert draft.platforms == ["gemini", "mcp"]
    assert draft.listing_types == ["gemini-extension"]
    assert draft.external_id == "gemini:obra-superpowers"
    assert draft.capabilities["gemini_has_context"] == "yes"
    assert draft.capabilities["gemini_has_custom_commands"] == "no"
    assert "hasContext:true" in draft.capability_evidence["gemini_has_context"]


def test_malformed_entries_are_skipped() -> None:
    source = GeminiExtensionsSource(fetch_json=lambda url: [_entry(), {"url": "https://example.com/no-id"}])

    drafts = list(source.iter_drafts())

    assert len(drafts) == 1
    assert len(source.parse_failures) == 1


@pytest.mark.django_db
def test_direct_ingest_persists_and_updates_by_external_id() -> None:
    Platform.objects.get_or_create(slug="gemini", defaults={"name": "Gemini", "public_path": "gemini-apps"})
    Platform.objects.get_or_create(slug="mcp", defaults={"name": "MCP", "public_path": "mcp-servers"})
    ListingType.objects.get_or_create(slug="gemini-extension", defaults={"name": "Gemini Extension"})
    for key in [
        "gemini_has_mcp",
        "gemini_has_context",
        "gemini_has_hooks",
        "gemini_has_skills",
        "gemini_has_custom_commands",
        "open_source",
    ]:
        Capability.objects.get_or_create(key=key, defaults={"label": key.replace("_", " ").title()})

    first = run_direct_ingest_batch(
        source_flag="gemini_extensions",
        source_label=Source.SourceType.GEMINI_EXTENSIONS,
        drafts=GeminiExtensionsSource(fetch_json=lambda url: [_entry()]).iter_drafts(),
        dry_run=False,
        enforce_flag=False,
    )
    second = run_direct_ingest_batch(
        source_flag="gemini_extensions",
        source_label=Source.SourceType.GEMINI_EXTENSIONS,
        drafts=GeminiExtensionsSource(fetch_json=lambda url: [_entry()]).iter_drafts(),
        dry_run=False,
        enforce_flag=False,
    )

    assert first["new"] == 1
    assert second["updated"] == 1
    assert App.objects.count() == 1
