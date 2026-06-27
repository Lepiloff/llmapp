from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

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
from apps.catalog.publishing import (
    apply_autopublish_decision,
    evaluate_autopublish_candidate,
)
from apps.catalog.services import transition_to_published
from apps.sources.models import DuplicateCandidate, Source

pytestmark = pytest.mark.django_db


def _candidate_app(
    *,
    source_type=Source.SourceType.GEMINI_EXTENSIONS,
    slug="acme-helper",
) -> App:
    platform = Platform.objects.get_or_create(
        slug="gemini",
        defaults={"name": "Gemini", "public_path": "gemini-extensions"},
    )[0]
    category = Category.objects.get_or_create(
        slug="developer-tools",
        defaults={"name": "Developer Tools"},
    )[0]
    listing_type = ListingType.objects.get_or_create(
        slug="gemini-extension",
        defaults={"name": "Gemini Extension"},
    )[0]
    app = App.objects.create(
        name=f"Acme Helper {slug}",
        slug=slug,
        short_description=(
            "A practical Gemini extension for developers who need help "
            "testing, debugging, and understanding code."
        ),
        long_description="Detailed source-derived description of the integration.",
        official_page_url=f"https://example.com/{slug}",
        platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
        launch_status=App.LaunchStatus.LIVE,
        pricing_model=App.PricingModel.UNKNOWN,
    )
    app.categories.add(category)
    app.listing_types.add(listing_type)
    AppPlatform.objects.create(
        app=app,
        platform=platform,
        official_directory_url=f"https://example.com/directory/{slug}",
        install_url=f"https://example.com/install/{slug}",
    )
    for index in range(3):
        capability = Capability.objects.get_or_create(
            key=f"capability-{index}",
            defaults={"label": f"Capability {index}"},
        )[0]
        AppCapability.objects.create(
            app=app,
            capability=capability,
            value=AppCapability.CapabilityValue.YES,
            note="Source explicitly supports this capability.",
        )
    Source.objects.create(
        app=app,
        source_type=source_type,
        external_id=f"{source_type}:{slug}",
        source_url=f"https://example.com/source/{slug}",
        is_active=True,
    )
    return app


def _review_entry(app: App, **payload_overrides) -> NeedsReviewQueueEntry:
    payload = {
        "rationale": "The source supports a narrow but useful developer workflow.",
        "proposed_verdict": (
            "Useful developer extension for testing and debugging code from Gemini."
        ),
        "proposed_launch_status": "live",
        "proposed_pricing_model": "free",
        "proposed_scope_summary": (
            "Gemini extension for testing, debugging, and code explanation."
        ),
        "skipped_field_updates": [],
        "skipped_capability_updates": [],
    }
    payload.update(payload_overrides)
    return NeedsReviewQueueEntry.objects.create(
        app=app,
        kind=NeedsReviewQueueEntry.Kind.ENRICHED,
        payload=payload,
    )


def _trusted_claude_connector() -> App:
    platform = Platform.objects.get_or_create(
        slug="claude",
        defaults={"name": "Claude", "public_path": "claude-connectors"},
    )[0]
    category = Category.objects.get_or_create(
        slug="productivity",
        defaults={"name": "Productivity"},
    )[0]
    listing_type = ListingType.objects.get_or_create(
        slug="claude-connector",
        defaults={"name": "Claude Connector"},
    )[0]
    app = App.objects.create(
        name="Compact Claude",
        slug="compact-claude",
        short_description="Search and update work records",
        long_description=(
            "Search and update work records from Claude using the official "
            "cloud connector directory listing."
        ),
        official_page_url="https://example.com/compact-claude",
        platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
        launch_status=App.LaunchStatus.LIVE,
    )
    app.categories.add(category)
    app.listing_types.add(listing_type)
    AppPlatform.objects.create(
        app=app,
        platform=platform,
        official_directory_url="https://claude.com/connectors/compact-claude",
    )
    for index, key in enumerate(("read_data", "write_actions")):
        capability = Capability.objects.get_or_create(
            key=key,
            defaults={"label": key.replace("_", " ").title()},
        )[0]
        AppCapability.objects.create(
            app=app,
            capability=capability,
            value=AppCapability.CapabilityValue.YES,
            note=f"Official connector page supports capability {index}.",
        )
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.CLAUDE_CONNECTORS,
        external_id="claude:compact-claude",
        source_url="https://claude.com/connectors/compact-claude",
    )
    return app


def test_apply_autopublish_accepts_safe_review_and_publishes() -> None:
    app = _candidate_app()
    entry = _review_entry(app)

    decision = apply_autopublish_decision(
        app.pk,
        source_types=(Source.SourceType.GEMINI_EXTENSIONS,),
    )

    assert decision.published is True
    app.refresh_from_db()
    entry.refresh_from_db()
    platform_link = app.platform_links.get()
    assert app.status == App.AppStatus.PUBLISHED
    assert app.editorial_review_status == App.EditorialReviewStatus.REVIEWED
    assert app.verdict == decision.app_updates["verdict"]
    assert app.pricing_model == App.PricingModel.FREE
    assert platform_link.scope_summary == decision.platform_scope_summary
    assert entry.review_outcome == NeedsReviewQueueEntry.ReviewOutcome.ACCEPTED
    assert entry.resolved_at is not None


def test_trusted_connector_compact_profile_passes_publish_gate() -> None:
    app = _trusted_claude_connector()
    app.editorial_review_status = App.EditorialReviewStatus.REVIEWED
    app.save(update_fields=["editorial_review_status"])

    transition_to_published(app, editor=None)

    app.refresh_from_db()
    assert app.status == App.AppStatus.PUBLISHED


def test_autopublish_ignores_low_information_verdict_for_trusted_connector() -> None:
    app = _trusted_claude_connector()
    entry = _review_entry(
        app,
        proposed_verdict="recommended",
        proposed_pricing_model="unknown",
    )

    decision = apply_autopublish_decision(
        app.pk,
        source_types=(Source.SourceType.CLAUDE_CONNECTORS,),
    )

    app.refresh_from_db()
    entry.refresh_from_db()
    assert decision.published is True
    assert app.status == App.AppStatus.PUBLISHED
    assert app.verdict == ""
    assert entry.review_outcome == NeedsReviewQueueEntry.ReviewOutcome.ACCEPTED


def test_autopublish_blocks_review_with_skipped_updates() -> None:
    app = _candidate_app()
    _review_entry(
        app,
        skipped_field_updates=[
            {"field": "developer_name", "new_value": "Acme"}
        ],
    )

    decision = evaluate_autopublish_candidate(
        app,
        source_types=(Source.SourceType.GEMINI_EXTENSIONS,),
    )

    assert decision.would_publish is False
    assert "review_has_skipped_field_updates" in decision.blockers


def test_autopublish_blocks_pending_duplicate_candidate() -> None:
    app = _candidate_app()
    other = _candidate_app(
        source_type=Source.SourceType.CLAUDE_CONNECTORS,
        slug="other-helper",
    )
    _review_entry(app)
    DuplicateCandidate.objects.create(
        app=app,
        candidate_app=other,
        match_reason="similar_name",
        score=0.7,
    )

    decision = evaluate_autopublish_candidate(
        app,
        source_types=(Source.SourceType.GEMINI_EXTENSIONS,),
    )

    assert decision.would_publish is False
    assert "pending_duplicate_candidate" in decision.blockers


def test_autopublish_blocks_mixed_mcp_source_without_mcp_opt_in() -> None:
    app = _candidate_app()
    _review_entry(app)
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.MCP_REGISTRY,
        external_id="mcp:acme-helper",
        source_url="https://registry.modelcontextprotocol.io/acme-helper",
    )

    decision = evaluate_autopublish_candidate(
        app,
        source_types=(Source.SourceType.GEMINI_EXTENSIONS,),
    )

    assert decision.would_publish is False
    assert "mcp_source_requires_include_mcp" in decision.blockers


def test_autopublish_blocks_mcp_platform_without_mcp_opt_in() -> None:
    app = _candidate_app()
    _review_entry(app)
    mcp = Platform.objects.get_or_create(
        slug="mcp",
        defaults={"name": "MCP", "public_path": "mcp-servers"},
    )[0]
    app.platforms.add(mcp)

    decision = evaluate_autopublish_candidate(
        app,
        source_types=(Source.SourceType.GEMINI_EXTENSIONS,),
    )

    assert decision.would_publish is False
    assert "mcp_platform_requires_include_mcp" in decision.blockers


def test_autopublish_command_requires_explicit_mcp_opt_in() -> None:
    out = StringIO()

    with pytest.raises(CommandError, match="--include-mcp"):
        call_command(
            "autopublish_candidates",
            "--source-type",
            Source.SourceType.MCP_REGISTRY,
            stdout=out,
        )
