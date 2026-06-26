from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.agent.models import NeedsReviewQueueEntry
from apps.catalog.models import App
from apps.sources import tasks
from apps.sources.models import LinkCheckResult, LinkHealth, Source

pytestmark = pytest.mark.django_db


def _published_app(slug: str, *, last_checked_at=None) -> App:
    return App.objects.create(
        name=slug.replace("-", " ").title(),
        slug=slug,
        short_description="Link checker fixture.",
        status=App.AppStatus.PUBLISHED,
        last_checked_at=last_checked_at,
        official_page_url=f"https://example.test/{slug}",
    )


def test_batch_includes_never_checked_published_apps(monkeypatch) -> None:
    now = timezone.now()
    never_checked = _published_app("never-checked")
    stale = _published_app("stale", last_checked_at=now - timedelta(days=2))
    recent = _published_app("recent", last_checked_at=now)
    checked_ids: list[int] = []

    def fake_check(app: App) -> None:
        checked_ids.append(app.pk)

    monkeypatch.setattr(tasks, "_check_app_links", fake_check)

    result = tasks.check_app_links_batch(batch_size=10)

    assert result == {"checked_count": 2, "failed_count": 0}
    assert checked_ids[0] == never_checked.pk
    assert set(checked_ids) == {never_checked.pk, stale.pk}
    recent.refresh_from_db()
    assert recent.last_checked_at == now


def test_auto_deprecates_after_seventh_official_link_failure_not_fifth() -> None:
    app = _published_app("threshold")
    url = app.official_page_url

    tasks._update_link_health(
        app,
        LinkCheckResult.Target.OFFICIAL,
        url,
        ok=False,
        status_code=500,
    )
    for _ in range(4):
        tasks._update_link_health(
            app,
            LinkCheckResult.Target.OFFICIAL,
            url,
            ok=False,
            status_code=500,
        )

    app.refresh_from_db()
    health = LinkHealth.objects.get(app=app, target=LinkCheckResult.Target.OFFICIAL)
    assert health.consecutive_failures == 5
    assert app.launch_status == App.LaunchStatus.LIVE

    for _ in range(2):
        tasks._update_link_health(
            app,
            LinkCheckResult.Target.OFFICIAL,
            url,
            ok=False,
            status_code=500,
        )

    app.refresh_from_db()
    health.refresh_from_db()
    assert health.consecutive_failures == 7
    assert app.launch_status == App.LaunchStatus.DEPRECATED


def test_threshold_crossing_queues_vanish_event_and_deactivates_source() -> None:
    """At the seventh consecutive failure we both auto-deprecate and
    write one NeedsReviewQueueEntry(kind=vanished) so the editor sees
    the event. Source rows whose source_url matches the dead URL flip
    to is_active=False so re-actualization doesn't re-fetch dead URLs.
    """
    app = _published_app("vanish")
    url = app.official_page_url
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.GITHUB_MCP,
        external_id=f"github_mcp:{app.slug}",
        source_url=url,
    )
    other_source = Source.objects.create(
        app=app,
        source_type=Source.SourceType.MCP_REGISTRY,
        external_id=f"mcp:{app.slug}",
        source_url="https://different.example/still-alive",
    )

    for _ in range(7):
        tasks._update_link_health(
            app, LinkCheckResult.Target.OFFICIAL, url,
            ok=False, status_code=404,
        )

    queued = NeedsReviewQueueEntry.objects.filter(
        app=app, kind=NeedsReviewQueueEntry.Kind.VANISHED
    )
    assert queued.count() == 1, "Should fire exactly once on threshold crossing"
    payload = queued.first().payload
    assert payload["target"] == LinkCheckResult.Target.OFFICIAL
    assert payload["url"] == url
    assert payload["consecutive_failures"] == 7
    assert payload["sources_deactivated"] == 1
    # Source matching the dying URL was deactivated.
    assert not Source.objects.get(source_url=url).is_active
    # Other source untouched — apps' other live ingestion paths keep working.
    other_source.refresh_from_db()
    assert other_source.is_active is True


def test_vanish_event_does_not_re_queue_on_subsequent_failures() -> None:
    """Crossing the threshold a *second* time without an intervening
    recovery must not write a duplicate queue entry — the editor's
    inbox is sacred."""
    app = _published_app("no-spam")
    url = app.official_page_url
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.AGENT_ENRICH,
        external_id=f"agent-enrich:{app.slug}",
        source_url=url,
    )

    for _ in range(10):  # 3 failures past threshold
        tasks._update_link_health(
            app, LinkCheckResult.Target.OFFICIAL, url,
            ok=False, status_code=500,
        )

    assert NeedsReviewQueueEntry.objects.filter(
        app=app, kind=NeedsReviewQueueEntry.Kind.VANISHED
    ).count() == 1


def test_vanish_event_does_not_fire_for_repo_target() -> None:
    """Only ``official`` and ``install`` targets auto-deprecate. The
    vanish queue entry tracks the same constraint — a dead repo URL
    is interesting, but not a "this app is gone" signal."""
    app = _published_app("repo-only")
    url = "https://github.com/example/abandoned"
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.GITHUB_MCP,
        external_id=f"github_mcp:{app.slug}",
        source_url=url,
    )

    for _ in range(10):
        tasks._update_link_health(
            app, LinkCheckResult.Target.REPO, url,
            ok=False, status_code=404,
        )

    assert not NeedsReviewQueueEntry.objects.filter(
        app=app, kind=NeedsReviewQueueEntry.Kind.VANISHED
    ).exists()
    # And the source remains active — the editor decides whether to
    # de-list a project whose only signal is a 404 repo.
    assert Source.objects.get(source_url=url).is_active is True


def test_vanish_event_refires_after_recovery_then_new_breakage() -> None:
    """Recovery resets consecutive_failures. If the URL later dies
    again, we want a fresh queue entry — the editor's previous
    resolution covered a different vanish event."""
    app = _published_app("flap")
    url = app.official_page_url
    Source.objects.create(
        app=app,
        source_type=Source.SourceType.GITHUB_MCP,
        external_id=f"github_mcp:{app.slug}",
        source_url=url,
    )

    for _ in range(7):
        tasks._update_link_health(
            app, LinkCheckResult.Target.OFFICIAL, url,
            ok=False, status_code=404,
        )
    tasks._update_link_health(
        app, LinkCheckResult.Target.OFFICIAL, url,
        ok=True, status_code=200,
    )
    # Re-activate source since we want to simulate a recovery here.
    Source.objects.filter(app=app, source_url=url).update(is_active=True)
    for _ in range(7):
        tasks._update_link_health(
            app, LinkCheckResult.Target.OFFICIAL, url,
            ok=False, status_code=404,
        )

    assert NeedsReviewQueueEntry.objects.filter(
        app=app, kind=NeedsReviewQueueEntry.Kind.VANISHED
    ).count() == 2
