"""Validators / guardrails applied to ``MergeSet`` before persistence.

These rules are the second layer of defense against LLM hallucinations
(the first is the prompt). Even if the LLM ignores the instructions
above, the validator strips out anything that would compromise the
catalog:

1. **Capabilities without evidence → unknown.** Trust signal is the
   evidence quote; the value field on its own is unverifiable.
2. **Slugs not in the taxonomy → dropped.** No invented categories or
   capabilities — these would 500 the merge layer or pollute the
   catalog's faceted search.
3. **Low-confidence categories / listing types → dropped.** The merge
   layer also drops these, but doing it here saves one ``or`` in two
   places. ``DEFAULT_CONFIDENCE_FLOOR`` is the configurable cutoff.
4. **URLs that fail a syntactic check → dropped.** Phase 1 doesn't
   actually probe URLs (the link-checker bug fixes are deferred to
   Phase 4 prerequisites); syntactic ``urlparse`` check is sufficient
   to catch obviously broken outputs.

The validator returns a *new* MergeSet — it never mutates the input
(Pydantic models are easy to copy via ``model_copy``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from apps.agent.llm.schemas import (
    CapabilityProposal,
    CategoryProposal,
    EnrichedDraft,
    ListingTypeProposal,
    MergeSet,
)
from apps.agent.pipeline.taxonomy import TaxonomySnapshot

DEFAULT_CONFIDENCE_FLOOR = 0.7


@dataclass
class ValidationReport:
    """What the validator stripped — surfaced in EnrichmentTask.diff_summary."""

    dropped_capabilities_no_evidence: list[str] = field(default_factory=list)
    dropped_capabilities_unknown_key: list[str] = field(default_factory=list)
    dropped_categories_unknown_slug: list[str] = field(default_factory=list)
    dropped_categories_low_confidence: list[str] = field(default_factory=list)
    dropped_listing_types_unknown_slug: list[str] = field(default_factory=list)
    dropped_listing_types_low_confidence: list[str] = field(default_factory=list)
    dropped_urls_invalid: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dropped_capabilities_no_evidence": list(self.dropped_capabilities_no_evidence),
            "dropped_capabilities_unknown_key": list(self.dropped_capabilities_unknown_key),
            "dropped_categories_unknown_slug": list(self.dropped_categories_unknown_slug),
            "dropped_categories_low_confidence": list(self.dropped_categories_low_confidence),
            "dropped_listing_types_unknown_slug": list(self.dropped_listing_types_unknown_slug),
            "dropped_listing_types_low_confidence": list(self.dropped_listing_types_low_confidence),
            "dropped_urls_invalid": [list(pair) for pair in self.dropped_urls_invalid],
        }


def _is_valid_http_url(url: str) -> bool:
    """Syntactic URL check — ``http(s)://host/...``.

    A real liveness probe is the link-checker's job (Phase 4
    prerequisites). Here we only want to drop literal junk like
    ``"see the docs"`` or ``"https://"`` from LLM output.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def validate_merge_set(
    merge: MergeSet,
    taxonomy: TaxonomySnapshot,
    *,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
) -> tuple[MergeSet, ValidationReport]:
    """Return ``(sanitized_merge, report)``. Pure; never raises."""
    report = ValidationReport()

    # ----- Capabilities -----
    cleaned_capabilities: dict[str, CapabilityProposal] = {}
    for key, proposal in merge.capabilities.items():
        if not taxonomy.has_capability(key):
            report.dropped_capabilities_unknown_key.append(key)
            continue
        if proposal.value in ("yes", "no") and not proposal.evidence.strip():
            # Strip the value but keep an explicit "unknown" so the
            # merge layer sees the LLM looked at this capability.
            cleaned_capabilities[key] = CapabilityProposal(
                value="unknown",
                evidence="",
                confidence=proposal.confidence,
            )
            report.dropped_capabilities_no_evidence.append(key)
        else:
            cleaned_capabilities[key] = proposal

    # ----- Categories -----
    cleaned_categories: list[CategoryProposal] = []
    for cat in merge.add_categories:
        if not taxonomy.has_category(cat.slug):
            report.dropped_categories_unknown_slug.append(cat.slug)
            continue
        if cat.confidence < confidence_floor:
            report.dropped_categories_low_confidence.append(cat.slug)
            continue
        cleaned_categories.append(cat)

    # ----- Listing types -----
    cleaned_listing_types: list[ListingTypeProposal] = []
    for lt in merge.add_listing_types:
        if not taxonomy.has_listing_type(lt.slug):
            report.dropped_listing_types_unknown_slug.append(lt.slug)
            continue
        if lt.confidence < confidence_floor:
            report.dropped_listing_types_low_confidence.append(lt.slug)
            continue
        cleaned_listing_types.append(lt)

    # ----- URLs (syntactic) -----
    url_fields = (
        "developer_url",
        "official_page_url",
        "install_url",
        "repo_url",
    )
    url_updates: dict[str, None] = {}
    for fld in url_fields:
        value = getattr(merge, fld)
        if value is None:
            continue
        if not _is_valid_http_url(value):
            url_updates[fld] = None
            report.dropped_urls_invalid.append((fld, value))

    sanitized = merge.model_copy(
        update={
            "capabilities": cleaned_capabilities,
            "add_categories": cleaned_categories,
            "add_listing_types": cleaned_listing_types,
            **url_updates,
        }
    )
    return sanitized, report


def validate_enriched_draft(
    draft: EnrichedDraft,
    taxonomy: TaxonomySnapshot,
    *,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
) -> tuple[EnrichedDraft, ValidationReport]:
    """Sanitize an `EnrichedDraft` before converting it to `AppDraft`."""
    report = ValidationReport()

    cleaned_capabilities: dict[str, CapabilityProposal] = {}
    for key, proposal in draft.capabilities.items():
        if not taxonomy.has_capability(key):
            report.dropped_capabilities_unknown_key.append(key)
            continue
        if proposal.value in ("yes", "no") and not proposal.evidence.strip():
            cleaned_capabilities[key] = CapabilityProposal(
                value="unknown",
                evidence="",
                confidence=proposal.confidence,
            )
            report.dropped_capabilities_no_evidence.append(key)
        else:
            cleaned_capabilities[key] = proposal

    cleaned_categories: list[CategoryProposal] = []
    for cat in draft.categories:
        if not taxonomy.has_category(cat.slug):
            report.dropped_categories_unknown_slug.append(cat.slug)
            continue
        if cat.confidence < confidence_floor:
            report.dropped_categories_low_confidence.append(cat.slug)
            continue
        cleaned_categories.append(cat)

    cleaned_listing_types: list[ListingTypeProposal] = []
    for lt in draft.listing_types:
        if not taxonomy.has_listing_type(lt.slug):
            report.dropped_listing_types_unknown_slug.append(lt.slug)
            continue
        if lt.confidence < confidence_floor:
            report.dropped_listing_types_low_confidence.append(lt.slug)
            continue
        cleaned_listing_types.append(lt)

    url_updates: dict[str, str] = {}
    for fld in ("developer_url", "official_page_url", "install_url", "repo_url"):
        value = getattr(draft, fld)
        if value and not _is_valid_http_url(value):
            url_updates[fld] = ""
            report.dropped_urls_invalid.append((fld, value))

    sanitized = draft.model_copy(
        update={
            "capabilities": cleaned_capabilities,
            "categories": cleaned_categories,
            "listing_types": cleaned_listing_types,
            **url_updates,
        }
    )
    return sanitized, report
