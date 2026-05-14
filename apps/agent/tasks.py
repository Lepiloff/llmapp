"""Celery + orchestration entry points for the LLM-pipeline agent.

This module owns the "Django side" of a single enrichment: build
snapshots, pick a provider, invoke the pure pipeline, persist results,
write ``AgentRun`` / ``EnrichmentTask`` / ``LLMCallLog`` rows for audit.

The orchestrator is split into a plain function ``run_enrich_existing_draft``
and a Celery wrapper ``enrich_existing_draft_task``. The function form
exists so the management command can call it directly without going
through the broker — important for Phase 1 dry-runs and tests where
``CELERY_TASK_ALWAYS_EAGER`` is unset.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Count
from django.urls import reverse
from django.utils import timezone

from apps.agent.llm.client import LLMProvider, build_provider
from apps.agent.models import AgentRun, EnrichmentTask, NeedsReviewQueueEntry
from apps.agent.pipeline.discovery import DiscoveryResult, classify_candidate
from apps.agent.pipeline.fetch import FetchResult, fetch_url_text
from apps.agent.persist import (
    AppNotEligibleError,
    NewDraftPersistResult,
    PersistResult,
    apply_merge_set,
    assert_app_is_eligible,
    build_app_snapshot,
    build_taxonomy_snapshot,
    pending_enrichment_app_ids,
    persist_new_draft,
    record_llm_call,
)
from apps.agent.pipeline.enrich import (
    EnrichmentResult,
    NewAppEnrichmentResult,
    enrich_existing_draft,
    enrich_new_app,
)
from apps.agent.sources.github_mcp_search import (
    GitHubMCPSearchSource,
    candidate_to_minimal_draft,
    fetch_github_readme_text,
)
from apps.agent.sources.rss_feeds import RSSFeedSource
from apps.sources.models import Source
from apps.sources.upsert import upsert_app_from_draft

logger = logging.getLogger(__name__)

SOURCE_FLAG_ENRICH_PENDING = "enrich_pending"
SOURCE_FLAG_RSS = "rss"
SOURCE_FLAG_GITHUB_MCP = "github_mcp"


@dataclass
class EnrichOutcome:
    """High-level result of one ``run_enrich_existing_draft`` call."""

    run_id: int
    task_id: int
    result: EnrichmentResult
    persist: PersistResult | None  # None when dry_run=True
    dry_run: bool


@dataclass
class NewAppOutcome:
    """High-level result of one new-app enrichment call."""

    run_id: int
    task_id: int
    result: NewAppEnrichmentResult
    persist: NewDraftPersistResult | None
    dry_run: bool


def run_enrich_existing_draft(
    app_id: int,
    *,
    llm: LLMProvider | None = None,
    raw_source_text: str = "",
    dry_run: bool = False,
    trigger: str = AgentRun.Trigger.MANUAL,
    triggered_by: str = "",
    run: AgentRun | None = None,
    allow_non_mcp: bool = False,
) -> EnrichOutcome:
    """Enrich one DRAFT App. Phase 1 entry point.

    * ``llm`` defaults to the configured primary provider. Pass a
      ``MockLLMProvider`` in tests / fixture-driven flows.
    * ``dry_run`` controls *only* whether ``apply_merge_set`` writes to
      the catalog. Even in dry-run mode we still write
      ``AgentRun`` / ``EnrichmentTask`` / ``LLMCallLog`` rows so the
      operator has an audit trail of what was *proposed*.
    * ``run`` may be supplied by a batch driver to group multiple
      tasks under a single ``AgentRun``; otherwise a per-task run is
      created.
    """
    owns_run = run is None
    if run is None:
        run = AgentRun.objects.create(
            source_type="agent_enrich",
            status=(
                AgentRun.Status.DRY_RUN if dry_run else AgentRun.Status.RUNNING
            ),
            trigger=trigger,
            triggered_by=triggered_by[:120],
        )

    task = EnrichmentTask.objects.create(
        run=run,
        app_id=app_id,
        status=EnrichmentTask.Status.ENRICHING,
    )

    try:
        # Phase 1 invariant — fast-fail before spending LLM tokens on an
        # ineligible target. The persist layer re-checks under a row lock.
        assert_app_is_eligible(app_id, allow_non_mcp=allow_non_mcp)

        llm = llm or build_provider("primary")
        taxonomy = build_taxonomy_snapshot()
        snapshot = build_app_snapshot(app_id)

        result = enrich_existing_draft(
            snapshot, taxonomy, llm, raw_source_text=raw_source_text
        )
        record_llm_call(task, result.call_meta)

        persist: PersistResult | None = None
        if dry_run:
            task.status = EnrichmentTask.Status.DRY_RUN
        else:
            task.status = EnrichmentTask.Status.VALIDATING
            persist = apply_merge_set(
                app_id,
                result,
                source_type=Source.SourceType.AGENT_ENRICH,
                enrichment_task=task,
            )
            task.status = EnrichmentTask.Status.PERSISTED

        task.diff_summary = result.outcome.as_dict()
        task.finished_at = timezone.now()
        task.save(update_fields=["status", "diff_summary", "finished_at"])

        if owns_run:
            run.status = (
                AgentRun.Status.DRY_RUN if dry_run else AgentRun.Status.SUCCEEDED
            )
            run.finished_at = timezone.now()
            run.total_cost_usd = result.call_meta.cost_usd
            run.stats = {
                "drafts_processed": 1,
                "fields_written": (
                    len(persist.fields_written) if persist else 0
                ),
                "capabilities_written": (
                    len(persist.capabilities_written) if persist else 0
                ),
                "queue_entries": int(bool(persist and persist.queue_entry_id)),
            }
            run.save(
                update_fields=[
                    "status",
                    "finished_at",
                    "total_cost_usd",
                    "stats",
                ]
            )

        return EnrichOutcome(
            run_id=run.pk,
            task_id=task.pk,
            result=result,
            persist=persist,
            dry_run=dry_run,
        )

    except Exception as exc:
        logger.exception(
            "agent_enrich_failed",
            extra={"app_id": app_id, "task_id": task.pk},
        )
        task.status = EnrichmentTask.Status.FAILED
        task.error = str(exc)[:2000]
        task.finished_at = timezone.now()
        task.save(update_fields=["status", "error", "finished_at"])
        if owns_run:
            run.status = AgentRun.Status.FAILED
            run.error = str(exc)[:2000]
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "error", "finished_at"])
        raise


def run_enrich_new_app(
    url: str,
    *,
    source_type: str,
    external_id: str = "",
    llm: LLMProvider | None = None,
    fetcher=fetch_url_text,
    dry_run: bool = False,
    trigger: str = AgentRun.Trigger.MANUAL,
    triggered_by: str = "",
    run: AgentRun | None = None,
) -> NewAppOutcome:
    """Fetch a candidate URL, enrich it into `EnrichedDraft`, optionally persist."""
    owns_run = run is None
    if run is None:
        run = AgentRun.objects.create(
            source_type=source_type,
            status=AgentRun.Status.DRY_RUN if dry_run else AgentRun.Status.RUNNING,
            trigger=trigger,
            triggered_by=triggered_by[:120],
        )
    task = EnrichmentTask.objects.create(
        run=run,
        app=None,
        source_url=url,
        status=EnrichmentTask.Status.FETCHING,
    )
    try:
        fetched = fetcher(url)
        if not isinstance(fetched, FetchResult):
            raise TypeError("fetcher must return apps.agent.pipeline.fetch.FetchResult")

        task.status = EnrichmentTask.Status.ENRICHING
        task.save(update_fields=["status"])

        llm = llm or build_provider("primary")
        taxonomy = build_taxonomy_snapshot()
        result = enrich_new_app([fetched], taxonomy, llm)
        record_llm_call(task, result.call_meta)

        raw_payload = {
            "source_url": url,
            "external_id": external_id or f"{source_type}:{url}",
            "fetch": fetched.raw_payload,
        }
        persist: NewDraftPersistResult | None = None
        if dry_run:
            task.status = EnrichmentTask.Status.DRY_RUN
        else:
            task.status = EnrichmentTask.Status.VALIDATING
            task.save(update_fields=["status"])
            persist = persist_new_draft(
                result.sanitized_draft,
                source_type=source_type,
                external_id=external_id or f"{source_type}:{url}",
                raw_payload=raw_payload,
                result=result,
            )
            task.status = EnrichmentTask.Status.PERSISTED
            task.app_id = persist.app_id

        task.diff_summary = result.as_dict()
        task.finished_at = timezone.now()
        task.save(update_fields=["app", "status", "diff_summary", "finished_at"])

        if owns_run:
            run.status = AgentRun.Status.DRY_RUN if dry_run else AgentRun.Status.SUCCEEDED
            run.finished_at = timezone.now()
            run.total_cost_usd = result.call_meta.cost_usd
            run.stats = {
                "drafts_processed": 1,
                "persisted": int(bool(persist and persist.app_id)),
                "outcome": persist.outcome if persist else "dry_run",
            }
            run.save(update_fields=["status", "finished_at", "total_cost_usd", "stats"])

        return NewAppOutcome(
            run_id=run.pk,
            task_id=task.pk,
            result=result,
            persist=persist,
            dry_run=dry_run,
        )
    except Exception as exc:
        logger.exception("agent_enrich_new_failed", extra={"url": url, "task_id": task.pk})
        task.status = EnrichmentTask.Status.FAILED
        task.error = str(exc)[:2000]
        task.finished_at = timezone.now()
        task.save(update_fields=["status", "error", "finished_at"])
        if owns_run:
            run.status = AgentRun.Status.FAILED
            run.error = str(exc)[:2000]
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "error", "finished_at"])
        raise


@shared_task
def enrich_new_app_task(
    url: str,
    *,
    source_type: str,
    external_id: str = "",
    dry_run: bool = False,
) -> dict:
    """Celery wrapper for Phase 3 new-app enrichment."""
    outcome = run_enrich_new_app(
        url,
        source_type=source_type,
        external_id=external_id,
        dry_run=dry_run,
        trigger=AgentRun.Trigger.BEAT,
    )
    return {
        "run_id": outcome.run_id,
        "task_id": outcome.task_id,
        "dry_run": outcome.dry_run,
        "persisted": outcome.persist.as_dict() if outcome.persist else None,
    }


@shared_task
def enrich_existing_draft_task(app_id: int, *, dry_run: bool = False) -> dict:
    """Celery wrapper around ``run_enrich_existing_draft``."""
    outcome = run_enrich_existing_draft(
        app_id,
        dry_run=dry_run,
        trigger=AgentRun.Trigger.BEAT,
    )
    return {
        "run_id": outcome.run_id,
        "task_id": outcome.task_id,
        "dry_run": outcome.dry_run,
        "applied": (
            outcome.persist.as_dict() if outcome.persist is not None else None
        ),
    }


@shared_task
def enrich_pending_drafts_batch(limit: int = 10, *, dry_run: bool = False) -> dict:
    """Beat task: walk a batch of un-enriched DRAFT cards.

    Selector: DRAFT App that has no Source row with ``external_id``
    starting with ``agent-enrich:`` (see ``pending_enrichment_app_ids``).
    """
    run = AgentRun.objects.create(
        source_type="agent_enrich_batch",
        status=(
            AgentRun.Status.DRY_RUN if dry_run else AgentRun.Status.RUNNING
        ),
        trigger=AgentRun.Trigger.BEAT,
    )

    counters = {"processed": 0, "failed": 0, "queued": 0}
    total_cost = 0.0
    try:
        app_ids = pending_enrichment_app_ids(limit=limit)
        for app_id in app_ids:
            try:
                outcome = run_enrich_existing_draft(
                    app_id, dry_run=dry_run, run=run
                )
            except Exception:
                counters["failed"] += 1
                continue
            counters["processed"] += 1
            total_cost += float(outcome.result.call_meta.cost_usd)
            if outcome.persist and outcome.persist.queue_entry_id:
                counters["queued"] += 1

        run.status = (
            AgentRun.Status.DRY_RUN if dry_run else AgentRun.Status.SUCCEEDED
        )
        run.stats = counters
        run.total_cost_usd = total_cost
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "stats", "total_cost_usd", "finished_at"])
        return counters

    except Exception as exc:
        logger.exception("agent_batch_failed", extra={"run_id": run.pk})
        run.status = AgentRun.Status.FAILED
        run.error = str(exc)[:2000]
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "error", "finished_at"])
        raise


def _source_enabled(flag: str) -> bool:
    return flag in set(getattr(settings, "AGENT_SOURCES_ENABLED", []) or [])


def _record_discovery(
    *,
    run: AgentRun,
    result: DiscoveryResult,
    status: str,
) -> EnrichmentTask:
    task = EnrichmentTask.objects.create(
        run=run,
        app=None,
        source_url=result.decision.canonical_url or result.candidate.url,
        status=status,
        finished_at=timezone.now(),
        diff_summary=result.as_dict(),
    )
    record_llm_call(task, result.call_meta)
    return task


def _run_discovery_batch(
    *,
    source_flag: str,
    source_label: str,
    candidates,
    llm: LLMProvider | None,
    dry_run: bool,
    persist_github_drafts: bool = False,
    enrich_relevant: bool = False,
    enrich_llm: LLMProvider | None = None,
    fetcher=fetch_url_text,
) -> dict:
    if not dry_run and not _source_enabled(source_flag):
        return {"skipped": "source_disabled", "source": source_flag}

    run = AgentRun.objects.create(
        source_type=source_label,
        status=AgentRun.Status.DRY_RUN if dry_run else AgentRun.Status.RUNNING,
        trigger=AgentRun.Trigger.BEAT,
    )
    llm = llm or build_provider("cheap")
    counters = {"seen": 0, "relevant": 0, "skipped_existing": 0, "persisted": 0}
    total_cost = 0.0
    try:
        for candidate in candidates:
            counters["seen"] += 1
            if Source.objects.filter(external_id=candidate.external_id).exists():
                counters["skipped_existing"] += 1
                continue

            result = classify_candidate(candidate, llm)
            total_cost += float(result.call_meta.cost_usd)
            if not result.decision.is_relevant:
                _record_discovery(
                    run=run,
                    result=result,
                    status=EnrichmentTask.Status.SKIPPED,
                )
                continue

            counters["relevant"] += 1
            task_status = (
                EnrichmentTask.Status.DRY_RUN if dry_run else EnrichmentTask.Status.PENDING
            )
            _record_discovery(run=run, result=result, status=task_status)

            if enrich_relevant and not dry_run:
                enriched = run_enrich_new_app(
                    result.decision.canonical_url or candidate.url,
                    source_type=source_label,
                    external_id=candidate.external_id,
                    llm=enrich_llm,
                    fetcher=fetcher,
                    dry_run=False,
                    trigger=AgentRun.Trigger.BEAT,
                    run=run,
                )
                if enriched.persist and enriched.persist.outcome == "new":
                    counters["persisted"] += 1
            elif persist_github_drafts and not dry_run:
                draft = candidate_to_minimal_draft(candidate)
                outcome = upsert_app_from_draft(draft, Source.SourceType.GITHUB_MCP)
                if outcome in {"new", "updated", "skipped"}:
                    counters["persisted"] += int(outcome == "new")

        run.status = AgentRun.Status.DRY_RUN if dry_run else AgentRun.Status.SUCCEEDED
        run.stats = counters
        run.total_cost_usd = total_cost
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "stats", "total_cost_usd", "finished_at"])
        return counters
    except Exception as exc:
        logger.exception("agent_discovery_batch_failed", extra={"run_id": run.pk})
        run.status = AgentRun.Status.FAILED
        run.error = str(exc)[:2000]
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "error", "finished_at"])
        raise


@shared_task
def discover_rss(limit: int = 20, *, dry_run: bool = False) -> dict:
    """Phase 3 RSS discovery.

    Beat calls this with ``dry_run=False``; it no-ops until
    ``AGENT_SOURCES_ENABLED`` contains ``rss``. Manual dry-runs bypass
    the feature flag for prompt/source testing.
    """
    source = RSSFeedSource()
    candidates = source.iter_candidates(limit=limit)
    return _run_discovery_batch(
        source_flag=SOURCE_FLAG_RSS,
        source_label=Source.SourceType.RSS_DISCOVERY,
        candidates=candidates,
        llm=None,
        dry_run=dry_run,
        enrich_relevant=True,
    )


@shared_task
def discover_github_mcp(limit: int = 20, *, dry_run: bool = False) -> dict:
    """Phase 3 GitHub MCP discovery."""
    source = GitHubMCPSearchSource(token=getattr(settings, "GITHUB_TOKEN", ""))
    candidates = source.iter_candidates(limit=limit)
    github_token = getattr(settings, "GITHUB_TOKEN", "")
    return _run_discovery_batch(
        source_flag=SOURCE_FLAG_GITHUB_MCP,
        source_label=Source.SourceType.GITHUB_MCP,
        candidates=candidates,
        llm=None,
        dry_run=dry_run,
        enrich_relevant=True,
        fetcher=lambda url: fetch_github_readme_text(url, token=github_token),
    )


def review_acceptance_stats(days: int = 30) -> dict:
    """Return editor outcome stats for Phase 2 acceptance-rate tracking."""
    since = timezone.now() - timedelta(days=days)
    qs = NeedsReviewQueueEntry.objects.filter(
        created_at__gte=since,
    ).exclude(review_outcome=NeedsReviewQueueEntry.ReviewOutcome.PENDING)

    total_reviewed = qs.count()
    accepted = qs.filter(
        review_outcome__in=[
            NeedsReviewQueueEntry.ReviewOutcome.ACCEPTED,
            NeedsReviewQueueEntry.ReviewOutcome.PUBLISHED,
        ]
    ).count()
    rejected = qs.filter(
        review_outcome=NeedsReviewQueueEntry.ReviewOutcome.REJECTED
    ).count()
    no_action = qs.filter(
        review_outcome=NeedsReviewQueueEntry.ReviewOutcome.NO_ACTION
    ).count()
    return {
        "days": days,
        "reviewed": total_reviewed,
        "accepted": accepted,
        "rejected": rejected,
        "no_action": no_action,
        "acceptance_rate": (
            round((accepted / total_reviewed) * 100, 2)
            if total_reviewed
            else 0.0
        ),
    }


@shared_task
def send_review_queue_digest() -> dict:
    """Send one daily digest for open agent review queue entries.

    This is intentionally a digest of the current open queue, not a
    per-entry notification. The task is safe to run repeatedly: when
    there are no open entries or no recipients, it sends nothing.
    """
    recipients = (
        list(getattr(settings, "AGENT_REVIEW_DIGEST_EMAILS", []) or [])
        or list(getattr(settings, "SUBMISSIONS_NOTIFY_EMAILS", []) or [])
    )
    open_entries = (
        NeedsReviewQueueEntry.objects.filter(resolved_at__isnull=True)
        .select_related("app", "task", "task__run")
        .order_by("created_at")
    )
    open_count = open_entries.count()
    if not open_count:
        return {"sent": 0, "open_entries": 0, "skipped": "empty_queue"}
    if not recipients:
        return {
            "sent": 0,
            "open_entries": open_count,
            "skipped": "no_recipients",
        }

    by_kind = dict(
        open_entries.values("kind").annotate(count=Count("id")).values_list("kind", "count")
    )
    by_source = dict(
        open_entries.values("task__run__source_type")
        .annotate(count=Count("id"))
        .values_list("task__run__source_type", "count")
    )
    admin_url = f"{settings.SITE_BASE_URL}{reverse('admin:agent_needsreviewqueueentry_changelist')}"

    lines = [
        f"{open_count} agent review queue entr{'y' if open_count == 1 else 'ies'} need editor attention.",
        "",
        f"Admin queue: {admin_url}",
        "",
        "By kind:",
    ]
    for kind, count in sorted(by_kind.items()):
        lines.append(f"- {kind}: {count}")

    lines.extend(["", "By source:"])
    for source_type, count in sorted(by_source.items(), key=lambda item: str(item[0])):
        lines.append(f"- {source_type or 'unknown'}: {count}")

    lines.extend(["", "Oldest open entries:"])
    for entry in open_entries[:10]:
        detail_url = (
            f"{settings.SITE_BASE_URL}"
            f"{reverse('admin:agent_needsreviewqueueentry_change', args=[entry.pk])}"
        )
        lines.append(
            f"- #{entry.pk} {entry.app.name} ({entry.kind}, created {entry.created_at:%Y-%m-%d}): {detail_url}"
        )

    sent = send_mail(
        subject=f"[LLM App Market] {open_count} agent review item(s) need attention",
        message="\n".join(lines),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=recipients,
        fail_silently=False,
    )
    return {
        "sent": sent,
        "recipients": len(recipients),
        "open_entries": open_count,
        "by_kind": by_kind,
        "by_source": by_source,
    }
