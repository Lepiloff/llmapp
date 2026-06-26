"""Search-related background tasks.

Architecture refs:
  * docs/architecture.md § 12.2 (search vector refresh)
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.contrib.postgres.search import SearchVector
from django.db import transaction
from django.db.models import TextField, Value
from django.db.models.functions import Coalesce

from apps.catalog.models import App

logger = logging.getLogger(__name__)


@shared_task
def refresh_search_vectors_batch(batch_size: int = 100) -> dict:
    """Refresh search vectors for all published apps in batches.

    This task rebuilds the search_vector field for all apps, which is used
    for full-text search. Run this periodically to ensure search index
    stays current with app data.
    """
    updated_count = 0
    error_count = 0

    try:
        # Get all published apps that need search vector refresh
        apps_qs = App.published.all().only('id', 'name', 'short_description', 'long_description', 'developer_name')

        total_apps = apps_qs.count()
        logger.info(f"Starting search vector refresh for {total_apps} apps")

        # Process in batches to avoid memory issues
        for i in range(0, total_apps, batch_size):
            batch = apps_qs[i:i + batch_size]

            try:
                with transaction.atomic():
                    for app in batch:
                        refresh_app_search_vector(app)
                        updated_count += 1

                logger.info(f"Updated search vectors for apps {i+1}-{min(i+batch_size, total_apps)}")

            except Exception as e:
                logger.error(f"Error updating batch {i}-{i+batch_size}: {e}")
                error_count += batch_size

        result = {
            'updated_count': updated_count,
            'error_count': error_count,
            'total_count': total_apps,
        }

        logger.info(f"Search vector refresh completed: {result}")
        return result

    except Exception as e:
        logger.error(f"Search vector refresh failed: {e}")
        raise


@shared_task
def refresh_search_vector_task(app_id: int) -> bool:
    """Refresh search vector for a single app."""
    try:
        app = App.objects.get(id=app_id)
        refresh_app_search_vector(app)
        logger.info(f"Updated search vector for app {app_id}")
        return True
    except App.DoesNotExist:
        logger.warning(f"App {app_id} not found")
        return False
    except Exception as e:
        logger.error(f"Error updating search vector for app {app_id}: {e}")
        raise


def refresh_app_search_vector(app: App) -> None:
    """Refresh search vector for a single app instance.

    The ``search_vector`` GIN index is what ``/apps/?q=...`` actually
    queries (``apps/search/views.py::app_search``). Indexing only the
    four direct columns leaves platforms/categories/use-cases unsearchable
    by FTS — historically the catalog fell back to trigram matching
    over ``name`` only for those queries.

    Strategy: collapse the related-data text into ``search_index_text``,
    then build a single ``tsvector`` covering both the direct columns
    AND that aggregated field. Two SQL statements because Postgres
    UPDATE evaluates each SET clause against the pre-update row, so a
    vector that references ``search_index_text`` must be built after
    the text column has already been written.
    """
    # Build search text components — pure metadata; assigned weight C
    # so taxonomy hits never outrank a name match.
    taxonomy_parts: list[str] = []
    platform_names = list(app.platforms.values_list("name", flat=True))
    if platform_names:
        taxonomy_parts.append(" ".join(platform_names))
    category_names = list(app.categories.values_list("name", flat=True))
    if category_names:
        taxonomy_parts.append(" ".join(category_names))
    use_case_titles = list(app.use_cases.values_list("title", flat=True))
    if use_case_titles:
        taxonomy_parts.append(" ".join(use_case_titles))

    search_index_text = " ".join(taxonomy_parts)

    from apps.catalog.models import App

    # 1. Persist the aggregated taxonomy text so the vector below can
    #    reference it.
    App.objects.filter(pk=app.pk).update(search_index_text=search_index_text)

    # 2. Recompute the vector across direct columns + the freshly-written
    #    taxonomy text. ``Coalesce`` keeps the SQL stable when any field
    #    is NULL (older rows pre-default).
    search_vector = (
        SearchVector("name", weight="A", config="english")
        + SearchVector("short_description", weight="B", config="english")
        + SearchVector("developer_name", weight="B", config="english")
        + SearchVector("long_description", weight="C", config="english")
        + SearchVector(
            Coalesce("search_index_text", Value(""), output_field=TextField()),
            weight="C",
            config="english",
        )
    )

    App.objects.filter(pk=app.pk).update(
        search_vector=search_vector
    )


@shared_task
def update_popular_searches() -> dict:
    """Update popular search terms based on recent search logs."""
    from datetime import timedelta

    from django.db.models import Count
    from django.utils import timezone

    from .models import PopularSearch, SearchLog

    try:
        # Get search terms from the last 30 days
        since = timezone.now() - timedelta(days=30)

        popular_terms = (
            SearchLog.objects
            .filter(created_at__gte=since, results_count__gt=0)
            .values('query')
            .annotate(search_count=Count('id'))
            .filter(search_count__gte=5)  # Minimum 5 searches
            .order_by('-search_count')[:50]
        )

        updated_count = 0
        created_count = 0

        for term_data in popular_terms:
            query = term_data['query'].strip()
            search_count = term_data['search_count']

            if len(query) < 2 or len(query) > 200:
                continue

            popular_search, created = PopularSearch.objects.get_or_create(
                query=query,
                defaults={'search_count': search_count}
            )

            if created:
                created_count += 1
            else:
                popular_search.search_count = search_count
                popular_search.save(update_fields=['search_count'])
                updated_count += 1

        result = {
            'updated_count': updated_count,
            'created_count': created_count,
        }

        logger.info(f"Popular searches update completed: {result}")
        return result

    except Exception as e:
        logger.error(f"Popular searches update failed: {e}")
        raise


@shared_task
def cleanup_old_search_logs(days_to_keep: int | None = None) -> dict:
    """Trim ``SearchLog`` rows older than the configured retention window.

    ``PopularSearch`` is rebuilt from this table by
    ``update_popular_searches`` so a 90-day window comfortably covers
    "what did users search for in the last quarter" without bloating
    the DB.
    """
    from datetime import timedelta

    from django.conf import settings as _settings
    from django.utils import timezone

    from .models import SearchLog

    days = days_to_keep or int(
        getattr(_settings, "SEARCH_LOG_RETENTION_DAYS", 90)
    )
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = SearchLog.objects.filter(created_at__lt=cutoff).delete()
    logger.info(
        "search_logs_cleanup_completed",
        extra={"days_to_keep": days, "deleted": deleted},
    )
    return {"days_to_keep": days, "deleted": deleted}