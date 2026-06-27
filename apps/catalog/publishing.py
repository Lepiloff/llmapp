"""Autopublish policy for controlled production pilots.

This module intentionally sits next to the strict publish gate in
``apps.catalog.services``. Automation may prepare an app for publication, but
the final transition still goes through ``transition_to_published`` so admin
actions and batch jobs share one validator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.agent.models import NeedsReviewQueueEntry
from apps.catalog.models import App, AppCapability, AppPlatform
from apps.catalog.services import transition_to_published
from apps.sources.models import DuplicateCandidate, Source

NON_MCP_AUTOPUBLISH_SOURCE_TYPES = (
    Source.SourceType.GEMINI_EXTENSIONS,
    Source.SourceType.CLAUDE_CONNECTORS,
    Source.SourceType.CHATGPT_UNOFFICIAL,
)

SOURCE_TYPE_ALIASES = {
    "chatgpt_apps": Source.SourceType.CHATGPT_UNOFFICIAL,
    "chatgpt_unofficial": Source.SourceType.CHATGPT_UNOFFICIAL,
    "claude": Source.SourceType.CLAUDE_CONNECTORS,
    "gemini": Source.SourceType.GEMINI_EXTENSIONS,
}

LOW_INFORMATION_VERDICTS = {
    "good fit",
    "positive",
    "propose",
    "propose approval",
    "recommended",
}


@dataclass(slots=True)
class ReviewPlan:
    blockers: list[str] = field(default_factory=list)
    app_updates: dict[str, Any] = field(default_factory=dict)
    platform_scope_summary: str = ""
    entry_ids: list[int] = field(default_factory=list)

    @property
    def has_updates(self) -> bool:
        return bool(self.app_updates or self.platform_scope_summary)


@dataclass(slots=True)
class PublishDecision:
    app_id: int
    slug: str
    name: str
    source_types: list[str]
    would_publish: bool
    blockers: list[str]
    auto_review_entry_ids: list[int]
    app_updates: dict[str, Any]
    platform_scope_summary: str
    published: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "slug": self.slug,
            "name": self.name,
            "source_types": self.source_types,
            "would_publish": self.would_publish,
            "published": self.published,
            "blockers": self.blockers,
            "auto_review_entry_ids": self.auto_review_entry_ids,
            "app_updates": self.app_updates,
            "platform_scope_summary": self.platform_scope_summary,
        }


def normalize_autopublish_source_types(source_types: list[str]) -> tuple[str, ...]:
    if not source_types:
        return tuple(NON_MCP_AUTOPUBLISH_SOURCE_TYPES)

    normalized: list[str] = []
    for source_type in source_types:
        value = SOURCE_TYPE_ALIASES.get(source_type, source_type)
        if value == "all_non_mcp":
            normalized.extend(NON_MCP_AUTOPUBLISH_SOURCE_TYPES)
            continue
        if value not in Source.SourceType.values:
            raise ValueError(f"Unknown source type: {source_type}")
        normalized.append(value)
    return tuple(dict.fromkeys(normalized))


def autopublish_candidates_queryset(source_types: tuple[str, ...]):
    return (
        App.objects.filter(
            status=App.AppStatus.DRAFT,
            sources__source_type__in=source_types,
            sources__is_active=True,
        )
        .distinct()
        .order_by("first_seen_at", "pk")
    )


def evaluate_autopublish_candidate(
    app: App,
    *,
    source_types: tuple[str, ...],
    auto_review: bool = True,
) -> PublishDecision:
    blockers = _base_publish_blockers(app, source_types=source_types)
    review_plan = ReviewPlan()
    pending_entries = list(
        NeedsReviewQueueEntry.objects.filter(
            app=app,
            review_outcome=NeedsReviewQueueEntry.ReviewOutcome.PENDING,
        ).order_by("pk")
    )
    if pending_entries:
        if auto_review:
            review_plan = _plan_auto_review(app, pending_entries)
            blockers.extend(review_plan.blockers)
        else:
            blockers.append("pending_review_entries")

    if not (app.verdict or review_plan.app_updates.get("verdict")):
        blockers.append("verdict_required")

    active_source_types = list(
        app.sources.filter(source_type__in=source_types, is_active=True)
        .order_by("source_type")
        .values_list("source_type", flat=True)
        .distinct()
    )
    blockers = list(dict.fromkeys(blockers))
    return PublishDecision(
        app_id=app.pk,
        slug=app.slug,
        name=app.name,
        source_types=active_source_types,
        would_publish=not blockers,
        blockers=blockers,
        auto_review_entry_ids=review_plan.entry_ids,
        app_updates=review_plan.app_updates,
        platform_scope_summary=review_plan.platform_scope_summary,
    )


def autopublish_batch(
    *,
    source_types: tuple[str, ...],
    limit: int = 50,
    apply: bool = False,
    auto_review: bool = True,
) -> dict[str, Any]:
    queryset = autopublish_candidates_queryset(source_types)[:limit]
    decisions = [
        evaluate_autopublish_candidate(
            app,
            source_types=source_types,
            auto_review=auto_review,
        )
        for app in queryset
    ]

    if apply:
        for decision in decisions:
            if decision.would_publish:
                applied = apply_autopublish_decision(
                    decision.app_id,
                    source_types=source_types,
                    auto_review=auto_review,
                )
                decision.published = applied.published
                decision.blockers = applied.blockers
                decision.would_publish = applied.would_publish

    blocker_counts: dict[str, int] = {}
    for decision in decisions:
        for blocker in decision.blockers:
            blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1

    return {
        "apply": apply,
        "source_types": list(source_types),
        "evaluated": len(decisions),
        "would_publish": sum(1 for item in decisions if item.would_publish),
        "published": sum(1 for item in decisions if item.published),
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "results": [item.as_dict() for item in decisions],
    }


@transaction.atomic
def apply_autopublish_decision(
    app_id: int,
    *,
    source_types: tuple[str, ...],
    auto_review: bool = True,
) -> PublishDecision:
    app = (
        App.objects.select_for_update()
        .prefetch_related("sources", "platform_links", "categories", "listing_types")
        .get(pk=app_id)
    )
    decision = evaluate_autopublish_candidate(
        app,
        source_types=source_types,
        auto_review=auto_review,
    )
    if not decision.would_publish:
        return decision

    update_fields: list[str] = []
    for field_name, value in decision.app_updates.items():
        setattr(app, field_name, value)
        update_fields.append(field_name)
    app.editorial_review_status = App.EditorialReviewStatus.REVIEWED
    update_fields.append("editorial_review_status")
    app.save(update_fields=[*update_fields, "updated_at"])

    if decision.platform_scope_summary:
        AppPlatform.objects.filter(app=app, scope_summary="").update(
            scope_summary=decision.platform_scope_summary
        )

    if decision.auto_review_entry_ids:
        NeedsReviewQueueEntry.objects.filter(
            pk__in=decision.auto_review_entry_ids,
            review_outcome=NeedsReviewQueueEntry.ReviewOutcome.PENDING,
        ).update(
            resolved_at=timezone.now(),
            review_outcome=(
                NeedsReviewQueueEntry.ReviewOutcome.ACCEPTED
                if decision.app_updates or decision.platform_scope_summary
                else NeedsReviewQueueEntry.ReviewOutcome.NO_ACTION
            ),
            resolution_note="Auto-resolved by conservative autopublish policy",
        )

    app.refresh_from_db()
    transition_to_published(app, editor=None)
    decision.published = True
    return decision


def _base_publish_blockers(app: App, *, source_types: tuple[str, ...]) -> list[str]:
    blockers: list[str] = []
    mcp_allowed = Source.SourceType.MCP_REGISTRY in source_types
    if not mcp_allowed and app.sources.filter(
        source_type=Source.SourceType.MCP_REGISTRY
    ).exists():
        blockers.append("mcp_source_requires_include_mcp")
    if not mcp_allowed and app.platforms.filter(slug="mcp").exists():
        blockers.append("mcp_platform_requires_include_mcp")
    if not mcp_allowed and app.listing_types.filter(slug="mcp-server").exists():
        blockers.append("mcp_listing_type_requires_include_mcp")
    if len(app.short_description or "") < 60:
        blockers.append("short_description_lt_60")
    if not (app.long_description or "").strip():
        blockers.append("long_description_required")
    if not app.listing_types.exists():
        blockers.append("listing_type_required")
    if not app.platforms.exists():
        blockers.append("platform_required")
    if not app.categories.exists():
        blockers.append("category_required")
    if _explicit_capability_count(app) < 3:
        blockers.append("explicit_capabilities_lt_3")
    if not (app.official_page_url or app.install_url):
        blockers.append("official_or_install_url_required")
    if app.platform_verification_status == App.PlatformVerificationStatus.UNKNOWN:
        blockers.append("platform_verification_unknown")
    if (
        app.platform_verification_status == App.PlatformVerificationStatus.OFFICIAL
        and not app.has_official_platform_url
    ):
        blockers.append("official_directory_url_required")
    if app.launch_status == App.LaunchStatus.DEPRECATED:
        blockers.append("deprecated_launch_status")
    if app.link_health.filter(consecutive_failures__gte=1).exists():
        blockers.append("link_health_failures")
    if DuplicateCandidate.objects.filter(
        Q(app=app) | Q(candidate_app=app),
        status=DuplicateCandidate.Status.PENDING,
    ).exists():
        blockers.append("pending_duplicate_candidate")
    return blockers


def _explicit_capability_count(app: App) -> int:
    return (
        AppCapability.objects.filter(app=app)
        .exclude(value=AppCapability.CapabilityValue.UNKNOWN)
        .count()
    )


def _plan_auto_review(
    app: App,
    entries: list[NeedsReviewQueueEntry],
) -> ReviewPlan:
    plan = ReviewPlan(entry_ids=[entry.pk for entry in entries])
    for entry in entries:
        payload = entry.payload or {}
        if entry.kind != NeedsReviewQueueEntry.Kind.ENRICHED:
            plan.blockers.append(f"unsafe_review_kind:{entry.kind}")
            continue
        if payload.get("skipped_field_updates"):
            plan.blockers.append("review_has_skipped_field_updates")
        if payload.get("skipped_capability_updates"):
            plan.blockers.append("review_has_skipped_capability_updates")

        _plan_verdict(app, payload, plan)
        _plan_launch_status(app, payload, plan)
        _plan_pricing_model(app, payload, plan)
        _plan_scope_summary(app, payload, plan)
    return plan


def _plan_verdict(app: App, payload: dict[str, Any], plan: ReviewPlan) -> None:
    verdict = (payload.get("proposed_verdict") or "").strip()
    if not verdict:
        return
    if app.verdict and app.verdict != verdict:
        plan.blockers.append("review_would_overwrite_verdict")
        return
    if not app.verdict:
        planned_verdict = plan.app_updates.get("verdict")
        if planned_verdict and planned_verdict != verdict:
            plan.blockers.append("review_conflicting_verdict")
            return
        if not _is_high_information_verdict(verdict):
            plan.blockers.append("low_information_verdict")
            return
        plan.app_updates["verdict"] = verdict[:280]


def _plan_launch_status(app: App, payload: dict[str, Any], plan: ReviewPlan) -> None:
    launch_status = payload.get("proposed_launch_status")
    if not launch_status:
        return
    if launch_status not in App.LaunchStatus.values:
        plan.blockers.append("invalid_launch_status")
        return
    if launch_status == App.LaunchStatus.DEPRECATED:
        plan.blockers.append("review_proposes_deprecated")
        return
    if app.launch_status != launch_status:
        plan.blockers.append("review_would_change_launch_status")


def _plan_pricing_model(app: App, payload: dict[str, Any], plan: ReviewPlan) -> None:
    pricing_model = payload.get("proposed_pricing_model")
    if not pricing_model:
        return
    if pricing_model not in App.PricingModel.values:
        plan.blockers.append("invalid_pricing_model")
        return
    if app.pricing_model == pricing_model:
        return
    if app.pricing_model != App.PricingModel.UNKNOWN:
        plan.blockers.append("review_would_overwrite_pricing_model")
        return
    planned_pricing = plan.app_updates.get("pricing_model")
    if planned_pricing and planned_pricing != pricing_model:
        plan.blockers.append("review_conflicting_pricing_model")
        return
    plan.app_updates["pricing_model"] = pricing_model


def _plan_scope_summary(app: App, payload: dict[str, Any], plan: ReviewPlan) -> None:
    scope_summary = (payload.get("proposed_scope_summary") or "").strip()
    if not scope_summary:
        return
    existing = app.platform_links.exclude(scope_summary="").exclude(
        scope_summary=scope_summary
    )
    if existing.exists():
        plan.blockers.append("review_would_overwrite_scope_summary")
        return
    if plan.platform_scope_summary and plan.platform_scope_summary != scope_summary:
        plan.blockers.append("review_conflicting_scope_summary")
        return
    plan.platform_scope_summary = scope_summary[:280]


def _is_high_information_verdict(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in LOW_INFORMATION_VERDICTS:
        return False
    return len(normalized) >= 40
