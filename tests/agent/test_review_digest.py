from __future__ import annotations

import pytest
from django.core import mail
from django.test import override_settings

from apps.agent.models import AgentRun, EnrichmentTask, NeedsReviewQueueEntry
from apps.agent.tasks import review_acceptance_stats, send_review_queue_digest
from apps.catalog.models import App

pytestmark = pytest.mark.django_db


def _app(slug: str) -> App:
    return App.objects.create(
        name=slug.replace("-", " ").title(),
        slug=slug,
        short_description="Draft for review digest tests.",
        status=App.AppStatus.DRAFT,
    )


def _entry(app: App, *, outcome: str = NeedsReviewQueueEntry.ReviewOutcome.PENDING):
    run = AgentRun.objects.create(source_type="mcp_registry")
    task = EnrichmentTask.objects.create(run=run, app=app)
    return NeedsReviewQueueEntry.objects.create(
        app=app,
        task=task,
        kind=NeedsReviewQueueEntry.Kind.ENRICHED,
        review_outcome=outcome,
        payload={"proposed_verdict": "Useful for digest tests."},
    )


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    AGENT_REVIEW_DIGEST_EMAILS=["editor@example.com"],
    DEFAULT_FROM_EMAIL="hello@example.com",
    SITE_BASE_URL="https://llmappmarket.test",
)
def test_send_review_queue_digest_emails_open_queue_entries() -> None:
    first = _entry(_app("digest-one"))
    _entry(_app("digest-two"))

    result = send_review_queue_digest()

    assert result["sent"] == 1
    assert result["recipients"] == 1
    assert result["open_entries"] == 2
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == ["editor@example.com"]
    assert "2 agent review queue entries need editor attention" in message.body
    assert "https://llmappmarket.test/admin/agent/needsreviewqueueentry/" in message.body
    assert f"#{first.pk} Digest One" in message.body


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    AGENT_REVIEW_DIGEST_EMAILS=["editor@example.com"],
)
def test_send_review_queue_digest_skips_empty_queue() -> None:
    result = send_review_queue_digest()

    assert result == {"sent": 0, "open_entries": 0, "skipped": "empty_queue"}
    assert len(mail.outbox) == 0


@override_settings(AGENT_REVIEW_DIGEST_EMAILS=[], SUBMISSIONS_NOTIFY_EMAILS=[])
def test_send_review_queue_digest_skips_without_recipients() -> None:
    _entry(_app("digest-no-recipient"))

    result = send_review_queue_digest()

    assert result["sent"] == 0
    assert result["open_entries"] == 1
    assert result["skipped"] == "no_recipients"


def test_review_acceptance_stats_counts_editor_outcomes() -> None:
    _entry(_app("accepted"), outcome=NeedsReviewQueueEntry.ReviewOutcome.ACCEPTED)
    _entry(_app("published"), outcome=NeedsReviewQueueEntry.ReviewOutcome.PUBLISHED)
    _entry(_app("rejected"), outcome=NeedsReviewQueueEntry.ReviewOutcome.REJECTED)
    _entry(_app("no-action"), outcome=NeedsReviewQueueEntry.ReviewOutcome.NO_ACTION)
    _entry(_app("pending"), outcome=NeedsReviewQueueEntry.ReviewOutcome.PENDING)

    stats = review_acceptance_stats(days=30)

    assert stats["reviewed"] == 4
    assert stats["accepted"] == 2
    assert stats["rejected"] == 1
    assert stats["no_action"] == 1
    assert stats["acceptance_rate"] == 50.0
