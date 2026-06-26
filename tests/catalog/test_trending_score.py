"""Regression: AppQuerySet.trending reads precomputed TrendingScore.

Before Sprint 3 the manager did a live `Count("clicks", filter=…)`
per request. We switched to reading the score table refreshed daily
by `calculate_trending_scores`, so the cost is predictable at any
catalog size.
"""
from __future__ import annotations

import pytest

from apps.analytics.models import TrendingScore
from apps.catalog.models import App

pytestmark = pytest.mark.django_db


def _make_app(slug: str, *, quality: int = 50) -> App:
    return App.objects.create(
        name=f"App {slug}",
        slug=slug,
        short_description="x",
        quality_score=quality,
        status=App.AppStatus.PUBLISHED,
        is_indexable=True,
    )


def test_trending_orders_by_score_7d_desc() -> None:
    low = _make_app("low")
    high = _make_app("high")
    TrendingScore.objects.create(app=low, score_7d=1.0)
    TrendingScore.objects.create(app=high, score_7d=10.0)

    order = list(App.published.all().trending().values_list("slug", flat=True))
    assert order.index("high") < order.index("low")


def test_trending_window_selects_correct_field() -> None:
    """window_days=1 → score_1d ordering, not score_7d."""
    a = _make_app("a")
    b = _make_app("b")
    TrendingScore.objects.create(app=a, score_1d=10.0, score_7d=1.0)
    TrendingScore.objects.create(app=b, score_1d=1.0, score_7d=10.0)

    order_1d = list(
        App.published.all().trending(window_days=1).values_list("slug", flat=True)
    )
    order_7d = list(
        App.published.all().trending(window_days=7).values_list("slug", flat=True)
    )
    assert order_1d.index("a") < order_1d.index("b")
    assert order_7d.index("b") < order_7d.index("a")


def test_apps_without_score_fall_back_to_quality() -> None:
    """No TrendingScore row → treated as 0.0, sorted by quality_score."""
    _make_app("hi-quality", quality=90)
    _make_app("lo-quality", quality=20)
    # Neither has a TrendingScore row → both 0.0 → quality breaks the tie.

    order = list(App.published.all().trending().values_list("slug", flat=True))
    assert order.index("hi-quality") < order.index("lo-quality")


def test_score_outweighs_quality() -> None:
    """An app with score=0 must rank below an app with score>0 even
    when its quality_score is higher."""
    _make_app("hi-quality", quality=90)
    trending = _make_app("trending-card", quality=20)
    TrendingScore.objects.create(app=trending, score_7d=5.0)

    order = list(App.published.all().trending().values_list("slug", flat=True))
    assert order.index("trending-card") < order.index("hi-quality")


def test_unknown_window_defaults_to_seven_days() -> None:
    a = _make_app("a")
    b = _make_app("b")
    TrendingScore.objects.create(app=a, score_7d=10.0, score_30d=1.0)
    TrendingScore.objects.create(app=b, score_7d=1.0, score_30d=10.0)

    # window_days=99 → no entry in the field_map → fall back to score_7d.
    order = list(
        App.published.all().trending(window_days=99).values_list("slug", flat=True)
    )
    assert order.index("a") < order.index("b")
