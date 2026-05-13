"""Backfill AppPlatform rows + platform_verification_status for MCP-imported apps.

This migration fixes drift introduced by the old apps/sources/tasks.py code
path that bypassed apps.sources.upsert: MCP-registry-imported Apps were created
without an AppPlatform row and with platform_verification_status='not_listed'
instead of 'official' (the MCP Registry IS the platform's official directory
according to docs/business.md § 6.5).

Operational guarantees:
  * Idempotent — re-running is a no-op (update_or_create + status flip is
    conditional on the previous state).
  * Conservative — only flips platform_verification_status for Apps where
    editorial_review_status == 'unreviewed'. Reviewed apps reflect an
    editor's deliberate choice and are left alone.
  * AppPlatform backfill runs for every MCP-imported app — a missing row is
    a bug, not an editorial decision.
  * Uses .update() rather than .save() to avoid firing search-vector refresh
    signals during the migration; the daily safety-net (apps.search.tasks
    .refresh_search_vectors_batch) will pick up the change.
  * Reverse is a no-op: we cannot distinguish backfilled rows from rows
    added later through legitimate ingest.
"""
from __future__ import annotations

from django.conf import settings
from django.db import migrations
from django.utils import timezone


def _build_metadata(payload: dict) -> dict:
    """Reconstruct AppPlatform.metadata from a stored registry payload.

    Mirrors apps.sources.mcp_registry.MCPRegistrySource._normalize so the
    backfilled rows are indistinguishable from rows created by future ingests.
    """
    transports = payload.get("transports") or {}
    install = payload.get("install") or {}
    repo = payload.get("repository") or {}

    transport_pick = next(
        (t for t in ("stdio", "sse", "http", "websocket") if transports.get(t)),
        None,
    )

    return {
        "protocol_version": payload.get("protocol_version"),
        "transport": transport_pick,
        "repository_url": repo.get("url"),
        "install_command": install.get("command"),
        "required_env_vars": install.get("env", []) or [],
    }


def _build_directory_url(external_id: str, base_url: str) -> str:
    if not external_id or not base_url:
        return ""
    return f"{base_url.rstrip('/')}/servers/{external_id}"


def backfill_mcp_apps(apps, schema_editor):
    App = apps.get_model("catalog", "App")
    Platform = apps.get_model("catalog", "Platform")
    AppPlatform = apps.get_model("catalog", "AppPlatform")
    Source = apps.get_model("sources", "Source")

    registry_base_url = getattr(settings, "MCP_REGISTRY_BASE_URL", "")

    mcp_platform = Platform.objects.filter(slug="mcp").first()
    if mcp_platform is None:
        # Fixtures haven't been loaded yet (fresh DB, refs-only deferred).
        # Nothing to backfill against; the next loaddata + ingest run will
        # produce correct rows via apps.sources.upsert.
        return

    mcp_sources = (
        Source.objects.filter(source_type="mcp_registry")
        .select_related("app")
    )

    seen_app_ids: set[int] = set()
    now = timezone.now()

    for src in mcp_sources:
        app = src.app
        if app is None or app.pk in seen_app_ids:
            continue
        seen_app_ids.add(app.pk)

        if (
            app.editorial_review_status == "unreviewed"
            and app.platform_verification_status in ("not_listed", "unknown")
        ):
            App.objects.filter(pk=app.pk).update(
                platform_verification_status="official",
            )

        payload = src.payload or {}
        metadata = _build_metadata(payload)
        directory_url = _build_directory_url(src.external_id, registry_base_url)

        AppPlatform.objects.update_or_create(
            app=app,
            platform=mcp_platform,
            defaults={
                "official_directory_url": directory_url,
                "metadata": metadata,
                "last_verified_on_platform_at": now,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("sources", "0001_initial"),
        ("catalog", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(backfill_mcp_apps, migrations.RunPython.noop),
    ]
