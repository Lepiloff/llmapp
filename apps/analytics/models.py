"""Analytics models for tracking user behavior.

Architecture refs:
  * docs/architecture.md § 8 (outbound redirect/click tracking)
  * docs/business.md § 9 (outbound redirect tracking)
"""
from __future__ import annotations

from django.db import models

from apps.core.models import TimeStampedModel


class ClickEvent(TimeStampedModel):
    """Track outbound clicks for analytics and trending calculations.

    Used to understand which apps are popular and to calculate
    trending rankings.
    """

    app = models.ForeignKey(
        "catalog.App",
        on_delete=models.CASCADE,
        related_name="clicks"
    )

    # What link was clicked
    link_type = models.CharField(
        max_length=20,
        choices=[
            ('official', 'Official page'),
            ('install', 'Install URL'),
            ('repo', 'Repository'),
            ('platform', 'Platform directory'),
        ],
        default='official'
    )

    # Context of the click
    source_page = models.CharField(
        max_length=100,
        help_text="Where the click originated (home, search, detail, etc.)",
        blank=True
    )
    source_position = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Position in list/grid (for ranking analysis)"
    )

    # User context
    user_agent = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    referer = models.URLField(blank=True, max_length=500)

    # Session tracking (for deduplication)
    session_key = models.CharField(max_length=40, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["app", "-created_at"]),
            models.Index(fields=["-created_at"]),
            models.Index(fields=["source_page", "-created_at"]),
            models.Index(fields=["link_type", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"Click: {self.app.name} ({self.link_type}) from {self.source_page}"


class PageView(TimeStampedModel):
    """Track page views for analytics.

    Helps understand which pages are most popular and user flow.
    """

    page_type = models.CharField(
        max_length=20,
        choices=[
            ('home', 'Home page'),
            ('search', 'Search results'),
            ('app_detail', 'App detail'),
            ('category', 'Category page'),
            ('platform', 'Platform page'),
        ]
    )

    # Page identifier (slug for detail pages, search query for search, etc.)
    page_identifier = models.CharField(max_length=200, blank=True)

    # Context
    user_agent = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    referer = models.URLField(blank=True, max_length=500)
    session_key = models.CharField(max_length=40, blank=True)

    # Performance
    load_time_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["page_type", "-created_at"]),
            models.Index(fields=["-created_at"]),
            models.Index(fields=["page_identifier", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"Page view: {self.page_type} {self.page_identifier}"


class TrendingScore(models.Model):
    """Precomputed trending scores for apps.

    Updated by background tasks to avoid expensive calculations
    on every page load.
    """

    app = models.OneToOneField(
        "catalog.App",
        on_delete=models.CASCADE,
        related_name="trending_score"
    )

    # Scores for different time windows
    score_1d = models.FloatField(default=0.0)
    score_7d = models.FloatField(default=0.0)
    score_30d = models.FloatField(default=0.0)

    # Metadata
    last_calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["-score_7d"]),
            models.Index(fields=["-score_1d"]),
            models.Index(fields=["-score_30d"]),
        ]

    def __str__(self) -> str:
        return f"Trending: {self.app.name} (7d: {self.score_7d:.2f})"
