"""Versioned prompt templates.

Each prompt is exposed as a *callable* that takes typed inputs and
returns a ``(system, messages)`` pair ready for ``LLMProvider.complete``.
The version string is returned alongside so the Django bridge can write
it to ``LLMCallLog.prompt_version`` — making regression evals
("did prompt v1.1 cause acceptance rate to drop?") possible.

Phase 3 adds a cheap discovery-classification prompt for source candidates.
"""
from __future__ import annotations

from dataclasses import dataclass

from apps.agent.llm.schemas import AppSnapshot
from apps.agent.pipeline.taxonomy import TaxonomySnapshot


@dataclass(frozen=True)
class Prompt:
    version: str
    system: str
    messages: list[dict]


ENRICH_EXISTING_DRAFT_VERSION = "enrich-existing-v1.0"
DISCOVERY_CLASSIFY_VERSION = "discover-v1.0"


def enrich_existing_draft_prompt(
    snapshot: AppSnapshot,
    taxonomy: TaxonomySnapshot,
    *,
    raw_source_text: str = "",
) -> Prompt:
    """Build the prompt for filling gaps in an existing DRAFT App.

    The prompt is intentionally explicit about the merge contract: the
    LLM is told it cannot overwrite existing values, that yes/no
    capability calls need evidence, that low-confidence taxonomy
    guesses should not be made. These same rules are *also* enforced
    in ``apps.agent.pipeline.validate`` — defense in depth.
    """
    system = (
        "You are an editorial assistant for the LLM App Market catalog. "
        "You help fill gaps in DRAFT cards for apps, connectors, and MCP "
        "servers. You NEVER publish; an editor reviews every change.\n\n"
        "Hard rules:\n"
        "1. Only propose values for fields that are currently empty in the "
        "card. If a field has any non-empty value, do not propose changes.\n"
        "2. For capabilities (read_data, write_actions, ...), output 'yes' "
        "or 'no' only when the source explicitly supports the answer; "
        "include a short evidence quote. Otherwise output 'unknown'.\n"
        "3. For categories and listing types, only propose slugs from the "
        "provided allowed lists. Include a confidence 0..1; do not propose "
        "when confidence < 0.7.\n"
        "4. launch_status, pricing_model, and verdict are editorial "
        "decisions. Provide them as PROPOSALS only; the editor decides.\n"
        "5. Never invent URLs. If the source doesn't contain a URL, omit "
        "the field.\n"
    )

    allowed = (
        f"Allowed platform slugs: {sorted(taxonomy.platform_slugs)}\n"
        f"Allowed category slugs: {sorted(taxonomy.category_slugs)}\n"
        f"Allowed listing-type slugs: {sorted(taxonomy.listing_type_slugs)}\n"
        f"Allowed capability keys: {sorted(taxonomy.capability_keys)}\n"
    )

    user_payload = (
        f"Existing card (DRAFT, app_id={snapshot.app_id}, slug={snapshot.slug}):\n"
        f"  name: {snapshot.name!r}\n"
        f"  short_description: {snapshot.short_description!r}\n"
        f"  long_description: {snapshot.long_description!r}\n"
        f"  developer_name: {snapshot.developer_name!r}\n"
        f"  official_page_url: {snapshot.official_page_url!r}\n"
        f"  install_url: {snapshot.install_url!r}\n"
        f"  repo_url: {snapshot.repo_url!r}\n"
        f"  current platforms: {list(snapshot.platform_slugs)}\n"
        f"  current categories: {list(snapshot.category_slugs)}\n"
        f"  current listing_types: {list(snapshot.listing_type_slugs)}\n"
        f"  current capabilities (yes/no/unknown):\n"
    )
    for key in sorted(snapshot.capabilities):
        user_payload += f"    {key}: {snapshot.capabilities[key]}\n"

    user_payload += "\n" + allowed
    if raw_source_text:
        user_payload += f"\nRaw source content:\n---\n{raw_source_text}\n---\n"

    return Prompt(
        version=ENRICH_EXISTING_DRAFT_VERSION,
        system=system,
        messages=[{"role": "user", "content": user_payload}],
    )


def discovery_classify_prompt(candidate_context: str) -> Prompt:
    """Build the prompt for Phase 3 cheap discovery classification."""
    system = (
        "You classify whether a discovered URL should enter the LLM App "
        "Market catalog pipeline. Return relevant=true only for apps, "
        "connectors, interactive apps, agents, or MCP servers that extend "
        "LLM assistants such as ChatGPT, Claude, Gemini, or MCP-compatible "
        "clients. Return false for generic AI news, model releases, blog "
        "posts with no installable/integratable product, tutorials, and "
        "unrelated developer libraries.\n\n"
        "Use the input URL as canonical_url unless the text clearly names a "
        "better product/repository URL. Do not invent URLs."
    )
    return Prompt(
        version=DISCOVERY_CLASSIFY_VERSION,
        system=system,
        messages=[{"role": "user", "content": candidate_context}],
    )
