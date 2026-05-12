"""Search-related background tasks.

Architecture refs:
  * docs/architecture.md § 12.2 (search vector refresh)
"""
from __future__ import annotations

import logging
from typing import List

from celery import shared_task
from django.contrib.postgres.search import SearchVector
from django.db import transaction

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

    Combines name, short_description, long_description, developer_name,
    and related data into a search vector optimized for PostgreSQL
    full-text search.
    """
    # Build search text components
    search_components = []

    if app.name:
        search_components.append(f"A {app.name}")  # A weight for name

    if app.short_description:
        search_components.append(f"B {app.short_description}")  # B weight for short description

    if app.developer_name:
        search_components.append(f"B {app.developer_name}")  # B weight for developer

    if app.long_description:
        search_components.append(f"C {app.long_description}")  # C weight for long description

    # Add platform names
    platform_names = list(app.platforms.values_list('name', flat=True))
    if platform_names:
        search_components.append(f"B {' '.join(platform_names)}")

    # Add category names
    category_names = list(app.categories.values_list('name', flat=True))
    if category_names:
        search_components.append(f"C {' '.join(category_names)}")

    # Add use case titles
    use_case_titles = list(app.use_cases.values_list('title', flat=True))
    if use_case_titles:
        search_components.append(f"C {' '.join(use_case_titles)}")

    # Combine all components
    search_text = ' '.join(search_components)

    # Create search vector
    search_vector = SearchVector(
        'name', weight='A', config='english'
    ) + SearchVector(
        'short_description', weight='B', config='english'
    ) + SearchVector(
        'developer_name', weight='B', config='english'
    ) + SearchVector(
        'long_description', weight='C', config='english'
    )

    # Update the app using update() to avoid triggering signals
    from apps.catalog.models import App
    App.objects.filter(pk=app.pk).update(
        search_index_text=search_text,
        search_vector=search_vector
    )


@shared_task
def update_popular_searches() -> dict:
    """Update popular search terms based on recent search logs."""
    from django.db.models import Count
    from django.utils import timezone
    from datetime import timedelta

    from .models import SearchLog, PopularSearch

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