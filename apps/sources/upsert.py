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
    """Create or update one `AppPlatform` row per platform slug."""
    for slug in draft.platforms:
        try:
            platform = Platform.objects.get(slug=slug)
        except Platform.DoesNotExist:
            logger.warning(
                "upsert_unknown_platform",
                extra={"slug": slug, "app_id": app.pk},
            )
            continue
        AppPlatform.objects.update_or_create(
            app=app,
            platform=platform,
            defaults={
                "official_directory_url": draft.official_directory_url,
                "supported_plans": draft.supported_plans,
                "region_availability": draft.region_availability or "unknown",
                "scope_summary": draft.scope_summary,
                "metadata": draft.platform_metadata or {},
                "last_verified_on_platform_at": timezone.now(),
            },
        )


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


def attach_capabilities(app: App, capabilities: dict[str, str]) -> None:
    """Ensure every known `Capability` has an `AppCapability` row.

    The invariant matters for the search facets: ``yes / no / unknown``
    counts must sum to the total app count. Without this, the "unknown"
    bucket would silently miss capabilities that were never assigned.
    """
    known = {c.key: c for c in Capability.objects.all()}
    for cap_key, cap_obj in known.items():
        value = capabilities.get(cap_key, AppCapability.CapabilityValue.UNKNOWN)
        AppCapability.objects.update_or_create(
            app=app,
            capability=cap_obj,
            defaults={"value": value},
        )


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
    attach_capabilities(app, draft.capabilities)

    Source.objects.create(
        app=app,
        source_type=source_type,
        external_id=draft.external_id,
        source_url=draft.official_directory_url or draft.official_page_url,
        payload=draft.raw_payload,
        is_primary=True,
    )
    return "new"
