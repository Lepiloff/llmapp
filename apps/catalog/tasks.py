"""Catalog background tasks.

Architecture refs:
  * docs/architecture.md § 12 (background tasks)
  * docs/business.md § 12 (quality scoring)
"""
from __future__ import annotations

import logging

from celery import shared_task

from apps.catalog.models import App
from apps.catalog.services import recalc_quality_score_bulk

logger = logging.getLogger(__name__)


@shared_task
def recalc_quality_scores_batch(batch_size: int = 200) -> dict:
    """Recompute quality scores for all published apps.

    The publish-time flow already calls ``recalc_quality_score``; this
    daily sweep catches drift driven by link-health failures, capability
    edits, and other state changes that don't pass through the publish
    transition. Walking ``App.published`` in batches keeps per-batch
    prefetch RAM bounded on large catalogs.
    """
    ids = list(App.published.values_list("pk", flat=True))
    if not ids:
        return {"processed": 0, "changed": 0}

    changed = 0
    for start in range(0, len(ids), batch_size):
        batch = ids[start : start + batch_size]
        changed += recalc_quality_score_bulk(batch)

    result = {"processed": len(ids), "changed": changed}
    logger.info("catalog_quality_score_recalc_completed", extra=result)
    return result
