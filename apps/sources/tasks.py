"""Sources background tasks for ingestion and link checking.

Architecture refs:
  * docs/architecture.md § 9 (sources & ingest)
  * docs/architecture.md § 12.1 (background tasks)
  * docs/agent-pipeline.md Phase 0 (ingest converged on MCPRegistrySource
    + upsert_app_from_draft; legacy inline logic removed)
"""
from __future__ import annotations

import logging
from datetime import timedelta

import requests
from celery import shared_task
from django.db.models import F, Q
from django.utils import timezone

from apps.catalog.models import App

from .mcp_registry import MCPRegistrySource
from .models import LinkCheckResult, LinkHealth, Source, UnparsedRegistryRecord
from .upsert import upsert_app_from_draft

logger = logging.getLogger(__name__)

AUTO_DEPRECATE_FAILURE_THRESHOLD = 7


@shared_task
def ingest_mcp_registry() -> dict[str, int]:
    """Ingest MCP Registry via MCPRegistrySource + upsert_app_from_draft.

    Operational guarantees:
      * Per-record failures are isolated — one bad draft never aborts the batch.
      * Schema-mismatched records land in ``UnparsedRegistryRecord`` for editor
        review (driven by ``MCPRegistrySource.unparsed``).
      * Observed schema versions are logged so upstream breaking changes surface
        early in monitoring.
      * Network failures inside ``MCPRegistrySource`` short-circuit cleanly
        (the source returns early); the task reports zero counts rather than
        raising into the worker, so Celery does not enter a retry loop on a
        transient upstream outage — the next beat tick picks it up.
    """
    source = MCPRegistrySource()
    counters = {"new": 0, "updated": 0, "skipped": 0, "failed": 0}

    for draft in source.iter_drafts():
        try:
            outcome = upsert_app_from_draft(draft, source.source_type)
        except Exception:
            logger.exception(
                "mcp_registry_upsert_failed",
                extra={"external_id": draft.external_id, "draft_name": draft.name},
            )
            counters["failed"] += 1
            continue
        counters[outcome] += 1

    for entry in source.unparsed:
        UnparsedRegistryRecord.objects.create(
            payload=entry.get("record") or {},
            error=str(entry.get("error", ""))[:1000],
            schema_version=str(entry.get("schema_version", "")),
        )

    logger.info(
        "mcp_registry_ingest_completed",
        extra={
            **counters,
            "unparsed": len(source.unparsed),
            "schema_versions": sorted(source.observed_schema_versions),
        },
    )
    return counters


@shared_task
def check_app_links_batch(batch_size: int = 50) -> dict[str, int]:
    """Check app links for health and update LinkHealth records."""

    checked_count = 0
    failed_count = 0

    try:
        cutoff = timezone.now() - timedelta(days=1)
        apps_to_check = (
            App.published.all()
            .filter(Q(last_checked_at__lt=cutoff) | Q(last_checked_at__isnull=True))
            .order_by(F("last_checked_at").asc(nulls_first=True))[:batch_size]
        )

        for app in apps_to_check:
            try:
                _check_app_links(app)
                checked_count += 1
                App.objects.filter(pk=app.pk).update(last_checked_at=timezone.now())

            except Exception as e:
                logger.error(f"Error checking links for app {app.pk}: {e}")
                failed_count += 1

        result = {
            "checked_count": checked_count,
            "failed_count": failed_count,
        }
        logger.info(f"Link check batch completed: {result}")
        return result

    except Exception as e:
        logger.error(f"Link check batch failed: {e}")
        raise


def _check_app_links(app: App) -> None:
    """Check all links for a single app and update LinkHealth."""
    links_to_check = [
        ("official", app.official_page_url),
        ("install", app.install_url),
        ("repo", app.repo_url),
    ]

    for platform_link in app.platform_links.all():
        if platform_link.official_directory_url:
            links_to_check.append(("directory", platform_link.official_directory_url))

    for link_type, url in links_to_check:
        if not url:
            continue

        try:
            start_time = timezone.now()
            response = requests.head(url, timeout=10, allow_redirects=True)
            duration_ms = int((timezone.now() - start_time).total_seconds() * 1000)

            check_result = LinkCheckResult.objects.create(
                app=app,
                target=link_type,
                url=url,
                status_code=response.status_code,
                ok=response.status_code < 400,
                duration_ms=duration_ms,
            )

            _update_link_health(app, link_type, url, check_result.ok, response.status_code)

        except Exception as e:
            LinkCheckResult.objects.create(
                app=app,
                target=link_type,
                url=url,
                ok=False,
                error_message=str(e)[:300],
            )

            _update_link_health(app, link_type, url, False, None)


def _update_link_health(app: App, target: str, url: str, ok: bool, status_code: int = None) -> None:
    """Update or create LinkHealth record."""
    health, created = LinkHealth.objects.get_or_create(
        app=app,
        target=target,
        defaults={
            "url": url,
            "consecutive_failures": 0 if ok else 1,
            "last_status_code": status_code,
            "last_ok_at": timezone.now() if ok else None,
            "last_failed_at": None if ok else timezone.now(),
        },
    )

    if not created:
        health.url = url
        health.last_status_code = status_code

        if ok:
            health.consecutive_failures = 0
            health.last_ok_at = timezone.now()
        else:
            health.consecutive_failures += 1
            health.last_failed_at = timezone.now()

            if (
                health.consecutive_failures >= AUTO_DEPRECATE_FAILURE_THRESHOLD
                and target in ("official", "install")
            ):
                App.objects.filter(pk=app.pk).update(
                    launch_status=App.LaunchStatus.DEPRECATED
                )
                logger.warning(
                    "app_auto_deprecated_link_failures",
                    extra={
                        "app_id": app.pk,
                        "target": target,
                        "consecutive_failures": health.consecutive_failures,
                    },
                )
                # Fire the vanish review event exactly when the count
                # *crosses* the threshold. Subsequent same-failure
                # increments keep auto-deprecating the App but must not
                # spam the editor's queue. Recovery resets the counter,
                # so a later vanish naturally fires a fresh event.
                if health.consecutive_failures == AUTO_DEPRECATE_FAILURE_THRESHOLD:
                    _record_vanished_source(
                        app, target=target, url=url, status_code=status_code,
                        consecutive_failures=health.consecutive_failures,
                    )

        health.save()


def _record_vanished_source(
    app: App,
    *,
    target: str,
    url: str,
    status_code: int | None,
    consecutive_failures: int,
) -> None:
    """Flip ``Source.is_active=False`` for the dead URL and queue a review.

    The Source flip is precise — only rows whose own ``source_url``
    equals the failing ``url`` get deactivated, so an app's other live
    sources (e.g. a working GitHub repo when the official page broke)
    stay discoverable. The queue entry tells the editor which target
    broke so they can confirm or roll back the auto-deprecate.

    NeedsReviewQueueEntry is a lazy local import to keep ``apps.sources``
    importable without ``apps.agent`` (the agent app depends on sources;
    a circular import would block migrations).
    """
    from apps.agent.models import NeedsReviewQueueEntry

    deactivated = Source.objects.filter(
        app=app, source_url=url, is_active=True
    ).update(is_active=False)
    NeedsReviewQueueEntry.objects.create(
        app=app,
        kind=NeedsReviewQueueEntry.Kind.VANISHED,
        payload={
            "target": target,
            "url": url,
            "status_code": status_code,
            "consecutive_failures": consecutive_failures,
            "auto_deprecated": True,
            "sources_deactivated": deactivated,
        },
    )
    logger.warning(
        "source_vanish_review_queued",
        extra={
            "app_id": app.pk, "target": target,
            "sources_deactivated": deactivated,
        },
    )
