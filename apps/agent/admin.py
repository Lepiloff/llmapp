"""Admin observability for the LLM-pipeline agent.

Phase 1 surfaces the operational tables as read-only views; Phase 2
will add custom diff-rendering / "apply proposal" actions on
``NeedsReviewQueueEntry``.
"""
from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html

from .models import AgentRun, EnrichmentTask, LLMCallLog, NeedsReviewQueueEntry


@admin.register(AgentRun)
class AgentRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source_type",
        "status",
        "trigger",
        "started_at",
        "finished_at",
        "total_cost_usd",
        "triggered_by",
    )
    list_filter = ("status", "trigger", "source_type", "started_at")
    search_fields = ("source_type", "triggered_by")
    readonly_fields = (
        "source_type",
        "status",
        "trigger",
        "triggered_by",
        "started_at",
        "finished_at",
        "total_cost_usd",
        "stats",
        "error",
    )
    date_hierarchy = "started_at"

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(EnrichmentTask)
class EnrichmentTaskAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "run",
        "app",
        "status",
        "started_at",
        "finished_at",
    )
    list_filter = ("status", "started_at")
    search_fields = ("app__name", "app__slug", "source_url")
    readonly_fields = (
        "run",
        "app",
        "source_url",
        "status",
        "started_at",
        "finished_at",
        "error",
        "diff_summary",
    )
    date_hierarchy = "started_at"

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(LLMCallLog)
class LLMCallLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "task",
        "provider",
        "model",
        "prompt_version",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cost_usd",
        "latency_ms",
        "is_mock",
        "created_at",
    )
    list_filter = ("provider", "model", "is_mock", "created_at")
    search_fields = ("provider", "model", "prompt_version")
    readonly_fields = (
        "task",
        "provider",
        "model",
        "prompt_version",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cost_usd",
        "latency_ms",
        "is_mock",
        "created_at",
    )
    date_hierarchy = "created_at"

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(NeedsReviewQueueEntry)
class NeedsReviewQueueEntryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "app",
        "kind",
        "created_at",
        "resolved_at",
        "resolved_by",
        "payload_preview",
    )
    list_filter = ("kind", "resolved_at", "created_at")
    search_fields = ("app__name", "app__slug", "resolution_note")
    readonly_fields = ("app", "task", "kind", "payload", "created_at")
    date_hierarchy = "created_at"

    def payload_preview(self, obj: NeedsReviewQueueEntry) -> str:
        text = str(obj.payload)
        return format_html("<code>{}</code>", text[:120] + ("…" if len(text) > 120 else ""))

    payload_preview.short_description = "Payload"

    def has_add_permission(self, request) -> bool:
        return False
