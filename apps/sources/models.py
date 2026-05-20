"""Catalog-source records and link-health tracking.

Architecture refs:
  * docs/architecture.md § 5.2 (Source)
  * docs/architecture.md § 5.4 (LinkCheckResult / LinkHealth)
  * docs/business.md § 11.3 (regular re-verification)
"""
from __future__ import annotations

from django.db import models
from django.db.models import Q, TextChoices
from django.utils import timezone


class Source(models.Model):
    """Tracks where a given catalog card came from.

    A single `App` can have multiple `Source` rows when the same product
    appears in several places (manual + MCP Registry, submission later
    enriched by an ingest). One row may be marked ``is_primary`` for UI.
    """

    class SourceType(TextChoices):
        MANUAL = "manual", "Manual"
        MCP_REGISTRY = "mcp_registry", "MCP Registry"
        SUBMISSION = "submission", "User submission"
        CHATGPT_DIRECTORY = "chatgpt_directory", "ChatGPT App Directory"
        CHATGPT_UNOFFICIAL = "chatgpt_unofficial", "ChatGPT unofficial discovery"
        CLAUDE_CONNECTORS = "claude_connectors", "Claude Connectors"
        AGENT_ENRICH = "agent_enrich", "Agent enrichment"
        RSS_DISCOVERY = "rss_discovery", "RSS discovery"
        GITHUB_MCP = "github_mcp", "GitHub MCP search"
        GEMINI_EXTENSIONS = "gemini_extensions", "Gemini Extensions"

    app = models.ForeignKey(
        "catalog.App", on_delete=models.CASCADE, related_name="sources"
    )
    source_type = models.CharField(max_length=40, choices=SourceType.choices)
    source_url = models.URLField(blank=True)
    external_id = models.CharField(max_length=200, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    fetched_at = models.DateTimeField(default=timezone.now)
    last_enriched_at = models.DateTimeField(null=True, blank=True)
    is_primary = models.BooleanField(default=False)
    is_active = models.BooleanField(
        default=True,
        help_text=(
            "Set to false when the source no longer references this app "
            "(e.g. server vanished from MCP Registry)."
        ),
    )

    class Meta:
        indexes = [
            models.Index(fields=["source_type", "external_id"]),
            models.Index(fields=["app", "source_type"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["source_type", "external_id"],
                name="source_dedupe_by_external_id",
                condition=~Q(external_id=""),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.app_id}:{self.source_type}:{self.external_id or '-'}"


class UnparsedRegistryRecord(models.Model):
    """Buffer for MCP Registry rows we couldn't normalize.

    Stored verbatim so an editor or developer can inspect and re-route them
    without re-fetching. Lives here so the admin queue is right next to the
    source plumbing.
    """

    payload = models.JSONField()
    error = models.TextField(blank=True)
    schema_version = models.CharField(max_length=20, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["-received_at"])]

    def __str__(self) -> str:
        return f"Unparsed registry record #{self.pk}"


class LinkCheckResult(models.Model):
    """One row per HTTP probe — the audit trail behind auto-deprecation."""

    class Target(TextChoices):
        OFFICIAL = "official", "Official page"
        INSTALL = "install", "Install URL"
        DIRECTORY = "directory", "Platform directory URL"
        REPO = "repo", "Repository URL"

    app = models.ForeignKey(
        "catalog.App",
        on_delete=models.CASCADE,
        related_name="link_checks",
    )
    target = models.CharField(max_length=20, choices=Target.choices)
    url = models.URLField()
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    ok = models.BooleanField()
    error_message = models.CharField(max_length=300, blank=True)
    checked_at = models.DateTimeField(auto_now_add=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["app", "target", "-checked_at"]),
            models.Index(fields=["-checked_at"]),
        ]


class LinkHealth(models.Model):
    """Rolling summary per (app, target) — drives auto-deprecate."""

    app = models.ForeignKey(
        "catalog.App",
        on_delete=models.CASCADE,
        related_name="link_health",
    )
    target = models.CharField(max_length=20, choices=LinkCheckResult.Target.choices)
    url = models.URLField()
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    last_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)
    last_failed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("app", "target")
        indexes = [models.Index(fields=["-consecutive_failures"])]

    def __str__(self) -> str:
        return f"{self.app_id}.{self.target} fails={self.consecutive_failures}"
