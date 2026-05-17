"""Regressions for the agent log retention task."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.agent.models import (
    AgentRun,
    EnrichmentTask,
    LLMCallLog,
    NeedsReviewQueueEntry,
)
from apps.agent.tasks import cleanup_old_agent_logs
from apps.catalog.models import App


@pytest.fixture
def app(db) -> App:
    return App.objects.create(
        name="Acme",
        slug="acme",
        short_description="x",
    )


def _make_run(*, days_ago: int) -> AgentRun:
    when = timezone.now() - timedelta(days=days_ago)
    run = AgentRun.objects.create(source_type="manual", status=AgentRun.Status.SUCCEEDED)
    AgentRun.objects.filter(pk=run.pk).update(started_at=when, finished_at=when)
    return AgentRun.objects.get(pk=run.pk)


def test_cleanup_drops_runs_older_than_window(app) -> None:
    old = _make_run(days_ago=400)
    young = _make_run(days_ago=10)

    # Cascade carriers so we can assert the chain is gone.
    task = EnrichmentTask.objects.create(run=old, app=app)
    LLMCallLog.objects.create(task=task, provider="mock", model="mock", is_mock=True)

    result = cleanup_old_agent_logs(days_to_keep=180)

    assert result["deleted_runs"] >= 1
    assert not AgentRun.objects.filter(pk=old.pk).exists()
    assert AgentRun.objects.filter(pk=young.pk).exists()
    assert not EnrichmentTask.objects.filter(pk=task.pk).exists()


def test_cleanup_preserves_pending_queue_entries(app) -> None:
    old_resolved = NeedsReviewQueueEntry.objects.create(
        app=app, kind=NeedsReviewQueueEntry.Kind.ENRICHED, payload={},
        resolved_at=timezone.now() - timedelta(days=400),
    )
    NeedsReviewQueueEntry.objects.filter(pk=old_resolved.pk).update(
        created_at=timezone.now() - timedelta(days=400),
    )
    old_pending = NeedsReviewQueueEntry.objects.create(
        app=app, kind=NeedsReviewQueueEntry.Kind.ENRICHED, payload={},
    )
    NeedsReviewQueueEntry.objects.filter(pk=old_pending.pk).update(
        created_at=timezone.now() - timedelta(days=400),
    )

    cleanup_old_agent_logs(days_to_keep=180)

    assert not NeedsReviewQueueEntry.objects.filter(pk=old_resolved.pk).exists()
    # Pending entries must survive even when ancient.
    assert NeedsReviewQueueEntry.objects.filter(pk=old_pending.pk).exists()
