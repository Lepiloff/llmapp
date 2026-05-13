"""Phase 1 orchestrator: enrich an existing DRAFT App.

Pure-Python entry point that ties together prompt → LLMProvider →
validate → merge. The caller (Django bridge or test) supplies:

* ``snapshot`` — current ``AppSnapshot``, built outside this module.
* ``taxonomy`` — allowed slugs ``TaxonomySnapshot``.
* ``llm`` — any ``LLMProvider`` (``MockLLMProvider`` in tests, real
  provider in production).

Returns an ``EnrichmentResult`` bundling the LLM raw output, the
validation report, the merge plan, and the call metadata. The Django
bridge consumes this to:

1. Write ``LLMCallLog`` from ``result.call_meta``.
2. Optionally apply ``result.outcome.plan`` to the App (``apply_merge_set``).
3. Write ``NeedsReviewQueueEntry`` from ``result.outcome.queue`` when
   non-empty.
4. Update ``EnrichmentTask`` with ``result.outcome.as_dict()`` for audit.

No DB access in this module.
"""
from __future__ import annotations

from dataclasses import dataclass

from apps.agent.llm.client import LLMCallMetadata, LLMProvider
from apps.agent.llm.prompts import enrich_existing_draft_prompt
from apps.agent.llm.schemas import AppSnapshot, MergeSet
from apps.agent.pipeline.merge import MergeOutcome, compute_merge
from apps.agent.pipeline.taxonomy import TaxonomySnapshot
from apps.agent.pipeline.validate import (
    DEFAULT_CONFIDENCE_FLOOR,
    ValidationReport,
    validate_merge_set,
)


@dataclass
class EnrichmentResult:
    """Single bundle returned by ``enrich_existing_draft``.

    Carrying both the *raw* MergeSet and the *sanitized* MergeSet costs
    a few bytes and lets the editor see what the LLM originally said
    next to what survived validation — useful for prompt iteration.
    """

    raw_merge: MergeSet
    sanitized_merge: MergeSet
    validation: ValidationReport
    outcome: MergeOutcome
    call_meta: LLMCallMetadata

    def as_dict(self) -> dict:
        return {
            "raw_merge": self.raw_merge.model_dump(),
            "sanitized_merge": self.sanitized_merge.model_dump(),
            "validation": self.validation.as_dict(),
            "outcome": self.outcome.as_dict(),
        }


def enrich_existing_draft(
    snapshot: AppSnapshot,
    taxonomy: TaxonomySnapshot,
    llm: LLMProvider,
    *,
    raw_source_text: str = "",
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
) -> EnrichmentResult:
    """Run prompt → LLM → validate → merge on one DRAFT card. Pure."""
    prompt = enrich_existing_draft_prompt(
        snapshot, taxonomy, raw_source_text=raw_source_text
    )
    response = llm.complete(
        system=prompt.system,
        messages=prompt.messages,
        schema=MergeSet,
        taxonomy=taxonomy,
        prompt_version=prompt.version,
    )
    raw_merge = response.data
    assert isinstance(raw_merge, MergeSet), (
        "LLMProvider returned wrong schema — contract violated."
    )

    sanitized, validation = validate_merge_set(
        raw_merge, taxonomy, confidence_floor=confidence_floor
    )
    outcome = compute_merge(snapshot, sanitized)
    return EnrichmentResult(
        raw_merge=raw_merge,
        sanitized_merge=sanitized,
        validation=validation,
        outcome=outcome,
        call_meta=response.meta,
    )
