"""TaxonomySnapshot — the pure-Python view of the catalog taxonomy.

The pipeline must know which slugs (platforms, categories, capabilities,
listing types) are *valid* before it asks an LLM to assign them. Reading
the taxonomy from the DB inside ``pipeline/enrich.py`` would couple the
pure-Python pipeline to Django and defeat the "extract to a separate
service" plan in ``docs/agent-pipeline.md``.

``TaxonomySnapshot`` is therefore a frozen dataclass produced by the
Django bridge (``apps.agent.persist.build_taxonomy_snapshot``) and
passed *in* to every pipeline call as a parameter. The pipeline never
imports Django.

Frozen + slug-tuples-not-lists keeps the immutable surface that
matters: pipeline code cannot accidentally mutate its inputs.
``capability_descriptions`` / ``category_descriptions`` are mappings
(dict-backed), which makes the dataclass *not* hashable — that's
intentional and not load-bearing; we want fast lookups, not set/dict
keys.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class TaxonomySnapshot:
    platform_slugs: tuple[str, ...] = ()
    category_slugs: tuple[str, ...] = ()
    capability_keys: tuple[str, ...] = ()
    listing_type_slugs: tuple[str, ...] = ()

    # Human-readable labels — passed to LLM prompts so it can disambiguate
    # similar slugs without us having to invent a separate prompt key.
    # Empty mappings are valid (e.g. when the snapshot is built for tests).
    capability_descriptions: Mapping[str, str] = field(default_factory=dict)
    category_descriptions: Mapping[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Cheap membership helpers — the validator layer uses these heavily.
    # ------------------------------------------------------------------
    def has_platform(self, slug: str) -> bool:
        return slug in self.platform_slugs

    def has_category(self, slug: str) -> bool:
        return slug in self.category_slugs

    def has_capability(self, key: str) -> bool:
        return key in self.capability_keys

    def has_listing_type(self, slug: str) -> bool:
        return slug in self.listing_type_slugs
