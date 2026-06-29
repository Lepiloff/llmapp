"""Trust-axis helpers for catalog source verification."""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from django.db import transaction
from django.db.models import Q

from apps.catalog.models import App, AppCapability, Capability
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
