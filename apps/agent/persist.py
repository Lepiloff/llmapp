"""Django bridge — the only place the agent touches the ORM.

Two responsibilities:

1. **Build pure-Python views** the pipeline can consume without
   importing Django: ``build_taxonomy_snapshot``, ``build_app_snapshot``.
2. **Apply pipeline outputs** to the DB safely:
   ``apply_merge_set`` writes the ``Plan`` and queues the
   ``QueueProposal`` for editor review.

Hard invariants (also enforced by tests):

* ``App.status``, ``App.editorial_review_status``,
  ``App.platform_verification_status``, ``App.developer_claim_status``,
  ``App.verdict`` — **never modified** by this module. Even attempting
  to change them in the merge plan is treated as a bug; we never
  reference those columns in any UPDATE here.
* All writes go through ``.update(...)`` or ``add(...)`` and skip
  ``.save()`` — search-vector refresh fires from m2m_changed and
  post_save on AppCapability, which IS what we want for additive
  taxonomy / capability writes.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.text import slugify

from apps.agent.llm.schemas import AppSnapshot, EnrichedDraft
from apps.agent.models import EnrichmentTask, LLMCallLog, NeedsReviewQueueEntry
from apps.agent.pipeline.enrich import EnrichmentResult, NewAppEnrichmentResult
from apps.agent.pipeline.merge import Plan, QueueProposal
from apps.agent.pipeline.reactualize import ReactualizationDiff
from apps.agent.pipeline.taxonomy import TaxonomySnapshot
from apps.catalog.models import (
    App,
    AppCapability,
    Capability,
    Category,
    ListingType,
    Platform,
    UseCase,
)
from apps.sources.base import AppDraft
from apps.sources.models import Source
from apps.sources.upsert import upsert_app_from_draft

logger = logging.getLogger(__name__)


# Maps a ListingType slug (the LLM's "what kind of thing is this" call)
# to the canonical Platform slug it belongs to. Listing types are what
# the LLM can reliably classify from a README; Platform membership for
# any real app is the editor's call (it requires checking the official
# directory), so this mapping is only used to seed an initial Platform
# row on agent-discovered DRAFTs. Two listing types map to `claude`
# because both Claude Connectors and Interactive Claude Apps live under
# the same Claude ecosystem on the catalog side.
#
# Listing-type slugs the LLM proposes but that aren't in this map (a
# net-new shape the catalog doesn't have a Platform for yet) yield no
# platform, leaving the DRAFT for editor review — exactly the Trigger.dev
# #12 failure mode (workflow runtime, no MCP-server listing → empty
# platforms → publish blocked, which is correct).
_LISTING_TYPE_TO_PLATFORM: dict[str, str] = {
    "chatgpt-app": "chatgpt",
    "claude-connector": "claude",
    "interactive-claude-app": "claude",
    "mcp-server": "mcp",
    "gemini-app": "gemini",
    "enterprise-agent": "enterprise",
}


def _derive_platforms(listing_type_slugs: Iterable[str]) -> list[str]:
    """Map proposed listing types to canonical Platform slugs (deduped)."""
    seen: list[str] = []
    for lt_slug in listing_type_slugs:
        platform = _LISTING_TYPE_TO_PLATFORM.get(lt_slug)
        if platform and platform not in seen:
            seen.append(platform)
    return seen


class AppNotEligibleError(ValueError):
    """Raised when the agent is asked to enrich an App that doesn't qualify.

    Phase 1 invariant (docs/agent-pipeline.md): the agent only processes
    DRAFT cards. PUBLISHED / HIDDEN apps are off-limits even via direct
    slug. The guard fires *before* the LLM call (so we don't spend tokens
    on an ineligible target) and *again* inside the persist transaction
    (so a race that publishes the card between snapshot and apply still
    cannot result in agent writes against an editorially-managed row).
    """

    def __init__(self, app_id: int, status: str, reason: str = "not DRAFT") -> None:
        super().__init__(
            f"App {app_id} is not eligible for agent enrichment "
            f"(status={status!r}, reason={reason})"
        )
        self.app_id = app_id
        self.status = status
        self.reason = reason


# Source types accepted by batch enrichment. The explicit allowlist forces
# every new automated source to be considered rather than silently letting any
# DRAFT through; manual editor entries and user submissions stay out of
# scheduled LLM writes.
_PHASE_1_ELIGIBLE_SOURCE_TYPES: tuple[str, ...] = (
    Source.SourceType.MCP_REGISTRY,
    Source.SourceType.GEMINI_EXTENSIONS,
    Source.SourceType.CLAUDE_CONNECTORS,
    Source.SourceType.CHATGPT_UNOFFICIAL,
)


# ---------------------------------------------------------------------------
# Eligibility check — call BEFORE the LLM, raise loud on miss.
# ---------------------------------------------------------------------------
def assert_app_is_eligible(app_id: int, *, allow_non_mcp: bool = False) -> None:
    """Pre-flight check used by ``tasks.run_enrich_existing_draft``.

    Enrichment invariants:

    * ``App.status == DRAFT`` (no agent writes against published / hidden).
    * App has an automated import ``Source`` type in
      ``_PHASE_1_ELIGIBLE_SOURCE_TYPES``.

    ``allow_non_mcp`` is an explicit operator override for prompt
    iteration against a card that isn't MCP-sourced — the ``--allow-non-mcp``
    flag on the management command threads through to here. The override
    intentionally cannot be set via env / settings; it must be re-typed
    every run so no scheduled task can pick it up silently.

    Cheap: one indexed PK read + one EXISTS subquery. Lets the
    orchestrator fail fast before spending LLM tokens on an ineligible
    target. The persist layer re-checks the status invariant under a
    row lock.
    """
    try:
        status = App.objects.values_list("status", flat=True).get(pk=app_id)
    except App.DoesNotExist as exc:
        raise AppNotEligibleError(app_id, "missing", "no such App") from exc
    if status != App.AppStatus.DRAFT:
        raise AppNotEligibleError(app_id, status)

    if allow_non_mcp:
        return

    has_eligible_source = Source.objects.filter(
        app_id=app_id, source_type__in=_PHASE_1_ELIGIBLE_SOURCE_TYPES
    ).exists()
    if not has_eligible_source:
        raise AppNotEligibleError(
            app_id,
            status,
            reason=(
                "Batch enrichment only handles DRAFT apps from automated "
                "catalog sources. "
                "Pass --allow-non-mcp to override for prompt iteration."
            ),
        )


# ---------------------------------------------------------------------------
# Builders: Django ORM → pure-Python view
# ---------------------------------------------------------------------------
def build_taxonomy_snapshot() -> TaxonomySnapshot:
    return TaxonomySnapshot(
        platform_slugs=tuple(
            Platform.objects.order_by("slug").values_list("slug", flat=True)
        ),
        category_slugs=tuple(
            Category.objects.order_by("slug").values_list("slug", flat=True)
        ),
        capability_keys=tuple(
            Capability.objects.order_by("key").values_list("key", flat=True)
        ),
        listing_type_slugs=tuple(
            ListingType.objects.order_by("slug").values_list("slug", flat=True)
        ),
        capability_descriptions=dict(
            Capability.objects.values_list("key", "label")
        ),
        category_descriptions=dict(
            Category.objects.values_list("slug", "name")
        ),
    )


def build_app_snapshot(app_id: int) -> AppSnapshot:
    app = App.objects.select_related(None).prefetch_related(
        "platforms", "listing_types", "categories", "use_cases",
    ).get(pk=app_id)

    current_caps = dict(
        AppCapability.objects.filter(app=app)
        .values_list("capability__key", "value")
    )
    # Pad with 'unknown' for capabilities the App has no row for, so the
    # merge layer has a uniform view of the universe.
    for key in Capability.objects.values_list("key", flat=True):
        current_caps.setdefault(key, "unknown")

    return AppSnapshot(
        app_id=app.pk,
        slug=app.slug,
        name=app.name,
        short_description=app.short_description or "",
        long_description=app.long_description or "",
        developer_name=app.developer_name or "",
        developer_url=app.developer_url or "",
        official_page_url=app.official_page_url or "",
        install_url=app.install_url or "",
        repo_url=app.repo_url or "",
        status=app.status,
        editorial_review_status=app.editorial_review_status,
        platform_verification_status=app.platform_verification_status,
        developer_claim_status=app.developer_claim_status,
        launch_status=app.launch_status,
        pricing_model=app.pricing_model,
        verdict=app.verdict or "",
        platform_slugs=tuple(app.platforms.values_list("slug", flat=True)),
        listing_type_slugs=tuple(app.listing_types.values_list("slug", flat=True)),
        category_slugs=tuple(app.categories.values_list("slug", flat=True)),
        use_case_slugs=tuple(app.use_cases.values_list("slug", flat=True)),
        capabilities=current_caps,
    )


# ---------------------------------------------------------------------------
# Apply: Plan → DB writes (real run) / no-op (dry-run)
# ---------------------------------------------------------------------------
@dataclass
class PersistResult:
    """Summary of what was actually written. Useful for dry-run vs real diff."""

    fields_written: list[str]
    capabilities_written: list[str]
    categories_added: list[str]
    listing_types_added: list[str]
    use_cases_added: list[str]
    queue_entry_id: int | None
    source_id: int | None

    def as_dict(self) -> dict:
        return {
            "fields_written": list(self.fields_written),
            "capabilities_written": list(self.capabilities_written),
            "categories_added": list(self.categories_added),
            "listing_types_added": list(self.listing_types_added),
            "use_cases_added": list(self.use_cases_added),
            "queue_entry_id": self.queue_entry_id,
            "source_id": self.source_id,
        }


@dataclass
class NewDraftPersistResult:
    """Summary for `persist_new_draft`."""

    outcome: str
    app_id: int | None
    source_id: int | None

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "app_id": self.app_id,
            "source_id": self.source_id,
        }


def persist_new_draft(
    enriched: EnrichedDraft,
    *,
    source_type: str,
    external_id: str,
    raw_payload: dict,
    result: NewAppEnrichmentResult | None = None,
) -> NewDraftPersistResult:
    """Persist a new LLM-generated draft through the existing upsert layer.

    The LLM never writes published/editorial state. `upsert_app_from_draft`
    creates DRAFT/UNREVIEWED cards; proposed verdict and the full
    enrichment audit stay inside `Source.payload`.
    """
    draft = _enriched_to_app_draft(
        enriched,
        external_id=external_id,
        raw_payload=raw_payload,
    )
    outcome = upsert_app_from_draft(draft, source_type)
    source = Source.objects.filter(
        source_type=source_type,
        external_id=external_id,
    ).select_related("app").first()
    if source and result is not None:
        # Editors review the DRAFT before publishing; the LLM's
        # proposed_verdict is the most editor-facing field of the
        # enrichment. Fall back to scope_summary when verdict is empty
        # so the admin always shows something concrete.
        proposed_verdict = (enriched.proposed_verdict or "").strip() or (
            enriched.scope_summary or ""
        ).strip()
        payload = {
            **(source.payload or {}),
            "agent_enrichment": result.as_dict(),
            "proposed_verdict": proposed_verdict,
            "scope_summary": enriched.scope_summary,
        }
        Source.objects.filter(pk=source.pk).update(payload=payload)
    return NewDraftPersistResult(
        outcome=outcome,
        app_id=source.app_id if source else None,
        source_id=source.pk if source else None,
    )


def _enriched_to_app_draft(
    enriched: EnrichedDraft,
    *,
    external_id: str,
    raw_payload: dict,
) -> AppDraft:
    platform_slugs = _derive_platforms(
        lt.slug for lt in enriched.listing_types
    )
    return AppDraft(
        name=enriched.name,
        slug_hint=enriched.name,
        short_description=enriched.short_description[:280],
        long_description=enriched.long_description,
        developer_name=enriched.developer_name,
        developer_url=enriched.developer_url,
        official_page_url=enriched.official_page_url,
        install_url=enriched.install_url,
        repo_url=enriched.repo_url,
        platforms=platform_slugs,
        listing_types=[lt.slug for lt in enriched.listing_types],
        categories=[cat.slug for cat in enriched.categories],
        capabilities={
            key: proposal.value
            for key, proposal in enriched.capabilities.items()
        },
        capability_evidence={
            key: proposal.evidence
            for key, proposal in enriched.capabilities.items()
            if proposal.evidence
        },
        use_cases=list(enriched.use_cases),
        pricing_model=enriched.pricing_model,
        launch_status=enriched.launch_status,
        external_id=external_id,
        raw_payload={
            **raw_payload,
            "proposed_verdict": enriched.proposed_verdict,
            "scope_summary": enriched.scope_summary,
        },
        scope_summary=enriched.scope_summary,
    )


# Columns this module is FORBIDDEN to touch — listed to keep the
# invariant grep-able and to fail loud if a future change tries to.
_FORBIDDEN_FIELDS = frozenset({
    "status",
    "editorial_review_status",
    "platform_verification_status",
    "developer_claim_status",
    "verdict",
})


def apply_merge_set(
    app_id: int,
    result: EnrichmentResult,
    *,
    source_type: str = Source.SourceType.AGENT_ENRICH,
    enrichment_task: EnrichmentTask | None = None,
) -> PersistResult:
    """Apply ``result.outcome.plan`` and queue ``result.outcome.queue``.

    Race-safe by construction:

    * Locks the ``App`` row with ``SELECT ... FOR UPDATE`` at the top of
      the transaction. Concurrent editor edits that committed *before*
      this transaction are visible inside the lock; concurrent edits
      from after this transaction starts are blocked until commit.
    * Field updates are conditional: only applied if the CURRENT (locked)
      value is still empty. If the editor filled the field between
      ``build_app_snapshot()`` and now, the merge plan is dropped on
      the floor for that field. The original LLM proposal is still
      available in ``Source.payload`` (audit trail).
    * Capability updates are conditional: only applied when the current
      ``AppCapability.value`` is still ``unknown``. Existing yes/no
      values are never overwritten under any circumstance.
    * Status guard re-checked under the lock — defends against a race
      where ``App.status`` was published between ``assert_app_is_eligible``
      and now.

    On any exception, the whole transaction rolls back (atomic).
    """
    outcome = result.outcome
    plan = outcome.plan
    queue = outcome.queue

    _assert_plan_does_not_touch_forbidden(plan)

    with transaction.atomic():
        locked_app = App.objects.select_for_update().get(pk=app_id)

        if locked_app.status != App.AppStatus.DRAFT:
            raise AppNotEligibleError(
                app_id,
                locked_app.status,
                "status changed between snapshot and apply",
            )

        fields_written = _apply_field_updates(locked_app, plan)
        capabilities_written = _apply_capability_updates(app_id, plan)
        categories_added = _apply_categories(app_id, plan)
        listing_types_added = _apply_listing_types(app_id, plan)
        use_cases_added = _apply_use_cases(app_id, plan)
        source_id = _upsert_agent_source(
            app_id, result, source_type=source_type
        )
        queue_entry_id = _maybe_queue_review(
            app_id, queue, enrichment_task=enrichment_task
        )

    return PersistResult(
        fields_written=fields_written,
        capabilities_written=capabilities_written,
        categories_added=categories_added,
        listing_types_added=listing_types_added,
        use_cases_added=use_cases_added,
        queue_entry_id=queue_entry_id,
        source_id=source_id,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _assert_plan_does_not_touch_forbidden(plan: Plan) -> None:
    forbidden = [u for u in plan.field_updates if u.field in _FORBIDDEN_FIELDS]
    if forbidden:
        raise ValueError(
            f"Refusing to write forbidden fields from agent: "
            f"{[u.field for u in forbidden]}. "
            "This is a Phase 1 invariant; the merge layer must never propose "
            "writes to App.status / editorial_review_status / verdict / "
            "platform_verification_status / developer_claim_status."
        )


def _apply_field_updates(locked_app: App, plan: Plan) -> list[str]:
    """Apply text-field updates, race-checked against the locked row.

    Caller must hold the ``SELECT ... FOR UPDATE`` lock on ``locked_app``
    (i.e. be inside ``apply_merge_set``'s atomic block). Any field whose
    current value is non-empty is skipped on the floor — the LLM proposal
    is preserved in ``Source.payload`` as audit trail.

    Because ``.update()`` bypasses ``post_save``, the search-vector
    refresh signal in ``apps.catalog.signals`` won't fire on its own.
    We schedule the refresh manually via ``transaction.on_commit`` so
    a rolled-back transaction doesn't try to refresh anything.
    """
    if not plan.field_updates:
        return []

    applicable: dict[str, str] = {}
    for upd in plan.field_updates:
        current = (getattr(locked_app, upd.field) or "").strip()
        if current:
            # Race: editor filled this between snapshot and apply, or the
            # snapshot was stale to begin with. Skip — never overwrite.
            logger.info(
                "agent_skip_field_race",
                extra={"app_id": locked_app.pk, "field": upd.field},
            )
            continue
        applicable[upd.field] = upd.new_value

    if not applicable:
        return []

    App.objects.filter(pk=locked_app.pk).update(**applicable)

    # post_save doesn't fire for .update(); schedule search-vector refresh
    # explicitly. Mirrors apps.catalog.signals._schedule_refresh.
    app_id = locked_app.pk

    def _schedule_search_refresh() -> None:
        from apps.search.tasks import refresh_search_vector_task

        refresh_search_vector_task.delay(app_id)

    transaction.on_commit(_schedule_search_refresh)

    return list(applicable.keys())


def _apply_capability_updates(app_id: int, plan: Plan) -> list[str]:
    """Apply capability updates, race-checked against locked AppCapability rows.

    For every ``CapabilityUpdate`` in the plan we ``SELECT ... FOR UPDATE``
    the matching ``AppCapability`` row (if it exists) and apply only when
    the current value is ``unknown``. Existing ``yes`` / ``no`` is *never*
    overwritten — the disagreement was already routed to
    ``NeedsReviewQueueEntry`` by ``compute_merge``; the persist layer's
    job here is only to make the read-modify-write cycle race-safe.
    """
    if not plan.capability_updates:
        return []

    keys = [u.key for u in plan.capability_updates]
    cap_objects = {c.key: c for c in Capability.objects.filter(key__in=keys)}

    # Lock existing AppCapability rows under the same transaction.
    existing_rows = {
        ac.capability_id: ac
        for ac in AppCapability.objects.select_for_update().filter(
            app_id=app_id, capability__key__in=keys
        )
    }

    applied: list[str] = []
    for upd in plan.capability_updates:
        cap_obj = cap_objects.get(upd.key)
        if cap_obj is None:
            # Validator should have stripped this, but guard anyway.
            logger.warning(
                "agent_skip_unknown_capability",
                extra={"app_id": app_id, "capability_key": upd.key},
            )
            continue

        existing_row = existing_rows.get(cap_obj.pk)
        if existing_row is not None:
            if existing_row.value != AppCapability.CapabilityValue.UNKNOWN:
                # Race: someone (editor or another transaction that
                # committed first) flipped the slot to a known value
                # since the snapshot was built. Never overwrite known
                # values; the disagreement is captured in the queue.
                logger.info(
                    "agent_skip_capability_race",
                    extra={
                        "app_id": app_id,
                        "capability_key": upd.key,
                        "existing_value": existing_row.value,
                    },
                )
                continue
            existing_row.value = upd.value
            existing_row.note = (upd.evidence or "")[:500]
            existing_row.save(update_fields=["value", "note"])
        else:
            AppCapability.objects.create(
                app_id=app_id,
                capability=cap_obj,
                value=upd.value,
                note=(upd.evidence or "")[:500],
            )
        applied.append(upd.key)
    return applied


def _apply_categories(app_id: int, plan: Plan) -> list[str]:
    """Add categories through the M2M manager so ``m2m_changed`` fires.

    Writing directly to the through-model via
    ``AppCategory.objects.get_or_create`` would bypass the
    ``m2m_changed`` signal that
    :mod:`apps.catalog.signals` uses to keep ``App.search_vector`` warm —
    the new category text would not be searchable until the nightly
    safety-net rebuild. Routing through ``app.categories.add()`` keeps
    the signal pipeline intact.

    ``through_defaults`` is not needed: ``AppCategory.is_primary``
    defaults to ``False``, and Django uses that default when ``add()``
    creates a through-row.
    """
    if not plan.add_categories:
        return []
    cats = list(Category.objects.filter(slug__in=plan.add_categories))
    if not cats:
        return []
    app = App.objects.get(pk=app_id)
    existing = set(app.categories.values_list("slug", flat=True))
    to_add = [c for c in cats if c.slug not in existing]
    if to_add:
        app.categories.add(*to_add)
    return [c.slug for c in to_add]


def _apply_listing_types(app_id: int, plan: Plan) -> list[str]:
    if not plan.add_listing_types:
        return []
    types = ListingType.objects.filter(slug__in=plan.add_listing_types)
    if not types:
        return []
    app = App.objects.get(pk=app_id)
    existing = set(app.listing_types.values_list("slug", flat=True))
    to_add = [t for t in types if t.slug not in existing]
    if to_add:
        app.listing_types.add(*to_add)
    return [t.slug for t in to_add]


def _apply_use_cases(app_id: int, plan: Plan) -> list[str]:
    """Add use-cases through the M2M manager (see ``_apply_categories``).

    Same rationale: routing through ``app.use_cases.add()`` fires
    ``m2m_changed`` on ``App.use_cases.through``, which schedules the
    search-vector refresh via :mod:`apps.catalog.signals`.
    """
    if not plan.add_use_cases:
        return []
    app = App.objects.get(pk=app_id)
    existing = set(app.use_cases.values_list("slug", flat=True))
    added_use_cases: list[UseCase] = []
    added_slugs: list[str] = []
    for title in plan.add_use_cases:
        slug = slugify(title)[:200] or "use-case"
        if slug in existing:
            continue
        use_case, _ = UseCase.objects.get_or_create(
            slug=slug, defaults={"title": title}
        )
        added_use_cases.append(use_case)
        added_slugs.append(slug)
    if added_use_cases:
        app.use_cases.add(*added_use_cases)
    return added_slugs


def _upsert_agent_source(
    app_id: int, result: EnrichmentResult, *, source_type: str
) -> int:
    """Record an agent-enrichment Source row carrying the audit trail.

    The payload is intentionally rich (raw merge + sanitized merge +
    validation report + plan + queue): the editor sees in one place
    what the LLM said, what we filtered, what we applied, what we queued.
    """
    payload = {
        "enriched_at": timezone.now().isoformat(),
        "llm": {
            "provider": result.call_meta.provider,
            "model": result.call_meta.model,
            "prompt_version": result.call_meta.prompt_version,
            "is_mock": result.call_meta.is_mock,
            "input_tokens": result.call_meta.input_tokens,
            "output_tokens": result.call_meta.output_tokens,
            "cached_tokens": result.call_meta.cached_tokens,
            "cost_usd": result.call_meta.cost_usd,
        },
        **result.as_dict(),
    }
    source, _ = Source.objects.update_or_create(
        app_id=app_id,
        source_type=source_type,
        external_id=f"agent-enrich:{app_id}",
        defaults={
            "payload": payload,
            "fetched_at": timezone.now(),
            "is_active": True,
        },
    )
    return source.pk


def _maybe_queue_review(
    app_id: int,
    queue: QueueProposal,
    *,
    enrichment_task: EnrichmentTask | None,
) -> int | None:
    if queue.is_empty():
        return None
    entry = NeedsReviewQueueEntry.objects.create(
        app_id=app_id,
        task=enrichment_task,
        kind=NeedsReviewQueueEntry.Kind.ENRICHED,
        payload=queue.as_dict(),
    )
    return entry.pk


# ---------------------------------------------------------------------------
# Phase 4 — re-actualization persist
# ---------------------------------------------------------------------------
@dataclass
class ReactualizationPersistResult:
    """What the Phase 4 bridge wrote — drives audit + orchestrator stats."""

    queue_entry_id: int | None
    source_id: int | None
    is_empty: bool

    def as_dict(self) -> dict:
        return {
            "queue_entry_id": self.queue_entry_id,
            "source_id": self.source_id,
            "is_empty": self.is_empty,
        }


def queue_reactualization(
    diff: ReactualizationDiff,
    *,
    source_id: int | None,
    enrichment_task: EnrichmentTask | None,
    raw_fetch_payload: dict | None = None,
) -> ReactualizationPersistResult:
    """Persist one re-actualization outcome.

    Writes:
      * One ``NeedsReviewQueueEntry(kind=REACTUALIZED, payload=diff)`` —
        only when ``diff`` reports actual changes.
      * ``Source.last_enriched_at = now()`` for the source row that
        owns this app, even when the diff is empty (re-actualization
        cadence is driven by this timestamp, so we always advance it).
      * ``Source.payload`` merged with an ``agent_reactualization``
        block carrying the diff snapshot and fetch metadata for audit.

    The agent never touches App fields directly here — that contract
    is the whole reason Phase 4 exists as a separate pipeline.
    """
    queue_entry_id: int | None = None
    if not diff.is_empty():
        entry = NeedsReviewQueueEntry.objects.create(
            app_id=diff.app_id,
            task=enrichment_task,
            kind=NeedsReviewQueueEntry.Kind.REACTUALIZED,
            payload=diff.as_dict(),
        )
        queue_entry_id = entry.pk

    if source_id is not None:
        source = Source.objects.filter(pk=source_id).first()
        if source is not None:
            payload = {
                **(source.payload or {}),
                "agent_reactualization": {
                    "diff": diff.as_dict(),
                    "fetch": raw_fetch_payload or {},
                    "queue_entry_id": queue_entry_id,
                },
            }
            Source.objects.filter(pk=source_id).update(
                last_enriched_at=timezone.now(),
                payload=payload,
            )

    return ReactualizationPersistResult(
        queue_entry_id=queue_entry_id,
        source_id=source_id,
        is_empty=diff.is_empty(),
    )


# ---------------------------------------------------------------------------
# Run / Task / Call-log helpers
# ---------------------------------------------------------------------------
def record_llm_call(task: EnrichmentTask, meta) -> LLMCallLog:
    return LLMCallLog.objects.create(
        task=task,
        provider=meta.provider,
        model=meta.model,
        prompt_version=meta.prompt_version,
        input_tokens=meta.input_tokens,
        output_tokens=meta.output_tokens,
        cached_tokens=meta.cached_tokens,
        cost_usd=meta.cost_usd,
        latency_ms=meta.latency_ms,
        is_mock=meta.is_mock,
    )


def pending_enrichment_app_ids(
    limit: int | None = None,
    *,
    source_types: Iterable[str] | None = None,
) -> Iterable[int]:
    """Phase 1 batch selector.

    Returns app PKs eligible for ``enrich_existing_draft``:

    * ``status == DRAFT`` — agent never touches published / hidden cards.
    * Has a ``Source`` row with ``source_type`` in
      ``_PHASE_1_ELIGIBLE_SOURCE_TYPES``. DRAFT cards from manual entry
      or submissions are out of scope.
    * Has not yet been agent-enriched (no ``Source.external_id``
      starting with ``agent-enrich:``).

    Used by ``enrich_pending_drafts_batch`` and by the management
    command's ``--enrich-pending`` flag.
    """
    eligible_source_types = tuple(source_types or _PHASE_1_ELIGIBLE_SOURCE_TYPES)
    qs = (
        App.objects.filter(
            status=App.AppStatus.DRAFT,
            sources__source_type__in=eligible_source_types,
        )
        .exclude(
            sources__external_id__startswith="agent-enrich:",
        )
        .distinct()
        .order_by("-first_seen_at")
        .values_list("pk", flat=True)
    )
    if limit is not None:
        qs = qs[:limit]
    return list(qs)


# ---------------------------------------------------------------------------
# Phase 4 — selectors for re-actualization
# ---------------------------------------------------------------------------
# Source types we know how to re-fetch. New discovery sources must
# opt in here once they have a deterministic fetch path. MANUAL is
# excluded — editor-curated cards aren't an agent's job to re-actualize.
_REACTUALIZABLE_SOURCE_TYPES: tuple[str, ...] = (
    Source.SourceType.MCP_REGISTRY,
    Source.SourceType.AGENT_ENRICH,
    Source.SourceType.RSS_DISCOVERY,
    Source.SourceType.GITHUB_MCP,
)


def pending_reactualization_app_ids(
    *, interval_days: int, limit: int | None = None
) -> list[int]:
    """Published apps whose freshest re-actualizable Source is overdue.

    "Overdue" = the source has either never been LLM-enriched
    (``last_enriched_at IS NULL``) or its last enrichment is older than
    ``interval_days`` ago. Apps with no re-actualizable active source
    are excluded — we only check in on cards we know how to re-fetch.

    Ordered NULLS FIRST so the never-enriched cards drain ahead of the
    long-tail of refresh cycles. Stable PKs let callers run the same
    selector twice without missing the freshly-enriched ones.
    """
    cutoff = timezone.now() - timedelta(days=interval_days)
    qs = (
        App.published.filter(
            sources__is_active=True,
            sources__source_type__in=_REACTUALIZABLE_SOURCE_TYPES,
        )
        .filter(
            Q(sources__last_enriched_at__isnull=True)
            | Q(sources__last_enriched_at__lt=cutoff),
        )
        .distinct()
        .order_by(F("sources__last_enriched_at").asc(nulls_first=True))
        .values_list("pk", flat=True)
    )
    if limit is not None:
        qs = qs[:limit]
    return list(qs)


def pick_primary_active_source(
    app_id: int,
) -> Source | None:
    """Return the source row a re-actualization run should re-fetch from.

    Strategy: most recently fetched active row in the re-actualizable
    allowlist. ``is_primary`` is honored as a soft tie-breaker — the
    editor can flip the flag to direct re-actualization at a specific
    Source — but we don't require it because most discovery rows leave
    it false.
    """
    return (
        Source.objects.filter(
            app_id=app_id,
            is_active=True,
            source_type__in=_REACTUALIZABLE_SOURCE_TYPES,
        )
        .order_by("-is_primary", "-fetched_at")
        .first()
    )
