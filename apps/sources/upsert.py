"""Upsert helpers: turn `AppDraft`s into `App` / `AppPlatform` / `Source` rows.

This module is the single place that knows the relationship between drafts
and the ORM. Sources should stay ORM-free; tasks call into here.

Architecture refs: docs/architecture.md § 9.3–9.5.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal
from urllib.parse import urlparse

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
from .models import DuplicateCandidate, Source

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
# Threshold for "essentially the same product name". 0.85 maps to roughly
# the old Levenshtein distance ≤ 3 cutoff for typical 10-30 char app names
# while staying pure-stdlib (no C-extension build at deploy time).
_NAME_SIMILARITY_THRESHOLD = 0.85
_WEAK_NAME_SIMILARITY_THRESHOLD = 0.92
_WEAK_DOMAIN_NAME_SIMILARITY_THRESHOLD = 0.65


@dataclass(frozen=True)
class DuplicateMatch:
    app: App
    reason: str
    score: float
    evidence: dict


def _name_similarity(a: str, b: str) -> float:
    """0..1 similarity ratio using stdlib ``difflib.SequenceMatcher``."""
    return SequenceMatcher(None, a, b).ratio()


def _parse_url(value: str):
    value = (value or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    if not parsed.netloc:
        return None
    return parsed


def _normalized_domain(value: str) -> str:
    parsed = _parse_url(value)
    if parsed is None:
        return ""
    host = (parsed.hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _normalized_url_identity(value: str) -> str:
    parsed = _parse_url(value)
    if parsed is None:
        return ""
    host = _normalized_domain(value)
    path = (parsed.path or "/").rstrip("/") or "/"
    return f"{host}{path}".lower()


def _github_repo_identity(value: str) -> str:
    parsed = _parse_url(value)
    if parsed is None:
        return ""
    host = _normalized_domain(value)
    if host != "github.com":
        return ""
    parts = [part for part in (parsed.path or "").split("/") if part]
    if len(parts) < 2:
        return ""
    owner = parts[0].lower()
    repo = parts[1].lower()
    if repo.endswith(".git"):
        repo = repo[:-4]
    return f"github.com/{owner}/{repo}"


def _draft_urls(draft: AppDraft) -> list[tuple[str, str]]:
    return [
        ("developer_url", draft.developer_url),
        ("official_page_url", draft.official_page_url),
        ("install_url", draft.install_url),
        ("repo_url", draft.repo_url),
        ("official_directory_url", draft.official_directory_url),
    ]


def _app_urls(app: App) -> list[tuple[str, str]]:
    return [
        ("developer_url", app.developer_url),
        ("official_page_url", app.official_page_url),
        ("install_url", app.install_url),
        ("repo_url", app.repo_url),
    ]


def _identity_match(draft: AppDraft, candidate: App) -> DuplicateMatch | None:
    """Return a strong identity match that is safe for automatic merge."""
    draft_repo_ids = {
        repo_id
        for _, url in _draft_urls(draft)
        if (repo_id := _github_repo_identity(url))
    }
    candidate_repo_ids = {
        repo_id
        for _, url in _app_urls(candidate)
        if (repo_id := _github_repo_identity(url))
    }
    shared_repo = draft_repo_ids & candidate_repo_ids
    if shared_repo:
        repo = sorted(shared_repo)[0]
        return DuplicateMatch(
            app=candidate,
            reason="github_repo",
            score=1.0,
            evidence={"github_repo": repo},
        )

    draft_url_ids = {
        normalized
        for _, url in _draft_urls(draft)
        if (normalized := _normalized_url_identity(url))
    }
    candidate_url_ids = {
        normalized
        for _, url in _app_urls(candidate)
        if (normalized := _normalized_url_identity(url))
    }
    shared_url = draft_url_ids & candidate_url_ids
    if shared_url:
        url_id = sorted(shared_url)[0]
        return DuplicateMatch(
            app=candidate,
            reason="exact_url",
            score=1.0,
            evidence={"url_identity": url_id},
        )

    draft_developer_domain = _normalized_domain(draft.developer_url)
    candidate_developer_domain = _normalized_domain(candidate.developer_url)
    name_score = _name_similarity(candidate.name.lower(), draft.name.lower())
    if (
        draft_developer_domain
        and draft_developer_domain == candidate_developer_domain
        and name_score >= _NAME_SIMILARITY_THRESHOLD
    ):
        return DuplicateMatch(
            app=candidate,
            reason="developer_domain_name",
            score=name_score,
            evidence={
                "developer_domain": draft_developer_domain,
                "name_similarity": name_score,
            },
        )

    if draft.slug_hint:
        slug = slugify(draft.slug_hint)[:200]
        if slug and candidate.slug == slug:
            return DuplicateMatch(
                app=candidate,
                reason="slug",
                score=1.0,
                evidence={"slug": slug},
            )

    return None


def _weak_duplicate_match(draft: AppDraft, candidate: App) -> DuplicateMatch | None:
    """Return a non-authoritative duplicate signal for editor review."""
    name_score = _name_similarity(candidate.name.lower(), draft.name.lower())

    draft_domains = {
        domain
        for _, url in _draft_urls(draft)
        if (domain := _normalized_domain(url))
    }
    candidate_domains = {
        domain
        for _, url in _app_urls(candidate)
        if (domain := _normalized_domain(url))
    }
    shared_domains = draft_domains & candidate_domains
    if (
        shared_domains
        and name_score >= _WEAK_DOMAIN_NAME_SIMILARITY_THRESHOLD
    ):
        return DuplicateMatch(
            app=candidate,
            reason="shared_domain_similar_name",
            score=name_score,
            evidence={
                "domains": sorted(shared_domains),
                "name_similarity": name_score,
            },
        )

    if name_score >= _WEAK_NAME_SIMILARITY_THRESHOLD:
        return DuplicateMatch(
            app=candidate,
            reason="similar_name",
            score=name_score,
            evidence={"name_similarity": name_score},
        )

    return None


def find_soft_duplicate(draft: AppDraft) -> App | None:
    """Look for an existing App that might be the same product."""
    for candidate in App.objects.all().only(
        "id", "name", "slug", "developer_url", "official_page_url",
        "install_url", "repo_url",
    ):
        match = _identity_match(draft, candidate)
        if match is not None:
            return match.app

    return None


def find_duplicate_candidates(draft: AppDraft, *, limit: int = 5) -> list[DuplicateMatch]:
    """Return weaker duplicate signals that should be reviewed by an editor."""
    matches: list[DuplicateMatch] = []
    for candidate in App.objects.all().only(
        "id", "name", "slug", "developer_url", "official_page_url",
        "install_url", "repo_url",
    ):
        if _identity_match(draft, candidate) is not None:
            continue
        match = _weak_duplicate_match(draft, candidate)
        if match is not None:
            matches.append(match)
    matches.sort(key=lambda item: item.score, reverse=True)
    return matches[:limit]


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
            defaults["note"] = cap_evidence[:500]
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

    duplicate_candidates = find_duplicate_candidates(draft)
    return _create_new_app(
        draft,
        source_type,
        duplicate_candidates=duplicate_candidates,
    )


@transaction.atomic
def _create_new_app(
    draft: AppDraft,
    source_type: str,
    *,
    duplicate_candidates: list[DuplicateMatch] | None = None,
) -> UpsertOutcome:
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

    source = Source.objects.create(
        app=app,
        source_type=source_type,
        external_id=draft.external_id,
        source_url=draft.official_directory_url or draft.official_page_url,
        payload=draft.raw_payload,
        is_primary=True,
    )

    for match in duplicate_candidates or []:
        DuplicateCandidate.objects.get_or_create(
            app=app,
            candidate_app=match.app,
            match_reason=match.reason,
            defaults={
                "source": source,
                "score": match.score,
                "evidence": match.evidence,
            },
        )
    return "new"
