"""Catalog views.

Architecture refs:
  * docs/architecture.md § 7 (HTMX), § 8 (routing), § 13 (caching).
  * docs/business.md § 7 (sitemap of pages).

The search-driven `/apps/` page lives in `apps.search.views.app_search`.
The views below cover the static slices (home, detail, platform, category,
cross, newly-added strip).
"""
from __future__ import annotations

from django.conf import settings
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import cache_page

from .models import App, Category, Platform


@cache_page(60 * 15, key_prefix="home_v1")
def home(request: HttpRequest) -> HttpResponse:
    """Landing page.

    Sections from docs/business.md § 7.1. Each block is computed with one
    DB round-trip; ``select_related`` is unnecessary because the partial
    template only renders `App.name/slug/short_description/...`.
    """
    qs = App.published.all().for_listing()

    trending = list(
        qs.trending(window_days=settings.TRENDING_WINDOW_DAYS)[:9]
    )
    newly_added = list(qs.order_by("-first_seen_at")[:9])
    featured = list(qs.featured().order_by("-quality_score")[:6])

    platforms = list(Platform.objects.all())
    categories = list(Category.objects.all())

    return render(
        request,
        "catalog/home.html",
        {
            "trending": trending,
            "newly_added": newly_added,
            "featured": featured,
            "platforms": platforms,
            "categories": categories,
        },
    )


def app_or_category_detail(request: HttpRequest, slug: str) -> HttpResponse:
    """Dispatch /apps/<slug>/ between Category page and App detail.

    Categories are an admin-curated closed set, so they take precedence on
    a slug collision; this matches `Category.get_absolute_url` and
    `App.get_absolute_url` both returning `/apps/<slug>/`. Falls through to
    App detail (which raises 404 itself when no published App matches).
    """
    if Category.objects.filter(slug=slug).exists():
        return category_page(request, category_slug=slug)
    return app_detail(request, slug=slug)


def app_detail(request: HttpRequest, slug: str) -> HttpResponse:
    """Single app page (docs/business.md § 7.3)."""
    app = get_object_or_404(
        App.published.all().for_detail(),
        slug=slug,
    )

    # Similar tools: same primary category, distinct app, highest quality.
    similar = (
        App.published.all()
        .for_listing()
        .filter(categories__in=app.categories.all())
        .exclude(pk=app.pk)
        .order_by("-quality_score", "-first_seen_at")
        .distinct()[:6]
    )

    return render(
        request,
        "catalog/detail.html",
        {"app": app, "similar": similar},
    )


@cache_page(60 * 30, key_prefix="platform_v1")
def platform_page(request: HttpRequest, public_path: str) -> HttpResponse:
    """Platform overview page (docs/business.md § 7.4)."""
    platform = get_object_or_404(Platform, public_path=public_path)

    qs = (
        App.published.all()
        .for_listing()
        .filter(platforms=platform)
        .order_by("-quality_score", "-first_seen_at")
    )
    top_apps = list(qs[:20])

    return render(
        request,
        "catalog/platform.html",
        {"platform": platform, "top_apps": top_apps},
    )


@cache_page(60 * 30, key_prefix="category_v1")
def category_page(request: HttpRequest, category_slug: str) -> HttpResponse:
    """Category page (docs/business.md § 7.5)."""
    category = get_object_or_404(Category, slug=category_slug)

    paginator = Paginator(
        App.published.all()
        .for_listing()
        .filter(categories=category)
        .order_by("-quality_score", "-first_seen_at"),
        settings.CATALOG_PAGE_SIZE,
    )
    page = paginator.get_page(request.GET.get("page") or 1)

    return render(
        request,
        "catalog/category.html",
        {"category": category, "page_obj": page},
    )


def cross_page(
    request: HttpRequest, public_path: str, category_slug: str
) -> HttpResponse:
    """Cross-page /<platform>/<category>/ (docs/business.md § 7.6).

    Active only when ≥ 3 published apps exist for the (platform, category)
    pair — otherwise we 404 to avoid thin SEO pages.
    """
    platform = get_object_or_404(Platform, public_path=public_path)
    category = get_object_or_404(Category, slug=category_slug)

    qs = (
        App.published.all()
        .for_listing()
        .filter(platforms=platform, categories=category)
        .order_by("-quality_score", "-first_seen_at")
        .distinct()
    )
    total = qs.count()
    if total < 3:
        raise Http404("Cross-page not enabled for this pair (need ≥ 3 cards).")

    paginator = Paginator(qs, settings.CATALOG_PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page") or 1)

    return render(
        request,
        "catalog/cross.html",
        {
            "platform": platform,
            "category": category,
            "page_obj": page,
            "total": total,
        },
    )


def newly_added(request: HttpRequest) -> HttpResponse:
    """Cursor-based HTMX strip for the home "Newly added" lazy-load."""
    cursor = request.GET.get("cursor")
    qs = App.published.all().for_listing().order_by("-first_seen_at", "-id")
    if cursor:
        try:
            cursor_id = int(cursor)
        except ValueError:
            cursor_id = 0
        qs = qs.filter(id__lt=cursor_id)
    items = list(qs[: settings.CATALOG_PAGE_SIZE])
    next_cursor = items[-1].id if items else None
    return render(
        request,
        "partials/newly_added_strip.html",
        {"items": items, "next_cursor": next_cursor},
    )
