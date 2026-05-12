"""Search views and filtering logic.

Architecture refs:
  * docs/architecture.md § 6 (faceted search)
  * docs/business.md § 7.2 (search page)
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.core.paginator import Paginator
from django.db.models import Q, Count, Value
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from apps.catalog.models import App, Category, Platform, Capability
from .models import SearchLog, PopularSearch
from .utils import get_client_ip


def app_search(request: HttpRequest) -> HttpResponse:
    """Main faceted search page for apps.

    This is the workhorse view that handles:
    - Text search using PostgreSQL full-text search
    - Filtering by platform, category, capabilities
    - Sorting and pagination
    """
    query = request.GET.get('q', '').strip()
    platform_filter = request.GET.get('platform')
    category_filter = request.GET.get('category')
    capability_filters = request.GET.getlist('capability')
    sort_by = request.GET.get('sort', 'relevance')

    # Start with published apps
    qs = App.published.all().for_listing()

    # Apply text search
    if query:
        # Try full-text search first
        search_query = SearchQuery(query, config='english')
        fts_qs = qs.filter(search_vector=search_query).annotate(
            search_rank=SearchRank('search_vector', search_query)
        )

        # If no FTS results, fall back to fuzzy trigram matching
        if not fts_qs.exists():
            qs = qs.extra(
                select={
                    'name_similarity': "similarity(name, %s)",
                    'dev_similarity': "similarity(developer_name, %s)",
                },
                select_params=[query, query],
                where=[
                    "similarity(name, %s) > %s OR similarity(developer_name, %s) > %s"
                ],
                params=[query, settings.CATALOG_TRIGRAM_THRESHOLD,
                       query, settings.CATALOG_TRIGRAM_THRESHOLD]
            ).annotate(search_rank=Value(1.0)).distinct()
        else:
            qs = fts_qs
    else:
        # No query - show all apps ordered by quality
        qs = qs.annotate(search_rank=Value(0.0))

    # Apply filters
    if platform_filter:
        qs = qs.filter(platforms__slug=platform_filter)

    if category_filter:
        qs = qs.filter(categories__slug=category_filter)

    if capability_filters:
        for cap in capability_filters:
            qs = qs.filter(
                appcapability__capability__key=cap,
                appcapability__value='yes'
            )

    # Apply sorting
    if sort_by == 'relevance' and query:
        qs = qs.order_by('-search_rank', '-quality_score')
    elif sort_by == 'newest':
        qs = qs.order_by('-first_seen_at')
    elif sort_by == 'name':
        qs = qs.order_by('name')
    else:  # default to quality
        qs = qs.order_by('-quality_score', '-first_seen_at')

    # Get total count before pagination
    total_results = qs.count()

    # Paginate results
    paginator = Paginator(qs, settings.CATALOG_PAGE_SIZE)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    # Get filter options for sidebar
    filter_options = get_filter_options()

    # Log the search
    if query:
        try:
            SearchLog.objects.create(
                query=query,
                filters={
                    'platform': platform_filter,
                    'category': category_filter,
                    'capabilities': capability_filters,
                    'sort': sort_by,
                },
                results_count=total_results,
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:1000],
                ip_address=get_client_ip(request),
            )
        except Exception:
            # Don't fail search if logging fails
            pass

    context = {
        'query': query,
        'page_obj': page_obj,
        'total_results': total_results,
        'current_filters': {
            'platform': platform_filter,
            'category': category_filter,
            'capabilities': capability_filters,
            'sort': sort_by,
        },
        'filter_options': filter_options,
        'popular_searches': PopularSearch.objects.filter(is_featured=True)[:10],
    }

    return render(request, 'search/app_search.html', context)


def search_suggestions(request: HttpRequest) -> HttpResponse:
    """HTMX endpoint for search autocomplete suggestions."""
    query = request.GET.get('q', '').strip()

    if len(query) < 2:
        return render(request, 'search/suggestions.html', {'suggestions': []})

    # Get app name suggestions using trigram similarity
    app_suggestions = (
        App.published.all()
        .extra(
            select={'similarity': "similarity(name, %s)"},
            select_params=[query],
            where=["similarity(name, %s) > %s"],
            params=[query, settings.CATALOG_TRIGRAM_THRESHOLD]
        )
        .order_by('-similarity', '-quality_score')
        .values_list('name', flat=True)[:5]
    )

    # Get popular search suggestions
    popular_suggestions = (
        PopularSearch.objects.filter(
            Q(query__icontains=query) | Q(display_name__icontains=query)
        )
        .order_by('-search_count')
        .values_list('display_name', flat=True)[:3]
    )

    # Combine and dedupe
    all_suggestions = list(app_suggestions) + list(popular_suggestions)
    unique_suggestions = []
    seen = set()
    for suggestion in all_suggestions:
        if suggestion.lower() not in seen:
            unique_suggestions.append(suggestion)
            seen.add(suggestion.lower())

    return render(request, 'search/suggestions.html', {
        'suggestions': unique_suggestions[:8]
    })


def get_filter_options() -> dict:
    """Get available filter options for the search sidebar."""
    platforms = Platform.objects.annotate(
        app_count=Count('apps', filter=Q(apps__status='published'))
    ).filter(app_count__gt=0).order_by('sort_order', 'name')

    categories = Category.objects.annotate(
        app_count=Count('apps', filter=Q(apps__status='published'))
    ).filter(app_count__gt=0).order_by('sort_order', 'name')

    capabilities = Capability.objects.annotate(
        app_count=Count('apps', filter=Q(
            apps__status='published',
            apps__appcapability__value='yes'
        ))
    ).filter(app_count__gt=0).order_by('sort_order', 'label')

    return {
        'platforms': platforms,
        'categories': categories,
        'capabilities': capabilities,
    }