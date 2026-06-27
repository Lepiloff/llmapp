"""Trust-axis helpers for catalog source verification."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from django.db import transaction
from django.db.models import Q

from apps.catalog.models import App
from apps.sources.models import Source

TRUSTED_NON_MCP_SOURCE_TYPES = (
    Source.SourceType.CLAUDE_CONNECTORS,
    Source.SourceType.CHATGPT_UNOFFICIAL,
)


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
