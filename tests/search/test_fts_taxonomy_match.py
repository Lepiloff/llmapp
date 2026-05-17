"""FTS regressions: search_vector must include platforms/categories/use-cases.

Before the fix, ``refresh_app_search_vector`` only indexed name /
short_description / long_description / developer_name. Querying for a
platform name like "mcp" or a category name like "developer tools" with
no overlap to the four base columns returned zero FTS rows; the search
view then fell back to trigram-on-name which couldn't match either.
"""
from __future__ import annotations

import pytest
from django.contrib.postgres.search import SearchQuery
from django.test import override_settings

from apps.catalog.models import App, Category, Platform, UseCase
from apps.search.tasks import refresh_app_search_vector


pytestmark = pytest.mark.django_db


def _make_app(name: str, slug: str) -> App:
    return App.objects.create(
        name=name,
        slug=slug,
        short_description="x",
        status=App.AppStatus.PUBLISHED,
        is_indexable=True,
    )


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=False)
def test_search_vector_includes_platform_name() -> None:
    platform, _ = Platform.objects.get_or_create(
        slug="mcp",
        defaults={"name": "MCP", "public_path": "mcp-servers"},
    )
    app = _make_app("Acme Tool", "acme-tool-platform")
    app.platforms.add(platform)
    refresh_app_search_vector(app)

    matches = App.objects.filter(
        pk=app.pk, search_vector=SearchQuery("mcp", config="english")
    )
    assert matches.exists()


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=False)
def test_search_vector_includes_category_name() -> None:
    category, _ = Category.objects.get_or_create(
        slug="developer-tools",
        defaults={"name": "Developer Tools"},
    )
    app = _make_app("Acme Tool", "acme-tool-category")
    app.categories.add(category)
    refresh_app_search_vector(app)

    matches = App.objects.filter(
        pk=app.pk, search_vector=SearchQuery("developer", config="english")
    )
    assert matches.exists()


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=False)
def test_search_vector_includes_use_case_title() -> None:
    use_case = UseCase.objects.create(title="Generate Reports", slug="generate-reports")
    app = _make_app("Acme Tool", "acme-tool-usecase")
    app.use_cases.add(use_case)
    refresh_app_search_vector(app)

    matches = App.objects.filter(
        pk=app.pk, search_vector=SearchQuery("reports", config="english")
    )
    assert matches.exists()


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=False)
def test_search_index_text_aggregates_taxonomy() -> None:
    platform, _ = Platform.objects.get_or_create(
        slug="mcp", defaults={"name": "MCP", "public_path": "mcp-servers"}
    )
    category, _ = Category.objects.get_or_create(
        slug="developer-tools", defaults={"name": "Developer Tools"}
    )
    use_case = UseCase.objects.create(title="Generate Reports", slug="generate-reports-2")
    app = _make_app("Acme Tool", "acme-tool-aggregated")
    app.platforms.add(platform)
    app.categories.add(category)
    app.use_cases.add(use_case)
    refresh_app_search_vector(app)
    app.refresh_from_db()

    assert "MCP" in app.search_index_text
    assert "Developer Tools" in app.search_index_text
    assert "Generate Reports" in app.search_index_text


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=False)
def test_name_still_outranks_taxonomy_match() -> None:
    """Name match (weight A) must rank higher than a category match (weight C).

    Concretely: searching for "acme" with two apps — one named Acme,
    another categorised as Acme — the named app should appear first.
    """
    from django.contrib.postgres.search import SearchRank

    category, _ = Category.objects.get_or_create(
        slug="acme-cat", defaults={"name": "Acme Cat"}
    )

    named = _make_app("Acme", "named-acme")
    refresh_app_search_vector(named)

    via_cat = _make_app("Other Tool", "other-via-cat")
    via_cat.categories.add(category)
    refresh_app_search_vector(via_cat)

    q = SearchQuery("acme", config="english")
    ranked = list(
        App.objects.filter(search_vector=q)
        .annotate(r=SearchRank("search_vector", q))
        .order_by("-r")
        .values_list("slug", flat=True)
    )
    assert ranked.index("named-acme") < ranked.index("other-via-cat")
