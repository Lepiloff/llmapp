"""SEO background tasks.

Architecture refs:
  * docs/architecture.md § 14.1 (sitemap generation)
"""
from __future__ import annotations

import logging
from django.core.management import call_command

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def rebuild_sitemap() -> dict:
    """Rebuild the sitemap files.

    This task is run periodically to ensure the sitemap stays
    current with new content.
    """
    try:
        # Django doesn't have a built-in command to rebuild sitemaps,
        # but we can trigger a request to the sitemap URL to regenerate it
        from django.test import Client
        from django.urls import reverse

        client = Client()

        # Ping the sitemap to regenerate it
        response = client.get('/sitemap.xml')

        if response.status_code == 200:
            logger.info("Sitemap rebuilt successfully")
            return {'status': 'success', 'status_code': response.status_code}
        else:
            logger.error(f"Sitemap rebuild failed with status {response.status_code}")
            return {'status': 'error', 'status_code': response.status_code}

    except Exception as e:
        logger.error(f"Sitemap rebuild failed: {e}")
        raise


@shared_task
def ping_search_engines() -> dict:
    """Ping search engines about sitemap updates.

    Notifies Google and Bing that the sitemap has been updated.
    """
    import urllib.request
    from urllib.parse import urlencode
    from django.conf import settings

    sitemap_url = f"{settings.SITE_BASE_URL}/sitemap.xml"
    results = {}

    # Google
    try:
        google_ping_url = f"https://www.google.com/ping?sitemap={sitemap_url}"
        urllib.request.urlopen(google_ping_url)
        results['google'] = 'success'
        logger.info("Successfully pinged Google about sitemap update")
    except Exception as e:
        results['google'] = f'error: {e}'
        logger.error(f"Failed to ping Google: {e}")

    # Bing
    try:
        bing_ping_url = f"https://www.bing.com/ping?sitemap={sitemap_url}"
        urllib.request.urlopen(bing_ping_url)
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