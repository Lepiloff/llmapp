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
from itertools import islice

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Count
from django.urls import reverse
from django.utils import timezone

from apps.agent.budget import (
    assert_agent_can_run,
    configured_budget_usd,
    current_month_cost,
    first_of_month,
    is_discovery_disabled,
)
from apps.agent.llm.client import LLMProvider, build_provider
from apps.agent.models import (
    AgentRun, BudgetMonthState, EnrichmentTask, NeedsReviewQueueEntry,
)
from apps.agent.pipeline.discovery import DiscoveryResult, classify_candidate
from apps.agent.pipeline.fetch import FetchResult, fetch_url_text
from apps.agent.persist import (
    AppNotEligibleError,
    NewDraftPersistResult,
    PersistResult,
    ReactualizationPersistResult,
    apply_merge_set,
    assert_app_is_eligible,
    build_app_snapshot,
    build_taxonomy_snapshot,
    pending_enrichment_app_ids,
    pending_reactualization_app_ids,
    persist_new_draft,
    pick_primary_active_source,
    queue_reactualization,
    record_llm_call,
)
from apps.agent.pipeline.enrich import (
    EnrichmentResult,
    NewAppEnrichmentResult,
    enrich_existing_draft,
    enrich_new_app,
)
from apps.agent.pipeline.reactualize import (
    ReactualizationDiff,
    compute_reactualization,
)
from apps.agent.sources.claude_connectors import ClaudeConnectorsSource
from apps.agent.sources.chatgpt_apps import ChatGPTAppsSource
from apps.agent.sources.gemini_extensions import GeminiExtensionsSource
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
SOURCE_FLAG_GEMINI_EXTENSIONS = "gemini_extensions"
SOURCE_FLAG_CLAUDE_CONNECTORS = "claude_connectors"
SOURCE_FLAG_CHATGPT_APPS = "chatgpt_apps"


def _fetcher_for_url(url: str) -> "Callable[[str], FetchResult]":
    """Pick a URL fetcher by host.

    GitHub repo URLs go through the README-via-API helper — the repo
    home page doesn't serve README markdown in raw HTML, the API does.
    Everything else uses ``fetch_url_text``. Dispatching on host rather
    than ``source.source_type`` is the right level: the canonical URL
    the LLM picked at discovery time often differs from the source
    *kind*, e.g. a github_mcp source whose ``source_url`` ended up on
    the vendor's product page (Speakeasy/gram, 2026-05-16).
    """
    if "github.com/" in url:
        github_token = getattr(settings, "GITHUB_TOKEN", "")
        return lambda u: fetch_github_readme_text(u, token=github_token)
    return fetch_url_text


def _source_fetch_url(source) -> str:
    """Best URL to re-fetch a Source from.

    For github_mcp rows, prefer ``payload.fetch.repo_url`` because the
    canonical ``source_url`` may hold a product page chosen by the LLM
    at discovery time. For every other source type, ``source_url`` is
    the right starting point; the discovery payload's canonical URL is
    the last fallback.
    """
    payload = source.payload or {}
    fetch_block = payload.get("fetch") or {}

    if source.source_type == Source.SourceType.GITHUB_MCP:
        repo_url = fetch_block.get("repo_url")
        if repo_url:
            return repo_url
    if source.source_url:
        return source.source_url
    return fetch_block.get("url") or payload.get("source_url") or ""


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
    assert_agent_can_run()
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
    assert_agent_can_run()
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
    if not dry_run and not _source_enabled(SOURCE_FLAG_ENRICH_PENDING):
        return {"skipped": "source_disabled", "source": SOURCE_FLAG_ENRICH_PENDING}
    if not dry_run and is_discovery_disabled():
        return {"skipped": "budget_threshold", "source": SOURCE_FLAG_ENRICH_PENDING}

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
    if not dry_run and is_discovery_disabled():
        return {"skipped": "budget_threshold", "source": source_flag}

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


def run_direct_ingest_batch(
    *,
    source_flag: str,
    source_label: str,
    drafts,
    dry_run: bool,
    limit: int | None = None,
    enforce_flag: bool = True,
    trigger: str = AgentRun.Trigger.BEAT,
) -> dict:
    """Persist normalized AppDrafts from a non-LLM source.

    Direct ingest sources already produce trusted-enough normalized drafts;
    they do not pass through cheap classification or enrichment, so this
    helper records batch audit stats but does not create LLMCallLog rows.
    """
    if not dry_run and enforce_flag and not _source_enabled(source_flag):
        return {"skipped": "source_disabled", "source": source_flag}

    run = AgentRun.objects.create(
        source_type=source_label,
        status=AgentRun.Status.DRY_RUN if dry_run else AgentRun.Status.RUNNING,
        trigger=trigger,
    )
    counters = {"seen": 0, "new": 0, "updated": 0, "skipped": 0, "failed": 0}
    iterable = islice(drafts, limit) if limit is not None else drafts
    try:
        for draft in iterable:
            counters["seen"] += 1
            if dry_run:
                continue
            try:
                outcome = upsert_app_from_draft(draft, source_label)
            except Exception:
                counters["failed"] += 1
                logger.exception(
                    "agent_direct_ingest_upsert_failed",
                    extra={
                        "source": source_label,
                        "external_id": getattr(draft, "external_id", ""),
                        "draft_name": getattr(draft, "name", ""),
                    },
                )
                continue
            counters[outcome] += 1

        run.status = AgentRun.Status.DRY_RUN if dry_run else AgentRun.Status.SUCCEEDED
        run.stats = counters
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "stats", "finished_at"])
        return counters
    except Exception as exc:
        logger.exception("agent_direct_ingest_batch_failed", extra={"run_id": run.pk})
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


@shared_task
def ingest_gemini_extensions(
    limit: int | None = None,
    *,
    dry_run: bool = False,
    enforce_flag: bool = True,
    trigger: str = AgentRun.Trigger.BEAT,
) -> dict:
    """Direct-ingest Gemini CLI extensions from Google's public JSON feed."""
    source = GeminiExtensionsSource()
    return run_direct_ingest_batch(
        source_flag=SOURCE_FLAG_GEMINI_EXTENSIONS,
        source_label=Source.SourceType.GEMINI_EXTENSIONS,
        drafts=source.iter_drafts(),
        dry_run=dry_run,
        limit=limit,
        enforce_flag=enforce_flag,
        trigger=trigger,
    )


@shared_task
def ingest_claude_connectors(
    limit: int | None = None,
    *,
    dry_run: bool = False,
    enforce_flag: bool = True,
    trigger: str = AgentRun.Trigger.BEAT,
) -> dict:
    """Direct-ingest public Claude Connectors pages."""
    source = ClaudeConnectorsSource()
    return run_direct_ingest_batch(
        source_flag=SOURCE_FLAG_CLAUDE_CONNECTORS,
        source_label=Source.SourceType.CLAUDE_CONNECTORS,
        drafts=source.iter_drafts(),
        dry_run=dry_run,
        limit=limit,
        enforce_flag=enforce_flag,
        trigger=trigger,
    )


@shared_task
def ingest_chatgpt_apps(
    limit: int | None = None,
    *,
    dry_run: bool = False,
    enforce_flag: bool = True,
    trigger: str = AgentRun.Trigger.BEAT,
) -> dict:
    """Direct-ingest ChatGPT Apps from a crawlable third-party index."""
    source = ChatGPTAppsSource()
    return run_direct_ingest_batch(
        source_flag=SOURCE_FLAG_CHATGPT_APPS,
        source_label=Source.SourceType.CHATGPT_UNOFFICIAL,
        drafts=source.iter_drafts(),
        dry_run=dry_run,
        limit=limit,
        enforce_flag=enforce_flag,
        trigger=trigger,
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


# ---------------------------------------------------------------------------
# Phase 4 — re-actualization orchestration
# ---------------------------------------------------------------------------
@dataclass
class ReactualizeOutcome:
    """High-level result for one ``run_reactualize_app`` invocation."""

    run_id: int
    task_id: int
    diff: ReactualizationDiff | None
    persist: ReactualizationPersistResult | None
    dry_run: bool
    skipped_reason: str = ""


def run_reactualize_app(
    app_id: int,
    *,
    llm: LLMProvider | None = None,
    fetcher=None,
    dry_run: bool = False,
    trigger: str = AgentRun.Trigger.MANUAL,
    triggered_by: str = "",
    run: AgentRun | None = None,
) -> ReactualizeOutcome:
    """Re-run enrichment on a published App and queue any drift for review.

    Owns the full pipeline for one App:
      1. Pick the freshest active re-actualizable Source.
      2. Re-fetch its URL with the per-source-type fetcher.
      3. ``enrich_new_app`` produces a fresh ``EnrichedDraft``.
      4. ``compute_reactualization`` diffs against the App's snapshot.
      5. ``queue_reactualization`` writes ``NeedsReviewQueueEntry`` and
         refreshes ``Source.last_enriched_at`` / ``payload``.

    Never modifies App fields — that contract lives in
    :func:`apps.agent.persist.queue_reactualization`. ``dry_run`` skips
    only the persist step; the AgentRun / EnrichmentTask / LLMCallLog
    audit rows are written regardless so operators see exactly what was
    proposed during a dry probe.
    """
    assert_agent_can_run()
    owns_run = run is None
    if run is None:
        run = AgentRun.objects.create(
            source_type="agent_reactualize",
            status=AgentRun.Status.DRY_RUN if dry_run else AgentRun.Status.RUNNING,
            trigger=trigger,
            triggered_by=triggered_by[:120],
        )
    task = EnrichmentTask.objects.create(
        run=run,
        app_id=app_id,
        status=EnrichmentTask.Status.FETCHING,
    )

    def _finalize(*, status, diff, persist, skipped_reason="", cost=0.0):
        task.diff_summary = (
            diff.as_dict() if diff is not None else {"skipped": skipped_reason}
        )
        task.status = status
        task.finished_at = timezone.now()
        task.save(update_fields=["status", "diff_summary", "finished_at"])
        if owns_run:
            run.status = (
                AgentRun.Status.DRY_RUN if dry_run else AgentRun.Status.SUCCEEDED
            )
            run.stats = {
                "apps_processed": 1,
                "queue_entries": int(bool(persist and persist.queue_entry_id)),
                "skipped": int(bool(skipped_reason)),
            }
            run.total_cost_usd = cost
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "stats", "total_cost_usd", "finished_at"])
        return ReactualizeOutcome(
            run_id=run.pk, task_id=task.pk, diff=diff,
            persist=persist, dry_run=dry_run, skipped_reason=skipped_reason,
        )

    try:
        source = pick_primary_active_source(app_id)
        if source is None:
            return _finalize(
                status=EnrichmentTask.Status.SKIPPED,
                diff=None, persist=None,
                skipped_reason="no_active_reactualizable_source",
            )

        url = _source_fetch_url(source)
        if not url:
            return _finalize(
                status=EnrichmentTask.Status.SKIPPED,
                diff=None, persist=None,
                skipped_reason="source_has_no_url",
            )

        active_fetcher = fetcher or _fetcher_for_url(url)
        fetched = active_fetcher(url)
        if not isinstance(fetched, FetchResult):
            raise TypeError("fetcher must return apps.agent.pipeline.fetch.FetchResult")

        task.status = EnrichmentTask.Status.ENRICHING
        task.save(update_fields=["status"])

        llm = llm or build_provider("primary")
        taxonomy = build_taxonomy_snapshot()
        result = enrich_new_app([fetched], taxonomy, llm)
        record_llm_call(task, result.call_meta)

        snapshot = build_app_snapshot(app_id)
        diff = compute_reactualization(snapshot, result.sanitized_draft)

        persist: ReactualizationPersistResult | None = None
        if dry_run:
            status = EnrichmentTask.Status.DRY_RUN
        else:
            persist = queue_reactualization(
                diff,
                source_id=source.pk,
                enrichment_task=task,
                raw_fetch_payload={
                    "url": url,
                    "source_type": source.source_type,
                    "fetch": fetched.raw_payload,
                },
            )
            status = EnrichmentTask.Status.PERSISTED
        return _finalize(
            status=status, diff=diff, persist=persist,
            cost=float(result.call_meta.cost_usd),
        )

    except Exception as exc:
        logger.exception(
            "agent_reactualize_failed",
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
def reactualize_apps_batch(
    limit: int | None = None,
    *,
    dry_run: bool = False,
    interval_days: int | None = None,
) -> dict:
    """Daily beat: re-actualize a bounded batch of overdue apps.

    Gated by ``AGENT_REACTUALIZATION_ENABLED`` so the schedule entry can
    sit in code without firing on dev / staging until an operator
    explicitly opts in. ``dry_run=True`` bypasses the flag for manual
    probes (mirrors the discovery-task convention).
    """
    if not dry_run and not getattr(
        settings, "AGENT_REACTUALIZATION_ENABLED", False
    ):
        return {"skipped": "reactualization_disabled"}

    limit = limit or int(getattr(settings, "AGENT_REACTUALIZATION_BATCH_SIZE", 20))
    interval = interval_days or int(
        getattr(settings, "AGENT_REACTUALIZATION_INTERVAL_DAYS", 30)
    )

    run = AgentRun.objects.create(
        source_type="agent_reactualize_batch",
        status=AgentRun.Status.DRY_RUN if dry_run else AgentRun.Status.RUNNING,
        trigger=AgentRun.Trigger.BEAT,
    )
    counters = {"processed": 0, "queued": 0, "skipped": 0, "failed": 0}
    total_cost = 0.0
    try:
        app_ids = pending_reactualization_app_ids(
            interval_days=interval, limit=limit
        )
        for app_id in app_ids:
            try:
                outcome = run_reactualize_app(
                    app_id, dry_run=dry_run, run=run
                )
            except Exception:
                counters["failed"] += 1
                continue
            counters["processed"] += 1
            if outcome.skipped_reason:
                counters["skipped"] += 1
                continue
            if outcome.persist and outcome.persist.queue_entry_id:
                counters["queued"] += 1

        # Sum cost via LLMCallLog aggregate so we don't double-track:
        # each EnrichmentTask write hits LLMCallLog already, and the
        # batch-level run row is the canonical place for the aggregate.
        from django.db.models import Sum as _Sum
        from apps.agent.models import LLMCallLog as _LLMCallLog
        total_cost = float(
            _LLMCallLog.objects.filter(task__run=run).aggregate(
                s=_Sum("cost_usd")
            )["s"] or 0
        )

        run.status = AgentRun.Status.DRY_RUN if dry_run else AgentRun.Status.SUCCEEDED
        run.stats = counters
        run.total_cost_usd = total_cost
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "stats", "total_cost_usd", "finished_at"])
        return counters
    except Exception as exc:
        logger.exception("agent_reactualize_batch_failed", extra={"run_id": run.pk})
        run.status = AgentRun.Status.FAILED
        run.error = str(exc)[:2000]
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "error", "finished_at"])
        raise


# ---------------------------------------------------------------------------
# Phase 5 — monthly budget hard-stop
# ---------------------------------------------------------------------------
_BUDGET_DISCOVERY_THRESHOLD = 0.80  # 80% disables discovery
_BUDGET_HARD_STOP_THRESHOLD = 1.00  # 100% blocks all agent work


@shared_task
def agent_budget_check() -> dict:
    """Hourly beat: recompute monthly spend, flip latches, email once.

    Two thresholds:
      * **80%** — disable discovery; re-actualization keeps running.
      * **100%** — hard stop on every new agent LLM call.

    Both latch on first crossing within a month (manual edit to the
    ``BudgetMonthState`` row clears them; the next calendar month
    starts a fresh row, fresh latches). Email recipients come from
    ``AGENT_BUDGET_ALERT_EMAILS`` (falls back to
    ``AGENT_REVIEW_DIGEST_EMAILS`` then ``SUBMISSIONS_NOTIFY_EMAILS``)
    and a missing list is logged + sent-count zero — the latch still
    flips so workers gate correctly even without alerting.
    """
    budget = configured_budget_usd()
    cost = current_month_cost()
    month_start = first_of_month()
    now = timezone.now()

    state, _ = BudgetMonthState.objects.get_or_create(
        month=month_start,
        defaults={"total_cost_usd": cost, "budget_usd": budget},
    )
    state.total_cost_usd = cost
    state.budget_usd = budget

    util = float(cost / budget) if budget > 0 else 0.0
    crossed_discovery_now = False
    crossed_hard_stop_now = False

    if budget > 0:
        if util >= _BUDGET_HARD_STOP_THRESHOLD and state.hard_stop_at is None:
            state.hard_stop_at = now
            crossed_hard_stop_now = True
        if util >= _BUDGET_DISCOVERY_THRESHOLD and state.discovery_disabled_at is None:
            state.discovery_disabled_at = now
            crossed_discovery_now = True

    state.save()

    sent_80 = 0
    sent_100 = 0
    if crossed_hard_stop_now:
        sent_100 = _send_budget_alert(
            subject_threshold=100,
            cost=cost, budget=budget, state=state,
        )
        state.notified_100_at = now
        state.save(update_fields=["notified_100_at"])
    elif crossed_discovery_now:
        sent_80 = _send_budget_alert(
            subject_threshold=80,
            cost=cost, budget=budget, state=state,
        )
        state.notified_80_at = now
        state.save(update_fields=["notified_80_at"])

    return {
        "month": month_start.isoformat(),
        "total_cost_usd": float(cost),
        "budget_usd": float(budget),
        "utilization": util,
        "discovery_disabled": state.is_discovery_disabled,
        "hard_stopped": state.is_hard_stopped,
        "notified_80_sent": sent_80,
        "notified_100_sent": sent_100,
    }


def _send_budget_alert(
    *, subject_threshold: int, cost, budget, state: BudgetMonthState
) -> int:
    """Email recipients about a budget threshold crossing. Returns sent count."""
    recipients = (
        list(getattr(settings, "AGENT_BUDGET_ALERT_EMAILS", []) or [])
        or list(getattr(settings, "AGENT_REVIEW_DIGEST_EMAILS", []) or [])
        or list(getattr(settings, "SUBMISSIONS_NOTIFY_EMAILS", []) or [])
    )
    if not recipients:
        logger.warning(
            "agent_budget_alert_no_recipients",
            extra={"threshold": subject_threshold, "month": state.month.isoformat()},
        )
        return 0
    subject = (
        f"[llmappmarket] Agent budget {subject_threshold}% reached — "
        f"${cost:.2f} / ${budget:.2f} ({state.month:%Y-%m})"
    )
    if subject_threshold >= 100:
        body = (
            f"Monthly agent budget exhausted for {state.month:%Y-%m}.\n"
            f"Total cost so far: ${cost:.4f} of ${budget:.2f}.\n\n"
            "Hard stop is now active: enrichment, re-actualization, and "
            "discovery tasks will refuse to run until manual review.\n\n"
            "To reset: bump AGENT_MONTHLY_BUDGET_USD or clear "
            "BudgetMonthState.hard_stop_at in the admin."
        )
    else:
        body = (
            f"Monthly agent budget reached 80% for {state.month:%Y-%m}.\n"
            f"Total cost so far: ${cost:.4f} of ${budget:.2f}.\n\n"
            "Discovery sources have been auto-disabled. Re-actualization "
            "and on-demand enrichment continue to run.\n\n"
            "To re-enable discovery before the budget refills, clear "
            "BudgetMonthState.discovery_disabled_at in the admin."
        )
    return send_mail(
        subject=subject,
        message=body,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@llmappmarket.com"),
        recipient_list=recipients,
        fail_silently=False,
    )


# ---------------------------------------------------------------------------
# Retention — agent-log cleanup
# ---------------------------------------------------------------------------
@shared_task
def cleanup_old_agent_logs(days_to_keep: int | None = None) -> dict:
    """Drop ``AgentRun`` rows older than ``days_to_keep``.

    ``EnrichmentTask`` and ``LLMCallLog`` cascade off ``AgentRun`` (FK
    on_delete=CASCADE), so deleting old runs reclaims the full audit
    chain in one statement. Resolved ``NeedsReviewQueueEntry`` rows are
    deleted via the same cutoff — pending entries are preserved
    regardless of age so editors don't lose work.
    """
    from apps.agent.models import AgentRun, NeedsReviewQueueEntry

    days = days_to_keep or int(
        getattr(settings, "AGENT_LOG_RETENTION_DAYS", 180)
    )
    cutoff = timezone.now() - timedelta(days=days)

    deleted_runs, _ = AgentRun.objects.filter(started_at__lt=cutoff).delete()
    deleted_queue, _ = NeedsReviewQueueEntry.objects.filter(
        created_at__lt=cutoff,
        resolved_at__isnull=False,
    ).delete()

    result = {
        "days_to_keep": days,
        "deleted_runs": deleted_runs,
        "deleted_resolved_queue_entries": deleted_queue,
    }
    logger.info("agent_logs_cleanup_completed", extra=result)
    return result
