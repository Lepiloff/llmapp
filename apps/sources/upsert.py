"""Upsert helpers: turn `AppDraft`s into `App` / `AppPlatform` / `Source` rows.

This module is the single place that knows the relationship between drafts
and the ORM. Sources should stay ORM-free; tasks call into here.

Architecture refs: docs/architecture.md § 9.3–9.5.
"""
from __future__ import annotations

import logging
from typing import Literal

import Levenshtein
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.catalog.models import (
    App,
    AppCapability,
    AppPlatform,
    Capability,
    Category,
    ListingType,
    Platform,
    UseCase,
)

from .base import AppDraft
from .models import Source

logger = logging.getLogger(__name__)


UpsertOutcome = Literal["new", "updated", "skipped"]


# ---------------------------------------------------------------------------
# Slugging
# ---------------------------------------------------------------------------
def unique_slug(hint: str) -> str:
    """Return a slug guaranteed to be unique in the App table.

    Hint is truncated to 200 chars to leave room for the `-N` suffix.
    """
    base = slugify(hint or "app")[:200] or "app"
    slug = base
    suffix = 2
    while App.objects.filter(slug=slug).exists():
        slug = f"{base[:200 - len(str(suffix)) - 1]}-{suffix}"
        suffix += 1
    return slug


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------
def find_soft_duplicate(draft: AppDraft) -> App | None:
    """Look for an existing App that might be the same product."""
    if draft.developer_url:
        for candidate in App.objects.filter(developer_url__iexact=draft.developer_url):
            if Levenshtein.distance(
                candidate.name.lower(), draft.name.lower()
            ) <= 3:
                return candidate

    if draft.install_url:
        candidate = App.objects.filter(install_url__iexact=draft.install_url).first()
        if candidate:
            return candidate

    if draft.slug_hint:
        candidate = App.objects.filter(slug=slugify(draft.slug_hint)[:200]).first()
        if candidate:
            return candidate

    return None


# ---------------------------------------------------------------------------
# attach_* — write per-relation data once the App exists.
# ---------------------------------------------------------------------------
def attach_platforms(app: App, draft: AppDraft) -> None:
    """Create or update one ``AppPlatform`` row per platform slug.

    First-time creation fills every field from the draft. On a refresh
    (the same (app, platform) row already exists) we mirror the
    ``apps.agent.persist._apply_field_updates`` contract: editor-owned
    fields are filled only when still empty/UNKNOWN. ``metadata`` is
    *shallow-merged* — draft keys are added/overwritten, but keys the
    editor put there by hand survive. This protects against the
    Phase 3 regression where re-discovering the same external_id
    silently clobbered ``region_availability`` / ``supported_plans``
    that an editor had set on a DRAFT card.

    Race-safety: the snapshot-then-update is wrapped in
    ``transaction.atomic()`` with ``select_for_update()`` on the
    existing row, so an editor edit committed between our read and our
    write would either block us until commit (and we'd see the
    editor's value under the lock, preserving it) or block the editor
    until we commit (and the editor's transaction would re-read our
    write before deciding what to overwrite).
    """
    for slug in draft.platforms:
        try:
            platform = Platform.objects.get(slug=slug)
        except Platform.DoesNotExist:
            logger.warning(
                "upsert_unknown_platform",
                extra={"slug": slug, "app_id": app.pk},
            )
            continue

        with transaction.atomic():
            existing = (
                AppPlatform.objects.select_for_update()
                .filter(app=app, platform=platform)
                .first()
            )
            if existing is None:
                AppPlatform.objects.create(
                    app=app,
                    platform=platform,
                    official_directory_url=draft.official_directory_url,
                    supported_plans=list(draft.supported_plans or []),
                    region_availability=draft.region_availability or "unknown",
                    scope_summary=draft.scope_summary,
                    metadata=dict(draft.platform_metadata or {}),
                    last_verified_on_platform_at=timezone.now(),
                )
                continue

            updates: dict = {"last_verified_on_platform_at": timezone.now()}

            # Fill empty fields only — never overwrite editor edits. The
            # locked row's values are what we compare against, so a
            # concurrent editor write that committed before our lock is
            # visible here and survives.
            if not existing.official_directory_url and draft.official_directory_url:
                updates["official_directory_url"] = draft.official_directory_url
            if not existing.supported_plans and draft.supported_plans:
                updates["supported_plans"] = list(draft.supported_plans)
            if (
                existing.region_availability
                == AppPlatform.RegionAvailability.UNKNOWN
                and draft.region_availability
                and draft.region_availability
                != AppPlatform.RegionAvailability.UNKNOWN
            ):
                updates["region_availability"] = draft.region_availability
            if not existing.scope_summary and draft.scope_summary:
                updates["scope_summary"] = draft.scope_summary

            # Shallow-merge metadata: draft fills missing keys, editor wins
            # on conflicts.
            merged_metadata = dict(draft.platform_metadata or {})
            merged_metadata.update(existing.metadata or {})
            if merged_metadata != (existing.metadata or {}):
                updates["metadata"] = merged_metadata

            AppPlatform.objects.filter(pk=existing.pk).update(**updates)


def attach_listing_types(app: App, slugs: list[str]) -> None:
    """Replace listing-type membership with the supplied slugs."""
    if not slugs:
        return
    types = list(ListingType.objects.filter(slug__in=slugs))
    if types:
        app.listing_types.add(*types)


def attach_categories(app: App, slugs: list[str]) -> None:
    if not slugs:
        return
    categories = list(Category.objects.filter(slug__in=slugs))
    if categories:
        app.categories.add(*categories)


def attach_capabilities(
    app: App,
    capabilities: dict[str, str],
    evidence: dict[str, str] | None = None,
) -> None:
    """Ensure every known `Capability` has an `AppCapability` row.

    The invariant matters for the search facets: ``yes / no / unknown``
    counts must sum to the total app count. Without this, the "unknown"
    bucket would silently miss capabilities that were never assigned.

    ``evidence`` (optional) carries the source quote backing each yes/no
    value. Stored verbatim in ``AppCapability.note`` so the admin review
    UI shows the LLM's justification next to the value.
    """
    evidence = evidence or {}
    known = {c.key: c for c in Capability.objects.all()}
    for cap_key, cap_obj in known.items():
        value = capabilities.get(cap_key, AppCapability.CapabilityValue.UNKNOWN)
        defaults = {"value": value}
        cap_evidence = (evidence.get(cap_key) or "").strip()
        if cap_evidence:
            defaults["note"] = cap_evidence[:200]
        AppCapability.objects.update_or_create(
            app=app,
            capability=cap_obj,
            defaults=defaults,
        )


def attach_use_cases(app: App, titles: list[str]) -> None:
    """Resolve free-text use-case labels to ``UseCase`` rows and attach.

    Matches the merge-path behaviour
    (``apps.agent.persist._apply_use_cases``): existing slugs are reused,
    new ones get created on the fly. Slug collisions (different titles
    that slugify to the same string) reuse the first existing row.
    """
    if not titles:
        return
    use_case_rows: list[UseCase] = []
    seen_slugs: set[str] = set()
    for title in titles:
        slug = slugify(title)[:200] or "use-case"
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        row, _ = UseCase.objects.get_or_create(slug=slug, defaults={"title": title})
        use_case_rows.append(row)
    if use_case_rows:
        app.use_cases.add(*use_case_rows)


# ---------------------------------------------------------------------------
# upsert_app_from_draft
# ---------------------------------------------------------------------------
def upsert_app_from_draft(draft: AppDraft, source_type: str) -> UpsertOutcome:
    """Create / refresh an App from a normalized draft.

    Behaviour:
      * If a `Source` row already exists for `(source_type, external_id)`,
        the existing app is refreshed with any *missing* fields only —
        editor edits are sacred and never overwritten.
      * Otherwise, we look for a soft duplicate. Soft matches don't get
        merged automatically; we just attach a second `Source` row so the
        editor can decide.
      * Truly new drafts get a fresh `App` in DRAFT status, with
        ``platform_verification_status`` initialised based on the source
        identity (the MCP Registry IS an official directory).
    """
    existing = Source.objects.filter(
        source_type=source_type, external_id=draft.external_id
    ).first()

    if existing and draft.external_id:
        app = existing.app
        # Refresh only safe fields; do NOT clobber editorial state.
        safe_text_fields = (
            "long_description",
            "official_page_url",
            "install_url",
            "repo_url",
        )
        dirty = []
        for field in safe_text_fields:
            if not getattr(app, field) and getattr(draft, field):
                setattr(app, field, getattr(draft, field))
                dirty.append(field)
        existing.payload = draft.raw_payload
        existing.fetched_at = timezone.now()
        existing.is_active = True
        existing.save(update_fields=["payload", "fetched_at", "is_active"])
        if dirty:
            app.save(update_fields=dirty + ["updated_at"])
        # Always refresh the platform link so directory URL stays current.
        attach_platforms(app, draft)
        return "updated"

    soft_dup = find_soft_duplicate(draft)
    if soft_dup is not None:
        Source.objects.create(
            app=soft_dup,
            source_type=source_type,
            external_id=draft.external_id,
            source_url=draft.official_directory_url or draft.official_page_url,
            payload=draft.raw_payload,
        )
        return "skipped"

    return _create_new_app(draft, source_type)


@transaction.atomic
def _create_new_app(draft: AppDraft, source_type: str) -> UpsertOutcome:
    platform_verification = (
        App.PlatformVerificationStatus.OFFICIAL
        if source_type == Source.SourceType.MCP_REGISTRY
        else App.PlatformVerificationStatus.UNKNOWN
    )

    app = App.objects.create(
        name=draft.name,
        slug=unique_slug(draft.slug_hint),
        short_description=draft.short_description,
        long_description=draft.long_description,
        developer_name=draft.developer_name,
        developer_url=draft.developer_url,
        official_page_url=draft.official_page_url,
        install_url=draft.install_url,
        repo_url=draft.repo_url,
        status=App.AppStatus.DRAFT,
        platform_verification_status=platform_verification,
        editorial_review_status=App.EditorialReviewStatus.UNREVIEWED,
        developer_claim_status=App.DeveloperClaimStatus.UNCLAIMED,
        pricing_model=draft.pricing_model or App.PricingModel.UNKNOWN,
        launch_status=draft.launch_status or App.LaunchStatus.LIVE,
    )

    attach_platforms(app, draft)
    attach_listing_types(app, draft.listing_types)
    attach_categories(app, draft.categories)
    attach_capabilities(app, draft.capabilities, draft.capability_evidence)
    attach_use_cases(app, draft.use_cases)

    Source.objects.create(
        app=app,
        source_type=source_type,
        external_id=draft.external_id,
        source_url=draft.official_directory_url or draft.official_page_url,
        payload=draft.raw_payload,
        is_primary=True,
    )
    return "new"
