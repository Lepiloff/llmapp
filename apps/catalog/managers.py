"""Catalog managers and queryset extensions.

The published manager is the public-visibility gate: it is the only path
through which users see an `App`. Editors talk to `App.objects`; the rest
of the codebase reaches for `App.published`.
"""
from __future__ import annotations

from django.db import models
from django.db.models import Prefetch, QuerySet


class AppQuerySet(QuerySet):
    """Reusable query helpers — keep view code declarative and DRY."""

    def published(self) -> "AppQuerySet":
        from .models import App

        return self.filter(status=App.AppStatus.PUBLISHED, is_indexable=True)

    def for_listing(self) -> "AppQuerySet":
        """Preload everything needed to render an `app_card` partial.

        N+1 prevention: list pages render dozens of cards; without this
        prefetch each card would issue separate queries for platforms,
        categories, listing_types.
        """
        return self.prefetch_related("platforms", "categories", "listing_types")

    def for_detail(self) -> "AppQuerySet":
        """Preload everything needed to render the app detail page."""
        from .models import AppCapability, AppPlatform

        return self.prefetch_related(
            "listing_types",
            "categories",
            "use_cases",
            "sources",
            Prefetch(
                "platform_links",
                queryset=AppPlatform.objects.select_related("platform"),
            ),
            Prefetch(
                "appcapability_set",
                queryset=AppCapability.objects.select_related("capability"),
            ),
        )

    def featured(self) -> "AppQuerySet":
        return self.filter(is_featured=True)

    def trending(self, window_days: int = 7) -> "AppQuerySet":
        """Order by precomputed trending score for the given window.

        Reads from ``analytics.TrendingScore`` (refreshed by the daily
        ``calculate_trending_scores`` beat task). Previously this did a
        live ``Count("clicks", filter=…)`` per request — fine at 24
        apps, O(clicks × apps) at scale. The daily refresh trades
        sub-minute freshness for predictable query cost; apps with no
        score yet (newly published or never clicked) sort to the
        bottom by 0.0 default and fall back to quality_score for the
        tiebreak.
        """
        from django.db.models import F, FloatField, Value
        from django.db.models.functions import Coalesce

        field_map = {1: "score_1d", 7: "score_7d", 30: "score_30d"}
        order_field = field_map.get(window_days, "score_7d")

        return self.annotate(
            trending_score_value=Coalesce(
                F(f"trending_score__{order_field}"),
                Value(0.0),
                output_field=FloatField(),
            )
        ).order_by("-trending_score_value", "-quality_score")


class PublishedAppManager(models.Manager):
    """Always-published queryset for public views.

    Using a dedicated manager is safer than re-applying the filter in every
    view: a missing `.published()` call cannot leak drafts to the public.
    """

    def get_queryset(self) -> AppQuerySet:
        from .models import App

        return AppQuerySet(self.model, using=self._db).filter(
            status=App.AppStatus.PUBLISHED,
            is_indexable=True,
            launch_status__in=[
                App.LaunchStatus.LIVE,
                App.LaunchStatus.BETA,
                App.LaunchStatus.WAITLIST,
            ]
        ).exclude(launch_status=App.LaunchStatus.DEPRECATED)


class AppManager(models.Manager):
    """Default editor-side manager — sees everything, including drafts."""

    def get_queryset(self) -> AppQuerySet:
        return AppQuerySet(self.model, using=self._db)
