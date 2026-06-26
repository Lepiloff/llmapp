"""Search-related models and utilities.

Architecture refs:
  * docs/architecture.md § 6 (search architecture)
  * docs/architecture.md § 12.2 (search vector refresh)
"""
from __future__ import annotations

from django.contrib.postgres.indexes import GinIndex
from django.db import models

from apps.core.models import TimeStampedModel


class SearchLog(TimeStampedModel):
    """Log search queries for analytics and improvement.

    Used to understand what users are looking for and to improve
    search quality over time.
    """

    query = models.CharField(max_length=500)
    filters = models.JSONField(default=dict, blank=True)
    results_count = models.PositiveIntegerField()
    user_agent = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["query"]),
            GinIndex(fields=["query"], opclasses=["gin_trgm_ops"], name="search_log_query_trgm_gin"),
        ]

    def __str__(self) -> str:
        return f"Search: {self.query} ({self.results_count} results)"


class PopularSearch(models.Model):
    """Curated list of popular/suggested search terms.

    Displayed on search page and used for search suggestions.
    """

    query = models.CharField(max_length=200, unique=True)
    display_name = models.CharField(max_length=200, blank=True)
    search_count = models.PositiveIntegerField(default=0)
    is_featured = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=100)

    class Meta:
        ordering = ["sort_order", "-search_count", "query"]

    def __str__(self) -> str:
        return self.display_name or self.query

    def save(self, *args, **kwargs):
        if not self.display_name:
            self.display_name = self.query
        super().save(*args, **kwargs)