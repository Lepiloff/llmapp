"""Tests for ``apps.agent.pipeline.reactualize.compute_reactualization``.

These tests pin the never-overwrite contract for Phase 4:

* No field is auto-applied — the diff is the only output.
* Capability flips are reported with evidence and confidence so the
  editor can judge without re-reading the source.
* Taxonomy axes report additions AND disappearances symmetrically so
  the editor sees both what to add and what may no longer apply.
* Empty proposals never produce a "delete" instruction.
"""
from __future__ import annotations

from apps.agent.llm.schemas import (
    AppSnapshot,
    CapabilityProposal,
    CategoryProposal,
    EnrichedDraft,
    ListingTypeProposal,
)
from apps.agent.pipeline.reactualize import compute_reactualization


def _snapshot(**overrides) -> AppSnapshot:
    base = {
        "app_id": 1,
        "slug": "demo",
        "name": "Demo",
        "short_description": "Old short",
        "long_description": "Old long",
        "developer_name": "Old Dev",
        "developer_url": "https://old.example/dev",
        "official_page_url": "https://old.example/page",
        "install_url": "",
        "repo_url": "",
        "status": "published",
        "editorial_review_status": "reviewed",
        "platform_verification_status": "not_listed",
        "developer_claim_status": "unclaimed",
        "launch_status": "live",
        "pricing_model": "free",
        "verdict": "Old verdict",
        "platform_slugs": ("mcp",),
        "listing_type_slugs": ("mcp-server",),
        "category_slugs": ("developer-tools",),
        "use_case_slugs": ("scaffold-a-service",),
        "capabilities": {
            "read_data": "yes",
            "write_actions": "no",
            "deletes_data": "unknown",
        },
    }
    base.update(overrides)
    return AppSnapshot(**base)


def _enriched(**overrides) -> EnrichedDraft:
    base = {
        "name": "Demo",
        "short_description": "Old short",
        "long_description": "Old long",
        "developer_name": "Old Dev",
        "developer_url": "https://old.example/dev",
        "official_page_url": "https://old.example/page",
        "install_url": "",
        "repo_url": "",
        "listing_types": [ListingTypeProposal(slug="mcp-server", confidence=0.95)],
        "categories": [CategoryProposal(slug="developer-tools", confidence=0.9)],
        "capabilities": {},
        "use_cases": ["Scaffold a service"],
        "launch_status": "live",
        "pricing_model": "free",
        "proposed_verdict": "",
        "scope_summary": "",
    }
    base.update(overrides)
    return EnrichedDraft(**base)


def test_unchanged_world_yields_empty_diff() -> None:
    """When the LLM proposes exactly what's already in the catalog, the
    diff is empty and the persist layer must not write a queue entry."""
    diff = compute_reactualization(_snapshot(), _enriched())
    assert diff.is_empty()


def test_text_field_drift_is_reported() -> None:
    """Non-empty text proposals that differ from the snapshot are
    surfaced. The catalog is never auto-overwritten — the editor reads
    the queue entry and approves."""
    diff = compute_reactualization(
        _snapshot(),
        _enriched(
            short_description="Brand-new tagline",
            long_description="Old long",  # unchanged
            install_url="https://example/install/v2",  # fills empty slot
        ),
    )
    fields_by_name = {fd.field: fd for fd in diff.fields}
    assert "short_description" in fields_by_name
    assert fields_by_name["short_description"].old_value == "Old short"
    assert fields_by_name["short_description"].new_value == "Brand-new tagline"
    assert "install_url" in fields_by_name
    assert fields_by_name["install_url"].old_value == ""
    assert fields_by_name["install_url"].new_value == "https://example/install/v2"
    # Unchanged fields don't appear.
    assert "long_description" not in fields_by_name


def test_empty_text_proposal_is_never_a_delete_instruction() -> None:
    """If the LLM has nothing to say for a field, that is NOT a directive
    to clear the catalog value. The catalog wins by default."""
    diff = compute_reactualization(
        _snapshot(),
        _enriched(short_description="", developer_name=""),
    )
    assert diff.fields == []


def test_capability_flip_includes_evidence_and_confidence() -> None:
    """Capability changes carry evidence so the editor can judge without
    re-reading the README. Confidence travels with the call too."""
    diff = compute_reactualization(
        _snapshot(),
        _enriched(capabilities={
            "read_data": CapabilityProposal(
                value="no",
                evidence="Server is now write-only; reads were removed in v3.",
                confidence=0.92,
            ),
            "deletes_data": CapabilityProposal(
                value="yes",
                evidence="DELETE /resource is documented under destructive ops.",
                confidence=0.85,
            ),
            "write_actions": CapabilityProposal(value="no", evidence="", confidence=1.0),
        }),
    )
    by_key = {cap.key: cap for cap in diff.capabilities}
    assert by_key["read_data"].old_value == "yes"
    assert by_key["read_data"].new_value == "no"
    assert by_key["read_data"].new_evidence.startswith("Server is now write-only")
    assert by_key["read_data"].new_confidence == 0.92
    assert by_key["deletes_data"].old_value == "unknown"
    assert by_key["deletes_data"].new_value == "yes"
    # Unchanged capability not in the diff.
    assert "write_actions" not in by_key


def test_taxonomy_reports_additions_and_disappearances_symmetrically() -> None:
    """Removals are *proposals*, not auto-applies — the catalog never
    drops a category on its own. The editor decides via the queue entry.
    """
    diff = compute_reactualization(
        _snapshot(category_slugs=("developer-tools", "productivity")),
        _enriched(categories=[
            # productivity disappears, design-tools appears, dev-tools stays
            CategoryProposal(slug="developer-tools", confidence=0.9),
            CategoryProposal(slug="design-tools", confidence=0.8),
        ]),
    )
    assert diff.categories.added == ["design-tools"]
    assert diff.categories.removed == ["productivity"]


def test_listing_type_disappearance_is_reported() -> None:
    diff = compute_reactualization(
        _snapshot(listing_type_slugs=("mcp-server", "claude-connector")),
        _enriched(listing_types=[
            ListingTypeProposal(slug="mcp-server", confidence=0.95),
        ]),
    )
    assert diff.listing_types.added == []
    assert diff.listing_types.removed == ["claude-connector"]


def test_use_case_diff_uses_django_slugify() -> None:
    """Use cases live in the snapshot as slugs but in the EnrichedDraft
    as titles. The diff must slugify titles the same way the upsert
    layer does, otherwise a phrasing tweak would falsely add a row."""
    diff = compute_reactualization(
        _snapshot(use_case_slugs=("turn-notes-into-slides",)),
        _enriched(use_cases=[
            "Turn notes into slides",      # same after slugify
            "Generate API reference docs",  # new
        ]),
    )
    assert diff.use_cases.added == ["generate-api-reference-docs"]
    assert diff.use_cases.removed == []


def test_launch_status_and_pricing_model_emitted_as_tuples() -> None:
    """Editorial fields drift to the queue entry as ``(old, new)`` so
    the renderer can show a clear before/after."""
    diff = compute_reactualization(
        _snapshot(launch_status="live", pricing_model="free"),
        _enriched(launch_status="beta", pricing_model="freemium"),
    )
    assert diff.proposed_launch_status_change == ("live", "beta")
    assert diff.proposed_pricing_model_change == ("free", "freemium")


def test_proposed_verdict_reported_only_when_different() -> None:
    same = compute_reactualization(
        _snapshot(verdict="Identical verdict"),
        _enriched(proposed_verdict="Identical verdict"),
    )
    assert same.proposed_verdict == ""

    drift = compute_reactualization(
        _snapshot(verdict="Old verdict"),
        _enriched(proposed_verdict="Fresh take after re-read"),
    )
    assert drift.proposed_verdict == "Fresh take after re-read"


def test_use_case_only_drift_does_not_fire_queue_entry() -> None:
    """is_empty() ignores use_cases on purpose. The LLM phrases use-case
    titles slightly differently every run (e.g. "Connect to Slack" vs
    "Connect Slack"), slugify yields different slugs, and a strict diff
    would queue a noisy "use cases changed" entry every cycle for every
    app. The diff still carries the use_cases delta — the editor sees it
    when the queue entry fires for *other* drift — we just refuse to
    fire on use_cases noise alone. (2026-05-16 dry-run pilot.)"""
    diff = compute_reactualization(
        _snapshot(),
        _enriched(use_cases=[
            "Scaffold a service",           # already in snapshot
            "Generate API reference docs",  # net-new
            "Spin up a server",             # net-new
        ]),
    )

    # The diff still records the use_case churn — the data is there
    # for the editor's review when a queue entry exists.
    assert diff.use_cases.added == sorted([
        "generate-api-reference-docs", "spin-up-a-server"
    ])
    # But on use_cases alone, no queue entry fires.
    assert diff.is_empty() is True


def test_use_case_drift_still_visible_when_other_axes_drift() -> None:
    """When some other axis (a text field, a capability, a category)
    drifts, is_empty() returns False as before and the use_cases delta
    rides along into the queue entry payload."""
    diff = compute_reactualization(
        _snapshot(),
        _enriched(
            short_description="Reworded tagline",
            use_cases=["Wholly new use case"],
        ),
    )
    assert diff.is_empty() is False
    assert diff.use_cases.added == ["wholly-new-use-case"]
    assert diff.use_cases.removed == ["scaffold-a-service"]


def test_as_dict_is_json_safe() -> None:
    """The diff is persisted into ``NeedsReviewQueueEntry.payload`` which
    is a JSONField. ``as_dict`` must produce JSON-serializable shapes."""
    import json
    diff = compute_reactualization(
        _snapshot(),
        _enriched(
            short_description="New",
            capabilities={"read_data": CapabilityProposal(value="no", evidence="e", confidence=0.9)},
            categories=[CategoryProposal(slug="new-cat", confidence=0.8)],
            launch_status="beta",
        ),
    )
    payload = diff.as_dict()
    assert json.dumps(payload)  # must not raise
    assert payload["app_id"] == 1
    assert payload["categories"]["added"] == ["new-cat"]
    assert payload["proposed_launch_status_change"] == ["live", "beta"]
