"""Analytics background tasks."""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import Count
from django.utils import timezone

from apps.catalog.models import App

from .models import ClickEvent, PageView, TrendingScore

logger = logging.getLogger(__name__)


@shared_task
def calculate_trending_scores() -> dict[str, int]:
    """Calculate trending scores for all apps based on recent clicks.

    This task runs periodically to update the trending scores used
    for homepage and trending lists.
    """
    now = timezone.now()
    updated_count = 0
    error_count = 0

    try:
        # Get all published apps
        apps = App.published.all()

        for app in apps:
            try:
                # Calculate scores for different time windows
                score_1d = _calculate_app_trending_score(app, now - timedelta(days=1))
                score_7d = _calculate_app_trending_score(app, now - timedelta(days=7))
                score_30d = _calculate_app_trending_score(app, now - timedelta(days=30))

                # Update or create trending score record
                trending_score, created = TrendingScore.objects.get_or_create(
                    app=app,
                    defaults={
                        'score_1d': score_1d,
                        'score_7d': score_7d,
                        'score_30d': score_30d,
                    }
                )

                if not created:
                    trending_score.score_1d = score_1d
                    trending_score.score_7d = score_7d
                    trending_score.score_30d = score_30d
                    trending_score.save(update_fields=['score_1d', 'score_7d', 'score_30d', 'last_calculated_at'])

                updated_count += 1

            except Exception as e:
                logger.error(f"Error calculating trending score for app {app.id}: {e}")
                error_count += 1

        result = {
            'updated_count': updated_count,
            'error_count': error_count,
        }

        logger.info(f"Trending scores calculation completed: {result}")
        return result

    except Exception as e:
        logger.error(f"Trending scores calculation failed: {e}")
        raise


@shared_task
def cleanup_old_analytics_data(days_to_keep: int = 90) -> dict[str, int]:
    """Clean up old analytics data to keep the database size manageable.

    Removes click events and page views older than the specified number of days.
    """
    cutoff_date = timezone.now() - timedelta(days=days_to_keep)

    try:
        # Delete old click events
        deleted_clicks = ClickEvent.objects.filter(created_at__lt=cutoff_date).delete()[0]

        # Delete old page views
        deleted_page_views = PageView.objects.filter(created_at__lt=cutoff_date).delete()[0]

        result = {
            'deleted_clicks': deleted_clicks,
            'deleted_page_views': deleted_page_views,
        }

        logger.info(f"Analytics data cleanup completed: {result}")
        return result

    except Exception as e:
        logger.error(f"Analytics data cleanup failed: {e}")
        raise


def _calculate_app_trending_score(app: App, since: timezone.datetime) -> float:
    """Calculate trending score for an app since a given date.

    The algorithm considers:
    - Number of clicks (weight: 1.0)
    - Click velocity (recent clicks weighted more)
    - App quality score as a baseline
    """
    # Get clicks since the specified date
    clicks = ClickEvent.objects.filter(app=app, created_at__gte=since)

    # Count total clicks
    total_clicks = clicks.count()

    if total_clicks == 0:
        # Use quality score as baseline for apps with no clicks
        return float(app.quality_score) / 100.0

    # Calculate time-weighted score (more recent clicks have higher weight)
    now = timezone.now()
    weighted_score = 0.0

    for click in clicks.iterator():
        # Time weight: more recent = higher weight
        hours_ago = (now - click.created_at).total_seconds() / 3600
        time_weight = 1.0 / (1.0 + hours_ago / 24.0)  # Decay over 24 hours

        # Source weight: clicks from different sources have different weights
        source_weight = {
            'home': 1.0,
            'search': 1.2,  # Search clicks are more intentional
            'detail': 0.8,  # Detail page clicks are expected
            'category': 1.1,
            'platform': 1.1,
        }.get(click.source_page, 1.0)

        weighted_score += time_weight * source_weight

    # Normalize by app quality to prevent low-quality apps from trending
    quality_factor = max(0.1, app.quality_score / 100.0)

    return weighted_score * quality_factor


@shared_task
def generate_analytics_report() -> dict[str, any]:
    """Generate a periodic analytics report for internal use."""
    now = timezone.now()
    last_24h = now - timedelta(days=1)
    last_7d = now - timedelta(days=7)

    try:
        # Click statistics
        clicks_24h = ClickEvent.objects.filter(created_at__gte=last_24h).count()
        clicks_7d = ClickEvent.objects.filter(created_at__gte=last_7d).count()

        # Page view statistics
        page_views_24h = PageView.objects.filter(created_at__gte=last_24h).count()
        page_views_7d = PageView.objects.filter(created_at__gte=last_7d).count()

        # Top clicked apps in the last 7 days
        top_apps = (
            ClickEvent.objects
            .filter(created_at__gte=last_7d)
            .values('app__name', 'app__slug')
            .annotate(click_count=Count('id'))
            .order_by('-click_count')[:10]
        )

        # Most popular page types
        popular_pages = (
            PageView.objects
            .filter(created_at__gte=last_7d)
            .values('page_type')
            .annotate(view_count=Count('id'))
            .order_by('-view_count')
        )

        report = {
            'period': '7_days',
            'clicks_24h': clicks_24h,
            'clicks_7d': clicks_7d,
            'page_views_24h': page_views_24h,
            'page_views_7d': page_views_7d,
            'top_apps': list(top_apps),
            'popular_pages': list(popular_pages),
            'generated_at': now.isoformat(),
        }

        logger.info(f"Analytics report generated: {report}")
        return report

    except Exception as e:
        logger.error(f"Analytics report generation failed: {e}")
        raise