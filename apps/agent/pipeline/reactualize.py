"""Phase 4 re-actualization: diff a fresh enrichment against current state.

A re-actualization is a *check-in*, not an enrichment. Where Phase 1
``compute_merge`` is allowed to fill empty slots in a DRAFT, this module
is forbidden to write *anything* to App fields directly: published
cards are editor-owned, and the agent's job here is to surface what
the world thinks the app looks like *now* so an editor can decide.

Output of ``compute_reactualization`` is a :class:`ReactualizationDiff`
that the Django bridge converts into one ``NeedsReviewQueueEntry(kind=
reactualized, payload=<diff>)``. Only Source audit fields and
``LinkHealth`` are allowed to auto-update — every change that touches a
user-visible App field is queued.

The diff intentionally includes both *additions* (LLM proposes a new
category) and *disappearances* (LLM no longer mentions a category the
catalog currently shows). The catalog never auto-removes a taxonomy
assignment — an editor reads the diff and decides whether the category
truly no longer applies. Same for capabilities: a flip from ``yes`` to
``no`` after re-reading is *evidence*, not an instruction; the editor
sees both old + new + the new evidence quote.

Pure-Python; no Django imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from apps.agent.llm.schemas import AppSnapshot, EnrichedDraft

# Fields the diff reports on. Mirrors ``_SAFE_TEXT_FIELDS`` from
# ``compute_merge`` plus ``launch_status`` / ``pricing_model`` since
# those are editorial fields the LLM is allowed to propose on
# re-actualization (the editor can accept or reject).
_DIFFABLE_TEXT_FIELDS: tuple[str, ...] = (
    "short_description",
    "long_description",
    "developer_name",
    "developer_url",
    "official_page_url",
    "install_url",
    "repo_url",
)


@dataclass
class FieldDelta:
    """One App-level field whose proposed value differs from the snapshot."""

    field: str
    old_value: str
    new_value: str

    def as_dict(self) -> dict:
        return {
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
        }


@dataclass
class CapabilityDelta:
    """A capability flag whose proposed value differs from the catalog."""

    key: str
    old_value: str  # yes / no / unknown
    new_value: str
    new_evidence: str
    new_confidence: float

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "new_evidence": self.new_evidence,
            "new_confidence": self.new_confidence,
        }


@dataclass
class TaxonomyDelta:
    """Additive + removal proposals for one taxonomy axis.

    ``added`` and ``removed`` are both editor-facing only — the agent
    never applies either; the queue entry lets the editor toggle each.
    """

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.added and not self.removed

    def as_dict(self) -> dict:
        return {"added": list(self.added), "removed": list(self.removed)}


@dataclass
class ReactualizationDiff:
    """Editor-facing summary of how the world drifted since last enrichment.

    Includes both proposed additions and proposed removals across every
    surface that an editor might want to act on. Empty axes are still
    listed for stable shape — the admin renderer can decide what to hide.

    ``is_empty()`` returns True only when nothing changed; the persist
    layer uses that to decide whether to write a queue entry at all
    (we never spam the editor with no-op reactualizations).
    """

    app_id: int
    fields: list[FieldDelta] = field(default_factory=list)
    capabilities: list[CapabilityDelta] = field(default_factory=list)
    categories: TaxonomyDelta = field(default_factory=TaxonomyDelta)
    listing_types: TaxonomyDelta = field(default_factory=TaxonomyDelta)
    use_cases: TaxonomyDelta = field(default_factory=TaxonomyDelta)
    proposed_verdict: str = ""
    proposed_launch_status_change: tuple[str, str] | None = None
    proposed_pricing_model_change: tuple[str, str] | None = None
    proposed_scope_summary: str = ""

    def is_empty(self) -> bool:
        # use_cases is intentionally excluded from this gate. LLM
        # phrasing varies between runs, so titles like "Generate API
        # reference docs" vs "Generate API reference documentation"
        # slugify to different slugs and produce a stable +N -N churn
        # every cycle. The diff still carries the use_case delta into
        # the queue entry payload when *anything else* drifted, so
        # editors see it on review; we just refuse to fire a queue
        # entry on use-case noise alone. (2026-05-16 dry-run pilot.)
        return not (
            self.fields
            or self.capabilities
            or not self.categories.is_empty()
            or not self.listing_types.is_empty()
            or self.proposed_verdict
            or self.proposed_launch_status_change
            or self.proposed_pricing_model_change
            or self.proposed_scope_summary
        )

    def as_dict(self) -> dict:
        return {
            "app_id": self.app_id,
            "fields": [d.as_dict() for d in self.fields],
            "capabilities": [d.as_dict() for d in self.capabilities],
            "categories": self.categories.as_dict(),
            "listing_types": self.listing_types.as_dict(),
            "use_cases": self.use_cases.as_dict(),
            "proposed_verdict": self.proposed_verdict,
            "proposed_launch_status_change": (
                list(self.proposed_launch_status_change)
                if self.proposed_launch_status_change else None
            ),
            "proposed_pricing_model_change": (
                list(self.proposed_pricing_model_change)
                if self.proposed_pricing_model_change else None
            ),
            "proposed_scope_summary": self.proposed_scope_summary,
        }


def compute_reactualization(
    snapshot: AppSnapshot, enriched: EnrichedDraft
) -> ReactualizationDiff:
    """Build a :class:`ReactualizationDiff` by comparing fresh LLM output
    against the catalog's current view of the App.

    Trim rules:

    * Text fields: report only if the LLM proposes a non-empty value
      that differs from the catalog. An empty proposal is not "delete";
      the LLM has just nothing to say, not a directive.
    * Capabilities: report any (old, new) where new != old, including
      flips from ``unknown`` to ``yes/no``. The evidence quote is
      attached so the editor doesn't need to chase the source.
    * Taxonomy: ``added`` = LLM-proposed slugs the catalog doesn't have;
      ``removed`` = catalog slugs the LLM no longer mentions.
    * Editorial proposals (verdict / launch_status / pricing /
      scope_summary): emit only when the LLM produced a non-empty value
      and it differs from the catalog. ``launch_status`` and
      ``pricing_model`` are emitted as old→new tuples; the editor flips
      them via the queue entry, not the agent.
    """
    diff = ReactualizationDiff(app_id=snapshot.app_id)

    for field_name in _DIFFABLE_TEXT_FIELDS:
        new_value = (getattr(enriched, field_name) or "").strip()
        old_value = (getattr(snapshot, field_name) or "").strip()
        if new_value and new_value != old_value:
            diff.fields.append(
                FieldDelta(field=field_name, old_value=old_value, new_value=new_value)
            )

    for key, proposal in enriched.capabilities.items():
        old = snapshot.capabilities.get(key, "unknown")
        if proposal.value == old:
            continue
        diff.capabilities.append(CapabilityDelta(
            key=key,
            old_value=old,
            new_value=proposal.value,
            new_evidence=proposal.evidence,
            new_confidence=proposal.confidence,
        ))

    new_category_slugs = {c.slug for c in enriched.categories}
    old_category_slugs = set(snapshot.category_slugs)
    diff.categories = TaxonomyDelta(
        added=sorted(new_category_slugs - old_category_slugs),
        removed=sorted(old_category_slugs - new_category_slugs),
    )

    new_listing_slugs = {lt.slug for lt in enriched.listing_types}
    old_listing_slugs = set(snapshot.listing_type_slugs)
    diff.listing_types = TaxonomyDelta(
        added=sorted(new_listing_slugs - old_listing_slugs),
        removed=sorted(old_listing_slugs - new_listing_slugs),
    )

    new_use_case_slugs = _slugify_use_cases(enriched.use_cases)
    old_use_case_slugs = set(snapshot.use_case_slugs)
    diff.use_cases = TaxonomyDelta(
        added=sorted(new_use_case_slugs - old_use_case_slugs),
        removed=sorted(old_use_case_slugs - new_use_case_slugs),
    )

    proposed_verdict = (enriched.proposed_verdict or "").strip()
    if proposed_verdict and proposed_verdict != (snapshot.verdict or "").strip():
        diff.proposed_verdict = proposed_verdict

    if enriched.launch_status and enriched.launch_status != snapshot.launch_status:
        diff.proposed_launch_status_change = (
            snapshot.launch_status, enriched.launch_status
        )
    if enriched.pricing_model and enriched.pricing_model != snapshot.pricing_model:
        diff.proposed_pricing_model_change = (
            snapshot.pricing_model, enriched.pricing_model
        )

    proposed_scope = (enriched.scope_summary or "").strip()
    # Snapshot doesn't carry scope_summary, so any non-empty value is a
    # candidate; the editor decides relative to AppPlatform.scope_summary
    # on review.
    if proposed_scope:
        diff.proposed_scope_summary = proposed_scope

    return diff


def _slugify_use_cases(titles: list[str]) -> set[str]:
    """Mirror the slug derivation that ``upsert.attach_use_cases`` uses."""
    from django.utils.text import slugify  # local import to keep top pure
    return {slugify(t)[:200] for t in titles if t}
