"""Public read-only API (v1) for the catalog.

Implemented with django-ninja so we get OpenAPI/Swagger out of the
box at ``/api/v1/docs``. The surface is intentionally narrow:

* Only the data a third-party would want for syndication, research,
  or static-site rendering of the catalog.
* Read-only: no auth, no writes, no rate-limit cost (responses are
  cacheable by URL — same as the public HTML pages).
* Mirrors the public manager (``App.published``) so deprecated /
  hidden / draft cards never leak.

Schemas keep DB column names where possible so consumers don't need
a translation layer; nested taxonomy stays flat (slugs only) to
keep payloads small. Use ``?include=details`` on the listing
endpoint to opt into the heavier per-card shape — defaults to a
lean ~20-line response per app.
"""
from __future__ import annotations

from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Query, Schema

from apps.catalog.models import App, Category, Platform

api = NinjaAPI(
    version="1.0",
    title="LLM App Market — Public API",
    description=(
        "Read-only listing data for the LLM App Market catalog. "
        "Includes only PUBLISHED + indexable apps; deprecated cards "
        "are filtered out by the same manager that drives the public "
        "site."
    ),
    docs_url="/docs",
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class PlatformOut(Schema):
    slug: str
    name: str
    public_path: str


class CategoryOut(Schema):
    slug: str
    name: str


class CapabilityOut(Schema):
    key: str
    value: str
    note: str = ""


class AppListOut(Schema):
    slug: str
    name: str
    short_description: str
    developer_name: str = ""
    official_page_url: str = ""
    install_url: str = ""
    repo_url: str = ""
    launch_status: str
    pricing_model: str
    quality_score: int
    platform_slugs: list[str]
    category_slugs: list[str]


class AppDetailOut(AppListOut):
    long_description: str = ""
    verdict: str = ""
    listing_type_slugs: list[str]
    use_case_slugs: list[str]
    capabilities: list[CapabilityOut]


# ---------------------------------------------------------------------------
# Serializers — pure mappers, kept here to keep ninja schemas dumb.
# ---------------------------------------------------------------------------
def _serialize_list(app: App) -> dict:
    return {
        "slug": app.slug,
        "name": app.name,
        "short_description": app.short_description,
        "developer_name": app.developer_name or "",
        "official_page_url": app.official_page_url or "",
        "install_url": app.install_url or "",
        "repo_url": app.repo_url or "",
        "launch_status": app.launch_status,
        "pricing_model": app.pricing_model,
        "quality_score": app.quality_score,
        "platform_slugs": [p.slug for p in app.platforms.all()],
        "category_slugs": [c.slug for c in app.categories.all()],
    }


def _serialize_detail(app: App) -> dict:
    base = _serialize_list(app)
    base.update(
        {
            "long_description": app.long_description or "",
            "verdict": app.verdict or "",
            "listing_type_slugs": [lt.slug for lt in app.listing_types.all()],
            "use_case_slugs": [uc.slug for uc in app.use_cases.all()],
            "capabilities": [
                {"key": ac.capability.key, "value": ac.value, "note": ac.note or ""}
                for ac in app.appcapability_set.all()
                if ac.value != "unknown"
            ],
        }
    )
    return base


# ---------------------------------------------------------------------------
# Listing filters
# ---------------------------------------------------------------------------
class AppListFilters(Schema):
    platform: str | None = None
    category: str | None = None
    page: int = 1
    page_size: int = 24


class PaginatedAppsOut(Schema):
    count: int
    page: int
    page_size: int
    results: list[AppListOut]


APP_LIST_FILTERS_QUERY = Query(...)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@api.get("/apps/", response=PaginatedAppsOut)
def list_apps(request, filters: AppListFilters = APP_LIST_FILTERS_QUERY):
    """Paginated list of published apps with optional filters."""
    qs = App.published.all().for_listing().order_by("-quality_score", "-first_seen_at")
    if filters.platform:
        qs = qs.filter(platforms__slug=filters.platform)
    if filters.category:
        qs = qs.filter(categories__slug=filters.category)
    qs = qs.distinct()

    page_size = max(1, min(filters.page_size, 100))
    page = max(1, filters.page)
    start = (page - 1) * page_size
    end = start + page_size

    return {
        "count": qs.count(),
        "page": page,
        "page_size": page_size,
        "results": [_serialize_list(a) for a in qs[start:end]],
    }


@api.get("/apps/{slug}/", response=AppDetailOut)
def app_detail(request, slug: str):
    """Full detail for one published app."""
    app = get_object_or_404(App.published.all().for_detail(), slug=slug)
    return _serialize_detail(app)


@api.get("/platforms/", response=list[PlatformOut])
def list_platforms(request):
    return [
        {"slug": p.slug, "name": p.name, "public_path": p.public_path}
        for p in Platform.objects.all().order_by("sort_order", "name")
    ]


@api.get("/categories/", response=list[CategoryOut])
def list_categories(request):
    return [
        {"slug": c.slug, "name": c.name}
        for c in Category.objects.all().order_by("sort_order", "name")
    ]
