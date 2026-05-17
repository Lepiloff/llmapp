"""Regression: /go/<slug>/ is rate-limited per IP.

Before the fix, /go/<slug>/ wrote one ClickEvent per request with no
throttle. A script could flood the table (DB bloat) or inflate the
trending score for any slug (apps/analytics/tasks.py weights recent
clicks).
"""
from __future__ import annotations

import pytest
from django.test import override_settings
from django.urls import reverse

from apps.analytics.models import ClickEvent
from apps.catalog.models import App


@pytest.fixture
def published_app(db) -> App:
    return App.objects.create(
        name="Acme",
        slug="acme",
        short_description="x",
        official_page_url="https://example.com/acme",
        status=App.AppStatus.PUBLISHED,
        is_indexable=True,
    )


def _go_url(app: App) -> str:
    return (
        reverse("analytics:outbound_redirect", kwargs={"slug": app.slug})
        + f"?url={app.official_page_url}&type=official"
    )


@pytest.mark.django_db
@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "ratelimit-tests",
        }
    },
)
def test_outbound_redirect_throttles_after_60_requests(client, published_app) -> None:
    """61st request from the same IP within a minute must be blocked."""
    url = _go_url(published_app)

    # 60 requests should succeed; 61st blocked. django-ratelimit returns
    # 403 (Ratelimited) by default — same code the submissions app surfaces
    # for its hourly cap.
    for _ in range(60):
        response = client.get(url)
        assert response.status_code == 302, response.status_code

    blocked = client.get(url)
    assert blocked.status_code == 403


@pytest.mark.django_db
@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "ratelimit-tests-allow",
        }
    },
)
def test_first_request_under_cap_records_click(client, published_app) -> None:
    """Sanity: throttle doesn't fire on the happy path."""
    response = client.get(_go_url(published_app))
    assert response.status_code == 302
    assert ClickEvent.objects.filter(app=published_app).count() == 1


@pytest.mark.django_db
@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "ratelimit-tests-xff",
        }
    },
)
def test_throttle_bucket_keyed_by_xff_not_remote_addr(client, published_app) -> None:
    """Behind nginx/ALB, the throttle must key on X-Forwarded-For.

    Otherwise every real client shares the proxy's REMOTE_ADDR and a
    single user can 403 outbound clicks for everyone else.
    """
    url = _go_url(published_app)

    # 60 requests from client A — fills its bucket.
    for _ in range(60):
        response = client.get(url, HTTP_X_FORWARDED_FOR="203.0.113.1")
        assert response.status_code == 302

    # 61st from client A is blocked.
    blocked = client.get(url, HTTP_X_FORWARDED_FOR="203.0.113.1")
    assert blocked.status_code == 403

    # But client B, behind the same proxy (same REMOTE_ADDR), still has
    # its own bucket and passes.
    ok = client.get(url, HTTP_X_FORWARDED_FOR="203.0.113.2")
    assert ok.status_code == 302
