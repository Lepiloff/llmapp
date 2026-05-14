from __future__ import annotations

from datetime import timedelta

import pytest

from django.utils import timezone

from apps.catalog.models import App
from apps.sources import tasks
from apps.sources.models import LinkCheckResult, LinkHealth


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
