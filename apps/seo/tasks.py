"""SEO background tasks.

Architecture refs:
  * docs/architecture.md § 11 (sitemap generation)
"""
from __future__ import annotations

import logging
from django.core.management import call_command

from celery import shared_task

logger = logging.getLogger(__name__)


SITEMAP_CACHE_PREFIX = "sitemap_v1"


def invalidate_sitemap_cache() -> int:
    """Drop every cached sitemap fragment.

    ``cache_page`` stores entries under prefixed keys
    (``views.decorators.cache.cache_page.<prefix>.GET.<...>``); the
    ``django-redis`` backend supports glob-pattern deletion. Cache
    backends without ``delete_pattern`` (e.g. local-memory in tests)
    fall back to ``clear`` so the next request rebuilds.
    """
    from django.core.cache import cache

    deleted = 0
    delete_pattern = getattr(cache, "delete_pattern", None)
    if delete_pattern is not None:
        try:
            deleted = int(delete_pattern(f"*{SITEMAP_CACHE_PREFIX}*") or 0)
        except Exception:  # pragma: no cover - backend-specific failure
            logger.exception("sitemap_cache_delete_pattern_failed")
    else:
        try:
            cache.clear()
        except Exception:  # pragma: no cover - backend-specific failure
            logger.exception("sitemap_cache_clear_failed")
    return deleted


@shared_task
def rebuild_sitemap() -> dict:
    """Invalidate the sitemap cache so the next probe rebuilds.

    The sitemap view itself is wrapped in ``cache_page`` (30 min TTL).
    The beat schedule runs this task every 30 minutes so a stale
    sitemap never lives longer than that window; ``post_save`` on
    published apps also calls into here for near-immediate freshness.
    """
    deleted = invalidate_sitemap_cache()
    result = {"status": "success", "cache_keys_deleted": deleted}
    logger.info("sitemap_cache_invalidated", extra=result)
    return result


# Hard timeout for ping_search_engines — a slow Google/Bing response would
# otherwise pin the Celery worker indefinitely (urlopen defaults to no timeout).
_PING_TIMEOUT_SECONDS = 10


@shared_task
def ping_search_engines() -> dict:
    """Ping search engines about sitemap updates.

    Notifies Google and Bing that the sitemap has been updated.
    """
    import urllib.request
    from django.conf import settings

    sitemap_url = f"{settings.SITE_BASE_URL}/sitemap.xml"
    results = {}

    # Google
    try:
        google_ping_url = f"https://www.google.com/ping?sitemap={sitemap_url}"
        urllib.request.urlopen(google_ping_url, timeout=_PING_TIMEOUT_SECONDS)
        results['google'] = 'success'
        logger.info("Successfully pinged Google about sitemap update")
    except Exception as e:
        results['google'] = f'error: {e}'
        logger.error(f"Failed to ping Google: {e}")

    # Bing
    try:
        bing_ping_url = f"https://www.bing.com/ping?sitemap={sitemap_url}"
        urllib.request.urlopen(bing_ping_url, timeout=_PING_TIMEOUT_SECONDS)
        results['bing'] = 'success'
        logger.info("Successfully pinged Bing about sitemap update")
    except Exception as e:
        results['bing'] = f'error: {e}'
        logger.error(f"Failed to ping Bing: {e}")

    return results


@shared_task
def generate_seo_reports() -> dict:
    """Generate SEO health reports.

    Analyzes the site for common SEO issues and generates reports.
    """
    from django.db.models import Q, Count
    from apps.catalog.models import App
    from apps.editorial.models import Post

    try:
        # Check for apps with missing SEO data
        apps_missing_meta = App.published.filter(
            Q(meta_title='') | Q(meta_description='')
        ).count()

        apps_missing_images = App.published.filter(
            Q(logo='') | Q(logo__isnull=True)
        ).count()

        # Check for posts with missing SEO data
        posts_missing_meta = Post.objects.filter(
            status=Post.Status.PUBLISHED
        ).filter(
            Q(meta_title='') | Q(meta_description='')
        ).count()

        posts_missing_images = Post.objects.filter(
            status=Post.Status.PUBLISHED
        ).filter(
            Q(cover_image='') | Q(cover_image__isnull=True)
        ).count()

        # Check for duplicate titles
        duplicate_app_titles = (
            App.published.values('meta_title')
            .annotate(title_count=Count('meta_title'))
            .filter(title_count__gt=1)
            .count()
        )

        duplicate_post_titles = (
            Post.objects.filter(status=Post.Status.PUBLISHED)
            .values('meta_title')
            .annotate(title_count=Count('meta_title'))
            .filter(title_count__gt=1)
            .count()
        )

        report = {
            'apps': {
                'total_published': App.published.count(),
                'missing_meta': apps_missing_meta,
                'missing_images': apps_missing_images,
                'duplicate_titles': duplicate_app_titles,
            },
            'posts': {
                'total_published': Post.objects.filter(status=Post.Status.PUBLISHED).count(),
                'missing_meta': posts_missing_meta,
                'missing_images': posts_missing_images,
                'duplicate_titles': duplicate_post_titles,
            },
        }

        logger.info(f"SEO report generated: {report}")
        return report

    except Exception as e:
        logger.error(f"SEO report generation failed: {e}")
        raise
