from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command

from apps.catalog.models import (
    App,
    AppCapability,
    AppPlatform,
    Capability,
    ListingType,
    Platform,
)
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


def test_backfill_trusted_connector_capabilities_dry_run_then_apply() -> None:
    claude = _platform("claude")
    app = App.objects.create(
        name="Trusted Capabilities",
        slug="trusted-capabilities",
        short_description="A Claude connector ready for capability backfill.",
        platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
    )
    AppPlatform.objects.create(
        app=app,
        platform=claude,
        official_directory_url="https://claude.com/connectors/trusted-capabilities",
    )
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.CLAUDE_CONNECTORS,
        external_id="claude:trusted-capabilities",
    )

    out = StringIO()
    call_command(
        "backfill_trusted_connector_capabilities",
        "--source-type=claude_connectors",
        "--limit=10",
        "--indent=0",
        stdout=out,
    )
    dry_run = json.loads(out.getvalue())
    assert dry_run["would_update"] == 1
    assert dry_run["updated_capabilities"] == 0
    assert dry_run["results"][0]["planned_updates"] == {
        "local_setup_required": "no",
        "remote_available": "yes",
    }
    assert AppCapability.objects.filter(app=app).count() == 0

    out = StringIO()
    call_command(
        "backfill_trusted_connector_capabilities",
        "--source-type=claude_connectors",
        "--limit=10",
        "--apply",
        "--indent=0",
        stdout=out,
    )
    applied = json.loads(out.getvalue())
    values = {
        item.capability.key: item.value
        for item in AppCapability.objects.filter(app=app).select_related("capability")
    }
    assert applied["would_update"] == 1
    assert applied["updated"] == 1
    assert applied["updated_capabilities"] == 2
    assert values["remote_available"] == AppCapability.CapabilityValue.YES
    assert values["local_setup_required"] == AppCapability.CapabilityValue.NO


def test_backfill_trusted_connector_capabilities_does_not_overwrite_known_values() -> None:
    claude = _platform("claude")
    remote = Capability.objects.create(
        key="remote_available",
        label="Hosted / remote available",
    )
    app = App.objects.create(
        name="Manual Capability",
        slug="manual-capability",
        short_description="A Claude connector with a manual capability value.",
        platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
    )
    AppPlatform.objects.create(
        app=app,
        platform=claude,
        official_directory_url="https://claude.com/connectors/manual-capability",
    )
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.CLAUDE_CONNECTORS,
        external_id="claude:manual-capability",
    )
    AppCapability.objects.create(
        app=app,
        capability=remote,
        value=AppCapability.CapabilityValue.NO,
        note="Manual review found no hosted endpoint.",
    )

    out = StringIO()
    call_command(
        "backfill_trusted_connector_capabilities",
        "--source-type=claude_connectors",
        "--limit=10",
        "--apply",
        "--indent=0",
        stdout=out,
    )
    applied = json.loads(out.getvalue())
    app_remote = AppCapability.objects.get(app=app, capability=remote)
    local_setup = AppCapability.objects.get(
        app=app,
        capability__key="local_setup_required",
    )

    assert applied["results"][0]["skipped_existing"] == {"remote_available": "no"}
    assert app_remote.value == AppCapability.CapabilityValue.NO
    assert app_remote.note == "Manual review found no hosted endpoint."
    assert local_setup.value == AppCapability.CapabilityValue.NO


def test_backfill_trusted_connector_capabilities_excludes_mcp_by_default() -> None:
    chatgpt = _platform("chatgpt")
    mcp = _platform("mcp")
    app = App.objects.create(
        name="Mixed Capability App",
        slug="mixed-capability-app",
        short_description="A ChatGPT app that also has MCP platform metadata.",
        platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
    )
    AppPlatform.objects.create(
        app=app,
        platform=chatgpt,
        official_directory_url="https://chatgpt.com/apps/mixed-capability-app",
    )
    app.platforms.add(mcp)
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.CHATGPT_UNOFFICIAL,
        external_id="chatgpt:mixed-capability-app",
    )

    out = StringIO()
    call_command(
        "backfill_trusted_connector_capabilities",
        "--limit=10",
        "--indent=0",
        stdout=out,
    )
    result = json.loads(out.getvalue())

    assert result["would_update"] == 0


def test_backfill_trusted_connector_categories_dry_run_then_apply() -> None:
    claude = _platform("claude")
    app = App.objects.create(
        name="Health Connector",
        slug="health-connector",
        short_description="A Claude connector for health and wellness data.",
        platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
    )
    AppPlatform.objects.create(
        app=app,
        platform=claude,
        official_directory_url="https://claude.com/connectors/health-connector",
    )
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.CLAUDE_CONNECTORS,
        external_id="claude:health-connector",
        payload={
            "card": {"categories": ["Health and wellness"]},
            "unmapped_categories": ["Health and wellness"],
        },
    )

    out = StringIO()
    call_command(
        "backfill_trusted_connector_categories",
        "--source-type=claude_connectors",
        "--limit=10",
        "--indent=0",
        stdout=out,
    )
    dry_run = json.loads(out.getvalue())
    assert dry_run["would_update"] == 1
    assert dry_run["updated_categories"] == 0
    assert dry_run["results"][0]["planned_categories"] == ["health-wellness"]
    assert list(app.categories.values_list("slug", flat=True)) == []

    out = StringIO()
    call_command(
        "backfill_trusted_connector_categories",
        "--source-type=claude_connectors",
        "--limit=10",
        "--apply",
        "--indent=0",
        stdout=out,
    )
    applied = json.loads(out.getvalue())
    app.refresh_from_db()
    assert applied["would_update"] == 1
    assert applied["updated"] == 1
    assert applied["updated_categories"] == 1
    assert list(app.categories.values_list("slug", flat=True)) == ["health-wellness"]


def test_backfill_trusted_connector_categories_excludes_mcp_by_default() -> None:
    claude = _platform("claude")
    mcp = _platform("mcp")
    app = App.objects.create(
        name="Mixed Health App",
        slug="mixed-health-app",
        short_description="A ChatGPT app that also has MCP platform metadata.",
        platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
    )
    AppPlatform.objects.create(
        app=app,
        platform=claude,
        official_directory_url="https://claude.com/connectors/mixed-health-app",
    )
    app.platforms.add(mcp)
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.CLAUDE_CONNECTORS,
        external_id="claude:mixed-health-app",
        payload={
            "card": {"categories": ["Health and wellness"]},
            "unmapped_categories": ["Health and wellness"],
        },
    )

    out = StringIO()
    call_command(
        "backfill_trusted_connector_categories",
        "--limit=10",
        "--indent=0",
        stdout=out,
    )
    result = json.loads(out.getvalue())

    assert result["would_update"] == 0


def test_backfill_trusted_connector_descriptions_dry_run_then_apply() -> None:
    claude = _platform("claude")
    app = App.objects.create(
        name="Trail Connector",
        slug="trail-connector",
        short_description="Find trails",
        long_description=(
            "Find trails\n\nFind your next outdoor adventure with Trail Connector, "
            "directly in Claude. Browse curated trail details and ratings."
        ),
        platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
    )
    AppPlatform.objects.create(
        app=app,
        platform=claude,
        official_directory_url="https://claude.com/connectors/trail-connector",
    )
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.CLAUDE_CONNECTORS,
        external_id="claude:trail-connector",
        payload={
            "detail": {
                "long_description": (
                    "Find trails\n\nFind your next outdoor adventure with Trail "
                    "Connector, directly in Claude. Browse curated trail details."
                )
            },
        },
    )

    out = StringIO()
    call_command(
        "backfill_trusted_connector_descriptions",
        "--source-type=claude_connectors",
        "--limit=10",
        "--indent=0",
        stdout=out,
    )
    dry_run = json.loads(out.getvalue())
    assert dry_run["would_update"] == 1
    assert dry_run["updated"] == 0
    assert dry_run["results"][0]["planned_short_description"] == (
        "Find your next outdoor adventure with Trail Connector, directly in Claude."
    )
    app.refresh_from_db()
    assert app.short_description == "Find trails"

    out = StringIO()
    call_command(
        "backfill_trusted_connector_descriptions",
        "--source-type=claude_connectors",
        "--limit=10",
        "--apply",
        "--indent=0",
        stdout=out,
    )
    applied = json.loads(out.getvalue())
    app.refresh_from_db()
    assert applied["would_update"] == 1
    assert applied["updated"] == 1
    assert app.short_description == (
        "Find your next outdoor adventure with Trail Connector, directly in Claude."
    )


def test_backfill_trusted_connector_descriptions_keeps_usable_short_description() -> None:
    claude = _platform("claude")
    app = App.objects.create(
        name="Usable Connector",
        slug="usable-connector",
        short_description="A usable connector description",
        long_description=(
            "A better but unnecessary source-derived description for the connector."
        ),
        platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
    )
    AppPlatform.objects.create(
        app=app,
        platform=claude,
        official_directory_url="https://claude.com/connectors/usable-connector",
    )
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.CLAUDE_CONNECTORS,
        external_id="claude:usable-connector",
        payload={
            "detail": {
                "long_description": (
                    "A better but unnecessary source-derived description."
                )
            },
        },
    )

    out = StringIO()
    call_command(
        "backfill_trusted_connector_descriptions",
        "--source-type=claude_connectors",
        "--limit=10",
        "--apply",
        "--indent=0",
        stdout=out,
    )
    applied = json.loads(out.getvalue())
    app.refresh_from_db()

    assert applied["would_update"] == 0
    assert applied["updated"] == 0
    assert app.short_description == "A usable connector description"


def test_repair_trusted_connector_mcp_taxonomy_dry_run_then_apply() -> None:
    claude = _platform("claude")
    cloud_listing = ListingType.objects.create(
        slug="claude-connector",
        name="Claude Connector",
    )
    mcp_listing = ListingType.objects.create(
        slug="mcp-server",
        name="MCP Server",
    )
    app = App.objects.create(
        name="False MCP Connector",
        slug="false-mcp-connector",
        short_description="A Claude connector with a false MCP listing type.",
        platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
    )
    AppPlatform.objects.create(
        app=app,
        platform=claude,
        official_directory_url="https://claude.com/connectors/false-mcp-connector",
    )
    app.listing_types.add(cloud_listing, mcp_listing)
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.CLAUDE_CONNECTORS,
        external_id="claude:false-mcp-connector",
    )

    out = StringIO()
    call_command(
        "repair_trusted_connector_mcp_taxonomy",
        "--source-type=claude_connectors",
        "--limit=10",
        "--indent=0",
        stdout=out,
    )
    dry_run = json.loads(out.getvalue())
    assert dry_run["would_update"] == 1
    assert dry_run["updated_listing_types"] == 0
    assert dry_run["results"][0]["planned_remove_listing_types"] == ["mcp-server"]
    assert set(app.listing_types.values_list("slug", flat=True)) == {
        "claude-connector",
        "mcp-server",
    }

    out = StringIO()
    call_command(
        "repair_trusted_connector_mcp_taxonomy",
        "--source-type=claude_connectors",
        "--limit=10",
        "--apply",
        "--indent=0",
        stdout=out,
    )
    applied = json.loads(out.getvalue())
    assert applied["would_update"] == 1
    assert applied["updated"] == 1
    assert applied["updated_listing_types"] == 1
    assert list(app.listing_types.values_list("slug", flat=True)) == [
        "claude-connector"
    ]


def test_repair_trusted_connector_mcp_taxonomy_excludes_mcp_platform() -> None:
    claude = _platform("claude")
    mcp = _platform("mcp")
    cloud_listing = ListingType.objects.create(
        slug="claude-connector",
        name="Claude Connector",
    )
    mcp_listing = ListingType.objects.create(
        slug="mcp-server",
        name="MCP Server",
    )
    app = App.objects.create(
        name="Real MCP Connector",
        slug="real-mcp-connector",
        short_description="A Claude connector with real MCP platform metadata.",
        platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
    )
    AppPlatform.objects.create(
        app=app,
        platform=claude,
        official_directory_url="https://claude.com/connectors/real-mcp-connector",
    )
    app.platforms.add(mcp)
    app.listing_types.add(cloud_listing, mcp_listing)
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.CLAUDE_CONNECTORS,
        external_id="claude:real-mcp-connector",
    )

    out = StringIO()
    call_command(
        "repair_trusted_connector_mcp_taxonomy",
        "--limit=10",
        "--indent=0",
        stdout=out,
    )
    result = json.loads(out.getvalue())

    assert result["would_update"] == 0
    assert app.listing_types.filter(slug="mcp-server").exists()


def test_backfill_trusted_connector_launch_statuses_dry_run_then_apply() -> None:
    claude = _platform("claude")
    app = App.objects.create(
        name="Beta Connector",
        slug="beta-connector",
        short_description="A Claude connector whose source says it is beta.",
        launch_status=App.LaunchStatus.LIVE,
        platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
    )
    AppPlatform.objects.create(
        app=app,
        platform=claude,
        official_directory_url="https://claude.com/connectors/beta-connector",
    )
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.CLAUDE_CONNECTORS,
        external_id="claude:beta-connector",
        payload={
            "detail": {
                "long_description": (
                    "Use Beta Connector from Claude.\n\n"
                    "The Beta Connector server is in beta."
                )
            }
        },
    )

    out = StringIO()
    call_command(
        "backfill_trusted_connector_launch_statuses",
        "--source-type=claude_connectors",
        "--limit=10",
        "--indent=0",
        stdout=out,
    )
    dry_run = json.loads(out.getvalue())
    assert dry_run["would_update"] == 1
    assert dry_run["updated"] == 0
    assert dry_run["results"][0]["planned_launch_status"] == "beta"
    app.refresh_from_db()
    assert app.launch_status == App.LaunchStatus.LIVE

    out = StringIO()
    call_command(
        "backfill_trusted_connector_launch_statuses",
        "--source-type=claude_connectors",
        "--limit=10",
        "--apply",
        "--indent=0",
        stdout=out,
    )
    applied = json.loads(out.getvalue())
    app.refresh_from_db()
    assert applied["would_update"] == 1
    assert applied["updated"] == 1
    assert app.launch_status == App.LaunchStatus.BETA


def test_backfill_trusted_connector_launch_statuses_excludes_mcp_source() -> None:
    claude = _platform("claude")
    app = App.objects.create(
        name="Mixed Beta Connector",
        slug="mixed-beta-connector",
        short_description="A mixed connector whose source says it is beta.",
        launch_status=App.LaunchStatus.LIVE,
        platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
    )
    AppPlatform.objects.create(
        app=app,
        platform=claude,
        official_directory_url="https://claude.com/connectors/mixed-beta-connector",
    )
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.CLAUDE_CONNECTORS,
        external_id="claude:mixed-beta-connector",
        payload={"detail": {"long_description": "The connector is in beta."}},
    )
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.MCP_REGISTRY,
        external_id="mcp:mixed-beta-connector",
    )

    out = StringIO()
    call_command(
        "backfill_trusted_connector_launch_statuses",
        "--limit=10",
        "--apply",
        "--indent=0",
        stdout=out,
    )
    result = json.loads(out.getvalue())
    app.refresh_from_db()

    assert result["would_update"] == 0
    assert app.launch_status == App.LaunchStatus.LIVE
