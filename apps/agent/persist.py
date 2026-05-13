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
from dataclasses import dataclass
from typing import Iterable

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.agent.llm.schemas import AppSnapshot
from apps.agent.models import AgentRun, EnrichmentTask, LLMCallLog, NeedsReviewQueueEntry
from apps.agent.pipeline.enrich import EnrichmentResult
from apps.agent.pipeline.merge import Plan, QueueProposal
from apps.agent.pipeline.taxonomy import TaxonomySnapshot
from apps.catalog.models import (
    App,
    AppCapability,
    AppCategory,
    AppUseCase,
    Capability,
    Category,
    ListingType,
    Platform,
    UseCase,
)
from apps.sources.models import Source

logger = logging.getLogger(__name__)


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
    source_type: str = Source.SourceType.MANUAL,
    enrichment_task: EnrichmentTask | None = None,
) -> PersistResult:
    """Apply ``result.outcome.plan`` and queue ``result.outcome.queue``.

    Wraps everything in a single transaction; on any exception, nothing
    is written. Returns a structured summary so the management command
    can print a human-readable diff.
    """
    outcome = result.outcome
    plan = outcome.plan
    queue = outcome.queue

    _assert_plan_does_not_touch_forbidden(plan)

    with transaction.atomic():
        fields_written = _apply_field_updates(app_id, plan)
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


def _apply_field_updates(app_id: int, plan: Plan) -> list[str]:
    if not plan.field_updates:
        return []
    updates = {u.field: u.new_value for u in plan.field_updates}
    App.objects.filter(pk=app_id).update(**updates)
    return list(updates.keys())


def _apply_capability_updates(app_id: int, plan: Plan) -> list[str]:
    if not plan.capability_updates:
        return []
    key_to_obj = {c.key: c for c in Capability.objects.filter(
        key__in=[u.key for u in plan.capability_updates]
    )}
    applied: list[str] = []
    for upd in plan.capability_updates:
        cap_obj = key_to_obj.get(upd.key)
        if cap_obj is None:
            # Validator should have stripped this, but guard anyway.
            logger.warning(
                "agent_skip_unknown_capability",
                extra={"app_id": app_id, "capability_key": upd.key},
            )
            continue
        AppCapability.objects.update_or_create(
            app_id=app_id,
            capability=cap_obj,
            defaults={
                "value": upd.value,
                "note": (upd.evidence or "")[:200],
            },
        )
        applied.append(upd.key)
    return applied


def _apply_categories(app_id: int, plan: Plan) -> list[str]:
    if not plan.add_categories:
        return []
    cats = list(Category.objects.filter(slug__in=plan.add_categories))
    added: list[str] = []
    for cat in cats:
        _, created = AppCategory.objects.get_or_create(
            app_id=app_id, category=cat
        )
        if created:
            added.append(cat.slug)
    return added


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
    if not plan.add_use_cases:
        return []
    added: list[str] = []
    for title in plan.add_use_cases:
        slug = slugify(title)[:200] or "use-case"
        use_case, _ = UseCase.objects.get_or_create(
            slug=slug, defaults={"title": title}
        )
        _, created = AppUseCase.objects.get_or_create(
            app_id=app_id, use_case=use_case
        )
        if created:
            added.append(slug)
    return added


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


def pending_enrichment_app_ids(limit: int | None = None) -> Iterable[int]:
    """Apps eligible for ``enrich_existing_draft``: DRAFT, with at least one
    Source that hasn't been agent-enriched yet (no Source.external_id matching
    ``agent-enrich:*``).

    Used by ``enrich_pending_drafts_batch`` (Phase 1) and by the
    management command's ``--enrich-pending`` flag.
    """
    qs = (
        App.objects.filter(status=App.AppStatus.DRAFT)
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
