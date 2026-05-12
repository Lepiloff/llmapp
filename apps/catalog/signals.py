"""Signal receivers that keep `App.search_vector` warm.

Architecture ref: docs/architecture.md § 6.2, § 12.

Why `transaction.on_commit`: tasks must run AFTER the transaction commits;
otherwise the worker can read a row in its pre-commit state and overwrite
the new content with the old. ``on_commit`` is a no-op outside a transaction.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver

from .models import App, AppCapability


def _schedule_refresh(app_id: int) -> None:
    """Enqueue a search-vector refresh for ``app_id`` after commit.

    The import is deferred so apps without Celery configured (tests, shell)
    can still import the module.
    """
    from apps.search.tasks import refresh_search_vector_task

    transaction.on_commit(lambda: refresh_search_vector_task.delay(app_id))


@receiver(post_save, sender=App)
def on_app_saved(sender, instance: App, **kwargs) -> None:
    _schedule_refresh(instance.pk)


@receiver(m2m_changed, sender=App.categories.through)
@receiver(m2m_changed, sender=App.platforms.through)
@receiver(m2m_changed, sender=App.use_cases.through)
@receiver(m2m_changed, sender=App.listing_types.through)
def on_app_m2m_changed(sender, instance: App, action: str, **kwargs) -> None:
    if action in {"post_add", "post_remove", "post_clear"}:
        _schedule_refresh(instance.pk)


@receiver(post_save, sender=AppCapability)
def on_app_capability_saved(sender, instance: AppCapability, **kwargs) -> None:
    _schedule_refresh(instance.app_id)
