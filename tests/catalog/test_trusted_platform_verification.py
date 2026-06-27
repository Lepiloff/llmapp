from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command

from apps.catalog.models import App, AppPlatform, Platform
from apps.sources.base import AppDraft
from apps.sources.models import Source
from apps.sources.upsert import upsert_app_from_draft

pytestmark = pytest.mark.django_db


def _platform(slug: str) -> Platform:
    return Platform.objects.get_or_create(
        slug=slug,
        defaults={"name": slug.title(), "public_path": slug},
    )[0]


def _draft(
    *,
    slug: str,
    platform: str,
    official_directory_url: str = "",
) -> AppDraft:
    return AppDraft(
        name=slug.replace("-", " ").title(),
        slug_hint=slug,
        short_description="A source-backed app with enough text for testing.",
        platforms=[platform],
        external_id=f"{platform}:{slug}",
        official_page_url=official_directory_url or f"https://example.com/{slug}",
        official_directory_url=official_directory_url,
        raw_payload={"slug": slug},
    )


def test_upsert_marks_claude_connector_as_official() -> None:
    _platform("claude")

    upsert_app_from_draft(
        _draft(
            slug="acme-claude",
            platform="claude",
            official_directory_url="https://claude.com/connectors/acme-claude",
        ),
        Source.SourceType.CLAUDE_CONNECTORS,
    )

    app = App.objects.get(slug="acme-claude")
    assert app.platform_verification_status == App.PlatformVerificationStatus.OFFICIAL


def test_upsert_marks_chatgpt_official_app_url_as_official() -> None:
    _platform("chatgpt")

    upsert_app_from_draft(
        _draft(
            slug="acme-chatgpt",
            platform="chatgpt",
            official_directory_url="https://chatgpt.com/apps/acme-chatgpt",
        ),
        Source.SourceType.CHATGPT_UNOFFICIAL,
    )

    app = App.objects.get(slug="acme-chatgpt")
    assert app.platform_verification_status == App.PlatformVerificationStatus.OFFICIAL


def test_upsert_keeps_gemini_extension_unknown_without_official_directory() -> None:
    _platform("gemini")

    upsert_app_from_draft(
        _draft(slug="acme-gemini", platform="gemini"),
        Source.SourceType.GEMINI_EXTENSIONS,
    )

    app = App.objects.get(slug="acme-gemini")
    assert app.platform_verification_status == App.PlatformVerificationStatus.UNKNOWN


def test_backfill_trusted_platform_verification_command_dry_run_then_apply() -> None:
    claude = _platform("claude")
    app = App.objects.create(
        name="Backfill Claude",
        slug="backfill-claude",
        short_description="A Claude connector ready for trust backfill.",
        platform_verification_status=App.PlatformVerificationStatus.UNKNOWN,
    )
    AppPlatform.objects.create(
        app=app,
        platform=claude,
        official_directory_url="https://claude.com/connectors/backfill-claude",
    )
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.CLAUDE_CONNECTORS,
        external_id="claude:backfill-claude",
    )

    out = StringIO()
    call_command(
        "backfill_trusted_platform_verification",
        "--limit=10",
        "--indent=0",
        stdout=out,
    )
    dry_run = json.loads(out.getvalue())
    app.refresh_from_db()
    assert dry_run["would_update"] == 1
    assert dry_run["updated"] == 0
    assert app.platform_verification_status == App.PlatformVerificationStatus.UNKNOWN

    out = StringIO()
    call_command(
        "backfill_trusted_platform_verification",
        "--limit=10",
        "--apply",
        "--indent=0",
        stdout=out,
    )
    applied = json.loads(out.getvalue())
    app.refresh_from_db()
    assert applied["would_update"] == 1
    assert applied["updated"] == 1
    assert app.platform_verification_status == App.PlatformVerificationStatus.OFFICIAL


def test_backfill_excludes_mixed_mcp_by_default() -> None:
    chatgpt = _platform("chatgpt")
    app = App.objects.create(
        name="Mixed App",
        slug="mixed-app",
        short_description="A mixed ChatGPT and MCP app.",
        platform_verification_status=App.PlatformVerificationStatus.UNKNOWN,
    )
    AppPlatform.objects.create(
        app=app,
        platform=chatgpt,
        official_directory_url="https://chatgpt.com/apps/mixed-app",
    )
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.CHATGPT_UNOFFICIAL,
        external_id="chatgpt:mixed-app",
    )
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.MCP_REGISTRY,
        external_id="mcp:mixed-app",
    )

    out = StringIO()
    call_command(
        "backfill_trusted_platform_verification",
        "--limit=10",
        "--indent=0",
        stdout=out,
    )
    result = json.loads(out.getvalue())

    assert result["would_update"] == 0


def test_backfill_excludes_mcp_platform_by_default() -> None:
    chatgpt = _platform("chatgpt")
    mcp = _platform("mcp")
    app = App.objects.create(
        name="Mixed Platform App",
        slug="mixed-platform-app",
        short_description="A ChatGPT app that also has MCP platform metadata.",
        platform_verification_status=App.PlatformVerificationStatus.UNKNOWN,
    )
    AppPlatform.objects.create(
        app=app,
        platform=chatgpt,
        official_directory_url="https://chatgpt.com/apps/mixed-platform-app",
    )
    app.platforms.add(mcp)
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.CHATGPT_UNOFFICIAL,
        external_id="chatgpt:mixed-platform-app",
    )

    out = StringIO()
    call_command(
        "backfill_trusted_platform_verification",
        "--limit=10",
        "--indent=0",
        stdout=out,
    )
    result = json.loads(out.getvalue())

    assert result["would_update"] == 0
