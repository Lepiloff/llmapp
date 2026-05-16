"""Validation regression eval — replay saved LLM raw outputs.

For each fixture the LLM raw output is fed into ``validate_enriched_draft``
and the sanitized result is compared against the saved expected
sanitization. This catches regressions in:

* The "no evidence → unknown" capability rule.
* Slug membership checks against the taxonomy.
* Confidence threshold for category / listing-type acceptance.
* URL validation rules.
* ``EnrichedDraft`` Pydantic schema drift.

Pure (no LLM call, no DB). The taxonomy is rebuilt from
``apps/catalog/fixtures/seed.json`` so the eval is stable even when
the dev DB drifts.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.agent.llm.schemas import EnrichedDraft
from apps.agent.pipeline.taxonomy import TaxonomySnapshot
from apps.agent.pipeline.validate import validate_enriched_draft
from tests.agent.eval.loader import load_fixtures


_SEED_JSON = (
    Path(__file__).resolve().parents[3]
    / "apps" / "catalog" / "fixtures" / "seed.json"
)


def _taxonomy_from_seed() -> TaxonomySnapshot:
    """Build a TaxonomySnapshot directly from seed.json — no DB."""
    with _SEED_JSON.open() as f:
        rows = json.load(f)
    platforms = tuple(r["fields"]["slug"] for r in rows if r["model"] == "catalog.platform")
    categories = tuple(r["fields"]["slug"] for r in rows if r["model"] == "catalog.category")
    capabilities = tuple(r["fields"]["key"] for r in rows if r["model"] == "catalog.capability")
    listings = tuple(r["fields"]["slug"] for r in rows if r["model"] == "catalog.listingtype")
    return TaxonomySnapshot(
        platform_slugs=platforms,
        category_slugs=categories,
        capability_keys=capabilities,
        listing_type_slugs=listings,
    )


@pytest.mark.parametrize("fixture", load_fixtures(prefix="validate_"))
def test_validation_pack_fixture(fixture) -> None:
    """One test per fixture: re-validate the saved raw draft and
    compare the sanitized result to the saved expected output.

    Direct dict equality on the sanitized model_dump — any drift in
    output shape surfaces immediately. When a prompt change legitimately
    yields a different sanitization, re-snapshot the affected fixtures
    by re-running ``manage.py shell`` and dumping the new Source.payload.
    agent_enrichment.sanitized_draft.
    """
    name = fixture["name"]
    raw_draft = EnrichedDraft.model_validate(fixture["raw_draft"])
    expected = fixture["expected_sanitized_draft"]

    taxonomy = _taxonomy_from_seed()
    sanitized, report = validate_enriched_draft(raw_draft, taxonomy)

    actual = sanitized.model_dump()
    # Normalize JSON round-trip differences (e.g. mode='python' vs json).
    actual_normalized = json.loads(json.dumps(actual, default=str))
    expected_normalized = json.loads(json.dumps(expected, default=str))

    assert actual_normalized == expected_normalized, (
        f"validation regression on fixture {name!r}: sanitized output "
        f"diverged from saved baseline. If this is an intended prompt "
        f"or validation change, re-snapshot the fixture."
    )
