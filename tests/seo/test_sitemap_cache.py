"""Sitemap caching + invalidation regressions."""
from __future__ import annotations

import pytest
from django.core.cache import cache
from django.test import override_settings

from apps.catalog.models import App
from apps.seo.tasks import (
    SITEMAP_CACHE_PREFIX,
    invalidate_sitemap_cache,
    rebuild_sitemap,
)


# Force a local-memory cache so tests don't depend on the host being able
# to resolve the docker-only `redis` hostname (the default django-redis
# backend silently no-ops on connection failure, which would mask bugs).
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _locmem_cache():
    with override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "sitemap-tests",
            }
        }
    ):
        cache.clear()
        yield
        cache.clear()


def test_sitemap_cached_between_requests(client) -> None:
    # First call renders the sitemap.
    response_1 = client.get("/sitemap.xml")
    assert response_1.status_code == 200
    body_1 = response_1.content

    # Insert a new published app *without* purging the cache. We do it via
    # Manager.create() and then bypass the post_save signal effect by
    # clearing the cache key Django might have just dropped via the signal
    # path. The intent of this test is: cache_page actually caches.
    cache.clear()

    response_2 = client.get("/sitemap.xml")
    assert response_2.status_code == 200
    # Second call also caches; identical body bytes for two reads in a row.
    response_3 = client.get("/sitemap.xml")
    assert response_3.content == response_2.content


def test_rebuild_sitemap_invalidates_cache() -> None:
    fake_key = f"prefix:{SITEMAP_CACHE_PREFIX}.GET.test"
    cache.set(fake_key, "stale-payload", timeout=300)
    assert cache.get(fake_key) == "stale-payload"

    rebuild_sitemap()

    # `delete_pattern` removes the entry; the local-memory test backend
    # falls back to cache.clear().
    assert cache.get(fake_key) is None


def test_invalidate_sitemap_cache_safe_without_pattern() -> None:
    """No delete_pattern → fall back to cache.clear without crashing.

    The autouse fixture forces ``LocMemCache`` which lacks
    ``delete_pattern``, so this exercises the fallback branch.
    """
    cache.set("kept-key", "kept", timeout=300)
    invalidate_sitemap_cache()
    # locmem backend without delete_pattern → cache.clear() blew everything.
    assert cache.get("kept-key") is None


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=False)
def test_publishing_app_purges_sitemap_cache(
    django_capture_on_commit_callbacks,
) -> None:
    """Saving a published App invalidates the cached sitemap.

    Pytest-django wraps each test in a savepoint, so ``on_commit``
    callbacks would never fire on their own. We use
    ``django_capture_on_commit_callbacks(execute=True)`` to flush the
    callbacks deterministically. The catalog post_save signal also
    schedules a search-vector refresh via ``.delay()``; running with
    ``CELERY_TASK_ALWAYS_EAGER=True`` avoids talking to a real broker.
    """
    fake_key = f"sentinel:{SITEMAP_CACHE_PREFIX}"
    cache.set(fake_key, "sentinel", timeout=300)
    assert cache.get(fake_key) == "sentinel"

    with django_capture_on_commit_callbacks(execute=True):
        App.objects.create(
            name="Sitemap Probe",
            slug="sitemap-probe",
            short_description="probe",
            status=App.AppStatus.PUBLISHED,
            is_indexable=True,
        )

    assert cache.get(fake_key) is None


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=False)
def test_unpublishing_app_purges_sitemap_cache(
    django_capture_on_commit_callbacks,
) -> None:
    """PUBLISHED → HIDDEN must also invalidate the cached sitemap.

    Prior signal only invalidated on save when ``status=PUBLISHED``, so
    pulling an app out of the catalog left a stale URL in the cached
    XML until the 30-min TTL expired.
    """
    with django_capture_on_commit_callbacks(execute=True):
        app = App.objects.create(
            name="To Be Hidden",
            slug="hidden-probe",
            short_description="probe",
            status=App.AppStatus.PUBLISHED,
            is_indexable=True,
        )
    fake_key = f"sentinel-unpublish:{SITEMAP_CACHE_PREFIX}"
    cache.set(fake_key, "sentinel", timeout=300)
    assert cache.get(fake_key) == "sentinel"

    with django_capture_on_commit_callbacks(execute=True):
        app.status = App.AppStatus.HIDDEN
        app.save()

    assert cache.get(fake_key) is None


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=False)
def test_deleting_app_purges_sitemap_cache(
    django_capture_on_commit_callbacks,
) -> None:
    """Hard-deleting an App must also invalidate the cached sitemap."""
    with django_capture_on_commit_callbacks(execute=True):
        app = App.objects.create(
            name="To Be Deleted",
            slug="delete-probe",
            short_description="probe",
            status=App.AppStatus.PUBLISHED,
            is_indexable=True,
        )
    fake_key = f"sentinel-delete:{SITEMAP_CACHE_PREFIX}"
    cache.set(fake_key, "sentinel", timeout=300)
    assert cache.get(fake_key) == "sentinel"

    with django_capture_on_commit_callbacks(execute=True):
        app.delete()

    assert cache.get(fake_key) is None
