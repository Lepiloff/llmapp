"""Sitemap cache invalidation hooks.

The sitemap view caches its rendered XML for 30 minutes. Without an
invalidation hook a freshly-published app would not appear in
sitemap.xml until the TTL expired — search engines pinged by
``ping_search_engines`` would fetch the stale file.

Invalidation policy:
* Any ``App.save()`` triggers a purge. We don't try to diff old vs new
  status — that would need ``pre_save`` book-keeping and the cost of a
  cache.delete_pattern is negligible. Critically this covers the
  unpublish path (``PUBLISHED → HIDDEN/DRAFT``) which previously kept
  stale URLs in the cached XML until the TTL.
* ``post_delete`` mirrors the save path — removed apps shouldn't keep
  surfacing in sitemap.xml.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.catalog.models import App

logger = logging.getLogger(__name__)


def _purge_after_commit() -> None:
    """Drop the cached sitemap on commit (no-op outside a transaction)."""
    from apps.seo.tasks import invalidate_sitemap_cache

    def _purge() -> None:
        try:
            invalidate_sitemap_cache()
        except Exception:  # pragma: no cover - cache backend rare failure
            logger.exception("sitemap_invalidation_failed")

    transaction.on_commit(_purge)


@receiver(post_save, sender=App)
def on_app_saved(sender, instance: App, **kwargs) -> None:
    _purge_after_commit()


@receiver(post_delete, sender=App)
def on_app_deleted(sender, instance: App, **kwargs) -> None:
    _purge_after_commit()
