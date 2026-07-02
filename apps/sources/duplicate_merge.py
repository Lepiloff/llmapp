"""Conservative duplicate-candidate merge helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from apps.agent.models import EnrichmentTask, NeedsReviewQueueEntry
from apps.catalog.models import (
    App,
    AppCapability,
    AppCategory,
    AppPlatform,
    AppUseCase,
)
from apps.sources.models import DuplicateCandidate, Source

MCP_SOURCE_TYPES = {Source.SourceType.MCP_REGISTRY}
MCP_PLATFORM_SLUG = "mcp"
MCP_LISTING_TYPE_SLUG = "mcp-server"
MERGEABLE_MATCH_REASONS = {"similar_name", "shared_domain_similar_name"}


@dataclass(slots=True)
class DuplicateMergeDecision:
    duplicate_id: int
    app_id: int
    app_slug: str
    app_name: str
    candidate_id: int
    candidate_slug: str
    candidate_name: str
    target_id: int | None = None
    target_slug: str = ""
    source_id: int | None = None
    source_slug: str = ""
    blockers: list[str] = field(default_factory=list)
    would_merge: bool = False
    merged: bool = False
    counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "duplicate_id": self.duplicate_id,
            "app_id": self.app_id,
            "app_slug": self.app_slug,
            "app_name": self.app_name,
            "candidate_id": self.candidate_id,
            "candidate_slug": self.candidate_slug,
            "candidate_name": self.candidate_name,
            "target_id": self.target_id,
            "target_slug": self.target_slug,
            "source_id": self.source_id,
            "source_slug": self.source_slug,
            "would_merge": self.would_merge,
            "merged": self.merged,
            "blockers": self.blockers,
            "counts": self.counts,
        }


def merge_cross_platform_duplicate_candidates(
    *,
    limit: int = 100,
    apply: bool = False,
    include_mcp: bool = False,
) -> dict[str, Any]:
    decisions = [
        _plan_duplicate_merge(duplicate, include_mcp=include_mcp)
        for duplicate in _candidate_duplicates(limit=limit)
    ]

    if apply:
        for decision in decisions:
            if decision.would_merge:
                applied = apply_duplicate_merge(
                    decision.duplicate_id,
                    include_mcp=include_mcp,
                )
                decision.merged = applied.merged
                decision.blockers = applied.blockers
                decision.counts = applied.counts

    blocker_counts: dict[str, int] = {}
    for decision in decisions:
        for blocker in decision.blockers:
            blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1

    return {
        "apply": apply,
        "include_mcp": include_mcp,
        "evaluated": len(decisions),
        "would_merge": sum(item.would_merge for item in decisions),
        "merged": sum(item.merged for item in decisions),
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "results": [item.as_dict() for item in decisions],
    }


def _candidate_duplicates(*, limit: int):
    return (
        DuplicateCandidate.objects.filter(
            status=DuplicateCandidate.Status.PENDING,
            match_reason__in=MERGEABLE_MATCH_REASONS,
        )
        .select_related("app", "candidate_app", "source")
        .order_by("id")[:limit]
    )


@transaction.atomic
def apply_duplicate_merge(
    duplicate_id: int,
    *,
    include_mcp: bool = False,
) -> DuplicateMergeDecision:
    duplicate = (
        DuplicateCandidate.objects.select_for_update()
        .select_related("app", "candidate_app")
        .get(pk=duplicate_id)
    )
    app_ids = sorted({duplicate.app_id, duplicate.candidate_app_id})
    list(App.objects.select_for_update().filter(pk__in=app_ids))
    decision = _plan_duplicate_merge(duplicate, include_mcp=include_mcp)
    if not decision.would_merge or decision.target_id is None or decision.source_id is None:
        return decision

    target = App.objects.get(pk=decision.target_id)
    source = App.objects.get(pk=decision.source_id)
    counts = _merge_duplicate_apps(target=target, source=source)

    now = timezone.now()
    DuplicateCandidate.objects.filter(pk=duplicate.pk).update(
        status=DuplicateCandidate.Status.CONFIRMED,
        resolved_at=now,
    )
    DuplicateCandidate.objects.filter(
        Q(app=source, candidate_app=target) | Q(app=target, candidate_app=source),
        status=DuplicateCandidate.Status.PENDING,
    ).update(
        status=DuplicateCandidate.Status.CONFIRMED,
        resolved_at=now,
    )

    source.status = App.AppStatus.HIDDEN
    source.is_indexable = False
    source.save(update_fields=["status", "is_indexable", "updated_at"])

    target.save(update_fields=["updated_at"])
    decision.merged = True
    decision.counts = counts
    return decision


def _plan_duplicate_merge(
    duplicate: DuplicateCandidate,
    *,
    include_mcp: bool,
) -> DuplicateMergeDecision:
    app = duplicate.app
    candidate = duplicate.candidate_app
    decision = DuplicateMergeDecision(
        duplicate_id=duplicate.pk,
        app_id=app.pk,
        app_slug=app.slug,
        app_name=app.name,
        candidate_id=candidate.pk,
        candidate_slug=candidate.slug,
        candidate_name=candidate.name,
    )
    blockers: list[str] = []
    if duplicate.status != DuplicateCandidate.Status.PENDING:
        blockers.append("duplicate_not_pending")
    if duplicate.match_reason not in MERGEABLE_MATCH_REASONS:
        blockers.append(f"unsupported_match_reason:{duplicate.match_reason}")
    if _normalized_name(app.name) != _normalized_name(candidate.name):
        blockers.append("name_not_exact_match")
    if app.status != App.AppStatus.DRAFT or candidate.status != App.AppStatus.DRAFT:
        blockers.append("requires_two_drafts")
    if not include_mcp and (_has_mcp_identity(app) or _has_mcp_identity(candidate)):
        blockers.append("mcp_requires_include_mcp")
    related_pending = DuplicateCandidate.objects.filter(
        Q(app__in=[app, candidate]) | Q(candidate_app__in=[app, candidate]),
        status=DuplicateCandidate.Status.PENDING,
    ).exclude(pk=duplicate.pk)
    if related_pending.exists():
        blockers.append("related_pending_duplicate_candidates")
    blockers.extend(_capability_conflict_blockers(app, candidate))

    target, source = _choose_canonical_pair(app, candidate)
    decision.target_id = target.pk
    decision.target_slug = target.slug
    decision.source_id = source.pk
    decision.source_slug = source.slug
    decision.blockers = list(dict.fromkeys(blockers))
    decision.would_merge = not decision.blockers
    return decision


def _normalized_name(value: str) -> str:
    return slugify(value or "")


def _has_mcp_identity(app: App) -> bool:
    return (
        app.sources.filter(source_type__in=MCP_SOURCE_TYPES).exists()
        or app.platforms.filter(slug=MCP_PLATFORM_SLUG).exists()
        or app.listing_types.filter(slug=MCP_LISTING_TYPE_SLUG).exists()
    )


def _choose_canonical_pair(first: App, second: App) -> tuple[App, App]:
    scored = sorted(
        ((first, _canonical_score(first)), (second, _canonical_score(second))),
        key=lambda item: (item[1], -item[0].pk),
        reverse=True,
    )
    target = scored[0][0]
    source = second if target.pk == first.pk else first
    return target, source


def _canonical_score(app: App) -> int:
    return (
        app.sources.filter(is_active=True).count() * 20
        + app.platforms.count() * 15
        + app.listing_types.count() * 10
        + app.categories.count() * 5
        + int(bool(app.install_url)) * 8
        + int(bool(app.official_page_url)) * 5
        + int(bool(app.long_description)) * 3
        + len(app.short_description or "") // 20
    )


def _capability_conflict_blockers(first: App, second: App) -> list[str]:
    blockers: list[str] = []
    first_caps = {
        row.capability_id: row
        for row in AppCapability.objects.filter(app=first)
        .exclude(value=AppCapability.CapabilityValue.UNKNOWN)
        .select_related("capability")
    }
    second_caps = {
        row.capability_id: row
        for row in AppCapability.objects.filter(app=second)
        .exclude(value=AppCapability.CapabilityValue.UNKNOWN)
        .select_related("capability")
    }
    for capability_id in first_caps.keys() & second_caps.keys():
        if first_caps[capability_id].value != second_caps[capability_id].value:
            blockers.append(f"capability_conflict:{first_caps[capability_id].capability.key}")
    return blockers


def _merge_duplicate_apps(*, target: App, source: App) -> dict[str, int]:
    counts = {
        "sources": _move_sources(target=target, source=source),
        "platforms": _merge_platforms(target=target, source=source),
        "listing_types": _copy_listing_types(target=target, source=source),
        "categories": _merge_categories(target=target, source=source),
        "capabilities": _merge_capabilities(target=target, source=source),
        "use_cases": _merge_use_cases(target=target, source=source),
        "review_entries": NeedsReviewQueueEntry.objects.filter(app=source).update(
            app=target
        ),
        "enrichment_tasks": EnrichmentTask.objects.filter(app=source).update(
            app=target
        ),
    }
    _fill_empty_app_fields(target=target, source=source)
    return counts


def _move_sources(*, target: App, source: App) -> int:
    moved = 0
    target_has_primary = Source.objects.filter(app=target, is_primary=True).exists()
    for row in Source.objects.select_for_update().filter(app=source).order_by("pk"):
        row.app = target
        if target_has_primary and row.is_primary:
            row.is_primary = False
        elif row.is_primary:
            target_has_primary = True
        row.save(update_fields=["app", "is_primary"])
        moved += 1
    return moved


def _merge_platforms(*, target: App, source: App) -> int:
    merged = 0
    for row in AppPlatform.objects.select_for_update().filter(app=source):
        existing = AppPlatform.objects.filter(
            app=target,
            platform=row.platform,
        ).first()
        if existing is None:
            row.app = target
            row.save(update_fields=["app"])
            merged += 1
            continue

        fields: list[str] = []
        if (
            existing.compatibility_status == AppPlatform.CompatibilityStatus.UNKNOWN
            and row.compatibility_status != AppPlatform.CompatibilityStatus.UNKNOWN
        ):
            existing.compatibility_status = row.compatibility_status
            fields.append("compatibility_status")
        if not existing.supported_plans and row.supported_plans:
            existing.supported_plans = row.supported_plans
            fields.append("supported_plans")
        if (
            existing.region_availability == AppPlatform.RegionAvailability.UNKNOWN
            and row.region_availability != AppPlatform.RegionAvailability.UNKNOWN
        ):
            existing.region_availability = row.region_availability
            fields.append("region_availability")
        for field_name in (
            "supported_models",
            "scope_summary",
            "official_directory_url",
            "install_url",
            "notes",
        ):
            if not getattr(existing, field_name) and getattr(row, field_name):
                setattr(existing, field_name, getattr(row, field_name))
                fields.append(field_name)
        merged_metadata = {**(row.metadata or {}), **(existing.metadata or {})}
        if merged_metadata != (existing.metadata or {}):
            existing.metadata = merged_metadata
            fields.append("metadata")
        if not existing.last_verified_on_platform_at and row.last_verified_on_platform_at:
            existing.last_verified_on_platform_at = row.last_verified_on_platform_at
            fields.append("last_verified_on_platform_at")
        if fields:
            existing.save(update_fields=fields)
        row.delete()
        merged += 1
    return merged


def _copy_listing_types(*, target: App, source: App) -> int:
    before = target.listing_types.count()
    target.listing_types.add(*source.listing_types.all())
    return target.listing_types.count() - before


def _merge_categories(*, target: App, source: App) -> int:
    merged = 0
    target_has_primary = AppCategory.objects.filter(app=target, is_primary=True).exists()
    for row in AppCategory.objects.select_for_update().filter(app=source):
        existing = AppCategory.objects.filter(
            app=target,
            category=row.category,
        ).first()
        if existing is None:
            row.app = target
            if row.is_primary and target_has_primary:
                row.is_primary = False
            row.save(update_fields=["app", "is_primary"])
            target_has_primary = target_has_primary or row.is_primary
            merged += 1
            continue
        if row.is_primary and not target_has_primary and not existing.is_primary:
            existing.is_primary = True
            existing.save(update_fields=["is_primary"])
            target_has_primary = True
        row.delete()
        merged += 1
    return merged


def _merge_capabilities(*, target: App, source: App) -> int:
    merged = 0
    for row in AppCapability.objects.select_for_update().filter(app=source):
        existing = AppCapability.objects.filter(
            app=target,
            capability=row.capability,
        ).first()
        if existing is None:
            row.app = target
            row.save(update_fields=["app"])
            merged += 1
            continue
        if (
            existing.value == AppCapability.CapabilityValue.UNKNOWN
            and row.value != AppCapability.CapabilityValue.UNKNOWN
        ):
            existing.value = row.value
            existing.note = row.note
            existing.save(update_fields=["value", "note"])
        elif not existing.note and row.note:
            existing.note = row.note
            existing.save(update_fields=["note"])
        row.delete()
        merged += 1
    return merged


def _merge_use_cases(*, target: App, source: App) -> int:
    merged = 0
    for row in AppUseCase.objects.select_for_update().filter(app=source):
        existing = AppUseCase.objects.filter(
            app=target,
            use_case=row.use_case,
        ).first()
        if existing is None:
            row.app = target
            row.save(update_fields=["app"])
        else:
            row.delete()
        merged += 1
    return merged


def _fill_empty_app_fields(*, target: App, source: App) -> None:
    fields: list[str] = []
    for field_name in (
        "long_description",
        "developer_name",
        "developer_url",
        "official_page_url",
        "install_url",
        "repo_url",
        "contact_email",
        "meta_title",
        "meta_description",
    ):
        if not getattr(target, field_name) and getattr(source, field_name):
            setattr(target, field_name, getattr(source, field_name))
            fields.append(field_name)
    if (
        target.platform_verification_status == App.PlatformVerificationStatus.UNKNOWN
        and source.platform_verification_status != App.PlatformVerificationStatus.UNKNOWN
    ):
        target.platform_verification_status = source.platform_verification_status
        fields.append("platform_verification_status")
    if fields:
        target.save(update_fields=[*fields, "updated_at"])
