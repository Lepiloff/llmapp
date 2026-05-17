"""Regression: merge_use_cases collapses synonyms without losing AppUseCase coverage."""
from __future__ import annotations

import pytest
from django.test import override_settings

from apps.catalog.models import App, AppUseCase, UseCase
from apps.catalog.services import merge_use_cases


pytestmark = pytest.mark.django_db


@pytest.fixture
def apps(db):
    return [
        App.objects.create(name=f"App {i}", slug=f"app-{i}", short_description="x")
        for i in range(3)
    ]


@pytest.fixture
def use_cases(db):
    return {
        "target": UseCase.objects.create(title="Generate Reports", slug="generate-reports"),
        "syn_a": UseCase.objects.create(title="Generate a report", slug="generate-a-report"),
        "syn_b": UseCase.objects.create(title="Report generation", slug="report-generation"),
    }


def test_reassigns_rows_into_target(apps, use_cases) -> None:
    a, b, c = apps
    AppUseCase.objects.create(app=a, use_case=use_cases["syn_a"])
    AppUseCase.objects.create(app=b, use_case=use_cases["syn_b"])

    stats = merge_use_cases(
        use_cases["target"].pk,
        [use_cases["syn_a"].pk, use_cases["syn_b"].pk],
    )

    assert stats == {"reassigned": 2, "deduplicated": 0, "deleted_use_cases": 2}
    assert not UseCase.objects.filter(pk=use_cases["syn_a"].pk).exists()
    assert not UseCase.objects.filter(pk=use_cases["syn_b"].pk).exists()
    target_apps = set(
        AppUseCase.objects.filter(use_case=use_cases["target"]).values_list("app_id", flat=True)
    )
    assert target_apps == {a.pk, b.pk}


def test_deduplicates_when_app_already_has_target(apps, use_cases) -> None:
    a, _, _ = apps
    AppUseCase.objects.create(app=a, use_case=use_cases["target"])
    AppUseCase.objects.create(app=a, use_case=use_cases["syn_a"])

    stats = merge_use_cases(use_cases["target"].pk, [use_cases["syn_a"].pk])

    assert stats["reassigned"] == 0
    assert stats["deduplicated"] == 1
    assert stats["deleted_use_cases"] == 1
    # App still has exactly one row, pointing at the target.
    rows = AppUseCase.objects.filter(app=a)
    assert rows.count() == 1
    assert rows.first().use_case_id == use_cases["target"].pk


def test_target_in_sources_list_is_ignored(apps, use_cases) -> None:
    """Passing the target's own pk in sources is a no-op (safety net)."""
    a, _, _ = apps
    AppUseCase.objects.create(app=a, use_case=use_cases["syn_a"])
    stats = merge_use_cases(
        use_cases["target"].pk,
        [use_cases["target"].pk, use_cases["syn_a"].pk],
    )
    assert stats["reassigned"] == 1
    assert UseCase.objects.filter(pk=use_cases["target"].pk).exists()


def test_unknown_source_ids_silently_skipped(use_cases) -> None:
    stats = merge_use_cases(use_cases["target"].pk, [999_999])
    assert stats == {"reassigned": 0, "deduplicated": 0, "deleted_use_cases": 0}


def test_missing_target_raises(use_cases) -> None:
    with pytest.raises(ValueError):
        merge_use_cases(999_999, [use_cases["syn_a"].pk])
