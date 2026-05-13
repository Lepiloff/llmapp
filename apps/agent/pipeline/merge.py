"""Merge policy for existing-DRAFT enrichment.

This module implements the **never-overwrite-editorial-intent** contract
described in ``docs/agent-pipeline.md`` Phase 1. It is a pure function:
given the current ``AppSnapshot`` and an LLM ``MergeSet``, it returns

* a ``Plan`` describing exactly which DB writes will happen, and
* a ``QueueProposal`` describing what the editor needs to review.

The Django bridge (``apps.agent.persist.apply_merge_set``) executes
the plan inside a transaction. Keeping the decision pure (no DB,
no Django) lets us drive thousands of merge tests from fixtures.

The policy is intentionally conservative: when in doubt, the change
goes to the review queue. Phase 4 re-actualization will tighten the
queue threshold further.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from apps.agent.llm.schemas import AppSnapshot, CapabilityProposal, MergeSet


# Keys that are filled only if the App currently has an empty string in
# the matching attribute. Listed explicitly (not derived) so a future
# pydantic field on MergeSet doesn't silently start overwriting an
# editorial decision.
_SAFE_TEXT_FIELDS: tuple[str, ...] = (
    "short_description",
    "long_description",
    "developer_name",
    "developer_url",
    "official_page_url",
    "install_url",
    "repo_url",
)


@dataclass
class FieldUpdate:
    field: str
    new_value: str

    def as_dict(self) -> dict:
        return {"field": self.field, "new_value": self.new_value}


@dataclass
class CapabilityUpdate:
    key: str
    value: str
    evidence: str
    confidence: float

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "evidence": self.evidence,
            "confidence": self.confidence,
        }


@dataclass
class Plan:
    """The set of DB writes the merge layer wants ``persist.apply_merge_set``
    to perform. Anything not in this plan is NOT applied to the App."""

    field_updates: list[FieldUpdate] = field(default_factory=list)
    capability_updates: list[CapabilityUpdate] = field(default_factory=list)
    add_categories: list[str] = field(default_factory=list)
    add_listing_types: list[str] = field(default_factory=list)
    add_use_cases: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.field_updates
            or self.capability_updates
            or self.add_categories
            or self.add_listing_types
            or self.add_use_cases
        )

    def as_dict(self) -> dict:
        return {
            "field_updates": [u.as_dict() for u in self.field_updates],
            "capability_updates": [u.as_dict() for u in self.capability_updates],
            "add_categories": list(self.add_categories),
            "add_listing_types": list(self.add_listing_types),
            "add_use_cases": list(self.add_use_cases),
        }


@dataclass
class QueueProposal:
    """What the editor needs to see (Phase 2 admin renders this).

    Always set when there is a meaningful proposal; ``is_empty()`` lets
    the caller decide whether to write a ``NeedsReviewQueueEntry``.
    """

    proposed_verdict: str = ""
    proposed_launch_status: str | None = None
    proposed_pricing_model: str | None = None
    proposed_scope_summary: str = ""
    skipped_field_updates: list[FieldUpdate] = field(default_factory=list)
    skipped_capability_updates: list[CapabilityUpdate] = field(default_factory=list)
    rationale: str = ""

    def is_empty(self) -> bool:
        return not (
            self.proposed_verdict
            or self.proposed_launch_status
            or self.proposed_pricing_model
            or self.proposed_scope_summary
            or self.skipped_field_updates
            or self.skipped_capability_updates
        )

    def as_dict(self) -> dict:
        return {
            "proposed_verdict": self.proposed_verdict,
            "proposed_launch_status": self.proposed_launch_status,
            "proposed_pricing_model": self.proposed_pricing_model,
            "proposed_scope_summary": self.proposed_scope_summary,
            "skipped_field_updates": [u.as_dict() for u in self.skipped_field_updates],
            "skipped_capability_updates": [u.as_dict() for u in self.skipped_capability_updates],
            "rationale": self.rationale,
        }


@dataclass
class MergeOutcome:
    plan: Plan
    queue: QueueProposal

    def as_dict(self) -> dict:
        return {"plan": self.plan.as_dict(), "queue": self.queue.as_dict()}


def compute_merge(snapshot: AppSnapshot, merge: MergeSet) -> MergeOutcome:
    """Decide what to write vs what to queue. Pure; never raises."""
    plan = Plan()
    queue = QueueProposal(
        proposed_verdict=merge.proposed_verdict.strip(),
        proposed_launch_status=merge.proposed_launch_status,
        proposed_pricing_model=merge.proposed_pricing_model,
        proposed_scope_summary=merge.proposed_scope_summary.strip(),
        rationale=merge.rationale,
    )

    _apply_text_fields(snapshot, merge, plan, queue)
    _apply_capabilities(snapshot, merge, plan, queue)
    _apply_categories(snapshot, merge, plan)
    _apply_listing_types(snapshot, merge, plan)
    _apply_use_cases(snapshot, merge, plan)

    return MergeOutcome(plan=plan, queue=queue)


# ---------------------------------------------------------------------------
# Field-by-field merge helpers
# ---------------------------------------------------------------------------
def _apply_text_fields(
    snapshot: AppSnapshot, merge: MergeSet, plan: Plan, queue: QueueProposal
) -> None:
    for fld in _SAFE_TEXT_FIELDS:
        proposed = getattr(merge, fld)
        if proposed is None:
            continue
        proposed = proposed.strip()
        if not proposed:
            continue
        current = (getattr(snapshot, fld) or "").strip()
        update = FieldUpdate(field=fld, new_value=proposed)
        if not current:
            plan.field_updates.append(update)
        else:
            # Editor (or earlier ingest) already filled this — never
            # overwrite. Surface it for review only if the proposal is
            # genuinely different; identical proposals are noise.
            if current != proposed:
                queue.skipped_field_updates.append(update)


def _apply_capabilities(
    snapshot: AppSnapshot, merge: MergeSet, plan: Plan, queue: QueueProposal
) -> None:
    for key, prop in merge.capabilities.items():
        current = snapshot.capabilities.get(key, "unknown")
        update = CapabilityUpdate(
            key=key,
            value=prop.value,
            evidence=prop.evidence,
            confidence=prop.confidence,
        )
        if prop.value == "unknown":
            # Nothing to do — we never write "unknown" over a known value,
            # and writing "unknown" over "unknown" is a no-op.
            continue
        if current == "unknown":
            # Fill the slot — yes/no with evidence is the whole point.
            plan.capability_updates.append(update)
        elif current != prop.value:
            # LLM disagrees with the existing call. NEVER auto-overwrite.
            queue.skipped_capability_updates.append(update)
        # Same value: no-op.


def _apply_categories(snapshot: AppSnapshot, merge: MergeSet, plan: Plan) -> None:
    have = set(snapshot.category_slugs)
    for cat in merge.add_categories:
        if cat.slug in have:
            continue
        plan.add_categories.append(cat.slug)


def _apply_listing_types(snapshot: AppSnapshot, merge: MergeSet, plan: Plan) -> None:
    have = set(snapshot.listing_type_slugs)
    for lt in merge.add_listing_types:
        if lt.slug in have:
            continue
        plan.add_listing_types.append(lt.slug)


def _apply_use_cases(snapshot: AppSnapshot, merge: MergeSet, plan: Plan) -> None:
    have = set(snapshot.use_case_slugs)
    for uc in merge.add_use_cases:
        # Use-cases in MergeSet are free-form titles; the persist layer
        # slugifies them. Here we simply de-duplicate by lowercase title
        # against the snapshot's already-attached slugs (best effort).
        title = (uc or "").strip()
        if not title:
            continue
        synthetic_slug = title.lower().replace(" ", "-")[:200]
        if synthetic_slug in have:
            continue
        plan.add_use_cases.append(title)
