"""Domain services for the catalog app.

Service layer lives here because:
  * Views must stay HTTP-only.
  * Admin actions need to call the same business logic as the public flows.
  * Test surfaces are reduced: one function per business rule.

Architecture refs:
  * docs/architecture.md § 5.6 (recalc_quality_score, transition_to_published)
  * docs/business.md § 11.2 (publish checklist), § 12 (quality scoring)
"""
from __future__ import annotations

from collections.abc import Iterable

from django.db import transaction
from django.utils import timezone

from .models import App, AppCapability
from .trust import is_trusted_non_mcp_connector_app

# Quality scoring (business.md § 12).
# Predicates take an `App` (with the M2M relations prefetched is a plus) and
# return a delta to apply. Keep the table close to the spec so reviewers can
# diff one against the other at a glance.
QUALITY_RULES: list[tuple] = [
    # Three independent trust axes — each contributes separately.
    (lambda a: a.platform_verification_status == App.PlatformVerificationStatus.OFFICIAL, +15),
    (lambda a: a.editorial_review_status == App.EditorialReviewStatus.REVIEWED, +10),
    (lambda a: a.developer_claim_status == App.DeveloperClaimStatus.CLAIMED, +10),
    # Content completeness
    (lambda a: bool(a.official_page_url) and bool(a.install_url), +15),
    (lambda a: bool(a.verdict), +10),
    (lambda a: a.use_cases.count() >= 5, +10),
    (
        lambda a: AppCapability.objects.filter(app=a)
        .exclude(value=AppCapability.CapabilityValue.UNKNOWN)
        .count()
        >= 5,
        +10,
    ),
    (lambda a: bool(a.developer_name) and a.developer_name.lower() != "unknown", +5),
    (lambda a: bool(a.logo), +5),
    (
        lambda a: bool(a.last_checked_at)
        and (timezone.now() - a.last_checked_at).days < 30,
        +5,
    ),
    (lambda a: bool(a.repo_url), +5),
    # Penalties
    (lambda a: a.launch_status == App.LaunchStatus.DEPRECATED, -10),
    (
        lambda a: not a.sources.exclude(source_type="mcp_registry").exists(),
        -10,
    ),
    (lambda a: a.link_health.filter(consecutive_failures__gte=1).exists(), -5),
]


def recalc_quality_score(app: App) -> int:
    """Recompute and persist the quality score for a single app.

    Uses `Model.objects.filter(pk=...).update(...)` rather than `.save()` so
    we don't fire post_save signals (which would re-enqueue the search vector
    refresh on every quality change).
    """
    score = sum(delta for predicate, delta in QUALITY_RULES if predicate(app))
    score = max(0, min(100, score))
    if app.quality_score != score:
        App.objects.filter(pk=app.pk).update(quality_score=score)
        app.quality_score = score
    return score


def _validate_publish(app: App) -> list[str]:
    """Return a list of human-readable errors blocking publication.

    Mirrors the checklist in docs/business.md § 11.2 line-by-line.
    """
    errors: list[str] = []
    compact_connector = is_trusted_non_mcp_connector_app(app)
    if compact_connector:
        if len(app.short_description or "") < 20:
            errors.append("short_description must be >= 20 chars")
        if len(app.long_description or "") < 60:
            errors.append("long_description must be >= 60 chars")
    elif len(app.short_description or "") < 60:
        errors.append("short_description must be >= 60 chars")
    if not app.platforms.exists():
        errors.append("at least one platform required")
    if not app.categories.exists():
        errors.append("at least one category required")

    explicit_caps = (
        AppCapability.objects.filter(app=app)
        .exclude(value=AppCapability.CapabilityValue.UNKNOWN)
        .count()
    )
    minimum_caps = 2 if compact_connector else 3
    if explicit_caps < minimum_caps:
        errors.append(f"at least {minimum_caps} explicit capabilities required")

    if not (app.official_page_url or app.install_url):
        errors.append("official_page_url or install_url required")

    if app.editorial_review_status != App.EditorialReviewStatus.REVIEWED:
        errors.append("editorial review required before publishing")

    if app.platform_verification_status == App.PlatformVerificationStatus.UNKNOWN:
        errors.append(
            "platform_verification_status must be official or not_listed"
        )

    if app.platform_verification_status == App.PlatformVerificationStatus.OFFICIAL:
        if not app.has_official_platform_url:
            errors.append(
                "official platforms need an official_directory_url on AppPlatform"
            )

    return errors


def get_publish_checklist(app: App) -> list[dict]:
    """Return a green/red checklist for the admin sidebar.

    Each item is `{label, ok, hint}`. Editors see at a glance what's missing
    before clicking Publish.
    """
    checks: list[dict] = []

    def add(label: str, ok: bool, hint: str = "") -> None:
        checks.append({"label": label, "ok": bool(ok), "hint": hint})

    compact_connector = is_trusted_non_mcp_connector_app(app)
    if compact_connector:
        add(
            "compact connector description",
            len(app.short_description or "") >= 20
            and len(app.long_description or "") >= 60,
            "trusted official connector profile",
        )
    else:
        add(
            "short_description ≥ 60 chars",
            len(app.short_description or "") >= 60,
        )
    add("at least one platform", app.platforms.exists())
    add("at least one category", app.categories.exists())
    add(
        "≥ 2 explicit capabilities" if compact_connector else "≥ 3 explicit capabilities",
        AppCapability.objects.filter(app=app)
        .exclude(value=AppCapability.CapabilityValue.UNKNOWN)
        .count()
        >= (2 if compact_connector else 3),
    )
    add(
        "official_page_url or install_url",
        bool(app.official_page_url or app.install_url),
    )
    add(
        "editorial_review_status = reviewed",
        app.editorial_review_status == App.EditorialReviewStatus.REVIEWED,
    )
    add(
        "platform_verification_status is set",
        app.platform_verification_status
        != App.PlatformVerificationStatus.UNKNOWN,
    )
    if app.platform_verification_status == App.PlatformVerificationStatus.OFFICIAL:
        add(
            "official_directory_url on AppPlatform",
            app.has_official_platform_url,
        )
    return checks


@transaction.atomic
def transition_to_published(app: App, editor) -> None:
    """Strict gatekeeper for publishing an App.

    Raises ``ValueError`` listing every failed precondition. Side effects
    happen only after validation succeeds; the whole flow is wrapped in
    ``transaction.atomic`` so a partial publish state is impossible.
    """
    errors = _validate_publish(app)
    if errors:
        raise ValueError("Cannot publish: " + "; ".join(errors))

    app.status = App.AppStatus.PUBLISHED
    app.last_checked_at = timezone.now()
    app.save(update_fields=["status", "last_checked_at", "updated_at"])
    recalc_quality_score(app)


@transaction.atomic
def merge_use_cases(
    target_id: int, source_ids: Iterable[int]
) -> dict[str, int]:
    """Collapse one or more ``UseCase`` rows into ``target_id``.

    For every ``AppUseCase`` whose ``use_case`` is in ``source_ids``,
    re-point the row at ``target_id`` (or drop it if the target row
    already exists for that App — ``AppUseCase`` has a unique-together
    on ``(app, use_case)`` so duplicates can't coexist). Then delete
    the source ``UseCase`` rows. Returns counters for admin feedback.

    LLM-driven enrichment creates a long tail of near-synonym
    use-cases (``"Generate sales reports"`` vs ``"Sales report
    generation"`` vs ``"Generate a sales report"``) — three distinct
    slugs for one editorial concept. Without this tool the
    ``UseCase`` taxonomy bloats and faceted browsing becomes useless.
    """
    from .models import AppUseCase, UseCase

    source_ids = [int(pk) for pk in source_ids if int(pk) != target_id]
    if not source_ids:
        return {"reassigned": 0, "deduplicated": 0, "deleted_use_cases": 0}

    # PKs of UseCases that actually exist (drop bogus IDs silently
    # rather than 500ing the admin action).
    existing_target = UseCase.objects.filter(pk=target_id).exists()
    if not existing_target:
        raise ValueError(f"Target UseCase {target_id} does not exist")

    valid_source_ids = list(
        UseCase.objects.filter(pk__in=source_ids).values_list("pk", flat=True)
    )
    if not valid_source_ids:
        return {"reassigned": 0, "deduplicated": 0, "deleted_use_cases": 0}

    # Apps that already have the target — for those, source rows are
    # simply deleted (no point re-pointing into a duplicate that
    # unique_together would reject).
    apps_with_target = set(
        AppUseCase.objects.filter(use_case_id=target_id).values_list("app_id", flat=True)
    )

    source_rows = AppUseCase.objects.select_for_update().filter(
        use_case_id__in=valid_source_ids
    )

    # Collect every app whose use-case set changes — search_vector
    # includes use_case titles (Sprint 1, see apps/search/tasks.py),
    # and direct through-table writes skip the m2m_changed signal
    # that normally schedules the refresh.
    affected_app_ids: set[int] = set()
    reassigned = 0
    deduplicated = 0
    for row in source_rows:
        affected_app_ids.add(row.app_id)
        if row.app_id in apps_with_target:
            row.delete()
            deduplicated += 1
        else:
            row.use_case_id = target_id
            row.save(update_fields=["use_case"])
            apps_with_target.add(row.app_id)
            reassigned += 1

    deleted_use_cases, _ = UseCase.objects.filter(pk__in=valid_source_ids).delete()

    # Re-index every affected app after commit so the canonical
    # use-case title makes it into search_vector and the deleted
    # synonyms stop matching FTS queries.
    if affected_app_ids:
        from apps.search.tasks import refresh_search_vector_task

        def _refresh() -> None:
            for app_id in affected_app_ids:
                refresh_search_vector_task.delay(app_id)

        transaction.on_commit(_refresh)

    return {
        "reassigned": reassigned,
        "deduplicated": deduplicated,
        "deleted_use_cases": deleted_use_cases,
    }


@transaction.atomic
def recalc_quality_score_bulk(app_ids: Iterable[int]) -> int:
    """Recompute quality for a set of apps. Returns the number of changes.

    Used by admin bulk actions; keeps the per-app SQL traffic minimal by
    operating on prefetched rows.
    """
    from django.db.models import Prefetch

    changed = 0
    queryset = (
        App.objects.filter(pk__in=list(app_ids))
        .prefetch_related(
            "platforms",
            "categories",
            "use_cases",
            "sources",
            "link_health",
            Prefetch("appcapability_set"),
        )
    )
    for app in queryset:
        new_score = sum(delta for pred, delta in QUALITY_RULES if pred(app))
        new_score = max(0, min(100, new_score))
        if app.quality_score != new_score:
            App.objects.filter(pk=app.pk).update(quality_score=new_score)
            changed += 1
    return changed
