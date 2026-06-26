from __future__ import annotations

import pytest
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.agent.models import AgentRun, EnrichmentTask, LLMCallLog, NeedsReviewQueueEntry
from apps.catalog.models import App, AppCapability, Capability, Category, Platform

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_user():
    return get_user_model().objects.create_superuser(
        username="editor",
        email="editor@example.com",
        password="password",
    )


@pytest.fixture
def app_ready_for_review() -> App:
    platform = Platform.objects.create(
        slug="mcp", name="MCP", public_path="mcp-servers"
    )
    category = Category.objects.create(
        slug="developer-tools", name="Developer Tools"
    )
    app = App.objects.create(
        name="Reviewable MCP",
        slug="reviewable-mcp",
        short_description=(
            "A sufficiently complete draft description for the publish gate."
        ),
        status=App.AppStatus.DRAFT,
        editorial_review_status=App.EditorialReviewStatus.REVIEWED,
        platform_verification_status=App.PlatformVerificationStatus.NOT_LISTED,
        official_page_url="https://example.com/reviewable",
    )
    app.platforms.add(platform)
    app.categories.add(category)
    for key in ("read_data", "open_source", "remote_available"):
        cap = Capability.objects.create(key=key, label=key.replace("_", " ").title())
        AppCapability.objects.create(
            app=app,
            capability=cap,
            value=AppCapability.CapabilityValue.YES,
            note="editor evidence",
        )
    return app


@pytest.fixture
def review_entry(app_ready_for_review) -> NeedsReviewQueueEntry:
    run = AgentRun.objects.create(source_type="mcp_registry", status=AgentRun.Status.SUCCEEDED)
    task = EnrichmentTask.objects.create(
        run=run,
        app=app_ready_for_review,
        status=EnrichmentTask.Status.PERSISTED,
    )
    LLMCallLog.objects.create(
        task=task,
        provider="openai",
        model="test-model",
        prompt_version="enrich-existing-v1.0",
        input_tokens=100,
        output_tokens=50,
        cost_usd="0.001000",
        latency_ms=1234,
    )
    return NeedsReviewQueueEntry.objects.create(
        app=app_ready_for_review,
        task=task,
        kind=NeedsReviewQueueEntry.Kind.ENRICHED,
        payload={
            "proposed_verdict": "Best when you need a reviewed MCP integration.",
            "proposed_launch_status": App.LaunchStatus.BETA,
            "proposed_pricing_model": App.PricingModel.FREEMIUM,
            "proposed_scope_summary": "Reads project metadata.",
            "skipped_field_updates": [
                {"field": "short_description", "new_value": "Agent alternative"}
            ],
            "skipped_capability_updates": [
                {
                    "key": "write_actions",
                    "value": "yes",
                    "evidence": "README says it can create issues",
                    "confidence": 0.8,
                }
            ],
            "rationale": "The source describes a narrow MCP integration.",
        },
    )


@pytest.fixture
def logged_in_admin(client, admin_user):
    client.force_login(admin_user)
    return client


def test_review_queue_change_view_renders_current_app_proposal_and_llm_context(
    logged_in_admin, review_entry
) -> None:
    url = reverse("admin:agent_needsreviewqueueentry_change", args=[review_entry.pk])

    response = logged_in_admin.get(url)

    assert response.status_code == 200
    body = response.content.decode()
    assert "Reviewable MCP" in body
    assert "Best when you need a reviewed MCP integration." in body
    assert "Reads project metadata." in body
    assert "openai/test-model" in body
    assert "Apply proposed verdict" in body
    assert "Approve & publish" in body


def test_apply_proposed_verdict_button_updates_app_without_resolving(
    logged_in_admin, review_entry
) -> None:
    url = reverse("admin:agent_needsreviewqueueentry_change", args=[review_entry.pk])

    response = logged_in_admin.post(url, {"_apply_verdict": "1"}, follow=True)

    assert response.status_code == 200
    review_entry.app.refresh_from_db()
    review_entry.refresh_from_db()
    assert review_entry.app.verdict == "Best when you need a reviewed MCP integration."
    assert review_entry.resolved_at is None
    assert review_entry.review_outcome == NeedsReviewQueueEntry.ReviewOutcome.ACCEPTED


def test_reject_all_marks_entry_resolved(logged_in_admin, review_entry, admin_user) -> None:
    url = reverse("admin:agent_needsreviewqueueentry_change", args=[review_entry.pk])

    response = logged_in_admin.post(url, {"_reject_all": "1"}, follow=True)

    assert response.status_code == 200
    review_entry.refresh_from_db()
    assert review_entry.resolved_at is not None
    assert review_entry.resolved_by == admin_user
    assert review_entry.review_outcome == NeedsReviewQueueEntry.ReviewOutcome.REJECTED
    assert review_entry.resolution_note == "Rejected all LLM proposals"


def test_bulk_approve_and_publish_uses_catalog_publish_gate(
    logged_in_admin, review_entry, admin_user
) -> None:
    url = reverse("admin:agent_needsreviewqueueentry_changelist")

    response = logged_in_admin.post(
        url,
        {
            "action": "action_approve_and_publish",
            ACTION_CHECKBOX_NAME: [str(review_entry.pk)],
            "index": "0",
        },
        follow=True,
    )

    assert response.status_code == 200
    review_entry.app.refresh_from_db()
    review_entry.refresh_from_db()
    assert review_entry.app.status == App.AppStatus.PUBLISHED
    assert review_entry.resolved_at is not None
    assert review_entry.resolved_by == admin_user
    assert review_entry.review_outcome == NeedsReviewQueueEntry.ReviewOutcome.PUBLISHED
    assert review_entry.resolution_note == "Approved and published by editor"
