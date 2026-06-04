"""Split MCP Registry Source rows that were incorrectly merged by repo URL."""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.catalog.models import App
from apps.sources.base import AppDraft
from apps.sources.mcp_registry import MCPRegistrySchemaError, MCPRegistrySource
from apps.sources.models import Source
from apps.sources.upsert import (
    attach_capabilities,
    attach_categories,
    attach_listing_types,
    attach_platforms,
    attach_use_cases,
    unique_slug,
)


class Command(BaseCommand):
    help = (
        "Move non-primary MCP Registry Source rows from multi-source Apps "
        "into their own App records. Fixes historical monorepo over-merges."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many Source rows would be split without writing.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of Source rows to split.",
        )

    def handle(self, *args, **options) -> None:
        dry_run = bool(options["dry_run"])
        limit = options["limit"]
        qs = _candidate_sources()
        if limit is not None:
            qs = qs[:limit]

        candidates = list(qs)
        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"[DRY-RUN] mcp_registry_splits candidates={len(candidates)}"
                )
            )
            return

        counters = {"split": 0, "skipped": 0, "failed": 0}
        normalizer = MCPRegistrySource()
        for source in candidates:
            try:
                draft = normalizer._normalize(source.payload or {})
            except MCPRegistrySchemaError:
                counters["failed"] += 1
                continue
            outcome = _split_source(source.pk, draft)
            counters[outcome] += 1

        self.stdout.write(
            self.style.SUCCESS(
                "[APPLIED] mcp_registry_splits "
                f"split={counters['split']} skipped={counters['skipped']} "
                f"failed={counters['failed']}"
            )
        )


def _candidate_sources():
    multi_app_ids = (
        Source.objects.filter(source_type=Source.SourceType.MCP_REGISTRY)
        .values("app_id")
        .annotate(c=Count("id"))
        .filter(c__gt=1)
        .values_list("app_id", flat=True)
    )
    return (
        Source.objects.filter(
            source_type=Source.SourceType.MCP_REGISTRY,
            app_id__in=multi_app_ids,
            is_primary=False,
        )
        .select_related("app")
        .order_by("app_id", "external_id")
    )


@transaction.atomic
def _split_source(source_id: int, draft: AppDraft) -> str:
    source = Source.objects.select_for_update().select_related("app").get(pk=source_id)
    if source.is_primary:
        return "skipped"
    sibling_count = Source.objects.filter(
        app_id=source.app_id,
        source_type=Source.SourceType.MCP_REGISTRY,
    ).count()
    if sibling_count <= 1:
        return "skipped"

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
        platform_verification_status=App.PlatformVerificationStatus.OFFICIAL,
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

    source.app = app
    source.source_url = draft.official_directory_url or draft.official_page_url
    source.payload = draft.raw_payload
    source.fetched_at = timezone.now()
    source.is_primary = True
    source.is_active = True
    source.save(
        update_fields=[
            "app",
            "source_url",
            "payload",
            "fetched_at",
            "is_primary",
            "is_active",
        ]
    )
    return "split"
