"""Tests for the public read-only catalog API at /api/v1/*."""
from __future__ import annotations

import pytest

from apps.catalog.models import App, AppCapability, Capability, Category, Platform

pytestmark = pytest.mark.django_db


@pytest.fixture
def mcp(db) -> Platform:
    return Platform.objects.get_or_create(
        slug="mcp",
        defaults={"name": "MCP", "public_path": "mcp-servers"},
    )[0]


@pytest.fixture
def category(db) -> Category:
    return Category.objects.get_or_create(
        slug="developer-tools", defaults={"name": "Developer Tools"}
    )[0]


@pytest.fixture
def published(db, mcp, category) -> App:
    app = App.objects.create(
        name="Acme MCP",
        slug="acme-mcp",
        short_description="x",
        long_description="long",
        verdict="solid",
        developer_name="Acme",
        official_page_url="https://example.com/acme",
        repo_url="https://github.com/acme/acme",
        status=App.AppStatus.PUBLISHED,
        is_indexable=True,
    )
    app.platforms.add(mcp)
    app.categories.add(category)
    cap = Capability.objects.create(key="reads_data", label="Reads data")
    AppCapability.objects.create(
        app=app, capability=cap, value=AppCapability.CapabilityValue.YES,
        note="README says it reads files",
    )
    return app


def test_list_endpoint_returns_published_only(client, published) -> None:
    App.objects.create(
        name="Draft",
        slug="draft-card",
        short_description="x",
        status=App.AppStatus.DRAFT,
    )
    response = client.get("/api/v1/apps/")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    slugs = [a["slug"] for a in payload["results"]]
    assert slugs == ["acme-mcp"]


def test_list_endpoint_filters_by_platform(client, published, mcp) -> None:
    response = client.get(f"/api/v1/apps/?platform={mcp.slug}")
    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_list_endpoint_filters_by_category(client, published, category) -> None:
    response = client.get(f"/api/v1/apps/?category={category.slug}")
    assert response.status_code == 200
    assert response.json()["count"] == 1

    response_empty = client.get("/api/v1/apps/?category=nonexistent")
    assert response_empty.status_code == 200
    assert response_empty.json()["count"] == 0


def test_detail_endpoint_returns_full_payload(client, published) -> None:
    response = client.get(f"/api/v1/apps/{published.slug}/")
    assert response.status_code == 200
    payload = response.json()
    assert payload["slug"] == "acme-mcp"
    assert payload["long_description"] == "long"
    assert payload["verdict"] == "solid"
    assert payload["platform_slugs"] == ["mcp"]
    assert payload["category_slugs"] == ["developer-tools"]
    # capabilities only contain non-unknown values
    assert any(c["key"] == "reads_data" and c["value"] == "yes" for c in payload["capabilities"])


def test_detail_endpoint_404_on_unpublished(client) -> None:
    App.objects.create(
        name="Hidden", slug="hidden-app", short_description="x",
        status=App.AppStatus.HIDDEN,
    )
    response = client.get("/api/v1/apps/hidden-app/")
    assert response.status_code == 404


def test_platforms_endpoint(client, mcp) -> None:
    response = client.get("/api/v1/platforms/")
    assert response.status_code == 200
    slugs = [p["slug"] for p in response.json()]
    assert "mcp" in slugs


def test_categories_endpoint(client, category) -> None:
    response = client.get("/api/v1/categories/")
    assert response.status_code == 200
    slugs = [c["slug"] for c in response.json()]
    assert "developer-tools" in slugs


def test_openapi_docs_reachable(client) -> None:
    """`/api/v1/docs` is the Swagger UI; should at least 200 (HTML)."""
    response = client.get("/api/v1/docs")
    # ninja redirects /docs → /docs/ which may 301; either is acceptable.
    assert response.status_code in (200, 301, 302)


def test_page_size_capped_at_100(client) -> None:
    """Pathological page_size requests get clamped, not honored."""
    # Create 5 apps so we can inspect the page_size echo.
    for i in range(5):
        App.objects.create(
            name=f"a{i}", slug=f"slug-{i}", short_description="x",
            status=App.AppStatus.PUBLISHED, is_indexable=True,
        )
    response = client.get("/api/v1/apps/?page_size=999")
    payload = response.json()
    assert payload["page_size"] == 100
