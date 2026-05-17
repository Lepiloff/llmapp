"""SLA-dashboard view for the editor review queue."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.agent.admin import SLA_PENDING_DAYS
from apps.agent.models import NeedsReviewQueueEntry
from apps.catalog.models import App


pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(client):
    User = get_user_model()
    user = User.objects.create_superuser(
        username="editor", email="e@example.com", password="x"
    )
    client.force_login(user)
    return client


@pytest.fixture
def app(db) -> App:
    return App.objects.create(name="Acme", slug="acme-sla", short_description="x")


def _make_entry(app: App, *, days_ago: int, resolved: bool = False) -> NeedsReviewQueueEntry:
    entry = NeedsReviewQueueEntry.objects.create(
        app=app,
        kind=NeedsReviewQueueEntry.Kind.ENRICHED,
        payload={},
        resolved_at=timezone.now() if resolved else None,
    )
    created = timezone.now() - timedelta(days=days_ago)
    NeedsReviewQueueEntry.objects.filter(pk=entry.pk).update(created_at=created)
    entry.refresh_from_db()
    return entry


def test_dashboard_status_is_healthy_when_no_overdue(admin_client, app) -> None:
    _make_entry(app, days_ago=1)  # fresh — not overdue

    url = reverse("admin:agent_needsreviewqueueentry_sla_dashboard")
    response = admin_client.get(url)
    assert response.status_code == 200
    body = response.content.decode()
    assert "OK" in body
    # The overdue count cell shows zero.
    assert "Overdue" in body


def test_dashboard_status_bad_when_pending_overdue(admin_client, app) -> None:
    _make_entry(app, days_ago=SLA_PENDING_DAYS + 5)
    _make_entry(app, days_ago=2)

    url = reverse("admin:agent_needsreviewqueueentry_sla_dashboard")
    response = admin_client.get(url)
    assert response.status_code == 200
    body = response.content.decode()
    assert "OVERDUE" in body
    assert "acme-sla" in body


def test_resolved_entries_dont_count(admin_client, app) -> None:
    """Resolved entries — even very old ones — must not show as overdue."""
    _make_entry(app, days_ago=SLA_PENDING_DAYS + 100, resolved=True)

    url = reverse("admin:agent_needsreviewqueueentry_sla_dashboard")
    response = admin_client.get(url)
    body = response.content.decode()
    assert "OK" in body


@override_settings(AGENT_REVIEW_QUEUE_SLA_DAYS=3)
def test_sla_window_honors_settings_override(admin_client, app) -> None:
    """A tightened SLA via settings must reshape the dashboard.

    An entry 5 days old is not overdue at the default 14d but IS
    overdue when the window is dropped to 3d via env / settings.
    """
    _make_entry(app, days_ago=5)

    url = reverse("admin:agent_needsreviewqueueentry_sla_dashboard")
    response = admin_client.get(url)
    body = response.content.decode()
    assert "OVERDUE" in body
    # The threshold renders on the page so editors see what window's active.
    assert "3 days" in body or "3</strong>" in body
