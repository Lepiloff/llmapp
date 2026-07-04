"""Trust-axis helpers for catalog source verification."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from django.db import transaction
from django.db.models import Q

from apps.catalog.models import App, AppCapability, Capability, Category
from apps.sources.models import Source

TRUSTED_NON_MCP_SOURCE_TYPES = (
    Source.SourceType.CLAUDE_CONNECTORS,
    Source.SourceType.CHATGPT_UNOFFICIAL,
)
TRUSTED_CONNECTOR_CAPABILITY_DEFAULTS = {
    "remote_available": {
        "label": "Hosted / remote available",
        "description": "A hosted / remote endpoint is provided.",
        "sort_order": 60,
        "value": AppCapability.CapabilityValue.YES,
        "note": "Trusted cloud connector directory listing is hosted remotely.",
    },
    "local_setup_required": {
        "label": "Requires local setup",
        "description": "Requires local installation on the user's machine.",
        "sort_order": 50,
        "value": AppCapability.CapabilityValue.NO,
        "note": "Trusted cloud connector directory listing does not require local server setup.",
    },
}
TRUSTED_CONNECTOR_CATEGORY_DEFAULTS = {
    "health-wellness": {
        "name": "Health & Wellness",
        "sort_order": 110,
    },
}
TRUSTED_CONNECTOR_SOURCE_CATEGORY_MAP = {
    Source.SourceType.CLAUDE_CONNECTORS: {
        "health and wellness": "health-wellness",
        "healthcare": "health-wellness",
    },
}
TRUSTED_CONNECTOR_MIN_SHORT_DESCRIPTION_LENGTH = 20
TRUSTED_CONNECTOR_FALSE_MCP_LISTING_TYPES = ("mcp-server",)
TRUSTED_CONNECTOR_CLOUD_LISTING_TYPES = ("claude-connector", "chatgpt-app")


@dataclass(slots=True)
class TrustBackfillDecision:
    app_id: int
    slug: str
    name: str
    source_types: list[str]
    directory_urls: list[str]
    would_update: bool
    updated: bool = False

    def as_dict(self) -> dict:
        return {
            "app_id": self.app_id,
            "slug": self.slug,
            "name": self.name,
            "source_types": self.source_types,
            "directory_urls": self.directory_urls,
            "would_update": self.would_update,
            "updated": self.updated,
        }


@dataclass(slots=True)
class TrustCapabilityBackfillDecision:
    app_id: int
    slug: str
    name: str
    source_types: list[str]
    directory_urls: list[str]
    planned_updates: dict[str, str]
    skipped_existing: dict[str, str]
    updated_capabilities: list[str] = field(default_factory=list)

    @property
    def would_update(self) -> bool:
        return bool(self.planned_updates)

    @property
    def updated(self) -> bool:
        return bool(self.updated_capabilities)

    def as_dict(self) -> dict:
        return {
            "app_id": self.app_id,
            "slug": self.slug,
            "name": self.name,
            "source_types": self.source_types,
            "directory_urls": self.directory_urls,
            "would_update": self.would_update,
            "updated": self.updated,
            "planned_updates": self.planned_updates,
            "updated_capabilities": self.updated_capabilities,
            "skipped_existing": self.skipped_existing,
        }


@dataclass(slots=True)
class TrustCategoryBackfillDecision:
    app_id: int
    slug: str
    name: str
    source_types: list[str]
    directory_urls: list[str]
    source_categories: list[str]
    planned_categories: list[str]
    updated_categories: list[str] = field(default_factory=list)

    @property
    def would_update(self) -> bool:
        return bool(self.planned_categories)

    @property
    def updated(self) -> bool:
        return bool(self.updated_categories)

    def as_dict(self) -> dict:
        return {
            "app_id": self.app_id,
            "slug": self.slug,
            "name": self.name,
            "source_types": self.source_types,
            "directory_urls": self.directory_urls,
            "source_categories": self.source_categories,
            "planned_categories": self.planned_categories,
            "would_update": self.would_update,
            "updated": self.updated,
            "updated_categories": self.updated_categories,
        }


@dataclass(slots=True)
class TrustDescriptionBackfillDecision:
    app_id: int
    slug: str
    name: str
    source_types: list[str]
    directory_urls: list[str]
    current_short_description: str
    planned_short_description: str
    updated: bool = False

    @property
    def would_update(self) -> bool:
        return bool(self.planned_short_description)

    def as_dict(self) -> dict:
        return {
            "app_id": self.app_id,
            "slug": self.slug,
            "name": self.name,
            "source_types": self.source_types,
            "directory_urls": self.directory_urls,
            "current_short_description": self.current_short_description,
            "planned_short_description": self.planned_short_description,
            "would_update": self.would_update,
            "updated": self.updated,
        }


@dataclass(slots=True)
class TrustMcpTaxonomyRepairDecision:
    app_id: int
    slug: str
    name: str
    source_types: list[str]
    directory_urls: list[str]
    planned_remove_listing_types: list[str]
    updated_listing_types: list[str] = field(default_factory=list)

    @property
    def would_update(self) -> bool:
        return bool(self.planned_remove_listing_types)

    @property
    def updated(self) -> bool:
        return bool(self.updated_listing_types)

    def as_dict(self) -> dict:
        return {
            "app_id": self.app_id,
            "slug": self.slug,
            "name": self.name,
            "source_types": self.source_types,
            "directory_urls": self.directory_urls,
            "planned_remove_listing_types": self.planned_remove_listing_types,
            "would_update": self.would_update,
            "updated": self.updated,
            "updated_listing_types": self.updated_listing_types,
        }


@dataclass(slots=True)
class TrustLaunchStatusBackfillDecision:
    app_id: int
    slug: str
    name: str
    source_types: list[str]
    directory_urls: list[str]
    current_launch_status: str
    planned_launch_status: str
    updated: bool = False

    @property
    def would_update(self) -> bool:
        return bool(self.planned_launch_status)

    def as_dict(self) -> dict:
        return {
            "app_id": self.app_id,
            "slug": self.slug,
            "name": self.name,
            "source_types": self.source_types,
            "directory_urls": self.directory_urls,
            "current_launch_status": self.current_launch_status,
            "planned_launch_status": self.planned_launch_status,
            "would_update": self.would_update,
            "updated": self.updated,
        }


def platform_verification_for_source(
    source_type: str,
    *,
    official_directory_url: str = "",
) -> str:
    if source_type == Source.SourceType.MCP_REGISTRY:
        return App.PlatformVerificationStatus.OFFICIAL
    if is_trusted_official_directory_url(source_type, official_directory_url):
        return App.PlatformVerificationStatus.OFFICIAL
    return App.PlatformVerificationStatus.UNKNOWN


def is_trusted_official_directory_url(source_type: str, url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    if source_type == Source.SourceType.CLAUDE_CONNECTORS:
        return host in {"claude.com", "www.claude.com"} and (
            path == "/connectors" or path.startswith("/connectors/")
        )
    if source_type == Source.SourceType.CHATGPT_UNOFFICIAL:
        return host in {"chatgpt.com", "www.chatgpt.com"} and (
            path == "/apps" or path.startswith("/apps/")
        )
    return False


def is_trusted_non_mcp_connector_app(app: App) -> bool:
    if app.sources.filter(source_type=Source.SourceType.MCP_REGISTRY).exists():
        return False
    if app.platforms.filter(slug="mcp").exists():
        return False
    if app.listing_types.filter(slug="mcp-server").exists():
        return False

    source_types = list(
        app.sources.filter(
            source_type__in=TRUSTED_NON_MCP_SOURCE_TYPES,
            is_active=True,
        )
        .values_list("source_type", flat=True)
        .distinct()
    )
    directory_urls = [
        url
        for url in app.platform_links.values_list("official_directory_url", flat=True)
        if url
    ]
    return any(
        is_trusted_official_directory_url(source_type, url)
        for source_type in source_types
        for url in directory_urls
    )


def trusted_platform_verification_backfill(
    *,
    source_types: tuple[str, ...] = TRUSTED_NON_MCP_SOURCE_TYPES,
    limit: int = 100,
    apply: bool = False,
    include_mcp: bool = False,
) -> dict:
    decisions = list(_trusted_backfill_decisions(source_types, limit, include_mcp))
    if apply:
        for decision in decisions:
            if decision.would_update:
                decision.updated = _apply_trust_backfill(decision.app_id)
    return {
        "apply": apply,
        "include_mcp": include_mcp,
        "source_types": list(source_types),
        "evaluated": len(decisions),
        "would_update": sum(item.would_update for item in decisions),
        "updated": sum(item.updated for item in decisions),
        "results": [item.as_dict() for item in decisions],
    }


def trusted_connector_capabilities_backfill(
    *,
    source_types: tuple[str, ...] = TRUSTED_NON_MCP_SOURCE_TYPES,
    limit: int = 100,
    apply: bool = False,
    include_mcp: bool = False,
) -> dict:
    decisions = list(
        _trusted_connector_capability_decisions(source_types, limit, include_mcp)
    )
    if apply:
        for decision in decisions:
            decision.updated_capabilities = _apply_trusted_capability_backfill(
                decision.app_id,
                decision.planned_updates,
            )
    return {
        "apply": apply,
        "include_mcp": include_mcp,
        "source_types": list(source_types),
        "evaluated": len(decisions),
        "would_update": sum(item.would_update for item in decisions),
        "updated": sum(item.updated for item in decisions),
        "updated_capabilities": sum(
            len(item.updated_capabilities) for item in decisions
        ),
        "results": [item.as_dict() for item in decisions],
    }


def trusted_connector_categories_backfill(
    *,
    source_types: tuple[str, ...] = TRUSTED_NON_MCP_SOURCE_TYPES,
    limit: int = 100,
    apply: bool = False,
    include_mcp: bool = False,
) -> dict:
    decisions = list(
        _trusted_connector_category_decisions(source_types, limit, include_mcp)
    )
    if apply:
        for decision in decisions:
            decision.updated_categories = _apply_trusted_category_backfill(
                decision.app_id,
                decision.planned_categories,
            )
    return {
        "apply": apply,
        "include_mcp": include_mcp,
        "source_types": list(source_types),
        "evaluated": len(decisions),
        "would_update": sum(item.would_update for item in decisions),
        "updated": sum(item.updated for item in decisions),
        "updated_categories": sum(len(item.updated_categories) for item in decisions),
        "results": [item.as_dict() for item in decisions],
    }


def trusted_connector_descriptions_backfill(
    *,
    source_types: tuple[str, ...] = TRUSTED_NON_MCP_SOURCE_TYPES,
    limit: int = 100,
    apply: bool = False,
    include_mcp: bool = False,
) -> dict:
    decisions = list(
        _trusted_connector_description_decisions(source_types, limit, include_mcp)
    )
    if apply:
        for decision in decisions:
            if decision.would_update:
                decision.updated = _apply_trusted_description_backfill(
                    decision.app_id,
                    decision.planned_short_description,
                )
    return {
        "apply": apply,
        "include_mcp": include_mcp,
        "source_types": list(source_types),
        "evaluated": len(decisions),
        "would_update": sum(item.would_update for item in decisions),
        "updated": sum(item.updated for item in decisions),
        "results": [item.as_dict() for item in decisions],
    }


def trusted_connector_mcp_taxonomy_repair(
    *,
    source_types: tuple[str, ...] = TRUSTED_NON_MCP_SOURCE_TYPES,
    limit: int = 100,
    apply: bool = False,
) -> dict:
    decisions = list(_trusted_connector_mcp_taxonomy_decisions(source_types, limit))
    if apply:
        for decision in decisions:
            if decision.would_update:
                decision.updated_listing_types = _apply_trusted_mcp_taxonomy_repair(
                    decision.app_id,
                    decision.planned_remove_listing_types,
                    source_types,
                )
    return {
        "apply": apply,
        "source_types": list(source_types),
        "evaluated": len(decisions),
        "would_update": sum(item.would_update for item in decisions),
        "updated": sum(item.updated for item in decisions),
        "updated_listing_types": sum(
            len(item.updated_listing_types) for item in decisions
        ),
        "results": [item.as_dict() for item in decisions],
    }


def trusted_connector_launch_statuses_backfill(
    *,
    source_types: tuple[str, ...] = TRUSTED_NON_MCP_SOURCE_TYPES,
    limit: int = 100,
    apply: bool = False,
) -> dict:
    decisions = list(_trusted_connector_launch_status_decisions(source_types, limit))
    if apply:
        for decision in decisions:
            if decision.would_update:
                decision.updated = _apply_trusted_launch_status_backfill(
                    decision.app_id,
                    decision.planned_launch_status,
                    source_types,
                )
    return {
        "apply": apply,
        "source_types": list(source_types),
        "evaluated": len(decisions),
        "would_update": sum(item.would_update for item in decisions),
        "updated": sum(item.updated for item in decisions),
        "results": [item.as_dict() for item in decisions],
    }


def _trusted_backfill_decisions(
    source_types: tuple[str, ...],
    limit: int,
    include_mcp: bool,
):
    queryset = (
        App.objects.filter(
            platform_verification_status=App.PlatformVerificationStatus.UNKNOWN,
            sources__source_type__in=source_types,
            sources__is_active=True,
        )
        .filter(_trusted_directory_query(source_types))
        .prefetch_related("sources", "platform_links")
        .distinct()
        .order_by("first_seen_at", "pk")
    )
    if not include_mcp:
        queryset = queryset.exclude(
            Q(sources__source_type=Source.SourceType.MCP_REGISTRY)
            | Q(platforms__slug="mcp")
            | Q(listing_types__slug="mcp-server")
        )

    for app in queryset[:limit]:
        source_values = list(
            app.sources.filter(source_type__in=source_types, is_active=True)
            .order_by("source_type")
            .values_list("source_type", flat=True)
            .distinct()
        )
        directory_urls = [
            url
            for url in app.platform_links.order_by("pk").values_list(
                "official_directory_url", flat=True
            )
            if url
        ]
        yield TrustBackfillDecision(
            app_id=app.pk,
            slug=app.slug,
            name=app.name,
            source_types=source_values,
            directory_urls=directory_urls,
            would_update=any(
                is_trusted_official_directory_url(source_type, url)
                for source_type in source_values
                for url in directory_urls
            ),
        )


def _trusted_connector_mcp_taxonomy_decisions(
    source_types: tuple[str, ...],
    limit: int,
):
    queryset = (
        App.objects.filter(
            sources__source_type__in=source_types,
            sources__is_active=True,
            listing_types__slug__in=TRUSTED_CONNECTOR_FALSE_MCP_LISTING_TYPES,
        )
        .filter(_trusted_directory_query(source_types))
        .filter(listing_types__slug__in=TRUSTED_CONNECTOR_CLOUD_LISTING_TYPES)
        .exclude(
            Q(sources__source_type=Source.SourceType.MCP_REGISTRY)
            | Q(platforms__slug="mcp")
        )
        .prefetch_related("sources", "platform_links", "listing_types")
        .distinct()
        .order_by("first_seen_at", "pk")
    )

    for app in queryset[:limit]:
        source_values = _trusted_source_values(app, source_types)
        directory_urls = _trusted_directory_urls(app)
        if not _has_trusted_directory(source_values, directory_urls):
            continue
        planned_remove = list(
            app.listing_types.filter(
                slug__in=TRUSTED_CONNECTOR_FALSE_MCP_LISTING_TYPES
            )
            .order_by("slug")
            .values_list("slug", flat=True)
        )
        yield TrustMcpTaxonomyRepairDecision(
            app_id=app.pk,
            slug=app.slug,
            name=app.name,
            source_types=source_values,
            directory_urls=directory_urls,
            planned_remove_listing_types=planned_remove,
        )


def _trusted_connector_launch_status_decisions(
    source_types: tuple[str, ...],
    limit: int,
):
    queryset = (
        App.objects.filter(
            sources__source_type__in=source_types,
            sources__is_active=True,
        )
        .filter(_trusted_directory_query(source_types))
        .exclude(
            Q(sources__source_type=Source.SourceType.MCP_REGISTRY)
            | Q(platforms__slug="mcp")
        )
        .prefetch_related("sources", "platform_links")
        .distinct()
        .order_by("first_seen_at", "pk")
    )

    for app in queryset[:limit]:
        source_values = _trusted_source_values(app, source_types)
        directory_urls = _trusted_directory_urls(app)
        if not _has_trusted_directory(source_values, directory_urls):
            continue
        source_text = _trusted_source_long_description(app, source_types)
        planned_status = _launch_status_from_source_text(source_text)
        if planned_status == app.launch_status:
            planned_status = ""
        yield TrustLaunchStatusBackfillDecision(
            app_id=app.pk,
            slug=app.slug,
            name=app.name,
            source_types=source_values,
            directory_urls=directory_urls,
            current_launch_status=app.launch_status,
            planned_launch_status=planned_status,
        )


def _trusted_connector_description_decisions(
    source_types: tuple[str, ...],
    limit: int,
    include_mcp: bool,
):
    queryset = (
        App.objects.filter(
            sources__source_type__in=source_types,
            sources__is_active=True,
        )
        .filter(_trusted_directory_query(source_types))
        .prefetch_related("sources", "platform_links")
        .distinct()
        .order_by("first_seen_at", "pk")
    )
    if not include_mcp:
        queryset = queryset.exclude(
            Q(sources__source_type=Source.SourceType.MCP_REGISTRY)
            | Q(platforms__slug="mcp")
            | Q(listing_types__slug="mcp-server")
        )

    for app in queryset[:limit]:
        source_values = list(
            app.sources.filter(source_type__in=source_types, is_active=True)
            .order_by("source_type")
            .values_list("source_type", flat=True)
            .distinct()
        )
        directory_urls = [
            url
            for url in app.platform_links.order_by("pk").values_list(
                "official_directory_url", flat=True
            )
            if url
        ]
        if not any(
            is_trusted_official_directory_url(source_type, url)
            for source_type in source_values
            for url in directory_urls
        ):
            continue

        current_short = (app.short_description or "").strip()
        planned_short = ""
        if len(current_short) < TRUSTED_CONNECTOR_MIN_SHORT_DESCRIPTION_LENGTH:
            source_long = _trusted_source_long_description(app, source_types)
            planned_short = _short_description_from_long_description(
                current_short=current_short,
                long_description=source_long or app.long_description,
            )

        yield TrustDescriptionBackfillDecision(
            app_id=app.pk,
            slug=app.slug,
            name=app.name,
            source_types=source_values,
            directory_urls=directory_urls,
            current_short_description=current_short,
            planned_short_description=planned_short,
        )


def _trusted_connector_category_decisions(
    source_types: tuple[str, ...],
    limit: int,
    include_mcp: bool,
):
    queryset = (
        App.objects.filter(
            sources__source_type__in=source_types,
            sources__is_active=True,
        )
        .filter(_trusted_directory_query(source_types))
        .prefetch_related("sources", "platform_links", "categories")
        .distinct()
        .order_by("first_seen_at", "pk")
    )
    if not include_mcp:
        queryset = queryset.exclude(
            Q(sources__source_type=Source.SourceType.MCP_REGISTRY)
            | Q(platforms__slug="mcp")
            | Q(listing_types__slug="mcp-server")
        )

    for app in queryset[:limit]:
        source_values = list(
            app.sources.filter(source_type__in=source_types, is_active=True)
            .order_by("source_type")
            .values_list("source_type", flat=True)
            .distinct()
        )
        directory_urls = [
            url
            for url in app.platform_links.order_by("pk").values_list(
                "official_directory_url", flat=True
            )
            if url
        ]
        if not any(
            is_trusted_official_directory_url(source_type, url)
            for source_type in source_values
            for url in directory_urls
        ):
            continue

        source_categories: list[str] = []
        planned_categories: list[str] = []
        existing_categories = set(app.categories.values_list("slug", flat=True))
        for source in app.sources.filter(source_type__in=source_types, is_active=True):
            payload = source.payload or {}
            raw_categories = list((payload.get("card") or {}).get("categories") or [])
            raw_categories += list(payload.get("unmapped_categories") or [])
            mapping = TRUSTED_CONNECTOR_SOURCE_CATEGORY_MAP.get(source.source_type, {})
            for category in raw_categories:
                if category:
                    source_categories.append(str(category))
                slug = mapping.get(_normalize_source_category(category))
                if (
                    slug
                    and slug not in existing_categories
                    and slug not in planned_categories
                ):
                    planned_categories.append(slug)

        yield TrustCategoryBackfillDecision(
            app_id=app.pk,
            slug=app.slug,
            name=app.name,
            source_types=source_values,
            directory_urls=directory_urls,
            source_categories=sorted(set(source_categories)),
            planned_categories=planned_categories,
        )


def _trusted_connector_capability_decisions(
    source_types: tuple[str, ...],
    limit: int,
    include_mcp: bool,
):
    capability_keys = tuple(TRUSTED_CONNECTOR_CAPABILITY_DEFAULTS)
    queryset = (
        App.objects.filter(
            sources__source_type__in=source_types,
            sources__is_active=True,
        )
        .filter(_trusted_directory_query(source_types))
        .prefetch_related("sources", "platform_links")
        .distinct()
        .order_by("first_seen_at", "pk")
    )
    if not include_mcp:
        queryset = queryset.exclude(
            Q(sources__source_type=Source.SourceType.MCP_REGISTRY)
            | Q(platforms__slug="mcp")
            | Q(listing_types__slug="mcp-server")
        )

    for app in queryset[:limit]:
        source_values = list(
            app.sources.filter(source_type__in=source_types, is_active=True)
            .order_by("source_type")
            .values_list("source_type", flat=True)
            .distinct()
        )
        directory_urls = [
            url
            for url in app.platform_links.order_by("pk").values_list(
                "official_directory_url", flat=True
            )
            if url
        ]
        if not any(
            is_trusted_official_directory_url(source_type, url)
            for source_type in source_values
            for url in directory_urls
        ):
            continue

        existing_values = {
            item.capability.key: item.value
            for item in AppCapability.objects.filter(
                app=app,
                capability__key__in=capability_keys,
            ).select_related("capability")
        }
        planned_updates: dict[str, str] = {}
        skipped_existing: dict[str, str] = {}
        for key, config in TRUSTED_CONNECTOR_CAPABILITY_DEFAULTS.items():
            target_value = config["value"]
            existing_value = existing_values.get(key)
            if existing_value in {"yes", "no"}:
                if existing_value != target_value:
                    skipped_existing[key] = existing_value
                continue
            planned_updates[key] = target_value

        yield TrustCapabilityBackfillDecision(
            app_id=app.pk,
            slug=app.slug,
            name=app.name,
            source_types=source_values,
            directory_urls=directory_urls,
            planned_updates=planned_updates,
            skipped_existing=skipped_existing,
        )


def _trusted_directory_query(source_types: tuple[str, ...]) -> Q:
    query = Q(pk__isnull=True)
    if Source.SourceType.CLAUDE_CONNECTORS in source_types:
        query |= Q(
            sources__source_type=Source.SourceType.CLAUDE_CONNECTORS,
            platform_links__official_directory_url__startswith=(
                "https://claude.com/connectors"
            ),
        )
    if Source.SourceType.CHATGPT_UNOFFICIAL in source_types:
        query |= Q(
            sources__source_type=Source.SourceType.CHATGPT_UNOFFICIAL,
            platform_links__official_directory_url__startswith=(
                "https://chatgpt.com/apps"
            ),
        )
    return query


@transaction.atomic
def _apply_trust_backfill(app_id: int) -> bool:
    updated = App.objects.filter(
        pk=app_id,
        platform_verification_status=App.PlatformVerificationStatus.UNKNOWN,
    ).update(platform_verification_status=App.PlatformVerificationStatus.OFFICIAL)
    return bool(updated)


@transaction.atomic
def _apply_trusted_mcp_taxonomy_repair(
    app_id: int,
    listing_type_slugs: list[str],
    source_types: tuple[str, ...],
) -> list[str]:
    app = App.objects.select_for_update().get(pk=app_id)
    if app.sources.filter(source_type=Source.SourceType.MCP_REGISTRY).exists():
        return []
    if app.platforms.filter(slug="mcp").exists():
        return []
    if not app.listing_types.filter(
        slug__in=TRUSTED_CONNECTOR_CLOUD_LISTING_TYPES
    ).exists():
        return []
    source_values = _trusted_source_values(app, source_types)
    directory_urls = _trusted_directory_urls(app)
    if not _has_trusted_directory(source_values, directory_urls):
        return []

    rows = list(app.listing_types.filter(slug__in=listing_type_slugs))
    if rows:
        app.listing_types.remove(*rows)
    return [row.slug for row in rows]


@transaction.atomic
def _apply_trusted_launch_status_backfill(
    app_id: int,
    planned_launch_status: str,
    source_types: tuple[str, ...],
) -> bool:
    if planned_launch_status not in App.LaunchStatus.values:
        return False
    app = App.objects.select_for_update().get(pk=app_id)
    if app.sources.filter(source_type=Source.SourceType.MCP_REGISTRY).exists():
        return False
    if app.platforms.filter(slug="mcp").exists():
        return False
    if app.launch_status == planned_launch_status:
        return False
    source_values = _trusted_source_values(app, source_types)
    directory_urls = _trusted_directory_urls(app)
    if not _has_trusted_directory(source_values, directory_urls):
        return False
    source_text = _trusted_source_long_description(app, source_types)
    if _launch_status_from_source_text(source_text) != planned_launch_status:
        return False
    app.launch_status = planned_launch_status
    app.save(update_fields=["launch_status", "updated_at"])
    return True


@transaction.atomic
def _apply_trusted_description_backfill(
    app_id: int,
    planned_short_description: str,
) -> bool:
    planned_short_description = planned_short_description.strip()[:280]
    if not planned_short_description:
        return False
    app = App.objects.select_for_update().get(pk=app_id)
    if len(app.short_description or "") >= TRUSTED_CONNECTOR_MIN_SHORT_DESCRIPTION_LENGTH:
        return False
    app.short_description = planned_short_description
    app.save(update_fields=["short_description", "updated_at"])
    return True


@transaction.atomic
def _apply_trusted_category_backfill(
    app_id: int,
    planned_categories: list[str],
) -> list[str]:
    updated: list[str] = []
    app = App.objects.select_for_update().get(pk=app_id)
    existing_categories = set(app.categories.values_list("slug", flat=True))
    for slug in planned_categories:
        config = TRUSTED_CONNECTOR_CATEGORY_DEFAULTS.get(slug)
        if config:
            category, _ = Category.objects.get_or_create(
                slug=slug,
                defaults={
                    "name": config["name"],
                    "sort_order": config["sort_order"],
                },
            )
        else:
            category = Category.objects.filter(slug=slug).first()
            if category is None:
                continue
        if slug in existing_categories:
            continue
        app.categories.add(category)
        existing_categories.add(slug)
        updated.append(slug)
    return updated


@transaction.atomic
def _apply_trusted_capability_backfill(
    app_id: int,
    planned_updates: dict[str, str],
) -> list[str]:
    updated: list[str] = []
    for key, value in planned_updates.items():
        config = TRUSTED_CONNECTOR_CAPABILITY_DEFAULTS[key]
        capability, _ = Capability.objects.get_or_create(
            key=key,
            defaults={
                "label": config["label"],
                "description": config["description"],
                "sort_order": config["sort_order"],
            },
        )
        app_capability, created = AppCapability.objects.get_or_create(
            app_id=app_id,
            capability=capability,
            defaults={
                "value": value,
                "note": config["note"],
            },
        )
        if created:
            updated.append(key)
            continue
        if app_capability.value != AppCapability.CapabilityValue.UNKNOWN:
            continue
        app_capability.value = value
        app_capability.note = config["note"]
        app_capability.save(update_fields=["value", "note"])
        updated.append(key)
    return updated


def _normalize_source_category(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _trusted_source_long_description(
    app: App,
    source_types: tuple[str, ...],
) -> str:
    for source in app.sources.filter(source_type__in=source_types, is_active=True):
        payload = source.payload or {}
        detail_long = ((payload.get("detail") or {}).get("long_description") or "").strip()
        if detail_long:
            return detail_long
    return ""


def _trusted_source_values(app: App, source_types: tuple[str, ...]) -> list[str]:
    return list(
        app.sources.filter(source_type__in=source_types, is_active=True)
        .order_by("source_type")
        .values_list("source_type", flat=True)
        .distinct()
    )


def _trusted_directory_urls(app: App) -> list[str]:
    return [
        url
        for url in app.platform_links.order_by("pk").values_list(
            "official_directory_url", flat=True
        )
        if url
    ]


def _has_trusted_directory(source_types: list[str], directory_urls: list[str]) -> bool:
    return any(
        is_trusted_official_directory_url(source_type, url)
        for source_type in source_types
        for url in directory_urls
    )


def _launch_status_from_source_text(source_text: str) -> str:
    normalized = " ".join((source_text or "").lower().split())
    if re.search(r"\bin beta\b", normalized):
        return App.LaunchStatus.BETA
    return ""


def _short_description_from_long_description(
    *,
    current_short: str,
    long_description: str,
) -> str:
    current_normalized = _normalize_description_line(current_short)
    for block in re.split(r"\n+", long_description or ""):
        candidate = _normalize_description_line(block)
        if not candidate or candidate.lower() == current_normalized.lower():
            continue
        candidate = _first_sentence(candidate)
        if len(candidate) >= TRUSTED_CONNECTOR_MIN_SHORT_DESCRIPTION_LENGTH:
            return candidate[:280]
    return ""


def _normalize_description_line(value: str) -> str:
    return " ".join((value or "").strip().split())


def _first_sentence(value: str) -> str:
    match = re.search(r"(?<=[.!?])\s+", value)
    if match:
        return value[: match.start()].strip()
    return value.strip()
