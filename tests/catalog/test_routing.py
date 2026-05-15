"""Routing regressions for /apps/<slug>/.

Both `Category.get_absolute_url` and `App.get_absolute_url` resolve to
`/apps/<slug>/`, so the URL is dispatched through
`apps.catalog.views.app_or_category_detail`. Category wins on collision.
"""
from __future__ import annotations

import pytest
from django.test import Client

from apps.catalog.models import App, Category


pytestmark = pytest.mark.django_db


@pytest.fixture
def category() -> Category:
    return Category.objects.create(
        slug="developer-tools", name="Developer Tools", sort_order=10
    )


@pytest.fixture
def published_app() -> App:
    return App.objects.create(
        name="Example App",
        slug="example-app",
        short_description="Example",
        status=App.AppStatus.PUBLISHED,
        editorial_review_status=App.EditorialReviewStatus.REVIEWED,
        platform_verification_status=App.PlatformVerificationStatus.NOT_LISTED,
    )


def test_category_slug_returns_200(client: Client, category: Category) -> None:
    response = client.get(f"/apps/{category.slug}/")
    assert response.status_code == 200
    assert b"Developer Tools" in response.content


def test_app_slug_returns_200(client: Client, published_app: App) -> None:
    response = client.get(f"/apps/{published_app.slug}/")
    assert response.status_code == 200
    assert b"Example App" in response.content


def test_unknown_slug_returns_404(client: Client) -> None:
    response = client.get("/apps/no-such-slug/")
    assert response.status_code == 404


def test_category_wins_on_slug_collision(client: Client) -> None:
    """If a Category and an App share a slug, the Category page is served."""
    Category.objects.create(slug="collide", name="Collide Category", sort_order=10)
    App.objects.create(
        name="Collide App",
        slug="collide",
        short_description="App that shares a slug with a category",
        status=App.AppStatus.PUBLISHED,
        editorial_review_status=App.EditorialReviewStatus.REVIEWED,
        platform_verification_status=App.PlatformVerificationStatus.NOT_LISTED,
    )

    response = client.get("/apps/collide/")
    assert response.status_code == 200
    assert b"Collide Category" in response.content
    assert b"Collide App" not in response.content
