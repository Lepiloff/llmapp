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

from celery import shared_task
from django.utils import timezone

from apps.agent.llm.client import LLMProvider, build_provider
from apps.agent.models import AgentRun, EnrichmentTask
from apps.agent.persist import (
    PersistResult,
    apply_merge_set,
    build_app_snapshot,
    build_taxonomy_snapshot,
    pending_enrichment_app_ids,
    record_llm_call,
)
from apps.agent.pipeline.enrich import EnrichmentResult, enrich_existing_draft
from apps.sources.models import Source

logger = logging.getLogger(__name__)


@dataclass
class EnrichOutcome:
    """High-level result of one ``run_enrich_existing_draft`` call."""

    run_id: int
    task_id: int
    result: EnrichmentResult
    persist: PersistResult | None  # None when dry_run=True
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
                source_type=Source.SourceType.MANUAL,
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
